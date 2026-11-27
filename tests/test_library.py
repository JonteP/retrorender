"""Library index, loop policy, the web API's browse/tracks routes, playlists and the Subsonic
playlist methods, with a fake engine so no audio libraries are needed
(python3 tests/test_library.py)."""
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from engines.base import Engine, TrackInfo  # noqa: E402
from library import Library, SEVERAL  # noqa: E402
from meta import MetaStore  # noqa: E402
from playlists import Playlists  # noqa: E402
from policy import BUILTIN, plan, settings_for  # noqa: E402
from subsonic import Subsonic  # noqa: E402
from web import WebUI  # noqa: E402


# --- a fake engine: a .fake file is JSON describing its tracks --------------------------
class FakeEngine(Engine):
    name = "fake"
    extensions = (".fake",)
    insight = "voices"

    def probe(self, path):
        d = json.loads(path.read_text())
        return [TrackInfo(index=i, **t) for i, t in enumerate(d["tracks"])]

    def voices(self, path, track=None):
        return ["Pulse 1", "Pulse 2"]


class FakeRegistry:
    def __init__(self):
        self.engines = [FakeEngine()]

    def engine_hint(self, path):
        return "fake" if path.suffix == ".fake" else None


ENG = FakeEngine()


def probe(p):
    return (ENG, ENG.probe(p)) if p.suffix == ".fake" else (None, [])


tmp = Path(tempfile.mkdtemp())
root, metadir = tmp / "lib", tmp / "meta"


def song(title, author="", **kw):
    return dict(title=title, author=author, **kw)


def put(rel, tracks):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"tracks": tracks}))


# a folder album (one file per track), a subtune file and a second console
put("NES/Mega Man 2/01 Title.fake", [song("Title", "Takashi Tateishi", game="Mega Man 2", loop_s=10, intro_s=2)])
put("NES/Mega Man 2/02 Wily.fake", [song("Dr. Wily Stage 1", "Takashi Tateishi", game="Mega Man 2", loop_s=60, intro_s=5)])
put("NES/Mega Man 2/03 Ending.fake", [song("Ending", "Manami Matsumae", game="Mega Man 2", length_s=90)])
put("NES/Kirby.fake", [song(f"Kirby {i}", "Jun Ishikawa & Hirokazu Ando", game="Kirby's Adventure", length_s=20)
                       for i in range(1, 4)])
# a game with streamed audio and sequences in sub-folders: one album with two parts
put("GameCube/Wind Waker/Stream/sea.fake", [song("Great Sea", game="Wind Waker", length_s=60)])
put("GameCube/Wind Waker/Stream/title.fake", [song("Title", game="Wind Waker", length_s=60)])
put("GameCube/Wind Waker/Seqs/JaiSeqs.fake", [song(f"seq {i}", game="Wind Waker (sequenced)", length_s=30)
                                              for i in range(3)])
put("GameBoy/Tetris/tetris.fake", [song("Type A", "Hirokazu Tanaka", game="Tetris"),
                                   song("Type B", "Hirokazu Tanaka", game="Tetris")])

cfg = {"platforms": {"NES": "Nintendo NES", "GameBoy": "Game Boy", "GameCube": "Nintendo GameCube"},
       "defaults": {"loops": 2, "min_s": 30, "max_s": 600, "default_s": 120, "fade_s": 8},
       "engine": {"fake": {"fade_s": 5}}}
meta = MetaStore(metadir, root)
lib = Library(root, FakeRegistry(), cfg, probe, 44100, meta)
lib.scan()
ix = lib.index


# --- library index ----------------------------------------------------------------------
names = sorted(a.name for a in ix.albums.values())
assert names == ["Kirby's Adventure", "Mega Man 2", "Tetris", "Wind Waker"], names
assert set(ix.genres) == {"Nintendo NES", "Game Boy", "Nintendo GameCube"}, ix.genres
ww = next(a for a in ix.albums.values() if a.name == "Wind Waker")
assert [p.label for p in ww.parts] == ["Streamed", "Sequenced"], ww.parts
assert [(ix.songs[s].disc, ix.songs[s].title) for s in ww.songs] == \
    [(1, "Great Sea"), (1, "Title"), (2, "seq 0"), (2, "seq 1"), (2, "seq 2")]
