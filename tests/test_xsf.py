"""xSF engine: tags, lengths and fades, _lib checks, the BIOS lookup and the render pipeline
(python3 tests/test_xsf.py). Uses a stand-in for xsfplay, plus the real one if it is on PATH
or in $XSFPLAY."""
import os
import shutil
import stat
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tools" / "xsfplay"))
import fixtures  # noqa: E402
from engines import Registry  # noqa: E402
from engines.base import EngineError  # noqa: E402
from engines.xsf import XsfEngine, lib_chain, parse_time, read_header  # noqa: E402
from policy import plan, BUILTIN  # noqa: E402

assert parse_time("1:23") == 83 and parse_time("1:02:03") == 3723
assert parse_time("2,5") == 2.5 and parse_time("0:10.25") == 10.25
assert parse_time("") is None and parse_time("abc") is None

tmp = Path(tempfile.mkdtemp())
files = fixtures.write_all(tmp / "rip")

# --- tags ------------------------------------------------------------------------------------
v, tags = read_header(files["tone.minigsf"])
assert v == 0x22 and tags["_lib"] == "tone.gsflib" and tags["title"] == "Test tone 880", tags
(tmp / "multi.psf").write_bytes(fixtures.psf(0x01, b"", {"comment": "a"}) + b"comment=b\nutf8=1\ntitle=\xc3\x85ngest\n")
v, tags = read_header(tmp / "multi.psf")
assert tags["comment"] == "a\nb" and tags["title"] == "Ångest", tags
(tmp / "ansi.psf").write_bytes(fixtures.psf(0x01, b"", {"game": "g"}) + b"title=\xc5ngest\n")
assert read_header(tmp / "ansi.psf")[1]["title"] == "Ångest"
try:
    (tmp / "x.psf").write_bytes(b"nope")
    read_header(tmp / "x.psf")
    raise AssertionError("not a PSF")
except EngineError:
    pass

# --- _lib chains -----------------------------------------------------------------------------
assert lib_chain(files["tone.minigsf"], read_header(files["tone.minigsf"])[1]) == []
(tmp / "rip" / "orphan.minigsf").write_bytes(fixtures.psf(0x22, b"", {"_lib": "gone.gsflib"}))
assert lib_chain(tmp / "rip" / "orphan.minigsf", {"_lib": "gone.gsflib"}) == ["gone.gsflib"]
# Windows rips are sloppy about case
assert lib_chain(files["tone.minigsf"], {"_lib": "TONE.GSFLIB"}) == []

# --- probe and the play plan -----------------------------------------------------------------
eng = XsfEngine(44100, cli="definitely-not-here")
assert not eng.available()
t = eng.probe(files["tone.gsf"])[0]
assert (t.title, t.author, t.game, t.system) == ("Test tone", "xsfplay", "Fixtures", "Game Boy Advance")
assert t.length_s == 3 and t.extra["fade_at_end"] and t.extra["fade_s"] == 1
pl = plan(t, dict(BUILTIN))
assert pl.seconds == 3 and pl.fade_s == 1, pl                 # the tag's fade, not the default 8
t = eng.probe(tmp / "rip" / "orphan.minigsf")[0]
assert t.title == "orphan" and t.length_s is None and t.extra["missing_lib"] == ["gone.gsflib"]
assert eng.probe(files["tone.2sf"])[0].extra["native_rate"] == 32728

# --- BIOS ------------------------------------------------------------------------------------
meta = tmp / "meta"
eng = XsfEngine(44100, meta_dir=str(meta))
assert eng.bios() is None and str(meta / "bios") in eng.bios_hint()
eng.cli = "xsfplay"
try:
    eng.command(tmp / "multi.psf", 0x01)
    raise AssertionError("PS1 without a BIOS")
except EngineError as e:
    assert "BIOS" in str(e)
