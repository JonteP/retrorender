"""JAudio2 sequence (BMS) interpreter, ported from the Twilight Princess decompilation
(JASSeqParser / JASSeqReader / JASSeqCtrl / JASTrack). Produces a timeline of note and
parameter events instead of driving a DSP."""
import struct
from dataclasses import dataclass

STACK = 8

# opcode table (0xa0..0xff) and argument type bits, as in JASSeqParser::sCmdInfo
_CMDS = {
    0xb1: ("noteon", 3, 0x0000), 0xb2: ("noteoff", 1, 0x0000), 0xb3: ("note", 4, 0x0040),
    0xb4: ("setlastnote", 1, 0), 0xb8: ("paramE", 2, 0), 0xb9: ("paramI", 2, 0x4),
    0xba: ("paramEI", 3, 0x10), 0xbb: ("paramII", 3, 0x14),
    0xc1: ("opentrack", 2, 0x8), 0xc2: ("closetrack", 1, 0), 0xc3: ("call", 1, 0x2),
    0xc4: ("callf", 2, 0x8), 0xc5: ("ret", 0, 0), 0xc6: ("retf", 1, 0), 0xc7: ("jmp", 1, 0x2),
    0xc8: ("jmpf", 2, 0x8), 0xc9: ("jmptable", 2, 0xb), 0xca: ("calltable", 2, 0xb),
    0xcb: ("loops", 1, 0x1), 0xcc: ("loope", 0, 0),
    0xd0: ("readport", 2, 0), 0xd1: ("writeport", 2, 0xc), 0xd2: ("checkportimport", 1, 0),
    0xd3: ("checkportexport", 1, 0), 0xd4: ("parentwriteport", 2, 0xc),
    0xd5: ("childwriteport", 2, 0xc), 0xd6: ("parentreadport", 2, 0), 0xd7: ("childreadport", 2, 0),
    0xd8: ("regload", 2, 0x4), 0xd9: ("reg", 3, 0x30), 0xda: ("reg", 3, 0x10), 0xdb: ("reguni", 2, 0),
    0xdc: ("regtblload", 4, 0xe0),
    0xe0: ("tempo", 1, 0x1), 0xe1: ("bankprg", 1, 0x1), 0xe2: ("bank", 1, 0), 0xe3: ("prg", 1, 0),
    0xe7: ("envscaleset", 2, 0x4), 0xe8: ("envset", 2, 0x8), 0xe9: ("simpleadsr", 5, 0x155),
    0xea: ("busconnect", 2, 0x4), 0xeb: ("iircutoff", 1, 0), 0xec: ("iirset", 4, 0x55),
    0xed: ("firset", 1, 0x2), 0xf0: ("wait", 0, 0), 0xf1: ("waitbyte", 1, 0),
    0xf3: ("setinttable", 1, 0x2), 0xf4: ("setinterrupt", 1, 0x1), 0xf5: ("disinterrupt", 1, 0x1),
    0xf6: ("reti", 0, 0), 0xf7: ("clri", 0, 0), 0xf8: ("inttimer", 2, 0x4), 0xf9: ("synccpu", 1, 0x1),
    0xfd: ("printf", 0, 0), 0xfe: ("nop", 0, 0), 0xff: ("finish", 0, 0),
}
# 0xb0 extended table: only 0x01 = dump (no args); everything else is a no-op with no args


@dataclass
class Event:
    tick: int
    time: float
    track: str
    kind: str            # on, off, gate, param, prog, tempo
    a: tuple = ()


