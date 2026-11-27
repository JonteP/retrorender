"""Library index for the web UI and the Subsonic API.

Model:
  artist = composer (from album.toml or the files' tags), split on , & /
  genre  = console: the platform folder, named via [platforms] in config.toml
  album  = a folder of tunes, OR a single file with several subtracks (NSF, SID)
  song   = one subtrack, with its length from the loop policy

Titles, order, hidden tracks and loop policy can be set in album.toml in the meta folder
(see meta.py). Ids are stable (hash of the path), so clients' libraries and playlists
survive re-indexing.
"""
from __future__ import annotations

import hashlib
import os
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from catalog import game_key, normalize_title, region_of
from txtp import hidden_in
from meta import MetaStore
from policy import plan, settings_for

log = logging.getLogger("library")

COVER_NAMES = ("folder.jpg", "folder.png", "cover.jpg", "cover.png", "front.jpg",
               "front.png", "album.jpg", "album.png")


def _id(prefix: str, key: str) -> str:
    return prefix + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class Song:
    id: str
    rel: str
    sub: int
    title: str
    artists: list            # [(id, namn)]
    track: int
    duration: float
    size: int
    album_id: str
    platform: str
    created: str
    engine: str
    composer: str = ""       # raw composer string before splitting
    disc: int = 1            # the part of a merged album (see Library._merge_parts)

    @property
    def artist(self) -> str:
        return ", ".join(n for _, n in self.artists)

    @property
    def artist_id(self) -> str:
        return self.artists[0][0]


@dataclass
class Part:
    """One source of an album: a folder or a file with its own album.toml. A rip with
    streamed audio in one folder and sequences in another becomes one album with two parts."""
    key: str
    label: str
    first_file: str
    songs: list = field(default_factory=list)


@dataclass
class Album:
    id: str
    key: str
    name: str
    platform: str
    created: str
    meta_artist: str = ""
    artists: list = field(default_factory=list)   # [(id, namn)]
    year: int | None = None
    cover: str | None = None          # absolute path to an image
    rip_cover: str | None = None      # the same, leaving out an uploaded cover
    rip_own: str | None = None        # the rip's own image
    cover_origin: str = ""            # upload | toml | rip | libretro
    covers: dict = field(default_factory=dict)   # every image found, by origin
    songs: list[str] = field(default_factory=list)
    region: str = ""
    chips: list = field(default_factory=list)
    game_key: str = ""
    first_file: str = ""
    parts: list = field(default_factory=list)      # [Part]; one for an ordinary album

    @property
    def artist(self) -> str:
        return ", ".join(n for _, n in self.artists)

    @property
    def artist_id(self) -> str:
        return self.artists[0][0] if self.artists else ""


@dataclass
class Artist:
    id: str
    name: str
    albums: list[str] = field(default_factory=list)


# Sub-folders with these names hold one kind of audio for the game in the folder above
# (e.g. Wind Waker's Stream/ and Seqs/), so they are parts of that album, not albums.
PART_DIRS = {"stream": "Streamed", "streams": "Streamed", "strm": "Streamed",
             "seqs": "Sequenced", "seq": "Sequenced", "sequences": "Sequenced",
             "bms": "Sequenced", "bgm": "", "music": "", "sound": "", "sounds": "", "audio": ""}
PART_ORDER = {"": 0, "Streamed": 1, "Sequenced": 2}

UNKNOWN = "Unknown composer"
SEVERAL = "Various composers"


def split_names(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|&|/|\band\b|\boch\b)\s*", s or "")
    out = []
    for x in (p.strip() for p in parts):
        if x and x.lower() not in ("unknown", "?", "-") and x not in out:
            out.append(x)
    return out


class Index:
    def __init__(self):
        self.artists: dict[str, Artist] = {}
        self.albums: dict[str, Album] = {}
        self.songs: dict[str, Song] = {}
        self.genres: dict[str, list[str]] = {}     # konsol -> album-id:n
        self.by_sha: dict[str, str] = {}           # sha1 -> first song's file (dedupe)
        self.hidden_duplicates: list[tuple[str, str]] = []
        self.built = 0.0

    def artist(self, name: str) -> tuple[str, str]:
        aid = _id("ar-", name.lower())
        if aid not in self.artists:
            self.artists[aid] = Artist(aid, name)
        return aid, self.artists[aid].name


