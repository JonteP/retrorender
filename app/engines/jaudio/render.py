"""Streaming JAudio2 mixer.

Follows JASChannel / JASOscillator / JASDsp from the Twilight Princess and Super Mario
Galaxy decompilations:
- voices are updated once per DSP subframe (80 samples at 32 kHz = 2.5 ms),
- envelopes count 600 units per second (48000 / 32000 * 400 subframes),
- volume = (velocity/127)^2 * instrument * key region * track volume^2 * envelope,
- sine pan law, pitch = 2^((key - base key)/12 + bend) * instrument pitch,
- the FX send goes through Galaxy's FX line (AudEffector::mFxLine): a 64-subframe
  circular buffer with an 8-tap filter whose output is fed back and returned at 0x7000.
How the DSP microcode feeds the FX buffer back is not in the decompilation; the feedback
loop here is our best reading of it.
"""
from __future__ import annotations

import math
from typing import Iterator, Optional

import numpy as np

from .banks import BankSet, decode
from .seq import Sequence

GAME_RATE = 32000.0
SUBFRAMES_PER_S = 400.0               # 80 samples at 32 kHz
ENV_UNITS_PER_S = 600.0               # 48000 / 32000 per subframe
FX_SECONDS = 64 * 80 / GAME_RATE      # 160 ms
FX_TAPS = np.array([0, 0x1000, 0, 0, 0, 0, 0x3FFF, 0], np.float32) / 32768.0
FX_RETURN = 0x7000 / 32768.0
MASTER = 0.35                         # fixed gain so we can stream without a second pass

CURVES = [
    [1.0, 0.9375, 0.875, 0.8125, 0.75, 0.6875, 0.625, 0.5625, 0.5, 0.4375, 0.375, 0.3125, 0.25, 0.1875, 0.125, 0.0625, 0.0],
    [1.0, 0.968246, 0.935414, 0.901388, 0.866025, 0.829156, 0.790569, 0.75, 0.707107, 0.661438, 0.612372, 0.559017, 0.5, 0.433013, 0.353553, 0.25, 0.0],
    [1.0, 0.878906, 0.765625, 0.660156, 0.5625, 0.472656, 0.390625, 0.316406, 0.25, 0.191406, 0.140625, 0.097656, 0.0625, 0.035156, 0.015625, 0.003906, 0.0],
    [1.0, 0.970489, 0.781274, 0.546281, 0.399792, 0.289315, 0.212104, 0.157476, 0.112613, 0.08179, 0.057985, 0.043642, 0.030824, 0.023713, 0.015259, 0.009156, 0.0],
]


class Env:
    """JASOscillator for the volume target. States: 0 stop, 1 attack table, 2 hold,
    3 release table, 4 direct release."""

    def __init__(self, osc, direct_release=0, units_per_update=1.5):
        self.osc, self.direct, self.inc_by = osc, direct_release, units_per_update
        self.t = self.val = self.start = self.target = 0.0
        self.point = self.mode = 0
        if osc is None or not osc.table:
            self.state, self.val, self.rate = 2, 1.0, 1.0
            return
        self.rate = osc.rate
        self.target = osc.table[0][2] / 32768.0
        self.mode = osc.table[0][0] & 3
        self.state = 1

    def value(self):
        return self.val if self.osc is None else self.val * self.osc.scale + self.osc.vertex

    def _curve(self, length):
        tbl = CURVES[self.mode]
        f = 16.0 * (self.t / length) if length else 16.0
        i = min(int(f), 15)
        fr = f - i
        c = (1 - fr) * tbl[i] + fr * tbl[i + 1]
        self.val = self.start * c + self.target * (1 - c)

    def update(self):
        if self.state == 4:
            x = self.direct & 0x3FFF
            if self.t < x:
                self._curve(x)
            else:
                self.val, self.state = self.target, 0
            return
        if self.state in (0, 2):
            return
        tbl = self.osc.table if self.state == 1 else self.osc.rel
        if not tbl:
            self.state = 2
            return
        while self.point < len(tbl) and self.t >= tbl[self.point][1]:
            self.t -= tbl[self.point][1]
            self.val = self.target
            self.point += 1
            self.start = self.val
            if self.point >= len(tbl):
                self.state = 0
                return
            mode, _, v = tbl[self.point]
            if mode == 0xF:
                self.state = 0
                return
            if mode == 0xE:
                self.state = 2
                return
            if mode == 0xD:
                self.point = v
                continue
            self.mode = mode & 3
            self.target = v / 32768.0
        if self.point < len(tbl):
            self._curve(tbl[self.point][1])

    def inc(self):
        if self.state in (1, 3, 4):
            self.t += self.inc_by * (self.rate if self.state != 4 else 1.0)
            self.update()

    def release(self):
        if self.osc is None and not self.direct:
            self.state = 0
            return
        if self.direct:
            self.t, self.start, self.target, self.point = 0.0, self.val, 0.0, 0
            self.mode, self.state = (self.direct >> 14) & 3, 4
            self.update()
            return
        if self.osc.rel and self.osc.rel is not self.osc.table:
            self.t, self.start, self.point = 0.0, self.val, 0
            self.target = self.osc.rel[0][2] / 32768.0
            self.mode = self.osc.rel[0][0] & 3
        self.state = 3
        self.update()

    @property
    def stopped(self):
        return self.state == 0


