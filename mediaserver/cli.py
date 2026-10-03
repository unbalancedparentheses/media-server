"""setup.sh's entry point: python3 -m mediaserver.cli [--yes] [mode]

  (no mode)            Full setup + verification (nix run .#install)
  --status             Service state and health
  --doctor             Is it working? Findings + what to do
  --logs <service>     Follow a service's log
  --restart [service]  Restart one or all services
  --test               Run verification only
  --e2e [--keep]       Download → import → Jellyfin test
  --backup             Back up configs
  --restore <file>     Restore configs from a backup
  --update             Back up, git pull, re-run setup
  --uninstall [--purge]  Stop and remove the services (and delete configs)
  --open               Open the dashboard
  --check-config, --preflight, --dry-run
"""
from __future__ import annotations

import getpass
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from mediaserver import doctor, e2e, lock, maintenance, tailscale, validate, verify
from mediaserver.config import Config, default_paths, load_toml
from mediaserver.steps import arrs
from mediaserver.ui import SetupError, err, info, ok, warn

REPO = Path(__file__).resolve().parents[1]
USAGE = ("Usage: setup.sh [--yes] [--dry-run] [--preflight|--check-config|--test|--e2e [--keep]|--status|--doctor|--open|"
         "--logs <service>|--restart [service]|--update|--backup|--restore <file>|--uninstall [--purge]]")
MODES = ("preflight", "check-config", "test", "e2e", "status", "doctor", "open", "update", "backup", "uninstall")
# Operations that change the services take the lock (read-only ones don't)
LOCKED = ("setup", "update", "restore", "backup", "uninstall", "restart", "e2e")
# The install, in order: (step, function in mediaserver/steps)
INSTALL = ["directories", "service-configs", "services", "wait", "api-keys", "resume-indexers", "qbittorrent", "jellyfin",
           "sabnzbd", "arrs", "junk-filters", "prowlarr", "usenet-providers", "bazarr", "sabnzbd-login", "seerr", "moonbase",
           "intro-skipper", "unpackerr", "cleanuparr", "postimport", "api-proxy"]


@dataclass
class Options:
    mode: str = "setup"
    arg: str = ""
    yes: bool = False
    dry_run: bool = False
    purge: bool = False
    keep: bool = False


def parse(argv: list[str]) -> Options:
    o = Options()
    chosen = ""

    def mode(name: str) -> None:
        nonlocal chosen
        if chosen:
            raise err("Only one mode can be used at a time")
        chosen = o.mode = name
    args = list(argv)
    while args:
        a = args.pop(0)
        if a in ("--yes", "-y"):
            o.yes = True
        elif a == "--dry-run":
            o.dry_run = True
        elif a == "--purge":
            o.purge = True
        elif a == "--keep":
            o.keep = True
        elif a.removeprefix("--") in MODES and a.startswith("--"):
            mode(a[2:])
        elif a in ("--logs", "--restore"):
            mode(a[2:])
            if not args:
                raise err(USAGE)
            o.arg = args.pop(0)
        elif a == "--restart":
            mode("restart")
            if args and not args[0].startswith("--"):
                o.arg = args.pop(0)
        elif a in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        else:
            raise err(USAGE)
    if o.purge and o.mode != "uninstall":
        raise err("--purge only goes with --uninstall")
    return o


# ─── config.toml ─────────────────────────────────────────────────

def toml_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_credentials(path: Path, jf_user: str, jf_pass: str, qb_user: str, qb_pass: str) -> None:
    """Set the logins in config.toml, leaving everything else (comments,
    layout) as it is"""
    values = {"jellyfin": (jf_user, jf_pass), "qbittorrent": (qb_user, qb_pass)}
    lines, section = path.read_text().splitlines(keepends=True), None
    for i, line in enumerate(lines):
        if m := re.match(r"^\s*\[([^\]]+)\]\s*$", line):
            section = m.group(1).strip()
        elif section in values:
            user, password = values[section]
            if re.match(r"^\s*username\s*=", line):
                lines[i] = f"username = {toml_string(user)}\n"
            elif re.match(r"^\s*password\s*=", line):
                lines[i] = f"password = {toml_string(password)}\n"
    path.write_text("".join(lines))


def generate_secret() -> str:
    return secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:24]


def secure_defaults(path: Path) -> None:
    jf, qb = generate_secret(), generate_secret()
    write_credentials(path, "admin", jf, "admin", qb)
    ok("Generated secure default passwords in config.toml")
    print(f"  Jellyfin password: {jf}")
    print(f"  qBittorrent password: {qb}")


def ask(prompt: str, secret: bool = False) -> str:
    """With nothing to read (piped or redirected input), explain instead of
    failing on the prompt"""
    try:
        return getpass.getpass(prompt) if secret else input(prompt)
    except EOFError:
        raise err(f"No answer to the password prompt; run with --yes to generate passwords (or edit {default_paths().config_file} first)") from None