class Library:
    settings = None                   # settings.Settings, set by the server (cover order)
    shaper = None                     # covers.ShapeCache, set by the server (square covers)
    after_scan: list = []             # called after each scan (e.g. fetching covers)

    def __init__(self, root: Path, registry, cfg: dict, probe, sample_rate: int,
                 meta: MetaStore, catalog=None):
        self.root = root
        self.reg = registry
        self.cfg = cfg
        self.probe = probe
        self.sr = sample_rate
        self.meta = meta
        self.cat = catalog
        self._seen: list[str] = []
        self._hashing = False
        self.index = Index()
        self.scanning = False
        self.count = 0
        self._lock = threading.Lock()

    # --- shared resolution (used by the index, /info and /render) ---------------
    def album_key(self, f: Path, n_tracks: int) -> str:
        rel = str(f.relative_to(self.root))
        if n_tracks > 1:
            return "file:" + rel
        return "dir:" + str(f.parent.relative_to(self.root))

    def ensure_meta(self, key: str, f: Path, eng, tracks) -> dict | None:
        """Reads album.toml, generating it first if it is missing."""
        if not self.meta.enabled:
            return None
        if self.meta.writable and (self.meta.outdated_untouched(key) or (
                self.meta.titles_newer(key) and self.meta.untouched(key))):
            log.info("updating untouched %s (older generation or a newer titles.toml)", key)
            self.meta.delete(key)
        if not self.meta.exists(key) and self.meta.writable:
            if key.startswith("file:"):
                items = [(f, t, len(tracks)) for t in tracks]
            else:
                items = []
                hidden = hidden_in(f.parent)
                for g in sorted(f.parent.iterdir(), key=lambda p: p.name.lower()):
                    if g.name.lower() in hidden:
                        continue
                    if g.is_file() and self.reg.engine_hint(g) not in (None, "missing"):
                        try:
                            _, ts = self.probe(g)
                        except Exception:
                            continue
                        if len(ts) == 1:
                            items.append((g, ts[0], 1))
            self.meta.generate(key, f.parent, items, platform=self.platform_for(f))
        return self.meta.load(key)

    def resolve(self, f: Path, sub: int, query: dict | None = None):
        """-> (engine, TrackInfo with overrides, policy settings, meta entry, album meta)."""
        eng, tracks = self.probe(f)
        if eng is None:
            raise ValueError("format not supported")
        # sub is the engine's track index; engines may skip indices (e.g. silent sequences)
        t = next((x for x in tracks if x.index == sub), None)
        if t is None:
            raise ValueError(f"no track {sub} in {f.name}")
        key = self.album_key(f, len(tracks))
        meta = self.ensure_meta(key, f, eng, tracks)
        entry = MetaStore.track_entry(meta, f.name, t.index, len(tracks))
        t2, layers = MetaStore.apply(t, meta or {}, entry)
        s = settings_for(self.cfg, eng.name, f.suffix, query or {}, layers)
        return eng, t2, s, entry, meta

    # --- skanning -------------------------------------------------------------------
    def start(self, every_minutes: float):
        def loop():
            while True:
                self.scan()
                if every_minutes <= 0:
                    return
                time.sleep(every_minutes * 60)
        threading.Thread(target=loop, daemon=True, name="library-scan").start()

    def scan(self):
        if not self._lock.acquire(blocking=False):
            return
        self.scanning, self.count = True, 0
        t0 = time.time()
        try:
            self.meta.prune_backups(float(self.cfg.get("meta_backup_days", 90)))
            idx = Index()
            self._seen = []
            # top-level folders starting with "_" or "." are reserved (e.g. _synced)
            for d in sorted(p for p in self.root.iterdir()
                            if p.is_dir() and not p.name.startswith(("_", "."))):
                self._scan_platform(idx, d)
            # downloads from remote sources come last, so local copies win duplicates
            synced = self.root / "_synced" / "joshw"
            if synced.is_dir():
                for d in sorted(p for p in synced.iterdir() if p.is_dir()):
                    self._scan_platform(idx, d, platform=self.synced_platform(d.name))
            self._finish(idx)
            idx.built = time.time()
            self.index = idx
            if self.cat and self.cat.enabled:
                self.cat.touch(self._seen)
                removed = self.cat.prune(t0 - 1)
                if removed:
                    log.info("catalog: removed %d files that no longer exist", removed)
                self._start_hashing()
            log.info("index ready: %d artists, %d albums, %d songs in %.1fs",
                     len(idx.artists), len(idx.albums), len(idx.songs), time.time() - t0)
            for fn in self.after_scan:
                fn()
        except Exception:
            log.exception("indexing failed")
        finally:
            self.scanning = False
            self._lock.release()

    def _start_hashing(self):
        """Hash new files in the background; rescan once if that reveals duplicates."""
        if self._hashing:
            return
        self._hashing = True

        def run():
            try:
                n = self.cat.hash_missing(self.root)
                if n:
                    log.info("catalog: hashed %d files", n)
                    if self.cat.duplicate_groups():
                        self.scan()
            finally:
                self._hashing = False
        threading.Thread(target=run, daemon=True, name="catalog-hash").start()

    def _finish(self, idx: Index):
        """Game keys, and suffixes for albums whose names collide (e.g. JP vs US)."""
        groups: dict[tuple, list[Album]] = {}
        for al in idx.albums.values():
            chips = self.cat.chips(al.first_file) if self.cat and al.first_file else []
            al.chips = chips
            al.region = region_of(al.name, Path(al.first_file).name, Path(al.first_file).parent.name)
            al.game_key = game_key(al.platform, al.name, al.region, chips)
            groups.setdefault((al.platform, normalize_title(al.name)), []).append(al)
        for albums in groups.values():
            if len({a.game_key for a in albums}) < 2:
                continue
            common = set.intersection(*(set(a.chips) for a in albums))
            regions = {a.region for a in albums}
            for a in albums:
                parts = []
                if len(regions) > 1 and a.region:
                    parts.append(a.region)
                parts += sorted(set(a.chips) - common)
                if parts:
                    a.name = f"{a.name} ({', '.join(parts)})"

    def catalog_report(self) -> dict:
        idx = self.index
        keys: dict[str, list[str]] = {}
        for al in idx.albums.values():
            keys.setdefault(al.game_key, []).append(al.name)
        return {
            "catalog": self.cat.stats() if self.cat else {"enabled": False},
            "hashing": self._hashing,
            "identical_files": self.cat.duplicate_groups() if self.cat else [],
            "hidden_duplicates": [{"hidden": h, "kept": k} for h, k in idx.hidden_duplicates],
            "editions": {k: v for k, v in keys.items() if len(v) > 1},
            "albums": [{"name": a.name, "platform": a.platform, "region": a.region,
                        "chips": a.chips, "game_key": a.game_key}
                       for a in sorted(idx.albums.values(), key=lambda a: a.name.lower())],
        }

    def platform_name(self, folder: str) -> str:
        return str(self.cfg.get("platforms", {}).get(folder, folder))

    def platform_for(self, f: Path) -> str:
        parts = f.relative_to(self.root).parts
        if len(parts) > 3 and parts[0] == "_synced" and parts[1] == "joshw":
            return self.synced_platform(parts[2])
        return self.platform_name(parts[0]) if parts else ""

    def synced_platform(self, system: str) -> str:
        """Console name for a synced source folder; replaced by the server (joshw names)."""
        return system

    def _scan_platform(self, idx: Index, pdir: Path, platform: str | None = None):
        default_platform = platform or self.platform_name(pdir.name)
        albums: list[Album] = []
        dirs: dict[Path, list[Path]] = {}
        for f in pdir.rglob("*"):
            if f.is_file() and self.reg.engine_hint(f) not in (None, "missing"):
                dirs.setdefault(f.parent, []).append(f)
        for d in sorted(dirs, key=lambda p: str(p).lower()):
            dir_album = None
            hidden = hidden_in(d)        # files played through a .txtp are not tracks themselves
            for f in sorted(dirs[d], key=lambda p: p.name.lower()):
                if f.name.lower() in hidden:
                    continue
                try:
                    eng, tracks = self.probe(f)
                except Exception as e:
                    log.warning("skipping %s: %s", f, e)
                    continue
                if not tracks:
                    continue
                rel = str(f.relative_to(self.root))
                self._seen.append(rel)
                sha = self.cat.sha1(rel) if self.cat else None
                if sha:
                    kept = idx.by_sha.setdefault(sha, rel)
                    if kept != rel:
                        idx.hidden_duplicates.append((rel, kept))
                        continue
                key = self.album_key(f, len(tracks))
                meta = self.ensure_meta(key, f, eng, tracks)
                if len(tracks) > 1:
                    album = self._album(idx, key, meta, f, d, default_platform,
                                        tracks[0].game or re.sub(r" \(\d+\)$", "",
                                                                 tracks[0].title) or f.stem)
                    albums.append(album)
                    for t in tracks:
                        self._song(idx, album, f, t.index, meta, t.index + 1)
                else:
                    if dir_album is None:
                        fallback = tracks[0].game or (
                            f"{default_platform} (loose files)" if d == pdir else d.name)
                        dir_album = self._album(idx, key, meta, f, d, default_platform, fallback)
                        albums.append(dir_album)
                    m = re.match(r"^(\d{1,3})\D", f.name)
                    num = int(m.group(1)) if m else len(dir_album.songs) + 1
                    self._song(idx, dir_album, f, 0, meta, num)
                self.count += 1
        for al in [a for a in albums if not a.songs]:
            del idx.albums[al.id]
        albums = [a for a in albums if a.songs]
        for al in albums:
            al.parts = [Part(al.key, "", al.first_file, list(al.songs))]
        if self.cfg.get("merge_parts", True):
            albums = self._merge_parts(idx, pdir, albums)
        for al in albums:
            al.songs.sort(key=lambda sid: (idx.songs[sid].disc, idx.songs[sid].track,
                                           idx.songs[sid].title.lower()))
            self._assign_artists(idx, al)
            idx.genres.setdefault(al.platform, []).append(al.id)

    def group_root(self, d: Path, pdir: Path) -> Path:
        """The folder whose album a folder of audio belongs to: a synced rip's own folder
        (the one with .source.json), or the parent of Stream/, Seqs/ and similar."""
        if d == pdir:
            return d                                # loose files of a console stay apart
        for a in [d, *d.parents]:
            if a == pdir or a == self.root:
                break
            if (a / ".source.json").is_file():
                return a
        r = d
        while r.parent != pdir and r.name.lower() in PART_DIRS:
            r = r.parent
        return r

    def _merge_parts(self, idx: Index, pdir: Path, albums: list) -> list:
        groups: dict[Path, list[Album]] = {}
        for al in albums:
            d = (self.root / al.first_file).parent
            groups.setdefault(self.group_root(d, pdir), []).append(al)
        out = []
        for root, als in groups.items():
            if len(als) == 1:
                out.append(als[0])
                continue
            rel = str(root.relative_to(self.root))
            key = "dir:" + rel

            def label(a):
                f = self.root / a.first_file
                sub = f.parent.relative_to(root)
                if a.key.startswith("file:") and f.parent == root:
                    return f.stem
                names = [PART_DIRS.get(x.lower(), x) for x in sub.parts]
                return " / ".join(n for n in names if n)
            labels = {a.id: label(a) for a in als}
            # the rip's own folder next to a Seqs/ folder holds the streamed recordings
            if "Sequenced" in labels.values():
                for a in als:
                    if not labels[a.id] and not any(idx.songs[i].engine == "jaudio" for i in a.songs):
                        labels[a.id] = "Streamed"
            als.sort(key=lambda a: (PART_ORDER.get(labels[a.id], 3),
                                    labels[a.id].lower(), a.first_file.lower()))
            main = als[0] if als[0].key == key else None
            gmeta = (self.meta.load(key) if not main and self.meta.enabled else None) or {}
            galb = gmeta.get("album", {}) or {}
            name = str(galb.get("title") or "") or (main.name if main else "")
            if not name:
                try:
                    name = json.loads((root / ".source.json").read_text("utf-8")).get("album", "")
                except (OSError, ValueError):
                    name = ""
            if not name:
                named = [a.name for a in als if a.name.lower() != (self.root / a.first_file).parent.name.lower()]
                name = re.sub(r"\s*\(sequenced\)$", "", named[0]) if named else root.name
            years = [a.year for a in als if a.year]
            merged = Album(_id("al-", key), key, name, als[0].platform,
                           max(a.created for a in als),
                           meta_artist=str(galb.get("artist") or "")
                           or next((a.meta_artist for a in als if a.meta_artist), ""),
                           year=galb.get("year") if isinstance(galb.get("year"), int)
                           else (min(years) if years else None))
            # the group's own images first, then any part's (e.g. Stream/folder.jpg)
            merged.covers = self._covers(gmeta or None, root, key)
            for a in als:
                for o, p in a.covers.items():
                    merged.covers.setdefault(o, p)
            self.apply_covers(merged)
            merged.first_file = als[0].first_file
            for n, a in enumerate(als, 1):
                idx.albums.pop(a.id, None)
                merged.parts.append(Part(a.key, labels[a.id] or ("Main" if a is main else a.name),
                                         a.first_file, list(a.songs)))
                for sid in a.songs:
                    idx.songs[sid].album_id = merged.id
                    idx.songs[sid].disc = n
                    merged.songs.append(sid)
            idx.albums[merged.id] = merged
            out.append(merged)
        return out

    def part_of(self, al: Album, n: int | str | None = None) -> Part:
        """One part of an album (0 = the first); ordinary albums have one."""
        parts = al.parts or [Part(al.key, "", al.first_file, list(al.songs))]
        try:
            return parts[int(n or 0)]
        except (ValueError, IndexError):
            raise KeyError("no such part")

    def _assign_artists(self, idx: Index, al: Album):
        """Album artist = album.toml artist, otherwise the songs' composers."""
        names = split_names(al.meta_artist)
        if not names:
            seen = []
            for sid in al.songs:
                for n in split_names(idx.songs[sid].composer):
                    if n not in seen:
                        seen.append(n)
            names = seen if 0 < len(seen) <= 3 else ([SEVERAL] if seen else [UNKNOWN])
        al.artists = [idx.artist(n) for n in names]
        for aid, _ in al.artists:
            idx.artists[aid].albums.append(al.id)
        for sid in al.songs:
            so = idx.songs[sid]
            own = split_names(so.composer)
            so.artists = [idx.artist(n) for n in own] if own else list(al.artists)

    def cover_target(self, key: str, ext: str, kind="") -> Path:
        """Where a cover image for this album is stored in /meta. kind "" is the cover you
        made, "source" the image you chose (to crop from), "auto" a fetched one."""
        kind = "source" if kind is True else (kind or "")
        p = self.meta.path_for(key)
        name = f"cover.{kind}" if kind else "cover"
        if key.startswith("dir:"):
            return p.parent / f"{name}.{ext}"
        return p.with_name(p.name.removesuffix(".album.toml") + f".{name}.{ext}")

    def uploaded_cover(self, key: str, kind="") -> str | None:
        if not self.meta.enabled:
            return None
        for ext in ("jpg", "png"):
            p = self.cover_target(key, ext, kind)
            if p.is_file():
                return str(p)
        return None

    def auto_cover(self, key: str) -> str | None:
        return self.uploaded_cover(key, "auto")

    def _covers(self, meta: dict | None, d: Path, key: str) -> dict:
        """Every cover image the album has, by origin."""
        c = {"upload": self.uploaded_cover(key), "auto": self.auto_cover(key)}
        meta_dir = self.meta.path_for(key).parent if self.meta.enabled else None
        wanted = ((meta or {}).get("album", {}) or {}).get("cover")
        if wanted:
            c["toml"] = next((str(b / wanted) for b in (meta_dir, d) if b and (b / wanted).is_file()), None)
        c["rip"] = next((str(b / n) for b in (d, meta_dir) if b for n in COVER_NAMES
                         if (b / n).is_file()), None)
        return {k: v for k, v in c.items() if v}

    def cover_order(self) -> list[str]:
        """Origins in priority order: yours, album.toml's cover = "…", then the sources
        as ordered on the Settings page (disabled ones left out)."""
        st = self.settings.covers() if self.settings else {"order": ["rip", "libretro"],
                                                           "enabled": ["rip", "libretro"]}
        names = {"rip": "rip", "libretro": "auto"}
        return ["upload", "toml"] + [names[x] for x in st["order"] if x in st["enabled"] and x in names]

    def apply_covers(self, al: Album):
        order = self.cover_order()
        pick = [(o, al.covers[o]) for o in order if o in al.covers]
        al.cover, al.cover_origin = pick[0][1] if pick else None, ""
        if pick:
            al.cover_origin = {"auto": "libretro"}.get(pick[0][0], pick[0][0])
        rest = [p for o, p in pick if o != "upload"]
        al.rip_cover = rest[0] if rest else None
        al.rip_own = al.covers.get("rip")

    def cover_rev(self, al: Album) -> str:
        """Changes whenever the served cover changes (image or square shape), for URLs:
        browsers keep images of a page in memory even when told to check them."""
        if not al.cover:
            return ""
        try:
            st = os.stat(al.cover)
        except OSError:
            return ""
        shape = self.settings.covers()["shape"] if self.settings and al.cover_origin != "upload" else "o"
        return f"{int(st.st_mtime) % 100000000}{st.st_size % 1000}{shape[0]}"

    def cover_image(self, al: Album, original: bool = False):
        """(bytes, content type, etag) of the cover as it is served, or None. A cover you
        made is served as it is; other images are made square as set on the Settings page
        (crop or pad). original=True: the image to crop from in the cover editor."""
        path = ((self.uploaded_cover(al.key, "source") or al.rip_cover) if original else al.cover)
        if not path or not Path(path).is_file():
            return None
        p = Path(path)
        st = p.stat()
        shape = self.settings.covers()["shape"] if self.settings else "original"
        if original or al.cover_origin == "upload" or not self.shaper:
            shape = "original"
        etag = f'"{int(st.st_mtime)}-{st.st_size}-{shape}"'
        data = self.shaper.get(path, shape) if shape != "original" else None
        if data:
            return data, "image/jpeg", etag
        return p.read_bytes(), ("image/png" if p.suffix.lower() == ".png" else "image/jpeg"), etag

    def _cover(self, meta: dict | None, d: Path, key: str, uploaded: bool = True) -> str | None:
        """The cover by the current priority order (kept for callers that want a path)."""
        a = Album("", key, "", "", "")
        a.covers = self._covers(meta, d, key)
        if not uploaded:
            a.covers.pop("upload", None)
        self.apply_covers(a)
        return a.cover

    def _album(self, idx, key, meta, f: Path, d: Path, platform: str, fallback: str) -> Album:
        alb = (meta or {}).get("album", {}) or {}
        year = alb.get("year")
        a = Album(_id("al-", key), key, str(alb.get("title") or fallback),
                  str(alb.get("platform") or platform), _iso(f.stat().st_mtime),
                  meta_artist=str(alb.get("artist") or ""),
                  year=int(year) if isinstance(year, int) else None)
        a.covers = self._covers(meta, d, key)
        self.apply_covers(a)
        a.first_file = str(f.relative_to(self.root))
        idx.albums[a.id] = a
        return a

    def _song(self, idx, album: Album, f: Path, sub: int, meta, num: int):
        eng, t, s, entry, _ = self.resolve(f, sub)
        if entry.get("skip"):
            return
        if (meta or {}).get("album", {}).get("only_listed") and not entry:
            return
        secs = plan(t, s).seconds
        frames = int(secs * self.sr)
        rel = str(f.relative_to(self.root))
        track_no = entry.get("track", num)
        # composer: the track's own album.toml entry, otherwise the file's tag
        composer = str(entry.get("artist") or t.author or "")
        song = Song(
            id=_id("tr-", f"{rel}#{sub}"), rel=rel, sub=sub,
            title=t.title or re.sub(r"^\d{1,3}\s*[-.]?\s*", "", f.stem) or f.stem,
            artists=[], track=int(track_no) if str(track_no).isdigit() else num,
            duration=secs, size=44 + frames * 4, album_id=album.id, platform=album.platform,
            created=_iso(f.stat().st_mtime), engine=eng.name, composer=composer)
        idx.songs[song.id] = song
        album.songs.append(song.id)
