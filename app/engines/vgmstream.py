"""vgmstream-cli as a subprocess: hundreds of streamed game formats (ADX, HCA, BRSTM, AFC ...).

vgmstream writes WAV at the track's own rate and channel count. We pass it through
ffmpeg to always get 2-channel s16le at the service's sample rate.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

from .base import Engine, EngineError, TrackInfo

_EXT = (
    ".adx", ".ahx", ".aix", ".hca", ".afc", ".ast", ".dsp", ".brstm", ".bcstm",
    ".bfstm", ".bwav", ".bcwav", ".strm", ".idsp", ".at3", ".at9", ".vag", ".xa",
    ".xwb", ".xwav", ".wem", ".bnk", ".fsb", ".bfwav", ".lopus", ".mus", ".txtp",
    ".sab", ".aus", ".mib", ".nwa", ".ogg", ".ktss", ".acb", ".awb", ".genh",
)


class VgmstreamEngine(Engine):
    name = "vgmstream"
    extensions = _EXT

    def __init__(self, sample_rate: int = 44100, cli: str = "vgmstream-cli",
                 ffmpeg: str = "ffmpeg"):
        self.sample_rate = sample_rate
        self.cli = shutil.which(cli)
        self.ffmpeg = shutil.which(ffmpeg)

    def available(self) -> bool:
        return bool(self.cli and self.ffmpeg)

    def _meta(self, path: Path, sub: int) -> dict:
        args = [self.cli, "-m"]
        if sub > 0:
            args += ["-s", str(sub)]
        r = subprocess.run(args + [str(path)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise EngineError(f"vgmstream: {r.stderr.strip() or r.stdout.strip()}")
        txt = r.stdout

        def num(pat):
            m = re.search(pat, txt, re.I | re.M)
            return int(m.group(1)) if m else None

        return {
            "rate": num(r"^sample rate:\s*(\d+)"),
            "total": num(r"^stream total samples:\s*(\d+)"),
            "loop_start": num(r"^loop start:\s*(\d+)"),
            "loop_end": num(r"^loop end:\s*(\d+)"),
            "streams": num(r"^stream count:\s*(\d+)") or 1,
            "name": (re.search(r"^stream name:\s*(.+)$", txt, re.M) or [None, ""])[1],
        }

    def probe(self, path: Path) -> list[TrackInfo]:
        first = self._meta(path, 0)
        out = []
        for i in range(first["streams"]):
            m = first if i == 0 else self._meta(path, i + 1)
            rate = m["rate"] or self.sample_rate
            ls, le = m["loop_start"], m["loop_end"]
            looped = ls is not None and le is not None and le > ls
            out.append(TrackInfo(
                index=i, title=m["name"] or path.stem,
                length_s=None if looped or not m["total"] else m["total"] / rate,
                intro_s=ls / rate if looped else None,
                loop_s=(le - ls) / rate if looped else None,
                sample_rate=self.sample_rate, extra={"native_rate": rate}))
        return out

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        # Many loops, no fade: the framework cuts and fades out itself.
        cmd = [self.cli, "-p", "-l", "100", "-f", "0"]
        if track.index > 0:
            cmd += ["-s", str(track.index + 1)]
        cmd.append(str(path))
        ff = [self.ffmpeg, "-v", "error", "-f", "wav", "-i", "pipe:0",
              "-ac", "2", "-ar", str(self.sample_rate), "-f", "s16le", "pipe:1"]
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(ff, stdin=p1.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        p1.stdout.close()
        try:
            left = frames * 4
            while left > 0:
                b = p2.stdout.read(min(65536, left))
                if not b:
                    break
                yield b
                left -= len(b)
        finally:
            for p in (p2, p1):
                if p.poll() is None:
                    p.kill()
                p.wait()