class Voice:
    def __init__(self, hit, key, vel, track, release_at, rate, env_inc):
        ins, reg, w, extra = hit
        self.pcm = decode(w)
        self.w, self.rate = w, rate
        drum = ins.perc is not None
        self.key = 0.0 if drum else key - w.base_key
        self.sweep = self.sweep_target = 0.0
        self.sweep_left = 0
        if drum:
            self.vol = extra["vol"] * reg.volume
            self.pitch = extra["pitch"] * reg.pitch
            self.pan_off = extra["pan"] - 0.5
            self.env = Env(None, extra["release"], env_inc)
        else:
            self.vol = ins.volume * reg.volume
            self.pitch = ins.pitch * reg.pitch
            self.pan_off = 0.0
            self.env = Env(next((o for o in ins.oscs if o.target == 0), None), 0, env_inc)
        self.vel, self.track = vel, track
        self.pos = 0.0
        self.release_at = release_at
        self.released = False

    def release(self):
        if not self.released:
            self.released = True
            self.env.release()

    def render(self, n, state, fx):
        vol, pan, bend = state
        self.env.inc()
        if self.env.stopped:
            return None
        if self.sweep_left:
            self.sweep += (self.sweep_target - self.sweep) / self.sweep_left
            self.sweep_left -= 1
        w, pcm = self.w, self.pcm
        step = (2.0 ** (bend + (self.key + self.sweep) / 12.0)) * self.pitch * (w.rate / self.rate)
        g = (self.vel / 127.0) ** 2 * self.vol * vol * max(self.env.value(), 0.0)
        idx = self.pos + step * np.arange(n)
        looped = w.loop and w.loop_end > w.loop_start
        if looped:
            ln = w.loop_end - w.loop_start
            idx = np.where(idx >= w.loop_end, w.loop_start + np.mod(idx - w.loop_start, ln), idx)
        elif idx[0] >= len(pcm) - 1:
            return None
        i0 = np.minimum(idx.astype(np.int64), len(pcm) - 1)
        i1 = np.minimum(i0 + 1, len(pcm) - 1)
        if looped:
            i1 = np.where(i1 >= w.loop_end, w.loop_start, i1)
        fr = (idx - i0).astype(np.float32)
        out = pcm[i0] * (1 - fr) + pcm[i1] * fr
        if not looped:
            out[idx >= len(pcm) - 1] = 0
        self.pos += step * n
        if looped and self.pos >= w.loop_end:
            self.pos = w.loop_start + (self.pos - w.loop_start) % (w.loop_end - w.loop_start)
        p = min(max(pan + self.pan_off, 0.0), 1.0)
        gl, gr = g * math.sin((1 - p) * math.pi / 2), g * math.sin(p * math.pi / 2)
        return out * gl, out * gr, out * (gl * fx), out * (gr * fx)


class FxLine:
    """Streaming version of the FX line: y[n] = sum_k h[k] * d[n - D + k], d = x + y."""

    def __init__(self, delay):
        self.D = delay
        self.hist = np.zeros(delay + len(FX_TAPS), np.float32)   # last D+8 samples of d

    def process(self, x):
        n = len(x)
        out = np.empty(n, np.float32)
        a = 0
        while a < n:
            b = min(n, a + self.D - len(FX_TAPS))
            m = b - a
            acc = np.zeros(m, np.float32)
            T = len(FX_TAPS)
            # hist[i] holds d[n - (D + T) + i]; d[n + j - D + k] is hist[j + k + T]
            for k, h in enumerate(FX_TAPS):
                if h:
                    acc += h * self.hist[T + k: T + k + m]
            out[a:b] = acc
            self.hist = np.concatenate([self.hist[m:], x[a:b] + acc])
            a = b
        return out


def sync_tracks(events):
    """Beat/bar marker tracks play only keys 24-31 and step bank/program as a bar
    counter; the game keeps them silent."""
    progs, keys = {}, {}
    for e in events:
        if e.kind == "prog":
            progs[e.track] = progs.get(e.track, 0) + 1
        elif e.kind in ("on", "gate"):
            keys.setdefault(e.track, set()).add(e.a[1] if e.kind == "on" else e.a[0])
    counts = {}
    for e in events:
        if e.kind in ("on", "gate"):
            counts[e.track] = counts.get(e.track, 0) + 1
    return {t for t, k in keys.items()
            if max(k) <= 31 and (progs.get(t, 0) >= 3 or counts.get(t, 0) >= 32)}


