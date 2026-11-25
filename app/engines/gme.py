"""libgme (Game Music Emu) via ctypes: NSF/NSFE, GBS, SPC, HES, KSS, AY, SAP, VGM/VGZ."""
from __future__ import annotations

import ctypes as C
from pathlib import Path
from typing import Iterator

from .base import Engine, EngineError, TrackInfo


class _GmeInfo(C.Structure):
    _fields_ = (
        [("length", C.c_int), ("intro_length", C.c_int),
         ("loop_length", C.c_int), ("play_length", C.c_int)]
        + [(f"i{i}", C.c_int) for i in range(4, 16)]
        + [(n, C.c_char_p) for n in
           ("system", "game", "song", "author", "copyright", "comment", "dumper")]
        + [(f"s{i}", C.c_char_p) for i in range(7, 16)]
    )


def _s(b: bytes | None) -> str:
    return b.decode("utf-8", "replace") if b else ""


def _ms(v: int):
    return v / 1000.0 if v is not None and v > 0 else None


class GmeEngine(Engine):
    name = "gme"
    extensions = (".nsf", ".nsfe", ".gbs", ".spc", ".hes", ".kss", ".ay",
                  ".sap", ".vgm", ".vgz", ".gym")

    def __init__(self, sample_rate: int = 44100, libname: str = "libgme.so.0"):
        self.sample_rate = sample_rate
        try:
            self.lib = C.CDLL(libname)
        except OSError:
            self.lib = None
            return
        L = self.lib
        L.gme_open_data.argtypes = [C.c_void_p, C.c_long, C.POINTER(C.c_void_p), C.c_int]
        L.gme_open_data.restype = C.c_char_p
        L.gme_track_count.argtypes = [C.c_void_p]
        L.gme_track_count.restype = C.c_int
        L.gme_track_info.argtypes = [C.c_void_p, C.POINTER(C.POINTER(_GmeInfo)), C.c_int]
        L.gme_track_info.restype = C.c_char_p
        L.gme_free_info.argtypes = [C.POINTER(_GmeInfo)]
        L.gme_start_track.argtypes = [C.c_void_p, C.c_int]
        L.gme_start_track.restype = C.c_char_p
        L.gme_play.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_short)]
        L.gme_play.restype = C.c_char_p
        L.gme_delete.argtypes = [C.c_void_p]
        L.gme_ignore_silence.argtypes = [C.c_void_p, C.c_int]
        self._autoload = getattr(L, "gme_set_autoload_playback_limit", None)
        if self._autoload is not None:
            self._autoload.argtypes = [C.c_void_p, C.c_int]

    def available(self) -> bool:
        return self.lib is not None

    def _open(self, path: Path):
        data = path.read_bytes()
        buf = C.create_string_buffer(data, len(data))
        emu = C.c_void_p()
        err = self.lib.gme_open_data(buf, len(data), C.byref(emu), self.sample_rate)
        if err:
            raise EngineError(f"gme: {_s(err)}")
        return emu

    def probe(self, path: Path) -> list[TrackInfo]:
        emu = self._open(path)
        try:
            out = []
            for i in range(self.lib.gme_track_count(emu)):
                p = C.POINTER(_GmeInfo)()
                if self.lib.gme_track_info(emu, C.byref(p), i):
                    out.append(TrackInfo(index=i, sample_rate=self.sample_rate))
                    continue
                inf = p.contents
                loop = _ms(inf.loop_length)
                intro = _ms(inf.intro_length)
                if loop and intro is None:
                    intro = 0.0
                out.append(TrackInfo(
                    index=i, title=_s(inf.song), author=_s(inf.author),
                    game=_s(inf.game), system=_s(inf.system),
                    # length gäller bara om spåret INTE har känd loop
                    length_s=None if loop else _ms(inf.length),
                    intro_s=intro if loop else None, loop_s=loop,
                    sample_rate=self.sample_rate,
                    extra={"date": _s(inf.copyright)},
                ))
                self.lib.gme_free_info(p)
            return out
        finally:
            self.lib.gme_delete(emu)

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        emu = self._open(path)
        try:
            if self._autoload is not None:
                self._autoload(emu, 0)        # ingen inbyggd fade, vi styr längden
            self.lib.gme_ignore_silence(emu, 1)
            err = self.lib.gme_start_track(emu, track.index)
            if err:
                raise EngineError(f"gme: {_s(err)}")
            chunk = 4096                       # ramar per varv
            buf = (C.c_short * (chunk * 2))()
            left = frames
            while left > 0:
                n = min(chunk, left)
                err = self.lib.gme_play(emu, n * 2, buf)
                if err:
                    raise EngineError(f"gme: {_s(err)}")
                yield bytes(memoryview(buf).cast("B")[: n * 4])
                left -= n
        finally:
            self.lib.gme_delete(emu)
