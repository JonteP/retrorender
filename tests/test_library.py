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
put("GameBoy/Tetris/tetris.fake", [song("Type A", "Hirokazu Tanaka", game="Tetris"),
                                   song("Type B", "Hirokazu Tanaka", game="Tetris")])

cfg = {"platforms": {"NES": "Nintendo NES", "GameBoy": "Game Boy"},
       "defaults": {"loops": 2, "min_s": 30, "max_s": 600, "default_s": 120, "fade_s": 8},
       "engine": {"fake": {"fade_s": 5}}}
meta = MetaStore(metadir, root)
lib = Library(root, FakeRegistry(), cfg, probe, 44100, meta)
lib.scan()
ix = lib.index


# --- library index ----------------------------------------------------------------------
names = sorted(a.name for a in ix.albums.values())
assert names == ["Kirby's Adventure", "Mega Man 2", "Tetris"], names
assert set(ix.genres) == {"Nintendo NES", "Game Boy"}, ix.genres
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
assert [p["name"] for p in f["platforms"]] == ["Game Boy", "Nintendo NES"]
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

# --- playlists ----------------------------------------------------------------------------
pl = post("/api/playlists", {"name": "Bosses", "songs": [mm2.songs[1], kirby.songs[0]]})
assert pl["songs"] == 2 and pl["name"] == "Bosses"
pid = pl["id"]
out = post(f"/api/playlist/{pid}", {"add": [mm2.songs[0]], "name": "Boss themes"})
assert out["name"] == "Boss themes" and [x["title"] for x in out["tracks"]] == \
    ["Dr. Wily Stage 1", "Kirby 1", "Title"]
out = post(f"/api/playlist/{pid}", {"songs": [mm2.songs[0], mm2.songs[1]]})
assert [x["title"] for x in out["tracks"]] == ["Title", "Dr. Wily Stage 1"]
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
assert [x["title"] for x in out["tracks"]] == ["Title", "Dr. Wily Stage 1"] and out["missing"] == 1
pls.update(pid, remove_at=[1])                     # "Dr. Wily Stage 1", not the gone entry
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
assert [e["title"] for e in r["playlist"]["entry"]] == ["Kirby 2", "Kirby 3"]
rest("updatePlaylist", playlistId=new, songIdToAdd=mm2.songs[2], **{"songIndexToRemove[]": ["0"]})
assert [e["title"] for e in rest("getPlaylist", id=new)["playlist"]["entry"]] == ["Kirby 3", "Ending"]
rest("deletePlaylist", id=new)
assert rest("getPlaylist", id=new)["status"] == "failed"
assert rest("getUser")["user"]["playlistRole"] is True

print("library, policy, web API, playlists and Subsonic: ok")
