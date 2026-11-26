"""Playlists, stored as one JSON file each in <meta>/_playlists/.

A playlist entry keeps the song id and also the file path and subtrack, so an entry can be
found again if a file is moved and its id changes. The files are plain JSON, so they are
easy to back up, read and edit by hand.

  {"id": "pl-…", "name": "Boss themes", "comment": "", "created": "…", "changed": "…",
   "songs": [{"id": "tr-…", "path": "nes/Mega Man 2.nsf", "sub": 10}, …]}
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("playlists")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Playlists:
    def __init__(self, meta_root: Path | None, writable: bool, index_fn):
        """index_fn() -> the current library Index (songs by id)."""
        self.dir = meta_root / "_playlists" if meta_root else None
        self.writable = bool(writable and self.dir)
        self.index = index_fn
        self._lock = threading.Lock()
        if self.writable:
            self.dir.mkdir(parents=True, exist_ok=True)

    # --- storage -------------------------------------------------------------------
    def _path(self, pid: str) -> Path:
        if not pid.startswith("pl-") or not pid[3:].isalnum():
            raise KeyError("no such playlist")
        return self.dir / f"{pid}.json"

    def _read(self, pid: str) -> dict:
        if not self.dir:
            raise KeyError("no such playlist")
        p = self._path(pid)
        if not p.is_file():
            raise KeyError("no such playlist")
        return json.loads(p.read_text("utf-8"))

    def _write(self, pl: dict):
        if not self.writable:
            raise PermissionError("the meta folder is not writable")
        p = self._path(pl["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(pl, ensure_ascii=False, indent=1) + "\n", "utf-8")
        tmp.replace(p)

    # --- entries ---------------------------------------------------------------------
    def _entry(self, song_id: str) -> dict | None:
        s = self.index().songs.get(song_id)
        return {"id": s.id, "path": s.rel, "sub": s.sub} if s else None

    def _found(self, entries: list) -> list:
        """[(position in the file, song or None)]. A moved file is found by path + subtrack."""
        ix = self.index()
        by_path = None
        out = []
        for i, e in enumerate(entries):
            s = ix.songs.get(e.get("id", ""))
            if s is None and e.get("path"):
                if by_path is None:
                    by_path = {(x.rel, x.sub): x for x in ix.songs.values()}
                s = by_path.get((e["path"], int(e.get("sub", 0))))
            out.append((i, s))
        return out

    def resolve(self, entries: list) -> list:
        """Entries -> songs in the index. Entries whose files are gone are left out here,
        but stay in the file, so they come back if the file does."""
        return [s for _, s in self._found(entries) if s is not None]

    # --- API ---------------------------------------------------------------------------
    def list(self) -> list[dict]:
        if not self.dir or not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("pl-*.json")):
            try:
                out.append(json.loads(p.read_text("utf-8")))
            except (OSError, ValueError) as e:
                log.warning("skipping playlist %s: %s", p.name, e)
        return sorted(out, key=lambda x: x.get("name", "").lower())

    def get(self, pid: str) -> dict:
        return self._read(pid)

    def create(self, name: str, song_ids: list[str] = (), comment: str = "") -> dict:
        with self._lock:
            pl = {"id": "pl-" + secrets.token_hex(6), "name": name.strip() or "New playlist",
                  "comment": comment, "created": _now(), "changed": _now(),
                  "songs": [e for e in map(self._entry, song_ids) if e]}
            self._write(pl)
            return pl

    def update(self, pid: str, name: str | None = None, song_ids: list[str] | None = None,
               add: list[str] = (), remove_at: list[int] = (), comment: str | None = None) -> dict:
        """Rename, replace the songs, append songs or remove positions."""
        with self._lock:
            pl = self._read(pid)
            if name is not None:
                pl["name"] = name.strip() or pl["name"]
            if comment is not None:
                pl["comment"] = comment
            found = self._found(pl["songs"])
            missing = [pl["songs"][i] for i, s in found if s is None]
            if song_ids is not None:
                # the new order of the songs that exist; missing entries are kept at the end
                pl["songs"] = [e for e in map(self._entry, song_ids) if e] + missing
            elif remove_at:
                # positions as clients see them: among the songs that exist
                present = [i for i, s in found if s is not None]
                drop = {present[int(i)] for i in remove_at if 0 <= int(i) < len(present)}
                pl["songs"] = [e for i, e in enumerate(pl["songs"]) if i not in drop]
            pl["songs"] += [e for e in map(self._entry, add) if e]
            pl["changed"] = _now()
            self._write(pl)
            return pl

    def delete(self, pid: str):
        with self._lock:
            if not self.writable:
                raise PermissionError("the meta folder is not writable")
            p = self._path(pid)
            if not p.is_file():
                raise KeyError("no such playlist")
            p.unlink()
