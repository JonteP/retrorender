"""Metadatalager: album.toml-filer i en egen, skrivbar mapp (/meta).

Strukturen speglar biblioteket:
  /meta/<plattform>/<spel>/album.toml          album = hel mapp
  /meta/<plattform>/<spel>/<fil>.album.toml    album = en fil med delspår (NSF, SID ...)

En fil som saknas genereras automatiskt. Källor i ordning:
  1. filens egna taggar   2. rippens sidofiler (!tags.m3u, .m3u)   3. filnamnet
En befintlig fil skrivs ALDRIG över. Ta bort den, eller anropa /meta/regenerate.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tomllib
from dataclasses import replace

from txtp import base_and_options, describe_options
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("meta")

POLICY_KEYS = ("loops", "min_s", "max_s", "default_s", "fade_s")

# Höjs när genereringen blir bättre. Filer som är ORÖRDA (innehållet matchar
# kontrollsumman i första raden) och har en lägre version genereras om automatiskt.
GEN_VERSION = 4
_MARK = "# retrorender-gen"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def gen_status(text: str) -> tuple[int | None, bool]:
    """-> (genereringsversion eller None, orörd?)."""
    first, _, rest = text.partition("\n")
    m = re.match(rf"^{_MARK} (\d+) ([0-9a-f]+)$", first.strip())
    if not m:
        return None, False
    return int(m.group(1)), _sha(rest) == m.group(2)
TRACK_INFO_KEYS = ("length_s", "intro_s", "loop_s")


# --- TOML-skrivning (stdlib kan bara läsa TOML) -----------------------------------
def _tv(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(round(v, 3)) if isinstance(v, float) else str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{s}"'


# --- sidofiler i rippen -------------------------------------------------------------
def _secs(s: str, allow_zero: bool = False) -> float | None:
    """'2:30', '0:02:30', '150', '1:05.500' -> sekunder."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(":")]
    except ValueError:
        return None
    v = 0.0
    for x in parts:
        v = v * 60 + x
    return v if v > 0 or (allow_zero and v == 0) else None


def parse_nezplug_line(line: str):
    """NEZplug-format: 'spel.nsf::NSF,1,Titel,2:30,,10,' -> (filnamn, post) eller None.
    Spårnummer med '$' är hexadecimala och 0-baserade, annars 1-baserade."""
    m = re.match(r"^(.+?)::(\w+),(.*)$", line)
    if not m:
        return None
    fields = [x.replace("\\,", ",").strip() for x in re.split(r"(?<!\\),", m.group(3))]
    fields += [""] * (6 - len(fields))
    num, title, time_, _loop, fade = fields[:5]
    try:
        sub = int(num[1:], 16) if num.startswith("$") else max(0, int(num) - 1)
    except ValueError:
        return None
    entry = {"sub": sub}
    if title:
        entry["title"] = title
    if (t := _secs(time_)):
        entry["length_s"] = t
    if (f := _secs(fade, allow_zero=True)) is not None:
        entry["fade_s"] = f
    return Path(m.group(1).strip()).name, entry


