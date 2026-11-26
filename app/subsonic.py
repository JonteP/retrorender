"""Minimal OpenSubsonic server (JSON) on top of the library index.

Written for Music Assistant's OpenSubsonic provider; works with other Subsonic apps.
Unknown methods answer "ok" with empty content and are logged, because MA gives up
when a method answers "unsupported".
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path

log = logging.getLogger("subsonic")

API_VERSION = "1.16.1"
SERVER_VERSION = "0.13"


class SubsonicError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _base(status="ok") -> dict:
    return {"status": status, "version": API_VERSION, "type": "retrorender",
            "serverVersion": SERVER_VERSION, "openSubsonic": True}


class Subsonic:
    def __init__(self, library, cfg: dict):
        self.lib = library
        sc = cfg.get("subsonic", {})
        self.user = str(sc.get("user", "musicassistant"))
        self.password = str(sc.get("password", "retro"))
        self.on_star = lambda rel: None      # replaced by the server (pins downloads)
        self.playlists = None                # set by the server (playlists.Playlists)

    # --- inramning ------------------------------------------------------------
    def handle(self, handler, method: str, q: dict):
        """Called from the HTTP server. Returns None if the response was already sent (stream)."""
        method = method.removesuffix(".view")
        try:
            self._auth(q)
            fn = getattr(self, "m_" + method, None)
            if fn is None:
                log.info("unknown Subsonic method %s, answering empty", method)
                return self._send(handler, {})
            out = fn(q, handler)
            if out is None:
                return None
            return self._send(handler, out)
        except SubsonicError as e:
            body = _base("failed")
            body["error"] = {"code": e.code, "message": str(e)}
            return self._raw(handler, {"subsonic-response": body})

    def _send(self, handler, payload: dict):
        body = _base()
        body.update(payload)
        return self._raw(handler, {"subsonic-response": body})

    @staticmethod
    def _raw(handler, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _auth(self, q):
        if q.get("u") != self.user:
            raise SubsonicError(40, "wrong username or password")
        if "t" in q and "s" in q:
            ok = hashlib.md5((self.password + q["s"]).encode()).hexdigest() == q["t"].lower()
        elif "p" in q:
            p = q["p"]
            if p.startswith("enc:"):
                p = bytes.fromhex(p[4:]).decode("utf-8", "replace")
            ok = p == self.password
        else:
            raise SubsonicError(10, "credentials missing")
        if not ok:
            raise SubsonicError(40, "wrong username or password")

    # --- omvandling till Subsonic-objekt ------------------------------------
    @property
    def ix(self):
        return self.lib.index

    def _song(self, s) -> dict:
        al = self.ix.albums.get(s.album_id)
        arts = [{"id": i, "name": n} for i, n in s.artists]
        alarts = [{"id": i, "name": n} for i, n in (al.artists if al else s.artists)]
        return _clean({
            "id": s.id, "parent": s.album_id, "isDir": False, "title": s.title,
            "album": al.name if al else "", "artist": s.artist, "track": s.track,
            "discNumber": 1, "coverArt": s.album_id if al and al.cover else None,
            "size": s.size, "contentType": "audio/wav", "suffix": "wav",
            "duration": int(round(s.duration)), "bitRate": 1411, "samplingRate": 44100,
            "channelCount": 2, "bitDepth": 16, "path": s.rel, "isVideo": False,
            "albumId": s.album_id, "artistId": s.artist_id, "type": "music",
            "mediaType": "song", "created": s.created, "playCount": 0,
            "year": al.year if al else None,
            "genre": s.platform, "genres": [{"name": s.platform}],
            "artists": arts, "displayArtist": s.artist,
            "albumArtists": alarts, "displayAlbumArtist": al.artist if al else s.artist,
            "displayComposer": s.artist,
            "contributors": [{"role": "composer", "artist": a} for a in arts],
            "comment": s.engine, "sortName": "", "musicBrainzId": "", "bpm": 0,
            "moods": [], "replayGain": {},
        })

    def _album(self, a, with_songs=False) -> dict:
        songs = [self.ix.songs[i] for i in a.songs if i in self.ix.songs]
        arts = [{"id": i, "name": n} for i, n in a.artists]
        d = _clean({
            "id": a.id, "name": a.name, "title": a.name, "album": a.name,
            "artist": a.artist, "artistId": a.artist_id, "isDir": True,
            "parent": a.artist_id, "coverArt": a.id if a.cover else None,
            "songCount": len(songs), "duration": int(sum(s.duration for s in songs)),
            "created": a.created, "year": a.year,
            "genre": a.platform, "genres": [{"name": a.platform}],
            "artists": arts, "displayArtist": a.artist,
            "playCount": 0, "musicBrainzId": "", "sortName": "", "recordLabels": [],
            "releaseTypes": ["album"], "moods": [], "isCompilation": False,
            "discTitles": [], "userRating": 0,
        })
        if with_songs:
            d["song"] = [self._song(s) for s in songs]
        return d

    def _artist(self, ar, with_albums=False) -> dict:
        d = {"id": ar.id, "name": ar.name, "albumCount": len(ar.albums),
             "musicBrainzId": "", "sortName": "", "roles": ["albumartist", "composer"]}
        if with_albums:
            d["album"] = [self._album(self.ix.albums[i]) for i in ar.albums
                          if i in self.ix.albums]
        return d

    def _get(self, table: str, q) -> object:
        obj = getattr(self.ix, table).get(q.get("id", ""))
        if obj is None:
            raise SubsonicError(70, "not found")
        return obj

    @staticmethod
    def _int(q, k, default):
        try:
            return int(q.get(k, default))
        except ValueError:
            return default

    # --- system ---------------------------------------------------------------
    def m_ping(self, q, h):
        return {}

    def m_getLicense(self, q, h):
        return {"license": {"valid": True}}

    def m_getOpenSubsonicExtensions(self, q, h):
        return {"openSubsonicExtensions": []}

    def m_getUser(self, q, h):
        roles = {k: True for k in ("streamRole", "downloadRole", "scrobblingEnabled",
                                   "playlistRole")}
        roles.update({k: False for k in ("adminRole", "settingsRole", "uploadRole",
                                         "coverArtRole", "commentRole",
                                         "podcastRole", "jukeboxRole", "shareRole",
                                         "videoConversionRole")})
        return {"user": {"username": self.user, "folder": [1], **roles}}

    def m_getScanStatus(self, q, h):
        return {"scanStatus": {"scanning": self.lib.scanning, "count": self.lib.count
                               if self.lib.scanning else len(self.ix.songs)}}

    def m_startScan(self, q, h):
        import threading
        threading.Thread(target=self.lib.scan, daemon=True).start()
        return {"scanStatus": {"scanning": True, "count": 0}}

    # --- browsing ---------------------------------------------------------------
    def m_getMusicFolders(self, q, h):
        return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Retro"}]}}

    def _index(self):
        groups: dict[str, list] = {}
        for ar in sorted(self.ix.artists.values(), key=lambda a: a.name.lower()):
            letter = ar.name[:1].upper() if ar.name[:1].isalpha() else "#"
            groups.setdefault(letter, []).append(self._artist(ar))
        return [{"name": k, "artist": v} for k, v in sorted(groups.items())]

    def m_getArtists(self, q, h):
        return {"artists": {"ignoredArticles": "", "index": self._index()}}

    def m_getIndexes(self, q, h):
        return {"indexes": {"ignoredArticles": "", "lastModified": int(self.ix.built * 1000),
                            "index": self._index()}}

    def m_getArtist(self, q, h):
        return {"artist": self._artist(self._get("artists", q), with_albums=True)}

    def m_getAlbum(self, q, h):
        return {"album": self._album(self._get("albums", q), with_songs=True)}

    def m_getSong(self, q, h):
        return {"song": self._song(self._get("songs", q))}

    def m_getMusicDirectory(self, q, h):
        i = q.get("id", "")
        if i in self.ix.artists:
            ar = self.ix.artists[i]
            return {"directory": {"id": ar.id, "name": ar.name, "parent": "1",
                                  "child": [self._album(self.ix.albums[x]) for x in ar.albums]}}
        if i in self.ix.albums:
            a = self._album(self.ix.albums[i], with_songs=True)
            return {"directory": {"id": a["id"], "name": a["name"], "parent": a["parent"],
                                  "child": a.pop("song")}}
        raise SubsonicError(70, "not found")

    def m_getAlbumList2(self, q, h):
        size = min(self._int(q, "size", 10), 500)
        off = self._int(q, "offset", 0)
        albums = list(self.ix.albums.values())
        kind = q.get("type", "alphabeticalByName")
        if kind == "random":
            random.shuffle(albums)
        elif kind == "newest":
            albums.sort(key=lambda a: a.created, reverse=True)
        elif kind == "alphabeticalByArtist":
            albums.sort(key=lambda a: (a.artist.lower(), a.name.lower()))
        elif kind == "byGenre":
            ids = self.ix.genres.get(q.get("genre", ""), [])
            albums = sorted((self.ix.albums[i] for i in ids if i in self.ix.albums),
                            key=lambda a: a.name.lower())
        elif kind == "byYear":
            lo, hi = self._int(q, "fromYear", 0), self._int(q, "toYear", 9999)
            a0, a1 = min(lo, hi), max(lo, hi)
            albums = sorted((a for a in albums if a.year and a0 <= a.year <= a1),
                            key=lambda a: a.year, reverse=lo > hi)
        elif kind in ("starred", "frequent", "recent", "highest"):
            albums = []
        else:
            albums.sort(key=lambda a: a.name.lower())
        return {"albumList2": {"album": [self._album(a) for a in albums[off:off + size]]}}

    m_getAlbumList = m_getAlbumList2

    def m_getRandomSongs(self, q, h):
        songs = list(self.ix.songs.values())
        random.shuffle(songs)
        return {"randomSongs": {"song": [self._song(s) for s in
                                         songs[:min(self._int(q, "size", 10), 500)]]}}

    def m_search3(self, q, h):
        term = q.get("query", "").strip().strip('"').lower()

        def page(items, key, prefix):
            hits = [x for x in items if not term or term in key(x).lower()]
            off = self._int(q, prefix + "Offset", 0)
            return hits[off:off + self._int(q, prefix + "Count", 20)]

        ars = page(sorted(self.ix.artists.values(), key=lambda a: a.name.lower()),
                   lambda a: a.name, "artist")
        als = page(sorted(self.ix.albums.values(), key=lambda a: a.name.lower()),
                   lambda a: f"{a.name} {a.artist} {a.platform}", "album")
        sos = page(sorted(self.ix.songs.values(), key=lambda s: (s.album_id, s.track)),
                   lambda s: f"{s.title} {s.artist} {s.platform}", "song")
        return {"searchResult3": {"artist": [self._artist(a) for a in ars],
                                  "album": [self._album(a) for a in als],
                                  "song": [self._song(s) for s in sos]}}

    m_search2 = m_search3

    # --- empty answers for what we do not have ---------------------------------
    def m_getArtistInfo2(self, q, h):
        return {"artistInfo2": {"biography": "", "musicBrainzId": "", "similarArtist": []}}

    m_getArtistInfo = m_getArtistInfo2

    def m_getAlbumInfo2(self, q, h):
        return {"albumInfo": {"notes": "", "musicBrainzId": ""}}

    m_getAlbumInfo = m_getAlbumInfo2

    def m_getSimilarSongs2(self, q, h):
        return {"similarSongs2": {"song": []}}

    def m_getSimilarSongs(self, q, h):
        return {"similarSongs": {"song": []}}

    def m_getTopSongs(self, q, h):
        return {"topSongs": {"song": []}}

    def m_getStarred2(self, q, h):
        return {"starred2": {"artist": [], "album": [], "song": []}}

    def m_getStarred(self, q, h):
        return {"starred": {"artist": [], "album": [], "song": []}}

    # --- playlists (the same ones as in the web UI) -----------------------------------
    def _playlist(self, pl, with_songs=False) -> dict:
        songs = self.playlists.resolve(pl["songs"])
        cover = next((s.album_id for s in songs
                      if self.ix.albums.get(s.album_id) and self.ix.albums[s.album_id].cover), None)
        d = _clean({"id": pl["id"], "name": pl["name"], "comment": pl.get("comment") or None,
                    "owner": self.user, "public": False, "songCount": len(songs),
                    "duration": int(sum(s.duration for s in songs)),
                    "created": pl.get("created"), "changed": pl.get("changed"),
                    "coverArt": cover})
        if with_songs:
            d["entry"] = [self._song(s) for s in songs]
        return d

    def _pl(self, pid):
        if self.playlists is None:
            raise SubsonicError(70, "no such playlist")
        try:
            return self.playlists.get(pid)
        except KeyError:
            raise SubsonicError(70, "no such playlist")

    @staticmethod
    def _all(q, key) -> list:
        """Repeated parameters (songId=…&songId=…) arrive as a list from the server."""
        v = q.get(key + "[]", q.get(key))
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    def m_getPlaylists(self, q, h):
        pls = self.playlists.list() if self.playlists else []
        return {"playlists": {"playlist": [self._playlist(p) for p in pls]}}

    def m_getPlaylist(self, q, h):
        return {"playlist": self._playlist(self._pl(q.get("id", "")), with_songs=True)}

    def m_createPlaylist(self, q, h):
        if self.playlists is None or not self.playlists.writable:
            raise SubsonicError(50, "playlists are read-only")
        songs = self._all(q, "songId")
        try:
            if q.get("playlistId"):
                pl = self.playlists.update(q["playlistId"], song_ids=songs)
            else:
                pl = self.playlists.create(q.get("name", ""), songs)
        except KeyError:
            raise SubsonicError(70, "no such playlist")
        return {"playlist": self._playlist(pl, with_songs=True)}

    def m_updatePlaylist(self, q, h):
        if self.playlists is None or not self.playlists.writable:
            raise SubsonicError(50, "playlists are read-only")
        try:
            self.playlists.update(q.get("playlistId", ""), name=q.get("name"),
                                  comment=q.get("comment"),
                                  add=self._all(q, "songIdToAdd"),
                                  remove_at=[int(i) for i in self._all(q, "songIndexToRemove")])
        except KeyError:
            raise SubsonicError(70, "no such playlist")
        return {}

    def m_deletePlaylist(self, q, h):
        if self.playlists is None or not self.playlists.writable:
            raise SubsonicError(50, "playlists are read-only")
        try:
            self.playlists.delete(q.get("id", ""))
        except KeyError:
            raise SubsonicError(70, "no such playlist")
        return {}

    def m_getPodcasts(self, q, h):
        return {"podcasts": {"channel": []}}

    def m_getNewestPodcasts(self, q, h):
        return {"newestPodcasts": {"episode": []}}

    def m_getGenres(self, q, h):
        out = []
        for g, ids in sorted(self.ix.genres.items(), key=lambda x: x[0].lower()):
            als = [self.ix.albums[i] for i in ids if i in self.ix.albums]
            out.append({"value": g, "songCount": sum(len(a.songs) for a in als),
                        "albumCount": len(als)})
        return {"genres": {"genre": out}}

    def m_getSongsByGenre(self, q, h):
        ids = self.ix.genres.get(q.get("genre", ""), [])
        songs = [self.ix.songs[s] for a in ids if a in self.ix.albums
                 for s in self.ix.albums[a].songs if s in self.ix.songs]
        off, cnt = self._int(q, "offset", 0), min(self._int(q, "count", 10), 500)
        return {"songsByGenre": {"song": [self._song(s) for s in songs[off:off + cnt]]}}

    def m_getInternetRadioStations(self, q, h):
        return {"internetRadioStations": {"internetRadioStation": []}}

    def m_getLyrics(self, q, h):
        return {"lyrics": {"value": ""}}

    def m_getLyricsBySongId(self, q, h):
        return {"lyricsList": {"structuredLyrics": []}}

    def m_getBookmarks(self, q, h):
        return {"bookmarks": {"bookmark": []}}

    def m_getPlayQueue(self, q, h):
        return {}

    def m_star(self, q, h):
        """A favourite in MA pins temporary downloads, so they are never evicted."""
        ids = [v for k, v in q.items() if k in ("id", "albumId", "artistId")]
        for i in ids:
            if i in self.ix.songs:
                self.on_star(self.ix.songs[i].rel)
            elif i in self.ix.albums:
                for sid in self.ix.albums[i].songs:
                    if sid in self.ix.songs:
                        self.on_star(self.ix.songs[sid].rel)
        return {}

    # scrobble, unstar, setRating, savePlayQueue etc. end up as "unknown method" = ok

    # --- media --------------------------------------------------------------------
    def m_stream(self, q, h):
        s = self._get("songs", q)
        try:
            h.render({"path": s.rel, "sub": str(s.sub)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:          # e.g. an engine error before anything was sent
            log.warning("stream %s misslyckades: %s", s.rel, e)
            raise SubsonicError(0, f"could not render: {e}")
        return None

    m_download = m_stream

    def m_getCoverArt(self, q, h):
        i = q.get("id", "")
        al = self.ix.albums.get(i)
        if al is None:
            song = self.ix.songs.get(i)
            al = self.ix.albums.get(song.album_id) if song else None
        if al is None or not al.cover:
            raise SubsonicError(70, "no image")
        p = Path(al.cover)
        data = p.read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", "image/png" if p.suffix.lower() == ".png" else "image/jpeg")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return None
