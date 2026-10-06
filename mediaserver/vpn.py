"""The optional VPN kill switch ([vpn] in config.toml), kept by netwatch
every minute:

- the VPN's interface is the one traffic to the internet leaves through
  when it's a tunnel (utun…, ipsec…, ppp…), or [vpn] interface if set;
- qBittorrent is bound to it ("network interface" in its settings): if the
  VPN drops, the interface goes and qBittorrent has no network at all, at
  once, until it's back (when it reconnects under another name, the
  binding follows);
- SABnzbd can't be bound, so it's paused while the VPN is down and resumed
  after (only if it was paused here);
- switched off, qBittorrent is unbound and SABnzbd resumed.

Setup writes the settings to $NETWATCH_STATE/vpn.json; the state goes to
vpn-status.json for the dashboard.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import local

TUNNELS = ("utun", "ipsec", "ppp", "tun", "wg")
# With the VPN down and no interface ever seen: a name that doesn't exist,
# so qBittorrent has no network rather than the normal one
NONE_YET = "vpn-not-connected"


def route_interface(target: str = "1.1.1.1") -> str:
    """The interface traffic to <target> leaves through ("" when unknown)"""
    try:
        out = subprocess.run(["/sbin/route", "-n", "get", target], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"^\s*interface:\s*(\S+)", out, re.M)
    return m.group(1) if m else ""


class Clients:
    """qBittorrent (skips its login for this Mac) and SABnzbd"""

    def __init__(self, config: Path):
        self.config = config

    def bound(self) -> str | None:
        r = c.request(local("qbittorrent") + "/api/v2/app/preferences", timeout=15)
        prefs = r.json(None) if r.ok else None
        return prefs.get("current_network_interface", "") if isinstance(prefs, dict) else None

    def bind(self, interface: str) -> bool:
        import json
        r = c.request(local("qbittorrent") + "/api/v2/app/setPreferences", "POST",
                      form={"json": json.dumps({"current_network_interface": interface, "current_interface_address": ""})}, timeout=15)
        return r.ok

    def sabnzbd(self, mode: str) -> bool:
        key = c.sabnzbd_key(self.config)
        if not key:
            return True
        r = c.request(local("sabnzbd") + f"/api?mode={mode}&apikey={key}&output=json", timeout=15)
        return r.ok and (r.json({}) or {}).get("status") is not False


def keep(state: Path, clients: Clients, detect=route_interface, now: float | None = None) -> str:
    """One round: "off", "up", "down" or "" (qBittorrent didn't answer)"""
    now = time.time() if now is None else now
    settings = c.read_json(state / "vpn.json", {}) or {}
    status = c.read_json(state / "vpn-status.json", {}) or {}
    if not settings.get("enabled") and not status:
        return "off"   # off, and nothing of ours to undo: nothing is asked
    bound = clients.bound()
    if bound is None:
        return ""
    if not settings.get("enabled"):
        if status.get("bound_here") and bound:
            clients.bind("")   # back to every interface
        if status.get("sabnzbd_paused"):
            clients.sabnzbd("resume")
        (state / "vpn-status.json").unlink(missing_ok=True)
        return "off"
    found = settings.get("interface") or detect()
    up = bool(found) and (bool(settings.get("interface")) or found.startswith(TUNNELS))
    if up:
        if bound != found and clients.bind(found):
            c.log(f"VPN on {found}: qBittorrent bound to it")
        if status.get("sabnzbd_paused") and clients.sabnzbd("resume"):
            status["sabnzbd_paused"] = False
        new = {"up": True, "interface": found, "bound_here": True, "sabnzbd_paused": status.get("sabnzbd_paused", False),
               "since": status.get("since") if status.get("up") else int(now)}
    else:
        # Down: qBittorrent stays bound to the VPN's interface (gone now, so
        # no traffic); bound to nothing yet means bound to a name that isn't there
        keep_on = status.get("interface") or (bound if bound and bound.startswith(TUNNELS) else NONE_YET)
        if bound != keep_on:
            clients.bind(keep_on)
        paused = status.get("sabnzbd_paused") or clients.sabnzbd("pause")
        if status.get("up", True):
            c.log("VPN down: downloads blocked until it's back")
            c.notify("Media server: VPN down", "Downloads are blocked until the VPN reconnects.")
        new = {"up": False, "interface": keep_on, "bound_here": True, "sabnzbd_paused": bool(paused),
               "since": status.get("since") if status.get("up") is False else int(now)}
    c.write_json(state / "vpn-status.json", new)
    return "up" if up else "down"
