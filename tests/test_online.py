"""Online libraries: catalog reading, filters, downloads into the cache, pinning and
eviction, without network (python3 tests/test_online.py)."""
import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from catalog import Catalog  # noqa: E402
from online import Online, parse_archive_name, size_bytes  # noqa: E402
from settings import Settings  # noqa: E402

p = parse_archive_name("Mega Man 2 [Rockman 2] (1988-12-24)(Capcom)[NES].7z")
assert (p["title"], p["alt_title"], p["date"], p["developer"]) == ("Mega Man 2", "Rockman 2", "1988-12-24", "Capcom")
assert size_bytes("31K") == 31 * 1024 and size_bytes("1.5M") == int(1.5 * 1024 ** 2)

tmp = Path(tempfile.mkdtemp())
lib, meta = tmp / "lib", tmp / "meta"
lib.mkdir(), meta.mkdir()
SITE = {
    "https://nsf.joshw.info/": '<a href="s/">s/</a> <a href="m/">m/</a>',
    "https://nsf.joshw.info/s/":
        '<a href="Super%20Mario%20Bros.%203%20(1988-10-23)(Nintendo)(Nintendo)%5BNES%5D.zip">x</a> 2020-01-01 10:00  31K\n'
        '<a href="Solstice%20(1990)(Software%20Creations)(CSG)%5BNES%5D.zip">x</a> 2020-01-01 10:00  20K\n',
    "https://nsf.joshw.info/m/":
        '<a href="Mega%20Man%202%20%5BRockman%202%5D%20(1988-12-24)(Capcom)%5BNES%5D.zip">x</a> 2020-01-01 10:00  40K\n',
}


def zipped(name, size):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, b"N" * size)
    return buf.getvalue()


def fake_get(url, dest=None):
    if dest is None:
        if url in SITE:
            return SITE[url]
        raise OSError("404 " + url)
    dest.write_bytes(zipped(url.rsplit("/", 1)[-1].split("%20")[0] + ".nsf", 600_000))


changes = []
settings = Settings(meta, True)
cat = Catalog(meta / "catalog.sqlite")
o = Online({"joshw": {"enabled": False, "delay_s": 0}}, cat, lib, on_change=lambda: changes.append(1),
           settings=settings, worker=False)
o._http = fake_get
assert not o.enabled and o.active() == []
settings.update("online", {"sources": {"joshw": {"enabled": True, "systems": ["nsf"]}},
                           "temp_limit_gb": 0.001})      # ~1 MB: room for one download
assert o.enabled and o.active() == [("joshw", "nsf")]
# saved as real TOML tables (0.16.0 wrote the sources as a string) and read back
assert Settings(meta, True).get("online")["sources"]["joshw"] == {"enabled": True, "systems": ["nsf"]}
assert "[online.sources.joshw]" in (meta / "_settings.toml").read_text()
o._refresh("joshw", "nsf", force=True)
total, rows = o.search()
assert total == 3, rows
assert o.search(q="rockman")[1][0]["title"] == "Mega Man 2"
assert o.search(platform="Nintendo NES", year="1980s")[0] == 2
assert o.search(platform="Sega Mega Drive")[0] == 0
assert o.counts()["platforms"] == {"Nintendo NES": 3}

smb3 = o.search(q="Mario")[1][0]["id"]
mm2 = o.search(q="Mega")[1][0]["id"]
o.fetch(smb3, pin=False)
o._fetch(*o.fetches.get()[1:])                # run the queued job here
r = o.get(smb3)
assert r["state"] == "temporary" and (lib / r["local_dir"] / ".source.json").is_file(), r
assert changes and o.entry_for(r["local_dir"] + "/Super.nsf") == {"id": smb3, "state": "temporary"}
assert o.search(not_local=True)[0] == 2
time.sleep(0.01)
o.fetch(mm2, pin=False)
o._fetch(*o.fetches.get()[1:])
# the cache holds ~1 MB: the older download is removed, the new one stays
assert o.get(smb3)["state"] == "remote" and not (lib / r["local_dir"]).exists()
assert o.get(mm2)["state"] == "temporary"
o.fetch(mm2, pin=True)                      # "Add to library" on a cached one: kept
assert o.get(mm2)["state"] == "pinned"
o.fetch(smb3, pin=False)
o._fetch(*o.fetches.get()[1:])
assert o.get(mm2)["state"] == "pinned"      # pinned ones are never removed
o.played(o.get(smb3)["local_dir"] + "/x.nsf")
assert o.get(smb3)["last_played"] > time.time() - 5
o.remove(smb3)
assert o.get(smb3)["state"] == "remote"
assert [d.name for d, _ in o.synced_dirs()] == ["nsf"]
# a download asked for while a long list is read goes first, between its pages
order = []
real_fetch = o._fetch
o._fetch = lambda rid, pin: (order.append("download"), real_fetch(rid, pin))
o._http = lambda url, dest=None: (order.append("page" if dest is None else "file"), fake_get(url, dest))[1]
o.fetch(o.search(q="Solstice")[1][0]["id"], pin=False)
o._refresh("joshw", "nsf", force=True)
assert order[:3] == ["download", "file", "page"], order
assert o.get(o.search(q="Solstice")[1][0]["id"])["state"] == "temporary"
# a download that was queued when the app stopped is not stuck after a restart
rid = o.search(q="Mega")[1][0]["id"]
o.remove(rid)
o._exec("UPDATE remote SET state='queued' WHERE id=?", (rid,))
o2 = Online({"joshw": {"delay_s": 0}}, cat, lib, on_change=lambda: None, settings=settings, worker=False)
assert o2.get(rid)["state"] == "remote"
# systems whose rips no engine here plays are marked, and left out of the library list
o.playable_exts = {".nsf", ".vgm"}
assert o.playable("nsf") and not o.playable("usf") and o.playable("wii")
settings.update("online", {"sources": {"joshw": {"enabled": True, "systems": ["nsf", "usf"]}}})
assert o.describe_sources()["joshw"]["unplayable"]["usf"] == [".usf", ".miniusf"]
assert o.search(playable_only=True)[0] == o.search(system="nsf")[0]
print("online libraries: ok")
