"""Sequenced Nintendo music (JAudio2 BMS: Super Mario Galaxy 1/2, Twilight Princess ...).

Our own engine, written from the decompiled game code (zeldaret/tp, SMGCommunity/Petari):
seq.py runs the sequence program, banks.py reads the instrument banks (.baa) and samples
(.aw), render.py mixes the voices. A .bms needs its game's .baa; we look for it next to
the file, in the parent folder and in sibling folders such as "Waves".
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from ..base import Engine, EngineError, TrackInfo

log = logging.getLogger("retrorender.jaudio")

try:
    import numpy  # noqa: F401
    _HAVE_NUMPY = True
except ImportError:          # pragma: no cover
    _HAVE_NUMPY = False

_bank_lock = threading.Lock()


@lru_cache(maxsize=4)
def _bankset(baa: str):
    from .banks import BankSet
    return BankSet(Path(baa))


def find_baa(bms: Path) -> Optional[Path]:
    """The bank archive for a sequence: same folder, parent, or a sibling folder."""
    seen = []
    for d in (bms.parent, bms.parent.parent):
        seen.append(d)
        if d.is_dir():
            seen += [c for c in sorted(d.iterdir()) if c.is_dir()]
    for d in seen:
        hits = sorted(d.glob("*.baa")) if d.is_dir() else []
        if hits:
            return hits[0]
    return None


STREAM_EXT = (".ast", ".brstm", ".bfstm", ".bcstm", ".dsp", ".adx", ".hca", ".txtp", ".bwav")


def streamed_twin(bms: Path) -> Optional[Path]:
    """Galaxy ships sequences next to orchestral recordings of the same piece: the
    sequence ('smg_boss07_midi_cnv') then only carries beat markers and layers the game
    plays on top of the recording ('SMG_boss07_strm.ast'). Such a sequence is not a
    track of its own."""
    import re
    base = re.sub(r"(_midi)?_cnv$", "", bms.stem.lower())
    for d in {bms.parent, bms.parent.parent}:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            name = f.name.lower()
            if not name.endswith(STREAM_EXT):
                continue
            stem = name.split(".")[0]
            if stem == base or stem.startswith(base + "_"):
                return f
    return None


class JAudioEngine(Engine):
    name = "jaudio"
    extensions = (".bms",)

    def __init__(self, sample_rate: int = 44100, fx_send: float = 0.35):
        self.sample_rate = sample_rate
        self.fx_send = fx_send

    def available(self) -> bool:
        return _HAVE_NUMPY

    def _banks(self, path: Path):
        baa = find_baa(path)
        if baa is None:
            raise EngineError("no .baa instrument bank found near the .bms file")
        with _bank_lock:
            return _bankset(str(baa))

    def probe(self, path: Path) -> list[TrackInfo]:
        from .render import analyse
        self._banks(path)                       # fail early if the banks are missing
        if streamed_twin(path):
            return []
        loop, end, audible = analyse(path.read_bytes())
        if not audible:
            # e.g. Galaxy's *_midi_cnv files: only beat/bar markers for streamed music
            return []
        t = TrackInfo(index=0, title="", sample_rate=self.sample_rate,
                      extra={"jaudio": "marker tracks muted"})
        if loop and loop[1] > loop[0]:
            t.intro_s, t.loop_s = round(loop[0], 3), round(loop[1] - loop[0], 3)
        else:
            t.length_s = round(end + 1.5, 3)    # let the last notes ring out
        return [t]

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        import numpy as np
        from .render import stream
        banks = self._banks(path)
        fx = self.fx_send
        left = frames
        for chunk in stream(path.read_bytes(), banks, self.sample_rate,
                            frames / self.sample_rate + 0.05, fx_send=fx):
            pcm = (np.clip(chunk, -1, 1) * 32767).astype("<i2")[:left]
            left -= len(pcm)
            yield pcm.tobytes()
            if left <= 0:
                break