def ask_login(service: str, width: int) -> tuple[str, str]:
    user = ask(f"  {service} username [admin]: ") or "admin"
    while True:
        password = ask(f"  {service + ' password:':<{width}} ", True)
        if not password:
            warn("Password cannot be empty")
            continue
        if ask(f"  {'Confirm password:':<{width}} ", True) == password:
            ok(f"{service}: {user}")
            return user, password
        warn("Passwords don't match — try again")


def prompt_credentials(path: Path) -> None:
    info("Setting up credentials...")
    print("  The Jellyfin login is shared by Seerr, Sonarr, Radarr, Prowlarr, Bazarr,")
    print("  SABnzbd and Cleanuparr. qBittorrent has its own.\n")
    jf = ask_login("Jellyfin", 18)
    print()
    qb = ask_login("qBittorrent", 21)
    write_credentials(path, *jf, *qb)
    ok("Credentials saved to config.toml")


def ensure_config(yes: bool) -> Config:
    """config.toml, created from the example (with generated or asked-for
    passwords) when missing; still-default passwords are replaced too.
    Every problem stops setup before anything changes."""
    paths = default_paths()
    path = paths.config_file
    paths.media.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copy(REPO / "config.toml.example", path)
        path.chmod(0o600)
        if yes:
            secure_defaults(path)
            warn(f"{path} not found — created with secure generated defaults")
        else:
            warn(f"{path} not found — created from config.toml.example")
            prompt_credentials(path)
    try:
        data = load_toml(path)
    except ValueError as e:
        raise err(f"{path} isn't valid TOML: {e}") from None
    if "changeme" in ((data.get("jellyfin") or {}).get("password"), (data.get("qbittorrent") or {}).get("password")):
        secure_defaults(path) if yes else prompt_credentials(path)
    check_config_file()
    return Config.load(paths)


def check_config_file() -> None:
    """Every key, type, range and combination, all problems at once"""
    if validate.main():
        raise SetupError("config.toml has problems")


def config_needed() -> Config:
    paths = default_paths()
    if not paths.config_file.exists():
        raise err(f"{paths.config_file} not found — run 'nix run .#install' first")
    check_config_file()
    return Config.load(paths)


def config_or_defaults() -> Config:
    """Uninstall, logs and the like work without config.toml"""
    paths = default_paths()
    try:
        return Config.load(paths)
    except (OSError, ValueError):
        return Config({}, paths)


# ─── Install ─────────────────────────────────────────────────────

def check_platform() -> str:
    """The tailscale command ("" when not installed)"""
    info("Checking prerequisites...")
    if platform.system() != "Darwin":
        raise err("This setup runs services as launchd agents and supports macOS only")
    if not Path(os.environ.get("MEDIA_SERVICES_JSON", "/nonexistent")).is_file():
        raise err("Run through Nix: 'nix run .#install' (or ./setup.sh, which does that for you)")
    ok(f"macOS {platform.mac_ver()[0]}, services from Nix")
    ts = tailscale.cli()
    if ts:
        ok("Tailscale")
    else:
        warn("Tailscale not installed (remote access step will be skipped)")
    return ts


def run_step(cfg: Config, name: str) -> None:
    import importlib

    from mediaserver.steps import STEPS
    if name == "resume-indexers":
        # Radarr indexers an interrupted e2e test left paused
        e2e.E2E(cfg).resume_indexers()
        return
    module, _, function = STEPS[name].partition(":")
    getattr(importlib.import_module(f"mediaserver.steps.{module}"), function or "run")(cfg)


def install(o: Options) -> int:
    started = time.time()
    times: list[tuple[float, str]] = []

    def timed(name: str, fn, *args):
        t = time.time()
        result = fn(*args)
        times.append((time.time() - t, name))
        return result
    ts = timed("check_platform", check_platform)
    cfg = timed("ensure_config", ensure_config, o.yes)
    # Before anything changes: the services step would stop an older
    # version's separate anime Sonarr and remove its agent
    arrs.require_no_unmerged_anime_sonarr(cfg)
    hostname = timed("tailscale", tailscale.configure, cfg, ts)
    for name in INSTALL:
        timed(name, run_step, cfg, name)
    failed = verify.main(cfg)
    if failed:
        print(f"\n\033[1;31m  Setup finished, but {failed} verification check(s) failed (see above).\033[0m")
        print("  Fix the cause and re-run 'nix run .#install', or check again with 'nix run .#test'.\n")
        return 1
    summary(cfg, hostname, time.time() - started, times)
    open_dashboard_once(cfg, o.yes)
    return 0


