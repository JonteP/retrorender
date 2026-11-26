"""JAudio2 sequence interpreter on a hand-made BMS (python3 tests/test_jaudio.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from engines.jaudio.render import FxLine, analyse, sync_tracks  # noqa: E402
from engines.jaudio.seq import Sequence  # noqa: E402

import numpy as np  # noqa: E402

# tempo 120, program 5, two 48-tick notes, jump back to the first note (a loop)
data = bytes([
    0xE0, 0x00, 0x78,              # tempo 120
    0xE3, 0x05,                    # program 5
    0x3C, 0x00, 0x64, 0x30,        # gate note at 0x05: key 60, vel 100, 48 ticks
    0x40, 0x00, 0x64, 0x30,        # gate note: key 64
    0xC7, 0x00, 0x00, 0x05,        # jump to 0x05
])
s = Sequence(data, max_seconds=10, loops=2)
assert s.run() == "looped", s.loop
gates = [e for e in s.events if e.kind == "gate"]
assert [g.a[0] for g in gates[:3]] == [60, 64, 60], gates[:3]
assert all(g.track == "root" for g in gates)
# 48 ticks at tempo 120 / timebase 48 = 0.5 s per note
assert abs(gates[1].time - 0.5) < 0.02 and abs(gates[0].a[2] - 0.5) < 0.01
assert abs((s.loop[1] - s.loop[0]) - 1.0) < 0.02, s.loop
assert ("prog" in {e.kind for e in s.events})

# a marker track (only keys 24-31, bar counter in bank/program) is muted
m = bytearray([0xE0, 0x00, 0x98, 0xC1, 0x00, 0x00, 0x00, 0x0C, 0xF1, 0xFF, 0xFF, 0xFF])
body = bytearray()
for bar in range(5):
    body += bytes([0xE1, bar, bar])                 # bank/program = bar number
    body += bytes([0x18, 0x01, 0x6E, 0x80, 0x18])   # note-on key 24 id 1
    body += bytes([0xF1, 0x30, 0x81])               # wait 48, note off id 1
body += bytes([0xFF])
seq = Sequence(bytes(m) + bytes(body), max_seconds=10)
seq.run()
assert sync_tracks(seq.events) == {"root.0"}, sync_tracks(seq.events)
assert analyse(bytes(m) + bytes(body))[2] is False

# FX line: an impulse comes back after the delay through the filter taps
fx = FxLine(100)
x = np.zeros(400, np.float32); x[0] = 1.0
y = np.concatenate([fx.process(x[:150]), fx.process(x[150:])])
assert abs(y[99] - 0x1000 / 32768) < 1e-6 and abs(y[94] - 0x3FFF / 32768) < 1e-6, np.nonzero(y)[0][:4]
print("ok")

# --- JAudio 1 (Wind Waker) encoding: tempo, two gate notes, unconditional jump back ---
from engines.jaudio.seq1 import Sequence1  # noqa: E402
from engines.jaudio.rarc import read_rarc  # noqa: E402

ww = bytes([
    0xFD, 0x00, 0x78,              # tempo 120
    0x3C, 0x08, 0x64, 0x64, 0x60,  # at 0x03: key 60, 1 duration byte, vel 100, gate 100 %, 96 ticks
    0x40, 0x08, 0x64, 0x32, 0x60,  # key 64, gate 50 %
    0xC8, 0x00, 0x00, 0x00, 0x03,  # jump (always) to 0x03
])
s1 = Sequence1(ww, max_seconds=10, loops=2)
assert s1.run() == "looped"
g1 = [e for e in s1.events if e.kind == "gate"]
assert [g.a[0] for g in g1[:3]] == [60, 64, 60], g1[:3]
assert abs(g1[0].a[2] - 1.0) < 0.01 and abs(g1[1].a[2] - 0.5) < 0.01       # gate percentage
assert abs((s1.loop[1] - s1.loop[0]) - 2.0) < 0.02, s1.loop

# --- RARC: one folder with one file ---
def rarc(files):
    names = b"\0".join([b".", b".."] + [n for n, _ in files]) + b"\0"
    data = b"".join(d for _, d in files)
    entries = b""
    off = 0
    name_off = 5
    for n, d in files:
        entries += struct.pack(">HHBBHIII", 0, 0, 0x11, 0, name_off, off, len(d), 0)
        name_off += len(n) + 1
        off += len(d)
    nodes = struct.pack(">4sIHHI", b"ROOT", 0, 0, len(files), 0)
    info = struct.pack(">6I", 1, 0x20, len(files), 0x20 + len(nodes), len(names), 0x20 + len(nodes) + len(entries))
    info += b"\0" * 8
    body = info + nodes + entries + names
    header = struct.pack(">4sIIIIII", b"RARC", 0, 0x20, len(body), len(data), len(data), 0) + b"\0" * 4
    return header + body + data

import struct  # noqa: E402
arc = read_rarc(rarc([(b"song.bms", ww), (b"jingle.bms", b"\xff")]))
assert arc == {"song.bms": ww, "jingle.bms": b"\xff"}, list(arc)
print("ok: jaudio 1")
