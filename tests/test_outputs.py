"""Cast framing, mDNS parsing, DLNA control against an in-process fake renderer
(python3 tests/test_outputs.py)."""
import re
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from outputs.cast import decode, encode  # noqa: E402
from outputs.dlna import DlnaDevice, didl_lite  # noqa: E402
from outputs.mdns import parse  # noqa: E402

# Cast frame round trip
f = encode("sender-0", "receiver-0", "urn:x-cast:com.google.cast.receiver", '{"type":"GET_STATUS"}')
assert struct.unpack(">I", f[:4])[0] == len(f) - 4
m = decode(f[4:])
assert m == {"src": "sender-0", "dst": "receiver-0", "ns": "urn:x-cast:com.google.cast.receiver",
             "payload": '{"type":"GET_STATUS"}'}, m


# mDNS answer: PTR + SRV + TXT + A, with name compression
def name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"


svc = name("_googlecast._tcp.local")
inst = b"\x0bKitchen-123\xc0\x0c"                     # "Kitchen-123" + pointer to service name
pkt = struct.pack(">HHHHHH", 0, 0x8400, 0, 4, 0, 0)
pkt += svc + struct.pack(">HHIH", 12, 1, 120, len(inst)) + inst
inst_ptr = b"\xc0" + bytes([12 + len(svc) + 10])          # pointer to the instance name above
srv = struct.pack(">HHH", 0, 0, 8009) + name("kitchen.local")
pkt += inst_ptr + struct.pack(">HHIH", 33, 1, 120, len(srv)) + srv
txt = b"".join(bytes([len(t)]) + t for t in (b"fn=Kitchen", b"md=Argon"))
pkt += inst_ptr + struct.pack(">HHIH", 16, 1, 120, len(txt)) + txt
pkt += name("kitchen.local") + struct.pack(">HHIH", 1, 1, 120, 4) + bytes([192, 168, 1, 50])
recs = parse(pkt)
assert (("Kitchen-123._googlecast._tcp.local", 33, (8009, "kitchen.local")) in recs), recs
assert any(t == 16 and d == {"fn": "Kitchen", "md": "Argon"} for _, t, d in recs)
assert ("kitchen.local", 1, "192.168.1.50") in recs

# DIDL-Lite escapes titles and carries the duration
d = didl_lite("http://x/render?a=1&b=2", "audio/flac", {"title": "A & B", "artist": "C"}, 75.5)
assert "A &amp; B" in d and 'duration="0:01:15.500"' in d and "a=1&amp;b=2" in d

# DLNA renderer
CALLS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = (b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>TV</friendlyName>'
             b"<UDN>uuid:tv-1</UDN><serviceList>"
             b"<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/avt</controlURL></service>"
             b"<service><serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType><controlURL>/cm</controlURL></service>"
             b"</serviceList></device></root>")
        self.send_response(200); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        act = self.headers["SOAPACTION"].split("#")[1].strip('"')
        CALLS.append((self.path, act, body))
        out = ""
        if act == "GetProtocolInfo":
            out = "<Sink>http-get:*:audio/x-flac:*,http-get:*:audio/mpeg:DLNA.ORG_PN=MP3</Sink>"
        elif act == "GetTransportInfo":
            out = "<CurrentTransportState>PLAYING</CurrentTransportState>"
        b = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
             f'<u:{act}Response xmlns:u="x">{out}</u:{act}Response></s:Body></s:Envelope>').encode()
        self.send_response(200); self.end_headers(); self.wfile.write(b)


srv_ = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv_.serve_forever, daemon=True).start()
dev = DlnaDevice(f"http://127.0.0.1:{srv_.server_port}/desc.xml")
assert dev.name == "TV" and dev.id == "dlna:tv-1" and dev.formats == ["audio/x-flac", "audio/mpeg"], dev.formats
dev.load("http://nas/render?x=1", "audio/x-flac", {"title": "Song"}, 10)
acts = [a for _, a, _ in CALLS]
assert acts[-3:] == ["Stop", "SetAVTransportURI", "Play"], acts
assert re.search(r"<CurrentURI>http://nas/render\?x=1</CurrentURI>", CALLS[-2][2])
assert dev.poll()["state"] == "PLAYING"
srv_.shutdown()

# --- a session's queue: repeat, a changed queue, jumping ---------------------------------
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from outputs import Session  # noqa: E402


class FakeDev:
    name, formats = "fake", []

    def __init__(self):
        self.loads, self.status = [], {}

    def load(self, url, mime, meta, duration):
        self.loads.append(url)
        self.status = {"state": "PLAYING", "uri": url}

    def stop(self):
        self.status = {"state": "IDLE", "idle_reason": "CANCELLED"}

    def finish(self):                       # the current track plays to the end
        self.status = dict(self.status, state="IDLE", idle_reason="FINISHED")


class FakeMgr:
    def pick_format(self, dev):
        return "flac"

    def url_and_meta(self, s, dev, fmt):
        return f"http://nas/{s.id}", {}


def wait_for(cond, what):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < 10, what
        time.sleep(0.05)


songs = [SimpleNamespace(id=f"s{i}", title=f"Song {i}", duration=10) for i in range(3)]
d = FakeDev()
ses = Session(FakeMgr(), d, songs, 1)
ses.repeat = "all"
wait_for(lambda: d.loads == ["http://nas/s1"], "first load")
time.sleep(2.2); d.finish()
wait_for(lambda: len(d.loads) == 2, "advance")
time.sleep(2.2); d.finish()
wait_for(lambda: d.loads[-1] == "http://nas/s0", "repeat all wraps to the start")
# a changed queue that keeps the current song does not restart it
ses.replace([songs[0], songs[2]], 0)
time.sleep(0.5)
assert len(d.loads) == 3 and ses.index == 0 and [x.id for x in ses.songs] == ["s0", "s2"]
# removing the current song starts the song now at its place, even at the same index
ses.replace([songs[2]], 0)
wait_for(lambda: d.loads[-1] == "http://nas/s2", "replaced current song starts")
ses.repeat = "one"
time.sleep(2.2); d.finish()
wait_for(lambda: len(d.loads) == 5 and d.loads[-1] == "http://nas/s2", "repeat one")
ses.repeat = "off"
time.sleep(2.2); d.finish()
wait_for(lambda: ses.stopped, "stops at the end without repeat")
print("ok")
