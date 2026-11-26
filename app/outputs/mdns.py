"""Minimal mDNS browser (stdlib only): find services such as _googlecast._tcp.local."""
from __future__ import annotations

import socket
import struct
import time

MDNS = ("224.0.0.251", 5353)


def _name(buf: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(128):
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack_from(">H", buf, off)[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(buf[off + 1: off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def _query(service: str) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in service.split(".")) + b"\0"
    # one question, type PTR (12), class IN with the unicast-response bit
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + q + struct.pack(">HH", 12, 0x8001)


def parse(buf: bytes) -> list[tuple[str, int, object]]:
    """-> [(name, type, data)]; data: str for PTR, (port, target) for SRV, dict for TXT,
    str (IPv4) for A."""
    out = []
    _, _, qd, an, ns, ar = struct.unpack_from(">HHHHHH", buf, 0)
    off = 12
    for _ in range(qd):
        _, off = _name(buf, off)
        off += 4
    for _ in range(an + ns + ar):
        name, off = _name(buf, off)
        rtype, _, _, rdlen = struct.unpack_from(">HHIH", buf, off)
        off += 10
        rd = buf[off: off + rdlen]
        if rtype == 12:
            data = _name(buf, off)[0]
        elif rtype == 33:
            data = (struct.unpack_from(">H", rd, 4)[0], _name(buf, off + 6)[0])
        elif rtype == 16:
            data, i = {}, 0
            while i < len(rd):
                n = rd[i]
                k, _, v = rd[i + 1: i + 1 + n].decode("utf-8", "replace").partition("=")
                data[k] = v
                i += 1 + n
        elif rtype == 1 and rdlen == 4:
            data = socket.inet_ntoa(rd)
        else:
            data = None
        out.append((name, rtype, data))
        off += rdlen
    return out


def browse(service: str, timeout: float = 2.0) -> list[dict]:
    """-> [{"name", "host", "port", "txt"}] for each instance that answered."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.settimeout(0.3)
    s.bind(("", 0))
    recs, sources = [], {}
    try:
        for _ in range(2):
            s.sendto(_query(service), MDNS)
            end = time.time() + timeout / 2
            while time.time() < end:
                try:
                    buf, addr = s.recvfrom(9000)
                except socket.timeout:
                    continue
                try:
                    rs = parse(buf)
                except (struct.error, IndexError):
                    continue
                recs += rs
                for n, t, _ in rs:
                    sources.setdefault(n, addr[0])
    finally:
        s.close()
    instances = {d for n, t, d in recs if t == 12 and n.lower() == service.lower()}
    srv = {n: d for n, t, d in recs if t == 33}
    txt = {n: d for n, t, d in recs if t == 16}
    addr = {n: d for n, t, d in recs if t == 1}
    out = []
    for inst in sorted(instances):
        port, target = srv.get(inst, (0, ""))
        host = addr.get(target) or sources.get(inst)
        if host and port:
            out.append({"name": inst, "host": host, "port": port, "txt": txt.get(inst, {})})
    return out
