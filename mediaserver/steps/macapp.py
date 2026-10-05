"""The Mac app: ~/Applications/Media Server.app, kept in the Dock.

With the Swift compiler (Xcode or its Command Line Tools), it's a native
app (templates/MediaServerApp.swift): the dashboard, Moonfin, Seerr and the
admin pages in one window, switched from its toolbar or with Cmd+1…9.
Without it, the app opens the dashboard in a window of its own (a Chromium
browser's app mode: Brave, Chrome, Edge…; else the default browser).

It's built on this Mac (Info.plist, the program, an icon drawn here), so
macOS runs it without signing, and rewritten only when what it would
contain changes.

The Dock: added once ([app] dock). If you take it out of the Dock, setup
leaves it out (.state/app-in-dock records that it was added). Uninstall
removes the app and its Dock tile."""
from __future__ import annotations

import hashlib
import json
import math
import plistlib
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from mediaserver.config import Config, Urls
from mediaserver.ui import info, ok, warn

NAME = "Media Server"
BUNDLE_ID = "org.media-server.dashboard"
ICON_VERSION = "1"
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
# Browsers with an app mode (--app=URL: a window without tabs or address bar), in order of preference
BROWSERS = ("Brave Browser", "Google Chrome", "Microsoft Edge", "Chromium", "Vivaldi")


SOURCE = Path(__file__).resolve().parents[2] / "templates/MediaServerApp.swift"


def pages(urls: Urls) -> list[dict]:
    """The app's toolbar, in order (Cmd+1…9)"""
    return [{"name": "Home", "url": urls.dashboard}, {"name": "Watch", "url": f"{urls.jellyfin}/Moonfin/Web/"},
            {"name": "Requests", "url": urls.seerr}, {"name": "Sonarr", "url": urls.sonarr},
            {"name": "Radarr", "url": urls.radarr}, {"name": "Prowlarr", "url": urls.prowlarr},
            {"name": "qBittorrent", "url": urls.qbittorrent}, {"name": "SABnzbd", "url": urls.sabnzbd},
            {"name": "Bazarr", "url": urls.bazarr}]


def apple_env() -> dict:
    """Apple's own toolchain, not Nix's (whose shells set DEVELOPER_DIR and SDKROOT)"""
    return {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def swiftc() -> list[str] | None:
    """The Swift compiler, when Xcode or its Command Line Tools are there
    (asked without xcrun's offer to install them)"""
    r = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, env=apple_env(), check=False)
    dev = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if not dev or not any((dev / p).exists() for p in ("usr/bin/swiftc", "Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc")):
        return None
    return ["/usr/bin/xcrun", "swiftc"]


def compile_app(compiler: list[str], out: Path) -> str:
    """"" when it built; else the compiler's complaint"""
    try:
        r = subprocess.run([*compiler, "-O", "-o", str(out), str(SOURCE)], capture_output=True, text=True, env=apple_env(),
                           timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return "" if r.returncode == 0 and out.is_file() else (r.stderr or r.stdout).strip()[-300:] or "failed"


def app_path() -> Path:
    return Path.home() / "Applications" / f"{NAME}.app"


def launcher(url: str) -> str:
    browsers = " ".join(f'"{b}"' for b in BROWSERS)
    return f"""#!/bin/sh
# {NAME}: the dashboard in a window of its own (written by setup)
URL='{url}'
for b in {browsers}; do
  for d in /Applications "$HOME/Applications"; do
    if [ -d "$d/$b.app" ]; then exec open -na "$d/$b.app" --args --app="$URL"; fi
  done
done
exec open "$URL"
"""


def info_plist(native: bool) -> bytes:
    return plistlib.dumps({
        "CFBundleName": NAME, "CFBundleDisplayName": NAME, "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "media-server", "CFBundleIconFile": "icon", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1",
        "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "13.0",
        # The launcher only hands the page to a browser: no Dock icon of its own while it does
        "LSUIElement": not native,
        # Plain http to this Mac's services (the status it reads; the pages)
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True, "NSAllowsArbitraryLoadsInWebContent": True},
    })


# ─── The icon: a gold play button on a dark rounded square ────────