class Track:
    def __init__(self, seq, name, parent=None):
        self.seq, self.name, self.parent = seq, name, parent
        self.base_pos = 0
        self.pos = 0
        self.ret = [0] * STACK
        self.loopc = [0] * STACK
        self.depth = 0
        self.wait = 0
        self.wait_note = False
        self.intr_save = None      # field_0x44
        self.intr_table = 0
        self.intr_mask = 0
        self.intr_pend = 0
        self.timer_count = self.timer_left = self.timer_reload = 0
        self.regs = [0] * 14
        self.ports = [0] * 16
        self.port_in = self.port_out = 0
        self.children = [None] * 16
        self.running = True
        self.transpose = 0
        self.latest_key = 60
        self.bank, self.prog = 0, 0xF0
        self.bend_sense = 12
        self.gate_rate = 100
        self.tie = False
        self.params = [1.0, 0.0, 0.0, 0.5, 0.0, 0.0]   # volume pitch fxmix pan dolby distfilter
        self.ptarget = list(self.params)
        self.pcount = [0] * 6
        self.notes = {}           # noteid -> end tick (None = until note off)
        self.osc_adsr = None
        self.pan_power = 1.0
        self.vib = {}
        if parent:
            self.bank, self.prog = parent.bank, parent.prog

    # --- reader ---
    @property
    def d(self):
        return self.seq.data

    def u8(self):
        v = self.d[self.pos]; self.pos += 1; return v

    def u16(self):
        v = struct.unpack_from(">H", self.d, self.pos)[0]; self.pos += 2; return v

    def u24(self):
        v = int.from_bytes(self.d[self.pos:self.pos + 3], "big"); self.pos += 3; return v

    def get24(self, off):
        return int.from_bytes(self.d[off:off + 3], "big")

    def midi(self):
        b = self.u8()
        if not b & 0x80:
            return b
        b &= 0x7F
        for _ in range(3):
            n = self.u8()
            b = (b << 7) | (n & 0x7F)
            if not n & 0x80:
                return b
        return 0

    # --- registers ---
    def rreg(self, r):
        if r < 0x40:
            if r < 14:
                return self.regs[r]
            pairs = {32: (0, 1, 8), 33: (4, 5, 16), 38: (6, 7, 16), 39: (8, 9, 16),
                     40: (10, 11, 16), 41: (12, 13, 16)}
            if r in pairs:
                a, b, sh = pairs[r]
                return (self.regs[a] << sh) | self.regs[b]
            return 0
        if 0x40 <= r < 0x50:
            return self.ports[r - 0x40]
        if r == 0x60:
            return sum(1 << i for i, c in enumerate(self.children) if c and c.running)
        if r == 0x61:
            return self.loopc[self.depth - 1] if self.depth else 0
        return {0x62: self.seq.timebase, 0x63: self.transpose, 0x64: self.bend_sense,
                0x65: self.gate_rate, 0x67: self.bank, 0x68: self.prog}.get(r, 0)

    def wreg(self, r, v):
        self.regs[3] = v & 0xFFFF
        if r < 0x40:
            if r < 14:
                self.regs[r] = (v & 0xFF) if r <= 2 else (v & 0xFFFF)
            elif r == 32:
                self.regs[0], self.regs[1] = (v >> 8) & 0xFF, v & 0xFF
            elif r == 33:
                self.regs[4], self.regs[5] = (v >> 16) & 0xFFFF, v & 0xFFFF
            elif r in (38, 39, 40, 41):
                i = 6 + 2 * (r - 38)
                self.regs[i], self.regs[i + 1] = (v >> 16) & 0xFFFF, v & 0xFFFF
            return
        if 0x40 <= r < 0x50:
            self.ports[r - 0x40] = v & 0xFFFF
        elif r == 0x62:
            self.seq.set_timebase(v)
        elif r == 0x63:
            self.transpose = v - 0x10000 if v & 0x8000 else v
            if self.transpose > 127:
                self.transpose = (v & 0xFF) - 256 if v & 0x80 else v & 0xFF
        elif r == 0x64:
            self.bend_sense = v
        elif r == 0x65:
            self.gate_rate = v
        elif r == 0x67:
            self.bank = v & 0xFF; self.emit_prog()
        elif r == 0x68:
            self.prog = v & 0xFF; self.emit_prog()
        elif r == 0x69:
            self.pan_power = v / 32767.0
        elif r in (0x6e, 0x6f, 0x70, 0x71, 0x72, 0x73, 0x74):
            self.vib[r] = v

    def total_transpose(self):
        t, p = self.transpose, self.parent
        while p:
            t += p.transpose; p = p.parent
        return t

    def emit(self, kind, *a):
        self.seq.events.append(Event(self.seq.tick, self.seq.time, self.name, kind, a))

    def emit_prog(self):
        self.emit("prog", self.bank, self.prog)

    def cond(self, c):
        r = self.regs[3]
        return [True, r == 0, r != 0, r == 1, r >= 0x8000, r < 0x8000][c] if c < 6 else False

    # --- notes ---
    def gate_on(self, key, vel, dur, flags):
        key += self.total_transpose()
        if self.gate_rate != 100:
            dur = dur * self.gate_rate / 100.0
        start_key = self.latest_key if flags & 1 else key
        secs = dur * 60.0 / (self.seq.tempo * self.seq.timebase)
        held = bool(flags & 6) or dur == 0
        self.emit("tie" if self.tie else "gate", start_key, vel, None if held else secs,
                  key if flags & 1 else None, secs)
        self.tie = bool(flags & 2)
        self.latest_key = key
        self.notes[0] = None if (flags & 6) else self.seq.tick + int(dur)

    def note_on(self, nid, key, vel):
        key += self.total_transpose()
        self.emit("on", nid, key, vel)
        self.notes[nid] = None

    def note_off(self, nid, rel=0):
        self.emit("off", nid, rel)
        self.notes.pop(nid, None)

    def note_stopped(self, nid):
        end = self.notes.get(nid, -1)
        if end is None:          # held (tie / no duration); a real DSP would decide
            return self.seq.assume_stop(self, nid)
        return end == -1 or self.seq.tick >= end

    # --- parse ---
    def parse(self):
        op = self.u8()
        if not op & 0x80:
            b1 = self.u8(); vel = self.u8()
            nid = b1 & 7
            if nid == 0:
                dur = self.midi()
                self.gate_on(op, vel, dur, b1)
                if dur:
                    self.wait = dur
                else:
                    self.wait_note = True
            else:
                self.note_on(nid, op, vel)
            return 0
        hi = op & 0xF0
        if hi == 0x80:
            if op & 7:
                self.note_off(op & 7)
            return 0
        if hi == 0x90:
            n = (op & 7) + 1
            mask = self.u8(); over = 0; bits = 3
            for _ in range(n):
                if mask & 0x80:
                    over |= bits
                mask = (mask << 1) & 0xFF; bits <<= 2
            return self.command(self.u8(), over)
        return self.command(op, 0)

    def command(self, op, over):
        if op == 0xB0:
            self.u8()
            return 0
        name, n, types = _CMDS.get(op, (None, 0, 0))
        types |= over
        args = []
        for _ in range(n):
            t = types & 3; types >>= 2
            args.append([self.u8, self.u16, self.u24, lambda: self.rreg(self.u8())][t]())
        if name is None:
            self.seq.unknown.add(op)
            return 0
        return getattr(self, "c_" + name)(*args) or 0

    # handlers
    def c_noteon(self, a0, a1, a2): self.note_on(a1, a0, a2)
    def c_noteoff(self, a0): self.note_off(a0)

    def c_note(self, flags, key, vel, dur):
        self.gate_on(key, vel, dur, flags)
        if dur:
            self.wait = dur
        else:
            self.wait_note = True

    def c_setlastnote(self, k):
        if k < 256:
            self.latest_key = k + self.total_transpose()

    def _param(self, pid, val, count):
        if pid >= 6:
            return
        self.ptarget[pid] = val
        if count == 0:
            self.params[pid] = val
        self.pcount[pid] = count
        self.emit("param", pid, val, count)

    @staticmethod
    def _e(v):
        r = ((v << 8) & 0xFFFF)
        if not v & 0x80:
            r |= (v << 1) & 0xFFFF
        return (r - 0x10000 if r & 0x8000 else r) / 32767.0

    @staticmethod
    def _i(v):
        v &= 0xFFFF
        return (v - 0x10000 if v & 0x8000 else v) / 32767.0

    def c_paramE(self, p, v): self._param(p, self._e(v), 0)
    def c_paramI(self, p, v): self._param(p, self._i(v), 0)
    def c_paramEI(self, p, v, t): self._param(p, self._e(v), t)
    def c_paramII(self, p, v, t): self._param(p, self._i(v), t)

    def c_opentrack(self, no, off):
        old = self.children[no]
        c = Track(self.seq, f"{self.name}.{no}", self)
        c.base_pos = c.pos = off
        self.children[no] = c
        self.seq.tracks_opened += 1

    def c_closetrack(self, no):
        c = self.children[no]
        if c:
            c.close()
        self.children[no] = None

    def close(self):
        for i, c in enumerate(self.children):
            if c:
                c.close()
        for nid in list(self.notes):
            self.note_off(nid)
        self.running = False

    def call(self, off):
        if self.depth < STACK:
            self.ret[self.depth] = self.pos; self.depth += 1; self.pos = off

    def jump(self, off, cond=0):
        self.seq.on_jump(self, self.pos, off, cond)
        self.pos = off

    def c_call(self, off): self.call(off)

    def c_callf(self, c, off):
        if self.cond(c):
            self.call(off)

    def c_ret(self):
        if self.depth:
            self.depth -= 1; self.pos = self.ret[self.depth]

    def c_retf(self, c):
        if self.cond(c):
            self.c_ret()

    def c_jmp(self, off): self.jump(off)

    def c_jmpf(self, c, off):
        if self.cond(c):
            self.jump(off, c)

    def c_jmptable(self, i, tbl): self.jump(self.get24(tbl + 3 * i), -1)
    def c_calltable(self, i, tbl): self.call(self.get24(tbl + 3 * i))

    def c_loops(self, n):
        if self.depth < STACK:
            self.ret[self.depth] = self.pos; self.loopc[self.depth] = n; self.depth += 1

    def c_loope(self):
        if not self.depth:
            return
        t = self.loopc[self.depth - 1]
        if t:
            t -= 1
        if not t:
            self.depth -= 1
            return
        self.loopc[self.depth - 1] = t
        self.pos = self.ret[self.depth - 1]

    def c_readport(self, p, r):
        self.port_in &= ~(1 << p); self.wreg(r, self.ports[p])
    def c_writeport(self, p, v):
        self.port_out |= 1 << p; self.ports[p] = v & 0xFFFF
    def c_checkportimport(self, p): self.regs[3] = 1 if self.port_in & (1 << p) else 0
    def c_checkportexport(self, p): self.regs[3] = 1 if self.port_out & (1 << p) else 0

    def write_port(self, p, v):
        self.port_in |= 1 << p; self.ports[p] = v & 0xFFFF
        if p in (0, 1):
            self.interrupt(2 if p == 0 else 3)

    def c_parentwriteport(self, p, v):
        if self.parent:
            self.parent.write_port(p, v)

    def c_childwriteport(self, pv, v):
        c = self.children[pv >> 4]
        if c:
            c.write_port(pv & 15, v)

    def c_parentreadport(self, p, r):
        if self.parent:
            self.parent.port_out &= ~(1 << p); self.wreg(r, self.parent.ports[p])

    def c_childreadport(self, pv, r):
        c = self.children[pv >> 4]
        if c:
            c.port_out &= ~(1 << (pv & 15)); self.wreg(r, c.ports[pv & 15])

    def c_regload(self, r, v): self.wreg(r, v)

    def c_reg(self, kind, r, v):
        rd = self.rreg
        if kind == 0: pass
        elif kind == 1: v += rd(r)
        elif kind == 2: v = rd(r) - v
        elif kind == 3: v = rd(r) - v; r = 3
        elif kind == 4: v *= rd(r); r = 0x21
        elif kind == 5: v &= rd(r)
        elif kind == 6: v |= rd(r)
        elif kind == 7: v ^= rd(r)
        elif kind == 8: v = self.seq.rand() % v if v else 0
        elif kind == 9: v = rd(r) << v
        elif kind == 10: v = rd(r) >> v
        else: return
        self.wreg(r, v & 0xFFFFFFFF)

    def c_reguni(self, kind, r):
        if kind == 11:
            self.wreg(r, (-self.rreg(r)) & 0xFFFFFFFF)

    def c_regtblload(self, kind, r, tbl, i):
        d = self.d
        if kind == 12: v = d[tbl + i]
        elif kind == 13: v = struct.unpack_from(">H", d, tbl + 2 * i)[0]
        elif kind == 14: v = self.get24(tbl + 3 * i)
        elif kind == 15: v = struct.unpack_from(">I", d, tbl + 4 * i)[0]
        elif kind == 16: v = struct.unpack_from(">I", d, tbl + i)[0]
        else: return
        self.wreg(r, v)

    def c_tempo(self, t):
        if self.parent is None:
            self.seq.set_tempo(t)
        self.emit("tempo", t)

    def c_bankprg(self, v): self.bank, self.prog = (v >> 8) & 0xFF, v & 0xFF; self.emit_prog()
    def c_bank(self, v): self.bank = v & 0xFF; self.emit_prog()
    def c_prg(self, v): self.prog = v & 0xFF; self.emit_prog()
    def c_envscaleset(self, o, v): self.emit("envscale", o, v / 16383.0)
    def c_envset(self, o, off): self.emit("envtable", o, off)
    def c_simpleadsr(self, a, d, s, lvl, rel): self.emit("adsr", a, d, s, lvl, rel)
    def c_busconnect(self, l, v): pass
    def c_iircutoff(self, v): self.emit("iircutoff", v)
    def c_iirset(self, *a): self.emit("iir", *a)
    def c_firset(self, off): self.emit("fir", off)
    def c_wait(self): self.wait = self.midi()
    def c_waitbyte(self, v): self.wait = v
    def c_setinttable(self, v): self.intr_table = v
    def c_setinterrupt(self, m): self.intr_mask |= m
    def c_disinterrupt(self, m): self.intr_mask &= ~m

    def c_clri(self):
        self.intr_save = None; self.check_intr()

    def c_reti(self):
        if self.intr_save is not None:
            self.pos = self.intr_save; self.intr_save = None
        self.check_intr()

    def c_inttimer(self, n, t): self.timer_count, self.timer_left, self.timer_reload = n, t, t
    def c_synccpu(self, v): self.regs[3] = 0xFFFF

    def c_printf(self):
        s = bytearray(); nargs = 0
        while len(s) < 128:
            b = self.u8()
            if b == 0:
                break
            if b == ord("\\"):
                b = self.u8()
                if b == 0:
                    break
            if b == ord("%"):
                nxt = self.u8(); s.append(b); b = nxt
                if b in b"dxsrRtT":
                    nargs += 1
            s.append(b)
        for _ in range(nargs):
            self.u8()
        self.emit("printf", s.decode("latin-1"))

    def c_nop(self): pass
    def c_finish(self): return -1

    # --- interrupts ---
    def interrupt(self, t):
        if self.intr_mask & (1 << t):
            self.intr_pend |= 1 << t

    def check_intr(self):
        if self.intr_save is not None:
            return
        m = self.intr_mask & self.intr_pend
        if not m:
            return
        i = (m & -m).bit_length() - 1
        self.intr_pend &= ~(1 << i)
        self.intr_save = self.pos
        self.pos = self.get24(self.intr_table + 3 * i)

    def timer(self):
        if not self.timer_left:
            return
        self.timer_left -= 1
        if self.timer_left:
            return
        self.interrupt(5)
        if self.timer_count:
            self.timer_count -= 1
            if self.timer_count:
                self.timer_left = self.timer_reload
        else:
            self.timer_left = self.timer_reload

    # --- tick ---
    def tick(self):
        if self.running:
            self.interrupt(6)
            self.timer()
            ok = True
            if self.wait_note:
                if self.note_stopped(0):
                    self.wait_note = False
                else:
                    ok = False
            if ok:
                if self.wait > 0:
                    self.wait -= 1
                self.check_intr()
                guard = 0
                while (self.wait == 0 or self.intr_save is not None) and not self.wait_note:
                    if self.parse() < 0:
                        self.close()
                        return -1
                    guard += 1
                    if guard > 100000:
                        raise RuntimeError(f"{self.name}: runaway at {self.pos:#x}")
            for i in range(6):
                if self.pcount[i]:
                    self.params[i] += (self.ptarget[i] - self.params[i]) / self.pcount[i]
                    self.pcount[i] -= 1
        for i, c in enumerate(self.children):
            if c and c.running:
                if c.tick() < 0:
                    self.children[i] = None
        return 0


