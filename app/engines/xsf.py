"""xSF (the PSF family) through xsfplay, our own small CLI around emulator cores:

  .psf  .minipsf    PlayStation          Highly Experimental   needs a BIOS image
  .psf2 .minipsf2   PlayStation 2        Highly Experimental   needs a BIOS image
  .ssf  .minissf    Saturn               Highly Theoretical
  .dsf  .minidsf    Dreamcast            Highly Theoretical
  .usf  .miniusf    Nintendo 64          lazyusf2 (low level RSP)
  .gsf  .minigsf    Game Boy Advance     mGBA
  .2sf  .mini2sf    Nintendo DS          DeSmuME (in_xsf), no interpolation

The cores were picked for accuracy over speed. The tags (title, length, fade ...) are read
here; xsfplay only renders. A minifile needs its _lib files next to it.

PS1/PS2 need Sony's BIOS, which we cannot ship: put hebios.bin, or a PS2 BIOS dump (4 MB,
e.g. SCPH-39001), in <meta_dir>/bios/ or set engines.psx_bios.
"""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
import threading
from pathlib import Path
from typing import Iterator, Optional

from .base import Engine, EngineError, TrackInfo

# version byte -> (system, native rate of xsfplay's output; None = any rate we ask for)
SYSTEMS = {
    0x01: ("PlayStation", 44100),
    0x02: ("PlayStation 2", 48000),
    0x11: ("Sega Saturn", 44100),
    0x12: ("Sega Dreamcast", 44100),
    0x21: ("Nintendo 64", None),
    0x22: ("Game Boy Advance", None),
    0x24: ("Nintendo DS", 32728),
}

EXTENSIONS = (".psf", ".minipsf", ".psf2", ".minipsf2", ".ssf", ".minissf", ".dsf", ".minidsf",
              ".usf", ".miniusf", ".gsf", ".minigsf", ".2sf", ".mini2sf")

_BIOS_HINT = "PS1/PS2 needs a BIOS: put hebios.bin or a PS2 BIOS dump in {dir}"


def parse_time(v: str) -> Optional[float]:
    """PSF time tags: "s", "m:ss", "h:mm:ss", with "." or "," before fractions."""
    v = (v or "").strip().replace(",", ".")
    if not v or not re.fullmatch(r"[0-9:.]+", v):
        return None
    try:
        secs = 0.0
        for part in v.split(":"):
            secs = secs * 60 + float(part or 0)
        return secs
    except ValueError:
        return None


def read_header(path: Path) -> tuple[int, dict[str, str]]:
    """(version byte, tags). Repeated keys are joined with newlines, as the spec says."""
    with open(path, "rb") as f:
        head = f.read(16)
        if len(head) < 16 or head[:3] != b"PSF":
            raise EngineError(f"{path.name}: not a PSF file")
        version = head[3]
        res, prog, _crc = struct.unpack("<III", head[4:16])
        f.seek(16 + res + prog)
        raw = f.read(50001)                      # the tag area is limited to 50000 bytes
    tags: dict[str, str] = {}
    if raw[:5] == b"[TAG]":
        body = raw[5:50005]
        # a non-empty utf8 tag says the text is UTF-8; otherwise it is the rip's ANSI codepage
        utf8 = re.search(rb"(?im)^\s*utf8\s*=\s*\S", body) is not None
        text = body.decode("utf-8" if utf8 else "cp1252", "replace")
        for line in text.splitlines():
            if "=" not in line:
                continue
            k, _, val = line.partition("=")
            k, val = k.strip().lower(), val.strip()
            if not k:
                continue
            tags[k] = tags[k] + "\n" + val if k in tags else val
    return version, tags


def lib_chain(path: Path, tags: dict[str, str], depth: int = 0) -> list[str]:
    """The _lib files a minifile needs that are missing next to it."""
    missing = []
    if depth > 10:
        return missing
    for k in sorted(k for k in tags if re.fullmatch(r"_lib\d*", k)):
        p = path.parent / tags[k]
        if not p.is_file():
            # rips made on Windows are case-insensitive about names
            alt = next((c for c in path.parent.iterdir() if c.name.lower() == tags[k].lower()), None) \
                if path.parent.is_dir() else None
            if alt is None:
                missing.append(tags[k])
                continue
            p = alt
        try:
            _v, sub = read_header(p)
        except (OSError, EngineError):
            missing.append(tags[k])
            continue
        missing += lib_chain(p, sub, depth + 1)
    return missing


