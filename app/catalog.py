"""Persistent catalog in SQLite (/meta/catalog.sqlite).

Stores, per file in the library:
  - probe results (engine, subtunes, tags, loop points), reused while size+mtime match,
    so restarts don't re-probe the whole library
  - SHA-1 of the content (computed in the background), used to hide identical files
  - sound chips and region, used to build the game key for duplicate handling

Later phases add remote catalogs (joshw, Modland) and per-file state (temporary/pinned)
to the same database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from engines.base import TrackInfo

log = logging.getLogger("catalog")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path      TEXT PRIMARY KEY,      -- relative to the library root
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    engine    TEXT,
    tracks    TEXT,                  -- JSON list of TrackInfo
    sha1      TEXT,
    chips     TEXT,                  -- comma separated, e.g. "2A03,VRC6"
    source    TEXT DEFAULT 'local',
    state     TEXT DEFAULT 'local',  -- local | temporary | pinned (later phases)
    last_seen REAL
);
CREATE INDEX IF NOT EXISTS files_sha1 ON files(sha1);
CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT);
"""
SCHEMA_VERSION = "1"

# NSF header byte 0x7B: expansion audio flags
NSF_EXPANSION = ((0x01, "VRC6"), (0x02, "VRC7"), (0x04, "FDS"), (0x08, "MMC5"),
                 (0x10, "N163"), (0x20, "S5B"))


def nsf_chips(path: Path) -> list[str]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x80)
    except OSError:
        return []
    if head[:5] != b"NESM\x1a" or len(head) < 0x7C:
        return []
    flags = head[0x7B]
    return ["2A03"] + [name for bit, name in NSF_EXPANSION if flags & bit]


def chips_for(path: Path, tracks: list[TrackInfo]) -> list[str]:
    ext = path.suffix.lower()
    if ext == ".nsf":
        return nsf_chips(path)
    for t in tracks:
        if t.extra and t.extra.get("chips"):
            return list(t.extra["chips"])
    return []


# --- region and game key ----------------------------------------------------------------
_REGIONS = {
    "JP": ("japan", "jp", "j", "jpn"),
    "US": ("usa", "us", "u", "america", "na"),
    "EU": ("europe", "eu", "e", "pal", "eur"),
    "World": ("world", "w"),
    "KR": ("korea", "k", "kr"),
}


def region_of(*names: str) -> str:
    """Region from tags like '(Japan)', '(USA, Europe)', '[J]' in titles or file names."""
    found = []
    for name in names:
        for grp in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", name or ""):
            for part in re.split(r"\s*[,/+]\s*", grp.lower()):
                for code, words in _REGIONS.items():
                    if part.strip() in words and code not in found:
                        found.append(code)
    return "+".join(sorted(found))


def normalize_title(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")      # drop (USA), [!], (Rev 1)
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def game_key(platform: str, title: str, region: str, chips: list[str]) -> str:
    return "|".join((platform.lower(), normalize_title(title), region,
                     ",".join(sorted(set(chips)))))


class Catalog:
    def __init__(self, path: Path | None):
        self.enabled = path is not None
        self._lock = threading.Lock()
        self.db = None
        if not self.enabled:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(path), check_same_thread=False)
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("INSERT OR REPLACE INTO info VALUES ('schema', ?)", (SCHEMA_VERSION,))
            self.db.commit()
        except (sqlite3.Error, OSError) as e:
            log.warning("catalog disabled (%s): %s", path, e)
            self.enabled, self.db = False, None

    # --- probe cache ---------------------------------------------------------------------
    def get_probe(self, rel: str, size: int, mtime: float, engine: str):
        if not self.enabled:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT engine, tracks FROM files WHERE path=? AND size=? AND mtime=?",
                (rel, size, mtime)).fetchone()
        if not row or row[0] != engine or not row[1]:
            return None
        try:
            return [TrackInfo(**d) for d in json.loads(row[1])]
        except (TypeError, ValueError):
            return None

    def put_probe(self, rel: str, size: int, mtime: float, engine: str,
                  tracks: list[TrackInfo], chips: list[str]):
        if not self.enabled:
            return
        data = json.dumps([asdict(t) for t in tracks], ensure_ascii=False)
        with self._lock:
            old = self.db.execute("SELECT size, mtime, sha1 FROM files WHERE path=?",
                                  (rel,)).fetchone()
            sha = old[2] if old and old[0] == size and old[1] == mtime else None
            self.db.execute(
                "INSERT OR REPLACE INTO files (path, size, mtime, engine, tracks, sha1, chips,"
                " source, state, last_seen) VALUES (?,?,?,?,?,?,?, 'local', 'local', ?)",
                (rel, size, mtime, engine, data, sha, ",".join(chips), time.time()))
            self.db.commit()

    def chips(self, rel: str) -> list[str]:
        if not self.enabled:
            return []
        with self._lock:
            row = self.db.execute("SELECT chips FROM files WHERE path=?", (rel,)).fetchone()
        return [c for c in (row[0] or "").split(",") if c] if row else []

    def touch(self, rels: list[str]):
        if not self.enabled or not rels:
            return
        now = time.time()
        with self._lock:
            self.db.executemany("UPDATE files SET last_seen=? WHERE path=?",
                                [(now, r) for r in rels])
            self.db.commit()

    def prune(self, older_than: float) -> int:
        """Remove rows for files that were not seen in the last scan."""
        if not self.enabled:
            return 0
        with self._lock:
            cur = self.db.execute("DELETE FROM files WHERE source='local' AND"
                                  " (last_seen IS NULL OR last_seen < ?)", (older_than,))
            self.db.commit()
            return cur.rowcount

    # --- hashes ---------------------------------------------------------------------------
    def sha1(self, rel: str) -> str | None:
        if not self.enabled:
            return None
        with self._lock:
            row = self.db.execute("SELECT sha1 FROM files WHERE path=?", (rel,)).fetchone()
        return row[0] if row else None

    def hash_missing(self, root: Path, limit: int = 100000) -> int:
        """Compute SHA-1 for files that don't have one yet. Returns how many were hashed."""
        if not self.enabled:
            return 0
        with self._lock:
            rows = self.db.execute("SELECT path FROM files WHERE sha1 IS NULL LIMIT ?",
                                   (limit,)).fetchall()
        n = 0
        for (rel,) in rows:
            p = root / rel
            h = hashlib.sha1()
            try:
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            with self._lock:
                self.db.execute("UPDATE files SET sha1=? WHERE path=?", (h.hexdigest(), rel))
                if n % 200 == 0:
                    self.db.commit()
            n += 1
        if n:
            with self._lock:
                self.db.commit()
        return n

    def duplicate_groups(self) -> list[list[str]]:
        if not self.enabled:
            return []
        with self._lock:
            rows = self.db.execute(
                "SELECT sha1, group_concat(path, char(10)) FROM files WHERE sha1 IS NOT NULL"
                " GROUP BY sha1 HAVING count(*) > 1").fetchall()
        return [sorted(r[1].split("\n")) for r in rows]

    def stats(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            total, hashed = self.db.execute(
                "SELECT count(*), count(sha1) FROM files").fetchone()
        return {"enabled": True, "files": total, "hashed": hashed}
