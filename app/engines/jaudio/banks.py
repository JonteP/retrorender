"""JAudio2 instrument banks (IBNK v1 / v0) and wave systems (WSYS) from a BAA archive,
plus AW sample decoding (ADPCM4/ADPCM2/PCM8/PCM16). Layouts follow JASBNKParser and
JASWSParser in the Twilight Princess decompilation."""
import struct
import threading
from typing import Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

AFC_COEF = np.array([(0, 0), (0x0800, 0), (0, 0x0800), (0x0400, 0x0400), (0x1000, -0x0800),
                     (0x0e00, -0x0600), (0x0c00, -0x0400), (0x1200, -0x0a00), (0x1068, -0x08c8),
                     (0x12c0, -0x08fc), (0x1400, -0x0c00), (0x0800, -0x0800), (0x0400, -0x0400),
                     (-0x0400, 0x0400), (-0x0400, 0), (-0x0800, 0)], dtype=np.int64)


def u32(d, o): return struct.unpack_from(">I", d, o)[0]
def f32(d, o): return struct.unpack_from(">f", d, o)[0]


@dataclass
class Wave:
    id: int
    fmt: int
    base_key: int
    rate: float
    start: int
    length: int
    loop: bool
    loop_start: int
    loop_end: int
    count: int
    aw: str
    _pcm: Optional[np.ndarray] = None


@dataclass
class Osc:
    target: int
    rate: float
    table: list          # [(mode, time, value)]
    rel: list
    scale: float
    vertex: float


@dataclass
class Region:
    high_key: int
    wave: int
    volume: float
    pitch: float


@dataclass
class Inst:
    volume: float = 1.0
    pitch: float = 1.0
    oscs: list = field(default_factory=list)
    regions: list = field(default_factory=list)      # key regions, ascending high_key
    perc: Optional[dict] = None                       # key -> (Region, pan, release, vol, pitch)


def read_env(d, off):
    pts = []
    for i in range(64):
        mode, t, v = struct.unpack_from(">hhh", d, off + 6 * i)
        pts.append((mode, t, v))
        if mode > 10:          # 0xb..0xf end/hold/loop markers
            break
    return pts


def parse_ibnk(d: bytes, base: int) -> Tuple[int, dict]:
    size, gid, ver = u32(d, base + 4), u32(d, base + 8), u32(d, base + 12)
    insts = {}
    if ver != 1:
        raise NotImplementedError(f"IBNK version {ver}")
    chunks = {}
    o = base + 0x20
    while o < base + size:
        cid, csz = d[o:o + 4], u32(d, o + 4)
        chunks[cid] = o
        o = (o + 0xB + csz) & ~3
    envt = chunks[b"ENVT"] + 8
    oc = chunks[b"OSCT"]
    oscs = []
    for i in range(u32(d, oc + 8)):
        p = oc + 12 + i * 0x1C
        assert d[p:p + 4] == b"Osci"
        oscs.append(Osc(d[p + 4], f32(d, p + 8),
                        read_env(d, envt + u32(d, p + 12)), read_env(d, envt + u32(d, p + 16)),
                        f32(d, p + 20), f32(d, p + 24)))
    lc = chunks[b"LIST"]
    for i in range(u32(d, lc + 8)):
        off = u32(d, lc + 12 + 4 * i)
        if not off:
            continue
        p = base + off
        kind = d[p:p + 4]; p += 4
        if kind == b"Inst":
            ins = Inst()
            n = u32(d, p); p += 4
            ins.oscs = [oscs[u32(d, p + 4 * j)] for j in range(n)]; p += 4 * n
            n = u32(d, p); p += 4 + 4 * n            # effects (skipped like the game)
            n = u32(d, p); p += 4
            for _ in range(n):
                hk = d[p]; nv = u32(d, p + 4)
                ins.regions.append(Region(hk, u32(d, p + 12) & 0xFFFF, f32(d, p + 16), f32(d, p + 20)))
                p += 8 + 16 * nv
            ins.volume, ins.pitch = f32(d, p), f32(d, p + 4)
            insts[i] = ins
        elif kind == b"Perc":
            ins = Inst(perc={})
            n = u32(d, p); p += 4
            for k in range(n):
                po = u32(d, p + 4 * k)
                if not po:
                    continue
                q = base + po
                assert d[q:q + 4] == b"Pmap"
                vol, pit, pan, rel = f32(d, q + 4), f32(d, q + 8), d[q + 12], struct.unpack_from(">H", d, q + 14)[0]
                r = q + 16
                c2 = u32(d, r); r += 4 + 4 * c2
                wave = u32(d, r + 8) & 0xFFFF
                rv, rp = f32(d, r + 12), f32(d, r + 16)
                ins.perc[k] = (Region(127, wave, rv, rp), pan / 127.0, rel, vol, pit)
            insts[i] = ins
    return gid, insts


