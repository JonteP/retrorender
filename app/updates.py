"""Tells the web UI when a newer image has been built than the one that is running.

Asks the GitHub API (public repo, no token) for the latest successful run of the image
workflow on main. That run's commit is what `:latest` contains; if it differs from the
commit this image was built from, a restart of the app pulls the new version.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("retrorender.updates")


class UpdateCheck:
    def __init__(self, cfg: dict, build: dict):
        c = cfg.get("updates", {}) or {}
        self.enabled = c.get("check", True) and build.get("commit") not in ("", "local")
        self.repo = c.get("repo", "JonteP/retrorender")
        self.workflow = c.get("workflow", "docker.yml")
        self.interval = max(60, int(c.get("interval_minutes", 5)) * 60)
        self.current = build.get("commit", "")
        self.latest: dict | None = None
        self.checked = 0.0
        self.error = ""

    def start(self):
        if not self.enabled:
            return

        def loop():
            while True:
                self.check()
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True, name="update-check").start()

    def check(self):
        url = (f"https://api.github.com/repos/{self.repo}/actions/workflows/{self.workflow}/runs"
               "?branch=main&event=push&status=success&per_page=1")
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "retrorender"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                runs = json.load(r).get("workflow_runs", [])
            if runs:
                run = runs[0]
                self.latest = {"commit": run["head_sha"][:7],
                               "title": (run.get("display_title") or "").split("\n")[0],
                               "date": run.get("updated_at", "")}
            self.error = ""
        except Exception as e:      # noqa: BLE001 - offline or rate limited: try again later
            self.error = str(e)
            log.info("update check: %s", e)
        self.checked = time.time()

    def status(self) -> dict:
        avail = bool(self.enabled and self.latest and self.latest["commit"] != self.current)
        return {"enabled": self.enabled, "available": avail, "current": self.current,
                "latest": self.latest, "checked": self.checked, "error": self.error}