def icon_png(size: int = 512) -> bytes:
    """Drawn with signed distances (smooth edges), as a PNG"""
    s = size / 1024
    half, radius = 412 * s, 185 * s   # Apple's icon grid: 824 of 1024, rounded
    c = size / 2
    # The play triangle, optically centred (nudged right)
    tri = [(c - 80 * s, c - 165 * s), (c + 205 * s, c), (c - 80 * s, c + 165 * s)]   # clockwise on screen

    def rounded_square(x: float, y: float) -> float:
        qx, qy = abs(x - c) - (half - radius), abs(y - c) - (half - radius)
        return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - radius

    def triangle(x: float, y: float) -> float:
        d = -1e9
        for i in range(3):
            (x1, y1), (x2, y2) = tri[i], tri[(i + 1) % 3]
            nx, ny = y2 - y1, x1 - x2
            n = math.hypot(nx, ny)
            d = max(d, ((x - x1) * nx + (y - y1) * ny) / n)
        return d - 18 * s   # rounded corners
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            a = min(max(0.5 - rounded_square(px, py), 0.0), 1.0)
            if a == 0:
                row += b"\x00\x00\x00\x00"
                continue
            t = (py - (c - half)) / (2 * half)   # a top-to-bottom gradient
            r, g, b = 34 - 18 * t, 34 - 18 * t, 48 - 26 * t
            k = min(max(0.5 - triangle(px, py), 0.0), 1.0)
            r, g, b = r + (229 - r) * k, g + (184 - g) * k, b + (75 - b) * k
            row += bytes((int(r), int(g), int(b), int(a * 255)))
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def icns(png: bytes) -> bytes | None:
    """The PNG as an .icns (sips and iconutil, which come with macOS); None
    when they aren't there or fail"""
    if not (shutil.which("sips") and shutil.which("iconutil")):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, iconset = Path(tmp) / "icon.png", Path(tmp) / "icon.iconset"
        src.write_bytes(png)
        iconset.mkdir()
        for px in (16, 32, 64, 128, 256, 512):
            for name, scale in ((f"icon_{px}x{px}.png", 1), (f"icon_{px // 2}x{px // 2}@2x.png", 2)):
                if scale == 2 and px < 32:
                    continue
                if subprocess.run(["sips", "-z", str(px), str(px), str(src), "--out", str(iconset / name)],
                                  capture_output=True, check=False).returncode:
                    return None
        shutil.copy(src, iconset / "icon_512x512.png")
        out = Path(tmp) / "icon.icns"
        if subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], capture_output=True, check=False).returncode:
            return None
        return out.read_bytes()


# ─── The app ─────────────────────────────────────────────────────

def stamp(services: list[dict], native: bool) -> str:
    """What the app contains, to tell when it needs rewriting"""
    what = json.dumps(services) + info_plist(native).decode() + ICON_VERSION
    what += SOURCE.read_text() if native else launcher(services[0]["url"])
    return hashlib.sha256(what.encode()).hexdigest()


def build(services: list[dict], app: Path | None = None) -> str:
    """Writes the app if it's missing or different: "native", "launcher"
    (the browser fallback) or "" when it was already right"""
    app = app or app_path()
    compiler = swiftc()
    marker = app / "Contents/Resources/setup-stamp"
    if marker.is_file() and marker.read_text().strip() == stamp(services, bool(compiler)):
        return ""
    app.parent.mkdir(parents=True, exist_ok=True)
    new = app.with_name(f".{app.name}.new")
    shutil.rmtree(new, ignore_errors=True)
    (new / "Contents/MacOS").mkdir(parents=True)
    (new / "Contents/Resources").mkdir()
    exe = new / "Contents/MacOS/media-server"
    native = bool(compiler)
    if compiler:
        trouble = compile_app(compiler, exe)
        if trouble:
            warn(f"Couldn't build the native app ({trouble.splitlines()[-1]}); it opens the dashboard in your browser instead")
            native = False
    if not native:
        exe.write_text(launcher(services[0]["url"]))
    exe.chmod(0o755)
    (new / "Contents/Info.plist").write_bytes(info_plist(native))
    (new / "Contents/Resources/services.json").write_text(json.dumps(services, indent=1) + "\n")
    icon = icns(icon_png())
    if icon:
        (new / "Contents/Resources/icon.icns").write_bytes(icon)
    # A fallback isn't recorded as done: the next run tries the native app again
    (new / "Contents/Resources/setup-stamp").write_text((stamp(services, native) if native == bool(compiler) else "retry") + "\n")
    shutil.rmtree(app, ignore_errors=True)
    new.rename(app)
    # So Finder and the Dock show the new icon
    if Path(LSREGISTER).exists():
        subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True, check=False)
    return "native" if native else "launcher"