def parse_wsys(d: bytes, base: int, aw_dir: Path) -> dict:
    waves = {}
    winf = base + u32(d, base + 0x10)
    wbct = base + u32(d, base + 0x14)
    for g in range(u32(d, wbct + 8)):
        scene = base + u32(d, wbct + 12 + 4 * g)
        ctrl = base + u32(d, scene + 12)
        arch = base + u32(d, winf + 8 + 4 * g)
        name = d[arch:arch + 0x70].split(b"\0")[0].decode()
        for j in range(u32(d, ctrl + 4)):
            w = base + u32(d, arch + 0x74 + 4 * j)
            cw = base + u32(d, ctrl + 8 + 4 * j)
            wid = u32(d, cw) & 0xFFFF
            fmt, bk = d[w + 1], d[w + 2]
            rate = f32(d, w + 4)
            st, ln, lf, ls, le, cnt = struct.unpack_from(">IIIIII", d, w + 8)
            waves[wid] = Wave(wid, fmt, bk, rate, st, ln, lf != 0, ls, le, cnt, str(aw_dir / name))
    return waves


_decode_lock = threading.Lock()


def decode(w: Wave) -> np.ndarray:
    if w._pcm is not None:
        return w._pcm
    with _decode_lock:
        if w._pcm is None:
            w._pcm = _decode(w)
    return w._pcm


def _decode(w: Wave) -> np.ndarray:
    with open(w.aw, "rb") as fh:
        fh.seek(w.start)
        raw = fh.read(w.length)
    if w.fmt in (0, 1):
        fsz, bits = (9, 4) if w.fmt == 0 else (5, 2)
        nfr = len(raw) // fsz
        out = np.zeros(nfr * 16, dtype=np.int32)
        h1 = h2 = 0
        a = np.frombuffer(raw[:nfr * fsz], dtype=np.uint8).reshape(nfr, fsz)
        for f in range(nfr):
            hdr = int(a[f, 0])
            scale = 1 << (hdr >> 4)
            c1, c2 = (int(x) for x in AFC_COEF[hdr & 15])
            body = a[f, 1:]
            if bits == 4:
                nib = np.empty(16, dtype=np.int64)
                nib[0::2] = body >> 4; nib[1::2] = body & 15
                nib = np.where(nib >= 8, nib - 16, nib)
                deltas = nib * scale << 11
            else:
                nib = np.empty(16, dtype=np.int64)
                for k in range(4):
                    nib[k::4] = (body >> (6 - 2 * k)) & 3
                nib = np.where(nib >= 2, nib - 4, nib)
                deltas = (nib * scale << 13)
            for k in range(16):
                s = (int(deltas[k]) + c1 * h1 + c2 * h2) >> 11
                s = -32768 if s < -32768 else 32767 if s > 32767 else s
                out[f * 16 + k] = s
                h2, h1 = h1, s
        pcm = out[:w.count] if w.count else out
    elif w.fmt == 2:
        pcm = np.frombuffer(raw, dtype=np.int8).astype(np.int32) << 8
    elif w.fmt == 3:
        pcm = np.frombuffer(raw, dtype=">i2").astype(np.int32)
    else:
        raise NotImplementedError(f"wave format {w.fmt}")
    return pcm.astype(np.float32) / 32768.0


class BankSet:
    """All banks in a BAA: bank id (as used by the sequence) -> (insts, waves)."""

    def __init__(self, baa: Path, aw_dir: Optional[Path] = None):
        d = baa.read_bytes()
        aw_dir = aw_dir or baa.parent
        o, wsys, bnk = 4, {}, []
        SZ = {b"bst ": 12, b"bstn": 12, b"ws  ": 16, b"bnk ": 12, b"bsc ": 12, b"bfca": 8, b"bms ": 16}
        while d[o:o + 4] != b">_AA":
            tag = d[o:o + 4]
            if tag == b"ws  ":
                wid, off = struct.unpack_from(">II", d, o + 4)
                wsys[wid] = off
            elif tag == b"bnk ":
                bnk.append(struct.unpack_from(">II", d, o + 4))
            o += SZ[tag]
        self.banks = {}
        for wid, off in bnk:
            gid, insts = parse_ibnk(d, off)
            waves = parse_wsys(d, wsys[wid], aw_dir)
            self.banks[gid] = (insts, waves)

    def lookup(self, bank, prog, key, vel=127):
        """-> (Inst, Region, Wave, extra) or None."""
        b = self.banks.get(bank)
        if not b:
            return None
        insts, waves = b
        ins = insts.get(prog)
        if not ins:
            return None
        if ins.perc is not None:
            p = ins.perc.get(key)
            if not p:
                return None
            reg, pan, rel, vol, pit = p
            w = waves.get(reg.wave)
            return (ins, reg, w, {"pan": pan, "release": rel, "vol": vol, "pitch": pit}) if w else None
        for reg in ins.regions:
            if key <= reg.high_key:
                w = waves.get(reg.wave)
                return (ins, reg, w, {}) if w else None
        return None
