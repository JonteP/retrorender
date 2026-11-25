"""vgmstream TXTP awareness.

A .txtp is a small "playlist" that tells vgmstream how to play other files: which file,
which subsong or channels, how intro and loop segments join. Rippers add them so tracks
play as intended. Two forms are common:

    SMG_galaxy03_multi.ast#C1,2.txtp       file name carries the command (body empty)
    level1.txtp  containing:  intro.adx / loop.adx / loop_start_segment = 2

When a folder has TXTP files, the TXTPs are the tracks and the files they reference are
hidden (otherwise every song would show up twice).
"""
from __future__ import annotations

import re
from pathlib import Path


def _strip_commands(entry: str) -> str:
    """'SMG_galaxy03_multi.ast#C1,2' -> 'SMG_galaxy03_multi.ast'; 'a/b.adx #i' -> 'b.adx'."""
    entry = entry.strip().split("#", 1)[0].strip()
    return Path(entry.replace("\\", "/")).name


def references(txtp: Path) -> set[str]:
    """File names (in the same folder tree) that a TXTP plays."""
    refs: set[str] = set()
    name_cmd = txtp.name[:-len(".txtp")]
    try:
        body = txtp.read_text("utf-8", "replace")
    except OSError:
        body = ""
    lines = [ln.strip() for ln in body.splitlines()]
    entries = [ln for ln in lines if ln and not ln.startswith("#") and "=" not in ln.split("#")[0]]
    if not entries:
        # empty body: the file name itself is the entry ("file.ext#commands")
        entries = [name_cmd]
    for e in entries:
        ref = _strip_commands(e)
        if ref and not ref.lower().endswith(".txtp"):
            refs.add(ref.lower())
    return refs


def hidden_in(folder: Path) -> set[str]:
    """Lower-case names of files in `folder` that are played through a TXTP."""
    hidden: set[str] = set()
    try:
        for t in folder.glob("*.txtp"):
            hidden |= references(t)
    except OSError:
        pass
    return hidden


def base_and_options(filename: str) -> tuple[str, str]:
    """Title-map key and options for a track file.
    'SMG_galaxy03_multi.ast#C1,2.txtp' -> ('smg_galaxy03_multi', 'C1,2')
    'SMG_galaxy14_strm.ast'            -> ('smg_galaxy14_strm', '')"""
    name = filename
    if name.lower().endswith(".txtp"):
        name = name[:-5]
    opts = ""
    if "#" in name:
        name, opts = name.split("#", 1)
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name.strip())
    return stem.lower(), opts.strip()


def describe_options(opts: str) -> str:
    """Human-readable suffix for common TXTP options."""
    m = re.fullmatch(r"[Cc]([\d,\-]+)", opts or "")
    if m:
        return f"channels {m.group(1)}"
    m = re.fullmatch(r"(\d+)", opts or "")
    if m:
        return f"subsong {m.group(1)}"
    return opts
