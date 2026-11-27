"""Web UI: serves the single-page admin interface and its JSON API.

Routes (all require HTTP Basic auth with the [web] or [subsonic] credentials):
  GET  /, /ui                     the page (app/web/index.html)
  GET  /api/status                version, engines, index and catalog stats
  GET  /api/platforms             consoles with album/song counts
  GET  /api/artists               composers with album counts
  GET  /api/facets                consoles, composers, years and sound chips with counts
  GET  /api/albums?platform=&artist=&year=&chip=&q=&sort=name|year|added|platform
  GET  /api/tracks?…              every track matching the same filters, or album=/playlist=
                                  (for "play all"), in album and track order
  GET  /api/album/<id>            album details, tracks, album.toml text
  GET  /api/cover/<album id>      cover image; ?original=1: the image to crop from (the one you
                                  uploaded, else the rip's own)
  POST /api/album/<id>/cover      ?source=1 keeps an image as the one to crop from
  POST /api/album/<id>/cover      upload a cover (body = JPEG or PNG), stored in /meta
  POST /api/album/<id>/cover/delete  remove an uploaded cover
  POST /api/song/<id>/title       rename one track {title}: titles.toml + album.toml
  GET  /api/settings             settings changed in the web UI (/meta/_settings.toml)
  POST /api/settings             {covers: {order, enabled, auto, libretro_type, shape, regions}}
  GET  /api/covers/status        the background cover fetch
  POST /api/covers/fetch         fetch missing covers now {retry: true = all again, also earlier
                                 misses and fetched covers (not ones you picked)}
  GET  /api/covers/remote?system=&path=   an image from libretro-thumbnails (for the editor)
  GET  /api/album/<id>/cover/candidates?q=&type=   covers from the sources for this album
  POST /api/album/<id>/cover/pick  {system, path, type, use: "cover" | "crop"}
  POST /api/album/<id>/cover/region {region: "Japan" | "" (automatic)}: release to prefer
  GET  /api/engines               what each engine can show and control (see Engine.describe)
  GET  /api/song/<id>/engine      the same for one track (voice names etc.)
  GET  /api/playlists             playlists (without songs)
  POST /api/playlists             create {name, songs: [song id], comment}
  GET  /api/playlist/<id>         one playlist with its tracks
  POST /api/playlist/<id>         change {name?, songs?, add?, remove_at?, comment?}
  POST /api/playlist/<id>/delete  remove it
  GET  /api/catalog               identical files, hidden duplicates, editions
  GET  /api/regenerate-all?path=  preview of a forced regeneration (no changes)
  POST /api/album/<id>/toml       save album.toml (body = TOML text, validated)
  POST /api/album/<id>/regenerate regenerate one album.toml (edited file is backed up)
  POST /api/album/<id>/titles     save the album's titles.toml (file name -> track title)
  GET  /api/album/<id>/tags.m3u   the album's titles as a vgmstream !tags.m3u (download)
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
    def __init__(self, cfg: dict, lib, meta, cat, status_fn, probe, joshw=None,
                 playlists=None, registry=None, settings=None, fetcher=None):
        self.cfg = cfg
        self.settings = settings
        self.fetcher = fetcher
        self.playlists = playlists
        self.registry = registry
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
        if name == "facets":
            return h._json(self.facets()) or True
        if name == "albums":
            return h._json(self.albums(q)) or True
        if name == "tracks":
            return h._json(*self.tracks(q)) or True
        if name == "engines":
            return h._json([e.describe() for e in self.registry.engines]
                           if self.registry else []) or True
        if name == "song" and len(parts) >= 3 and parts[2] == "engine":
            return h._json(*self.song_engine(parts[1])) or True
        if name == "song" and len(parts) >= 3 and parts[2] == "title" and post:
            return h._json(*self.set_title(parts[1], h)) or True
        if name == "settings":
            return h._json(*self.settings_api(h, post)) or True
        if name == "covers" and len(parts) > 1:
            return self.covers_api(h, parts[1], q, post)
        if name in ("playlists", "playlist"):
            return h._json(*self._playlists(h, parts, post)) or True
        if name == "album" and len(parts) >= 2:
            al = self.lib.index.albums.get(parts[1])
            if al is None:
                return h._json({"error": "album not found"}, 404) or True
            action = parts[2] if len(parts) > 2 else ""
            if action == "" and not post:
                return h._json(self.album(al)) or True
            try:          # album.toml, titles and export belong to one part (?part=n)
                part = self.lib.part_of(al, q.get("part"))
            except KeyError:
                return h._json({"error": "no such part"}, 404) or True
            if action == "toml" and post:
                return h._json(*self.save_toml(part, h)) or True
            if action == "tags.m3u" and not post:
                return self._tags_m3u(h, al, part)
            if action == "titles" and post:
                return h._json(*self.save_titles(part, h)) or True
            if action == "regenerate" and post:
                return h._json(self.regenerate(part)) or True
            if action == "cover" and len(parts) > 3 and parts[3] == "candidates":
                return h._json(*self.cover_candidates(al, q)) or True
            if action == "cover" and post and len(parts) > 3 and parts[3] == "region":
                return h._json(*self.cover_region(al, h)) or True
            if action == "cover" and post and len(parts) > 3 and parts[3] == "pick":
                return h._json(*self.cover_pick(al, h)) or True
            if action == "cover" and post:
                if len(parts) > 3 and parts[3] == "delete":
                    return h._json(*self.delete_cover(al, everything=bool(q.get("all")))) or True
                return h._json(*self.save_cover(al, h, source=bool(q.get("source")))) or True
        if name == "cover" and len(parts) >= 2:
            return self._cover(h, parts[1], original=bool(q.get("original")))
        if name == "catalog":
            return h._json(self.lib.catalog_report()) or True
        if name == "rescan" and post:
            threading.Thread(target=self.lib.scan, daemon=True).start()
            return h._json({"ok": True}) or True
        if name == "joshw":
            return self._joshw(h, parts[1:], q, post)
        if name == "outputs":
            return self._outputs(h, parts[1:], post)
        if name == "regenerate-all":
            return h._json(*self.regenerate_all(q, execute=post)) or True
        return h._json({"error": "unknown API route"}, 404) or True

    # --- network players ---------------------------------------------------------------
    outputs = None          # set by server.main (Cast/DLNA)

    def _outputs(self, h, parts, post) -> bool:
        out = self.outputs
        if out is None or not out.enabled:
            return h._json({"enabled": False, "devices": []}) or True
        try:
            if not parts or parts == [""]:
                return h._json({"enabled": True, "devices": out.list(),
                                "last_discovery": out.last_discovery}) or True
            if parts[0] == "discover" and post:
                out.discover()
                return h._json({"enabled": True, "devices": out.list(),
                                "last_discovery": out.last_discovery}) or True
            if len(parts) >= 2 and post:
                body = json.loads(self._body(h) or "{}")
                from urllib.parse import unquote
                dev, action = unquote(parts[0]), parts[1]
                if action == "play":
                    return h._json(out.play(dev, body.get("songs", []), int(body.get("start", 0)),
                                            str(body.get("repeat", "off")))) or True
                if action == "queue":
                    return h._json(out.queue(dev, body.get("songs", []),
                                             int(body.get("index", 0)))) or True
                return h._json(out.control(dev, action, body.get("value"))) or True
        except KeyError as e:
            return h._json({"error": str(e)}, 404) or True
        except ValueError as e:
            return h._json({"error": str(e)}, 400) or True
        except Exception as e:      # noqa: BLE001 - device errors go back to the page
            return h._json({"error": str(e)}, 502) or True
        return h._json({"error": "unknown outputs route"}, 404) or True

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
                "cover": bool(al.cover), "cover_rev": self.lib.cover_rev(al),
                "region": al.region, "chips": al.chips,
                "added": al.created}

    def song_brief(self, s) -> dict:
        """A track as the player needs it: enough to show it in a queue and play it."""
        al = self.lib.index.albums.get(s.album_id)
        return {"id": s.id, "track": s.track, "title": s.title, "artist": s.artist,
                "artists": [{"id": i, "name": n} for i, n in s.artists],
                "album": al.name if al else "", "album_id": s.album_id,
                "platform": s.platform, "cover": bool(al and al.cover),
                "cover_rev": self.lib.cover_rev(al) if al else "",
                "duration": round(s.duration, 1), "engine": s.engine,
                "file": s.rel, "sub": s.sub, "disc": s.disc,
                "render": f"/render?path={quote(s.rel)}&sub={s.sub}"}

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

    def facets(self) -> dict:
        """Everything the library can be browsed by, with album counts."""
        years: dict[int, int] = {}
        chips: dict[str, int] = {}
        for a in self.lib.index.albums.values():
            if a.year:
                years[a.year] = years.get(a.year, 0) + 1
            for c in set(a.chips):
                chips[c] = chips.get(c, 0) + 1
        return {"platforms": self.platforms(), "artists": self.artists(),
                "years": [{"year": y, "albums": n} for y, n in sorted(years.items())],
                "chips": [{"name": c, "albums": n}
                          for c, n in sorted(chips.items(), key=lambda x: x[0].lower())]}

    def _filtered(self, q: dict) -> tuple[list, list]:
        """(albums, matching songs) for the library filters."""
        ix = self.lib.index
        als = list(ix.albums.values())
        if q.get("platform"):
            als = [a for a in als if a.platform == q["platform"]]
        if q.get("artist"):
            als = [a for a in als if any(i == q["artist"] for i, _ in a.artists)
                   or any(any(i == q["artist"] for i, _ in ix.songs[s].artists)
                          for s in a.songs if s in ix.songs)]
        if q.get("year"):
            y = q["year"]
            if y.endswith("s") and y[:-1].isdigit():          # a decade: "1990s"
                d = int(y[:-1])
                als = [a for a in als if a.year and d <= a.year < d + 10]
            elif y.isdigit():
                als = [a for a in als if a.year == int(y)]
        if q.get("chip"):
            als = [a for a in als if q["chip"] in a.chips]
        term = q.get("q", "").strip().lower()
        songs = []
        if term:
            def hit(text):
                return term in (text or "").lower()
            match = [a for a in als if hit(a.name) or hit(a.artist) or hit(a.platform)]
            ids = {a.id for a in als}
            songs = [s for s in ix.songs.values()
                     if s.album_id in ids and (hit(s.title) or hit(s.artist))]
            als = match + [ix.albums[i] for i in dict.fromkeys(s.album_id for s in songs)
                           if ix.albums[i] not in match]
        key = {"year": lambda a: (a.year or 9999, a.name.lower()),
               "added": lambda a: a.created,
               "name": lambda a: a.name.lower()}.get(q.get("sort", ""),
                                                    lambda a: (a.platform.lower(), a.name.lower()))
        als.sort(key=key, reverse=q.get("sort") == "added")
        return als, songs

    def albums(self, q: dict) -> dict:
        ix = self.lib.index
        als, songs = self._filtered(q)
        return {"albums": [self._album_brief(a) for a in als],
                "songs": [self.song_brief(s) for s in songs[:200]]}

    MAX_TRACKS = 5000

    def tracks(self, q: dict):
        """Every track for "play all": an album, a playlist, or the library filters.
        With a search term, the matching tracks come first, then the rest of the
        matching albums."""
        ix = self.lib.index
        if q.get("playlist"):
            try:
                pl = self.playlists.get(q["playlist"])
            except (KeyError, AttributeError):
                return {"error": "no such playlist"}, 404
            songs = self.playlists.resolve(pl["songs"])
        elif q.get("album"):
            al = ix.albums.get(q["album"])
            if al is None:
                return {"error": "album not found"}, 404
            songs = [ix.songs[i] for i in al.songs if i in ix.songs]
        else:
            als, hits = self._filtered(q)
            songs = list(hits) if q.get("q") else []
            seen = {s.id for s in songs}
            if q.get("artist") and not q.get("q"):
                # a composer: their own tracks, not every track of a shared album
                aid = q["artist"]
                songs = [ix.songs[i] for a in als for i in a.songs if i in ix.songs
                         and any(x == aid for x, _ in ix.songs[i].artists)]
            elif not q.get("q") or q.get("whole_albums"):
                songs += [ix.songs[i] for a in als for i in a.songs
                          if i in ix.songs and i not in seen]
        total = len(songs)
        return {"total": total, "songs": [self.song_brief(s) for s in songs[:self.MAX_TRACKS]]}, 200

    def song_engine(self, sid: str):
        s = self.lib.index.songs.get(sid)
        if s is None:
            return {"error": "song not found"}, 404
        f = self.lib.root / s.rel
        try:
            eng, tracks = self.probe(f)
            t = next((x for x in tracks if x.index == s.sub), None)
            return eng.describe(f, t), 200
        except Exception as e:      # noqa: BLE001 - shown on the page
            return {"error": str(e)}, 500

    def set_title(self, sid: str, h):
        """Rename one track. The title goes to the album's titles.toml (so it survives
        regeneration) and straight into album.toml, also when that was edited by hand."""
        from txtp import base_and_options
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        s = self.lib.index.songs.get(sid)
        if s is None:
            return {"error": "song not found"}, 404
        try:
            title = str(json.loads(self._body(h) or "{}").get("title", "")).strip()
        except ValueError:
            return {"error": "bad request"}, 400
        if not title:
            return {"error": "the title is empty"}, 400
        f = self.lib.root / s.rel
        eng, tracks = self.probe(f)
        n = len(tracks)
        key = self.lib.album_key(f, n)           # the part's own album.toml
        base = base_and_options(f.name)[0]
        self.meta.set_user_title(key, f"{base}#{s.sub}" if n > 1 else base, title)
        if not self.meta.set_track_title(key, f.name, s.sub, n, title):
            self.lib.ensure_meta(key, f, eng, tracks)
        s.title = title                                 # shows at once; the scan follows
        threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"ok": True, "title": title}, 200

    # --- settings and cover sources ---------------------------------------------------------
    def settings_api(self, h, post):
        from settings import COVER_SOURCES, LIBRETRO_TYPES, SHAPES
        st = self.settings
        if st is None:
            return {"error": "no settings"}, 503
        if post:
            try:
                b = json.loads(self._body(h) or "{}")
                for sec, vals in b.items():
                    if sec != "covers" or not isinstance(vals, dict):
                        raise ValueError(f"unknown section {sec}")
                    st.update(sec, vals)
            except PermissionError as e:
                return {"error": str(e)}, 403
            except ValueError as e:
                return {"error": str(e)}, 400
        try:
            import PIL  # noqa: F401
            pillow = True
        except ImportError:
            pillow = False
        return {"covers": st.covers(), "writable": st.writable, "path": str(st.path or ""),
                "sources": COVER_SOURCES, "libretro_types": list(LIBRETRO_TYPES), "shapes": list(SHAPES),
                "pillow": pillow, "status": self.fetcher.status if self.fetcher else None}, 200

    def covers_api(self, h, what, q, post):
        f = self.fetcher
        if f is None:
            return h._json({"error": "cover fetching is off"}, 503) or True
        if what == "status":
            return h._json(f.status) or True
        if what == "fetch" and post:
            try:
                b = json.loads(self._body(h) or "{}")
            except ValueError:
                b = {}
            if b.get("retry"):
                f.forget_misses()
            f.start(manual=True, refresh=bool(b.get("retry")))
            return h._json({"ok": True, "status": f.status}) or True
        if what == "remote":
            system, path = q.get("system", ""), q.get("path", "")
            try:
                if not f.libretro.known(system, path):        # only images that are listed
                    return h._json({"error": "unknown image"}, 404) or True
                data = f.remote_image(system, path)
            except Exception as e:      # noqa: BLE001
                return h._json({"error": str(e)}, 502) or True
            h.send_response(200)
            h.send_header("Content-Type", "image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Cache-Control", "max-age=86400")
            h.end_headers()
            h.wfile.write(data)
            return True
        return h._json({"error": "unknown route"}, 404) or True

    def cover_candidates(self, al, q):
        f = self.fetcher
        if f is None:
            return {"candidates": []}, 200
        c = self.settings.covers()
        kind = q.get("type") or c["libretro_type"]
        if kind not in ("boxart", "title", "snap", "logo"):
            return {"error": "unknown type"}, 400
        sc = f.settings_for(al, c)
        try:
            if q.get("q"):
                hits = f.libretro.browse(al.platform, q["q"], kind, sc)
            else:
                hits = f.libretro.search(al.platform, f.names(al), kind, sc, limit=12)
        except Exception as e:      # noqa: BLE001 - shown in the editor
            return {"error": str(e), "candidates": []}, 200
        systems = f.libretro.systems_for(al.platform, c.get("libretro_systems", {}))
        for x in hits:
            x["url"] = f"/api/covers/remote?system={quote(x['system'])}&path={quote(x['path'])}"
            x["type"] = kind
        errors = [f.libretro.listing(x).get("error") for x in systems]
        return {"candidates": hits, "systems": systems, "type": kind, "prefer": sc["prefer"],
                "error": "; ".join(e for e in errors if e)}, 200

    def cover_region(self, al, h):
        """Choose which release's cover this album gets ("" = automatic), and fetch it."""
        if self.fetcher is None or not self.settings.writable:
            return {"error": "the meta folder is not writable"}, 403
        try:
            region = str(json.loads(self._body(h) or "{}").get("region", "")).strip()
        except ValueError:
            return {"error": "bad request"}, 400
        regions = dict(self.settings.covers().get("album_regions", {}))
        if region:
            regions[al.key] = region
        else:
            regions.pop(al.key, None)
        self.settings.update("covers", {"album_regions": regions})
        try:
            found = self.fetcher.refetch(al)
        except Exception as e:      # noqa: BLE001
            return {"error": str(e)}, 502
        return {"ok": True, "found": found, "info": self.fetcher.auto_info(al.key)}, 200

    def cover_pick(self, al, h):
        """Use an image from a source: as the album's cover (kept even if the automatic
        choice would differ), or as the image to crop from in the editor."""
        f = self.fetcher
        if f is None or not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        try:
            b = json.loads(self._body(h) or "{}")
            system, path = str(b.get("system", "")), str(b.get("path", ""))
            if not f.libretro.known(system, path):
                return {"error": "unknown image"}, 404
            data = f.remote_image(system, path)
        except ValueError as e:
            return {"error": str(e)}, 400
        except Exception as e:      # noqa: BLE001
            return {"error": str(e)}, 502
        kind = str(b.get("type") or "boxart")
        hit = {"system": system, "path": path, "name": path.split("/", 1)[-1].rsplit(".", 1)[0]}
        if b.get("use") == "crop":
            ext = "png" if data[:4] == b"\x89PNG" else "jpg"
            for e in ("jpg", "png"):
                self.lib.cover_target(al.key, e, "source").unlink(missing_ok=True)
            p = self.lib.cover_target(al.key, ext, "source")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return {"ok": True}, 200
        f.save(al, hit, kind, data, pinned=True)
        old = self.lib.uploaded_cover(al.key)        # your own cover would hide it
        if old:
            Path(old).unlink(missing_ok=True)
        al.covers.pop("upload", None)
        al.covers["auto"] = self.lib.auto_cover(al.key)
        self.lib.apply_covers(al)
        if al.cover_origin != "libretro":
            return {"ok": True, "note": "saved, but another source comes first on the Settings page"}, 200
        return {"ok": True}, 200

    # --- playlists -----------------------------------------------------------------------
    def _playlist_brief(self, pl: dict) -> dict:
        songs = self.playlists.resolve(pl["songs"])
        covers = list(dict.fromkeys(s.album_id for s in songs
                                    if self.lib.index.albums.get(s.album_id)
                                    and self.lib.index.albums[s.album_id].cover))[:4]
        return {"id": pl["id"], "name": pl["name"], "comment": pl.get("comment", ""),
                "created": pl.get("created"), "changed": pl.get("changed"),
                "songs": len(songs), "missing": len(pl["songs"]) - len(songs),
                "duration": int(sum(s.duration for s in songs)), "covers": covers}

    def _playlists(self, h, parts, post):
        st = self.playlists
        if st is None or not st.dir:
            return {"error": "playlists need a meta folder"}, 503
        try:
            if parts[0] == "playlists":
                if post:
                    b = json.loads(self._body(h) or "{}")
                    pl = st.create(str(b.get("name", "")), list(b.get("songs", [])),
                                   str(b.get("comment", "")))
                    return self._playlist_brief(pl), 200
                return {"writable": st.writable,
                        "playlists": [self._playlist_brief(p) for p in st.list()]}, 200
            pid = parts[1] if len(parts) > 1 else ""
            if len(parts) > 2 and parts[2] == "delete" and post:
                st.delete(pid)
                return {"ok": True}, 200
            if post:
                b = json.loads(self._body(h) or "{}")
                pl = st.update(pid, name=b.get("name"), song_ids=b.get("songs"),
                               add=b.get("add", []), remove_at=b.get("remove_at", []),
                               comment=b.get("comment"))
            else:
                pl = st.get(pid)
            d = self._playlist_brief(pl)
            d["tracks"] = [self.song_brief(s) for s in st.resolve(pl["songs"])]
            d["writable"] = st.writable
            return d, 200
        except KeyError:
            return {"error": "no such playlist"}, 404
        except PermissionError as e:
            return {"error": str(e)}, 403
        except ValueError as e:
            return {"error": f"bad request: {e}"}, 400

    def part_info(self, al, part) -> dict:
        """album.toml and titles.toml of one part of an album."""
        toml_text, state = "", "missing"
        p = self.meta.path_for(part.key) if self.meta.enabled else None
        if p and p.is_file():
            toml_text = p.read_text("utf-8")
            ver, untouched = gen_status(toml_text)
            state = "untouched" if untouched else ("edited" if ver is not None else "legacy")
        tp = self.meta.titles_path(part.key)
        m = re.search(r"Title map: built-in title map (\S+\.toml)", toml_text)
        return {"label": part.label, "key": part.key, "songs": len(part.songs),
                "toml": toml_text, "toml_state": state, "toml_path": str(p) if p else None,
                "titles": tp.read_text("utf-8") if tp and tp.is_file() else "",
                "titles_builtin": m.group(1) if m else "",
                "titles_template": self._titles_template(part)}

    def album(self, al) -> dict:
        ix = self.lib.index
        tracks = [self.song_brief(ix.songs[sid]) for sid in al.songs if sid in ix.songs]
        parts = [self.part_info(al, p) for p in (al.parts or [self.lib.part_of(al)])]
        d = self._album_brief(al)
        d.update(tracks=tracks, key=al.key, game_key=al.game_key, parts=parts,
                 discs=[{"disc": i, "label": p["label"]} for i, p in enumerate(parts, 1)]
                 if len(parts) > 1 else [],
                 cover_uploaded=bool(self.lib.uploaded_cover(al.key)),
                 cover_origin=al.cover_origin,
                 cover_info=self.fetcher.auto_info(al.key) if self.fetcher else None,
                 rip_cover=bool(al.rip_cover),
                 cover_source=bool(self.lib.uploaded_cover(al.key, "source")),
                 writable=self.meta.writable)
        d.update({k: parts[0][k] for k in ("toml", "toml_state", "toml_path", "titles",
                                           "titles_builtin", "titles_template")})
        return d

    def _cover(self, h, album_id: str, original: bool = False) -> bool:
        """The album's cover; original=True: the image to crop from (for the editor)."""
        al = self.lib.index.albums.get(album_id)
        img = self.lib.cover_image(al, original) if al else None
        if img is None:
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return True
        data, ctype, etag = img
        if h.headers.get("If-None-Match") == etag:      # a changed cover shows up at once
            h.send_response(304)
            h.send_header("ETag", etag)
            h.end_headers()
            return True
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-cache")
        h.send_header("ETag", etag)
        h.end_headers()
        h.wfile.write(data)
        return True

    MAX_COVER = 8 * 1024 * 1024

    def save_cover(self, al, h, source: bool = False):
        """The uploaded image goes to /meta next to album.toml and wins over the rip's own.
        source=True stores the image you chose, to crop from (it is not shown itself)."""
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        n = int(h.headers.get("Content-Length") or 0)
        if not 0 < n <= self.MAX_COVER:
            return {"error": "send a JPEG or PNG image of at most 8 MB"}, 413
        data = h.rfile.read(n)
        ext = ("png" if data[:8] == b"\x89PNG\r\n\x1a\n"
               else "jpg" if data[:3] == b"\xff\xd8\xff" else None)
        if ext is None:
            return {"error": "only JPEG and PNG images are supported"}, 415
        for e in ("jpg", "png"):                    # the other format of the same kind
            self.lib.cover_target(al.key, e, "source" if source else "").unlink(missing_ok=True)
        p = self.lib.cover_target(al.key, ext, "source" if source else "")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        if source:
            return {"ok": True}, 200
        al.covers["upload"] = str(p)
        self.lib.apply_covers(al)
        log.info("cover for %s saved to %s", al.name, p)
        return {"ok": True}, 200

    def delete_cover(self, al, everything: bool = False):
        """Removes the cover made from your image; everything=True also the image itself."""
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        if everything:
            src = self.lib.uploaded_cover(al.key, "source")
            if src:
                Path(src).unlink(missing_ok=True)
        old = self.lib.uploaded_cover(al.key)
        if old:
            Path(old).unlink(missing_ok=True)
            al.covers.pop("upload", None)
            self.lib.apply_covers(al)
            threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"ok": True}, 200

    def tags_m3u(self, al, part=None) -> tuple[str, str | None]:
        """(text, error). One line per file, so albums made of subtunes in one file don't fit."""
        ix = self.lib.index
        part = part or self.lib.part_of(al)
        songs = sorted((ix.songs[sid] for sid in part.songs if sid in ix.songs),
                       key=lambda s: (s.track, s.title.lower()))
        if not songs:
            return "", "the album has no tracks"
        if any(s.sub for s in songs) or len({s.rel for s in songs}) != len(songs):
            return "", "this album is subtunes in one file; !tags.m3u lists one file per track"
        folder = Path(songs[0].rel).parent
        if any(Path(s.rel).parent != folder for s in songs):
            return "", "the tracks are spread over several folders"
        from library import UNKNOWN
        artist = ", ".join(n for _, n in al.artists if n != UNKNOWN) or al.meta_artist
        m = re.search(r"Title map: built-in title map (\S+\.toml)",
                      self.part_info(al, part).get("toml", ""))
        L = [f"# @album {al.name}"]
        if artist:
            L.append(f"# @artist {artist}")
        if al.year:
            L.append(f"# @year {al.year}")
        L += ["#", "# Exported by retrorender (https://github.com/JonteP/retrorender)."]
        if m:
            L.append("# Track titles from Luma's Workshop (lumasworkshop.com)." if "mario-galaxy" in m.group(1)
                     else f"# Track titles from the built-in map {m.group(1)}.")
        L.append("")
        for i, so in enumerate(sorted(songs, key=lambda x: (x.track or 9999, x.rel)), 1):
            L.append(f"# %title {so.title}")
            if so.artist and so.artist != artist:
                L.append(f"# %artist {so.artist}")
            L.append(Path(so.rel).name)
        return "\n".join(L) + "\n", None

    def _tags_m3u(self, h, al, part=None) -> bool:
        text, err = self.tags_m3u(al, part)
        if err:
            return h._json({"error": err}, 422) or True
        data = text.encode("utf-8")
        h.send_response(200)
        h.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        h.send_header("Content-Disposition", 'attachment; filename="!tags.m3u"')
        h.send_header("Content-Length", str(len(data)))
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

    def save_toml(self, part, h):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        text = self._body(h)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return {"error": f"invalid TOML: {e}"}, 422
        p = self.meta.path_for(part.key)
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

    def _titles_template(self, part) -> str:
        from txtp import base_and_options
        ix = self.lib.index
        from meta import _tv
        keys = []
        for sid in part.songs:
            s = ix.songs.get(sid)
            if s:
                k = base_and_options(Path(s.rel).name)[0]
                if part.key.startswith("file:"):
                    k += f"#{s.sub}"                    # subtunes inside one file
                if k not in keys:
                    keys.append(k)
        lines = ["# Track titles for this album, keyed by internal file name (case-insensitive,",
                 "# without extension and TXTP options; 'name#2' is subtune 2 inside one file).",
                 "# This file is yours; retrorender never overwrites it.",
                 "# Saving regenerates an untouched album.toml.", "", "[titles]"]
        lines += [f'{_tv(k)} = ""' for k in keys]
        return "\n".join(lines) + "\n"

    def save_titles(self, part, h):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        text = self._body(h)
        try:
            d = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return {"error": f"invalid TOML: {e}"}, 422
        if not isinstance(d.get("titles", {}), dict):
            return {"error": "[titles] must be a table"}, 422
        tp = self.meta.titles_path(part.key)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tmp = tp.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(tp)
        edited = not self.meta.untouched(part.key) and self.meta.path_for(part.key).is_file()
        if not edited:
            f = self.lib.root / part.first_file
            eng, tracks = self.probe(f)
            self.meta.delete(part.key)
            self.lib.ensure_meta(part.key, f, eng, tracks)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        msg = ("saved; album.toml is edited by hand, so it was kept — regenerate it to use the titles"
               if edited else "saved; album.toml regenerated, re-indexing")
        return {"ok": True, "edited": edited, "message": msg}, 200

    def regenerate(self, part) -> dict:
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}
        f = self.lib.root / part.first_file
        eng, tracks = self.probe(f)
        self.meta.delete(part.key)
        self.lib.ensure_meta(part.key, f, eng, tracks)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"ok": True, "message": "regenerated; re-indexing"}

    def regenerate_all(self, q: dict, execute: bool):
        if not self.meta.writable:
            return {"error": "the meta folder is not writable"}, 403
        under = q.get("path", "").strip("/")
        files = self.meta.all_files(under)
        groups = self.meta.classify(files)
        summary = {"files": len(files),
                   "untouched": len(groups["untouched"]),
                   "edited": groups["edited"],
                   "legacy": len(groups["legacy"])}
        if not execute:
            return summary, 200
        dest = self.meta.backup_and_remove(files)
        threading.Thread(target=self.lib.scan, daemon=True).start()
        summary.update(ok=True, backup=str(dest))
        return summary, 200
