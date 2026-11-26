"""sidplayfp as a subprocess (C64 SID). Writes WAV to a FIFO that we read in real time.

SID files carry no length. The length comes from HVSC's Songlengths.md5 if available
(path in config), otherwise the loop policy's default time applies.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator, Optional

from .base import Engine, EngineError, TrackInfo


def _sid_header(data: bytes) -> dict:
    if len(data) < 0x76 or data[:4] not in (b"PSID", b"RSID"):
        raise EngineError("sid: ogiltig header")
    songs = int.from_bytes(data[0x0E:0x10], "big")
    start = int.from_bytes(data[0x10:0x12], "big")
    txt = lambda a: data[a:a + 32].split(b"\0")[0].decode("latin-1").strip()
    return {"songs": max(1, songs), "start": start, "title": txt(0x16),
            "author": txt(0x36), "released": txt(0x56)}


class Songlengths:
    """HVSC Songlengths.md5: '<md5>=m:ss.mmm m:ss ...' per fil."""
    def __init__(self, path: Optional[str]):
        self.db: dict[str, list[float]] = {}
        if path and Path(path).is_file():
            for line in Path(path).read_text("latin-1").splitlines():
                m = re.match(r"^([0-9a-f]{32})=(.*)$", line.strip())
                if m:
                    self.db[m.group(1)] = [self._t(x) for x in m.group(2).split()]

    @staticmethod
    def _t(s: str) -> float:
        s = re.sub(r"\(.*?\)", "", s)
        mm, ss = s.split(":")
        return int(mm) * 60 + float(ss)

    def get(self, data: bytes) -> list[float]:
        return self.db.get(hashlib.md5(data).hexdigest(), [])


class Stil:
    """HVSC's STIL.txt: name and composer per subtune.
    Key = path in HVSC, e.g. '/MUSICIANS/H/Hubbard_Rob/Commando.sid'."""
    FIELDS = ("NAME", "AUTHOR", "TITLE", "ARTIST", "COMMENT")

    def __init__(self, path: Optional[str]):
        self.root: Optional[Path] = None
        self.db: dict[str, dict[int, dict]] = {}
        if not path or not Path(path).is_file():
            return
        self.root = Path(path).resolve().parent.parent      # HVSC/DOCUMENTS/STIL.txt
        cur, sub, last = None, 0, None
        for raw in Path(path).read_text("latin-1").splitlines():
            if raw.startswith("/"):
                cur, sub, last = raw.strip(), 0, None
                self.db.setdefault(cur, {})
                continue
            if cur is None or not raw.strip() or raw.startswith("#"):
                continue
            m = re.match(r"^\(#(\d+)\)\s*$", raw.strip())
            if m:
                sub, last = int(m.group(1)), None
                continue
            m = re.match(r"^\s*([A-Z]+):\s?(.*)$", raw)
            if m and m.group(1) in self.FIELDS:
                rec = self.db[cur].setdefault(sub, {})
                last = m.group(1).lower()
                rec.setdefault(last, m.group(2).strip())    # first occurrence wins
                continue
            if last and raw.startswith(" "):               # continuation line
                rec = self.db[cur].setdefault(sub, {})
                if rec.get(last) is not None and last == "comment":
                    rec[last] += " " + raw.strip()

    def lookup(self, file: Path, sub1: int) -> dict:
        if self.root is None:
            return {}
        try:
            key = "/" + str(file.resolve().relative_to(self.root))
        except ValueError:
            return {}
        recs = self.db.get(key, {})
        out = dict(recs.get(0, {}))
        out.update(recs.get(sub1, {}))
        return out


class SidplayEngine(Engine):
    name = "sidplay"
    extensions = (".sid", ".psid", ".rsid")

    def __init__(self, sample_rate: int = 44100, cli: str = "sidplayfp",
                 songlengths: Optional[str] = None, ffmpeg: str = "ffmpeg",
                 stil: Optional[str] = None):
        self.sample_rate = sample_rate
        self.cli = shutil.which(cli)
        self.ffmpeg = shutil.which(ffmpeg)
        self.lengths = Songlengths(songlengths)
        if stil is None and songlengths:
            stil = str(Path(songlengths).parent / "STIL.txt")   # ligger bredvid i HVSC
        self.stil = Stil(stil)

    def available(self) -> bool:
        return bool(self.cli and self.ffmpeg)

    def probe(self, path: Path) -> list[TrackInfo]:
        data = path.read_bytes()
        h = _sid_header(data)
        lens = self.lengths.get(data)
        out = []
        for i in range(h["songs"]):
            st = self.stil.lookup(path, i + 1)
            title = h["title"] if h["songs"] == 1 else f"{h['title']} ({i + 1})"
            if st.get("name"):
                title = st["name"]
            out.append(TrackInfo(
                index=i, title=title, author=st.get("author") or h["author"], system="C64",
                game=h["title"] if h["songs"] > 1 else "",
                length_s=lens[i] if i < len(lens) else None, sample_rate=self.sample_rate,
                extra={"released": h["released"], "date": h["released"],
                       **({"stil": st} if st else {})}))
        return out

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        secs = frames / self.sample_rate + 1
        tmp = tempfile.mkdtemp(prefix="sid")
        fifo = os.path.join(tmp, "out.wav")
        os.mkfifo(fifo)
        # Flags per sidplayfp(1): -o<n> subtune (1-based), -t<m:ss> length,
        # -w<file> write WAV, -q quiet. Check against the installed version.
        cmd = [self.cli, "-q", f"-o{track.index + 1}",
               f"-t{int(secs // 60)}:{int(secs % 60):02d}", f"-w{fifo}", str(path)]
        player = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ff = subprocess.Popen(
            [self.ffmpeg, "-v", "error", "-f", "wav", "-i", fifo, "-ac", "2",
             "-ar", str(self.sample_rate), "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            left = frames * 4
            while left > 0:
                b = ff.stdout.read(min(65536, left))
                if not b:
                    break
                yield b
                left -= len(b)
        finally:
            for p in (ff, player):
                if p.poll() is None:
                    p.kill()
                p.wait()
            shutil.rmtree(tmp, ignore_errors=True)
