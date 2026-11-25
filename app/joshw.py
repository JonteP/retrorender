"""joshw.info integration: remote catalog, downloads and the temporary/pinned model.

joshw.info has one subdomain per system (wii.joshw.info, gcn.joshw.info, nsf.joshw.info,
...). Each is an Apache directory index with one folder per letter ("0-9/", "a/", ...)
and one archive per game, named by the ripping guidelines:

    Game Name [name in other region] (YYYY-MM-DD)(Developer)(Publisher)[Platform].7z

Remote entries live in the `remote` table of the SQLite catalog. A download is unpacked
to /library/_synced/joshw/<system>/<archive name>/ together with a `.source.json` that
carries title, year and publisher into album.toml generation.

States:
  temporary  fetched to listen; evicted least-recently-played first above a size limit
  pinned     added to the library; never evicted automatically

The site is run by volunteers: one download at a time, a pause between requests, and
directory listings are refreshed at most once per `refresh_hours`.
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
from urllib.parse import quote, unquote, urljoin

log = logging.getLogger("joshw")

USER_AGENT = "retrorender/0.8 (+https://github.com/JonteP/retrorender)"

# subdomain -> console name (genre in MA); can be overridden in [joshw.platforms]
DEFAULT_PLATFORMS = {
    "wii": "Nintendo Wii", "gcn": "Nintendo GameCube", "wiiu": "Nintendo Wii U",
    "nsf": "Nintendo NES", "spc": "Super Nintendo", "usf": "Nintendo 64",
    "gbs": "Nintendo Game Boy", "gsf": "Game Boy Advance", "2sf": "Nintendo DS",
    "3ds": "Nintendo 3DS", "switch": "Nintendo Switch", "smd": "Sega Mega Drive",
    "ssf": "Sega Saturn", "dsf": "Sega Dreamcast", "psf": "PlayStation",
    "psf2": "PlayStation 2", "psf3": "PlayStation 3", "psp": "PlayStation Portable",
    "kss": "MSX", "s98": "PC-98", "pc": "PC", "fmtowns": "FM Towns", "hoot": "Hoot",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS remote (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,          -- 'joshw'
    system      TEXT NOT NULL,          -- subdomain, e.g. 'wii'
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


# --- archive names -----------------------------------------------------------------------
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


def safe_dirname(name: str) -> str:
    stem = re.sub(r"\.(7z|zip|rar)$", "", name, flags=re.I)
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .") or "unnamed"


class Joshw:
    def __init__(self, cfg: dict, catalog, library_root: Path, on_change):
        jc = cfg.get("joshw", {})
        self.enabled = bool(jc.get("enabled", False)) and catalog.enabled
        self.systems = list(jc.get("systems", ["wii", "gcn", "nsf"]))
        self.platforms = {**DEFAULT_PLATFORMS, **jc.get("platforms", {})}
        self.refresh_hours = float(jc.get("refresh_hours", 24))
        self.delay = float(jc.get("delay_s", 2))
        self.temp_limit = float(jc.get("temp_limit_gb", 20)) * 1024 ** 3
        self.base_tpl = str(jc.get("base_url", "https://{system}.joshw.info/"))
        self.cat = catalog
        self.root = library_root
        self.synced = library_root / "_synced"
        self.on_change = on_change          # called after downloads (re-index)
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.current: dict | None = None
        self.log: list[str] = []
        self._last_request = 0.0
        if self.enabled:
            with self.cat._lock:
                self.cat.db.executescript(SCHEMA)
                self.cat.db.commit()
            threading.Thread(target=self._worker, daemon=True, name="joshw").start()

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

    def platform(self, system: str) -> str:
        return str(self.platforms.get(system, system))

    # --- HTTP ----------------------------------------------------------------------------
    def _get(self, url: str, dest: Path | None = None):
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
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
        finally:
            self._last_request = time.time()

    # --- remote catalog --------------------------------------------------------------------
    def refresh(self, system: str, force: bool = False):
        self.jobs.put(("refresh", system, force))

    def _refresh(self, system: str, force: bool):
        row = self._q1("SELECT refreshed FROM remote_systems WHERE source='joshw' AND system=?",
                       (system,))
        if row and row[0] and not force and time.time() - row[0] < self.refresh_hours * 3600:
            return
        base = self.base_tpl.format(system=system)
        self._note(f"reading the {system} index")
        try:
            top_dirs, files = parse_index(self._get(base), base)
            for d in top_dirs:
                _, f2 = parse_index(self._get(d), d)
                files += f2
        except Exception as e:
            self._exec("INSERT OR REPLACE INTO remote_systems VALUES ('joshw', ?, ?, ?, ?)",
                       (system, time.time(), None, str(e)))
            self._note(f"{system}: index failed: {e}")
            return
        now = time.time()
        rows = []
        for name, url, mod, size in files:
            p = parse_archive_name(name)
            rows.append(("joshw", system, name, url, size, mod, p["title"], p["alt_title"],
                         p["date"], p["developer"], p["publisher"], p["tags"], now))
        with self.cat._lock:
            self.cat.db.executemany(
                "INSERT INTO remote (source, system, name, url, size, modified, title, alt_title,"
                " date, developer, publisher, tags, seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source, system, name) DO UPDATE SET url=excluded.url,"
                " size=excluded.size, modified=excluded.modified, seen=excluded.seen", rows)
            # archives that disappeared upstream and were never downloaded
            self.cat.db.execute("DELETE FROM remote WHERE source='joshw' AND system=? AND seen<?"
                                " AND state='remote'", (system, now - 1))
            self.cat.db.execute("INSERT OR REPLACE INTO remote_systems VALUES"
                                " ('joshw', ?, ?, ?, NULL)", (system, now, len(rows)))
            self.cat.db.commit()
        self._note(f"{system}: {len(rows)} archives listed")

    def refresh_all(self, force: bool = False):
        for s in self.systems:
            self.refresh(s, force)

    # --- downloads -------------------------------------------------------------------------
    def fetch(self, rid: int, pin: bool):
        row = self.get(rid)
        if not row:
            raise KeyError(rid)
        if row["state"] in ("temporary", "pinned"):
            if pin and row["state"] == "temporary":
                self.set_state(rid, "pinned")
            return row
        self._exec("UPDATE remote SET state='queued', error=NULL WHERE id=?", (rid,))
        self.jobs.put(("fetch", rid, pin))
        return self.get(rid)

    def _fetch(self, rid: int, pin: bool):
        row = self.get(rid)
        if not row or row["state"] in ("temporary", "pinned"):
            return
        dest = self.synced / "joshw" / row["system"] / safe_dirname(row["name"])
        tmpdir = self.synced / ".tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        archive = tmpdir / row["name"]
        self._exec("UPDATE remote SET state='downloading' WHERE id=?", (rid,))
        self.current = {"id": rid, "name": row["name"], "done": 0, "total": 0}
        self._note(f"downloading {row['name']}")
        try:
            self._get(row["url"], archive)
            work = tmpdir / (dest.name + ".part")
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            self._extract(archive, work)
            (work / ".source.json").write_text(json.dumps({
                "source": "joshw", "system": row["system"], "archive": row["name"],
                "url": row["url"], "album": row["title"], "alt_title": row["alt_title"],
                "date": row["date"], "developer": row["developer"],
                "publisher": row["publisher"], "platform": self.platform(row["system"]),
            }, ensure_ascii=False, indent=2), "utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            work.replace(dest)
            rel = str(dest.relative_to(self.root))
            self._exec("UPDATE remote SET state=?, local_dir=?, last_played=? WHERE id=?",
                       ("pinned" if pin else "temporary", rel, time.time(), rid))
            self._note(f"ready: {row['title']}")
            self.evict(keep=rid)
            self.on_change()
        except Exception as e:
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
        total = sum(sizes.values())
        for rid, local, _ in rows:
            if total <= self.temp_limit:
                break
            self._note(f"evicting {local}")
            total -= sizes.get(rid, 0)
            shutil.rmtree(self.root / local, ignore_errors=True)
            self._exec("UPDATE remote SET state='remote', local_dir=NULL WHERE id=?", (rid,))

    def played(self, rel_path: str):
        """Called from /render: keeps recently played temporary downloads around."""
        parts = Path(rel_path).parts
        if len(parts) >= 4 and parts[0] == "_synced" and parts[1] == "joshw":
            local = str(Path(*parts[:4]))
            self._exec("UPDATE remote SET last_played=? WHERE local_dir=?", (time.time(), local))

    def pin_dir(self, rel_path: str) -> bool:
        """Pin the download that contains rel_path (used for favourites in MA)."""
        parts = Path(rel_path).parts
        if len(parts) >= 4 and parts[0] == "_synced" and parts[1] == "joshw":
            local = str(Path(*parts[:4]))
            self._exec("UPDATE remote SET state='pinned' WHERE local_dir=? AND state='temporary'",
                       (local,))
            return True
        return False

    # --- worker ------------------------------------------------------------------------------
    def _worker(self):
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "refresh":
                    self._refresh(job[1], job[2])
                elif job[0] == "fetch":
                    self._fetch(job[1], job[2])
            except Exception:
                log.exception("joshw job failed: %s", job)

    def _note(self, msg: str):
        log.info(msg)
        self.log = (self.log + [time.strftime("%H:%M:%S ") + msg])[-50:]

    # --- queries -----------------------------------------------------------------------------
    COLS = ("id", "system", "name", "url", "size", "modified", "title", "alt_title", "date",
            "developer", "publisher", "tags", "state", "local_dir", "error", "last_played")

    def get(self, rid: int) -> dict | None:
        r = self._q1(f"SELECT {', '.join(self.COLS)} FROM remote WHERE id=?", (rid,))
        return dict(zip(self.COLS, r)) if r else None

    def search(self, system: str = "", q: str = "", state: str = "", offset: int = 0,
               limit: int = 100) -> tuple[int, list[dict]]:
        where, args = ["source='joshw'"], []
        if system:
            where.append("system=?"); args.append(system)
        if state:
            where.append("state=?"); args.append(state)
        for word in q.split():
            where.append("(title LIKE ? OR alt_title LIKE ? OR developer LIKE ? OR publisher LIKE ?)")
            args += [f"%{word}%"] * 4
        w = " AND ".join(where)
        total = self._q1(f"SELECT count(*) FROM remote WHERE {w}", tuple(args))[0]
        rows = self._qa(f"SELECT {', '.join(self.COLS)} FROM remote WHERE {w}"
                        " ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                        tuple(args) + (limit, offset))
        return total, [dict(zip(self.COLS, r)) for r in rows]

    def systems_status(self) -> list[dict]:
        out = []
        for s in self.systems:
            r = self._q1("SELECT refreshed, count, error FROM remote_systems"
                         " WHERE source='joshw' AND system=?", (s,))
            n = self._q1("SELECT count(*), sum(state='pinned'), sum(state='temporary')"
                         " FROM remote WHERE source='joshw' AND system=?", (s,))
            out.append({"system": s, "platform": self.platform(s),
                        "refreshed": r[0] if r else None, "listed": n[0] or 0,
                        "pinned": n[1] or 0, "temporary": n[2] or 0,
                        "error": r[2] if r else None})
        return out

    def status(self) -> dict:
        return {"enabled": self.enabled, "writable": self.writable if self.enabled else False,
                "systems": self.systems_status() if self.enabled else [],
                "current": self.current, "queued": self.jobs.qsize(), "log": self.log[-15:],
                "temp_limit_gb": round(self.temp_limit / 1024 ** 3, 1)}

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


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0
