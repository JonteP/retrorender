"""Tiny hand-assembled xSF files that make a tone, for testing xsfplay without real rips.

    python3 fixtures.py OUTDIR

writes:
  tone.gsf                GBA: PSG square wave, ~440 Hz
  tone.minigsf + tone.gsflib   the same program split into a library and a patch that
                          changes the pitch to ~880 Hz (tests the _lib chain)
  tone.ssf                Saturn: one SCSP slot looping a 16-bit square wave, 441 Hz
  tone.dsf                Dreamcast: the same on the AICA, driven by the ARM7
  tone.2sf                DS: PSG channel 8, square wave, ~440 Hz

Each program only writes a few sound registers and then loops forever; they are not
derived from any game. The machine code is written out by hand below.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path


def psf(version: int, program: bytes, tags: dict[str, str] | None = None,
        reserved: bytes = b"") -> bytes:
    comp = zlib.compress(program, 9) if program else b""
    out = b"PSF" + bytes([version]) + struct.pack("<III", len(reserved), len(comp),
                                                  zlib.crc32(comp) & 0xFFFFFFFF)
    out += reserved + comp
    if tags:
        out += b"[TAG]" + "".join(f"{k}={v}\n" for k, v in tags.items()).encode()
    return out


def words(*ws: int) -> bytes:
    return b"".join(struct.pack("<I", w) for w in ws)


# ------------------------------------------------------------------ GBA

def gba_rom(freq_reg: int = 1750) -> bytes:
    """ARM code at 0xC0: enable sound, PSG channel 1 at 131072/(2048-n) Hz, loop."""
    rom = bytearray(0x400)
    rom[0:4] = words(0xEA00002E)                # b 0xC0
    rom[0xB2] = 0x96                            # fixed header byte
    code = words(
        0xE3A00301,                             # mov r0, #0x04000000
        0xE59F101C,                             # ldr r1, =0x00000080
        0xE5801084,                             # str r1, [r0, #0x84]   SOUNDCNT_X: on
        0xE59F1018,                             # ldr r1, =0x0002FF77
        0xE5801080,                             # str r1, [r0, #0x80]   SOUNDCNT_L/H
        0xE59F1014,                             # ldr r1, =0xF0800000
        0xE5801060,                             # str r1, [r0, #0x60]   SOUND1CNT_L/H
        0xE59F1010,                             # ldr r1, =freq
        0xE5801064,                             # str r1, [r0, #0x64]   SOUND1CNT_X
        0xEAFFFFFE,                             # b .
        0x00000080, 0x0002FF77, 0xF0800000, 0x00008000 | freq_reg,
    )
    rom[0xC0:0xC0 + len(code)] = code
    return bytes(rom)


def gsf_program(rom: bytes, offset: int = 0x08000000) -> bytes:
    return struct.pack("<III", 0x08000000, offset, len(rom)) + rom


# ------------------------------------------------------------------ Saturn

def m68k_move_w(value: int, addr: int) -> bytes:
    return struct.pack(">HHI", 0x33FC, value, addr)    # move.w #value, (addr).l


def ssf_program() -> bytes:
    ram = bytearray(0x2000)
    ram[0:8] = struct.pack(">II", 0x0007F000, 0x400)  # reset SP, PC
    slot = 0x100000
    code = b"".join([
        m68k_move_w(0x000F, 0x100400),          # master volume 15
        m68k_move_w(0x1000, slot + 0x02),       # start address 0x1000
        m68k_move_w(0x0000, slot + 0x04),       # loop start 0
        m68k_move_w(0x0064, slot + 0x06),       # loop end 100 samples
        m68k_move_w(0x001F, slot + 0x08),       # attack rate max
        m68k_move_w(0x001F, slot + 0x0A),       # release rate max
        m68k_move_w(0x0000, slot + 0x0C),       # total level 0 (loudest)
        m68k_move_w(0x0000, slot + 0x10),       # octave 0, fns 0: 44100 samples/s
        m68k_move_w(0xE000, slot + 0x16),       # direct send level 7, centre
        m68k_move_w(0x1820, slot + 0x00),       # key on + execute, forward loop, 16-bit
        b"\x60\xFE",                            # bra.s .
    ])
    ram[0x400:0x400 + len(code)] = code
    for i in range(100):                        # 16-bit big-endian square wave
        struct.pack_into(">h", ram, 0x1000 + i * 2, 8000 if i < 50 else -8000)
    return struct.pack("<I", 0) + bytes(ram)


# ------------------------------------------------------------------ Dreamcast

def arm_code(base: int, ops: list) -> bytes:
    """ARM words; ("ldr", rd, value) becomes ldr rd, [pc, #x] with a literal pool after the code."""
    lits = [op[2] for op in ops if isinstance(op, tuple)]
    pool = base + 4 * len(ops)
    out, n = [], 0
    for i, op in enumerate(ops):
        if isinstance(op, tuple):
            off = pool + 4 * n - (base + 4 * i + 8)
            out.append(0xE59F0000 | (op[1] << 12) | off)
            n += 1
        else:
            out.append(op)
    return words(*out, *lits)


def dsf_program() -> bytes:
    """ARM7: AICA slot 0 loops a 16-bit square wave of 100 samples (441 Hz)."""
    ram = bytearray(0x2000)
    ram[0:4] = words(0xEA00000E)                # b 0x40
    for v in range(4, 0x20, 4):                 # exception vectors: return at once
        ram[v:v + 4] = words(0xE25EF004)        # subs pc, lr, #4
    code = arm_code(0x40, [
        0xE321F0D3,                             # msr cpsr_c, #0xD3     IRQ/FIQ off
        ("ldr", 0, 0x00800000),                 # r0 = AICA registers
        ("ldr", 1, 0x0F), ("ldr", 2, 0x2800),
        0xE7801002,                             # str r1, [r0, r2]      master volume 15
        ("ldr", 1, 0x1000),
        0xE5801004,                             # str r1, [r0, #4]      start address
        0xE3A01000, 0xE5801008,                 # loop start 0
        0xE3A01064, 0xE580100C,                 # loop end 100
        0xE3A0101F, 0xE5801010, 0xE5801014,     # attack and release rate max
        0xE3A01000, 0xE5801018,                 # octave 0, fns 0: 44100 samples/s
        0xE3A01020, 0xE5801028,                 # total level 0, filter off
        0xE3A01C0F, 0xE5801024,                 # direct send level 15, centre
        ("ldr", 1, 0xC200),
        0xE5801000,                             # key on + execute, forward loop, 16-bit
        0xEAFFFFFE,                             # b .
    ])
    ram[0x40:0x40 + len(code)] = code
    for i in range(100):                        # 16-bit little-endian square wave
        struct.pack_into("<h", ram, 0x1000 + i * 2, 8000 if i < 50 else -8000)
    return struct.pack("<I", 0) + bytes(ram)


# ------------------------------------------------------------------ DS

def nds_rom() -> bytes:
    """ARM9 just loops; ARM7 starts PSG channel 8 (square) at ~440 Hz."""
    arm9 = words(0xEAFFFFFE)
    arm7 = words(
        0xE59F0020,                             # ldr r0, =0x04000500
        0xE59F1020,                             # ldr r1, =0x0000807F
        0xE5801000,                             # str r1, [r0]          SOUNDCNT on, vol 127
        0xE59F001C,                             # ldr r0, =0x04000480
        0xE59F101C,                             # ldr r1, =timer
        0xE5801008,                             # str r1, [r0, #8]      SOUND8TMR
        0xE59F1018,                             # ldr r1, =cnt
        0xE5801000,                             # str r1, [r0]          SOUND8CNT: start
        0xE1A00000,                             # nop
        0xEAFFFFFE,                             # b .
        0x04000500, 0x0000807F, 0x04000480,
        (0x10000 - 16756991 // (440 * 8)) & 0xFFFF,
        0x80000000 | (3 << 29) | (1 << 27) | (3 << 24) | (0x40 << 16) | 0x7F,
    )
    rom = bytearray(0x400)
    rom[0:12] = b"XSFPLAYTEST\0"
    struct.pack_into("<IIII", rom, 0x20, 0x200, 0x02000000, 0x02000000, len(arm9))
    struct.pack_into("<IIII", rom, 0x30, 0x300, 0x02380000, 0x02380000, len(arm7))
    struct.pack_into("<I", rom, 0x80, len(rom))
    rom[0x200:0x200 + len(arm9)] = arm9
    rom[0x300:0x300 + len(arm7)] = arm7
    return bytes(rom)


def ds_program(rom: bytes, offset: int = 0) -> bytes:
    return struct.pack("<II", offset, len(rom)) + rom


def write_all(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    tags = {"title": "Test tone", "artist": "xsfplay", "game": "Fixtures",
            "length": "0:02", "fade": "1"}
    files = {
        "tone.gsf": psf(0x22, gsf_program(gba_rom()), tags),
        "tone.gsflib": psf(0x22, gsf_program(gba_rom())),
        # patch the frequency literal: 2048 - 131072/880 = 1899
        "tone.minigsf": psf(0x22, gsf_program(struct.pack("<I", 0x8000 | 1899),
                                              0x08000000 + 0xC0 + 13 * 4),
                            {**tags, "_lib": "tone.gsflib", "title": "Test tone 880"}),
        "tone.ssf": psf(0x11, ssf_program(), tags),
        "tone.dsf": psf(0x12, dsf_program(), tags),
        "tone.2sf": psf(0x24, ds_program(nds_rom()), tags),
    }
    paths = {}
    for name, data in files.items():
        p = out / name
        p.write_bytes(data)
        paths[name] = p
    return paths


if __name__ == "__main__":
    for p in write_all(Path(sys.argv[1] if len(sys.argv) > 1 else ".")).values():
        print(p)
