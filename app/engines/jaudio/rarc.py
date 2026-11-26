"""Minimal RARC (Nintendo archive) reader: returns {path: bytes} for files."""
import struct


def read_rarc(d: bytes) -> dict:
    if d[:4] == b"Yaz0":
        raise ValueError("Yaz0-compressed archive")
    assert d[:4] == b"RARC"
    data_start = 0x20 + struct.unpack_from(">I", d, 0x0C)[0]
    info = 0x20
    ncount, noff, ecount, eoff, ssize, soff = struct.unpack_from(">6I", d, info)
    noff += info; eoff += info; soff += info

    def name(o):
        e = d.index(b"\0", soff + o)
        return d[soff + o:e].decode("latin-1")

    nodes = [struct.unpack_from(">4sIHHI", d, noff + 16 * i) for i in range(ncount)]
    out = {}

    def walk(ni, prefix):
        _, nameoff, _, cnt, first = nodes[ni]
        for k in range(cnt):
            eid, _, flags, _, nmo, off, size = struct.unpack_from(">HHBBHII", d, eoff + 20 * (first + k))
            nm = name(nmo)
            if flags & 0x02:
                if nm in (".", ".."):
                    continue
                walk(off, prefix + nm + "/")
            else:
                out[prefix + nm] = d[data_start + off: data_start + off + size]

    walk(0, "")
    return out
