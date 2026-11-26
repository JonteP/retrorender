"""JAudio 1 (Wind Waker) banks: JaiInit.aaf with IBNK 'BANK' (version 0) instruments and
WSYS wave systems. Layout from zeldaret/tww JASBNKParser / JAIInitData / JAIBankWave."""
import struct
from pathlib import Path
from typing import Optional

from .banks import Inst, Osc, Region, read_env, parse_wsys, u32, f32


def _osc(d, base, off):
    p = base + off
    tgt, rate = d[p], f32(d, p + 4)
    t, r = u32(d, p + 8), u32(d, p + 12)
    return Osc(tgt, rate, read_env(d, base + t) if t else [], read_env(d, base + r) if r else [],
               f32(d, p + 16), f32(d, p + 20))


def _vmaps(d, base, count_off):
    n = u32(d, count_off)
    out = []
    for k in range(n):
        v = base + u32(d, count_off + 4 + 4 * k)
        out.append((d[v], Region(127, u32(d, v + 4) & 0xFFFF, f32(d, v + 8), f32(d, v + 12))))
    return out


def parse_bank0(d: bytes, base: int):
    assert d[base:base + 4] == b"IBNK" and d[base + 0x20:base + 0x24] == b"BANK"
    vir = u32(d, base + 8)
    insts = {}
    for i in range(0x80):
        off = u32(d, base + 0x24 + 4 * i)
        if not off:
            continue
        p = base + off
        ins = Inst(volume=f32(d, p + 8), pitch=f32(d, p + 12))
        for j in range(2):
            o = u32(d, p + 0x10 + 4 * j)
            if o:
                ins.oscs.append(_osc(d, base, o))
        ins.sense = []
        for j in range(2):
            o = u32(d, p + 0x20 + 4 * j)
            if o:
                q = base + o
                ins.sense.append((d[q], d[q + 1], d[q + 2], f32(d, q + 4), f32(d, q + 8)))
        for j in range(u32(d, p + 0x28)):
            km = base + u32(d, p + 0x2C + 4 * j)
            vr = _vmaps(d, base, km + 4)
            reg = vr[0][1] if vr else None
            if reg is None:
                continue
            reg.high_key = d[km]
            reg.velo = vr
            ins.regions.append(reg)
        insts[i] = ins
    for i in range(12):
        off = u32(d, base + 0x3B4 + 4 * i)
        if not off:
            continue
        p = base + off
        per2 = d[p:p + 4] == b"PER2"
        ins = Inst(perc={})
        for k in range(0x80):
            po = u32(d, p + 0x88 + 4 * k)
            if not po:
                continue
            q = base + po
            vr = _vmaps(d, base, q + 0x10)
            if not vr:
                continue
            pan = (struct.unpack_from(">b", d, p + 0x288 + k)[0] / 127.0) if per2 else 0.5
            rel = struct.unpack_from(">H", d, p + 0x308 + 2 * k)[0] if per2 else 0
            reg = vr[0][1]
            reg.velo = vr
            ins.perc[k] = (reg, pan, rel, f32(d, q), f32(d, q + 4))
        insts[0xE4 + i] = ins
    return vir, insts


class BankSet1:
    """JaiInit.aaf: bank virtual id (as used by sequences) -> (insts, waves)."""

    def __init__(self, aaf: Path, aw_dir: Optional[Path] = None):
        d = aaf.read_bytes()
        aw_dir = aw_dir or aaf.parent / "Banks"
        o, bnk, ws = 0, [], []
        while True:
            t = u32(d, o); o += 4
            if t == 0:
                break
            if t in (2, 3):
                lst = bnk if t == 2 else ws
                while u32(d, o):
                    lst.append((u32(d, o), u32(d, o + 4), u32(d, o + 8))); o += 12
                o += 4
            elif t == 1 or t >= 4:
                o += 12 if t != 4 else 12
        wsys = [parse_wsys(d, off, aw_dir) for off, _, _ in ws]
        self.banks = {}
        for off, _, wsi in bnk:
            vir, insts = parse_bank0(d, off)
            self.banks[vir] = (insts, wsys[wsi] if wsi < len(wsys) else {})

    def lookup(self, bank, prog, key, vel=127):
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
            reg = next((r for hv, r in reg.velo if vel <= hv), reg.velo[-1][1])
            w = waves.get(reg.wave)
            return (ins, reg, w, {"pan": pan, "release": rel, "vol": vol, "pitch": pit}) if w else None
        for reg in ins.regions:
            if key <= reg.high_key:
                r = next((r for hv, r in reg.velo if vel <= hv), reg.velo[-1][1])
                w = waves.get(r.wave)
                return (ins, r, w, {}) if w else None
        return None
