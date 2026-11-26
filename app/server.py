"""Retrorender: renderar retro- och chiptunefiler i realtid över HTTP.

GET / (eller /ui)                       webbgränssnittet (kräver inloggning)
GET /api/...                            webbgränssnittets API (kräver inloggning)
GET /health
GET /browse?path=<mapp>                 mappar och spelbara filer
GET /info?path=<fil>[&policyparametrar] delspår, metadata och planerad speltid
GET /render?path=<fil>&sub=<n>[&fmt=wav|flac][&loops=&min_s=&max_s=&default_s=&fade_s=]
GET /m3u?path=<mapp eller fil>          spellista med render-URL:er (för VLC m.m.)
GET /rescan                             indexera om biblioteket nu
GET /catalog                            katalogstatus: dubbletter (identiska filer), spelnycklar
GET /meta/show?path=<mapp eller fil>    visa album.toml (genereras om den saknas)
GET /meta/regenerate?path=<mapp/fil>    generera om en album.toml (redigerad sparas i .backup)
GET /meta/regenerate-all[?path=<mapp>]  tvingad omgenerering: visar först en varning,
                                        utför först med &confirm=ja (allt sparas i .backup)
GET /rest/...                           Subsonic-API (för Music Assistant)
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

VERSION = "0.11.1"
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
    return {"ok": True, "version": VERSION, "build": BUILD, "library": str(LIBRARY),
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


WEB = WebUI(CFG, LIB, META, CAT, health, probe, JOSHW)


def wav_header(frames: int, rate: int) -> bytes:
    data = frames * 4
    return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
            + b"data" + struct.pack("<I", data))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = f"retrorender/{VERSION}"

    # --- hjälpare -------------------------------------------------------
    def _q(self) -> dict:
        q = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n and "form-urlencoded" in (self.headers.get("Content-Type") or ""):
                self._raw_body = self.rfile.read(n)       # kept for handlers that want the body
                form = parse_qs(self._raw_body.decode("utf-8", "replace"))
                for k, v in form.items():
                    q.setdefault(k, v[-1])     # query-parametrar vinner
        return q

    def _resolve(self, rel: str) -> Path:
        p = (LIBRARY / rel.lstrip("/")).resolve()
        if p != LIBRARY and not p.is_relative_to(LIBRARY):
            raise PermissionError("utanför biblioteket")
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
        if "/rest/" in msg:            # visa inte inloggningsuppgifter i loggen
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
            if route == "/m3u":
                return self.m3u(q)
            if route == "/rescan":
                threading.Thread(target=LIB.scan, daemon=True).start()
                return self._json({"ok": True, "message": "indexering startad"})
            if route == "/catalog":
                return self._json(LIB.catalog_report())
            if route == "/meta/show":
                return self.meta_show(q, regenerate=False)
            if route == "/meta/regenerate":
                return self.meta_show(q, regenerate=True)
            if route == "/meta/regenerate-all":
                return self.meta_regenerate_all(q)
            return self._json({"error": "okänd väg", "routes": __doc__}, 404)
        except FileNotFoundError as e:
            return self._json({"error": f"finns inte: {e}"}, 404)
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except (EngineError, ValueError, IndexError) as e:
            return self._json({"error": str(e)}, 422)
        except (BrokenPipeError, ConnectionResetError):
            pass   # klienten (t.ex. Chromecast) stängde strömmen

    do_POST = do_GET

    def browse(self, q):
        d = self._resolve(q.get("path", ""))
        if not d.is_dir():
            raise ValueError("inte en mapp")
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
            raise ValueError("inget stöd för formatet")
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

    def render(self, q):
        p = self._resolve(q["path"])
        eng, t, s, _, _ = LIB.resolve(p, int(q.get("sub", 0)), q)
        JOSHW.played(str(p.relative_to(LIBRARY)))
        pl = plan(t, s)
        frames = int(pl.seconds * SAMPLE_RATE)
        pcm = shape(eng.render(p, t, frames), frames, int(pl.fade_s * SAMPLE_RATE))
        title = (t.title or p.stem).encode("ascii", "replace").decode()

        if q.get("fmt", "wav") == "flac":
            ff = shutil.which("ffmpeg")
            if not ff:
                raise ValueError("ffmpeg saknas, FLAC går inte")
            proc = subprocess.Popen(
                [ff, "-v", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
                 "-i", "pipe:0", "-metadata", f"title={t.title}", "-f", "flac", "pipe:1"],
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
            self.send_header("Content-Type", "audio/flac")
            self.send_header("X-Title", title)
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
        self.end_headers()
        self.wfile.write(wav_header(frames, SAMPLE_RATE))
        for c in pcm:
            self.wfile.write(c)

    def meta_show(self, q, regenerate: bool):
        """Visa (och ev. generera om) album.toml för en mapp eller fil."""
        p = self._resolve(q.get("path", ""))
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.is_file() and REG.engine_hint(f)
                           not in (None, "saknas"))
            if not files:
                raise ValueError("inga spelbara filer i mappen")
            singles = [f for f in files if len(probe(f)[1]) == 1]
            if not singles:
                raise ValueError("mappen har bara filer med delspår; ange en fil i stället")
            f = singles[0]
        else:
            f = p
        eng, tracks = probe(f)
        key = LIB.album_key(f, len(tracks))
        if not META.enabled:
            raise ValueError("ingen metamapp konfigurerad")
        if regenerate:
            if not META.writable:
                raise PermissionError("metamappen är inte skrivbar")
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
        """Tvingad omgenerering. Utan confirm=ja: bara en varning med vad som påverkas."""
        if not META.writable:
            raise PermissionError("metamappen är inte skrivbar")
        under = q.get("path", "").strip("/")
        files = META.all_files(under)
        groups = META.classify(files)
        if q.get("confirm", "").lower() != "ja":
            scope = f"under '{under}'" if under else "i hela biblioteket"
            url = (f"{self._base()}/meta/regenerate-all?"
                   + (f"path={quote(under)}&" if under else "") + "confirm=ja")
            return self._json({
                "varning": (f"Detta tar bort ALLA {len(files)} album.toml {scope} och genererar "
                            f"nya. {len(groups['redigerade'])} filer har ändrats för hand och "
                            f"{len(groups['utan_kontrollsumma'])} saknar kontrollsumma (äldre "
                            "version, kan vara ändrade). Dina ändringar försvinner ur biblioteket, "
                            "men alla filer flyttas först till en backupmapp i /meta/.backup/."),
                "påverkas": {k: len(v) for k, v in groups.items()},
                "redigerade": groups["redigerade"],
                "utan_kontrollsumma": groups["utan_kontrollsumma"][:50],
                "bekräfta": url,
            })
        dest = META.backup_and_remove(files)
        threading.Thread(target=LIB.scan, daemon=True).start()
        return self._json({"ok": True, "flyttade": len(files), "backup": str(dest),
                           "message": "indexering startad; nya album.toml genereras"})

    def m3u(self, q):
        p = self._resolve(q.get("path", ""))
        files = sorted((f for f in (p.rglob("*") if p.is_dir() else [p])
                        if f.is_file() and REG.engine_hint(f) not in (None, "saknas")),
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
    print(f"retrorender: {LIBRARY} på :{port}; motorer: "
          f"{[e.name for e in REG.engines]}, saknas: {REG.missing}", flush=True)
    if CFG.get("subsonic", {}).get("enabled", True):
        LIB.start(float(CFG.get("subsonic", {}).get("rescan_minutes", 60)))
    if JOSHW.enabled:
        def refresh_loop():
            while True:
                JOSHW.refresh_all()               # each system at most once per refresh_hours
                time.sleep(3600)
        threading.Thread(target=refresh_loop, daemon=True, name="joshw-refresh").start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