def analyse(data: bytes, max_seconds=900.0):
    """-> (loop (start, end) or None, end time, has audible notes)."""
    seq = Sequence(data, max_seconds=max_seconds, loops=2)
    seq.run()
    mute = sync_tracks(seq.events)
    audible = any(e.kind in ("on", "gate") and e.track not in mute for e in seq.events)
    return seq.loop, seq.time, audible


def stream(data: bytes, bankset: BankSet, rate: int, seconds: float, fx_send=0.35,
           chunk_s=0.5) -> Iterator[np.ndarray]:
    """Yield float32 stereo chunks (n, 2) covering `seconds` of audio."""
    seq = Sequence(data, max_seconds=seconds + 1.0, loops=10 ** 9)
    seq.run()
    events = sorted(seq.events, key=lambda e: e.tick)
    mute = sync_tracks(events)
    sub = max(1, round(rate / SUBFRAMES_PER_S))
    env_inc = ENV_UNITS_PER_S * sub / rate
    fxl, fxr = FxLine(round(FX_SECONDS * rate)), FxLine(round(FX_SECONDS * rate))
    total = int(seconds * rate)
    state, prog, slots, voices = {}, {}, {}, []
    ei, pos = 0, 0
    per_chunk = max(1, int(chunk_s * rate) // sub)
    while pos < total:
        nsub = min(per_chunk, (total - pos + sub - 1) // sub)
        buf = np.zeros((4, nsub * sub), np.float32)
        for s in range(nsub):
            t = (pos + s * sub) / rate
            while ei < len(events) and events[ei].time <= t:
                e = events[ei]
                ei += 1
                if e.kind == "state":
                    state[e.track] = e.a
                    continue
                if e.kind == "prog":
                    prog[e.track] = e.a
                    continue
                if e.track in mute:
                    continue
                bank, prg = prog.get(e.track, (0, 0xF0))
                if e.kind in ("gate", "on"):
                    nid = 0 if e.kind == "gate" else e.a[0]
                    key, vel = (e.a[0], e.a[1]) if e.kind == "gate" else (e.a[1], e.a[2])
                    old = slots.pop((e.track, nid), None)
                    if old:
                        old.release()
                    hit = bankset.lookup(bank, prg, key)
                    if not hit:
                        continue
                    rel = e.time + e.a[2] if e.kind == "gate" and e.a[2] is not None else None
                    v = Voice(hit, key, vel, e.track, rel, rate, env_inc)
                    if e.kind == "gate" and e.a[3] is not None:          # portamento
                        v.sweep_target = e.a[3] - key
                        v.sweep_left = max(1, int(e.a[4] * SUBFRAMES_PER_S))
                    voices.append(v)
                    slots[(e.track, nid)] = v
                elif e.kind == "tie":                                     # legato: retune
                    v = slots.get((e.track, 0))
                    if v:
                        v.key, v.vel = e.a[0] - v.w.base_key, e.a[1]
                        v.release_at = e.time + e.a[2] if e.a[2] is not None else None
                        if e.a[3] is not None:
                            v.sweep, v.sweep_target = 0.0, e.a[3] - e.a[0]
                            v.sweep_left = max(1, int(e.a[4] * SUBFRAMES_PER_S))
                elif e.kind == "off":
                    v = slots.pop((e.track, e.a[0]), None)
                    if v:
                        v.release()
            a, b = s * sub, s * sub + sub
            alive = []
            for v in voices:
                if v.release_at is not None and not v.released and t >= v.release_at:
                    v.release()
                res = v.render(sub, state.get(v.track, (1.0, 0.5, 0.0)), fx_send)
                if res is None:
                    for k in [k for k, x in slots.items() if x is v]:
                        del slots[k]
                    continue
                buf[0, a:b] += res[0]
                buf[1, a:b] += res[1]
                buf[2, a:b] += res[2]
                buf[3, a:b] += res[3]
                alive.append(v)
            voices = alive
        L, R = buf[0], buf[1]
        if fx_send:
            L = L + FX_RETURN * fxl.process(buf[2])
            R = R + FX_RETURN * fxr.process(buf[3])
        out = np.stack([L, R], 1) * MASTER
        out = np.where(np.abs(out) > 0.9, np.sign(out) * (0.9 + 0.1 * np.tanh((np.abs(out) - 0.9) / 0.1)), out)
        n = min(len(out), total - pos)
        yield out[:n]
        pos += n
