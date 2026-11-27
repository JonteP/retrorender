"""Cover art: fetching from online sources, and making covers square.

Sources (in the order set on the Settings page; a cover you made yourself always wins):
  rip       the rip's own image (found by the library scan)
  libretro  libretro-thumbnails, RetroArch's collection of box art, title screens,
            screenshots and logos per console, named after the No-Intro release names

A fetched cover is stored next to the album's album.toml in /meta as cover.auto.<ext>,
with cover.auto.json telling where it came from. Albums where nothing was found are
remembered in /meta/_cache/covers.json and tried again after MISS_DAYS days or when the
settings change.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from settings import LIBRETRO_TYPES

log = logging.getLogger("covers")

USER_AGENT = "retrorender (+https://github.com/JonteP/retrorender)"
MISS_DAYS = 30
LIST_DAYS = 7
MAX_IMAGE = 8 * 1024 * 1024

# our console names (config [platforms], joshw) -> libretro-thumbnails systems, best first
LIBRETRO_SYSTEMS = {
    "Nintendo NES": ["Nintendo - Nintendo Entertainment System", "Nintendo - Family Computer Disk System"],
    "Nintendo Famicom Disk System": ["Nintendo - Family Computer Disk System"],
    "Super Nintendo": ["Nintendo - Super Nintendo Entertainment System", "Nintendo - Satellaview"],
    "Nintendo 64": ["Nintendo - Nintendo 64"],
    "Nintendo GameCube": ["Nintendo - GameCube"],
    "Nintendo Wii": ["Nintendo - Wii"],
    "Nintendo Wii U": ["Nintendo - Wii U"],
    "Nintendo Game Boy": ["Nintendo - Game Boy", "Nintendo - Game Boy Color"],
    "Nintendo Game Boy Color": ["Nintendo - Game Boy Color", "Nintendo - Game Boy"],
    "Game Boy Advance": ["Nintendo - Game Boy Advance"],
    "Nintendo DS": ["Nintendo - Nintendo DS"],
    "Nintendo 3DS": ["Nintendo - Nintendo 3DS"],
    "Nintendo Virtual Boy": ["Nintendo - Virtual Boy"],
    "Sega Mega Drive": ["Sega - Mega Drive - Genesis", "Sega - Mega-CD - Sega CD", "Sega - 32X"],
    "Sega Master System": ["Sega - Master System - Mark III"],
    "Sega Game Gear": ["Sega - Game Gear"],
    "Sega Saturn": ["Sega - Saturn"],
    "Sega Dreamcast": ["Sega - Dreamcast"],
    "PlayStation": ["Sony - PlayStation"],
    "PlayStation 2": ["Sony - PlayStation 2"],
    "PlayStation 3": ["Sony - PlayStation 3"],
    "PlayStation Portable": ["Sony - PlayStation Portable"],
    "NEC PC Engine": ["NEC - PC Engine - TurboGrafx 16", "NEC - PC Engine CD - TurboGrafx-CD"],
    "PC-98": ["NEC - PC-98"],
    "Sharp X68000": ["Sharp - X68000"],
    "MSX": ["Microsoft - MSX", "Microsoft - MSX2"],
    "Commodore 64": ["Commodore - 64"],
    "Commodore Amiga": ["Commodore - Amiga"],
    "ZX Spectrum": ["Sinclair - ZX Spectrum"],
    "Atari ST": ["Atari - ST"],
    "Amstrad CPC": ["Amstrad - CPC"],
}


BAD_RELEASE = re.compile(r"\((?:[^)]*\b)?(Beta|Proto|Demo|Sample|Unl|Pirate|Hack|Kiosk)\b", re.I)


# --- names -------------------------------------------------------------------------------
def norm(name: str) -> str:
    """A title reduced for matching: "The Legend of Zelda: The Wind Waker" and
    "Legend of Zelda, The - The Wind Waker (USA)" become the same."""
    s = name.lower()
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)        # (USA), [Wii], (sequenced)
    s = s.replace("&", " and ").replace("_", " ")
    s = re.sub(r",\s*the\b", "", s)                      # "Legend of Zelda, The"
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


def regions_of(name: str) -> list[str]:
    out = []
    for grp in re.findall(r"\(([^)]*)\)", name):
        out += [x.strip() for x in grp.split(",")]
    return out


# --- libretro-thumbnails --------------------------------------------------------------------
class Libretro:
    """Lists come from the GitHub API (one call per console, cached for a week), with
    thumbnails.libretro.com's folder listing as fallback; images from GitHub, with
    thumbnails.libretro.com as fallback."""
    name = "libretro"
    ORG = "libretro-thumbnails"

    def __init__(self, cache_dir: Path | None, http_get):
        self.cache = cache_dir
        self.get = http_get
        self._lists: dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def repo(system: str) -> str:
        return re.sub(r"[^0-9A-Za-z\-]", "_", system)

    def systems_for(self, platform: str, extra: dict) -> list[str]:
        if platform in extra:
            v = extra[platform]
            return v if isinstance(v, list) else [v]
        if platform in LIBRETRO_SYSTEMS:
            return LIBRETRO_SYSTEMS[platform]
        # a console named like libretro's own ("Nintendo - Wii") or close to it
        return [s for v in LIBRETRO_SYSTEMS.values() for s in v if norm(s) == norm(platform)][:1]

    def listing(self, system: str) -> dict:
        """{"files": {folder: [file names]}} for one console."""
        with self._lock:
            if system in self._lists:
                return self._lists[system]
            p = self.cache / f"{self.repo(system)}.json" if self.cache else None
            if p and p.is_file() and time.time() - p.stat().st_mtime < LIST_DAYS * 86400:
                try:
                    self._lists[system] = json.loads(p.read_text("utf-8"))
                    return self._lists[system]
                except ValueError:
                    pass
            files: dict[str, list] = {}
            try:
                data = json.loads(self.get(
                    f"https://api.github.com/repos/{self.ORG}/{self.repo(system)}/git/trees/HEAD?recursive=1"))
                for e in data.get("tree", []):
                    folder, _, f = e.get("path", "").partition("/")
                    if f and folder in LIBRETRO_TYPES.values():
                        files.setdefault(folder, []).append(f)
            except Exception as e:      # noqa: BLE001 - fall back to the plain folder listing
                log.info("GitHub listing of %s failed (%s); trying thumbnails.libretro.com", system, e)
                for folder in LIBRETRO_TYPES.values():
                    try:
                        html = self.get(f"https://thumbnails.libretro.com/{urllib.parse.quote(system)}/{folder}/")
                        files[folder] = [urllib.parse.unquote(h) for h in
                                         re.findall(r'href="([^"?/][^"]*\.png)"', html.decode("utf-8", "replace"))]
                    except Exception as e2:  # noqa: BLE001
                        log.info("listing %s/%s failed: %s", system, folder, e2)
            listing = {"system": system, "files": files}
            if files and p:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(listing), "utf-8")
            self._lists[system] = listing
            return listing

    def search(self, platform: str, names: list[str], kind: str, settings: dict,
               limit: int = 1) -> list[dict]:
        """Best matches for the album's names: [{system, path, name, score}]."""
        folder = LIBRETRO_TYPES[kind]
        regions = settings.get("regions", [])
        wanted = [norm(n) for n in names if n and norm(n)]
        if not wanted:
            return []
        hits = []
        for system in self.systems_for(platform, settings.get("libretro_systems", {})):
            files = self.listing(system)["files"].get(folder, [])
            by_norm: dict[str, list[str]] = {}
            by_main: dict[str, set] = {}        # "Castlevania III" for "… - Dracula's Curse"
            for f in files:
                base = f.rsplit(".", 1)[0]
                k = norm(base)
                by_norm.setdefault(k, []).append(f)
                main = re.split(r" - |_ |: ", re.sub(r"\s*\(.*", "", base), maxsplit=1)
                if len(main) > 1 and not main[0].endswith(", The"):
                    by_main.setdefault(norm(main[0]), set()).add(k)
            for w in wanted:
                exact = by_norm.get(w)
                close = [(1.0, w)] if exact else [
                    (difflib.SequenceMatcher(None, w, k).ratio(), k)
                    for k in difflib.get_close_matches(w, by_norm, n=max(limit, 3), cutoff=0.86)]
                if not close and len(by_main.get(w, ())) == 1:    # one game with that main title
                    close = [(0.9, next(iter(by_main[w])))]
                for score, k in close:
                    for f in by_norm[k]:
                        rs = regions_of(f)
                        rank = min((regions.index(r) for r in rs if r in regions), default=len(regions))
                        if BAD_RELEASE.search(f):           # prototypes, demos, unlicensed …
                            rank += 100
                        hits.append({"system": system, "path": f"{folder}/{f}", "name": f.rsplit(".", 1)[0],
                                     "score": round(score, 3), "_rank": (-score, rank, len(f))})
            if hits and limit == 1:
                break                       # the first console that knows the game
        hits.sort(key=lambda h: h["_rank"])
        seen, out = set(), []
        for h in hits:
            if h["path"] not in seen:
                seen.add(h["path"])
                out.append({k: v for k, v in h.items() if k != "_rank"})
        return out[:limit]

    def browse(self, platform: str, query: str, kind: str, settings: dict, limit=12) -> list[dict]:
        """For the cover editor: matches for a typed name, also partial ones."""
        q = norm(query)
        out = self.search(platform, [query], kind, settings, limit=limit)
        if len(out) < limit and q:
            folder = LIBRETRO_TYPES[kind]
            for system in self.systems_for(platform, settings.get("libretro_systems", {})):
                for f in self.listing(system)["files"].get(folder, []):
                    if q in norm(f) and not any(o["path"] == f"{folder}/{f}" for o in out):
                        out.append({"system": system, "path": f"{folder}/{f}",
                                    "name": f.rsplit(".", 1)[0], "score": 0.5})
                        if len(out) >= limit:
                            return out
        return out

    def known(self, system: str, path: str) -> bool:
        folder, _, f = path.partition("/")
        return f in self.listing(system)["files"].get(folder, [])

    def download(self, system: str, path: str) -> bytes:
        q = urllib.parse.quote(path)
        urls = [f"https://raw.githubusercontent.com/{self.ORG}/{self.repo(system)}/HEAD/{q}",
                f"https://thumbnails.libretro.com/{urllib.parse.quote(system)}/{q}"]
        err = None
        for u in urls:
            try:
                return self.get(u)
            except Exception as e:      # noqa: BLE001
                err = e
        raise err