def parse_sidecars(d: Path) -> tuple[dict, dict, dict]:
    """Läser !tags.m3u (vgmstream-format), .m3u med #EXTINF och NEZplug-m3u.
    Returnerar (albumtaggar, {filnamn: {title, artist, track}},
                {filnamn: [delspårsposter i spellistans ordning]})."""
    album, files, nez = {}, {}, {}
    cands = sorted(d.glob("!tags.m3u")) + sorted(
        p for p in d.glob("*.m3u*") if p.name.lower() != "!tags.m3u")
    for m3u in cands:
        try:
            lines = m3u.read_text("utf-8", "replace").splitlines()
        except OSError:
            continue
        pending: dict = {}
        num = 0
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            m = re.match(r"^#\s*@(\w+)\s+(.*)$", line)          # globala taggar
            if m:
                album.setdefault(m.group(1).lower(), m.group(2).strip())
                continue
            m = re.match(r"^#\s*%(\w+)\s+(.*)$", line)          # taggar för nästa fil
            if m:
                pending[m.group(1).lower()] = m.group(2).strip()
                continue
            m = re.match(r"^#EXTINF:[^,]*,(.*)$", line, re.I)
            if m:
                pending.setdefault("title", m.group(1).strip())
                continue
            if line.startswith("#"):
                continue
            nz = parse_nezplug_line(line)
            if nz:
                fname, entry = nz
                lst = nez.setdefault(fname, [])
                if all(e["sub"] != entry["sub"] for e in lst):
                    lst.append(entry)
                continue
            name = Path(line.replace("\\", "/")).name
            if not (d / name).exists():         # 'file.ext #cmd' -> 'file.ext' ('x.ast#C1,2.txtp' is a real name)
                name = Path(line.split("#")[0].strip().replace("\\", "/")).name
            num += 1
            entry = files.setdefault(name, {})
            for k in ("title", "artist", "track"):
                if k in pending and k not in entry:
                    entry[k] = pending[k]
            entry.setdefault("order", num)
            pending = {}
    # .source.json is written by retrorender for synced downloads (e.g. joshw): the
    # archive's official title and release date, used when tags and playlists lack them
    src = d / ".source.json"
    if src.is_file():
        try:
            import json
            info = json.loads(src.read_text("utf-8"))
            album.setdefault("album", info.get("album", ""))
            if info.get("date"):
                album.setdefault("date", info["date"])
        except (OSError, ValueError):
            pass
    return album, files, nez


def clean_filename(stem: str) -> tuple[int | None, str]:
    """'05 Vampire Killer (Stage 1)' -> (5, 'Vampire Killer (Stage 1)')."""
    m = re.match(r"^(\d{1,3})\s*[-._)]*\s*(.*)$", stem)
    if m and m.group(2):
        num, rest = int(m.group(1)), m.group(2)
    else:
        num, rest = None, stem
    rest = re.sub(r"\s+", " ", rest.replace("_", " ")).strip()
    return num, rest or stem


