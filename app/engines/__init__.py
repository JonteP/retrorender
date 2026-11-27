from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import vgmfile
from .base import Engine, EngineError, TrackInfo
from .gme import GmeEngine
from .jaudio import JAudioEngine
from .libvgm import LibvgmEngine
from .openmpt import OpenmptEngine
from .sidplay import SidplayEngine
from .vgmstream import VgmstreamEngine
from .xsf import XsfEngine

VGM_EXT = (".vgm", ".vgz")


class Registry:
    """Picks the engine. VGM/VGZ by the chips the file uses, everything else by extension."""

    def __init__(self, cfg: dict):
        sr = int(cfg.get("sample_rate", 44100))
        eng = cfg.get("engines", {})
        # "auto": libgme for Sega-only files, libvgm for everything else
        # "libvgm": libvgm for all VGM   "gme": libgme for all VGM
        self.vgm_mode = eng.get("vgm_engine", "auto")
        self.gme = GmeEngine(sr)
        self.libvgm = LibvgmEngine(sr, cli=eng.get("vgm2wav_cli", "vgm2wav"),
                                   args=eng.get("vgm2wav_args"))
        candidates: list[Engine] = [
            self.gme,
            self.libvgm,
            OpenmptEngine(sr),
            VgmstreamEngine(sr, cli=eng.get("vgmstream_cli", "vgmstream-cli")),
            SidplayEngine(sr, cli=eng.get("sidplayfp_cli", "sidplayfp"),
                          songlengths=eng.get("songlengths"), stil=eng.get("stil")),
            JAudioEngine(sr, fx_send=float(eng.get("jaudio_fx_send", 0.35))),
            XsfEngine(sr, cli=eng.get("xsfplay_cli", "xsfplay"), bios=eng.get("psx_bios"),
                      meta_dir=os.environ.get("RETRO_META", cfg.get("meta_dir", "/meta")),
                      ds_jit=bool(eng.get("xsf_ds_jit", False)),
                      psx_dma=str(eng.get("psx_dma", "harsh"))),
        ]
        self.xsf = candidates[-1]
        self.engines = [e for e in candidates if e.available()]
        self.missing = [e.name for e in candidates if not e.available()]
        self.by_ext: dict[str, Engine] = {}
        for e in self.engines:
            for x in e.extensions:
                self.by_ext.setdefault(x, e)

    def diagnostics(self) -> dict:
        d = {"vgm_engine": self.vgm_mode}
        if self.libvgm.available():
            d["vgm2wav_help"] = self.libvgm.selftest()
        if self.xsf.available():
            b = self.xsf.bios()
            d["psx_bios"] = str(b) if b else self.xsf.bios_hint()
        return d

    def for_path(self, path: Path) -> Optional[Engine]:
        ext = path.suffix.lower()
        if ext in VGM_EXT:
            return self._for_vgm(path)
        return self.by_ext.get(ext)

    def _for_vgm(self, path: Path) -> Optional[Engine]:
        gme_ok, lib_ok = self.gme.available(), self.libvgm.available()
        if self.vgm_mode == "libvgm" and lib_ok:
            return self.libvgm
        if self.vgm_mode == "gme" and gme_ok:
            return self.gme
        try:
            h = vgmfile.parse(path)
        except Exception:
            return self.gme if gme_ok else (self.libvgm if lib_ok else None)
        if h.gme_compatible and gme_ok:
            return self.gme
        if lib_ok:
            return self.libvgm
        raise EngineError(f"needs libvgm (chips: {', '.join(h.chips)}), but vgm2wav is missing")

    def engine_hint(self, path: Path) -> Optional[str]:
        """Engine name for listings; never raises."""
        try:
            e = self.for_path(path)
            return e.name if e else None
        except EngineError:
            return "missing"


__all__ = ["Registry", "Engine", "EngineError", "TrackInfo"]
