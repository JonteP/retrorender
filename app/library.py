"""Biblioteksindex för Subsonic-API:et.

Modell:
  artist = kompositör (från album.toml eller filernas taggar), delad på , & /
  genre  = konsol: plattformsmappen, med namn från [platforms] i config.toml
  album  = en mapp med spel/låtar, ELLER en enskild fil med flera delspår (NSF, SID)
  låt    = ett delspår, med längd enligt looppolicyn

Titlar, ordning, dolda spår och looppolicy kan styras med album.toml i metamappen
(se meta.py). Id:n är stabila (hash av sökvägen), så att MA:s bibliotek och
spellistor överlever omindexering.
"""
from __future__ import annotations

import hashlib
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
    composer: str = ""       # rå kompositörssträng innan delning

    @property
    def artist(self) -> str:
        return ", ".join(n for _, n in self.artists)

    @property
    def artist_id(self) -> str:
        return self.artists[0][0]


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
    cover: str | None = None          # absolut sökväg till bild
    songs: list[str] = field(default_factory=list)
    region: str = ""
    chips: list = field(default_factory=list)
    game_key: str = ""
    first_file: str = ""

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


UNKNOWN = "Okänd kompositör"
SEVERAL = "Flera kompositörer"


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

    # --- gemensam upplösning (används av index, /info och /render) --------------
    def album_key(self, f: Path, n_tracks: int) -> str:
        rel = str(f.relative_to(self.root))
        if n_tracks > 1:
            return "file:" + rel
        return "dir:" + str(f.parent.relative_to(self.root))

    def ensure_meta(self, key: str, f: Path, eng, tracks) -> dict | None:
        """Läser album.toml, och genererar den först om den saknas."""
        if not self.meta.enabled:
            return None
        if self.meta.writable and (self.meta.outdated_untouched(key) or (
                self.meta.titles_newer(key) and self.meta.untouched(key))):
            log.info("uppdaterar orörd %s (äldre generering eller ny titles.toml)", key)
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
                    if g.is_file() and self.reg.engine_hint(g) not in (None, "saknas"):
                        try:
                            _, ts = self.probe(g)
                        except Exception:
                            continue
                        if len(ts) == 1:
                            items.append((g, ts[0], 1))
            self.meta.generate(key, f.parent, items, platform=self.platform_for(f))
        return self.meta.load(key)

    def resolve(self, f: Path, sub: int, query: dict | None = None):
        """-> (motor, TrackInfo med overrides, policyinställningar, metapost, album-meta)."""
        eng, tracks = self.probe(f)
        if eng is None:
            raise ValueError("inget stöd för formatet")
        t = tracks[sub]
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
            log.info("index klart: %d artister, %d album, %d låtar på %.1fs",
                     len(idx.artists), len(idx.albums), len(idx.songs), time.time() - t0)
        except Exception:
            log.exception("indexering misslyckades")
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
            if f.is_file() and self.reg.engine_hint(f) not in (None, "saknas"):
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
                    log.warning("hoppar över %s: %s", f, e)
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
                            f"{default_platform} (lösa filer)" if d == pdir else d.name)
                        dir_album = self._album(idx, key, meta, f, d, default_platform, fallback)
                        albums.append(dir_album)
                    m = re.match(r"^(\d{1,3})\D", f.name)
                    num = int(m.group(1)) if m else len(dir_album.songs) + 1
                    self._song(idx, dir_album, f, 0, meta, num)
                self.count += 1
        for al in albums:
            if not al.songs:
                del idx.albums[al.id]
                continue
            al.songs.sort(key=lambda sid: (idx.songs[sid].track, idx.songs[sid].title.lower()))
            self._assign_artists(idx, al)
            idx.genres.setdefault(al.platform, []).append(al.id)

    def _assign_artists(self, idx: Index, al: Album):
        """Albumartist = album.toml artist, annars låtarnas kompositörer."""
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

    def _cover(self, meta: dict | None, d: Path, key: str) -> str | None:
        meta_dir = self.meta.path_for(key).parent if self.meta.enabled else None
        wanted = ((meta or {}).get("album", {}) or {}).get("cover")
        places = [x for x in (meta_dir, d) if x]
        if wanted:
            for base in places:
                if (base / wanted).is_file():
                    return str(base / wanted)
        for base in (d, meta_dir):
            if base:
                for n in COVER_NAMES:
                    if (base / n).is_file():
                        return str(base / n)
        return None

    def _album(self, idx, key, meta, f: Path, d: Path, platform: str, fallback: str) -> Album:
        alb = (meta or {}).get("album", {}) or {}
        year = alb.get("year")
        a = Album(_id("al-", key), key, str(alb.get("title") or fallback),
                  str(alb.get("platform") or platform), _iso(f.stat().st_mtime),
                  meta_artist=str(alb.get("artist") or ""),
                  year=int(year) if isinstance(year, int) else None,
                  cover=self._cover(meta, d, key))
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
        # kompositör: spårets egen post i album.toml, annars filens tagg
        composer = str(entry.get("artist") or t.author or "")
        song = Song(
            id=_id("tr-", f"{rel}#{sub}"), rel=rel, sub=sub,
            title=t.title or re.sub(r"^\d{1,3}\s*[-.]?\s*", "", f.stem) or f.stem,
            artists=[], track=int(track_no) if str(track_no).isdigit() else num,
            duration=secs, size=44 + frames * 4, album_id=album.id, platform=album.platform,
            created=_iso(f.stat().st_mtime), engine=eng.name, composer=composer)
        idx.songs[song.id] = song
        album.songs.append(song.id)
