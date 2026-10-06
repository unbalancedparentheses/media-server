"""Downloads held for a reason: "disk" (diskwatch: too little space) or
"vpn" (netwatch: the VPN is down). One record (.state/holds.json) for both,
so neither undoes the other: a client is resumed only when no reason holds
it any more, and SABnzbd paused by hand before any hold stays paused.

What each reason holds: "disk" the downloading torrents and SABnzbd; "vpn"
SABnzbd only (qBittorrent is bound to the VPN's interface instead, see
vpn.py). Each round reconciles with what the clients actually do, so a
pause or resume that didn't take is retried until it shows.
"""
from __future__ import annotations

import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

HOLDS = {"disk": {"torrents", "sabnzbd"}, "vpn": {"sabnzbd"}}
STOPPED = ("pausedDL", "stoppedDL", "pausedUP", "stoppedUP")


class Clients:
    """qBittorrent (it skips its login for this Mac) and SABnzbd; None from
    a read when the client didn't answer"""

    def __init__(self, config: Path):
        self.config = config

    def torrents(self) -> list | None:
        r = c.request(local("qbittorrent") + "/api/v2/torrents/info", timeout=20)
        data = r.json(None) if r.ok else None
        return data if isinstance(data, list) else None

    def torrent_action(self, action: str, hashes: list) -> bool:
        """action "stop"/"start" (qBittorrent 5; 4 calls them pause/resume)"""
        if not hashes:
            return True
        for name in (action, {"stop": "pause", "start": "resume"}[action]):
            r = c.request(local("qbittorrent") + f"/api/v2/torrents/{name}", "POST", form={"hashes": "|".join(hashes)}, timeout=20)
            if r.ok:
                return True
            if r.status != 404:
                return False
        return False

    def has_sabnzbd(self) -> bool:
        return bool(c.sabnzbd_key(self.config))

    def sabnzbd_paused(self) -> bool | None:
        key = c.sabnzbd_key(self.config)
        r = c.request(local("sabnzbd") + f"/api?mode=queue&apikey={key}&output=json", timeout=15)
        queue = (r.json({}) or {}).get("queue") if r.ok else None
        return bool(queue.get("paused")) if isinstance(queue, dict) else None

    def sabnzbd(self, mode: str) -> bool:
        key = c.sabnzbd_key(self.config)
        r = c.request(local("sabnzbd") + f"/api?mode={mode}&apikey={key}&output=json", timeout=15)
        return r.ok and (r.json({}) or {}).get("status") is not False


def file(state: Path) -> Path:
    return state / "holds.json"


def load(state: Path) -> dict:
    data = c.read_json(file(state), {}) or {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("reasons", {})
    data.setdefault("torrents", [])
    return data


def held(state: Path) -> dict:
    """reason → its details, for the dashboard"""
    return load(state)["reasons"]


def reconcile(state: Path, clients: Clients, reason: str, on: bool, details: dict | None = None) -> bool:
    """Hold (on) or release <reason>, then make the clients match every
    reason held; True when they do (else it's retried next round)"""
    data = load(state)
    reasons = data["reasons"]
    if on:
        reasons[reason] = dict(details or {}, since=reasons.get(reason, {}).get("since", int(time.time())))
    else:
        reasons.pop(reason, None)
    wanted = set().union(*(HOLDS[r] for r in reasons)) if reasons else set()
    ok = True

    # Torrents: every downloading one stopped while held (also ones started
    # since); the ones stopped here started again when nothing holds them
    torrents = clients.torrents() if ("torrents" in wanted or data["torrents"]) else []
    if torrents is None:
        ok = False
    elif "torrents" in wanted:
        moving = [t["hash"] for t in torrents if t.get("hash") and t.get("state") not in STOPPED and t.get("progress", 0) < 1]
        data["torrents"] = sorted(set(data["torrents"]) | set(moving))
        c.write_json(file(state), data)   # recorded before stopping: resumed later whatever happens
        if moving and not clients.torrent_action("stop", moving):
            ok = False
        ok = ok and not moving   # confirmed on a later round, once none moves
    elif data["torrents"]:
        # Ours that are still stopped get started; the record goes once a
        # read shows none of them stopped (or they're gone)
        present = {t.get("hash"): t.get("state") for t in torrents}
        still = [h for h in data["torrents"] if present.get(h) in STOPPED]
        data["torrents"] = still
        if still:
            clients.torrent_action("start", still)
            ok = False   # confirmed on a later round

    # SABnzbd: paused while held (a pause by hand before any hold is kept)
    if clients.has_sabnzbd():
        paused = clients.sabnzbd_paused()
        if paused is None:
            ok = False
        elif "sabnzbd" in wanted:
            if not data.get("sabnzbd_held"):
                data["sabnzbd_by_hand"] = paused   # already paused before any hold: never resumed here
                data["sabnzbd_held"] = True
            if not paused and not clients.sabnzbd("pause"):
                ok = False
        elif data.get("sabnzbd_held"):
            if not data.get("sabnzbd_by_hand") and paused and not clients.sabnzbd("resume"):
                ok = False
            else:
                data.pop("sabnzbd_held", None)
                data.pop("sabnzbd_by_hand", None)

    if reasons or data["torrents"] or data.get("sabnzbd_held"):
        c.write_json(file(state), data)
    else:
        file(state).unlink(missing_ok=True)
    return ok
