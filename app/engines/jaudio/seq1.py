"""JAudio 1 (Wind Waker) sequence interpreter, ported from zeldaret/tww
(JASystem::TSeqParser / TSeqCtrl / TTrack / TIntrMgr / TNoteMgr). Emits the same event
stream as seq.py so the mixer can be shared."""
import struct

from .seq import Event

ARGS = {  # op: (name, count, types)
    0xC1: ("opentrack", 2, 8), 0xC2: ("opentrackbros", 2, 8), 0xC3: (None, 1, 2),
    0xC4: ("call", 0, 0), 0xC6: ("ret", 1, 0), 0xC7: (None, 1, 2), 0xC8: ("jmp", 0, 0),
    0xC9: ("loops", 1, 1), 0xCA: ("loope", 0, 0), 0xCB: ("readport", 2, 0),
    0xCC: ("writeport", 2, 12), 0xCD: ("checkportimport", 1, 0), 0xCE: ("checkportexport", 1, 0),
    0xCF: ("waitcmd", 1, 3), 0xD1: ("parentwriteport", 2, 12), 0xD2: ("childwriteport", 2, 12),
    0xD4: ("setlastnote", 1, 0), 0xD5: ("timerelate", 1, 0), 0xD6: ("nop", 1, 0),
    0xD7: ("nop", 2, 8), 0xD8: ("simpleadsr", 5, 341), 0xD9: ("transpose", 1, 0),
    0xDA: ("closetrack", 1, 0), 0xDB: ("nop", 1, 0), 0xDC: ("nop", 1, 1), 0xDD: ("nop", 2, 4),
    0xDE: ("nop", 1, 0), 0xDF: ("setinterrupt", 2, 8), 0xE0: ("disinterrupt", 1, 0),
    0xE1: ("clri", 0, 0), 0xE2: ("seti", 0, 0), 0xE3: ("reti", 0, 0), 0xE4: ("inttimer", 2, 4),
    0xE5: ("vibdepth", 1, 0), 0xE6: ("vibdepthmidi", 1, 1), 0xE7: ("synccpu", 1, 1),
    0xE8: ("flushall", 0, 0), 0xE9: ("flushrelease", 0, 0), 0xEA: ("waitcmd", 1, 2),
    0xEB: ("panpowset", 5, 0), 0xEC: ("nop", 4, 85), 0xED: ("nop", 1, 2), 0xEE: ("nop", 1, 2),
    0xEF: ("nop", 3, 0), 0xF0: ("nop", 1, 0), 0xF1: ("nop", 1, 0), 0xF2: ("nop", 3, 40),
    0xF3: ("volumemode", 1, 0), 0xF4: ("vibpitch", 1, 0), 0xFA: ("checkwave", 1, 1),
    0xFB: ("printf", 0, 0), 0xFC: ("nop", 0, 0), 0xFD: ("tempo", 1, 1), 0xFE: ("timebase", 1, 1),
    0xFF: ("finish", 0, 0),
}