assert ww.key == "dir:GameCube/Wind Waker" and ww.parts[1].key.startswith("file:")
assert (metadir / "GameCube/Wind Waker/Stream/album.toml").is_file()   # each part keeps its own
assert (metadir / "GameCube/Wind Waker/Seqs/JaiSeqs.fake.album.toml").is_file()
mm2 = next(a for a in ix.albums.values() if a.name == "Mega Man 2")
assert [ix.songs[s].title for s in mm2.songs] == ["Title", "Dr. Wily Stage 1", "Ending"]
# two composers on the album -> both are album artists; each track keeps its own
assert sorted(n for _, n in mm2.artists) == ["Manami Matsumae", "Takashi Tateishi"]
assert ix.songs[mm2.songs[2]].artist == "Manami Matsumae"
kirby = next(a for a in ix.albums.values() if a.name == "Kirby's Adventure")
assert sorted(n for _, n in kirby.artists) == ["Hirokazu Ando", "Jun Ishikawa"], kirby.artists
assert len(kirby.songs) == 3 and kirby.key.startswith("file:")
assert (metadir / "NES/Mega Man 2/album.toml").is_file()
assert (metadir / "NES/Kirby.fake.album.toml").is_file()
assert SEVERAL == "Various composers"

# --- loop policy --------------------------------------------------------------------------
s = settings_for(cfg, "fake", ".fake", {"loops": "3"}, [{"fade_s": 2}])
assert s["loops"] == 3 and s["fade_s"] == 2 and s["max_s"] == 600, s
assert settings_for({}, "x", ".x", {}) == {k: float(v) for k, v in BUILTIN.items()}
p = plan(TrackInfo(0, intro_s=2, loop_s=10), dict(s, loops=2))
assert p.seconds == 30 and "extended to min" in p.reason, p          # 22 s -> min 30 s
p = plan(TrackInfo(0, intro_s=5, loop_s=400), s)
assert p.seconds == 600 and "cut to max" in p.reason, p
assert plan(TrackInfo(0, length_s=90), s).fade_s == 0                 # ends by itself
assert plan(TrackInfo(0), s).seconds == 120                             # [defaults] default_s
assert abs(ix.songs[mm2.songs[1]].duration - 125) < 0.01                # 5 + 2 x 60


# --- web API ------------------------------------------------------------------------------
class Req:
    """Just enough of BaseHTTPRequestHandler for WebUI.handle."""
    def __init__(self, method="GET", body=b"", ctype="application/json"):
        self.command = method
        self.headers = {"Authorization": "Basic dTpw", "Content-Length": str(len(body)),
                        "Content-Type": ctype}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.out = None

    def _json(self, obj, code=200):
        self.out, self.code = obj, code

    def send_response(self, code):
        self.code = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass


pls = Playlists(metadir, True, lambda: lib.index)
web = WebUI({"web": {"user": "u", "password": "p"}}, lib, meta, None, lambda: {}, probe,
            playlists=pls, registry=FakeRegistry())


def get(route, q=None):
    h = Req()
    assert web.handle(h, route, q or {})
    return h.out


def post(route, body):
    h = Req("POST", json.dumps(body).encode())
    assert web.handle(h, route, {})
    return h.out


f = get("/api/facets")
assert [p["name"] for p in f["platforms"]] == ["Game Boy", "Nintendo GameCube", "Nintendo NES"]
assert any(a["name"] == "Takashi Tateishi" for a in f["artists"])

al = get("/api/albums", {"platform": "Nintendo NES", "sort": "name"})["albums"]
assert [a["name"] for a in al] == ["Kirby's Adventure", "Mega Man 2"]

