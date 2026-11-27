"""Online music libraries: remote catalogs, downloads on demand and the cache.

Each source lists what it has per system (console) into the `remote` table of the SQLite
catalog. The web UI shows those entries next to the local albums ("Local + online").
Playing one downloads and unpacks it to

    /library/_synced/<source>/<system>/<archive name>/

with a `.source.json` that carries title, year and publisher into album.toml generation.

States of an entry:
  remote       listed only
  queued, downloading, error
  temporary    downloaded to listen; removed least-recently-played first when the cache
               is larger than temp_limit_gb
  pinned       added to the library; never removed automatically

Sources are run by volunteers: one download at a time, a pause between requests, and lists
are read again at most once per refresh_hours.

Settings come from config.toml ([joshw]) and can be changed on the Settings page, which
stores them in /meta/_settings.toml under [online].
"""
from __future__ import annotations

import html
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urljoin

log = logging.getLogger("online")

USER_AGENT = "retrorender (+https://github.com/JonteP/retrorender)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS remote (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,          -- 'joshw'
    system      TEXT NOT NULL,          -- e.g. 'wii'
    name        TEXT NOT NULL,          -- archive file name
    url         TEXT NOT NULL,
    size        TEXT,                   -- as listed, e.g. '182M'
    modified    TEXT,
    title       TEXT, alt_title TEXT, date TEXT, developer TEXT, publisher TEXT, tags TEXT,
    state       TEXT DEFAULT 'remote',  -- remote | queued | downloading | temporary | pinned | error
    local_dir   TEXT,                   -- relative to the library root
    error       TEXT,
    last_played REAL,
    seen        REAL,
    UNIQUE(source, system, name)
);
CREATE TABLE IF NOT EXISTS remote_systems (
    source TEXT, system TEXT, refreshed REAL, count INTEGER, error TEXT,
    PRIMARY KEY (source, system)
);
"""


# --- joshw.info ---------------------------------------------------------------------------------
def parse_archive_name(name: str) -> dict:
    """'Fire Emblem - Radiant Dawn [Akatsuki no Megami] (2007-02-22)(IS)(Nintendo)[Wii].7z'"""
    stem = re.sub(r"\.(7z|zip|rar)$", "", name, flags=re.I)
    m = re.match(r"^(?P<title>.*?)\s*(?P<groups>(\([^()]*\)\s*)+)(?P<rest>.*)$", stem)
    out = {"title": stem.strip(), "alt_title": "", "date": "", "developer": "",
           "publisher": "", "tags": ""}
    if not m:
        return out
    title = m.group("title").strip()
    alt = re.findall(r"\[([^\]]+)\]", title)
    tags = [a for a in alt if a.lower() in ("wiiware", "virtual console", "eshop", "dlc")]
    alt_names = [a for a in alt if a not in tags]
    title = re.sub(r"\s*\[[^\]]+\]", "", title).strip()
    groups = re.findall(r"\(([^()]*)\)", m.group("groups"))
    if groups and re.match(r"^\d{4}(-\d\d){0,2}$", groups[0]):
        out["date"] = groups.pop(0)
    if groups:
        dev = groups.pop(0)
        out["developer"] = "" if dev == "-" else dev
    if groups:
        pub = groups.pop(0)
        out["publisher"] = "" if pub == "-" else pub
    rest = re.findall(r"\[([^\]]+)\]", m.group("rest"))
    tags += rest[1:]                                   # rest[0] is the platform, e.g. [Wii]
    out.update(title=title or stem, alt_title=alt_names[0] if alt_names else "",
               tags=",".join(t for t in tags if t))
    return out


def parse_index(page: str, base: str) -> tuple[list[str], list[tuple[str, str, str, str]]]:
    """Apache index -> (sub-directory URLs, [(file name, url, modified, size)])."""
    dirs, files = [], []
    for m in re.finditer(r'<a href="([^"?#]+)">([^<]*)</a>\s*([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{5})?'
                         r'\s*([0-9.]+[KMGT]?|-)?', page):
        href, mod, size = m.group(1), m.group(3) or "", m.group(4) or ""
        if href.startswith(("/", "http", "..")):
            continue
        name = html.unescape(unquote(href))
        if href.endswith("/"):
            dirs.append(urljoin(base, href))
        elif re.search(r"\.(7z|zip)$", name, re.I):
            files.append((name, urljoin(base, href), mod, size))
    return dirs, files


class JoshwSource:
    """joshw.info: one subdomain per system (wii.joshw.info …), an Apache index with one
    folder per letter, one archive per game named by the ripping guidelines."""
    name = "joshw"
    label = "joshw.info"
    description = "Rips of console game music: Nintendo, Sega, Sony and more, one archive per game"
    SYSTEMS = {
        "nsf": "Nintendo NES", "spc": "Super Nintendo", "usf": "Nintendo 64",
        "gcn": "Nintendo GameCube", "wii": "Nintendo Wii", "wiiu": "Nintendo Wii U",
        "switch": "Nintendo Switch", "gbs": "Nintendo Game Boy", "gsf": "Game Boy Advance",
        "2sf": "Nintendo DS", "3ds": "Nintendo 3DS", "smd": "Sega Mega Drive",
        "ssf": "Sega Saturn", "dsf": "Sega Dreamcast", "psf": "PlayStation",
        "psf2": "PlayStation 2", "psf3": "PlayStation 3", "psp": "PlayStation Portable",
        "kss": "MSX", "s98": "PC-98", "pc": "PC", "fmtowns": "FM Towns", "hoot": "Hoot",
    }

    def __init__(self, cfg: dict):
        jc = cfg.get("joshw", {})
        self.platforms = {**self.SYSTEMS, **jc.get("platforms", {})}
        self.base_tpl = str(jc.get("base_url", "https://{system}.joshw.info/"))

    def platform(self, system: str) -> str:
        return str(self.platforms.get(system, system))

    def list_system(self, system: str, get) -> list[dict]:
        base = self.base_tpl.format(system=system)
        top_dirs, files = parse_index(get(base), base)
        for d in top_dirs:
            _, f2 = parse_index(get(d), d)
            files += f2
        out = []
        for name, url, mod, size in files:
            out.append({"name": name, "url": url, "size": size, "modified": mod,
                        **parse_archive_name(name)})
        return out


SOURCES = {"joshw": JoshwSource}


def safe_dirname(name: str) -> str:
    stem = re.sub(r"\.(7z|zip|rar)$", "", name, flags=re.I)
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .") or "unnamed"


def size_bytes(s: str) -> int:
    m = re.match(r"^([0-9.]+)([KMGT]?)$", s or "")
    if not m:
        return 0
    return int(float(m.group(1)) * 1024 ** " KMGT".index(m.group(2) or " "))


# --- the manager --------------------------------------------------------------------------------
class Online:
    def __init__(self, cfg: dict, catalog, library_root: Path, on_change, settings=None,
                 worker: bool = True):
        jc = cfg.get("joshw", {})
        self.cfg_defaults = {
            "temp_limit_gb": float(jc.get("temp_limit_gb", 20)),
            "sources": {"joshw": {"enabled": bool(jc.get("enabled", False)),
                                  "systems": list(jc.get("systems", ["wii", "gcn", "nsf"]))}},
        }
        self.refresh_hours = float(jc.get("refresh_hours", 24))
        self.delay = float(jc.get("delay_s", 2))
        self.settings = settings
        self.sources = {k: cls(cfg) for k, cls in SOURCES.items()}
        self.cat = catalog
        self.root = library_root
        self.synced = library_root / "_synced"
        self.on_change = on_change          # called after downloads (re-index)
        self.jobs: "queue.Queue[tuple]" = queue.Queue()        # list reads
        self.fetches: "queue.Queue[tuple]" = queue.Queue()     # downloads: always first
        self._wake = threading.Event()
        self._listing = False
        self.current: dict | None = None
        self.log: list[str] = []
        self._last_request = 0.0
        self._local = None                  # local_dir -> (id, state), rebuilt on change
        self._pin_after: set = set()        # "add to library" while still downloading
        if self.cat.enabled:
            with self.cat._lock:
                self.cat.db.executescript(SCHEMA)
                # downloads that were queued or running when the app stopped start over
                self.cat.db.execute("UPDATE remote SET state='remote', error=NULL"
                                    " WHERE state IN ('queued', 'downloading')")
                self.cat.db.commit()
            if worker:
                threading.Thread(target=self._worker, daemon=True, name="online").start()

    # --- settings --------------------------------------------------------------------------
    def conf(self) -> dict:
        """Settings: the Settings page's [online] over config.toml's [joshw]."""
        c = json.loads(json.dumps(self.cfg_defaults))
        user = self.settings.get("online") if self.settings else {}
        if "temp_limit_gb" in user:
            c["temp_limit_gb"] = float(user["temp_limit_gb"])
        for name, sc in (user.get("sources") or {}).items():
            if name in c["sources"] and isinstance(sc, dict):
                c["sources"][name].update({k: v for k, v in sc.items() if k in ("enabled", "systems")})
        return c

    @property
    def enabled(self) -> bool:
        return self.cat.enabled and any(s["enabled"] for s in self.conf()["sources"].values())

    def active(self) -> list[tuple[str, str]]:
        """(source, system) pairs that are switched on."""
        if not self.cat.enabled:
            return []
        return [(n, s) for n, sc in self.conf()["sources"].items() if sc["enabled"]
                for s in sc["systems"]]

    @property
    def temp_limit(self) -> float:
        return self.conf()["temp_limit_gb"] * 1024 ** 3

    @property
    def writable(self) -> bool:
        try:
            self.synced.mkdir(exist_ok=True)
            probe = self.synced / ".writetest"
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False

    def platform(self, system: str, source: str = "joshw") -> str:
        src = self.sources.get(source)
        return src.platform(system) if src else system

    def synced_dirs(self) -> list[tuple[Path, str]]:
        """(folder, console name) for every source/system folder with downloads."""
        out = []
        for sd in sorted(p for p in self.synced.glob("*") if p.is_dir() and p.name in self.sources):
            for d in sorted(p for p in sd.iterdir() if p.is_dir()):
                out.append((d, self.platform(d.name, sd.name)))
        return out

    # --- HTTP ----------------------------------------------------------------------------------
    def _get(self, url: str, dest: Path | None = None):
        if dest is None and self._listing:
            self._run_fetches()             # a download asked for meanwhile goes first
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            return self._http(url, dest)
        finally:
            self._last_request = time.time()

    def _http(self, url: str, dest: Path | None = None):
        """A page as text, or a file saved to dest (with progress in self.current)."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            if dest is None:
                return r.read().decode("utf-8", "replace")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if self.current is not None:
                        self.current.update(done=done, total=total)
        return None

    # --- remote catalogs ---------------------------------------------------------------------------
    def refresh(self, source: str, system: str, force: bool = False):
        self.jobs.put(("refresh", source, system, force))
        self._wake.set()

    def refresh_all(self, force: bool = False):
        for source, system in self.active():
            self.refresh(source, system, force)

    def _refresh(self, source: str, system: str, force: bool):
        row = self._q1("SELECT refreshed FROM remote_systems WHERE source=? AND system=?",
                       (source, system))
        if row and row[0] and not force and time.time() - row[0] < self.refresh_hours * 3600:
            return
        self._note(f"reading the {source} {system} list")
        try:
            self._listing = True
            try:
                entries = self.sources[source].list_system(system, self._get)
            finally:
                self._listing = False
        except Exception as e:      # noqa: BLE001
            # keep the time of the last *successful* read, so a failure is retried soon
            self._exec("INSERT INTO remote_systems VALUES (?, ?, NULL, NULL, ?)"
                       " ON CONFLICT(source, system) DO UPDATE SET error=excluded.error",
                       (source, system, str(e)[:300]))
            self._note(f"{system}: list failed: {e}")
            return
        now = time.time()
        rows = [(source, system, e["name"], e["url"], e["size"], e["modified"], e["title"],
                 e["alt_title"], e["date"], e["developer"], e["publisher"], e["tags"], now)
                for e in entries]
        with self.cat._lock:
            self.cat.db.executemany(
                "INSERT INTO remote (source, system, name, url, size, modified, title, alt_title,"
                " date, developer, publisher, tags, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source, system, name) DO UPDATE SET url=excluded.url,"
                " size=excluded.size, modified=excluded.modified, seen=excluded.seen", rows)
            # entries that disappeared upstream and were never downloaded
            self.cat.db.execute("DELETE FROM remote WHERE source=? AND system=? AND seen<?"
                                " AND state='remote'", (source, system, now - 1))
            self.cat.db.execute("INSERT OR REPLACE INTO remote_systems VALUES (?, ?, ?, ?, NULL)",
                                (source, system, now, len(rows)))
            self.cat.db.commit()
        self._note(f"{system}: {len(rows)} entries listed")

    # --- downloads --------------------------------------------------------------------------------
    def fetch(self, rid: int, pin: bool):
        row = self.get(rid)
        if not row:
            raise KeyError(rid)
        if row["state"] in ("temporary", "pinned"):
            if pin and row["state"] == "temporary":
                self.set_state(rid, "pinned")
            return self.get(rid)
        if row["state"] in ("queued", "downloading"):
            if pin:
                self._pin_after.add(rid)
            return row
        self._exec("UPDATE remote SET state='queued', error=NULL WHERE id=?", (rid,))
        self.fetches.put(("fetch", rid, pin))
        self._wake.set()
        return self.get(rid)

    def _fetch(self, rid: int, pin: bool):
        row = self.get(rid)
        if not row or row["state"] in ("temporary", "pinned"):
            return
        dest = self.synced / row["source"] / row["system"] / safe_dirname(row["name"])
        tmpdir = self.synced / ".tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        archive = tmpdir / row["name"]
        self._exec("UPDATE remote SET state='downloading' WHERE id=?", (rid,))
        self.current = {"id": rid, "name": row["name"], "done": 0,
                        "total": size_bytes(row["size"]), "phase": "downloading"}
        self._note(f"downloading {row['name']}")
        try:
            self._get(row["url"], archive)
            self.current["phase"] = "unpacking"
            work = tmpdir / (dest.name + ".part")
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            self._extract(archive, work)
            (work / ".source.json").write_text(json.dumps({
                "source": row["source"], "system": row["system"], "archive": row["name"],
                "url": row["url"], "album": row["title"], "alt_title": row["alt_title"],
                "date": row["date"], "developer": row["developer"],
                "publisher": row["publisher"], "platform": self.platform(row["system"], row["source"]),
            }, ensure_ascii=False, indent=2), "utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            work.replace(dest)
            rel = str(dest.relative_to(self.root))
            pin = pin or rid in self._pin_after
            self._pin_after.discard(rid)
            self.current["phase"] = "indexing"
            self._exec("UPDATE remote SET state=?, local_dir=?, last_played=? WHERE id=?",
                       ("pinned" if pin else "temporary", rel, time.time(), rid))
            self._note(f"ready: {row['title']}")
            self.evict(keep=rid)
            self.on_change()
        except Exception as e:      # noqa: BLE001
            log.exception("download failed")
            self._exec("UPDATE remote SET state='error', error=? WHERE id=?", (str(e)[:300], rid))
            self._note(f"failed: {row['name']}: {e}")
        finally:
            archive.unlink(missing_ok=True)
            self.current = None

    @staticmethod
    def _extract(archive: Path, dest: Path):
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    target = (dest / info.filename).resolve()
                    if not target.is_relative_to(dest.resolve()):
                        raise ValueError(f"unsafe path in archive: {info.filename}")
                z.extractall(dest)
            return
        exe = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
        if not exe:
            raise RuntimeError("7z is not installed")
        r = subprocess.run([exe, "x", "-y", f"-o{dest}", str(archive)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"7z failed: {(r.stderr or r.stdout).strip()[-300:]}")
        for p in dest.rglob("*"):                      # 7z refuses ../ paths, but be sure
            if not p.resolve().is_relative_to(dest.resolve()):
                raise ValueError(f"unsafe path in archive: {p}")

    def set_state(self, rid: int, state: str):
        self._exec("UPDATE remote SET state=? WHERE id=?", (state, rid))

    def remove(self, rid: int):
        row = self.get(rid)
        if row and row["local_dir"]:
            target = (self.root / row["local_dir"]).resolve()
            if target.is_relative_to(self.synced.resolve()):
                shutil.rmtree(target, ignore_errors=True)
        self._exec("UPDATE remote SET state='remote', local_dir=NULL WHERE id=?", (rid,))
        self.on_change()

    def evict(self, keep: int | None = None):
        """Remove least-recently-played temporary downloads above the size limit.
        `keep` (the download that just finished) is never evicted by its own arrival."""
        rows = self._qa("SELECT id, local_dir, last_played FROM remote WHERE state='temporary'"
                        " AND id IS NOT ? ORDER BY last_played", (keep,))
        sizes = {r[0]: _dir_size(self.root / r[1]) for r in rows if r[1]}
        keep_size = 0
        if keep is not None:
            k = self.get(keep)
            keep_size = _dir_size(self.root / k["local_dir"]) if k and k["state"] == "temporary" else 0
        total = sum(sizes.values()) + keep_size
        limit = self.temp_limit
        for rid, local, _ in rows:
            if total <= limit:
                break
            self._note(f"removing {local} from the cache")
            total -= sizes.get(rid, 0)
            shutil.rmtree(self.root / local, ignore_errors=True)
            self._exec("UPDATE remote SET state='remote', local_dir=NULL WHERE id=?", (rid,))

    def cache_size(self) -> int:
        rows = self._qa("SELECT local_dir FROM remote WHERE state='temporary'")
        return sum(_dir_size(self.root / r[0]) for r in rows if r[0])

    @staticmethod
    def _local_key(rel_path: str) -> str | None:
        parts = Path(rel_path).parts
        if len(parts) >= 4 and parts[0] == "_synced":
            return str(Path(*parts[:4]))
        return None

    def played(self, rel_path: str):
        """Called from /render: keeps recently played temporary downloads around."""
        local = self._local_key(rel_path)
        if local:
            self._exec("UPDATE remote SET last_played=? WHERE local_dir=?", (time.time(), local))

    def pin_dir(self, rel_path: str) -> bool:
        """Pin the download that contains rel_path (favourites in Subsonic apps)."""
        local = self._local_key(rel_path)
        if local:
            self._exec("UPDATE remote SET state='pinned' WHERE local_dir=? AND state='temporary'",
                       (local,))
            return True
        return False

    def entry_for(self, rel_path: str) -> dict | None:
        """The remote entry a local file was downloaded from, or None."""
        local = self._local_key(rel_path)
        if not local or not self.cat.enabled:
            return None
        if self._local is None:
            self._local = {r[0]: {"id": r[1], "state": r[2]} for r in self._qa(
                "SELECT local_dir, id, state FROM remote WHERE local_dir IS NOT NULL")}
        return self._local.get(local)

    # --- worker ----------------------------------------------------------------------------------
    def _run_fetches(self):
        while True:
            try:
                job = self.fetches.get_nowait()
            except queue.Empty:
                return
            try:
                self._fetch(job[1], job[2])
            except Exception:       # noqa: BLE001
                log.exception("download failed: %s", job)

    def _worker(self):
        """One thread, so one request at a time. Downloads go before list reads, also in
        the middle of reading a long list (a big system takes minutes)."""
        while True:
            self._wake.wait(30)
            self._wake.clear()
            self._run_fetches()
            while True:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._refresh(job[1], job[2], job[3])
                except Exception:       # noqa: BLE001
                    log.exception("list read failed: %s", job)
                self._run_fetches()

    def _note(self, msg: str):
        log.info(msg)
        self.log = (self.log + [time.strftime("%H:%M:%S ") + msg])[-50:]

    # --- queries ----------------------------------------------------------------------------------
    COLS = ("id", "source", "system", "name", "url", "size", "modified", "title", "alt_title",
            "date", "developer", "publisher", "tags", "state", "local_dir", "error", "last_played")

    def get(self, rid: int) -> dict | None:
        r = self._q1(f"SELECT {', '.join(self.COLS)} FROM remote WHERE id=?", (rid,))
        return dict(zip(self.COLS, r)) if r else None

    def search(self, system: str = "", q: str = "", state: str = "", offset: int = 0,
               limit: int = 100, source: str = "", not_local: bool = False,
               platform: str = "", year: str = "", any_system: bool = False,
               order: str = "title") -> tuple[int, list[dict]]:
        """Entries of the switched-on sources and systems (any_system: also ones switched
        off since, e.g. downloads made before)."""
        pairs = self.active()
        if not pairs and not any_system:
            return 0, []
        where = ["1"] if any_system else \
            ["(" + " OR ".join("(source=? AND system=?)" for _ in pairs) + ")"]
        args: list = [] if any_system else [x for p in pairs for x in p]
        if source:
            where.append("source=?"); args.append(source)
        if system:
            where.append("system=?"); args.append(system)
        if platform:
            systems = [s for src, s in pairs if self.platform(s, src) == platform]
            if not systems:
                return 0, []
            where.append(f"system IN ({','.join('?' * len(systems))})"); args += systems
        if state:
            where.append("state=?"); args.append(state)
        if not_local:
            where.append("state NOT IN ('temporary', 'pinned')")
        if year:
            if year.endswith("s") and year[:-1].isdigit():
                where.append("substr(date, 1, 3) = ?"); args.append(year[:3])
            else:
                where.append("substr(date, 1, 4) = ?"); args.append(year[:4])
        for word in q.split():
            where.append("(title LIKE ? OR alt_title LIKE ? OR developer LIKE ? OR publisher LIKE ?)")
            args += [f"%{word}%"] * 4
        w = " AND ".join(where)
        total = self._q1(f"SELECT count(*) FROM remote WHERE {w}", tuple(args))[0]
        rows = self._qa(f"SELECT {', '.join(self.COLS)} FROM remote WHERE {w}"
                        f" ORDER BY {'last_played DESC' if order == 'played' else 'title COLLATE NOCASE'}"
                        " LIMIT ? OFFSET ?",
                        tuple(args) + (limit, offset))
        return total, [dict(zip(self.COLS, r)) for r in rows]

    def counts(self) -> dict:
        """Entries not downloaded, per console and per year (for the library's filters)."""
        out = {"platforms": {}, "years": {}}
        for source, system in self.active():
            n = self._q1("SELECT count(*) FROM remote WHERE source=? AND system=?"
                         " AND state NOT IN ('temporary', 'pinned')", (source, system))[0]
            p = self.platform(system, source)
            out["platforms"][p] = out["platforms"].get(p, 0) + n
            for y, c in self._qa("SELECT substr(date, 1, 4), count(*) FROM remote WHERE source=?"
                                 " AND system=? AND state NOT IN ('temporary', 'pinned')"
                                 " AND date != '' GROUP BY 1", (source, system)):
                if y and y.isdigit():
                    out["years"][int(y)] = out["years"].get(int(y), 0) + c
        return out

    def systems_status(self) -> list[dict]:
        out = []
        conf = self.conf()
        for name, src in self.sources.items():
            for s in conf["sources"][name]["systems"]:
                r = self._q1("SELECT refreshed, count, error FROM remote_systems"
                             " WHERE source=? AND system=?", (name, s))
                n = self._q1("SELECT count(*), sum(state='pinned'), sum(state='temporary')"
                             " FROM remote WHERE source=? AND system=?", (name, s))
                out.append({"source": name, "system": s, "platform": self.platform(s, name),
                            "refreshed": r[0] if r else None, "listed": n[0] or 0,
                            "pinned": n[1] or 0, "temporary": n[2] or 0,
                            "error": r[2] if r else None})
        return out

    def status(self) -> dict:
        return {"enabled": self.enabled, "writable": self.writable if self.enabled else False,
                "systems": self.systems_status() if self.cat.enabled else [],
                "current": self.current, "queued": self.fetches.qsize(),
                "lists_queued": self.jobs.qsize(), "log": self.log[-15:],
                "temp_limit_gb": round(self.temp_limit / 1024 ** 3, 1),
                "cache_gb": round(self.cache_size() / 1024 ** 3, 2) if self.cat.enabled else 0}

    def describe_sources(self) -> dict:
        conf = self.conf()
        return {name: {"label": src.label, "description": src.description,
                       "known": src.platforms, **conf["sources"][name]}
                for name, src in self.sources.items()}

    def _q1(self, sql, args=()):
        with self.cat._lock:
            return self.cat.db.execute(sql, args).fetchone()

    def _qa(self, sql, args=()):
        with self.cat._lock:
            return self.cat.db.execute(sql, args).fetchall()

    def _exec(self, sql, args=()):
        with self.cat._lock:
            self.cat.db.execute(sql, args)
            self.cat.db.commit()
        self._local = None


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0
