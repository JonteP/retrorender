"""libvgm via verktyget vgm2wav: VGM/VGZ/S98/DRO med nästan alla ljudchip.

vgm2wav anropas som subprocess. Argumenten är en mall i config.toml
([engines] vgm2wav_args) så att de kan justeras utan ombyggnad. Platshållare:
{in} indatafil, {loops} antal loopar, {rate} samplingsfrekvens.
Utdatan (WAV på stdout) normaliseras med ffmpeg till stereo s16le.
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

    def available(self) -> bool:
        return bool(self.cli and self.ffmpeg)

    def selftest(self) -> str:
        """Första raderna av vgm2wav:s hjälptext, så att flaggorna kan kontrolleras via /health."""
        try:
            r = subprocess.run([self.cli, "--help"], capture_output=True, text=True, timeout=5)
            txt = (r.stdout + r.stderr).strip().splitlines()
            return "\n".join(txt[:25])
        except Exception as e:
            return f"kunde inte köra vgm2wav: {e}"

    def probe(self, path: Path) -> list[TrackInfo]:
        if path.suffix.lower() not in (".vgm", ".vgz"):
            # S98/DRO: ingen egen headerparser ännu, längden lämnas okänd
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
                raise EngineError(f"vgm2wav gav inget ljud: {err[:300]}")
        finally:
            for p in (p2, p1):
                if p.poll() is None:
                    p.kill()
                p.wait()
