"""Builds the fixture files and checks that xsfplay turns each into the right tone.

    python3 smoketest.py path/to/xsfplay

Exits non-zero if a file gives no sound, the wrong pitch or an error. Runs in the Docker
build, so a broken core never reaches the image. Standard library only.
"""
from __future__ import annotations

import array
import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

EXPECT = {"tone.gsf": 440, "tone.minigsf": 880, "tone.ssf": 441, "tone.dsf": 441, "tone.2sf": 440,
          "tone.psf": 787.5, "tone.minipsf": 1575}


def pitch(samples: array.array, rate: int) -> float:
    """Frequency from zero crossings of the mono mix (enough for a square wave)."""
    mono = [samples[i] + samples[i + 1] for i in range(0, len(samples) - 1, 2)]
    mono = mono[len(mono) // 4:]
    mean = sum(mono) / len(mono)
    ups = [i for i in range(1, len(mono)) if mono[i - 1] - mean < 0 <= mono[i] - mean]
    if len(ups) < 3:
        return 0.0
    return (len(ups) - 1) * rate / (ups[-1] - ups[0])


def main() -> int:
    cli = sys.argv[1] if len(sys.argv) > 1 else "xsfplay"
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        files = fixtures.write_all(Path(tmp))
        for name, want in EXPECT.items():
            extra = ["-b", str(files["test-hebios.bin"])] if "psf" in name else []
            r = subprocess.run([cli, "-n", "44100", *extra, str(files[name])], capture_output=True,
                               timeout=120)
            data = r.stdout
            if r.returncode != 0 or len(data) <= 44:
                print(f"FAIL {name}: {r.stderr.decode(errors='replace').strip()}")
                bad += 1
                continue
            rate = struct.unpack("<I", data[24:28])[0]
            s = array.array("h")
            s.frombytes(data[44:44 + (len(data) - 44) // 4 * 4])
            peak = max(abs(v) for v in s)
            f = pitch(s, rate)
            ok = peak > 1000 and math.isclose(f, want, rel_tol=0.03)
            print(f"{'ok  ' if ok else 'FAIL'} {name}: rate {rate}, peak {peak}, {f:.1f} Hz (want {want})")
            bad += not ok
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
