from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import vgmfile
from .base import Engine, EngineError, TrackInfo
from .gme import GmeEngine
from .libvgm import LibvgmEngine
from .openmpt import OpenmptEngine
from .sidplay import SidplayEngine
from .vgmstream import VgmstreamEngine

VGM_EXT = (".vgm", ".vgz")


class Registry:
    """Väljer motor. VGM/VGZ väljs efter vilka chip filen använder, resten efter filändelse."""

    def __init__(self, cfg: dict):
        sr = int(cfg.get("sample_rate", 44100))
        eng = cfg.get("engines", {})
        # "auto": libgme för rena Sega-filer, libvgm för allt annat
        # "libvgm": libvgm för alla VGM   "gme": libgme för alla VGM
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
        ]
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
        raise EngineError(f"kräver libvgm (chip: {', '.join(h.chips)}), men vgm2wav saknas")

    def engine_hint(self, path: Path) -> Optional[str]:
        """Motorns namn för listningar; kastar aldrig."""
        try:
            e = self.for_path(path)
            return e.name if e else None
        except EngineError:
            return "saknas"


__all__ = ["Registry", "Engine", "EngineError", "TrackInfo"]