def s8to16(v):
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Track1:
    def __init__(self, seq, name, parent=None, mode=0):
        self.seq, self.name, self.parent, self.mode = seq, name, parent, mode
        self.pos = 0
        self.wait = 0
        self.stack = []                # (pos, loop count or None for call)
        self.prev = None               # interrupt return position
        self.saved_wait = 0
        self.intr_on, self.intr_pend, self.intr_mask = True, 0, 0
        self.intr_vec = [0] * 8
        self.t_count = self.t_left = self.t_reload = 0
        self.regs = [0] * 32
        self.regs[6] = 0xF0            # bank 0, program 0xF0
        self.regs[7] = 0x0C            # bend sense
        self.ports = [0] * 16
        self.port_in = self.port_out = 0
        self.children = [None] * 16
        self.running = True
        self.transpose = 0
        self.last_note = 0
        self.base_time = 0
        self.connect = 0
        self.before_tie = False
        self.notes = {}                # noteid -> end tick (None = held)
        self.vol_mode = 0
        self.params = [1.0] * 18
        for i, v in ((1, 0.0), (3, 0.5), (16, 0.5), (17, 0.0), (2, 0.0), (4, 0.0), (5, 0.0),
                     (13, 0.0), (14, 0.0), (15, 0.0)):
            self.params[i] = v
        self.ptarget = list(self.params)
        self.pstep = [0.0] * 18
        self.pleft = [0] * 18
        self.release = 0
        if parent:
            self.regs = list(parent.regs)
            self.vol_mode = parent.vol_mode

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

    # --- registers ---
    @property
    def bank(self):
        return (self.regs[6] >> 8) & 0xFF

    @property
    def prog(self):
        return self.regs[6] & 0xFF

    def flag(self, v):
        self.regs[3] = v & 0xFFFF

    def xreg(self, t):            # exchangeRegisterValue
        if t < 0x40:
            return self.r32(t)
        return self.ports[(t - 0x40) & 15]

    def r32(self, t):
        if t == 0x23:
            return (self.r16(4) << 16) | self.r16(5)
        if 0x28 <= t <= 0x2B:
            return 0
        return self.r16(t)

    def r16(self, t):
        if t == 0x20:
            return self.bank
        if t == 0x21:
            return self.prog
        if t == 0x22:
            return ((self.r16(0) << 8) | self.r16(1)) & 0xFFFF
        if t == 0x2C:
            return sum(1 << i for i, c in enumerate(self.children) if c and c.running)
        if t == 0x2D:
            return sum(1 << i for i in range(8) if self.note_stopped(i))
        if t == 0x30:
            return self.stack[-1][1] if self.stack and self.stack[-1][1] is not None else 0
        return self.regs[t] if t < len(self.regs) else 0

    def wdirect(self, t, v):
        v &= 0xFFFF
        if t in (0, 1, 2):
            v &= 0xFF
            f = s8to16(v)
        elif t in (0x20, 0x21):
            return
        elif t == 0x22:
            self.wdirect(0, v >> 8)
            t, f, v = 1, v, v & 0xFF
        else:
            f = v
        if t < len(self.regs):
            self.regs[t] = v
        self.flag(f)

    def write_reg_param(self, p):
        kind = p & 0xF
        tbl_type = 0
        if kind == 11:
            src, op = 0, 11
        elif kind == 10:
            b = self.u8(); src = b & 0xC; tbl_type = (b >> 4) + 4; op = 10
        elif kind == 9:
            b = self.u8(); src = b & 0xC; op = b & 0xF0
            if src == 8:
                src = 16
        else:
            src, op = p & 0xC, p & 0x3
        target = self.u8()
        tbl = self.r32(self.u8()) if op == 10 else 0
        if src == 0:
            v = s16(self.r16(self.u8()))
        elif src == 4:
            v = self.u8()
        elif src == 12:
            v = s16(self.u16())
        elif src == 8:
            b = self.u8()
            v = s16(b << 8 if b & 0x80 else (b << 8) | (b << 1))
        else:
            v = -1
        cur = s16(self.r16(target))
        if op == 1:
            if src == 4:
                v = s8to16(v)
            v = v + cur
        elif op == 2:
            r = (cur * v) & 0xFFFFFFFF
            self.wdirect(4, r >> 16); self.wdirect(5, r & 0xFFFF)
            return
        elif op == 3:
            self.flag(cur - v)
            return
        elif op == 11:
            v = cur - v
        elif op in (0x10, 0x20):
            if src == 4:
                v = s8to16(v)
            base = cur & 0xFFFF if op == 0x10 else cur
            v = base >> -v if v < 0 else base << v
        elif op == 0x30:
            v &= cur
        elif op == 0x40:
            v |= cur
        elif op == 0x50:
            v ^= cur
        elif op == 0x60:
            v = -cur
        elif op == 0x90:
            v = self.seq.rand() % (v & 0xFFFF) if v & 0xFFFF else 0
        elif op == 10:
            v = self.load_tbl(tbl, v & 0xFFFF, tbl_type) & 0xFFFF
        v &= 0xFFFF
        f = v
        if target in (0, 1, 2):
            v &= 0xFF; f = s8to16(v)
        elif target == 0x21:
            v = (self.bank << 8) | (v & 0xFF); target = 6
        elif target == 0x20:
            v = self.prog | ((v & 0xFF) << 8); target = 6
        elif target == 0x22:
            self.wdirect(0, v >> 8); target = 1; v &= 0xFF; f = v
        elif 0x28 <= target <= 0x2B:
            return
        if target < len(self.regs):
            self.regs[target] = v
        self.flag(f)
        if target == 6:
            self.emit("prog", self.bank, self.prog)

    def load_tbl(self, base, i, kind):
        d = self.d
        if kind == 4:
            return d[base + i]
        if kind == 5:
            return struct.unpack_from(">H", d, base + 2 * i)[0]
        if kind == 6:
            return self.get24(base + 3 * i)
        if kind == 7:
            return struct.unpack_from(">I", d, base + 4 * i)[0]
        if kind == 8:
            return struct.unpack_from(">I", d, base + i)[0]
        return 0

    def total_transpose(self):
        return self.transpose + (self.parent.total_transpose() if self.parent else 0)

    def emit(self, kind, *a):
        self.seq.events.append(Event(self.seq.tick, self.seq.time, self.name, kind, a))

    def cond(self, c):
        r = self.regs[3]
        c &= 0xF
        return [True, r == 0, r != 0, r == 1, r >= 0x8000, r < 0x8000][c] if c < 6 else False

    def secs(self, ticks):
        return ticks * 60.0 / (self.seq.tempo * self.seq.timebase)

    def note_stopped(self, nid):
        end = self.notes.get(nid, -1)
        if end == -1:
            return True
        if end is None:
            self.seq.held_assumed += 1
            return True
        return self.seq.tick >= end

    # --- notes (cmdNoteOn / cmdNoteOff) ---
    def note_on(self, note):
        b = self.u8()
        if b & 0x80:
            note = self.xreg(note)
        note = (note + self.total_transpose()) & 0xFF
        connect = (b >> 5) & 3
        target = None
        if connect & 2:
            target, note = note, self.last_note
        vel = self.u8()
        if vel & 0x80:
            vel = self.xreg(vel & 0x7F) & 0xFF
        nid = b & 7
        if nid == 0:
            gate = self.u8()
            if gate & 0x80:
                gate = self.xreg(gate & 0x7F) & 0xFF
            dur = 0
            n = (b >> 3) & 3
            for _ in range(n):
                dur = (dur << 8) | self.u8()
            if n == 1 and dur & 0x80:
                dur = self.xreg(dur & 0x7F)
        else:
            if (b >> 3) & 3:
                nid = self.xreg(nid - 1) & 7
            if connect & 1:
                self.xreg(self.u8())       # skip samples (not modelled)
                connect ^= 1
            dur, gate = None, 100
        self.connect = connect
        held = (connect & 1) or dur is None
        gate_s = None if held else self.secs(dur * gate / 100.0)
        full_s = self.secs((dur or 0) * gate / 100.0)
        if self.before_tie:
            self.emit("tie", note, vel, gate_s, target, full_s)
        elif nid == 0:
            self.emit("gate", note, vel, gate_s, target, full_s)
        else:
            self.emit("on", nid, note, vel)
        if not self.before_tie or nid not in self.notes:
            self.notes[nid] = None if gate_s is None else self.seq.tick + int(round((dur or 0) * gate / 100.0))
        self.base_time = -1 if dur is None else dur
        self.before_tie = bool(connect & 1)
        if connect & 2:
            note = target
        self.last_note = note
        if dur is None:
            return 0
        self.wait = dur if dur else -1
        return 1

    def note_off(self, op):
        if op == 0xF9:
            b = self.u8()
            op = 0x80 + (self.xreg(b & 7) & 0xFF)
            if b & 0x80:
                op |= 8
        rel = 0
        if op & 8:
            rel = self.u8()
            if rel > 100:
                rel = (rel - 98) * 20
        self.emit("off", op & 7, rel)
        self.notes.pop(op & 7, None)
        return 0

    def end_process(self):          # TNoteMgr::endProcess
        if self.base_time != -1 and self.connect == 0:
            if 0 in self.notes:
                self.emit("detach", 0)
                self.notes.pop(0, None)

    def set_param(self, p):
        pid = self.u8()
        k = p & 0xC
        if k == 0:
            v = s16(self.r16(self.u8()))
        elif k == 4:
            v = self.u8()
        elif k == 8:
            b = self.u8()
            v = s16(b << 8 if b & 0x80 else (b << 8) | (b << 1))
        else:
            v = s16(self.u16())
        k = p & 3
        t = -1 if k == 0 else self.r16(self.u8()) if k == 1 else self.u8() if k == 2 else self.u16()
        val = v / 32767.0
        if pid < 18:
            self.ptarget[pid] = val
            if t <= 0:
                self.params[pid], self.pleft[pid] = val, 0
            else:
                self.pstep[pid] = (val - self.params[pid]) / t
                self.pleft[pid] = t
            self.emit("param", pid, val, max(t, 0))
        return 0

    # --- commands ---
    def command(self, op, over=0):
        name, n, types = ARGS.get(op, (None, 0, 0))
        types |= over
        a = []
        for _ in range(n):
            t = types & 3; types >>= 2
            a.append(self.u8() if t == 0 else self.u16() if t == 1 else self.u24() if t == 2
                     else self.xreg(self.u8()))
        if name is None:
            self.seq.unknown.add(op)
            return 0
        return getattr(self, "c_" + name)(*a) or 0

    def reg_command(self, use_reg, n):
        op = self.u8()
        if use_reg:
            op = self.xreg(op) & 0xFF
        over = 0
        if not use_reg or n:
            m = self.u8(); bits = 3
            for _ in range(n + 1):
                if m & 0x80:
                    over |= bits
                m = (m << 1) & 0xFF; bits <<= 2
        return self.command(op, over)

    def c_nop(self, *a): return 0

    def c_opentrack(self, p, off, bros=False):
        owner = self.parent if bros else self
        if owner is None:
            return 0
        no, mode = p & 0xF, (4 if p & 0x20 else (p >> 6) & 3)
        old = owner.children[no]
        if old:
            old.close()
        c = Track1(self.seq, f"{owner.name}.{no}", owner, mode)
        c.pos = off
        owner.children[no] = c
        self.seq.tracks_opened += 1
        return 0

    def c_opentrackbros(self, p, off): return self.c_opentrack(p, off, True)

    def _target(self):
        f = self.u8()
        if f & 0x80:
            c = self.u8()
            if f & 0x40:
                idx = self.r16(c) & 0xFFFF
                tbl = self.r16(self.u8()) if f & 0x20 else self.u24()
                return f, self.get24(tbl + idx * 3)
            return f, self.r32(c)
        return f, self.u24()

    def c_call(self):
        f, dst = self._target()
        if self.cond(f):
            self.stack.append((self.pos, None)); self.pos = dst
        return 0

    def c_ret(self, c):
        if self.cond(c):
            if not self.stack:
                return 3
            self.pos = self.stack.pop()[0]
        return 0

    def c_jmp(self):
        f, dst = self._target()
        if self.cond(f):
            self.seq.on_jump(self, self.pos, dst, f & 0xF)
            self.pos = dst
        return 0

    def c_loops(self, n):
        self.stack.append((self.pos, n)); return 0

    def c_loope(self):
        if not self.stack:
            return 0
        pos, n = self.stack[-1]
        n = (n or 0)
        if n:
            n -= 1
        if n == 0:
            self.stack.pop()
        else:
            self.stack[-1] = (pos, n); self.pos = pos
        return 0

    def c_readport(self, p, r):
        self.port_in &= ~(1 << (p & 15)); self.wdirect(r, self.ports[p & 15]); return 0

    def c_writeport(self, p, v):
        self.port_out |= 1 << (p & 15); self.ports[p & 15] = v & 0xFFFF; return 0

    def c_checkportimport(self, p): self.flag(1 if self.port_in & (1 << (p & 15)) else 0)
    def c_checkportexport(self, p): self.flag(1 if self.port_out & (1 << (p & 15)) else 0)

    def write_port_app(self, p, v):
        self.port_in |= 1 << p; self.ports[p] = v & 0xFFFF

    def c_parentwriteport(self, p, v):
        if self.parent:
            self.parent.write_port_app(p & 15, v)

    def c_childwriteport(self, p, v):
        c = self.children[(p >> 4) & 15]
        if c:
            c.write_port_app(p & 15, v)

    def c_waitcmd(self, v):
        self.wait = v
        return 1 if v else 0

    def c_setlastnote(self, k): self.last_note = (k + self.total_transpose()) & 0xFF
    def c_timerelate(self, v): pass
    def c_simpleadsr(self, a, d, s, lvl, rel): self.emit("adsr", a, d, s, lvl, rel); self.release = rel
    def c_transpose(self, v): self.transpose = s8to16(v)

    def c_closetrack(self, no):
        c = self.children[no & 15]
        if c:
            c.close()
        self.children[no & 15] = None

    def close(self):
        for c in self.children:
            if c:
                c.close()
        for nid in list(self.notes):
            self.emit("off", nid, 0)
        self.notes.clear()
        self.running = False

    def c_setinterrupt(self, i, off):
        self.intr_mask |= 1 << i; self.intr_vec[i & 7] = off

    def c_disinterrupt(self, i): self.intr_mask &= ~(1 << i)

    def c_clri(self):
        self.intr_on = True; self.prev = None

    def c_seti(self): self.intr_on = False

    def c_reti(self):
        self.intr_on = True
        if self.prev is not None:
            self.wait, self.pos, self.prev = self.saved_wait, self.prev, None
        self.try_intr()

    def c_inttimer(self, n, t): self.t_count, self.t_left, self.t_reload = n, t, t
    def c_vibdepth(self, v): pass
    def c_vibdepthmidi(self, v): pass
    def c_vibpitch(self, v): pass
    def c_synccpu(self, v): self.flag(0xFFFF)

    def c_flushall(self):
        for nid in list(self.notes):
            self.emit("off", nid, 0)
        self.notes.clear()

    def c_flushrelease(self): pass
    def c_panpowset(self, *a): pass
    def c_volumemode(self, v): self.vol_mode = v
    def c_checkwave(self, v): self.flag(0)

    def c_printf(self):
        n = 0
        for _ in range(128):
            b = self.u8()
            if b == 0:
                break
            if b == ord("\\"):
                if self.u8() == 0:
                    break
                continue
            if b == ord("%"):
                if self.u8() == 0:
                    break
                n += 1
        for _ in range(n):
            self.u8()

    def c_tempo(self, t):
        if self.parent is None:
            self.seq.tempo = t or 1
        self.emit("tempo", t)

    def c_timebase(self, t):
        if self.parent is None:
            self.seq.timebase = t or 1

    def c_finish(self): return 3

    # --- interrupts ---
    def request(self, i):
        if self.intr_mask & (1 << i):
            self.intr_pend |= 1 << i

    def try_intr(self):
        if self.prev is not None or not self.intr_on:
            return
        m = self.intr_mask & self.intr_pend
        if not m:
            return
        i = (m & -m).bit_length() - 1
        self.intr_pend &= ~(1 << i)
        self.prev, self.saved_wait, self.wait = self.pos, self.wait, 0
        self.pos = self.intr_vec[i]

    def timer(self):
        if not self.t_left:
            return
        self.t_left -= 1
        if self.t_left:
            return
        self.request(6)
        if self.t_count:
            self.t_count -= 1
            if self.t_count:
                self.t_left = self.t_reload
        else:
            self.t_left = self.t_reload

    # --- parse / tick ---
    def parse(self):
        guard = 0
        while True:
            guard += 1
            if guard > 100000:
                raise RuntimeError(f"{self.name}: runaway at {self.pos:#x}")
            self.seq.visit.setdefault((self.name, self.pos), self.seq.time)
            op = self.u8()
            if not op & 0x80:
                r = self.note_on(op)
            elif (op & 0xF0) == 0x80 and not op & 7:
                n = 1 if op == 0x80 else 2
                v = 0
                for _ in range(n):
                    v = (v << 8) | self.u8()
                self.wait = v
                r = 1 if v else 0
            elif (op & 0xF0) == 0x80 or op == 0xF9:
                r = self.note_off(op)
            elif (op & 0xF0) == 0x90:
                r = self.set_param(op & 0xF)
            elif (op & 0xF0) == 0xA0:
                self.write_reg_param(op & 0xF); r = 0
            elif (op & 0xF0) == 0xB0:
                r = self.reg_command((op >> 3) & 1, op & 7)
            else:
                r = self.command(op)
            if r == 1:
                return 0
            if r == 3:
                return -1

    def tick(self):
        if not self.running:
            return -1
        r = 0
        self.request(7)
        self.timer()
        self.try_intr()
        go = True
        if self.wait == -1:
            if self.note_stopped(0):
                self.wait = 0
            else:
                go = False
        if go and self.wait > 0:
            self.wait -= 1
            if self.wait:
                go = False
            else:
                self.end_process()
        if go:
            r = self.parse()
        for i in range(18):
            if self.pleft[i]:
                self.params[i] += self.pstep[i]
                self.pleft[i] -= 1
                if not self.pleft[i]:
                    self.params[i] = self.ptarget[i]
        if r < 0:
            self.close()
            return -1
        for i, c in enumerate(self.children):
            if c and c.running:
                if c.tick() < 0:
                    c.close()
                    self.children[i] = None
        return 0


