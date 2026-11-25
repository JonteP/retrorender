"""Loop- och speltidspolicy.

Inställningar slås ihop i ordningen (sist vinner):
  [defaults] -> [engine.<motor>] -> [ext.<ändelse>] -> album.toml (album, spår) -> query

loops    antal gånger loopdelen spelas (intro + loops * loop)
min_s    minsta speltid för loopande spår (fler loopar om det behövs)
max_s    högsta speltid för alla spår
default_s speltid när motorn inte vet något om längden
fade_s   fade-out i slutet när spåret kapas
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Iterator

from engines.base import TrackInfo

KEYS = ("loops", "min_s", "max_s", "default_s", "fade_s")
BUILTIN = {"loops": 2, "min_s": 30, "max_s": 600, "default_s": 180, "fade_s": 8}


def settings_for(cfg: dict, engine: str, ext: str, query: dict, layers=()) -> dict:
    """layers = extra lager mellan filändelse och query, t.ex. [album, spår] från album.toml."""
    s = dict(BUILTIN)
    for layer in (cfg.get("defaults", {}),
                  cfg.get("engine", {}).get(engine, {}),
                  cfg.get("ext", {}).get(ext.lstrip(".").lower(), {}),
                  *layers,
                  {k: v for k, v in query.items() if k in KEYS}):
        for k in KEYS:
            if k in layer and layer[k] not in (None, ""):
                s[k] = float(layer[k])
    return s


@dataclass
class Plan:
    seconds: float
    fade_s: float
    reason: str


def plan(track: TrackInfo, s: dict) -> Plan:
    mx, mn = s["max_s"], s["min_s"]
    if track.loop_s and track.loop_s > 0:
        intro = track.intro_s or 0.0
        t = intro + max(1.0, s["loops"]) * track.loop_s
        reason = f"intro {intro:.1f}s + {s['loops']:g} x loop {track.loop_s:.1f}s"
        if t < mn:
            t, reason = mn, reason + f", förlängt till min {mn:g}s"
        if t > mx:
            t, reason = mx, reason + f", kapat till max {mx:g}s"
        return Plan(t, min(s["fade_s"], t / 2), reason)
    if track.length_s and track.length_s > 0:
        if track.length_s > mx:
            return Plan(mx, min(s["fade_s"], mx / 2), f"fast längd kapad till max {mx:g}s")
        if (track.extra or {}).get("fade_at_end"):
            return Plan(track.length_s, min(s["fade_s"], track.length_s / 2),
                        "fast längd med fade på slutet")
        return Plan(track.length_s, 0.0, "fast längd")
    t = min(max(s["default_s"], 0), mx) if mx > 0 else s["default_s"]
    return Plan(t, min(s["fade_s"], t / 2), "okänd längd, standardtid")


def shape(chunks: Iterator[bytes], frames: int, fade_frames: int) -> Iterator[bytes]:
    """Exakt `frames` stereo-ramar s16le ut: kapar, fyller ut med tystnad, tonar ut."""
    total = frames * 4
    fade_start = (frames - fade_frames) * 4
    pos = 0
    rest = b""

    def fade(b: bytes, at: int) -> bytes:
        if fade_frames <= 0 or at + len(b) <= fade_start:
            return b
        a = array("h")
        a.frombytes(b)
        f0 = at // 4
        for i in range(len(a)):
            fr = f0 + i // 2
            k = fr - (frames - fade_frames)
            if k > 0:
                a[i] = int(a[i] * (1.0 - k / fade_frames))
        return a.tobytes()

    for c in chunks:
        if pos >= total:
            break
        c = rest + c
        cut = len(c) - len(c) % 4
        c, rest = c[:cut], c[cut:]
        c = c[: total - pos]
        if c:
            yield fade(c, pos)
            pos += len(c)
    while pos < total:
        n = min(65536, total - pos)
        yield bytes(n)
        pos += n
