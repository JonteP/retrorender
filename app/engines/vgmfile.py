"""Reads the VGM/VGZ header: which sound chips are used, length, loop and GD3 tags.

Used to pick the engine: libgme only handles Sega's chips (SN76489, YM2413, YM2612),
everything else (YM2151, OPN/OPL, OKI, NES APU, Game Boy ...) goes to libvgm.
"""
from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field
from pathlib import Path

VGM_RATE = 44100

# offset -> chip name (clock fields in the header, per the VGM specification)
CLOCKS = {
    0x0C: "SN76489", 0x10: "YM2413", 0x2C: "YM2612", 0x30: "YM2151",
    0x38: "SegaPCM", 0x40: "RF5C68", 0x44: "YM2203", 0x48: "YM2608",
    0x4C: "YM2610", 0x50: "YM3812", 0x54: "YM3526", 0x58: "Y8950",
    0x5C: "YMF262", 0x60: "YMF278B", 0x64: "YMF271", 0x68: "YMZ280B",
    0x6C: "RF5C164", 0x70: "PWM", 0x74: "AY8910", 0x80: "GB DMG",
    0x84: "NES APU", 0x88: "MultiPCM", 0x8C: "uPD7759", 0x90: "OKIM6258",
    0x98: "OKIM6295", 0x9C: "K051649", 0xA0: "K054539", 0xA4: "HuC6280",
    0xA8: "C140", 0xAC: "K053260", 0xB0: "Pokey", 0xB4: "QSound",
    0xB8: "SCSP", 0xC0: "WonderSwan", 0xC4: "VSU", 0xC8: "SAA1099",
    0xCC: "ES5503", 0xD0: "ES5506", 0xD8: "X1-010", 0xDC: "C352", 0xE0: "GA20",
}
GME_CHIPS = {"SN76489", "YM2413", "YM2612"}

# order of the GD3 fields
_GD3 = ("title", "title_jp", "game", "game_jp", "system", "system_jp",
        "author", "author_jp", "date", "ripper", "notes")


@dataclass
class VgmHeader:
    version: int
    chips: list[str]
    total_s: float
    loop_s: float | None
    intro_s: float | None
    tags: dict = field(default_factory=dict)

    @property
    def gme_compatible(self) -> bool:
        return bool(self.chips) and set(self.chips) <= GME_CHIPS


def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse(path: Path) -> VgmHeader:
    d = read_bytes(path)
    if d[:4] != b"Vgm ":
        raise ValueError("not a VGM file")
    u32 = lambda o: struct.unpack_from("<I", d, o)[0] if o + 4 <= len(d) else 0
    version = u32(0x08)
    data_start = 0x40
    if version >= 0x150 and u32(0x34):
        data_start = 0x34 + u32(0x34)
    chips = []
    for off, name in CLOCKS.items():
        if off + 4 <= data_start and u32(off) & 0x3FFFFFFF:
            chips.append(name)
    total = u32(0x18) / VGM_RATE
    loop_samples = u32(0x20) if u32(0x1C) else 0
    loop = loop_samples / VGM_RATE if loop_samples else None
    intro = max(0.0, total - loop) if loop else None

    tags = {}
    gd3 = 0x14 + u32(0x14) if u32(0x14) else 0
    if gd3 and d[gd3:gd3 + 4] == b"Gd3 ":
        n = u32(gd3 + 8)
        raw = d[gd3 + 12: gd3 + 12 + n].decode("utf-16-le", "replace").split("\0")
        tags = {k: v.strip() for k, v in zip(_GD3, raw)}
    return VgmHeader(version, chips, total, loop, intro, tags)