# play all for a composer: only their own tracks, not the whole shared album
matsumae = next(a["id"] for a in f["artists"] if a["name"] == "Manami Matsumae")
t = get("/api/tracks", {"artist": matsumae})
assert [x["title"] for x in t["songs"]] == ["Ending"], t
# play all for a console, an album, and a search (matching tracks first)
assert get("/api/tracks", {"platform": "Nintendo NES"})["total"] == 6
t = get("/api/tracks", {"album": mm2.id})
assert [x["title"] for x in t["songs"]] == ["Title", "Dr. Wily Stage 1", "Ending"]
assert t["songs"][0]["render"].startswith("/render?path=NES/Mega%20Man%202/01%20Title.fake")
assert [x["title"] for x in get("/api/tracks", {"q": "wily"})["songs"]] == ["Dr. Wily Stage 1"]
d = get(f"/api/song/{mm2.songs[0]}/engine")
assert d["insight"] == "voices" and d["voices"] == ["Pulse 1", "Pulse 2"], d
assert get("/api/engines")[0]["engine"] == "fake"

d = get(f"/api/album/{ww.id}")
assert [x["label"] for x in d["discs"]] == ["Streamed", "Sequenced"]
assert d["parts"][1]["toml_path"].endswith("Seqs/JaiSeqs.fake.album.toml")
assert "jaiseqs#0" in d["parts"][1]["titles_template"]
h = Req()
web.handle(h, f"/api/album/{ww.id}/tags.m3u", {"part": "0"})
assert b"sea.fake" in h.wfile.getvalue(), h.out
h = Req("POST", json.dumps({"title": "Dragon Roost"}).encode())
web.handle(h, f"/api/song/{ww.songs[3]}/title", {})
assert '"jaiseqs#1" = "Dragon Roost"' in meta.titles_path(ww.parts[1].key).read_text()

# --- covers: the image you chose is kept to crop from; removing the cover keeps it -------
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 40
JPG = b"\xff\xd8\xff" + b"\0" * 40
for body, q in ((PNG, {"source": "1"}), (JPG, {})):
    h = Req("POST", body, "image/png")
    web.handle(h, f"/api/album/{ww.id}/cover", q)
    assert h.out == {"ok": True}, h.out
wdir = metadir / "GameCube/Wind Waker"
assert sorted(p.name for p in wdir.glob("cover*")) == ["cover.jpg", "cover.source.png"]
assert get(f"/api/album/{ww.id}")["cover_source"] and ww.cover.endswith("cover.jpg")
h = Req("POST"); web.handle(h, f"/api/album/{ww.id}/cover/delete", {})
assert sorted(p.name for p in wdir.glob("cover*")) == ["cover.source.png"]
h = Req("POST"); web.handle(h, f"/api/album/{ww.id}/cover/delete", {"all": "1"})
assert not list(wdir.glob("cover*"))
lib.scan()
ix = lib.index
ww = next(a for a in ix.albums.values() if a.name == "Wind Waker")

# --- renaming one track: titles.toml + album.toml, which stays untouched -------------------
from meta import gen_status  # noqa: E402
h = Req("POST", json.dumps({"title": "Boss Theme"}).encode())
assert web.handle(h, f"/api/song/{kirby.songs[1]}/title", {}) and h.out["ok"], h.out
assert '"kirby#1" = "Boss Theme"' in meta.titles_path(kirby.key).read_text()
text = meta.path_for(kirby.key).read_text()
assert 'title = "Boss Theme"' in text and gen_status(text)[1], text   # still untouched
h = Req("POST", json.dumps({"title": "Wily 1"}).encode())
web.handle(h, f"/api/song/{mm2.songs[1]}/title", {})
assert '"02 wily" = "Wily 1"' in meta.titles_path(mm2.key).read_text()
meta.delete(mm2.key)                       # a regenerated file keeps the new title
lib.scan()
assert [lib.index.songs[s].title for s in mm2.songs][1] == "Wily 1"
assert lib.index.songs[kirby.songs[1]].title == "Boss Theme"
ix = lib.index

