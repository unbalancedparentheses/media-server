"""Remote access over Tailscale: Jellyfin and Seerr published over HTTPS
with `tailscale serve`, and taken down again (also routes published for an
earlier config, recorded in tailscale-routes.json). The dashboard isn't
published: it answers on this Mac only."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from mediaserver import common as c
from mediaserver.config import Config
from mediaserver.ui import ok, warn

APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def cli() -> str:
    """The tailscale command, or "" when Tailscale isn't installed"""
    return shutil.which("tailscale") or (APP if Path(APP).is_file() else "")


def run(ts: str, *args: str, timeout: float = 10) -> subprocess.CompletedProcess | None:
    """None when it doesn't finish in time"""
    try:
        return subprocess.run([ts, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None


def routes_file(cfg: Config) -> Path:
    return cfg.paths.state / "tailscale-routes.json"


def record(cfg: Config, port: str, target: str) -> None:
    """Remember a route setup published, so it can be removed even after the
    config changes"""
    routes = c.read_json(routes_file(cfg), {})
    routes = routes if isinstance(routes, dict) else {}
    routes[port] = target
    c.write_json(routes_file(cfg), routes, mode=0o600, compact=True)


def wanted(cfg: Config) -> list[tuple[str, str, str]]:
    """(port, target, label) setup publishes"""
    return [("8096", "http://127.0.0.1:8096", "Jellyfin"), ("5055", "http://127.0.0.1:5055", "Seerr")]


def is_dashboard(target: str, port: int) -> bool:
    """A proxy target that is this Mac's dashboard (http://127.0.0.1:80, http://localhost, …)"""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(target if "://" in target else f"http://{target}")
        return (u.hostname or "") in ("127.0.0.1", "localhost", "::1") and (u.port or 80) == port
    except ValueError:
        return False


def forget(cfg: Config, port: str) -> None:
    routes = c.read_json(routes_file(cfg), {})
    if isinstance(routes, dict) and routes.pop(port, None) is not None:
        c.write_json(routes_file(cfg), routes, mode=0o600, compact=True)


def take_down_stale(cfg: Config, ts: str, now: dict[str, list[str]] | None) -> bool:
    """Routes an earlier setup published that it no longer wants (the
    dashboard used to be on :443): removed when still pointing where it put
    them. False when one may still be there (Tailscale's routes couldn't be
    read, or removing failed); its record is kept to try again. nginx and
    the dashboard refuse requests relayed by such a route regardless."""
    recorded = c.read_json(routes_file(cfg), {})
    keep = {port for port, _, _ in wanted(cfg)}
    stale = {p: t for p, t in (recorded.items() if isinstance(recorded, dict) else []) if p not in keep}
    if stale and now is None:
        warn(f"Couldn't read Tailscale's published routes, so HTTPS :{', :'.join(stale)} (the old dashboard route) "
             f"may still be there; check with: tailscale serve status")
        return False
    gone = True
    for port, target in stale.items():
        if target not in (now or {}).get(port, []):
            forget(cfg, port)
        elif (r := run(ts, "serve", f"--https={port}", "off")) and r.returncode == 0:
            forget(cfg, port)
            ok(f"Tailscale HTTPS :{port} removed (no longer published)")
        else:
            warn(f"Couldn't remove Tailscale HTTPS :{port} (remove it with: tailscale serve --https={port} off)")
            gone = False
    return gone


def published(status: dict) -> dict[str, list[str]]:
    """port → the proxy targets published there"""
    out: dict[str, list[str]] = {}
    for host_port, web in (status.get("Web") or {}).items():
        port = host_port.rsplit(":", 1)[-1]
        out.setdefault(port, []).extend(h.get("Proxy", "") for h in (web.get("Handlers") or {}).values())
    return out


def configure(cfg: Config, ts: str | None = None) -> str:
    """Publish the routes; the Tailscale hostname when they are ("" when not)"""
    ts = cli() if ts is None else ts
    if not ts:
        return ""
    if cfg.get("network.tailscale_https", True) is not True:
        # Take down what an earlier run published
        if remove(cfg, ts):
            ok("Tailscale HTTPS disabled (network.tailscale_https = false)")
        else:
            warn("Tailscale HTTPS is disabled in config.toml, but some routes are still published (see above)")
        return ""
    status = run(ts, "status")
    if not status or status.returncode:
        warn("Tailscale is not connected; open it from the menu bar to enable remote access")
        return ""
    ip = run(ts, "ip", "-4")
    if ip and ip.returncode == 0 and ip.stdout.strip():
        ok(f"Tailscale connected ({ip.stdout.strip().splitlines()[0]})")
    me = run(ts, "status", "--json")
    try:
        hostname = (json.loads(me.stdout).get("Self") or {}).get("DNSName", "").rstrip(".") if me else ""
    except ValueError:
        hostname = ""
    if not hostname:
        return ""
    serve = run(ts, "serve", "status", "--json")
    now: dict[str, list[str]] | None
    try:
        now = published(json.loads(serve.stdout) if serve.stdout.strip() else {}) if serve and serve.returncode == 0 else None
    except (ValueError, AttributeError):
        now = None
    take_down_stale(cfg, ts, now)
    now = now or {}
    for port, target, label in wanted(cfg):
        # Already published to the same place (not just the same port)?
        if target in now.get(port, []):
            record(cfg, port, target)
            ok(f"HTTPS :{port} → {label}")
        elif (r := run(ts, "serve", "--bg", "--yes", f"--https={port}", target)) and r.returncode == 0:
            record(cfg, port, target)
            ok(f"HTTPS :{port} → {label} (published)")
        else:
            warn(f"Failed to publish HTTPS :{port}")
    return hostname


def remove(cfg: Config, ts: str | None = None) -> bool:
    """Undo the routes setup added: HTTPS ports whose handler proxies to what
    setup published there, recorded when publishing or what the current
    config would publish. False if a removal failed (the record is kept)."""
    ts = cli() if ts is None else ts
    if not ts:
        return True
    recorded = c.read_json(routes_file(cfg), {})
    ours: dict[str, list[str]] = {port: [target] for port, target, _ in wanted(cfg)}
    for port, target in (recorded.items() if isinstance(recorded, dict) else []):
        ours.setdefault(port, []).append(target)
    # Can't tell what's published: keep the record and report it
    serve = run(ts, "serve", "status", "--json")
    try:
        status = json.loads(serve.stdout) if serve and serve.returncode == 0 else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        warn(f"Couldn't read Tailscale's published routes; nothing removed (record kept in {routes_file(cfg)})")
        return False
    failed = False
    for port, targets in published(status).items():
        if not any(t in ours.get(port, []) for t in targets):
            continue
        if (r := run(ts, "serve", f"--https={port}", "off")) and r.returncode == 0:
            ok(f"Tailscale HTTPS :{port} removed")
        else:
            warn(f"Couldn't remove Tailscale HTTPS :{port} (remove it with: tailscale serve --https={port} off)")
            failed = True
    if not failed:
        routes_file(cfg).unlink(missing_ok=True)
    return not failed