def http_get(url: str, timeout=30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise ValueError("response too large")
    return data


def image_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    return None


# --- square covers ----------------------------------------------------------------------------
def shape_image(data: bytes, shape: str, size: int = 1000) -> bytes | None:
    """A square JPEG: crop (centre) or pad (over a blurred copy). None without Pillow
    or for 'original'."""
    if shape not in ("crop", "pad"):
        return None
    try:
        from PIL import Image, ImageFilter, ImageEnhance
    except ImportError:
        return None
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    w, h = im.size
    if abs(w - h) <= 2:
        side = min(size, w)
        out = im.resize((side, side), Image.LANCZOS) if side != w else im
    elif shape == "crop":
        s = min(w, h)
        out = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        out = out.resize((min(size, s),) * 2, Image.LANCZOS)
    else:
        side = min(size, max(w, h))
        k = side / max(w, h)
        kb = side / min(w, h)
        bg = im.resize((round(w * kb), round(h * kb)), Image.BILINEAR)
        bg = bg.crop(((bg.width - side) // 2, (bg.height - side) // 2,
                      (bg.width - side) // 2 + side, (bg.height - side) // 2 + side))
        bg = ImageEnhance.Brightness(bg.filter(ImageFilter.GaussianBlur(side / 40))).enhance(0.55)
        fg = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
        bg.paste(fg, ((side - fg.width) // 2, (side - fg.height) // 2))
        out = bg
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class ShapeCache:
    """Square versions of covers, made once per image and shape in /meta/_cache/covers."""

    def __init__(self, cache_dir: Path | None):
        self.dir = cache_dir

    def get(self, path: str, shape: str) -> bytes | None:
        p = Path(path)
        st = p.stat()
        key = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{shape}".encode()).hexdigest()[:20]
        c = self.dir / f"{key}.jpg" if self.dir else None
        if c and c.is_file():
            return c.read_bytes()
        try:
            data = shape_image(p.read_bytes(), shape)
        except Exception as e:      # noqa: BLE001 - a broken image is served as it is
            log.warning("could not shape %s: %s", p, e)
            return None
        if data and c:
            c.parent.mkdir(parents=True, exist_ok=True)
            c.write_bytes(data)
        return data


# --- fetching -----------------------------------------------------------------------------------
class CoverFetcher:
    def __init__(self, lib, settings, http=http_get):
        self.lib = lib
        self.settings = settings
        root = lib.meta.root if lib.meta.enabled else None
        self.cache_dir = root / "_cache" if root else None
        self.libretro = Libretro(self.cache_dir / "libretro" if root else None, http)
        self.misses_path = self.cache_dir / "covers.json" if root else None
        self.status = {"running": False, "checked": 0, "total": 0, "found": 0,
                       "missing": 0, "errors": 0, "last_run": None, "last_error": ""}
        self._lock = threading.Lock()
        self._again = False

    # --- what an album has ---------------------------------------------------------------
    def auto_info(self, key: str) -> dict | None:
        p = self.lib.auto_cover(key)
        if not p:
            return None
        try:
            return json.loads(Path(p).with_suffix(".json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _misses(self) -> dict:
        try:
            return json.loads(self.misses_path.read_text("utf-8"))
        except (OSError, ValueError, AttributeError):
            return {}

    def needs(self, al, c: dict, misses: dict) -> bool:
        if "libretro" not in c["enabled"] or self.lib.uploaded_cover(al.key):
            return False
        order = c["order"]
        if "rip" in c["enabled"] and al.rip_own and order.index("rip") < order.index("libretro"):
            return False                 # the rip's image comes first and exists
        info = self.auto_info(al.key)
        if info is not None:
            return info.get("type") != c["libretro_type"] and not info.get("pinned")
        m = misses.get(al.key, {})
        return not (m.get("type") == c["libretro_type"]
                    and time.time() - m.get("at", 0) < MISS_DAYS * 86400)

    @staticmethod
    def names(al) -> list[str]:
        return [al.name]

    # --- running ---------------------------------------------------------------------------------
    def start(self, manual: bool = False):
        """Fetch for all albums that need it, in the background (one run at a time).
        Runs after each scan when "auto" is on; manual=True runs anyway."""
        if not self.lib.meta.writable or not (manual or self.settings.covers().get("auto")):
            return
        with self._lock:
            if self.status["running"]:
                self._again = True
                return
            self.status["running"] = True
        threading.Thread(target=self._run, daemon=True, name="covers").start()

    def _run(self):
        try:
            while True:
                self.run_once()
                with self._lock:
                    if not self._again:
                        break
                    self._again = False
        finally:
            with self._lock:
                self.status["running"] = False

    def run_once(self):
        c = self.settings.covers()
        misses = self._misses()
        todo = [a for a in list(self.lib.index.albums.values()) if self.needs(a, c, misses)]
        st = self.status
        st.update(checked=0, total=len(todo), found=0, missing=0, errors=0, last_error="")
        for al in todo:
            try:
                if self.fetch(al, c):
                    st["found"] += 1
                    misses.pop(al.key, None)
                    al.covers["auto"] = self.lib.auto_cover(al.key)
                    self.lib.apply_covers(al)
                else:
                    st["missing"] += 1
                    misses[al.key] = {"type": c["libretro_type"], "at": time.time(), "name": al.name}
            except Exception as e:      # noqa: BLE001 - one album must not stop the run
                st["errors"] += 1
                st["last_error"] = f"{al.name}: {e}"
                log.warning("cover for %s: %s", al.name, e)
            st["checked"] += 1
        if self.misses_path:
            self.misses_path.parent.mkdir(parents=True, exist_ok=True)
            self.misses_path.write_text(json.dumps(misses, ensure_ascii=False, indent=1), "utf-8")
        st["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        if todo:
            log.info("covers: %d found, %d not found, %d errors", st["found"], st["missing"], st["errors"])

    def fetch(self, al, c: dict) -> bool:
        hits = self.libretro.search(al.platform, self.names(al), c["libretro_type"], c)
        if not hits:
            return False
        h = hits[0]
        self.save(al, h, c["libretro_type"], self.remote_image(h["system"], h["path"]))
        return True

    def save(self, al, hit: dict, kind: str, data: bytes, pinned: bool = False):
        ext = image_ext(data)
        if not ext:
            raise ValueError("not a PNG or JPEG image")
        for e in ("jpg", "png"):
            self.lib.cover_target(al.key, e, "auto").unlink(missing_ok=True)
        p = self.lib.cover_target(al.key, ext, "auto")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.with_suffix(".json").write_text(json.dumps(
            {"source": "libretro", "type": kind, "system": hit["system"], "path": hit["path"],
             "name": hit["name"], "pinned": pinned, "fetched": time.strftime("%Y-%m-%d")},
            ensure_ascii=False, indent=1) + "\n", "utf-8")

    def remote_image(self, system: str, path: str) -> bytes:
        """An image from libretro-thumbnails, cached in /meta/_cache/libretro-img."""
        key = hashlib.sha1(f"{system}|{path}".encode()).hexdigest()[:20]
        c = self.cache_dir / "libretro-img" / key if self.cache_dir else None
        if c and c.is_file():
            return c.read_bytes()
        data = self.libretro.download(system, path)
        if not image_ext(data):
            raise ValueError("not a PNG or JPEG image")
        if c:
            c.parent.mkdir(parents=True, exist_ok=True)
            c.write_bytes(data)
        return data

    def forget_misses(self):
        if self.misses_path:
            self.misses_path.unlink(missing_ok=True)