# --- playlists ----------------------------------------------------------------------------
pl = post("/api/playlists", {"name": "Bosses", "songs": [mm2.songs[1], kirby.songs[0]]})
assert pl["songs"] == 2 and pl["name"] == "Bosses"
pid = pl["id"]
out = post(f"/api/playlist/{pid}", {"add": [mm2.songs[0]], "name": "Boss themes"})
assert out["name"] == "Boss themes" and [x["title"] for x in out["tracks"]] == \
    ["Wily 1", "Kirby 1", "Title"]
out = post(f"/api/playlist/{pid}", {"songs": [mm2.songs[0], mm2.songs[1]]})
assert [x["title"] for x in out["tracks"]] == ["Title", "Wily 1"]
assert get("/api/tracks", {"playlist": pid})["total"] == 2
assert get("/api/playlists")["playlists"][0]["name"] == "Boss themes"
assert get("/api/playlist/pl-nothere")["error"] == "no such playlist"
assert get("/api/playlist/../../etc")["error"] == "no such playlist"   # no path tricks

# an entry whose song id changed is found again by path + subtrack; one that is gone stays
# in the file but is not shown, and positions count only the songs that exist
raw = json.loads((metadir / "_playlists" / f"{pid}.json").read_text())
raw["songs"][0]["id"] = "tr-0000000000000000"
raw["songs"].insert(1, {"id": "tr-gone", "path": "NES/gone.fake", "sub": 0})
(metadir / "_playlists" / f"{pid}.json").write_text(json.dumps(raw))
out = get(f"/api/playlist/{pid}")
assert [x["title"] for x in out["tracks"]] == ["Title", "Wily 1"] and out["missing"] == 1
pls.update(pid, remove_at=[1])                     # "Wily 1", not the gone entry
raw = json.loads((metadir / "_playlists" / f"{pid}.json").read_text())
assert [e["path"] for e in raw["songs"]] == ["NES/Mega Man 2/01 Title.fake", "NES/gone.fake"], raw

# --- Subsonic sees the same playlists ---------------------------------------------------
sub = Subsonic(lib, {"subsonic": {"user": "u", "password": "p"}})
sub.playlists = pls


def rest(method, **q):
    h = Req()
    q.update(u="u", p="p")
    sub.handle(h, method, q)
    return json.loads(h.wfile.getvalue())["subsonic-response"]


r = rest("getPlaylists")
assert r["status"] == "ok" and r["playlists"]["playlist"][0]["songCount"] == 1, r
r = rest("createPlaylist", name="From an app", **{"songId[]": [kirby.songs[1], kirby.songs[2]]})
new = r["playlist"]["id"]
assert [e["title"] for e in r["playlist"]["entry"]] == ["Boss Theme", "Kirby 3"]
rest("updatePlaylist", playlistId=new, songIdToAdd=mm2.songs[2], **{"songIndexToRemove[]": ["0"]})
assert [e["title"] for e in rest("getPlaylist", id=new)["playlist"]["entry"]] == ["Kirby 3", "Ending"]
rest("deletePlaylist", id=new)
assert rest("getPlaylist", id=new)["status"] == "failed"
assert rest("getUser")["user"]["playlistRole"] is True
al = rest("getAlbum", id=ww.id)["album"]
assert [d["title"] for d in al["discTitles"]] == ["Streamed", "Sequenced"]
assert [x["discNumber"] for x in al["song"]] == [1, 1, 2, 2, 2]

# --- covers from libretro-thumbnails (no network: a fake GitHub) --------------------------
import struct  # noqa: E402
import zlib  # noqa: E402
from covers import CoverFetcher, Libretro, ShapeCache, norm  # noqa: E402
from settings import Settings  # noqa: E402


