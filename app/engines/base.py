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


# How much an engine can see of the music while it plays. This is what visualisation and
# per-voice controls can build on later; the order is from most to least insight.
INSIGHT = {
    "sequence": "notes, instruments and channel state from our own sequencer",
    "pattern": "tracker patterns, rows and channels",
    "voices": "separate sound-chip voices that can be muted or rendered one by one",
    "registers": "sound-chip register writes (notes have to be decoded per chip)",
    "audio": "finished audio only",
}


class Engine:
    """Base class. An engine must be able to:
    - probe(): list subtracks with metadata and loop points
    - render(): yield exactly `frames` s16le stereo frames (fewer is allowed; the
      framework pads with silence and applies the fade).
    It can describe itself with describe(): see INSIGHT, voices() and `controls`.
    """
    name = "base"
    extensions: tuple[str, ...] = ()
    insight = "audio"
    # settings the engine takes for a whole render, e.g.
    # {"key": "fx_send", "label": "Reverb", "min": 0, "max": 1, "step": 0.05, "default": 0.35}
    controls: tuple[dict, ...] = ()

    def voices(self, path: Path, track: Optional["TrackInfo"] = None) -> list[str]:
        """Names of the voices/channels in this file, if the engine knows them."""
        return []

    def describe(self, path: Optional[Path] = None, track: Optional["TrackInfo"] = None) -> dict:
        d = {"engine": self.name, "insight": self.insight, "insight_text": INSIGHT[self.insight],
             "controls": [dict(c) for c in self.controls]}
        if path is not None:
            try:
                d["voices"] = self.voices(path, track)
            except Exception as e:      # noqa: BLE001 - a description must never fail
                d["voices"], d["error"] = [], str(e)
        return d

    def available(self) -> bool:
        return True

    def probe(self, path: Path) -> list[TrackInfo]:
        raise NotImplementedError

    def render(self, path: Path, track: TrackInfo, frames: int) -> Iterator[bytes]:
        raise NotImplementedError
