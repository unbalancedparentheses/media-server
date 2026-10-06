"""Everything besides installing: backup, restore, update, uninstall,
status, logs, restart, preflight and the config check."""
from __future__ import annotations

import fnmatch
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver import launchd, lock, tailscale, validate
from mediaserver.config import Config
from mediaserver.ui import SetupError, err, info, ok, warn

MAX_BACKUPS = 10
# Setup's own records in ~/media/.state (applied logins, e2e test
# ownership, …) describe the databases and travel with them
STATE_RECORDS = (
    ".state/credentials.json",
    ".state/e2e/owned.json",
    ".state/e2e/paused-indexers.json",
    ".state/tailscale-routes.json",
    ".state/renamed-sonarr",
    ".state/renamed-radarr",
    ".state/postimport/state.json",
)
# Logs and caches are large and recreated on start
EXCLUDE = ("config/*/logs", "config/jellyfin/log", "config/jellyfin/cache", "config/nginx/temp")


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"  {question} [y/N] ").strip() in ("y", "Y")
    except EOFError:
        return False


# ─── Backup ──────────────────────────────────────────────────────

def archive(media: Path, target: Path) -> None:
    """config/, config.toml and setup's records, without logs and caches.
    Written under a .partial name and renamed only once complete, so an
    interrupted backup never looks like a finished one."""
    def skip(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if any(fnmatch.fnmatch(member.name, pattern) for pattern in EXCLUDE) else member
    partial = target.with_name(target.name + ".partial")
    old = os.umask(0o077)
    try:
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(media / "config", "config", filter=skip)
            if (media / "config.toml").exists():
                tar.add(media / "config.toml", "config.toml")
            for record in STATE_RECORDS:
                if (media / record).exists():
                    tar.add(media / record, record)
        os.replace(partial, target)
    finally:
        os.umask(old)
        partial.unlink(missing_ok=True)


def backup(cfg: Config) -> Path:
    """Services are stopped while archiving so their SQLite databases are
    consistent, and exactly the ones that were running are started again,
    also if archiving fails or is interrupted"""
    p = cfg.paths
    if not p.config.is_dir():
        raise err(f"Config directory not found: {p.config}")
    p.backups.mkdir(parents=True, exist_ok=True)
    # What an interrupted earlier backup left
    for leftover in p.backups.glob("media-server_*.tar.gz.partial"):
        leftover.unlink(missing_ok=True)
    target = p.backups / f"media-server_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    info("Backing up service configs...")
    running = [n for n in launchd.SERVICE_NAMES if launchd.loaded(n)]
    try:
        if running:
            info("Stopping services for a consistent snapshot...")
            for name in running:
                if not launchd.stop(p.config, name):
                    raise err(f"Could not stop {name}; backup aborted (nothing was archived)")
        try:
            archive(p.media, target)
        except (OSError, tarfile.TarError) as e:
            target.unlink(missing_ok=True)
            raise err(f"Backup failed: {e}") from None
    finally:
        if running:
            for name in running:
                if not launchd.loaded(name) and not launchd.bootstrap(name):
                    warn(f"Could not restart {name} (nix run .#restart -- {name})")
            ok(f"Services restarted: {' '.join(running)}")
    size = target.stat().st_size
    ok(f"Created: {target} ({size / 1e6:.1f} MB)")
    backups = sorted(p.backups.glob("media-server_*.tar.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink()
        ok(f"Pruned: {old.name}")
    print(f"\n  Backups in {p.backups} ({len(backups)} total, keeping last {MAX_BACKUPS})")
    print("  Backups contain passwords and API keys; keep a copy on another disk.")
    print(f"  Restore with: nix run .#restore -- {target}\n")
    return target


# ─── Restore ─────────────────────────────────────────────────────

def restore_records(media: Path, extract: Path, stamp: str) -> None:
    """Setup's records must match the restored databases: take the
    backup's, or, for a backup made before they were included, set the
    current ones aside (they describe the newer databases). Setup then
    re-checks every login, and the
    e2e test forgets items that aren't in the restored databases."""
    aside = media / ".state" / f"pre-restore-{stamp}"
    moved = False
    for record in STATE_RECORDS:
        current, saved = media / record, extract / record
        if current.exists():
            target = aside / record.removeprefix(".state/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(current, target)
            moved = True
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(saved, current)
    if moved:
        ok(f"Setup's previous records set aside in {aside}")
    if not (extract / ".state").exists():
        warn("This backup predates setup's records; the next install re-checks every login")


def restore(cfg: Config, file: str, yes: bool) -> None:
    """The current config directory and config.toml are kept alongside with
    a .pre-restore-<timestamp> suffix rather than overwritten"""
    p = cfg.paths
    source = Path(file)
    if not source.is_file():
        raise err(f"Backup file not found: {file}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    extract = p.media / f".restore-{stamp}"
    info(f"Restoring from {source}...")
    print(f"  Current configs move to {p.config}.pre-restore-{stamp}")
    if not confirm("Continue?", yes):
        print("  Aborted.")
        return
    extract.mkdir(parents=True)
    try:
        with tarfile.open(source) as tar:
            tar.extractall(extract, filter="data")
        if not (extract / "config").is_dir():
            raise err("Not a media-server backup (no config/ inside)")
        info("Stopping services...")
        if not all([launchd.stop(p.config, n) for n in launchd.SERVICE_NAMES]):
            raise err("Some services didn't stop; restore aborted (nothing was changed)")
        if p.config.is_dir():
            p.config.rename(f"{p.config}.pre-restore-{stamp}")
        (extract / "config").rename(p.config)
        if (extract / "config.toml").exists():
            if p.config_file.exists():
                p.config_file.rename(f"{p.config_file}.pre-restore-{stamp}")
            (extract / "config.toml").rename(p.config_file)
        restore_records(p.media, extract, stamp)
    except (OSError, tarfile.TarError) as e:
        raise err(f"Restore failed: {e}") from None
    finally:
        shutil.rmtree(extract, ignore_errors=True)
    ok("Configs restored")
    print("\n  Now run 'nix run .#install' to start the services with the restored configs.")
    print(f"  Previous configs: {p.config}.pre-restore-{stamp} (delete once you're happy)\n")


# ─── Update ──────────────────────────────────────────────────────

def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def rollback_file(cfg: Config) -> Path:
    return cfg.paths.state / "update-rollback.json"


def update(cfg: Config, yes: bool) -> None:
    """Versions are pinned in flake.lock, so updating means pulling this
    repo and re-running setup, which rewrites the agents to the new Nix
    store paths. If the new version's checks fail, setup goes back to the
    previous commit and the pre-update backup (roll_back)."""
    repo = Path(os.environ.get("MEDIA_SERVER_REPO") or os.getcwd())
    in_git = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"], capture_output=True, check=False)
    if not (repo / "flake.nix").exists() or in_git.returncode:
        raise err("Run this from your media-server checkout (or set MEDIA_SERVER_REPO)")
    saved = None
    if cfg.paths.config.is_dir():
        info("Creating pre-update backup...")
        saved = backup(cfg)
    info(f"Updating {repo}...")
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    pulled = False
    if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        warn(f"Local changes in {repo}; not pulling (commit or stash them to update)")
    elif subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"], check=False).returncode:
        warn("git pull failed; continuing with the current version")
    else:
        pulled = True
    after = git(repo, "rev-parse", "HEAD").stdout.strip()
    env = dict(os.environ)
    if pulled and before and after != before and saved:
        # What to go back to if the new version's checks fail
        c.write_json(rollback_file(cfg), {"repo": str(repo), "from": before, "to": after, "backup": str(saved)})
        env["MEDIA_UPDATE_ROLLBACK"] = "1"
        ok(f"Updated {before[:7]} → {after[:7]}; if its checks fail, setup goes back to {before[:7]}")
    elif pulled:
        ok("Already up to date")
    info("Re-running setup...")
    # The re-run takes the lock itself
    lock.release(cfg.paths.state)
    os.execvpe("nix", ["nix", "--extra-experimental-features", "nix-command flakes", "run", f"path:{repo}#install", "--",
                       *(["--yes"] if yes else [])], env)


def roll_back(cfg: Config, failed: int) -> bool:
    """After an update whose checks failed: the previous commit, the
    pre-update backup (a newer service may have upgraded its database), and
    setup again on the old version. False when there's nothing to go back
    to (it isn't an update, or the record is gone)."""
    record = c.read_json(rollback_file(cfg), None)
    if not record or os.environ.get("MEDIA_UPDATE_ROLLBACK") != "1":
        return False
    repo = Path(record["repo"])
    print(f"\n\033[1;33m  The update to {record['to'][:7]} failed {failed} check(s): going back to {record['from'][:7]}\033[0m")
    if git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip() or \
            git(repo, "rev-parse", "HEAD").stdout.strip() != record["to"]:
        warn(f"{repo} changed since the update; not rolling back (go back by hand: git reset --hard {record['from'][:7]}, "
             f"then nix run .#restore -- {record['backup']})")
        rollback_file(cfg).unlink(missing_ok=True)
        return False
    if git(repo, "reset", "--hard", record["from"]).returncode:
        warn("Couldn't go back to the previous commit; nothing was changed")
        return False
    restore(cfg, record["backup"], True)
    rollback_file(cfg).unlink(missing_ok=True)
    info("Re-running setup on the previous version...")
    env = {k: v for k, v in os.environ.items() if k != "MEDIA_UPDATE_ROLLBACK"}
    env["MEDIA_ROLLED_BACK"] = f"{record['to'][:7]} failed {failed} check(s); back on {record['from'][:7]}"
    lock.release(cfg.paths.state)
    os.execvpe("nix", ["nix", "--extra-experimental-features", "nix-command flakes", "run", f"path:{repo}#install", "--", "--yes"], env)
    return True


# ─── Uninstall ───────────────────────────────────────────────────

def uninstall(cfg: Config, purge: bool, yes: bool) -> None:
    """Stops the services and removes their agents and Nix GC root. --purge
    also deletes configs, logs, state and config.toml. The library,
    downloads and backups are never deleted."""
    p = cfg.paths
    info("Stopping and removing services...")
    failed = []
    for plist in sorted((Path.home() / "Library/LaunchAgents").glob(f"{launchd.LABEL_PREFIX}.*.plist")):
        name = plist.stem.removeprefix(f"{launchd.LABEL_PREFIX}.")
        if launchd.stop(p.config, name):
            plist.unlink(missing_ok=True)
            ok(f"{name} removed")
        else:
            failed.append(name)
    if failed:
        raise err(f"Could not stop: {' '.join(failed)} (the others were removed). Configs and Tailscale were left alone; "
                  "check 'nix run .#status' and re-run uninstall")
    (p.state / "gcroot").unlink(missing_ok=True)
    from mediaserver.steps import macapp
    macapp.remove(cfg)
    routes_removed = tailscale.remove(cfg)
    if not routes_removed:
        warn("Some Tailscale HTTPS routes are still published (see above)")
        if purge:
            # Purging would delete the record of what's still published,
            # which the next uninstall needs to remove it
            raise err("Not purging while Tailscale routes are still published; configs and state were kept. "
                      "Fix that (see above), then run 'nix run .#uninstall -- --purge' again")
    cache = Path.home() / "Library/Caches/invisible-playwright"
    if purge:
        print("\n  --purge deletes all service settings, accounts, watch history and API")
        print(f"  keys: {p.config}, {p.config_file}, {p.logs}, {p.state},")
        print("  and Byparr's browser in ~/Library/Caches/invisible-playwright.")
        if not confirm("Delete them?", yes):
            print("  Kept configs.")
            purge = False
    if purge:
        for d in (p.config, p.logs, p.state, cache):
            shutil.rmtree(d, ignore_errors=True)
        p.config_file.unlink(missing_ok=True)
        ok("Configs, logs and state deleted")
    print("\n  Uninstalled. Not touched:")
    print(f"    Library and downloads: {p.movies}, {p.tv}, {p.anime}, {p.downloads}")
    print(f"    Backups: {p.backups}")
    if not purge:
        print(f"    Configs (reinstall picks them up): {p.config}, {p.config_file}")
    print("  Free the Nix store space with: nix-collect-garbage\n")


# ─── Status / logs / restart ─────────────────────────────────────

def status(cfg: Config) -> None:
    info("Services")
    for name in launchd.SERVICE_NAMES:
        state = launchd.state(name)
        (ok if state.startswith("running") else warn)(f"{name:<13} {state}")
    info("Health")
    for name, url in cfg.urls.health_endpoints():
        code = c.request(url, timeout=5, follow=False).status
        if 200 <= code < 400:
            ok(f"{name:<13} {url}")
        else:
            warn(f"{name:<13} {url} (HTTP {code or 'none'})")
    info("Disk")
    free = shutil.disk_usage(cfg.paths.media).free // 1024 ** 3
    if free < cfg.disk_warn_gb:
        warn(f"{free} GB free on the media disk (warning below {cfg.disk_warn_gb} GB, imports stop below {cfg.disk_min_gb} GB)")
    else:
        ok(f"{free} GB free on the media disk")
    print(f"\n  Logs: {cfg.paths.logs} (nix run .#logs -- <service>)")


def known(name: str) -> str:
    if name not in launchd.SERVICE_NAMES:
        raise err(f"Unknown service '{name}'. One of: {' '.join(launchd.SERVICE_NAMES)}")
    return name


def logs(cfg: Config, name: str) -> None:
    log = cfg.paths.logs / f"{known(name)}.log"
    if not log.exists():
        raise err(f"No log yet: {log}")
    os.execvp("tail", ["tail", "-n", "100", "-F", str(log)])


def restart(cfg: Config, name: str = "") -> None:
    for service in [known(name)] if name else launchd.SERVICE_NAMES:
        if launchd.restart(cfg.paths.config, service):
            ok(f"{service} restarted")
        else:
            warn(f"{service} is not installed")


# ─── Preflight / config check ────────────────────────────────────

def preflight(cfg: Config) -> int:
    failed = False

    def check(passed: bool, good: str, bad: str) -> None:
        nonlocal failed
        if passed:
            print(f"\033[1;32m[OK]\033[0m {good}")
        else:
            print(f"\033[1;31m[FAIL]\033[0m {bad}")
            failed = True
    print("Preflight checks for media-server\n")
    check(platform.system() == "Darwin", "macOS", "macOS is required (services run as launchd agents)")
    check(sys.version_info >= (3, 11), f"Python {platform.python_version()}", "Python 3.11 or newer is required")
    for cmd in ("launchctl", "git", "tar"):
        check(bool(shutil.which(cmd)), cmd, f"{cmd} is missing")
    check(Path(os.environ.get("MEDIA_SERVICES_JSON", "/nonexistent")).is_file(), "Nix service manifest",
          "not running through Nix (use 'nix run .#install')")
    config = cfg.paths.config_file
    if config.exists():
        check(not validate.check_file(config), f"{config} is valid", f"{config} is invalid (run with --check-config for details)")
    else:
        check(True, f"no {config} yet (setup creates it)", "")
    print()
    check(not failed, "preflight passed", "preflight failed")
    return 1 if failed else 0


def check_config(cfg: Config) -> None:
    if not cfg.paths.config_file.exists():
        raise err(f"{cfg.paths.config_file} not found")
    if validate.main():
        raise SetupError("config.toml has problems")  # printed by validate
    info("Config validation passed")
    ok("Credentials, quality profiles, network settings and timezone are valid")