def png(w, h):
    raw = b"".join(b"\0" + bytes([200, 60, 40]) * w for _ in range(h))
    c = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + c(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + c(b"IDAT", zlib.compress(raw)) + c(b"IEND", b""))


TREE = ["Named_Boxarts/Tetris (Japan).png", "Named_Boxarts/Tetris (World) (Rev 1).png",
        "Named_Boxarts/Tetris (World) (Beta).png", "Named_Titles/Tetris (World) (Rev 1).png",
        "Named_Boxarts/Kirby's Adventure (USA).png",
        "Named_Boxarts/Mega Man 2 - Dr. Wily no Nazo (Japan).png"]
calls = []


def fake_http(url, timeout=30):
    calls.append(url)
    if url.startswith("https://api.github.com/repos/libretro-thumbnails/Nintendo_-_Game_Boy/"):
        return json.dumps({"tree": [{"path": p} for p in TREE]}).encode()
    if url.startswith("https://api.github.com/repos/libretro-thumbnails/Nintendo_-_Nintendo_Entertainment_System/"):
        return json.dumps({"tree": [{"path": p} for p in TREE[4:]]}).encode()
    if url.startswith("https://raw.githubusercontent.com/"):
        return png(40, 60)
    raise OSError("offline: " + url)


assert norm("The Legend of Zelda: The Wind Waker") == norm("Legend of Zelda, The - The Wind Waker (USA)")
settings = Settings(metadir, True)
settings.update("covers", {"libretro_systems": {"Game Boy": "Nintendo - Game Boy"}})
lr = Libretro(None, fake_http)
c = settings.covers()
assert [h["name"] for h in lr.search("Game Boy", ["Tetris"], "boxart", c)] == ["Tetris (World) (Rev 1)"]
assert [h["name"] for h in lr.search("Game Boy", ["Tetris"], "title", c)] == ["Tetris (World) (Rev 1)"]
assert lr.search("Nintendo NES", ["Mega Man 2"], "boxart", c)[0]["name"].startswith("Mega Man 2 - Dr. Wily")
assert not lr.known("Nintendo - Game Boy", "Named_Boxarts/../../etc/passwd")

lib.settings = settings
lib.shaper = ShapeCache(metadir / "_cache" / "covers")
fetcher = CoverFetcher(lib, settings, fake_http)
fetcher.run_once()
tetris = next(a for a in lib.index.albums.values() if a.name == "Tetris")
kirby = next(a for a in lib.index.albums.values() if a.name == "Kirby's Adventure")
assert tetris.cover_origin == "libretro" and kirby.cover_origin == "libretro", (tetris.covers, kirby.covers)
assert fetcher.auto_info(tetris.key)["name"] == "Tetris (World) (Rev 1)"
assert fetcher.status["found"] >= 2
n = len(calls)
fetcher.run_once()                       # nothing new to do: found ones and misses are kept
assert len(calls) == n, calls[n:]
settings.update("covers", {"libretro_type": "title"})
fetcher.run_once()                       # another image type: fetched again
assert fetcher.auto_info(tetris.key)["type"] == "title"
settings.update("covers", {"order": ["rip", "libretro"], "enabled": ["rip"]})
lib.apply_covers(tetris)
assert tetris.cover is None              # libretro switched off
settings.update("covers", {"enabled": ["rip", "libretro"], "shape": "pad"})
lib.apply_covers(tetris)
try:
    import PIL  # noqa: F401
    data, ctype, _ = lib.cover_image(tetris)
    from PIL import Image
    assert ctype == "image/jpeg" and Image.open(io.BytesIO(data)).size == (60, 60)
except ImportError:
    print("(Pillow missing: square covers not tested)")
assert lib.cover_image(tetris, original=True)[1] == "image/png"      # the editor gets it as it is
lib.settings = lib.shaper = None

print("library, policy, web API, playlists, Subsonic and covers: ok")