# --- title maps ---------------------------------------------------------------------------
TITLEMAP_DIR = Path(__file__).parent / "titlemaps"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class TitleMaps:
    """Built-in title maps (app/titlemaps/*.toml): internal file names -> track titles,
    matched on album title and console. Users can add their own per album (titles.toml)."""

    def __init__(self, folder: Path = TITLEMAP_DIR):
        self.maps = []
        for f in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            try:
                d = tomllib.loads(f.read_text("utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as e:
                log.warning("title map %s: %s", f.name, e)
                continue
            m = d.get("match", {})
            self.maps.append({
                "file": f.name,
                "titles": {_norm(t) for t in m.get("titles", [])},
                "platforms": {_norm(p) for p in m.get("platforms", [])},
                "map": {str(k).lower(): str(v) for k, v in d.get("titles", {}).items()},
            })

    def lookup(self, album_title: str, platform: str) -> tuple[dict, str]:
        p = _norm(platform)
        # also try without "(date)(dev)" / "[alt]" parts, as in joshw archive names
        bare = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", album_title or "")
        cands = {_norm(album_title), _norm(bare)} - {""}
        for m in self.maps:
            if cands & m["titles"] and (not m["platforms"] or p in m["platforms"]):
                return m["map"], f"built-in title map {m['file']}"
        return {}, ""


def load_user_titles(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        d = tomllib.loads(path.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("titles file %s: %s", path, e)
        return {}
    # empty values (as in the web UI template) mean "no title yet"
    return {str(k).lower(): str(v) for k, v in d.get("titles", {}).items() if str(v).strip()}


class MetaStore:
    def __init__(self, meta_root: Path | None, library_root: Path):
        self.root = meta_root
        self.lib = library_root
        self.enabled = bool(meta_root)
        self._cache: dict[str, tuple[float, dict]] = {}
        self.titlemaps = TitleMaps()
        if self.enabled:
            try:
                meta_root.mkdir(parents=True, exist_ok=True)
                probe = meta_root / ".skrivtest"
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                log.warning("metamappen %s är inte skrivbar (%s): genererar inga filer", meta_root, e)
                self.writable = False
            else:
                self.writable = True
        else:
            self.writable = False

    # --- sökvägar ---------------------------------------------------------------
    def path_for(self, album_key: str) -> Path | None:
        """album_key: 'dir:<rel mapp>' eller 'file:<rel fil>'."""
        if not self.enabled:
            return None
        kind, rel = album_key.split(":", 1)
        if kind == "dir":
            return self.root / rel / "album.toml"
        return self.root / (rel + ".album.toml")

    def titles_path(self, album_key: str) -> Path | None:
        """User-owned title map for an album (never generated or overwritten)."""
        if not self.enabled:
            return None
        kind, rel = album_key.split(":", 1)
        if kind == "dir":
            return self.root / rel / "titles.toml"
        return self.root / (rel + ".titles.toml")

    def titles_newer(self, album_key: str) -> bool:
        tp, ap = self.titles_path(album_key), self.path_for(album_key)
        return bool(tp and ap and tp.is_file() and ap.is_file()
                    and tp.stat().st_mtime > ap.stat().st_mtime)

    def load(self, album_key: str) -> dict | None:
        p = self.path_for(album_key)
        if p is None or not p.is_file():
            return None
        mt = p.stat().st_mtime
        hit = self._cache.get(str(p))
        if hit and hit[0] == mt:
            return hit[1]
        try:
            data = tomllib.loads(p.read_text("utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            log.warning("kunde inte läsa %s: %s (filen används inte, men skrivs inte över)", p, e)
            data = {}
        self._cache[str(p)] = (mt, data)
        return data

    def exists(self, album_key: str) -> bool:
        p = self.path_for(album_key)
        return bool(p and p.exists())

    def untouched(self, album_key: str) -> bool:
        p = self.path_for(album_key)
        if not (p and p.is_file()):
            return False
        try:
            ver, ok = gen_status(p.read_text("utf-8"))
        except OSError:
            return False
        return ver is not None and ok

    def outdated_untouched(self, album_key: str) -> bool:
        """Sann om filen är genererad av en äldre version och inte har ändrats för hand."""
        p = self.path_for(album_key)
        if not (p and p.is_file()):
            return False
        try:
            ver, untouched = gen_status(p.read_text("utf-8"))
        except OSError:
            return False
        return ver is not None and untouched and ver < GEN_VERSION

    # --- tvingad omgenerering ------------------------------------------------------
    def all_files(self, under: str = "") -> list[Path]:
        if not self.enabled:
            return []
        base = (self.root / under).resolve()
        if not base.is_relative_to(self.root) or not base.exists():
            return []
        return sorted(p for p in base.rglob("*.toml")
                      if ".backup" not in p.relative_to(self.root).parts)

    def classify(self, files: list[Path]) -> dict[str, list[str]]:
        out = {"orörda": [], "redigerade": [], "utan_kontrollsumma": []}
        for p in files:
            try:
                ver, untouched = gen_status(p.read_text("utf-8"))
            except OSError:
                continue
            rel = str(p.relative_to(self.root))
            if ver is None:
                out["utan_kontrollsumma"].append(rel)
            elif untouched:
                out["orörda"].append(rel)
            else:
                out["redigerade"].append(rel)
        return out

    def prune_backups(self, keep_days: float):
        """Tar bort backupmappar äldre än keep_days (körs vid varje indexering)."""
        bdir = self.root / ".backup" if self.enabled else None
        if not (bdir and bdir.is_dir() and self.writable and keep_days > 0):
            return
        import shutil, time
        limit = time.time() - keep_days * 86400
        for d in bdir.iterdir():
            if d.is_dir() and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)
                log.info("tog bort gammal backup %s", d.name)

    def backup_and_remove(self, files: list[Path]) -> Path:
        """Flyttar filerna till /meta/.backup/<tidpunkt>/ med bevarad struktur."""
        dest = self.root / ".backup" / datetime.now().strftime("%Y%m%d-%H%M%S")
        for p in files:
            target = dest / p.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            p.replace(target)
            self._cache.pop(str(p), None)
        return dest

    def delete(self, album_key: str) -> bool:
        p = self.path_for(album_key)
        if p and p.is_file():
            ver, untouched = gen_status(p.read_text("utf-8"))
            if untouched:
                p.unlink()                       # orörd: inget att spara
                self._cache.pop(str(p), None)
            else:
                self.backup_and_remove([p])      # redigerad/okänd: spara en kopia
            return True
        return False

    # --- generering ---------------------------------------------------------------
    def generate(self, album_key: str, album_dir: Path, items: list, platform: str = "") -> str:
        """items: lista av (Path, TrackInfo, n_subtracks). Skriver bara om filen saknas.
        Title priority: user titles.toml > built-in title map > tags > sidecars > file name."""
        side_album, side_files, side_nez = parse_sidecars(album_dir)
        nez_file = None
        if album_key.startswith("file:"):
            nez_file = side_nez.get(Path(album_key[5:]).name)
            if nez_file:
                # spellistan bestämmer vilka delspår som finns och i vilken ordning
                order = {e["sub"]: i for i, e in enumerate(nez_file)}
                items = sorted((it for it in items if it[1].index in order),
                               key=lambda it: order[it[1].index])
        stats = {"title map": 0, "tags": 0, "sidecar": 0, "filename": 0}
        # title maps: the user's own titles.toml wins; otherwise a built-in map matched on
        # the album's likely title (playlist/source title, tags, or folder name) and console
        guess = (side_album.get("album") or side_album.get("title")
                 or next((t.game for _, t, _ in items if t.game), "") or album_dir.name)
        user_map = load_user_titles(self.titles_path(album_key))
        builtin_map, builtin_src = self.titlemaps.lookup(guess, platform)
        tmap_src = " + ".join(x for x in ((builtin_src, "titles.toml" if user_map else "")) if x)
        bases: dict[str, int] = {}
        for f, t, n in items:
            bases[base_and_options(f.name)[0]] = bases.get(base_and_options(f.name)[0], 0) + 1
        tracks_out = []
        games, authors, years = [], [], []
        for f, t, n in items:
            num, nice = clean_filename(f.stem)
            side = side_files.get(f.name, {})
            nzp = next((e for e in (nez_file or []) if e["sub"] == t.index), None)
            base, opts = base_and_options(f.name)
            if f.suffix.lower() == ".txtp":             # 'X.ast#C1,2.txtp' -> 'X (channels 1,2)'
                num, nice = clean_filename(Path(f.name.split("#")[0]).stem if "#" in f.name else f.stem)
                if opts:
                    nice += f" ({describe_options(opts)})"
                if t.title and ("#" in t.title or t.title.lower().endswith((".txtp", ".ast"))):
                    t = replace(t, title="")
            # your titles.toml first, then the rip's own playlist (!tags.m3u), then a built-in map
            if n == 1 and (base in user_map or (base in builtin_map and not side.get("title"))):
                title, src, track_no = user_map.get(base) or builtin_map[base], "title map", num
                if opts and bases.get(base, 0) > 1:
                    title += f" ({describe_options(opts)})"
            elif n > 1 and nzp and nzp.get("title"):           # NEZplug-spellista
                title, src = nzp["title"], "sidecar"
                track_no = (nez_file.index(nzp) + 1)
            elif n > 1:                                       # delspår i en fil
                title, src = t.title, "tags"
                if not title or re.fullmatch(r".* \(\d+\)", title or ""):
                    title, src = f"{nice} {t.index + 1}" if n > 1 else nice, "filename"
                track_no = t.index + 1
            elif t.title and t.title not in (f.stem, f.name):
                title, src, track_no = t.title, "tags", num
            elif side.get("title"):
                title, src, track_no = side["title"], "sidecar", num
            else:
                title, src, track_no = nice, "filename", num
            if side.get("track", "").isdigit():
                track_no = int(side["track"])
            elif track_no is None and "order" in side:
                track_no = side["order"]
            stats[src] += 1
            entry = {"file": f.name}
            if n > 1:
                entry["sub"] = t.index
            entry["title"] = title
            if track_no is not None:
                entry["track"] = track_no
            if nzp:
                for k in ("length_s", "fade_s"):
                    if k in nzp:
                        entry[k] = round(nzp[k], 3)
            artist = t.author or side.get("artist")
            if side_album.get("composer") and not side.get("artist"):
                artist = None      # playlistens @COMPOSER gäller hela albumet
            if artist:
                entry["artist"] = artist
                authors.append(artist)
            if t.game:
                games.append(t.game)
            yr = re.search(r"(19|20)\d\d", t.extra.get("date", "") if t.extra else "")
            if yr:
                years.append(int(yr.group(0)))
            hint = None
            if t.loop_s:
                hint = f"measured: intro {t.intro_s or 0:.2f} s, loop {t.loop_s:.2f} s"
            elif t.length_s:
                hint = f"measured: fixed length {t.length_s:.2f} s"
            tracks_out.append((entry, src, hint))

        title = (side_album.get("album") or side_album.get("title")
                 or (max(set(games), key=games.count) if games else None)
                 or (album_dir.name if album_key.startswith("dir:") else Path(album_key[5:]).stem))
        # Kompositör: playlistens @COMPOSER, sedan filernas taggar, sist playlistens @ARTIST
        # (som i NEZplug-rippar ofta är utgivaren, t.ex. "Konami").
        artist = (side_album.get("composer")
                  or (authors[0] if authors and len(set(authors)) == 1 else "")
                  or side_album.get("artist") or "")
        if not years:
            yr = re.search(r"(19|20)\d\d", side_album.get("date", "") or side_album.get("year", ""))
            if yr:
                years.append(int(yr.group(0)))

        L = [f"# Generated by retrorender on {date.today().isoformat()}.",
             "# Sources: " + ", ".join(f"{k} ({v})" for k, v in stats.items() if v) + "."
             + (f" Title map: {tmap_src}." if stats["title map"] else ""),
             "# Edit freely. While this file is untouched it is updated automatically when",
             "# generation improves; once you change anything it is never overwritten.",
             "# To regenerate manually: delete the file or use /meta/regenerate.",
             "", "[album]", f"title = {_tv(title)}"]
        L.append(f"artist = {_tv(artist)}" if artist else '# artist = "Composer"')
        L.append(f"year = {years[0]}" if years else "# year = 1990")
        L += ['# platform = "Sega Mega Drive"   # console (genre in MA); default = folder name',
              '# cover = "folder.jpg"     # path in the library or meta folder',
              "# loops = 2                # loop policy for the whole album",
              ("only_listed = true         # the rip's playlist decides which subtunes are shown"
               if nez_file else
               "only_listed = false        # true = hide files not listed below"), ""]
        for entry, src, hint in tracks_out:
            L.append("[[track]]")
            for k, v in entry.items():
                L.append(f"{k} = {_tv(v)}")
            L.append(f"# source: {src}" + (f"; {hint}" if hint else ""))
            L.append("# skip = true   length_s = 90   intro_s = 0   loop_s = 60   loops = 2")
            L.append("")
        body = "\n".join(L)
        text = f"{_MARK} {GEN_VERSION} {_sha(body)}\n" + body
        p = self.path_for(album_key)
        if self.writable and p and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(text, "utf-8")
            tmp.replace(p)
            log.info("genererade %s", p)
        return text

    # --- tillämpning ----------------------------------------------------------------
    @staticmethod
    def track_entry(meta: dict | None, filename: str, sub: int, n_sub: int) -> dict:
        if not meta:
            return {}
        for e in meta.get("track", []):
            if e.get("file") != filename:
                continue
            if n_sub > 1 and int(e.get("sub", -1)) != sub:
                continue
            return e
        return {}

    @staticmethod
    def apply(track, album_meta: dict, entry: dict):
        """Returnerar (TrackInfo med overrides, policylager [album, spår])."""
        changes = {}
        if entry.get("title"):
            changes["title"] = str(entry["title"])
        artist = entry.get("artist") or (album_meta or {}).get("album", {}).get("artist")
        if artist:
            changes["author"] = str(artist)
        if "length_s" in entry:
            length = float(entry["length_s"])
            fade = float(entry.get("fade_s", 0) or 0)
            extra = dict(track.extra or {})
            if fade > 0:
                # NEZplug-semantik: spela length_s, tona sedan ut under fade_s
                length += fade
                extra["fade_at_end"] = True
            changes.update(length_s=length, intro_s=None, loop_s=None, extra=extra)
        if "loop_s" in entry:
            changes.update(loop_s=float(entry["loop_s"]),
                           intro_s=float(entry.get("intro_s", 0)), length_s=None)
        t = replace(track, **changes) if changes else track
        alb = (album_meta or {}).get("album", {})
        layers = [{k: alb[k] for k in POLICY_KEYS if k in alb},
                  {k: entry[k] for k in POLICY_KEYS if k in entry}]
        return t, layers
