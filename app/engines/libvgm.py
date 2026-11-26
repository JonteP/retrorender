"""libvgm via the vgm2wav tool: VGM/VGZ/S98/DRO with almost every sound chip.

vgm2wav runs as a subprocess. Its arguments are a template in config.toml
([engines] vgm2wav_args) so they can be adjusted without a rebuild. Placeholders:
{in} input file, {loops} number of loops, {rate} sample rate.
The output (WAV on stdout) is normalised with ffmpeg to stereo s16le.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

from . import vgmfile
from .base import Engine, EngineError, TrackInfo

DEFAULT_ARGS = ["--loops={loops}", "--samplerate={rate}", "{in}", "-"]


class LibvgmEngine(Engine):
    name = "libvgm"
    extensions = (".vgm", ".vgz", ".s98", ".dro")

    def __init__(self, sample_rate: int = 44100, cli: str = "vgm2wav",
                 args: list[str] | None = None, ffmpeg: str = "ffmpeg"):
        self.sample_rate = sample_rate
        self.cli = shutil.which(cli)
        self.ffmpeg = shutil.which(ffmpeg)
        self.args = args or DEFAULT_ARGS

    insight = "registers"

    def available(self) -> bool:
        return bool(self.cli and self.ffmpeg)

    def voices(self, path: Path, track: TrackInfo | None = None) -> list[str]:
        """The sound chips (vgm2wav gives no per-voice access)."""
        if path.suffix.lower() not in (".vgm", ".vgz"):
            return []
        return list(vgmfile.parse(path).chips)

    def selftest(self) -> str:
        """First lines of vgm2wav's help text, so the flags can be checked via /health."""
        try:
            r = subprocess.run([self.cli, "--help"], capture_output=True, text=True, timeout=5)
            txt = (r.stdout + r.stderr).strip().splitlines()
            return "\n".join(txt[:25])
        except Exception as e:
            return f"could not run vgm2wav: {e}"

    def probe(self, path: Path) -> list[TrackInfo]:
        if path.suffix.lower() not in (".vgm", ".vgz"):
            # S98/DRO: no header parser of our own yet, length left unknown
            return [TrackInfo(index=0, title=path.stem, sample_rate=self.sample_rate)]
        h = vgmfile.parse(path)
        t = h.tags
        return [TrackInfo(
            index=0, title=t.get("title") or path.stem, author=t.get("author", ""),
            game=t.get("game", ""), system=t.get("system", ""),
            length_s=None if h.loop_s else (h.total_s or None),
            intro_s=h.intro_s, loop_s=h.loop_s, sample_rate=self.sample_rate,
            extra={"chips": h.chips, "date": t.get("date", "")})]

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        want = frames / self.sample_rate
        loops = 1
        if track.loop_s:
            loops = max(1, math.ceil((want - (track.intro_s or 0)) / track.loop_s) + 1)
        args = [a.format(**{"in": str(path), "loops": loops, "rate": self.sample_rate})
                for a in self.args]
        p1 = subprocess.Popen([self.cli, *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p2 = subprocess.Popen(
            [self.ffmpeg, "-v", "error", "-f", "wav", "-i", "pipe:0", "-ac", "2",
             "-ar", str(self.sample_rate), "-f", "s16le", "pipe:1"],
            stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p1.stdout.close()
        got = 0
        try:
            left = frames * 4
            while left > 0:
                b = p2.stdout.read(min(65536, left))
                if not b:
                    break
                got += len(b)
                yield b
                left -= len(b)
            if got == 0:
                err = p1.stderr.read().decode("utf-8", "replace").strip()
                raise EngineError(f"vgm2wav produced no audio: {err[:300]}")
        finally:
            for p in (p2, p1):
                if p.poll() is None:
                    p.kill()
                p.wait()
