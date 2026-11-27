# xsfplay

Renders xSF files (PSF, PSF2, SSF, DSF, USF, GSF, 2SF) to WAV on stdout. retrorender runs it
as a separate process (`app/engines/xsf.py`); the length, fade and title come from the tags,
which the Python side reads itself.

| Version byte | Format | Core | License |
| --- | --- | --- | --- |
| 0x01, 0x02 | PSF, PSF2 | [Highly Experimental](https://github.com/kode54/Highly_Experimental) | see its Readme (needs a Sony BIOS) |
| 0x11, 0x12 | SSF, DSF | [Highly Theoretical](https://github.com/kode54/Highly_Theoretical) | GPLv3 |
| 0x21 | USF | [lazyusf2](https://github.com/derselbst/lazyusf2) | GPLv2 |
| 0x22 | GSF | [mGBA](https://github.com/mgba-emu/mgba) 0.10.3 | MPL 2.0 |
| 0x24 | 2SF | DeSmuME from [in_xsf](https://github.com/CyberBotX/in_xsf) | GPLv2+ (DeSmuME) |

All files go through [psflib](https://github.com/kode54/psflib) (MIT). Because of Highly
Theoretical the program as a whole is GPLv3. `shim/` holds two stand-in headers so that
in_xsf's Windows build of DeSmuME compiles on Linux.

`fixtures.py` writes small hand-assembled test programs (GBA, Saturn, Dreamcast, DS) that
play a tone; `smoketest.py` checks their pitch and runs in the Docker build.