(meta / "bios").mkdir(parents=True)
(meta / "bios" / "small.bin").write_bytes(b"\0" * 1000)
assert eng.bios() is None                                     # neither hebios nor a PS2 dump
(meta / "bios" / "scph39001.bin").write_bytes(b"\0" * 0x400000)
assert eng.bios().name == "scph39001.bin"
(meta / "bios" / "hebios.bin").write_bytes(b"\0" * 1000)
assert eng.bios().name == "hebios.bin"
cmd = eng.command(tmp / "multi.psf", 0x01)
assert cmd[cmd.index("-b") + 1].endswith("hebios.bin") and "-c" in cmd
assert "-b" not in eng.command(files["tone.gsf"], 0x22)
assert "-j" not in eng.command(files["tone.2sf"], 0x24)
assert "-j" in XsfEngine(44100, ds_jit=True).command(files["tone.2sf"], 0x24)

# --- rendering through a stand-in xsfplay ----------------------------------------------------
fake = tmp / "fake-xsfplay"
fake.write_text(f"""#!{sys.executable}
import struct, sys
args = sys.argv[1:]
path = args[-1]
if "fail" in path:
    sys.stderr.write("Opened file\\nxsfplay: the emulator rejected the program data\\n")
    sys.exit(1)
rate = 32728 if path.endswith("2sf") else int(args[args.index("-r") + 1])
h = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
sys.stdout.buffer.write(h + b"data" + struct.pack("<I", 0xFFFFFFFF))
frame = struct.pack("<hh", 1000, -1000)
try:
    while True:
        sys.stdout.buffer.write(frame * 4096)
except BrokenPipeError:
    pass
""")
fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
eng = XsfEngine(44100, cli=str(fake))
if eng.available():                                           # needs ffmpeg for the 2SF part
    t = eng.probe(files["tone.gsf"])[0]
    pcm = b"".join(eng.render(files["tone.gsf"], t, 10000))
    assert len(pcm) == 40000 and pcm[:4] == struct.pack("<hh", 1000, -1000), pcm[:8]
    t = eng.probe(files["tone.2sf"])[0]
    pcm = b"".join(eng.render(files["tone.2sf"], t, 10000))
    assert len(pcm) == 40000, len(pcm)                        # resampled by ffmpeg
    bad = tmp / "rip" / "fail.gsf"
    shutil.copy(files["tone.gsf"], bad)
    try:
        b"".join(eng.render(bad, eng.probe(bad)[0], 1000))
        raise AssertionError("should fail")
    except EngineError as e:
        assert str(e) == "the emulator rejected the program data", e
    orphan = tmp / "rip" / "orphan.minigsf"
    try:
        b"".join(eng.render(orphan, eng.probe(orphan)[0], 1000))
        raise AssertionError("should fail")
    except EngineError as e:
        assert "gone.gsflib" in str(e)
    reg = Registry({"engines": {"xsfplay_cli": str(fake)}, "meta_dir": str(meta)})
    assert reg.for_path(files["tone.minigsf"]).name == "xsf"
    assert reg.for_path(tmp / "rip" / "tone.gsflib") is None   # libraries are not tracks
    assert reg.diagnostics()["psx_bios"].endswith("hebios.bin")
else:
    print("ffmpeg missing: skipped the render tests")

# --- the real xsfplay, when there is one ---------------------------------------------------------
real = os.environ.get("XSFPLAY") or shutil.which("xsfplay")
if real and shutil.which("ffmpeg"):
    import array
    eng = XsfEngine(44100, cli=real)
    for name in ("tone.gsf", "tone.minigsf", "tone.ssf", "tone.dsf", "tone.2sf"):
        t = eng.probe(files[name])[0]
        pcm = b"".join(eng.render(files[name], t, 44100))
        a = array.array("h")
        a.frombytes(pcm)
        assert len(pcm) == 44100 * 4 and max(map(abs, a)) > 1000, name
    print("real xsfplay: ok")

print("test_xsf: ok")
