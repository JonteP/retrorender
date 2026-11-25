"""Gemensamt gränssnitt för alla renderingsmotorer."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class TrackInfo:
    """Det en motor vet om ett delspår. Tider i sekunder, None = okänt."""
    index: int
    title: str = ""
    author: str = ""
    game: str = ""
    system: str = ""
    length_s: Optional[float] = None   # fast längd för spår som inte loopar
    intro_s: Optional[float] = None    # tid före looppunkten
    loop_s: Optional[float] = None     # looplängd (>0 = spåret loopar)
    sample_rate: int = 44100           # motorns utdatafrekvens för detta spår
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EngineError(RuntimeError):
    pass


class Engine:
    """Basklass. En motor ska kunna:
    - probe(): lista delspår med metadata och looppunkter
    - render(): ge exakt `frames` stereo-ramar s16le (motorn får ge färre;
      ramverket fyller ut med tystnad och lägger på fade).
    """
    name = "base"
    extensions: tuple[str, ...] = ()

    def available(self) -> bool:
        return True

    def probe(self, path: Path) -> list[TrackInfo]:
        raise NotImplementedError

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        raise NotImplementedError
