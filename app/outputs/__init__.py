"""Play on network devices: Google Cast and DLNA/UPnP renderers, found automatically.

The device fetches the audio itself from /render on this server; we only tell it the URL
and follow its state to move on through the queue. No Home Assistant / Music Assistant
needed. Discovery uses multicast, so the container needs the host network (or list the
devices under [outputs] in config.toml).
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional
from urllib.parse import quote

from .cast import CastDevice
from .dlna import DlnaDevice
from . import dlna, mdns

log = logging.getLogger("retrorender.outputs")

FORMATS = {  # our fmt -> mime types a device may list for it
    "wav": ("audio/wav", "audio/x-wav", "audio/wave", "audio/l16"),
    "flac": ("audio/flac", "audio/x-flac"),
    "mp3": ("audio/mpeg", "audio/mp3"),
}
MIME = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}


def local_ip_for(host: str) -> str:
    """Our address as seen from `host` (the interface the route to it uses)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _same_url(a: str, b: str) -> bool:
    from urllib.parse import unquote
    return unquote(a.replace("&amp;", "&")) == unquote(b)


class Session:
    """A queue playing on one device."""

    def __init__(self, mgr: "Outputs", dev, songs: list, index: int):
        self.mgr, self.dev, self.songs, self.index = mgr, dev, songs, index
        self.repeat = "off"         # off | all | one
        self.gen = 0                # bumped by every jump, so a replaced song is noticed
        self.stopped = False
        self.error = ""
        self.started_at = 0.0
        self.seen_playing = False
        self._wake = threading.Event()
        self._lock = threading.Lock()
        if isinstance(dev, CastDevice):
            dev.on_status = lambda st: self._wake.set()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def song(self):
        return self.songs[self.index] if 0 <= self.index < len(self.songs) else None

    def _start(self):
        s = self.song
        fmt = self.mgr.pick_format(self.dev)
        url, meta = self.mgr.url_and_meta(s, self.dev, fmt)
        mime = next((m for m in self.dev.formats if m in FORMATS[fmt]), MIME[fmt])
        log.info("%s: playing %s (%s)", self.dev.name, s.title, fmt)
        self.seen_playing = False
        self.started_at = time.time()
        self.dev.status = {"state": "IDLE"}
        self.url = url
        self.dev.load(url, mime, meta, s.duration or None)

    def _run(self):
        while not self.stopped and self.song is not None:
            try:
                with self._lock:
                    self._start()
                    self.error = ""
            except Exception as e:      # noqa: BLE001 - report and try the next track
                self.error = str(e)
                log.warning("%s: %s", self.dev.name, e)
                time.sleep(2)
                if not self._advance(1):
                    break
                continue
            if self._follow():
                if self.repeat == "one":
                    continue
                if not self._advance(1):
                    break
        self.stopped = True

    def _follow(self) -> bool:
        """Wait until the current track ends. True = finished, go on; False = replaced or
        stopped. States only count once the device reports our own URL: right after a load
        it may still report the previous track (an LG TV does while it asks for approval)."""
        gen = self.gen
        while not self.stopped and self.gen == gen:
            self._wake.wait(2.0)
            self._wake.clear()
            if self.stopped or self.gen != gen:
                return False
            try:
                st = self.dev.poll() if isinstance(self.dev, DlnaDevice) else self.dev.status
            except Exception:           # noqa: BLE001 - a missed poll is not an error
                continue
            uri = st.get("uri")
            if uri and not _same_url(uri, self.url):
                continue                    # still the previous track
            state = st.get("state")
            if state in ("PLAYING", "BUFFERING", "PAUSED"):
                self.seen_playing = True
                self.error = ""
            elif self.seen_playing and state == "IDLE":
                reason = st.get("idle_reason")
                if reason in (None, "FINISHED", "ERROR"):
                    return True
                if reason == "CANCELLED":       # stopped from another app or the remote
                    self.stopped = True
                    return False
                # INTERRUPTED: our own next load replaced it; keep waiting
            if not self.seen_playing:
                waited = time.time() - self.started_at
                if waited > 20:
                    self.error = "waiting for the device (approve playback on the TV?)"
                if waited > 180:            # never started: stop instead of skipping
                    self.error = "the device did not start playing"
                    self.stopped = True
                    return False
        return False

    def _advance(self, step: int) -> bool:
        nxt = self.index + step
        if self.repeat == "all" and self.songs:
            nxt %= len(self.songs)
        if 0 <= nxt < len(self.songs):
            self.index = nxt
            self.gen += 1
            return True
        return False

    # user commands
    def jump(self, step: int):
        n = len(self.songs)
        nxt = (self.index + step) % n if self.repeat == "all" and n else self.index + step
        if 0 <= nxt < n:
            self.goto(nxt)

    def goto(self, index: int):
        if not 0 <= index < len(self.songs):
            return
        self.index = index
        self.gen += 1
        self._wake.set()
        if self.stopped:
            self.stopped = False
            threading.Thread(target=self._run, daemon=True).start()

    def replace(self, songs: list, index: int):
        """A changed queue (reordered, added to, removed from). If the current song is
        still current, it keeps playing; otherwise the new current song starts."""
        if not songs:
            return self.stop()
        index = max(0, min(index, len(songs) - 1))
        same = self.song is not None and songs[index].id == self.song.id
        with self._lock:
            self.songs = songs
            if same:
                self.index = index          # same object position moved: no restart
                return
        self.goto(index)

    def stop(self):
        self.stopped = True
        self._wake.set()
        try:
            self.dev.stop()
        except Exception:               # noqa: BLE001
            pass

    def info(self) -> dict:
        s = self.song
        st = getattr(self.dev, "status", {}) or {}
        return {"index": self.index, "count": len(self.songs), "stopped": self.stopped,
                "repeat": self.repeat, "songs": [x.id for x in self.songs],
                "error": self.error, "state": "STOPPED" if self.stopped else st.get("state"),
                "position": st.get("position"),
                "song": {"id": s.id, "title": s.title, "artist": s.artist,
                         "album_id": s.album_id, "duration": s.duration} if s else None}


