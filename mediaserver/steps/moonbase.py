"""Moonbase: the Jellyfin plugin behind the Moonfin apps. It serves the
Moonfin web app at /Moonfin/Web/ and connects Moonfin to Seerr for
requests. Pinned in pins.json: the manifest at a fixed commit and the
version to install."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config
from mediaserver.jellyfin import Jellyfin
from mediaserver.pins import MOONBASE
from mediaserver.ui import info, ok, warn

# The dashboard links to /Moonfin/Web/?open=item/<id>. Moonfin's startup
# screen always goes to its home page, dropping any route it was opened
# with, so this keeps the route aside, lets Moonfin start (and log in, if
# needed) and then goes to it once home is shown. Marked so it's added once.
DEEP_LINK = """<script id="media-server-open">
(function () {
  var open = new URLSearchParams(location.search).get("open");
  if (!open || !/^[\\w\\/-]+$/.test(open)) return;
  history.replaceState(null, "", location.pathname);  // start normally
  // Home: Moonfin has drawn its screen for a few seconds and isn't on a
  // sign-in page (those show #/login..., #/server..., #/setup...). Signed
  // out, this waits until you've signed in.
  var start = Date.now(), steady = 0;
  var timer = setInterval(function () {
    var h = location.hash, drawn = document.querySelector("flutter-view, flt-glass-pane");
    var home = drawn && (h === "" || h === "#/" || h.indexOf("#/home") === 0);
    steady = home ? steady + 1 : 0;
    if (home && steady >= 3 && Date.now() - start > 3000) {
      clearInterval(timer);
      location.hash = "#/" + open;
    } else if (Date.now() - start > 600000) { clearInterval(timer); }  // 10 minutes (time to sign in)
  }, 500);
})();
</script>"""


# Moonfin keeps its playback preferences in the browser. Two of its defaults
# work against the files' default tracks: it picks the audio track with
# the most channels (a 5.1 track browsers can't play) unless "prefer the
# default audio track" is on, and has no fallback subtitle language. These
# set them, once: a preference you've changed in Moonfin is left alone.
# Moonfin stores preferences per server and user ("<key>_<server>_<user>")
# and globally; both are set when missing.
PREFS = """<script id="media-server-prefs">
(function () {
  try {
    var defaults = %s;
    var sid = JSON.parse(localStorage.getItem("flutter.pref_last_server_id") || "null");
    var uid = JSON.parse(localStorage.getItem("flutter.pref_last_user_id") || "null");
    Object.keys(defaults).forEach(function (k) {
      var keys = [k];
      if (sid && uid) keys.push(k + "_" + sid + "_" + uid);
      keys.forEach(function (key) {
        if (localStorage.getItem("flutter." + key) === null) localStorage.setItem("flutter." + key, JSON.stringify(defaults[k]));
      });
    });
  } catch (e) {}
})();
</script>"""


def moonfin_defaults(cfg: Config) -> dict:
    """Moonfin preferences from config.toml (ISO 639-2 codes, like Jellyfin)"""
    from mediaserver.postimport import LANGUAGES
    langs = [LANGUAGES.get(x, [x])[0] for x in cfg.get("subtitles.languages", ["en"]) or []]
    prefs: dict = {"pref_prefer_default_audio_track": True}
    if len(langs) > 1:
        prefs["pref_fallback_subtitle_language"] = langs[1]
    return prefs


def frontend(cfg: Config) -> Path | None:
    d = cfg.paths.config / f"jellyfin/data/plugins/Moonbase_{MOONBASE.version}/frontend"
    return d if (d / "canvaskit").is_dir() else None


def patch_web_app(d: Path, hls: str | None, prefs: dict | None = None) -> None:
    """Make the Moonfin web app work without internet, and open titles the
    dashboard links to. As shipped it downloads Flutter's renderer
    (CanvasKit) from www.gstatic.com and hls.js from cdn.jsdelivr.net on
    every start, so offline it never draws a screen. The plugin already
    bundles CanvasKit (frontend/canvaskit/): tell Flutter to use it, and
    serve a pinned hls.js from the Nix store. Jellyfin serves these files
    from disk; re-applied on every run (idempotent)."""
    boot = d / "flutter_bootstrap.js"
    s = boot.read_text()
    if "canvasKitBaseUrl" not in s.split("_flutter.loader.load(", 1)[-1]:
        boot.write_text(s.replace("_flutter.loader.load({", '_flutter.loader.load({\n  config: { canvasKitBaseUrl: "canvaskit/" },', 1))
    index = d / "index.html"
    h = index.read_text()
    new = h
    if hls:
        dst = d / "vendor/hls/hls.min.js"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != Path(hls).read_bytes():
            shutil.copyfile(hls, dst)
            os.chmod(dst, 0o644)
        new = re.sub(r'src="https://cdn\.jsdelivr\.net/npm/hls\.js@[^"]*"', 'src="vendor/hls/hls.min.js"', new)
    new = re.sub(r'<script id="media-server-(open|prefs)">.*?</script>\n?', "", new, flags=re.S)
    scripts = DEEP_LINK + "\n" + (PREFS % json.dumps(prefs) + "\n" if prefs else "")
    new = new.replace("</head>", scripts + "</head>", 1)
    if new != h:
        index.write_text(new)


def run(cfg: Config) -> None:
    info("Configuring Moonbase (Moonfin)...")
    jf = Jellyfin(cfg)
    if not jf.login():
        warn("Skipping: not logged in to Jellyfin")
        return
    plugin = jf.plugin(MOONBASE.guid)
    plugins_dir = cfg.paths.config / "jellyfin/data/plugins"
    installed_now = not plugin
    if not plugin:
        if not jf.pin_repository(f"Moonbase {MOONBASE.version}", MOONBASE.manifest, "Moonfin-Client/Plugin"):
            return
        if not jf.install_plugin("Moonbase", MOONBASE.guid, MOONBASE.version, MOONBASE.manifest):
            warn(f"Could not install Moonbase {MOONBASE.version}")
            return
        # Installation is asynchronous; the plugin loads on the next restart
        for _ in range(90):
            if any(plugins_dir.glob("Moonbase*")):
                break
            time.sleep(1)
        else:
            warn("Moonbase download didn't finish; re-run setup")
            return
        ok("Moonbase installed; restarting Jellyfin")
        if not jf.restart_ready():
            warn("Moonbase: Jellyfin isn't back yet; re-run setup")
            return
        plugin = jf.wait_plugin(MOONBASE.guid)
        if not plugin:
            warn(f"Moonbase didn't load after restart (see {cfg.paths.logs}/jellyfin.log)")
            return
    ok("Moonbase loaded")
    # Existing installs: replace the moving repository with the pinned one
    jf.pin_repository(f"Moonbase {MOONBASE.version}", MOONBASE.manifest, "Moonfin-Client/Plugin")
    if plugin.get("Version") != MOONBASE.version:
        warn(f"Moonbase {plugin.get('Version')} is installed; setup pins {MOONBASE.version}")

    d = frontend(cfg)
    if d is None:
        warn("Moonfin web files not found; can't make them work offline")
    else:
        try:
            manifest = c.read_json(os.environ.get("MEDIA_SERVICES_JSON", ""), {}) or {}
            patch_web_app(d, manifest.get("moonfinHlsJs"), moonfin_defaults(cfg))
            ok("Moonfin web app works offline (local renderer and player) and opens titles from the dashboard")
        except OSError as e:
            warn(f"Could not patch the Moonfin web app ({e})")

    # Point Moonfin's requests at Seerr (Moonbase proxies to it server-side)
    seerr = cfg.urls.seerr
    try:
        conf = jf.get(f"Plugins/{plugin['Id']}/Configuration")
        want = dict(conf, SeerrEnabled=True, SeerrUrl=seerr)
        if want != conf:
            jf.post(f"Plugins/{plugin['Id']}/Configuration", want)
        ok(f"Seerr connected ({seerr})")
    except ApiError:
        warn("Could not set the Seerr URL in Moonbase")

    # Upstream: the web app needs the "Moonfin Startup" task once after
    # install (it unpacks the web files)
    try:
        if not (installed_now or d is None):
            raise ApiError("not needed")
        task = next((t for t in jf.get("ScheduledTasks") or [] if re.search("Moonfin Startup", t.get("Name") or "", re.I)), None)
        if task:
            jf.post(f"ScheduledTasks/Running/{task['Id']}")
            ok("Moonfin web app prepared")
    except ApiError:
        pass

    status = c.status_code(f"{jf.url}/Moonfin/Web/")
    if 200 <= status < 400:
        ok("Moonfin web app: http://localhost:8096/Moonfin/Web/")
    else:
        warn(f"Moonfin web app not answering yet (HTTP {status:03d}); it may need a minute")
