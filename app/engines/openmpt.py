"""libopenmpt via ctypes: MOD, XM, IT, S3M, MPTM och många fler trackerformat."""
from __future__ import annotations

import ctypes as C
from pathlib import Path
from typing import Iterator

from .base import Engine, EngineError, TrackInfo


class OpenmptEngine(Engine):
    name = "openmpt"
    extensions = (".mod", ".xm", ".it", ".s3m", ".mptm", ".mtm", ".669", ".ult",
                  ".med", ".okt", ".far", ".dbm", ".digi", ".psm", ".stm", ".amf",
                  ".dsm", ".imf", ".j2b", ".mdl", ".mt2", ".ptm", ".umx", ".plm",
                  ".gdm", ".dtm", ".symmod", ".fmt", ".ice", ".st", ".stp", ".dmf")

    def __init__(self, sample_rate: int = 44100, libname: str = "libopenmpt.so.0"):
        self.sample_rate = sample_rate
        try:
            self.lib = C.CDLL(libname)
        except OSError:
            self.lib = None
            return
        L = self.lib
        L.openmpt_module_create_from_memory2.argtypes = [
            C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p,
            C.POINTER(C.c_int), C.POINTER(C.c_char_p), C.c_void_p]
        L.openmpt_module_create_from_memory2.restype = C.c_void_p
        L.openmpt_module_destroy.argtypes = [C.c_void_p]
        L.openmpt_module_get_num_subsongs.argtypes = [C.c_void_p]
        L.openmpt_module_get_num_subsongs.restype = C.c_int32
        L.openmpt_module_select_subsong.argtypes = [C.c_void_p, C.c_int32]
        L.openmpt_module_select_subsong.restype = C.c_int
        L.openmpt_module_set_repeat_count.argtypes = [C.c_void_p, C.c_int32]
        L.openmpt_module_get_duration_seconds.argtypes = [C.c_void_p]
        L.openmpt_module_get_duration_seconds.restype = C.c_double
        L.openmpt_module_read_interleaved_stereo.argtypes = [
            C.c_void_p, C.c_int32, C.c_size_t, C.POINTER(C.c_int16)]
        L.openmpt_module_read_interleaved_stereo.restype = C.c_size_t
        L.openmpt_module_get_metadata.argtypes = [C.c_void_p, C.c_char_p]
        L.openmpt_module_get_metadata.restype = C.c_void_p
        L.openmpt_free_string.argtypes = [C.c_void_p]

    def available(self) -> bool:
        return self.lib is not None

    def _open(self, path: Path):
        data = path.read_bytes()
        buf = C.create_string_buffer(data, len(data))
        err = C.c_int(0)
        msg = C.c_char_p()
        mod = self.lib.openmpt_module_create_from_memory2(
            buf, len(data), None, None, None, None, C.byref(err), C.byref(msg), None)
        if not mod:
            raise EngineError(f"openmpt: kunde inte öppna ({err.value})")
        return mod

    def _meta(self, mod, key: str) -> str:
        p = self.lib.openmpt_module_get_metadata(mod, key.encode())
        if not p:
            return ""
        s = C.cast(p, C.c_char_p).value or b""
        self.lib.openmpt_free_string(p)
        return s.decode("utf-8", "replace")

    def probe(self, path: Path) -> list[TrackInfo]:
        mod = self._open(path)
        try:
            n = max(1, self.lib.openmpt_module_get_num_subsongs(mod))
            title, artist = self._meta(mod, "title"), self._meta(mod, "artist")
            kind = self._meta(mod, "type_long")
            out = []
            for i in range(n):
                self.lib.openmpt_module_select_subsong(mod, i)
                dur = self.lib.openmpt_module_get_duration_seconds(mod)
                # Trackerlåtar hoppar oftast tillbaka till början: hela låten = en loop.
                out.append(TrackInfo(
                    index=i, title=title if n == 1 else f"{title} ({i + 1})",
                    author=artist, system=kind, intro_s=0.0,
                    loop_s=dur if dur > 0 else None, sample_rate=self.sample_rate))
            return out
        finally:
            self.lib.openmpt_module_destroy(mod)

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        mod = self._open(path)
        try:
            self.lib.openmpt_module_select_subsong(mod, track.index)
            self.lib.openmpt_module_set_repeat_count(mod, -1)   # oändligt, vi kapar
            chunk = 4096
            buf = (C.c_int16 * (chunk * 2))()
            left = frames
            while left > 0:
                got = self.lib.openmpt_module_read_interleaved_stereo(
                    mod, self.sample_rate, min(chunk, left), buf)
                if got == 0:
                    break                                    # ramverket fyller ut
                yield bytes(memoryview(buf).cast("B")[: got * 4])
                left -= got
        finally:
            self.lib.openmpt_module_destroy(mod)