def lan_ip() -> str:
    r = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def summary(cfg: Config, hostname: str, took: float, times: list[tuple[float, str]]) -> None:
    ip = lan_ip()
    slowest = ", ".join(f"{name} {t:.0f}s" if t >= 10 else f"{name} {t:.1f}s" for t, name in sorted(times, reverse=True)[:3])
    lines = ["", "  Setup Complete!", "", "  On this Mac:",
             "    Watch (Moonfin): http://localhost:8096/Moonfin/Web/",
             "    Request:         http://localhost:5055",
             f"    Dashboard:       {cfg.urls.dashboard}"]
    if ip:
        lines.append(f"  On your network:   replace localhost with {ip}")
    lines.append("")
    if hostname:
        lines += ["  Remote (HTTPS over Tailscale):", f"    Watch:     https://{hostname}:8096/Moonfin/Web/",
                  f"    Request:   https://{hostname}:5055", f"    Dashboard: https://{hostname}", ""]
    lines += ["  Apps: install Moonfin (App Store, Google Play, Amazon) and point it at",
              f"  http://{ip or '<this Mac>'}:8096. Log in with your Jellyfin user.", "",
              "  What the checks above prove: every service is running, configured,",
              "  connected to the others, and accepts your login. They don't prove that",
              "  releases are found (that depends on the indexers), that playback works",
              "  on your devices, or that Moonfin loads offline in a browser.",
              "    Is it working right now?        nix run .#doctor",
              "    Request → library → subtitles:  nix run .#e2e", "",
              f"  Setup took {int(took)}s; slowest: {slowest}", "",
              "  Manage: nix run .#status | .#logs -- <service> | .#restart | .#uninstall",
              "  Keep the Mac awake while serving: System Settings → Energy → Prevent",
              "  automatic sleeping when the display is off.", ""]
    print("\n".join(lines))


def interactive() -> bool:
    return sys.stdout.isatty()


def open_dashboard_once(cfg: Config, yes: bool) -> None:
    """The first install that succeeds opens the dashboard; later runs are
    usually config changes, so they don't. Never with --yes, in CI or
    without a terminal (scripted installs)."""
    marker = cfg.paths.state / "dashboard-opened"
    if marker.exists():
        return
    marker.touch()
    if yes or os.environ.get("CI") or not interactive():
        return
    info(f"Opening the dashboard ({cfg.urls.dashboard}); next time: nix run .#open")
    subprocess.run(["open", cfg.urls.dashboard], capture_output=True, check=False)


# ─── Modes ───────────────────────────────────────────────────────

def dry_run(o: Options) -> int:
    paths = default_paths()
    if o.mode == "setup":
        info("Dry-run mode: validating prerequisites and config only (no writes, no service changes)")
        maintenance.preflight(config_or_defaults())
        if paths.config_file.exists():
            maintenance.check_config(config_or_defaults())
        info("Dry-run complete")
        return 0
    messages = {"update": "would back up, git pull and re-run setup",
                "restore": f"would stop services, restore {o.arg} and restart",
                "backup": f"would back up {paths.config} and {paths.config_file}",
                "uninstall": "would stop and remove the launchd agents" + (", then delete configs and state" if o.purge else "")}
    if o.mode not in messages:
        raise err(f"--dry-run is not supported with --{o.mode}")
    info(f"Dry-run mode: {messages[o.mode]}")
    return 0


def run(o: Options) -> int:
    if o.dry_run:
        return dry_run(o)
    m = o.mode
    if m == "check-config":
        maintenance.check_config(config_or_defaults())
        return 0
    if m == "preflight":
        return maintenance.preflight(config_or_defaults())
    if m == "backup":
        maintenance.backup(config_or_defaults())
    elif m == "restore":
        maintenance.restore(config_or_defaults(), o.arg, o.yes)
    elif m == "update":
        maintenance.update(config_or_defaults(), o.yes)
    elif m == "uninstall":
        maintenance.uninstall(config_or_defaults(), o.purge, o.yes)
    elif m == "logs":
        maintenance.logs(config_or_defaults(), o.arg)
    elif m == "restart":
        maintenance.restart(config_or_defaults(), o.arg)
    elif m == "open":
        cfg = config_needed()
        if subprocess.run(["open", cfg.urls.dashboard], check=False).returncode:
            raise err(f"Couldn't open {cfg.urls.dashboard}")
    elif m == "status":
        maintenance.status(config_needed())
    elif m == "doctor":
        return doctor.main(config_needed())
    elif m == "test":
        return verify.main(config_needed())
    elif m == "e2e":
        config_needed()
        return e2e.main(["--keep"] if o.keep else [])
    else:
        return install(o)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        o = parse(sys.argv[1:] if argv is None else argv)
        state = default_paths().state
        locked = o.mode in LOCKED and not o.dry_run
        if locked:
            lock.acquire(state)
        try:
            return min(run(o), 125)
        finally:
            if locked:
                lock.release(state)
    except SetupError as e:
        return int(e.code or 1)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