class Sequence:
    def __init__(self, data: bytes, max_seconds=600.0, loops=2):
        self.data = data + b"\0" * 8
        self.events = []
        self.tick = 0
        self.time = 0.0
        self.tempo, self.timebase = 120, 0x30
        self.unknown = set()
        self.tracks_opened = 0
        self.max_seconds, self.loops_wanted = max_seconds, loops
        self.root = Track(self, "root")
        self.visit = {}            # (track, pos) -> first time when a jump target ran
        self.loop = None           # (start_time, end_time)
        self.loop_hits = 0
        self._rand = 0x12345
        self.held_assumed = 0

    def rand(self):
        self._rand = (self._rand * 1664525 + 1013904223) & 0xFFFFFFFF
        return self._rand >> 9

    def set_tempo(self, t): self.tempo = t or 1
    def set_timebase(self, t): self.timebase = t or 1

    def assume_stop(self, track, nid):
        self.held_assumed += 1
        return True

    def on_jump(self, tr, src, dst, cond):
        if tr is not self.root:
            return
        if dst < src:
            if self.loop is None and (tr.name, dst) in self.visit:
                self.loop = (self.visit[(tr.name, dst)], self.time)
            self.loop_hits += 1
        self.visit.setdefault((tr.name, dst), self.time)

    def snapshot(self, tr, pvol=1.0, ppan=0.5, pbend=0.0, ppow=1.0):
        v = tr.params[0] * tr.params[0] * pvol
        pan = (ppan - 0.5) + (tr.params[3] - 0.5) * tr.pan_power * ppow + 0.5
        bend = pbend + tr.params[1] * tr.bend_sense / 3.0
        st = (round(v, 5), round(pan, 4), round(bend, 5))
        if self.state.get(tr.name) != st:
            self.state[tr.name] = st
            self.events.append(Event(self.tick, self.time, tr.name, "state", st))
        for c in tr.children:
            if c and c.running:
                self.snapshot(c, v, pan, bend, ppow * tr.pan_power)

    def run(self):
        self.state = {}
        # record first time any root position is reached as a potential loop target
        r = self.root
        while self.time < self.max_seconds:
            self.visit.setdefault((r.name, r.pos), self.time)
            if r.tick() < 0 or not r.running:
                return "finished"
            self.snapshot(r)
            if self.loop and self.loop_hits >= self.loops_wanted:
                return "looped"
            self.tick += 1
            self.time += 60.0 / (self.tempo * self.timebase)
        return "timeout"
