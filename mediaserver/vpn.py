"""The optional VPN kill switch ([vpn] in config.toml), kept by netwatch
every minute:

- the VPN's interface is the one traffic to the internet leaves through
  when it's a tunnel (utun…, ipsec…, ppp…), or [vpn] interface if set;
  either way it counts as connected only when it exists, is active and
  has an address;
- qBittorrent: bound to it ("network interface" in its settings), read
  back to confirm. If the VPN drops, the interface goes and qBittorrent
  has no network at all, at once (a guarantee from qBittorrent itself),
  until it's back; when it reconnects under another name, the binding
  follows within a minute;
- SABnzbd can't be bound: it's paused within a minute of the VPN going
  down (not instant), and resumed when it's back unless something else
  still holds it (holds.py: low disk space) or it was paused by hand;
- switched off, qBittorrent is unbound and the VPN's hold released.

Setup writes the settings to $NETWATCH_STATE/vpn.json and applies them at
once (before it finishes); netwatch keeps them. The state goes to
vpn-status.json for the dashboard.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver import holds
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


def interface_up(name: str) -> bool:
    """<name> exists, is active and has an address (an interface that
    carries traffic, not just a name in config.toml)"""
    try:
        r = subprocess.run(["/sbin/ifconfig", name], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode or "status: inactive" in r.stdout:
        return False
    return bool(re.search(r"^\s*inet\s", r.stdout, re.M) or re.search(r"^\s*inet6\s+(?!fe80)", r.stdout, re.M))


class Clients(holds.Clients):
    """holds.Clients, and qBittorrent's interface binding"""

    def bound(self) -> str | None:
        r = c.request(local("qbittorrent") + "/api/v2/app/preferences", timeout=15)
        prefs = r.json(None) if r.ok else None
        return prefs.get("current_network_interface", "") if isinstance(prefs, dict) else None

    def bind(self, interface: str) -> bool:
        import json
        r = c.request(local("qbittorrent") + "/api/v2/app/setPreferences", "POST",
                      form={"json": json.dumps({"current_network_interface": interface, "current_interface_address": ""})}, timeout=15)
        return r.ok


def keep(state: Path, clients: Clients, detect=route_interface, is_up=interface_up, now: float | None = None) -> str:
    """One round: "off", "up", "down" or "" (qBittorrent didn't answer).
    "up"/"down" with protected: qBittorrent read back as bound to the
    interface it should be; SABnzbd is held (holds.py, "vpn") while down"""
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
        holds.reconcile(state.parent, clients, "vpn", False)
        (state / "vpn-status.json").unlink(missing_ok=True)
        return "off"
    fixed = settings.get("interface") or ""
    found = fixed or detect()
    up = bool(found) and (fixed != "" or found.startswith(TUNNELS)) and is_up(found)
    # Down: qBittorrent stays bound to the VPN's interface (gone, so no
    # traffic); bound to nothing yet means a name that isn't there
    want = found if up else (status.get("interface") or fixed or (bound if bound.startswith(TUNNELS) else NONE_YET))
    if bound != want:
        clients.bind(want)
        bound = clients.bound()   # read back: protected only if it took
    protected = bound == want
    held = holds.reconcile(state.parent, clients, "vpn", not up, {"interface": want})
    if up and not status.get("up") and status:
        c.log(f"VPN back on {want}")
    if not up and status.get("up", True):
        c.log("VPN down: downloads blocked until it's back")
        c.notify("Media server: VPN down", "Downloads are blocked until the VPN reconnects.")
    if not protected:
        c.log(f"qBittorrent didn't take the binding to {want}; retried next round")
    c.write_json(state / "vpn-status.json", {"up": up, "interface": want, "bound_here": True, "protected": protected,
                                              "sabnzbd_confirmed": held, "since": status.get("since") if status.get("up") == up else int(now)})
    return "up" if up else "down"
