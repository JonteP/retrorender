"""Web UI: serves the single-page admin interface and its JSON API.

Routes (all require HTTP Basic auth with the [web] or [subsonic] credentials):
  GET  /, /ui                     the page (app/web/index.html)
  GET  /api/status                version, engines, index and catalog stats
  GET  /api/platforms             consoles with album/song counts
  GET  /api/artists               composers with album counts
  GET  /api/albums?platform=&artist=&q=
  GET  /api/album/<id>            album details, tracks, album.toml text
  GET  /api/cover/<album id>      cover image
  GET  /api/catalog               identical files, hidden duplicates, editions
  GET  /api/regenerate-all?path=  preview of a forced regeneration (no changes)
  POST /api/album/<id>/toml       save album.toml (body = TOML text, validated)
  POST /api/album/<id>/regenerate regenerate one album.toml (edited file is backed up)
  POST /api/album/<id>/titles     save the album's titles.toml (file name -> track title)
  POST /api/regenerate-all?path=  forced regeneration (everything backed up first)
  POST /api/rescan                re-index now
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import re
import tomllib
from pathlib import Path
from urllib.parse import quote

from catalog import normalize_title
from meta import gen_status

log = logging.getLogger("web")

PAGE = Path(__file__).parent / "web" / "index.html"


class WebUI:
    def __init__(self, cfg: dict, lib, meta, cat, status_fn, probe, joshw=None):
        self.cfg = cfg
        self.lib = lib
        self.meta = meta
        self.cat = cat
        self.status_fn = status_fn
        self.probe = probe
        self.joshw = joshw
        web = cfg.get("web", {})
        sub = cfg.get("subsonic", {})
        self.user = str(web.get("user", sub.get("user", "musicassistant")))
        self.password = str(web.get("password", sub.get("password", "retro")))

    # --- auth ------------------------------------------------------------------------
    def authorized(self, h) -> bool:
        hdr = h.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (hmac.compare_digest(user, self.user)
                and hmac.compare_digest(pw, self.password))

    @staticmethod
    def challenge(h):
        body = b"Login required"
        h.send_response(401)
        h.send_header("WWW-Authenticate", 'Basic realm="retrorender", charset="UTF-8"')
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    # --- dispatch ----------------------------------------------------------------------
    def handle(self, h, route: str, q: dict) -> bool:
        """Returns True if the route belonged to the web UI (and was answered)."""
        if route not in ("/", "/ui") and not route.startswith("/api/"):
            return False
        if not self.authorized(h):
            self.challenge(h)
            return True
        if route in ("/", "/ui"):
            return self._page(h)
        parts = route[len("/api/"):].split("/")
        post = h.command == "POST"
        name = parts[0]
        if name == "status":
            return h._json(self.status_fn()) or True
        if name == "platforms":
            return h._json(self.platforms()) or True
        if name == "artists":
            return h._json(self.artists()) or True
        if name == "albums":
            return h._json(self.albums(q)) or True
        if name == "album" and len(parts) >= 2:
            al = self.lib.index.albums.get(parts[1])
            if al is None:
                return h._json({"error": "album not found"}, 404) or True
            action = parts[2] if len(parts) > 2 else ""
            if action == "" and not post:
                return h._json(self.album(al)) or True
            if action == "toml" and post:
                return h._json(*self.save_toml(al, h)) or True
            if action == "titles" and post:
                return h._json(*self.save_titles(al, h)) or True
            if action == "regenerate" and post:
                return h._json(self.regenerate(al)) or True
        if name == "cover" and len(parts) >= 2:
            return self._cover(h, parts[1])
        if name == "catalog":
            return h._json(self.lib.catalog_report()) or True
        if name == "rescan" and post:
            threading.Thread(target=self.lib.scan, daemon=True).start()
            return h._json({"ok": True}) or True
        if name == "joshw":
            return self._joshw(h, parts[1:], q, post)
        if name == "regenerate-all":
            return h._json(*self.regenerate_all(q, execute=post)) or True
        return h._json({"error": "unknown API route"}, 404) or True

    # --- views ---------------------------------------------------------------------------
    def _page(self, h) -> bool:
        data = PAGE.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)
        return True

    def _album_brief(self, al) -> dict:
        ix = self.lib.index
        return {"id": al.id, "name": al.name, "platform": al.platform, "year": al.year,
                "artists": [{"id": i, "name": n} for i, n in al.artists],
                "songs": len(al.songs),
                "duration": int(sum(ix.songs[s].duration for s in al.songs if s in ix.songs)),
                "cover": bool(al.cover), "region": al.region}

    def platforms(self) -> list:
        ix = self.lib.index
        out = []
        for g, ids in ix.genres.items():
            als = [ix.albums[i] for i in ids if i in ix.albums]
            out.append({"name": g, "albums": len(als), "songs": sum(len(a.songs) for a in als)})
        return sorted(out, key=lambda x: x["name"].lower())

    def artists(self) -> list:
        ix = self.lib.index
        return sorted(({"id": a.id, "name": a.name, "albums": len(a.albums)}
                       for a in ix.artists.values()), key=lambda x: x["name"].lower())

    def albums(self, q: dict) -> dict:
        ix = self.lib.index
        als = list(ix.albums.values())
        if q.get("platform"):
            als = [a for a in als if a.platform == q["platform"]]
        if q.get("artist"):
            als = [a for a in als if any(i == q["artist"] for i, _ in a.artists)]
        term = q.get("q", "").strip().lower()
        songs = []
        if term:
            def hit(text):
                return term in (text or "").lower()
            match = [a for a in als if hit(a.name) or hit(a.artist) or hit(a.platform)]
            ids = {a.id for a in als}
            songs = [s for s in ix.songs.values()
                     if s.album_id in ids and (hit(s.title) or hit(s.artist))][:200]
            als = match + [ix.albums[i] for i in {s.album_id for s in songs}
                           if ix.albums[i] not in match]
        als.sort(key=lambda a: (a.platform.lower(), a.name.lower()))
        return {"albums": [self._album_brief(a) for a in als],
                "songs": [{"id": s.id, "title": s.title, "artist": s.artist,
                           "album_id": s.album_id,
                           "album": ix.albums[s.album_id].name} for s in songs]}

    def album(self, al) -> dict:
        ix = self.lib.index
        tracks = []
        for sid in al.songs:
            s = ix.songs.get(sid)
            if not s:
                continue
            tracks.append({"id": s.id, "track": s.track, "title": s.title, "artist": s.artist,
                           "duration": round(s.duration, 1), "engine": s.engine,
                           "file": s.rel, "sub": s.sub,
                           "render": f"/render?path={quote(s.rel)}&sub={s.sub}"})
        toml_text, state = "", "missing"
        p = self.meta.path_for(al.key) if self.meta.enabled else None
        if p and p.is_file():
            toml_text = p.read_text("utf-8")
            ver, untouched = gen_status(toml_text)
            state = "untouched" if untouched else ("edited" if ver is not None else "legacy")
        titles_text = ""
        tp = self.meta.titles_path(al.key)
        m = re.search(r"Title map: built-in title map (\S+\.toml)", toml_text)
        builtin = m.group(1) if m else ""
        if tp and tp.is_file():
            titles_text = tp.read_text("utf-8")
        d = self._album_brief(al)
        d.update(titles=titles_text, titles_builtin=builtin, titles_template=self._titles_template(al),
                 tracks=tracks, key=al.key, game_key=al.game_key, chips=al.chips,
                 toml=toml_text, toml_state=state, toml_path=str(p) if p else None,
                 writable=self.meta.writable)
        return d

    def _cover(self, h, album_id: str) -> bool:
        al = self.lib.index.albums.get(album_id)
        if al is None or not al.cover or not Path(al.cover).is_file():
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        p = Path(al.cover)
        data = p.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", "image/png" if p.suffix.lower() == ".png" else "image/jpeg")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "max-age=3600")
        h.end_headers()
        h.wfile.write(data)
        return True

    # --- joshw ---------------------------------------------------------------------------
    def _joshw(self, h, parts: list, q: dict, post: bool) -> bool:
        j = self.joshw
        if j is None or not j.enabled:
            return h._json({"enabled": False}) or True
        if not parts or parts[0] == "status":
            return h._json(j.status()) or True
        if parts[0] == "search":
            total, rows = j.search(q.get("system", ""), q.get("q", ""), q.get("state", ""),
                                   int(q.get("offset", 0) or 0), min(int(q.get("limit", 100) or 100), 500))
            local = self._local_titles()
            for r in rows:
                r["platform"] = j.platform(r["system"])
                r["in_library"] = (r["platform"].lower(), normalize_title(r["title"])) in local
                r["albums"] = self._albums_under(r["local_dir"]) if r["local_dir"] else []
            return h._json({"total": total, "rows": rows}) or True
        if parts[0] == "refresh" and post:
            systems = [q["system"]] if q.get("system") else j.systems
            for s in systems:
                j.refresh(s, force=q.get("force") == "1")
            return h._json({"ok": True}) or True
        if parts[0].isdigit() and len(parts) > 1 and post:
            rid, action = int(parts[0]), parts[1]
            if not j.writable:
                return h._json({"error": "/library/_synced is not writable"}, 403) or True
            try:
                if action == "fetch":
                    return h._json(j.fetch(rid, pin=q.get("pin") == "1")) or True
                if action == "pin":
                    j.set_state(rid, "pinned")
                    return h._json(j.get(rid)) or True
                if action == "unpin":
                    j.set_state(rid, "temporary")
                    return h._json(j.get(rid)) or True
                if action == "remove":
                    j.remove(rid)
                    return h._json(j.get(rid)) or True
            except KeyError:
                return h._json({"error": "not found"}, 404) or True
        return h._json({"error": "unknown joshw route"}, 404) or True

    def _local_titles(self) -> set:
        """(console, normalized title) of albums outside _synced: 'already in the library'."""
        return {(a.platform.lower(), normalize_title(a.name.split(" (")[0]))
                for a in self.lib.index.albums.values()
                if not a.first_file.startswith("_synced/")}

    def _albums_under(self, local_dir: str) -> list:
        return [{"id": a.id, "name": a.name} for a in self.lib.index.albums.values()
                if a.first_file.startswith(local_dir.rstrip("/") + "/")]

    # --- changes -------------------------------------------------------------------------
    @staticmethod
    def _body(h) -> str:
        raw = getattr(h, "_raw_body", None)         # already read as a form post
        if raw is None:
            n = int(h.headers.get("Content-Length") or 0)
            raw = h.rfile.read(n) if n else b""
        return raw.decode("utf-8", "replace")

    def save_toml(self, al, h):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        text = self._body(h)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return {"error": f"invalid TOML: {e}"}, 422
        p = self.meta.path_for(al.key)
        if p.is_file():
            ver, untouched = gen_status(p.read_text("utf-8"))
            if not untouched:                      # keep a copy of earlier hand edits
                self.meta.backup_and_remove([p])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(p)
        self.meta._cache.pop(str(p), None)
        log.info("saved %s from the web UI", p)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"ok": True, "message": "saved; re-indexing"}, 200

    def _titles_template(self, al) -> str:
        from txtp import base_and_options
        ix = self.lib.index
        keys = []
        for sid in al.songs:
            s = ix.songs.get(sid)
            if s:
                k = base_and_options(Path(s.rel).name)[0]
                if k not in keys:
                    keys.append(k)
        lines = ["# Track titles for this album, keyed by internal file name (case-insensitive,",
                 "# without extension and TXTP options). This file is yours; retrorender never",
                 "# overwrites it. Saving regenerates an untouched album.toml.", "", "[titles]"]
        lines += [f'{k} = ""' for k in keys]
        return "\n".join(lines) + "\n"

    def save_titles(self, al, h):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        text = self._body(h)
        try:
            d = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return {"error": f"invalid TOML: {e}"}, 422
        if not isinstance(d.get("titles", {}), dict):
            return {"error": "[titles] must be a table"}, 422
        tp = self.meta.titles_path(al.key)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tmp = tp.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(tp)
        edited = not self.meta.untouched(al.key) and self.meta.path_for(al.key).is_file()
        if not edited:
            f = self.lib.root / al.first_file
            eng, tracks = self.probe(f)
            self.meta.delete(al.key)
            self.lib.ensure_meta(al.key, f, eng, tracks)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        msg = ("saved; album.toml is edited by hand, so it was kept — regenerate it to use the titles"
               if edited else "saved; album.toml regenerated, re-indexing")
        return {"ok": True, "edited": edited, "message": msg}, 200

    def regenerate(self, al) -> dict:
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}
        f = self.lib.root / al.first_file
        eng, tracks = self.probe(f)
        self.meta.delete(al.key)
        self.lib.ensure_meta(al.key, f, eng, tracks)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"ok": True, "message": "regenerated; re-indexing"}

    def regenerate_all(self, q: dict, execute: bool):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        under = q.get("path", "").strip("/")
        files = self.meta.all_files(under)
        groups = self.meta.classify(files)
        summary = {"files": len(files),
                   "untouched": len(groups["orörda"]),
                   "edited": groups["redigerade"],
                   "legacy": len(groups["utan_kontrollsumma"])}
        if not execute:
            return summary, 200
        dest = self.meta.backup_and_remove(files)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        summary.update(ok=True, backup=str(dest))
        return summary, 200
