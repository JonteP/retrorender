"""UPnP/DLNA MediaRenderer control (stdlib only): SSDP discovery, device description,
AVTransport / RenderingControl / ConnectionManager SOAP actions."""
from __future__ import annotations

import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger("retrorender.dlna")

SSDP = ("239.255.255.250", 1900)
ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
NS = {"d": "urn:schemas-upnp-org:device-1-0"}


def discover(timeout: float = 3.0) -> list[str]:
    """-> description URLs (LOCATION) of media renderers that answered."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP[0]}:{SSDP[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {ST}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    s.settimeout(0.3)
    s.bind(("", 0))
    found = []
    try:
        for _ in range(2):
            s.sendto(msg, SSDP)
            end = time.time() + timeout / 2
            while time.time() < end:
                try:
                    data, _ = s.recvfrom(4096)
                except socket.timeout:
                    continue
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    loc = m.group(1).decode()
                    if loc not in found:
                        found.append(loc)
    finally:
        s.close()
    return found


class DlnaDevice:
    kind = "dlna"

    def __init__(self, location: str):
        self.location = location
        xml = urllib.request.urlopen(location, timeout=5).read()
        root = ET.fromstring(xml)
        base = root.findtext("d:URLBase", default="", namespaces=NS) or location
        dev = root.find("d:device", NS)
        self.name = dev.findtext("d:friendlyName", default="DLNA renderer", namespaces=NS)
        self.model = dev.findtext("d:modelName", default="", namespaces=NS)
        self.uuid = dev.findtext("d:UDN", default=location, namespaces=NS)
        self.id = "dlna:" + self.uuid.replace("uuid:", "")
        self.host = re.sub(r"^https?://([^:/]+).*$", r"\1", location)
        self.services = {}
        for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
            st = svc.findtext("d:serviceType", default="", namespaces=NS)
            ctl = svc.findtext("d:controlURL", default="", namespaces=NS)
            for short in ("AVTransport", "RenderingControl", "ConnectionManager"):
                if f":{short}:" in st and short not in self.services:
                    self.services[short] = (st, urljoin(base, ctl))
        if "AVTransport" not in self.services:
            raise ValueError(f"{self.name}: no AVTransport service")
        self.formats = self._sink_formats()
        self.status = {"state": "IDLE"}

    # SOAP
    def _soap(self, service: str, action: str, args: dict, timeout=6) -> dict:
        st, url = self.services[service]
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        env = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{st}">{body}</u:{action}></s:Body></s:Envelope>')
        req = urllib.request.Request(url, data=env.encode("utf-8"), headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{st}#{action}"'})
        try:
            xml = urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            code = re.search(r"<errorCode>(\d+)</errorCode>", detail)
            raise RuntimeError(f"{self.name}: {action} failed ({code.group(1) if code else e.code})")
        out = {}
        for el in ET.fromstring(xml).iter():
            tag = el.tag.split("}")[-1]
            if el.text is not None and not list(el):
                out[tag] = el.text
        return out

    def _sink_formats(self) -> list[str]:
        if "ConnectionManager" not in self.services:
            return ["audio/mpeg"]
        try:
            sink = self._soap("ConnectionManager", "GetProtocolInfo", {}).get("Sink", "")
        except Exception as e:      # noqa: BLE001 - some renderers fail this; fall back to MP3
            log.info("%s: GetProtocolInfo: %s", self.name, e)
            return ["audio/mpeg"]
        fmts = []
        for p in sink.split(","):
            parts = p.split(":")
            if len(parts) >= 3 and parts[0] == "http-get":
                mime = parts[2].split(";")[0].lower()
                if mime.startswith("audio/") and mime not in fmts:
                    fmts.append(mime)
        return fmts or ["audio/mpeg"]

    # control
    def load(self, url: str, content_type: str, meta: dict, duration: Optional[float] = None):
        didl = didl_lite(url, content_type, meta, duration)
        try:
            self._soap("AVTransport", "Stop", {"InstanceID": 0})
        except Exception:           # noqa: BLE001 - stopping an idle renderer may fail
            pass
        self._soap("AVTransport", "SetAVTransportURI",
                   {"InstanceID": 0, "CurrentURI": url, "CurrentURIMetaData": didl})
        self._soap("AVTransport", "Play", {"InstanceID": 0, "Speed": 1})

    def poll(self) -> dict:
        info = self._soap("AVTransport", "GetTransportInfo", {"InstanceID": 0}, timeout=4)
        state = info.get("CurrentTransportState", "STOPPED")
        pos = None
        try:
            p = self._soap("AVTransport", "GetPositionInfo", {"InstanceID": 0}, timeout=4)
            pos = _hms(p.get("RelTime", ""))
        except Exception:           # noqa: BLE001
            pass
        st = {"PLAYING": "PLAYING", "PAUSED_PLAYBACK": "PAUSED", "TRANSITIONING": "BUFFERING"}.get(state, "IDLE")
        self.status = {"state": st, "position": pos, "raw": state}
        return self.status

    def pause(self): self._soap("AVTransport", "Pause", {"InstanceID": 0})
    def resume(self): self._soap("AVTransport", "Play", {"InstanceID": 0, "Speed": 1})
    def stop(self): self._soap("AVTransport", "Stop", {"InstanceID": 0})

    @property
    def volume(self) -> Optional[float]:
        if "RenderingControl" not in self.services:
            return None
        try:
            v = self._soap("RenderingControl", "GetVolume", {"InstanceID": 0, "Channel": "Master"}, timeout=3)
            return int(v.get("CurrentVolume", 0)) / 100.0
        except Exception:           # noqa: BLE001
            return None

    def set_volume(self, level: float):
        if "RenderingControl" in self.services:
            self._soap("RenderingControl", "SetVolume", {"InstanceID": 0, "Channel": "Master",
                                                         "DesiredVolume": int(round(max(0, min(1, level)) * 100))})

    def close(self):
        pass


def _hms(s: str) -> Optional[float]:
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", s or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def didl_lite(url, content_type, meta, duration=None) -> str:
    dur = ""
    if duration:
        h, r = divmod(duration, 3600)
        mm, ss = divmod(r, 60)
        dur = f' duration="{int(h)}:{int(mm):02d}:{ss:06.3f}"'
    cover = f"<upnp:albumArtURI>{escape(meta['cover'])}</upnp:albumArtURI>" if meta.get("cover") else ""
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="1" parentID="0" restricted="1">'
            f"<dc:title>{escape(meta.get('title', ''))}</dc:title>"
            f"<dc:creator>{escape(meta.get('artist', ''))}</dc:creator>"
            f"<upnp:artist>{escape(meta.get('artist', ''))}</upnp:artist>"
            f"<upnp:album>{escape(meta.get('album', ''))}</upnp:album>"
            f"{cover}<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f'<res protocolInfo="http-get:*:{content_type}:*"{dur}>{escape(url)}</res>'
            "</item></DIDL-Lite>")