class Sequence1:
    def __init__(self, data: bytes, max_seconds=600.0, loops=2):
        self.data = data + b"\0" * 16
        self.events = []
        self.tick = 0
        self.time = 0.0
        self.tempo, self.timebase = 120, 0x30
        self.unknown = set()
        self.tracks_opened = 0
        self.max_seconds, self.loops_wanted = max_seconds, loops
        self.root = Track1(self, "root")
        self.visit = {}
        self.loop = None
        self.loop_hits = 0
        self.track_loops, self.loop_hits_by = {}, {}
        self._rand = 0x12345
        self.held_assumed = 0
        self.state = {}

    def rand(self):
        self._rand = (self._rand * 1664525 + 1013904223) & 0xFFFFFFFF
        return self._rand >> 9

    def on_jump(self, tr, src, dst, cond):
        """A track's loop is its first unconditional jump back to code it already ran.
        The song loop is the root's, unless the root only spins (waiting on the game)
        while a child carries the music: then the longest child loop is used."""
        if dst >= src or cond != 0 or (tr.name, dst) not in self.visit:
            return
        lp = (self.visit[(tr.name, dst)], self.time)
        self.track_loops.setdefault(tr.name, lp)
        hits = self.loop_hits_by.get(tr.name, 0) + 1
        self.loop_hits_by[tr.name] = hits
        root = self.track_loops.get("root")
        if root and root[1] - root[0] >= 2.0:
            self.loop, self.loop_hits = root, self.loop_hits_by["root"]
        else:
            best = max((v for k, v in self.track_loops.items() if k != "root"),
                       key=lambda v: v[1] - v[0], default=None)
            if best and best[1] - best[0] >= 2.0:
                self.loop = best
                self.loop_hits = max(h for k, h in self.loop_hits_by.items()
                                     if self.track_loops.get(k) == best)

    def snapshot(self, tr, pvol=1.0, pbend=0.0):
        v = tr.params[0]
        if tr.vol_mode == 0:
            v *= v
        v *= pvol
        # Player::pitchToCent: semitones = pitch * 4 * bend sense -> octaves
        bend = pbend + tr.params[1] * tr.regs[7] / 3.0
        pan = tr.params[3]
        st = (round(v, 5), round(pan, 4), round(bend, 5))
        if self.state.get(tr.name) != st:
            self.state[tr.name] = st
            self.events.append(Event(self.tick, self.time, tr.name, "state", st))
        for c in tr.children:
            if c and c.running:
                self.snapshot(c, v, bend)

    def run(self):
        r = self.root
        while self.time < self.max_seconds:
            if r.tick() < 0 or not r.running:
                return "finished"
            self.snapshot(r)
            if self.loop and self.loop_hits >= self.loops_wanted:
                return "looped"
            self.tick += 1
            self.time += 60.0 / (self.tempo * self.timebase)
        return "timeout"
