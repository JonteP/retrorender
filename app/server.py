"""Retrorender: renders retro and chiptune files in real time over HTTP.

GET / (or /ui)                          web UI (login required)
GET /api/...                            web UI API (login required)
GET /health
GET /browse?path=<folder>               folders and playable files
GET /info?path=<file>[&policy params]   subtracks, metadata and planned play time
GET /render?path=<file>&sub=<n>[&fmt=wav|flac|mp3][&loops=&min_s=&max_s=&default_s=&fade_s=]
GET /art/<album id>                     album cover (no login; for network players)
GET /m3u?path=<folder or file>          playlist of render URLs (for VLC etc.)
GET /rescan                             re-index the library now
GET /catalog                            catalog status: duplicates (identical files), game keys
GET /meta/show?path=<folder or file>    show album.toml (generated if missing)
GET /meta/regenerate?path=<folder/file> regenerate one album.toml (an edited one is backed up)
GET /meta/regenerate-all[?path=<folder>] forced regeneration: first shows a warning,
                                        runs only with &confirm=yes (everything backed up)
GET /rest/...                           Subsonic API (Music Assistant, Subsonic apps)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tomllib
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from engines import EngineError, Registry
from catalog import Catalog, chips_for
from joshw import Joshw
from library import Library
from meta import MetaStore
from policy import plan, settings_for, shape
from subsonic import Subsonic
from web import WebUI

VERSION = "0.14.0"
BUILD = {"commit": os.environ.get("RETRO_BUILD_COMMIT", "local")[:7],
         "date": os.environ.get("RETRO_BUILD_DATE", "")}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

CONFIG_PATH = os.environ.get("RETRO_CONFIG", "/config/config.toml")
CFG: dict = tomllib.loads(Path(CONFIG_PATH).read_text()) if Path(CONFIG_PATH).is_file() else {}
LIBRARY = Path(os.environ.get("RETRO_LIBRARY", CFG.get("library", "/library"))).resolve()
SAMPLE_RATE = int(CFG.get("sample_rate", 44100))
REG = Registry(CFG)


@lru_cache(maxsize=8192)
def _probe(path: str, mtime: float, size: int):
    """Probe a file, using the SQLite catalog as a persistent cache."""
    P = Path(path)
    eng = REG.for_path(P)
    if eng is None:
        return None, []
    try:
        rel = str(P.relative_to(LIBRARY))
    except ValueError:
        rel = None
    tracks = CAT.get_probe(rel, size, mtime, eng.name) if rel else None
    if tracks is None:
        tracks = eng.probe(P)
        if rel:
            CAT.put_probe(rel, size, mtime, eng.name, tracks, chips_for(P, tracks))
    return eng, tracks


def probe(p: Path):
    st = p.stat()
    return _probe(str(p), st.st_mtime, st.st_size)


_meta_dir = os.environ.get("RETRO_META", CFG.get("meta_dir", "/meta"))
META = MetaStore(Path(_meta_dir).resolve() if _meta_dir else None, LIBRARY)
CAT = Catalog(META.root / "catalog.sqlite" if META.writable else None)
LIB = Library(LIBRARY, REG, CFG, probe, SAMPLE_RATE, META, CAT)
SUB = Subsonic(LIB, CFG)
JOSHW = Joshw(CFG, CAT, LIBRARY,
              on_change=lambda: threading.Thread(target=LIB.scan, daemon=True).start())
LIB.synced_platform = JOSHW.platform
SUB.on_star = JOSHW.pin_dir


def health() -> dict:
    return {"ok": True, "version": VERSION, "build": BUILD, "update": UPD.status(),
            "library": str(LIBRARY),
            "meta": {"dir": str(META.root) if META.enabled else None,
                     "writable": META.writable},
            "catalog": CAT.stats(),
            "index": {"scanning": LIB.scanning, "hashing": LIB._hashing,
                      "artists": len(LIB.index.artists),
                      "albums": len(LIB.index.albums),
                      "genres": len(LIB.index.genres),
                      "songs": len(LIB.index.songs),
                      "built": LIB.index.built},
            "engines": [e.name for e in REG.engines],
            "missing": REG.missing,
            "joshw": {"enabled": JOSHW.enabled, "systems": JOSHW.systems},
            "diagnostics": REG.diagnostics()}


from updates import UpdateCheck  # noqa: E402
UPD = UpdateCheck(CFG, BUILD)
from playlists import Playlists  # noqa: E402
PLAYLISTS = Playlists(META.root if META.enabled else None, META.writable, lambda: LIB.index)
SUB.playlists = PLAYLISTS
WEB = WebUI(CFG, LIB, META, CAT, health, probe, JOSHW, playlists=PLAYLISTS, registry=REG)


def wav_header(frames: int, rate: int) -> bytes:
    data = frames * 4
    return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
            + b"data" + struct.pack("<I", data))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = f"retrorender/{VERSION}"

    # --- helpers --------------------------------------------------------
    def _q(self) -> dict:
        multi = parse_qs(urlparse(self.path).query)
        q = {k: v[-1] for k, v in multi.items()}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n and "form-urlencoded" in (self.headers.get("Content-Type") or ""):
                self._raw_body = self.rfile.read(n)       # kept for handlers that want the body
                form = parse_qs(self._raw_body.decode("utf-8", "replace"))
                for k, v in form.items():
                    q.setdefault(k, v[-1])     # query parameters win
                    multi.setdefault(k, v)
        # repeated parameters (Subsonic's songId=…&songId=…) as lists under "key[]"
        q.update({k + "[]": v for k, v in multi.items() if not k.endswith("[]")})
        return q

    def _resolve(self, rel: str) -> Path:
        p = (LIBRARY / rel.lstrip("/")).resolve()
        if p != LIBRARY and not p.is_relative_to(LIBRARY):
            raise PermissionError("outside the library")
        if not p.exists():
            raise FileNotFoundError(rel)
        return p

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _base(self) -> str:
        host = self.headers.get("Host") or f"localhost:{self.server.server_port}"
        return f"http://{host}"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/rest/" in msg:            # keep credentials out of the log
            msg = re.sub(r"\?[^ ]*", "", msg, count=1)
        sys.stderr.write("%s %s\n" % (self.address_string(), msg))

    # --- routing ----------------------------------------------------------
    def do_GET(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        q = self._q()
        if route.startswith("/rest/"):
            try:
                return SUB.handle(self, route[len("/rest/"):], q)
            except (BrokenPipeError, ConnectionResetError):
                return
        try:
            if WEB.handle(self, route, q):
                return
            if route in ("/rescan", "/meta/regenerate", "/meta/regenerate-all") \
                    and not WEB.authorized(self):
                return WEB.challenge(self)              # changes require login
            if route == "/health":
                return self._json(health())
            if route == "/browse":
                return self.browse(q)
            if route == "/info":
                return self.info(q)
            if route == "/render":
                return self.render(q)
            if route.startswith("/art/"):
                return self.art(route[5:])
            if route == "/m3u":
                return self.m3u(q)
            if route == "/rescan":
                threading.Thread(target=LIB.scan, daemon=True).start()
                return self._json({"ok": True, "message": "re-indexing started"})
            if route == "/catalog":
                return self._json(LIB.catalog_report())
            if route == "/meta/show":
                return self.meta_show(q, regenerate=False)
            if route == "/meta/regenerate":
                return self.meta_show(q, regenerate=True)
            if route == "/meta/regenerate-all":
                return self.meta_regenerate_all(q)
            return self._json({"error": "unknown route", "routes": __doc__}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"not found: {e}"}, 404)
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except (EngineError, ValueError, IndexError) as e:
            return self._json({"error": str(e)}, 422)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the client (e.g. a Chromecast) closed the stream

    do_POST = do_GET

    def browse(self, q):
        d = self._resolve(q.get("path", ""))
        if not d.is_dir():
            raise ValueError("not a folder")
        dirs, files = [], []
        for e in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            rel = str(e.relative_to(LIBRARY))
            if e.is_dir():
                dirs.append(rel)
            elif (hint := REG.engine_hint(e)):
                files.append({"path": rel, "engine": hint})
        return self._json({"path": str(d.relative_to(LIBRARY)), "dirs": dirs, "files": files})

    def info(self, q):
        p = self._resolve(q["path"])
        eng, tracks = probe(p)
        if eng is None:
            raise ValueError("format not supported")
        out = []
        for t0 in tracks:
            _, t, s, entry, _ = LIB.resolve(p, t0.index, q)
            pl = plan(t, s)
            d = t.to_dict()
            d.update(planned_s=round(pl.seconds, 3), fade_s=pl.fade_s, reason=pl.reason,
                     settings=s, meta=entry or None,
                     url=f"{self._base()}/render?path={quote(q['path'])}&sub={t.index}")
            out.append(d)
        return self._json({"path": q["path"], "engine": eng.name,
                           "album_key": LIB.album_key(p, len(tracks)), "tracks": out})

    def _dlna_headers(self):
        # DLNA renderers (e.g. LG TVs) want these on media responses
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.send_header("Accept-Ranges", "none")

    def do_HEAD(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if route == "/render":
                return self.render(self._q(), head=True)
            if route.startswith("/art/"):
                return self.art(route[5:], head=True)
        except Exception:               # noqa: BLE001
            pass
        self.send_response(404)
        self.end_headers()

    def art(self, album_id: str, head: bool = False):
        """Album cover without login, for network players (album ids are not guessable
        paths and only the album's own cover image is served)."""
        al = LIB.index.albums.get(album_id)
        if al is None or not al.cover or not Path(al.cover).is_file():
            return self._json({"error": "no cover"}, 404)
        p = Path(al.cover)
        data = b"" if head else p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if p.suffix.lower() == ".png" else "image/jpeg")
        self.send_header("Content-Length", str(p.stat().st_size))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def render(self, q, head: bool = False):
        p = self._resolve(q["path"])
        eng, t, s, _, _ = LIB.resolve(p, int(q.get("sub", 0)), q)
        JOSHW.played(str(p.relative_to(LIBRARY)))
        pl = plan(t, s)
        frames = int(pl.seconds * SAMPLE_RATE)
        pcm = shape(eng.render(p, t, frames), frames, int(pl.fade_s * SAMPLE_RATE))
        title = (t.title or p.stem).encode("ascii", "replace").decode()

        fmt = q.get("fmt", "wav")
        if head:
            self.send_response(200)
            self.send_header("Content-Type", {"flac": "audio/flac", "mp3": "audio/mpeg"}.get(fmt, "audio/wav"))
            if fmt == "wav":
                self.send_header("Content-Length", str(44 + frames * 4))
            self._dlna_headers()
            self.end_headers()
            return
        if fmt in ("flac", "mp3"):
            ff = shutil.which("ffmpeg")
            if not ff:
                raise ValueError("ffmpeg is missing, so no FLAC/MP3")
            codec = ["-f", "flac"] if fmt == "flac" else ["-f", "mp3", "-b:a", "320k"]
            proc = subprocess.Popen(
                [ff, "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
                 "-i", "pipe:0", "-metadata", f"title={t.title}", *codec, "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            def feed():
                try:
                    for c in pcm:
                        proc.stdin.write(c)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            threading.Thread(target=feed, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac" if fmt == "flac" else "audio/mpeg")
            self.send_header("X-Title", title)
            self._dlna_headers()
            self.end_headers()
            try:
                while b := proc.stdout.read(65536):
                    self.wfile.write(b)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(44 + frames * 4))
        self.send_header("X-Title", title)
        self._dlna_headers()
        self.end_headers()
        self.wfile.write(wav_header(frames, SAMPLE_RATE))
        for c in pcm:
            self.wfile.write(c)

    def meta_show(self, q, regenerate: bool):
        """Show (and optionally regenerate) album.toml for a folder or file."""
        p = self._resolve(q.get("path", ""))
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.is_file() and REG.engine_hint(f)
                           not in (None, "missing"))
            if not files:
                raise ValueError("no playable files in the folder")
            singles = [f for f in files if len(probe(f)[1]) == 1]
            if not singles:
                raise ValueError("the folder only has files with subtracks; give a file instead")
            f = singles[0]
        else:
            f = p
        eng, tracks = probe(f)
        key = LIB.album_key(f, len(tracks))
        if not META.enabled:
            raise ValueError("no meta folder configured")
        if regenerate:
            if not META.writable:
                raise PermissionError("the meta folder is not writable")
            META.delete(key)
        LIB.ensure_meta(key, f, eng, tracks)
        mp = META.path_for(key)
        text = mp.read_text("utf-8") if mp and mp.is_file() else ""
        if regenerate:
            threading.Thread(target=LIB.scan, daemon=True).start()
        body = (f"# {mp}\n" + text).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def meta_regenerate_all(self, q):
        """Forced regeneration. Without confirm=yes: only a warning listing what is affected."""
        if not META.writable:
            raise PermissionError("the meta folder is not writable")
        under = q.get("path", "").strip("/")
        files = META.all_files(under)
        groups = META.classify(files)
        if q.get("confirm", "").lower() not in ("yes", "ja"):
            scope = f"under '{under}'" if under else "in the whole library"
            url = (f"{self._base()}/meta/regenerate-all?"
                   + (f"path={quote(under)}&" if under else "") + "confirm=yes")
            return self._json({
                "warning": (f"This removes ALL {len(files)} album.toml {scope} and generates "
                            f"new ones. {len(groups['edited'])} files were edited by hand and "
                            f"{len(groups['legacy'])} have no checksum (older version, may be "
                            "edited). Your edits leave the library, but every file is first "
                            "moved to a backup folder in /meta/.backup/."),
                "affected": {k: len(v) for k, v in groups.items()},
                "edited": groups["edited"],
                "legacy": groups["legacy"][:50],
                "confirm": url,
            })
        dest = META.backup_and_remove(files)
        threading.Thread(target=LIB.scan, daemon=True).start()
        return self._json({"ok": True, "moved": len(files), "backup": str(dest),
                           "message": "re-indexing started; new album.toml files are generated"})

    def m3u(self, q):
        p = self._resolve(q.get("path", ""))
        files = sorted((f for f in (p.rglob("*") if p.is_dir() else [p])
                        if f.is_file() and REG.engine_hint(f) not in (None, "missing")),
                       key=lambda f: str(f).lower())
        lines = ["#EXTM3U"]
        for f in files:
            try:
                eng, tracks = probe(f)
            except Exception:
                continue
            rel = str(f.relative_to(LIBRARY))
            for t0 in tracks:
                _, t, s, entry, _ = LIB.resolve(f, t0.index, q)
                if entry.get("skip"):
                    continue
                name = " - ".join(x for x in (t.author, t.title or f.stem) if x)
                lines.append(f"#EXTINF:{int(plan(t, s).seconds)},{name}")
                lines.append(f"{self._base()}/render?path={quote(rel)}&sub={t.index}")
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("RETRO_PORT", CFG.get("port", 8090)))
    print(f"retrorender: {LIBRARY} on :{port}; engines: "
          f"{[e.name for e in REG.engines]}, missing: {REG.missing}", flush=True)
    if CFG.get("subsonic", {}).get("enabled", True):
        LIB.start(float(CFG.get("subsonic", {}).get("rescan_minutes", 60)))
    if JOSHW.enabled:
        def refresh_loop():
            while True:
                JOSHW.refresh_all()               # each system at most once per refresh_hours
                time.sleep(3600)
        threading.Thread(target=refresh_loop, daemon=True, name="joshw-refresh").start()
    UPD.start()                               # "new version available" in the web UI
    from outputs import Outputs
    WEB.outputs = Outputs(CFG, LIB, port)     # Cast/DLNA players in the network
    WEB.outputs.start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
