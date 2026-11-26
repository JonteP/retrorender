"""Google Cast sender (stdlib only): TLS on port 8009, length-prefixed protobuf CastMessage
frames carrying JSON, the Default Media Receiver app and the media namespace."""
from __future__ import annotations

import json
import logging
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("retrorender.cast")

NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HEART = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"


# --- protobuf (just the CastMessage fields we need) ---------------------------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode(src: str, dst: str, ns: str, payload: str) -> bytes:
    def s(field, v):
        b = v.encode("utf-8")
        return _varint(field << 3 | 2) + _varint(len(b)) + b
    msg = (_varint(1 << 3) + _varint(0) + s(2, src) + s(3, dst) + s(4, ns)
           + _varint(5 << 3) + _varint(0) + s(6, payload))
    return struct.pack(">I", len(msg)) + msg


def decode(msg: bytes) -> dict:
    out, i = {}, 0
    while i < len(msg):
        key, i = _read_varint(msg, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(msg, i)
        elif wt == 2:
            n, i = _read_varint(msg, i)
            v, i = msg[i:i + n], i + n
        else:
            raise ValueError(f"wire type {wt}")
        out[field] = v
    return {"src": out.get(2, b"").decode(), "dst": out.get(3, b"").decode(),
            "ns": out.get(4, b"").decode(), "payload": out.get(6, b"").decode("utf-8", "replace")}


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = b[i]; i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, i


# --- connection ----------------------------------------------------------------------------
class CastDevice:
    kind = "cast"

    def __init__(self, host: str, port: int = 8009, name: str = "", uuid: str = "",
                 model: str = ""):
        self.host, self.port, self.name, self.uuid, self.model = host, port, name or host, uuid, model
        self.id = "cast:" + (uuid or host)
        self.formats = ["audio/wav", "audio/flac", "audio/mpeg"]
        self._sock: Optional[ssl.SSLSocket] = None
        self._lock = threading.Lock()
        self._req = 0
        self.transport: Optional[str] = None
        self.session_id: Optional[int] = None
        self.status: dict = {"state": "IDLE"}
        self.volume: Optional[float] = None
        self.on_status: Optional[Callable[[dict], None]] = None
        self._waiters: dict[int, threading.Event] = {}
        self._replies: dict[int, dict] = {}

    # transport
    def _connect(self):
        if self._sock is not None:
            return
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE           # Cast devices use self-signed certificates
        raw = socket.create_connection((self.host, self.port), timeout=5)
        self._sock = ctx.wrap_socket(raw)
        self._sock.settimeout(None)
        self.transport = None
        self._send(NS_CONN, "receiver-0", {"type": "CONNECT"})
        threading.Thread(target=self._reader, args=(self._sock,), daemon=True).start()
        threading.Thread(target=self._heartbeat, args=(self._sock,), daemon=True).start()

    def close(self):
        s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _send(self, ns: str, dst: str, payload: dict):
        with self._lock:
            if self._sock is None:
                raise ConnectionError("not connected")
            self._sock.sendall(encode("sender-0", dst, ns, json.dumps(payload)))

    def _request(self, ns: str, dst: str, payload: dict, wait: float = 8.0) -> dict:
        self._req += 1
        rid = self._req
        payload = dict(payload, requestId=rid)
        ev = threading.Event()
        self._waiters[rid] = ev
        self._send(ns, dst, payload)
        ev.wait(wait)
        self._waiters.pop(rid, None)
        return self._replies.pop(rid, {})

    def _heartbeat(self, sock):
        while self._sock is sock:
            try:
                self._send(NS_HEART, "receiver-0", {"type": "PING"})
            except (OSError, ConnectionError):
                break
            time.sleep(5)

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _reader(self, sock):
        try:
            while self._sock is sock:
                n = struct.unpack(">I", self._recv_exact(sock, 4))[0]
                m = decode(self._recv_exact(sock, n))
                try:
                    p = json.loads(m["payload"]) if m["payload"] else {}
                except json.JSONDecodeError:
                    continue
                self._handle(m["ns"], p)
        except (OSError, ConnectionError, struct.error) as e:
            log.info("cast %s: connection ended (%s)", self.name, e)
        finally:
            if self._sock is sock:
                self._sock = None

    def _handle(self, ns, p):
        t = p.get("type")
        if ns == NS_HEART and t == "PING":
            self._send(NS_HEART, "receiver-0", {"type": "PONG"})
            return
        if t == "RECEIVER_STATUS":
            st = p.get("status", {})
            vol = st.get("volume", {})
            if "level" in vol:
                self.volume = vol["level"]
            app = next((a for a in st.get("applications", [])
                        if a.get("appId") == DEFAULT_MEDIA_RECEIVER), None)
            self.transport = app.get("transportId") if app else None
        elif t == "MEDIA_STATUS":
            for s in p.get("status", []):
                self.session_id = s.get("mediaSessionId", self.session_id)
                self.status = {"state": s.get("playerState", "IDLE"),
                               "idle_reason": s.get("idleReason"),
                               "position": s.get("currentTime")}
                if self.on_status:
                    self.on_status(self.status)
        rid = p.get("requestId")
        if rid in self._waiters:
            self._replies[rid] = p
            self._waiters[rid].set()

    # media control
    def _ensure_app(self):
        self._connect()
        if not self.transport:
            self._request(NS_RECV, "receiver-0", {"type": "GET_STATUS"}, 4)
        if not self.transport:
            self._request(NS_RECV, "receiver-0", {"type": "LAUNCH", "appId": DEFAULT_MEDIA_RECEIVER}, 10)
            for _ in range(20):
                if self.transport:
                    break
                time.sleep(0.25)
        if not self.transport:
            raise ConnectionError("Cast receiver app did not start")
        self._send(NS_CONN, self.transport, {"type": "CONNECT"})

    def load(self, url: str, content_type: str, meta: dict, duration: Optional[float] = None):
        try:
            self._ensure_app()
        except (OSError, ConnectionError):
            self.close()                      # stale socket: reconnect once
            self._ensure_app()
        media = {"contentId": url, "contentType": content_type, "streamType": "BUFFERED",
                 "metadata": {"metadataType": 3, "title": meta.get("title", ""),
                              "artist": meta.get("artist", ""),
                              "albumName": meta.get("album", ""),
                              "images": [{"url": meta["cover"]}] if meta.get("cover") else []}}
        if duration:
            media["duration"] = duration
        r = self._request(NS_MEDIA, self.transport, {"type": "LOAD", "media": media, "autoplay": True})
        if r.get("type") in ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"):
            raise RuntimeError(f"Cast: {r.get('type')}")

    def _media(self, typ: str, **kw):
        if not (self.transport and self.session_id is not None):
            return
        self._send(NS_MEDIA, self.transport, dict(type=typ, mediaSessionId=self.session_id,
                                                  requestId=0, **kw))

    def pause(self): self._media("PAUSE")
    def resume(self): self._media("PLAY")
    def stop(self): self._media("STOP")

    def set_volume(self, level: float):
        self._connect()
        self._send(NS_RECV, "receiver-0", {"type": "SET_VOLUME", "volume": {"level": max(0.0, min(1.0, level))},
                                           "requestId": 0})
