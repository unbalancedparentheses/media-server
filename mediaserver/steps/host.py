"""The host: the library and download folders, service config files
written before first start, nginx and the dashboard, the launchd agents,
and waiting until every service answers."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from mediaserver import api, launchd
from mediaserver import common as c
from mediaserver.config import Config, Keys
from mediaserver.steps import cleanuparr, downloads
from mediaserver.ui import err, info, mask, ok, warn

REPO = Path(__file__).resolve().parents[2]
CONFIG_DIRS = ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "sabnzbd", "qbittorrent", "seerr", "unpackerr",
               "cleanuparr", "byparr", "netwatch", "dashstatus")
ARR_PORTS = {"sonarr": 8989, "radarr": 7878, "prowlarr": 9696}


def manifest() -> dict:
    """The Nix manifest: each service's command line, nginx's mime types"""
    path = os.environ.get("MEDIA_SERVICES_JSON", "")
    if not path or not Path(path).is_file():
        raise err("Run through Nix: 'nix run .#install' (or ./setup.sh, which does that for you)")
    return json.loads(Path(path).read_text())


def admin_bind(cfg: Config) -> str:
    return cfg.get("network.admin_bind", "0.0.0.0")


def admin_host(cfg: Config) -> str:
    """Where the admin UIs listen, as an address to connect to"""
    return "127.0.0.1" if admin_bind(cfg) == "0.0.0.0" else admin_bind(cfg)


def render(template: Path, values: dict) -> str:
    """{{NAME}} replaced by its value, with backslashes and double quotes
    escaped for nginx strings; unknown names become empty"""
    def value(m: re.Match) -> str:
        return str(values.get(m.group(1), "")).replace("\\", "\\\\").replace('"', '\\"')
    return re.sub(r"\{\{([A-Za-z0-9_]+)\}\}", value, template.read_text())


def write_if_changed(path: Path, text: str, mode: int | None = None) -> bool:
    """True if the file was created or its content changed"""
    if path.exists() and path.read_text() == text:
        return False
    c.write_atomic(path, text, mode)
    return True


# Services whose config file changed: a running service only reads it at
# start, so they're restarted with the others (kept in a file so it
# survives between steps, and an interrupted install still restarts them)
def changed_file(cfg: Config) -> Path:
    return cfg.paths.state / "config-changed"


def mark_changed(cfg: Config, name: str) -> None:
    names = set(c.read_text(changed_file(cfg)).split()) | {name}
    c.write_atomic(changed_file(cfg), "\n".join(sorted(names)) + "\n")


# ─── Folders ─────────────────────────────────────────────────────

def directories(cfg: Config) -> None:
    info("Creating directory structure...")
    p = cfg.paths
    for library in (p.movies, p.tv, p.anime):
        library.mkdir(parents=True, exist_ok=True)
        # Jellyfin doesn't watch an empty library folder for new files, so the
        # first show in an empty TV library would only appear at the next
        # scheduled scan; a hidden file keeps each folder non-empty
        (library / ".jellyfin-watch").touch()
    # Per-category folders too: Sonarr/Radarr flag a download client whose
    # folder doesn't exist yet (qBittorrent only creates it on first download)
    for kind in ("torrents", "usenet"):
        (p.downloads / kind / "incomplete").mkdir(parents=True, exist_ok=True)
        for app in ("sonarr", "radarr"):
            (p.downloads / kind / "complete" / app).mkdir(parents=True, exist_ok=True)
    for d in (p.backups, p.logs, p.state, *(p.config / n for n in CONFIG_DIRS), p.config / "nginx/www", p.config / "nginx/temp"):
        d.mkdir(parents=True, exist_ok=True)
    ok(f"{p.media} directory tree ready")


# ─── Config files ────────────────────────────────────────────────

def seed_arr(cfg: Config, name: str, port: int) -> None:
    """*arr config.xml: API key, port and bind address are set before first
    start so setup knows the key. Existing files keep their key; port and
    bind address follow config.toml."""
    path = cfg.paths.config / name / "config.xml"
    bind = "*" if admin_bind(cfg) == "0.0.0.0" else admin_bind(cfg)
    if not path.exists():
        c.write_atomic(path, f"""<Config>
  <BindAddress>{bind}</BindAddress>
  <Port>{port}</Port>
  <ApiKey>{secrets.token_hex(16)}</ApiKey>
  <LaunchBrowser>False</LaunchBrowser>
  <UpdateMechanism>External</UpdateMechanism>
  <AuthenticationMethod>Forms</AuthenticationMethod>
  <AuthenticationRequired>{"DisabledForLocalAddresses" if cfg.admin_local_only else "Enabled"}</AuthenticationRequired>
