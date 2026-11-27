"""Settings changed from the web UI, stored in <meta>/_settings.toml.

config.toml is read-only in the container, so settings that you change in the web UI live
here instead. The file is plain TOML and can be edited by hand; unknown keys are kept.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import tomllib
from pathlib import Path

log = logging.getLogger("settings")

COVER_SOURCES = {
    "rip": "folder.jpg, cover.png and similar in the rip's folder",
    "libretro": "RetroArch's collection of box art, title screens, screenshots and logos",
}
LIBRETRO_TYPES = {"boxart": "Named_Boxarts", "title": "Named_Titles",
                  "snap": "Named_Snaps", "logo": "Named_Logos"}
SHAPES = ("crop", "pad", "original")

DEFAULTS = {
    "covers": {
        # sources in priority order; a cover you made yourself always comes first
        "order": ["libretro", "rip"],
        "enabled": ["libretro", "rip"],
        "auto": True,                    # fetch covers for albums in the background
        "libretro_type": "boxart",       # boxart | title | snap | logo
        "shape": "crop",                 # non-square images: crop | pad | original
        "regions": ["USA", "World", "Europe", "Japan"],   # preferred release, in order
        "libretro_systems": {},          # extra console -> libretro system name
        "album_regions": {},             # album key -> release to prefer for that album
    },
}

HEADER = """\
# retrorender settings changed in the web UI (Settings page).
# config.toml is read-only in the container, so these live here. You can edit this
# file by hand; restart or save in the web UI to apply.
"""


def _tv(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_tv(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k, ensure_ascii=False)


def _table(out: list, name: str, vals: dict):
    """A TOML table and, after it, its sub-tables (any depth)."""
    out.append(f"[{name}]")
    out += [f"{_key(k)} = {_tv(v)}" for k, v in vals.items() if not isinstance(v, dict)]
    out.append("")
    for k, v in vals.items():
        if isinstance(v, dict):
            _table(out, f"{name}.{_key(k)}", v)


def dumps(data: dict) -> str:
    out = [HEADER]
    for section, vals in data.items():
        _table(out, _key(section), vals)
    return "\n".join(out)


def _repair(v):
    """0.16.0–0.16.2 wrote nested tables as a Python dict in a string; read those back."""
    if isinstance(v, dict):
        return {k: _repair(x) for k, x in v.items()}
    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
        import ast
        try:
            x = ast.literal_eval(v)
            return _repair(x) if isinstance(x, dict) else v
        except (ValueError, SyntaxError):
            return v
    return v


class Settings:
    def __init__(self, meta_root: Path | None, writable: bool):
        self.path = meta_root / "_settings.toml" if meta_root else None
        self.writable = bool(writable and self.path)
        self._lock = threading.Lock()
        self._data = self._load()
        self.listeners = []            # fn(section, old, new) after a change

    def _load(self) -> dict:
        data = copy.deepcopy(DEFAULTS)
        if self.path and self.path.is_file():
            try:
                user = tomllib.loads(self.path.read_text("utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as e:
                log.warning("ignoring %s: %s", self.path, e)
                user = {}
            for sec, vals in user.items():
                if isinstance(vals, dict):
                    data.setdefault(sec, {}).update(_repair(vals))
        return data

    def get(self, section: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data.get(section, {}))

    def covers(self) -> dict:
        c = self.get("covers")
        known = [s for s in c.get("order", []) if s in COVER_SOURCES]
        c["order"] = known + [s for s in COVER_SOURCES if s not in known]
        c["enabled"] = [s for s in c.get("enabled", []) if s in COVER_SOURCES]
        if c.get("libretro_type") not in LIBRETRO_TYPES:
            c["libretro_type"] = "boxart"
        if c.get("shape") not in SHAPES:
            c["shape"] = "crop"
        return c

    def update(self, section: str, values: dict) -> dict:
        """Merges values into a section, checks them and writes the file."""
        if not self.writable:
            raise PermissionError("the meta folder is not writable")
        with self._lock:
            old = copy.deepcopy(self._data.get(section, {}))
            new = {**old, **values}
            if section == "covers":
                self._check_covers(new)
            elif section == "online":
                self._check_online(new)
            else:
                raise ValueError(f"unknown section {section}")
            self._data[section] = new
            text = dumps(self._data)
            tomllib.loads(text)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            tmp.replace(self.path)
        for fn in self.listeners:
            try:
                fn(section, old, new)
            except Exception:       # noqa: BLE001 - a listener must not break saving
                log.exception("settings listener failed")
        return new

    @staticmethod
    def _check_online(c: dict):
        lim = c.get("temp_limit_gb", 20)
        if not isinstance(lim, (int, float)) or lim < 0:
            raise ValueError("temp_limit_gb must be a number of GB")
        for name, sc in (c.get("sources") or {}).items():
            if not isinstance(sc, dict) or not isinstance(sc.get("enabled", False), bool):
                raise ValueError(f"source {name}: enabled must be true or false")
            if not all(isinstance(x, str) and x.isalnum() for x in sc.get("systems", [])):
                raise ValueError(f"source {name}: systems must be a list of names")

    @staticmethod
    def _check_covers(c: dict):
        if not set(c.get("order", [])) <= set(COVER_SOURCES):
            raise ValueError("unknown cover source")
        if not set(c.get("enabled", [])) <= set(COVER_SOURCES):
            raise ValueError("unknown cover source")
        if c.get("libretro_type") not in LIBRETRO_TYPES:
            raise ValueError("libretro_type must be one of " + ", ".join(LIBRETRO_TYPES))
        if c.get("shape") not in SHAPES:
            raise ValueError("shape must be one of " + ", ".join(SHAPES))
        if not isinstance(c.get("auto"), bool):
            raise ValueError("auto must be true or false")
        if not all(isinstance(x, str) for x in c.get("regions", [])):
            raise ValueError("regions must be a list of names")
        if not isinstance(c.get("libretro_systems", {}), dict):
            raise ValueError("libretro_systems must be a table")
        if not isinstance(c.get("album_regions", {}), dict):
            raise ValueError("album_regions must be a table")
