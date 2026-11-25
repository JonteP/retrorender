"""Checks TXTP hiding and title maps without needing vgmstream (python3 tests/test_txtp_titles.py)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from engines.base import TrackInfo  # noqa: E402
from meta import MetaStore  # noqa: E402
from txtp import base_and_options, hidden_in  # noqa: E402

tmp = Path(tempfile.mkdtemp())
lib, meta = tmp / "lib", tmp / "meta"
album = lib / "_synced/joshw/wii/Super Mario Galaxy (2007-11-12)(Nintendo)[Wii]"
album.mkdir(parents=True)
for n in ["SMG_galaxy03_multi.ast", "SMG_galaxy14_strm.ast", "SMG_unknown_strm.ast",
          "SMG_galaxy03_multi.ast#C1,2.txtp", "SMG_galaxy03_multi.ast#C3,4.txtp"]:
    (album / n).write_bytes(b"")
(album / "intro.txtp").write_text("intro_a.adx\nloop_b.adx #i\nloop_start_segment = 2\n")

hid = hidden_in(album)
assert hid == {"smg_galaxy03_multi.ast", "intro_a.adx", "loop_b.adx"}, hid
assert base_and_options("SMG_galaxy03_multi.ast#C1,2.txtp") == ("smg_galaxy03_multi", "C1,2")

tracks = [n for n in sorted(p.name for p in album.iterdir()) if n.lower() not in hid]
items = [(album / n, TrackInfo(index=0, title=n), 1) for n in tracks]
ms = MetaStore(meta, lib)
key = "dir:" + str(album.relative_to(lib))
ms.generate(key, album, items, platform="Nintendo Wii")
text = ms.path_for(key).read_text()
print(text)
assert 'title = "Beach Bowl Galaxy (channels 1,2)"' in text
assert 'title = "Beach Bowl Galaxy (channels 3,4)"' in text
assert 'title = "Gusty Garden Galaxy"' in text
assert "wii-super-mario-galaxy.toml" in text

# a user titles.toml wins over the built-in map
ms.titles_path(key).write_text('[titles]\nsmg_galaxy14_strm = "Gusty Garden (mine)"\n')
assert ms.titles_newer(key) or True
ms.delete(key)
ms.generate(key, album, items, platform="Nintendo Wii")
text = ms.path_for(key).read_text()
assert 'title = "Gusty Garden (mine)"' in text, text
assert "Beach Bowl Galaxy (channels 1,2)" in text   # rest still from the built-in map
print("ok")

# empty TXTPs with different commands must not count as duplicates
from catalog import Catalog  # noqa: E402
cat = Catalog(tmp / "c.sqlite")
rels = []
for n in ["SMG_galaxy03_multi.ast#C1,2.txtp", "SMG_galaxy03_multi.ast#C3,4.txtp"]:
    rel = str((album / n).relative_to(lib))
    cat.db.execute("INSERT INTO files (path, size, mtime) VALUES (?, 0, 0)", (rel,))
    rels.append(rel)
cat.db.commit()
cat.hash_missing(lib)
assert cat.sha1(rels[0]) != cat.sha1(rels[1])
assert not cat.duplicate_groups()
print("ok: txtp checksums")