class Outputs:
    def __init__(self, cfg: dict, lib, port: int):
        self.cfg = cfg.get("outputs", {}) or {}
        self.enabled = self.cfg.get("enabled", True)
        self.lib, self.port = lib, port
        self.devices: dict = {}
        self.sessions: dict[str, Session] = {}
        self.last_discovery = 0.0
        self._lock = threading.Lock()

    def start(self):
        if not self.enabled:
            return

        def loop():
            while True:
                try:
                    self.discover()
                except Exception as e:  # noqa: BLE001
                    log.warning("discovery: %s", e)
                time.sleep(max(60, int(self.cfg.get("rediscover_minutes", 10)) * 60))
        threading.Thread(target=loop, daemon=True).start()

    def discover(self):
        found = {}
        if self.cfg.get("discover", True):
            for s in mdns.browse("_googlecast._tcp.local"):
                txt = s["txt"]
                d = CastDevice(s["host"], s["port"], txt.get("fn", s["name"]), txt.get("id", ""),
                               txt.get("md", ""))
                found[d.id] = d
            locations = dlna.discover()
        else:
            locations = []
        for ent in self.cfg.get("cast", []):          # "192.168.1.50" or "host:port"
            host, _, port = str(ent).partition(":")
            d = CastDevice(host, int(port or 8009), name=f"Cast {ent}")
            found.setdefault(d.id, d)
        for loc in list(locations) + list(self.cfg.get("dlna", [])):
            try:
                d = DlnaDevice(loc)
                found.setdefault(d.id, d)
            except Exception as e:      # noqa: BLE001 - one odd device must not stop the rest
                log.info("dlna %s: %s", loc, e)
        with self._lock:
            for k, d in found.items():
                if k not in self.devices or k not in self.sessions:
                    self.devices[k] = d
            self.last_discovery = time.time()
        log.info("outputs: %d devices", len(self.devices))

    # --- helpers ---
    def base_url(self, dev) -> str:
        return (self.cfg.get("base_url") or f"http://{local_ip_for(dev.host)}:{self.port}").rstrip("/")

    def pick_format(self, dev) -> str:
        want = self.cfg.get("format", "auto")
        if want in FORMATS:
            return want
        if isinstance(dev, CastDevice):
            return "flac"
        for fmt in ("flac", "wav", "mp3"):
            if any(m in dev.formats for m in FORMATS[fmt]):
                return fmt
        return "mp3"

    def url_and_meta(self, song, dev, fmt):
        base = self.base_url(dev)
        url = f"{base}/render?path={quote(song.rel)}&sub={song.sub}&fmt={fmt}"
        al = self.lib.index.albums.get(song.album_id)
        from library import UNKNOWN
        artist = "" if song.artist == UNKNOWN else song.artist
        meta = {"title": song.title, "artist": artist or (al.platform if al else ""),
                "album": al.name if al else "",
                "cover": f"{base}/art/{song.album_id}" if al and al.cover else ""}
        return url, meta

    # --- API ---
    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.devices.values(), key=lambda d: d.name.lower()):
            s = self.sessions.get(d.id)
            out.append({"id": d.id, "name": d.name, "kind": d.kind, "model": d.model,
                        "host": d.host, "formats": d.formats,
                        "session": s.info() if s else None})
        return out

    def _songs(self, song_ids: list[str]) -> list:
        songs = [self.lib.index.songs[i] for i in song_ids if i in self.lib.index.songs]
        if not songs:
            raise ValueError("nothing to play")
        return songs

    def play(self, dev_id: str, song_ids: list[str], start: int = 0,
             repeat: str = "off") -> dict:
        dev = self.devices.get(dev_id)
        if dev is None:
            raise KeyError("unknown device")
        songs = self._songs(song_ids)
        old = self.sessions.pop(dev_id, None)
        if old:
            old.stopped = True
            old._wake.set()
        s = Session(self, dev, songs, max(0, min(start, len(songs) - 1)))
        s.repeat = repeat if repeat in ("off", "all", "one") else "off"
        self.sessions[dev_id] = s
        return s.info()

    def queue(self, dev_id: str, song_ids: list[str], index: int) -> dict:
        """Change the queue of a playing session without restarting the current song."""
        if dev_id not in self.devices:
            raise KeyError("unknown device")
        s = self.sessions.get(dev_id)
        if s is None or s.stopped:
            return self.play(dev_id, song_ids, index)
        s.replace(self._songs(song_ids), index)
        return s.info()

    def control(self, dev_id: str, action: str, value: Optional[float] = None) -> dict:
        dev = self.devices.get(dev_id)
        if dev is None:
            raise KeyError("unknown device")
        s = self.sessions.get(dev_id)
        if action == "pause":
            dev.pause()
        elif action == "resume":
            dev.resume()
        elif action in ("next", "prev") and s:
            s.jump(1 if action == "next" else -1)
        elif action == "stop" and s:
            s.stop()
        elif action == "goto" and s and value is not None:
            s.goto(int(value))
        elif action == "repeat" and s and value in ("off", "all", "one"):
            s.repeat = value
        elif action == "volume" and value is not None:
            dev.set_volume(float(value))
        else:
            raise ValueError(f"unknown action {action}")
        return s.info() if s else {}