# ─── The Dock ────────────────────────────────────────────────────

def dock_read() -> dict | None:
    r = subprocess.run(["defaults", "export", "com.apple.dock", "-"], capture_output=True, check=False)
    try:
        return plistlib.loads(r.stdout) if r.returncode == 0 else None
    except Exception:   # noqa: BLE001 (plistlib raises several kinds)
        return None


def dock_write(prefs: dict) -> bool:
    r = subprocess.run(["defaults", "import", "com.apple.dock", "-"], input=plistlib.dumps(prefs), capture_output=True, check=False)
    if r.returncode:
        return False
    subprocess.run(["killall", "Dock"], capture_output=True, check=False)   # it reads its settings when it starts
    return True


def tile_url(app: Path) -> str:
    return app.resolve().as_uri() + "/"


def in_dock(prefs: dict, app: Path) -> bool:
    return any(_tile_url(t) == tile_url(app) for t in prefs.get("persistent-apps") or [])


def _tile_url(tile) -> str:
    try:
        return tile["tile-data"]["file-data"]["_CFURLString"]
    except (KeyError, TypeError):
        return ""


def add_to_dock(app: Path) -> str:
    """"added", "there" (already) or "failed\""""
    prefs = dock_read()
    if prefs is None:
        return "failed"
    if in_dock(prefs, app):
        return "there"
    prefs.setdefault("persistent-apps", []).append(
        {"tile-type": "file-tile", "tile-data": {"file-label": NAME, "file-type": 41,
                                                  "file-data": {"_CFURLString": tile_url(app), "_CFURLStringType": 15}}})
    return "added" if dock_write(prefs) else "failed"


def remove_from_dock(app: Path) -> bool:
    """True when it isn't in the Dock (any more)"""
    prefs = dock_read()
    if prefs is None:
        return False
    if not in_dock(prefs, app):
        return True
    prefs["persistent-apps"] = [t for t in prefs["persistent-apps"] if _tile_url(t) != tile_url(app)]
    return dock_write(prefs)


# ─── Setup and uninstall ─────────────────────────────────────────

def run(cfg: Config) -> None:
    info("Mac app...")
    app = app_path()
    if cfg.get("app.enabled", True) is not True:
        remove(cfg)
        return
    built = build(pages(cfg.urls), app)
    if built == "native":
        ok(f"{NAME} app built: the dashboard, Moonfin, Seerr and the admin pages in one window ({app})")
    elif built == "launcher":
        ok(f"{NAME} app written: opens the dashboard in a window of its own ({app}; install Xcode's "
           "Command Line Tools with 'xcode-select --install' for the app with every page in it)")
    else:
        ok(f"{NAME} app ({app})")
    marker = cfg.paths.state / "app-in-dock"
    if cfg.get("app.dock", True) is not True or marker.exists():
        return   # added before: if it's not there now, you took it out
    result = add_to_dock(app)
    if result == "failed":
        warn(f"Couldn't add {NAME} to the Dock; drag it there from {app.parent}")
        return
    marker.touch()
    ok(f"{NAME} added to the Dock" if result == "added" else f"{NAME} is in the Dock")


def remove(cfg: Config) -> None:
    """The app and its Dock tile, if setup made them"""
    app = app_path()
    if not (app / "Contents/Resources/setup-stamp").exists():
        return   # not one setup made
    if not remove_from_dock(app):
        warn(f"Couldn't take {NAME} out of the Dock; remove its tile by hand")
    shutil.rmtree(app, ignore_errors=True)
    (cfg.paths.state / "app-in-dock").unlink(missing_ok=True)
    ok(f"{NAME} app removed")