</Config>
""", 0o600)
        ok(f"{name}: config.xml (port {port})")
        return
    text = path.read_text()
    new = re.sub(r"<Port>[^<]*</Port>", f"<Port>{port}</Port>", text)
    new = re.sub(r"<BindAddress>[^<]*</BindAddress>", f"<BindAddress>{bind}</BindAddress>", new)
    if new != text:
        c.write_atomic(path, new, 0o600)
        mark_changed(cfg, name)


def seed_sabnzbd(cfg: Config) -> None:
    path = cfg.paths.config / "sabnzbd/sabnzbd.ini"
    if path.exists():
        return
    d = cfg.paths.downloads
    c.write_atomic(path, f"""__version__ = 19
__encoding__ = utf-8
[misc]
api_key = {secrets.token_hex(16)}
download_dir = {d}/usenet/incomplete
complete_dir = {d}/usenet/complete
""", 0o600)
    ok("SABnzbd: sabnzbd.ini (wizard skipped)")


def nginx(cfg: Config, mime_types: str) -> None:
    www = cfg.paths.config / "nginx/www"
    conf = cfg.paths.config / "nginx/nginx.conf"
    existed = conf.exists()
    text = render(REPO / "templates/nginx.conf.tpl", {"NGINX_MIME_TYPES": mime_types, "DASHBOARD_PORT": cfg.urls.dashboard_port,
                                                       "ADMIN_HOST": admin_host(cfg)})
    # nginx only reads it at start (e.g. a new dashboard_port): restart it
    # with the others, before setup waits for the dashboard
    if write_if_changed(conf, text) and existed:
        mark_changed(cfg, "nginx")
    # Placeholder until the API keys are known (the api-proxy step)
    proxy = cfg.paths.config / "nginx/api-proxy.conf"
    if not proxy.exists():
        proxy.touch()
    for page, target in (("landing.html", "index.html"), ("admin.html", "admin.html")):
        c.write_atomic(www / target, (REPO / page).read_text(), 0o644)
    # Left by earlier versions: the homepage before Home/Manage, and the
    # setting for admin pages seen from another device (the dashboard is
    # this Mac's only now)
    for old in ("classic.html", "settings.js"):
        (www / old).unlink(missing_ok=True)


def service_configs(cfg: Config) -> None:
    info("Writing service configs...")
    for name, port in ARR_PORTS.items():
        seed_arr(cfg, name, port)
    downloads.seed_qbittorrent(cfg)
    seed_sabnzbd(cfg)
    nginx(cfg, manifest().get("nginxMimeTypes", ""))
    ok("Service configs ready")


# ─── launchd ─────────────────────────────────────────────────────

def remove_retired(cfg: Config, agents_dir: Path | None = None) -> None:
    """Stop and remove agents for services this version no longer runs (e.g.
    the separate anime Sonarr, merged into Sonarr). Their config folders are
    left in place."""
    agents_dir = agents_dir or Path.home() / "Library/LaunchAgents"
    for plist in sorted(agents_dir.glob(f"{launchd.LABEL_PREFIX}.*.plist")):
        name = plist.stem.removeprefix(f"{launchd.LABEL_PREFIX}.")
        if name in launchd.SERVICE_NAMES:
            continue
        if launchd.stop(cfg.paths.config, name):
            plist.unlink(missing_ok=True)
            ok(f"{name}: no longer used, stopped and removed (its data stays in {cfg.paths.config / name})")


def services(cfg: Config) -> None:
    info("Starting services...")
    remove_retired(cfg)
    # Before Cleanuparr can start on a wider address
    cleanuparr.run_require_login(cfg)
    m = manifest()
    p = cfg.paths
    subst = {"@MEDIA@": str(p.media), "@CONFIG@": str(p.config), "@STATE@": str(p.state), "@ADMIN_BIND@": admin_bind(cfg),
             "@DISK_WARN_GB@": str(cfg.disk_warn_gb), "@DISK_MIN_GB@": str(cfg.disk_min_gb)}
    # Each restart is recorded before its agent is replaced: an install
    # interrupted from there on (while writing, a service that won't stop,
    # Ctrl-C) leaves agents that no longer look changed, and the next run
    # must still restart them. Cleared only after they all restarted.
    launchd.write_agents(m["services"], subst, p.config, p.logs, cfg.timezone,
                         before_change=lambda name: mark_changed(cfg, name))
    # Also restart services whose config file changed (e.g. a new admin_bind)
    changed = set(c.read_text(changed_file(cfg)).split())
    launchd.start_all(p.config, changed)
    changed_file(cfg).unlink(missing_ok=True)
    # Keep this version's Nix store paths from being garbage-collected while
    # the agents point at them
    gcroot = subprocess.run(["nix-store", "--add-root", str(p.state / "gcroot"), "--realise", os.environ["MEDIA_SERVICES_JSON"]],
                            capture_output=True, check=False)
    if gcroot.returncode:
        warn("Could not register a Nix GC root; 'nix-collect-garbage' may remove the running services")


def wait(cfg: Config) -> None:
    info("Waiting for all services (the first start downloads Byparr's browser)...")
    for name, url in cfg.urls.health_endpoints():
        if name == "Byparr":
            # Only Cloudflare-protected indexers need it; don't stop setup for it
            if not api.wait_for(name, url, 600):
                warn(f"Byparr didn't start (see {cfg.paths.logs}/byparr.log); indexers with flaresolverr = true won't work until it does")
        elif not api.wait_for(name, url, 120):
            raise err(f"{name} didn't start within 120s; see {cfg.paths.logs} and 'nix run .#status', then re-run setup")


def api_keys(cfg: Config) -> None:
    info("Reading API keys...")
    keys = Keys.read(cfg.paths.config)
    for label, key, required in (("Sonarr", keys.sonarr, True), ("Radarr", keys.radarr, True), ("Prowlarr", keys.prowlarr, True),
                                 ("SABnzbd", keys.sabnzbd, False), ("Seerr", keys.seerr, False)):
        if key:
            ok(f"{label + ':':<13} {mask(key)}")
        elif required:
            raise err(f"{label} key not found")
        else:
            warn(f"{label} key not found" + (" (will read after setup)" if label == "Seerr" else ""))
    ok(f"qBittorrent:  user {cfg.qbit_user}")


def api_proxy(cfg: Config) -> None:
    """nginx's proxy for the dashboard's widgets: the keys stay on the server"""
    info("Generating API proxy config for the dashboard...")
    keys = Keys.read(cfg.paths.config)
    text = render(REPO / "templates/nginx.api-proxy.conf.tpl", {
        "ADMIN_HOST": admin_host(cfg), "JELLYFIN_API_KEY": c.read_text(cfg.paths.state / "dashstatus/jellyfin-key"),
        "SEERR_KEY": keys.seerr, "SABNZBD_KEY": keys.sabnzbd, "SONARR_KEY": keys.sonarr, "RADARR_KEY": keys.radarr})
    path = cfg.paths.config / "nginx/api-proxy.conf"
    c.write_atomic(path, text, 0o600)
    ok("api-proxy.conf written")
    if launchd.restart(cfg.paths.config, "nginx"):
        ok("nginx reloaded")
    else:
        warn("Could not restart nginx")
