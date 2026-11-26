"""Creates two small test files: a VGM (SN76489, with intro + loop) and a MOD."""
import struct
import sys
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "testlib")
(out / "Sega").mkdir(parents=True, exist_ok=True)
(out / "Amiga").mkdir(parents=True, exist_ok=True)

# --- VGM: 1 s intro, then a 2 s loop ---------------------------------------
CLK = 3579545


def tone(ch, hz, vol=2):
    n = int(CLK / (32 * hz))
    psg = [0x80 | (ch << 5) | (n & 0xF), (n >> 4) & 0x3F, 0x90 | (ch << 5) | vol]
    return b"".join(bytes([0x50, x]) for x in psg)   # 0x50 = write to the PSG


def wait(samples):
    b = b""
    while samples > 0:
        w = min(samples, 65535)
        b += bytes([0x61]) + struct.pack("<H", w)
        samples -= w
    return b


intro = tone(0, 262) + wait(22050) + tone(0, 330) + wait(22050)
loop = b"".join(tone(0, f) + wait(11025) for f in (392, 440, 494, 523, 494, 440, 392, 330))
data = intro + loop + b"\x66"
hdr = bytearray(0x40)
hdr[0:4] = b"Vgm "
struct.pack_into("<I", hdr, 0x04, 0x40 + len(data) - 4)
struct.pack_into("<I", hdr, 0x08, 0x150)
struct.pack_into("<I", hdr, 0x0C, CLK)
struct.pack_into("<I", hdr, 0x18, 44100 * 3)                 # totalt antal samples
struct.pack_into("<I", hdr, 0x1C, 0x40 + len(intro) - 0x1C)  # loopoffset (relativ)
struct.pack_into("<I", hdr, 0x20, 44100 * 2)                 # loopens samples
struct.pack_into("<I", hdr, 0x34, 0x40 - 0x34)               # dataoffset (relativ)
(out / "Sega" / "test.vgm").write_bytes(bytes(hdr) + data)

# --- MOD: one channel, square wave, 4 notes per bar ---------------------------
title = b"retrorender test".ljust(20, b"\0")
smp = bytes([0x40] * 16 + [0xC0] * 16)                       # 32 byte fyrkant
s1 = b"square".ljust(22, b"\0") + struct.pack(">HBBHH", len(smp) // 2, 0, 48, 0, len(smp) // 2)
empty = bytes(22) + struct.pack(">HBBHH", 0, 0, 0, 0, 1)
pat = bytearray(1024)
periods = [428, 381, 339, 320, 285, 254, 226, 214]
for row in range(0, 64, 4):
    per = periods[(row // 4) % len(periods)]
    off = row * 16
    pat[off:off + 4] = bytes([(1 & 0xF0) | (per >> 8), per & 0xFF, (1 & 0x0F) << 4, 0])
mod = title + s1 + empty * 30 + bytes([1, 0x7F]) + bytes(128) + b"M.K." + pat + smp
(out / "Amiga" / "test.mod").write_bytes(mod)
print("skrev", out)

# --- VGZ som X68000 (YM2151 + OKIM6258) med GD3-taggar --------------------
# No real chip commands: the file tests engine selection, not the sound.
import gzip


def gd3(**t):
    order = ("title", "title_jp", "game", "game_jp", "system", "system_jp",
             "author", "author_jp", "date", "ripper", "notes")
    body = "".join(t.get(k, "") + "\0" for k in order).encode("utf-16-le")
    return b"Gd3 " + struct.pack("<II", 0x100, len(body)) + body


(out / "X68000").mkdir(parents=True, exist_ok=True)
xdata = wait(44100 * 4) + b"\x66"                      # 4 s, varav loop 3 s
xhdr = bytearray(0x100)
xhdr[0:4] = b"Vgm "
struct.pack_into("<I", xhdr, 0x08, 0x171)
struct.pack_into("<I", xhdr, 0x30, 4000000)           # YM2151
struct.pack_into("<I", xhdr, 0x90, 4000000)           # OKIM6258
struct.pack_into("<I", xhdr, 0x18, 44100 * 4)
struct.pack_into("<I", xhdr, 0x1C, 0x100 + 3 - 0x1C)  # loop after the first wait command
struct.pack_into("<I", xhdr, 0x20, 44100 * 3)
struct.pack_into("<I", xhdr, 0x34, 0x100 - 0x34)
tags = gd3(title="Vampire Killer (test)", game="Akumajo Dracula", system="Sharp X68000",
           author="Konami Kukeiha Club")
struct.pack_into("<I", xhdr, 0x14, 0x100 + len(xdata) - 0x14)
raw = bytearray(bytes(xhdr) + xdata + tags)
struct.pack_into("<I", raw, 0x04, len(raw) - 4)              # EOF-offset
(out / "X68000" / "test.vgz").write_bytes(gzip.compress(bytes(raw)))
print("skrev X68000/test.vgz")
