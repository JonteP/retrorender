"""Common interface for all rendering engines."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class TrackInfo:
    """What an engine knows about a subtrack. Times in seconds, None = unknown."""
    index: int
    title: str = ""
    author: str = ""
    game: str = ""
    system: str = ""
    length_s: Optional[float] = None   # fixed length for tracks that do not loop
    intro_s: Optional[float] = None    # time before the loop point
    loop_s: Optional[float] = None     # loop length (>0 = the track loops)
    sample_rate: int = 44100           # the engine's output rate for this track
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EngineError(RuntimeError):
    pass


class Engine:
    """Base class. An engine must be able to:
    - probe(): list subtracks with metadata and loop points
    - render(): yield exactly `frames` s16le stereo frames (fewer is allowed; the
      framework pads with silence and applies the fade).
    """
    name = "base"
    extensions: tuple[str, ...] = ()

    def available(self) -> bool:
        return True

    def probe(self, path: Path) -> list[TrackInfo]:
        raise NotImplementedError

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        raise NotImplementedError
