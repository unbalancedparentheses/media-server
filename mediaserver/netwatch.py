"""netwatch: keeps the stack sensible when the Mac goes offline (e.g. on a
flight) and comes back. Runs as a launchd agent (see flake.nix).

Every NETWATCH_INTERVAL seconds (60) it checks the connection and then
reconciles, rather than acting once per transition, so a failed step is
simply retried on the next round:
  - offline (3 failed checks in a row, so blips don't count): Cleanuparr's
    queue cleaner must be off; otherwise it takes every download for
    stalled, removes it and blocklists the release.
  - online: the queue cleaner must be what config.toml says, which setup
    writes to $NETWATCH_STATE/cleanuparr-wanted ("true"/"false").
  - before the first successful check nothing is changed, so a restart
    while offline never switches the cleaner back on.
  - while an install or other operation holds setup's lock, it only
    watches: setup is configuring Cleanuparr itself.
Coming back online also re-tests the indexers (Prowlarr backs off for up
to a day after failures, and Sonarr/Radarr then can't search) and clears
Bazarr's provider throttling.
The current state is written to $NETWATCH_STATE/connection for setup.

Environment: NETWATCH_CONFIG (~/media/config), NETWATCH_STATE,
NETWATCH_INTERVAL, NETWATCH_SIMULATE_OFFLINE (a file whose presence means
offline, for tests), NETWATCH_CLEANUPARR_URL, NETWATCH_LOCK.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

PROBES = ("https://www.gstatic.com/generate_204", "https://cloudflare.com/cdn-cgi/trace")


class Netwatch:
    def __init__(self, config: Path, state: Path, cleanuparr_url: str, lock: Path):
        self.config, self.state_dir, self.cleanuparr_url, self.lock = config, state, cleanuparr_url, lock
        self.state = "unknown"   # unknown | online | offline
        self.misses = 0
        self.reconnected = False
        self.waiting = False
        self.unknown_said = False

    def probe(self) -> bool:
        simulated = os.environ.get("NETWATCH_SIMULATE_OFFLINE")
        if simulated:
            return not os.path.exists(simulated)
        for url in PROBES:
            try:
                c.http(url, timeout=10)
                return True
            except c.HTTP_ERRORS:
                continue
        return False

    def wanted(self) -> bool | None:
        """What the queue cleaner should be while online (config.toml, via
        setup); None when that can't be read: left as it is, not guessed"""
        value = c.read_text(self.state_dir / "cleanuparr-wanted", "")
        return {"true": True, "false": False}.get(value)

    def cleaner(self, method: str = "GET", body: dict | None = None) -> dict:
        key = c.cleanuparr_key(self.config)
        if not key:
            raise OSError("no Cleanuparr API key")
        raw = c.http(f"{self.cleanuparr_url}/api/configuration/queue_cleaner", {"X-Api-Key": key},
                     method=method, body=body)
        return json.loads(raw) if raw else {}

    def set_cleaner(self, on: bool) -> bool:
        """Make Cleanuparr's queue cleaner enabled = on; False if it couldn't
        be read or set (retried next round)"""
        try:
            conf = self.cleaner()
            if conf.get("enabled") == on:
                return True
            self.cleaner("PUT", dict(conf, enabled=on))
        except c.HTTP_ERRORS:
            return False
        c.log("resumed Cleanuparr's queue cleaner" if on else "paused Cleanuparr's queue cleaner")
        return True

    def after_reconnect(self) -> None:
        """Re-test the indexers (can take minutes, so in the background; the
        apps answer 400 when some indexer still fails, which still means the
        test ran) and clear Bazarr's provider throttling"""
        def retest(name: str, url: str, key: str) -> None:
            if not key:
                return
            try:
                c.http(url, {"X-Api-Key": key}, method="POST", timeout=900)
            except c.urllib.error.HTTPError:
                pass  # some indexer still failing; the test ran
            except c.HTTP_ERRORS:
                c.log(f"couldn't re-test {name}'s indexers")
                return
            c.log(f"re-tested {name}'s indexers")

        def all_tests() -> None:
            retest("Prowlarr", local("prowlarr") + "/api/v1/indexer/testall", c.arr_key(self.config, "prowlarr"))
            apps = [threading.Thread(target=retest, args=(name, url, c.arr_key(self.config, app)), daemon=True)
                    for name, url, app in (("Sonarr", local("sonarr") + "/api/v3/indexer/testall", "sonarr"),
                                           ("Radarr", local("radarr") + "/api/v3/indexer/testall", "radarr"))]
            for t in apps:
                t.start()
            for t in apps:
                t.join()
        threading.Thread(target=all_tests, daemon=True).start()
        key = c.bazarr_key(self.config)
        if key:
            try:
                c.http(local("bazarr") + f"/api/providers?apikey={key}", method="POST",
                       form={"action": "reset"}, timeout=30)
                c.log("cleared Bazarr's provider throttling")
            except c.HTTP_ERRORS:
                pass

    def round(self) -> None:
        before = self.state
        if self.probe():
            self.state, self.misses = "online", 0
        else:
            self.misses += 1
            if self.misses >= 3:
                self.state = "offline"
        if self.state != before:
            c.log(f"connection: {self.state}")
            c.write_atomic(self.state_dir / "connection", self.state + "\n", mode=0o644)
        # Came back online: the re-tests run once nothing else is in the way
        if before == "offline" and self.state == "online":
            self.reconnected = True
        # Setup is changing things: keep watching, don't touch Cleanuparr
        if c.operation_running(self.lock):
            if not self.waiting:
                c.log("an install or other operation is running; leaving Cleanuparr to it")
            self.waiting = True
            return
        self.waiting = False
        if self.state == "offline":
            if not self.set_cleaner(False):
                c.log("couldn't pause Cleanuparr's queue cleaner (retrying)")
        elif self.state == "online":
            wanted = self.wanted()
            if wanted is None:
                if not self.unknown_said:
                    c.log("whether Cleanuparr's queue cleaner should run isn't known (no cleanuparr-wanted); leaving it as it is")
                self.unknown_said = True
            elif not self.set_cleaner(wanted):
                c.log("couldn't set Cleanuparr's queue cleaner (retrying)")
            if self.reconnected:
                self.reconnected = False
                self.after_reconnect()


def main() -> None:
    config = Path(os.environ.get("NETWATCH_CONFIG", c.MEDIA / "config"))
    state = Path(os.environ.get("NETWATCH_STATE", c.MEDIA / ".state/netwatch"))
    nw = Netwatch(config, state, os.environ.get("NETWATCH_CLEANUPARR_URL", local("cleanuparr")),
                  Path(os.environ.get("NETWATCH_LOCK", state.parent / "lock")))
    state.mkdir(parents=True, exist_ok=True)
    (state / "cleanuparr-paused").unlink(missing_ok=True)  # older versions' pause flag
    while True:
        nw.round()
        time.sleep(int(os.environ.get("NETWATCH_INTERVAL", "60")))


if __name__ == "__main__":
    main()