class XsfEngine(Engine):
    name = "xsf"
    extensions = EXTENSIONS

    def __init__(self, sample_rate: int = 44100, cli: str = "xsfplay", ffmpeg: str = "ffmpeg",
                 bios: Optional[str] = None, meta_dir: Optional[str] = None,
                 ds_jit: bool = False, psx_dma: str = "friendly"):
        self.sample_rate = sample_rate
        self.cli = shutil.which(cli)
        self.ffmpeg = shutil.which(ffmpeg)
        self.bios_setting = bios
        self.bios_dir = Path(meta_dir) / "bios" if meta_dir else None
        self.ds_jit = ds_jit
        self.psx_dma = psx_dma if psx_dma in ("harsh", "friendly") else "friendly"
        self._soxr: Optional[bool] = None

    def available(self) -> bool:
        return bool(self.cli and self.ffmpeg)

    # --- PS1/PS2 BIOS ---------------------------------------------------------------------
    def bios(self) -> Optional[Path]:
        """hebios.bin, else the largest PS2 BIOS dump (>= 4 MB) in <meta>/bios."""
        if self.bios_setting:
            p = Path(self.bios_setting)
            return p if p.is_file() else None
        d = self.bios_dir
        if not d or not d.is_dir():
            return None
        files = [p for p in d.iterdir() if p.is_file()]
        he = [p for p in files if p.name.lower().startswith("hebios")]
        if he:
            return sorted(he)[0]
        big = [p for p in files if p.stat().st_size >= 0x400000]
        return max(big, key=lambda p: p.stat().st_size) if big else None

    def bios_hint(self) -> str:
        return _BIOS_HINT.format(dir=self.bios_setting or self.bios_dir or "engines.psx_bios")

    # --- metadata -------------------------------------------------------------------------
    def probe(self, path: Path) -> list[TrackInfo]:
        version, tags = read_header(path)
        if version not in SYSTEMS:
            raise EngineError(f"{path.name}: unknown PSF version 0x{version:02x}")
        system, native = SYSTEMS[version]
        length = parse_time(tags.get("length", ""))
        fade = parse_time(tags.get("fade", ""))
        extra = {"psf_version": version, "native_rate": native or self.sample_rate}
        for k in ("year", "copyright", "comment", "psfby", "usfby", "gsfby", "2sfby", "ssfby",
                  "dsfby", "genre", "track"):
            if tags.get(k):
                extra[k] = tags[k]
        missing = lib_chain(path, tags)
        if missing:
            extra["missing_lib"] = missing
        info = TrackInfo(index=0, title=tags.get("title") or path.stem,
                         author=tags.get("artist", ""), game=tags.get("game", ""),
                         system=system, sample_rate=self.sample_rate, extra=extra)
        if length:
            # the tags say: play `length`, then fade out over `fade`
            info.length_s = length + (fade or 0)
            if fade:
                extra["fade_at_end"] = True
                extra["fade_s"] = fade
        return [info]

    # --- rendering ------------------------------------------------------------------------
    def _resampler(self) -> list[str]:
        """soxr (very high quality) when ffmpeg has it, else ffmpeg's own resampler."""
        if self._soxr is None:
            try:
                r = subprocess.run([self.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                                    "anullsrc=r=32000:cl=stereo", "-t", "0.05",
                                    "-af", "aresample=44100:resampler=soxr", "-f", "null", "-"],
                                   capture_output=True, timeout=20)
                self._soxr = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                self._soxr = False
        return ["-af", "aresample=resampler=soxr:precision=28"] if self._soxr else []

    def command(self, path: Path, version: int) -> list[str]:
        cmd = [self.cli, "-r", str(self.sample_rate)]
        if version in (0x01, 0x02):
            bios = self.bios()
            if bios is None:
                raise EngineError(self.bios_hint())
            cmd += ["-b", str(bios), "-c", self.psx_dma]
        if version == 0x24 and self.ds_jit:
            cmd.append("-j")
        return cmd + [str(path)]

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        version = (track.extra or {}).get("psf_version")
        if version is None:
            version, _tags = read_header(path)
        missing = (track.extra or {}).get("missing_lib")
        if missing:
            raise EngineError(f"{path.name} needs {', '.join(missing)} next to it")
        native = SYSTEMS.get(version, ("", self.sample_rate))[1] or self.sample_rate
        p1 = subprocess.Popen(self.command(path, version), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        errors: list[bytes] = []
        t = threading.Thread(target=lambda: errors.append(p1.stderr.read()), daemon=True)
        t.start()
        procs = [p1]
        if native == self.sample_rate:
            src = p1.stdout
            head = src.read(44)                     # our own WAV header: skip it
            if len(head) < 44:
                p1.wait()
                t.join(2)
                raise EngineError(self._error(errors, p1))
        else:
            ff = [self.ffmpeg, "-v", "error", "-f", "wav", "-i", "pipe:0", *self._resampler(),
                  "-ac", "2", "-ar", str(self.sample_rate), "-f", "s16le", "pipe:1"]
            p2 = subprocess.Popen(ff, stdin=p1.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
            p1.stdout.close()
            src = p2.stdout
            procs.insert(0, p2)
        try:
            left = frames * 4
            first = True
            while left > 0:
                b = src.read(min(65536, left))
                if not b:
                    if first:
                        p1.wait()
                        t.join(2)
                        raise EngineError(self._error(errors, p1))
                    break
                first = False
                yield b
                left -= len(b)
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                p.wait()

    @staticmethod
    def _error(errors: list[bytes], p) -> str:
        msg = b"".join(errors).decode("utf-8", "replace").strip().splitlines()
        msg = [m for m in msg if m.startswith("xsfplay:")] or msg
        return msg[-1].removeprefix("xsfplay:").strip() if msg else f"xsfplay exited with {p.returncode}"


__all__ = ["XsfEngine", "read_header", "parse_time", "lib_chain", "SYSTEMS", "EXTENSIONS"]
