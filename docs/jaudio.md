# JAudio2 sequenced music (BMS) — notes from building retrorender's engine

Everything here was read from the decompiled game code (zeldaret/tp `libs/JSystem/src/JAudio2`,
SMGCommunity/Petari) and checked against Super Mario Galaxy's files. Offsets are big-endian.

## Files
- `*.bms` — sequence program (no header; execution starts at offset 0 in the root track).
- `*.baa` — archive of instrument banks and wave systems. Chunk list after `AA_<`:
  `bst `/`bstn`/`bsc ` (12 bytes), `ws  ` (16: wave system id, offset, flags),
  `bnk ` (12: wave system id, offset), until `>_AA`. The bank number a sequence selects is the
  IBNK's global id (header +8), not its position.
- `*.aw` — raw sample data, addressed by offset/length from the wave system.

## Sequence program
- `0x00–0x7F` note: next byte `b` (low 3 bits = voice id, bits 0/1 = sweep/tie flags), velocity.
  Voice id 0 = gate note followed by a MIDI-style variable-length duration in ticks (0 = wait
  until the note stops); 1–7 = note-on held until `0x81–0x87` (note-off for that id).
- `0x90–0x97` register form: a mask byte marks which of the next command's
  `(low 3 bits + 1)` arguments are register numbers, then the command itself.
- `0xA0–0xFF` commands, `0xB0` = extended table (only `0xB0 0x01` dump exists).
  Argument types are 2 bits each, LSB first: 0 u8, 1 u16, 2 u24, 3 register.

| op | command | args (types) | op | command | args |
|---|---|---|---|---|---|
| B1 | note on | 3 | D8 | reg load | 2 (u8,u16) |
| B2 | note off | 1 | D9/DA | reg arith | 3 |
| B3 | note | flags, key, velocity, u16 duration | DB | reg unary | 2 |
| B4 | set last note | 1 | DC | reg table load | 4 |
| B8–BB | param E/I/EI/II (volume 0, pitch 1, fx 2, pan 3, dolby 4, filter 5) | 2–3 | E0 | tempo | u16 |
| C1 | open track | u8, u24 | E1/E2/E3 | bank+prog / bank / prog | |
| C2 | close track | 1 | E7–E9 | envelope scale / table / simple ADSR | |
| C3/C4 | call / conditional call | u24 | EA–ED | bus connect, IIR cutoff/set, FIR | |
| C5/C6 | return / conditional return | | F0 | wait (variable length) | |
| C7/C8 | jump / conditional jump | u24 | F1 | wait | u8 |
| C9/CA | jump / call through table | u8, u24 | F3–F8 | interrupt table, mask, return, timer | |
| CB/CC | loop start (count) / loop end | | F9 | sync CPU (reg 3 = 0xFFFF without a game) | |
| D0–D7 | port read/write (self, parent, child) | | FD/FE/FF | printf / nop / finish | |

Conditions: 0 always, 1 reg3==0, 2 reg3!=0, 3 reg3==1, 4 reg3>=0x8000, 5 reg3<0x8000.
Registers 0x62 timebase (default 48), 0x63 transpose, 0x64 bend sense (12), 0x67/0x68 bank/program.
Time: tempo × timebase / 60 ticks per second; the root track's tempo drives all children.

## Banks (IBNK version 1)
Chunks `ENVT` (envelope points: mode, time, value as s16 triples), `OSCT` (`Osci` records:
target, rate, attack/release table offsets into ENVT, scale, vertex), `LIST` (per program:
`Inst` or `Perc`). `Inst`: oscillator indices, effects, key regions (high key, velocity regions
of [high velocity, wave id (low 16 bits), volume f32, pitch f32]), then volume and pitch f32.
`Perc`: `Pmap` per key with volume, pitch, pan, release and a wave id.
Envelope modes 0–3 = linear / square / square root / "sample cell" curves (16-step tables),
0xD loop, 0xE hold, 0xF stop. Envelope time units are 1/600 s.

## Samples
Format 0 = 4-bit ADPCM in 9-byte frames of 16 samples (header: high nibble = scale exponent,
low nibble = predictor), the "AFC" coefficient table in `JASDsp::DSPADPCM_FILTER`;
1 = 2-bit ADPCM (5-byte frames), 2 = PCM8, 3 = PCM16.

## Mixing
Volume = (velocity/127)² × instrument × region × (track volume)² × envelope; sine pan law;
pitch = 2^((key − base key)/12 + bend) × instrument pitch × sample rate / DAC rate.
Voices update every DSP subframe (80 samples at 32 kHz).

## Galaxy specifics
- `*_midi_cnv.bms` belong to orchestral recordings (`SMG_*_strm.ast`): one track plays only
  keys 24/25/27/30 and steps bank/program as a bar counter (beat markers), other tracks
  hold layers the game plays over the recording.
- FX (reverb/echo) comes from `AudEffector::mFxLine`: four lines, 64-subframe buffers,
  returned to left/right at 0x7000, filter `{0, 0x1000, 0, 0, 0, 0, 0x3FFF, 0}`. The send
  amount is set per level by the game, not by the sequence.

## JAudio 1 (Wind Waker) differences
Source: zeldaret/tww `libs/JSystem/src/JAudio` (JASystem::TSeqParser, TTrack, BNKParser).
- Files: `JaiInit.aaf` is a list of u32 records: type 1 sound table, type 2 banks and
  type 3 wave systems (each `offset, size, x` triples ending in 0; for banks x is the wave
  system index), 5 stream list, 6 SE scenes, 7 FX scenes, 0 end. Sequences are packed in a
  RARC archive (`JaiSeqs.arc`). The bank number a sequence uses is the IBNK id at +8.
- Banks are IBNK with a `BANK` block at +0x20: 128 instrument offsets at +0x24, 12
  percussion sets at +0x3B4 (programs 0xE4–0xEF). `INST`: volume, pitch, 2 oscillators,
  2 random and 2 sense effects, key regions with velocity regions (high velocity, wave id,
  volume, pitch). `PERC`/`PER2`: 128 key maps, PER2 adds pan and release per key.
- Sequence encoding:
  - `0x00–0x7F` note: flag byte (bit7 key from register, bits 5–6 tie/sweep, bits 3–4
    number of duration bytes, bits 0–2 voice id), velocity, and for voice 0 a gate
    percentage and the duration. Duration 0 waits for the note to end.
  - `0x80` / `0x88` wait (1/2 bytes), `0x81–0x8F` and `0xF9` note-off (bit 3 = release byte).
  - `0x90–0x9F` timed parameter (value and time encodings in the low bits),
    `0xA0–0xAF` register arithmetic, `0xB0–0xBF` command with register arguments.
  - `0xC0–0xFF` commands, e.g. C1 open track, C4 call, C6 return, C8 jump (flag byte:
    condition in low nibble, bit 7 = register/table target), C9/CA loop, DF interrupt,
    FD tempo, FE timebase, FF finish.
- Bend: semitones = pitch parameter × 4 × bend sense (register 7, default 12).
- FX: `JaiInit.aaf` record type 7 holds FX scenes (JAInter::Fx): scene count, four buffer
  sizes, scene offsets; each scene is four `FxlineConfig_` (on, two destinations with
  levels, buffer size in subframes, 8 filter taps). Scene 0 is set at start-up: lines 0/1
  are 80 ms (return 0x2000, tap 0x3FFF), lines 2/3 140 ms (return 0x4000, tap 0x5800).
  Scene 1 routes lines 2/3 into other buses. The game picks the scene by location.
