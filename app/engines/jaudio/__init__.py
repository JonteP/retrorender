"""Sequenced Nintendo GameCube/Wii music (JAudio).

Our own engine, written from the decompiled game code (zeldaret/tp, zeldaret/tww,
SMGCommunity/Petari):
- JAudio2 (Super Mario Galaxy, Twilight Princess): loose .bms files + a .baa bank archive
  (seq.py, banks.py)
- JAudio 1 (Wind Waker): sequences packed in a RARC archive (JaiSeqs.arc) + JaiInit.aaf
  with banks, samples in Banks/*.aw (seq1.py, banks1.py, rarc.py)
render.py mixes the voices for both. The bank file is looked up next to the sequence, in
the parent folder and in sibling folders such as "Waves".
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
def _bankset(bankfile: str):
    p = Path(bankfile)
    if p.suffix.lower() == ".aaf":
        from .banks1 import BankSet1
        return BankSet1(p)
    from .banks import BankSet
    return BankSet(p)


@lru_cache(maxsize=4)
def _archive(path: str, mtime: float):
    """Sequences in a RARC archive, in archive order: [(name, data)]."""
    from .rarc import read_rarc
    files = read_rarc(Path(path).read_bytes())
    return [(n, d) for n, d in files.items() if n.lower().endswith(".bms")]


def find_banks(seq: Path) -> Optional[Path]:
    """The bank file for a sequence (.baa, or JaiInit.aaf for JAudio 1): same folder,
    parent, or a sibling folder."""
    seen = []
    for d in (seq.parent, seq.parent.parent):
        seen.append(d)
        if d.is_dir():
            seen += [c for c in sorted(d.iterdir()) if c.is_dir()]
    for pattern in ("*.baa", "*.aaf"):
        for d in seen:
            hits = sorted(d.glob(pattern)) if d.is_dir() else []
            if hits:
                return hits[0]
    return None


find_baa = find_banks


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
    extensions = (".bms", ".arc")

    def __init__(self, sample_rate: int = 44100, fx_send: float = 0.35):
        self.sample_rate = sample_rate
        self.fx_send = fx_send

    def available(self) -> bool:
        return _HAVE_NUMPY

    def _banks(self, path: Path):
        bankfile = find_banks(path)
        if bankfile is None:
            raise EngineError("no instrument banks (.baa or JaiInit.aaf) found near the sequence")
        with _bank_lock:
            return _bankset(str(bankfile)), (1 if bankfile.suffix.lower() == ".aaf" else 2)

    def _sequences(self, path: Path):
        """[(index, name, data)] for a .bms or a sequence archive."""
        if path.suffix.lower() == ".arc":
            if path.read_bytes()[:4] != b"RARC":
                return []
            return [(i, n, d) for i, (n, d) in enumerate(_archive(str(path), path.stat().st_mtime))]
        return [(0, path.name, path.read_bytes())]

    def probe(self, path: Path) -> list[TrackInfo]:
        from .render import analyse
        seqs = self._sequences(path)
        if not seqs:
            return []                           # some other kind of .arc
        _, version = self._banks(path)          # fail early if the banks are missing
        if path.suffix.lower() == ".bms" and streamed_twin(path):
            return []
        out = []
        for i, name, data in seqs:
            try:
                loop, end, audible = analyse(data, version=version)
            except Exception as e:              # one broken sequence must not hide the rest
                log.warning("%s/%s: %s", path.name, name, e)
                continue
            if not audible:
                # beat markers only (Galaxy *_midi_cnv) or sound-effect programs
                continue
            t = TrackInfo(index=i, title=Path(name).stem if len(seqs) > 1 else "",
                          sample_rate=self.sample_rate, extra={"jaudio": version})
            if loop and loop[1] > loop[0]:
                t.intro_s, t.loop_s = round(loop[0], 3), round(loop[1] - loop[0], 3)
            else:
                t.length_s = round(end + 1.5, 3)    # let the last notes ring out
            out.append(t)
        return out

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        import numpy as np
        from .render import stream
        banks, version = self._banks(path)
        data = next((d for i, _, d in self._sequences(path) if i == track.index), None)
        if data is None:
            raise EngineError(f"sequence {track.index} not found in {path.name}")
        left = frames
        for chunk in stream(data, banks, self.sample_rate, frames / self.sample_rate + 0.05,
                            fx_send=self.fx_send, version=version):
            pcm = (np.clip(chunk, -1, 1) * 32767).astype("<i2")[:left]
            left -= len(pcm)
            yield pcm.tobytes()
            if left <= 0:
                break
