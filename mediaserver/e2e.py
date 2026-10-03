"""End-to-end import test: nix run .#e2e [-- --keep]

Proves the automatic path works with no manual step, using Blender's
Creative Commons film "Tears of Steel" (downloaded once from
download.blender.org into ~/media/.state/e2e):

  Movie: request in Seerr → Radarr adds it → a correctly named release is
         handed to Radarr → qBittorrent → automatic import → Jellyfin →
         Bazarr downloads English subtitles → Seerr shows it as available.
  TV:    Sonarr gets the free series "Pioneer One" (no indexer search) →
         a correctly named S01E01 release → qBittorrent → automatic import
         → Jellyfin.

The releases are torrents built here whose data is already in the download
folder, so qBittorrent completes them instantly without peers or public
indexers. The TV "episode" is the same film file under an episode name.
So this proves the integration (request → grab → import → library →
subtitles → Seerr), not searching indexers or downloading from peers.
Everything the test adds is removed afterwards unless --keep.
Results go to ~/media/logs/e2e-<timestamp>.log.
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from mediaserver import api
from mediaserver import common as c
from mediaserver.api import ApiError
from mediaserver.config import Config, Keys
from mediaserver.jellyfin import Jellyfin
from mediaserver.logins import qbittorrent_cookie
from mediaserver.steps.arrs import App, series_settled
from mediaserver.ui import SetupError, err, info, ok, warn

MOVIE_TMDB = 133701
SERIES_TVDB = 170551
TAG = "MEDIASERVERTEST"
SOURCE_URL = "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov"
SOURCE_SIZE = 372178639
PORT = 18765


def step(message: str) -> None:
    print(f"{time.strftime('%T')}  {message}", flush=True)


def wait_until(seconds: float, check: Callable[[], Any], every: float = 3) -> Any:
    """Poll until check() returns something truthy (a failing call counts as
    not yet); None after <seconds> of elapsed time, however slow the check"""
    start = time.time()
    while True:
        try:
            result = check()
            if result:
                return result
        except (ApiError, OSError, KeyError, IndexError, TypeError, ValueError):
            pass
        if time.time() - start >= seconds:
            return None
        time.sleep(every)


# ─── Torrents ────────────────────────────────────────────────────

def bencode(x: Any) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(map(bencode, x)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(x.items())) + b"e"


def make_torrent(folder: Path, out: Path, piece: int = 1 << 20) -> str:
    """A torrent for one folder's files; returns the info hash. A per-run
    nonce gives each run a new hash: Sonarr/Radarr remember hashes they
    already imported and would silently ignore a repeat."""
    pieces, buf, entries = b"", b"", []
    for f in sorted(p for p in folder.iterdir() if not p.name.startswith(".")):
        entries.append({"length": f.stat().st_size, "path": [f.name]})
        with f.open("rb") as fh:
            while chunk := fh.read(piece):
                buf += chunk
                while len(buf) >= piece:
                    pieces += hashlib.sha1(buf[:piece]).digest()
                    buf = buf[piece:]
    if buf:
        pieces += hashlib.sha1(buf).digest()
    torrent_info = {"name": folder.name, "piece length": piece, "pieces": pieces, "files": entries, "private": 1,
                    "x-e2e-run": os.urandom(8).hex()}
    out.write_bytes(bencode({"info": torrent_info, "created by": "media-server e2e"}))
    return hashlib.sha1(bencode(torrent_info)).hexdigest()


def serve(folder: Path, port: int = PORT) -> http.server.ThreadingHTTPServer:
    """Serve the test torrents to Radarr/Sonarr (stop with .shutdown())"""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 (the base class's name)
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Quiet, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def release(title: str, port: int = PORT) -> dict:
    return {"title": title, "downloadUrl": f"http://127.0.0.1:{port}/{title}.torrent", "protocol": "torrent",
            "publishDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "size": SOURCE_SIZE, "indexer": "e2e test"}


def verdict(answer: Any) -> str:
    """"approved", or why Radarr/Sonarr rejected a pushed release"""
    first = (answer[0] if answer else {}) if isinstance(answer, list) else (answer or {})
    return "approved" if first.get("approved") else "rejected: " + "; ".join(first.get("rejections") or [])


class E2E:
    def __init__(self, cfg: Config, keys: Keys | None = None):
        self.cfg = cfg
        keys = keys or Keys.read(cfg.paths.config)
        self.radarr = App("Radarr", cfg.urls.radarr, keys.radarr)
        self.sonarr = App("Sonarr", cfg.urls.sonarr, keys.sonarr)
        self.seerr = App("Seerr", cfg.urls.seerr, keys.seerr, "v1")
        self.dir = cfg.paths.state / "e2e"
        self.complete = cfg.paths.downloads / "torrents/complete"
        self.jellyfin = Jellyfin(cfg)
        self.passed = self.failed = 0

    def passes(self, message: str) -> None:
        self.passed += 1
        ok(message)

    def fails(self, message: str) -> None:
        self.failed += 1
        print(f"\033[1;31m   ✗ {message}\033[0m", flush=True)

    # ─── Paused indexers ─────────────────────────────────────────
    # Indexers whose automatic search/RSS the test switched off, with their
    # previous settings, saved before anything changes so a crash can't lose
    # them. An entry is removed only once its indexer is back on; what's left
    # is restored at the next test run or install.

    @property
    def paused_file(self) -> Path:
        return self.dir / "paused-indexers.json"

    def pause_indexers(self) -> None:
        if not self.resume_indexers():
            raise err(f"Radarr indexers paused by an earlier test couldn't be re-enabled; fix that first (record: {self.paused_file})")
        try:
            paused = [{"id": x["id"], "enableAutomaticSearch": x.get("enableAutomaticSearch"), "enableRss": x.get("enableRss")}
                      for x in self.radarr.call("GET", "indexer") or [] if x.get("enableAutomaticSearch") or x.get("enableRss")]
        except ApiError:
            raise err("Couldn't read Radarr's indexers") from None
        c.write_json(self.paused_file, paused, compact=True)
        failed = False
        for entry in paused:
            try:
                current = self.radarr.call("GET", f"indexer/{entry['id']}")
                self.radarr.call("PUT", f"indexer/{entry['id']}?forceSave=true",
                                 {**current, "enableAutomaticSearch": False, "enableRss": False})
            except ApiError:
                failed = True
        # A public release could win the race against the test's own: don't
        # run with automatic search still on
        if failed:
            self.resume_indexers()
            raise err("Couldn't pause automatic search on Radarr's indexers; not running the test")
        step(f"Paused automatic search on {len(paused)} Radarr indexers for the test")

    def resume_indexers(self) -> bool:
        """False (and the record kept) if any indexer couldn't be restored"""
        f = self.paused_file
        if not f.exists():
            return True
        entries = c.read_json(f)
        # An unreadable record must not count as "nothing to restore"
        if not isinstance(entries, list) or not all(isinstance(e, dict) and type(e.get("id")) is int for e in entries):
            warn(f"Can't read {f}; re-enable automatic search on Radarr's indexers by hand (Settings → Indexers), then delete it")
            return False
        left = []
        for entry in entries:
            url = f"{self.radarr.url}/api/v3/indexer/{entry['id']}"
            r = c.request(url, headers={"X-Api-Key": self.radarr.key}, timeout=30)
            if r.status == 404:
                continue  # deleted since: nothing to restore
            try:
                if r.status != 200:
                    raise ApiError(url)
                self.radarr.call("PUT", f"indexer/{entry['id']}?forceSave=true",
                                 {**r.json({}), "enableAutomaticSearch": entry.get("enableAutomaticSearch"),
                                  "enableRss": entry.get("enableRss")})
            except ApiError:
                left.append(entry)
        if not left:
            f.unlink(missing_ok=True)
            step("Radarr indexers restored")
            return True
        c.write_json(f, left, compact=True)
        warn(f"{len(left)} Radarr indexer(s) still paused; retried at the next test or install (record: {f})")
        return False

    # ─── Ownership ───────────────────────────────────────────────
    # Everything the test creates (Radarr movie, Sonarr series, Seerr media,
    # torrent hashes, download folders) is recorded here as it's created,
    # and cleanup removes only what's recorded. Nothing is matched by title.

    @property
    def owned_file(self) -> Path:
        return self.dir / "owned.json"

    def owned(self) -> dict | None:
        """The record; {} when there's none, None when it can't be read (an
        unreadable record isn't "nothing to clean up")"""
        if not self.owned_file.exists():
            return {}
        record = c.read_json(self.owned_file)
        return record if isinstance(record, dict) else None

    def own(self, key: str, value: Any) -> None:
        record = self.owned()
        if record is None:
            raise err(f"Can't read {self.owned_file}; not adding to it (it may list test items still to remove)")
        if key in ("hashes", "paths", "library_paths"):
            record[key] = sorted(set(record.get(key, [])) | {str(value)})
        else:
            record[key] = value
        c.write_json(self.owned_file, record)

    def remove(self, label: str, app: App, path: str, delete: str) -> bool:
        """Only a 404 means "already gone"; any other failure keeps the
        record so the next run retries"""
        status = c.request(f"{app.url}/api/v3/{path}", headers={"X-Api-Key": app.key}, timeout=30).status
        if status == 404:
            return True
        if status != 200:
            warn(f"Couldn't check the test's {label} (service not answering)")
            return False
        try:
            app.call("DELETE", delete)
            return True
        except ApiError:
            return False

    def clear_queue(self, app: App, query: str) -> bool:
        try:
            for record in (app.call("GET", f"queue?{query}") or {}).get("records", []):
                app.call("DELETE", f"queue/{record['id']}?removeFromClient=true&blocklist=false")
            return True
        except ApiError:
            return False

    def cleanup(self, jellyfin_wait: float = 300) -> bool:
        """Remove what the test owns, in dependency order: downloads, then
        Radarr/Sonarr (with files), then wait for Jellyfin to drop the items,
        and only then Seerr (otherwise Seerr re-syncs them from Jellyfin).
        False if anything couldn't be removed; the record is kept for next time."""
        if not self.owned_file.exists():
            return True
        record = self.owned()
        if record is None:
            warn(f"Can't read {self.owned_file}, so the test's leftovers can't be found; it's kept. "
                 "Check Radarr, Sonarr and Seerr for Tears of Steel and Pioneer One, then delete it")
            return False
        done = True
        if movie := record.get("movie_id"):
            done &= self.clear_queue(self.radarr, f"movieIds={movie}")
            done &= self.remove("movie", self.radarr, f"movie/{movie}", f"movie/{movie}?deleteFiles=true&addImportExclusion=false")
        if series := record.get("series_id"):
            done &= self.clear_queue(self.sonarr, f"seriesIds={series}")
            done &= self.remove("series", self.sonarr, f"series/{series}", f"series/{series}?deleteFiles=true")
        if hashes := record.get("hashes"):
            cookie = qbittorrent_cookie(self.cfg.urls.qbittorrent, self.cfg.qbit_user, self.cfg.qbit_pass)
            done &= bool(cookie) and c.request(f"{self.cfg.urls.qbittorrent}/api/v2/torrents/delete", "POST", {"Cookie": cookie},
                                               form={"hashes": "|".join(hashes), "deleteFiles": "true"}, timeout=20).ok
        complete = self.complete.resolve()
        for path in record.get("paths", []):
            # Only ever inside the download folders (resolved, so ".." or a
            # symlink can't lead out of them)
            real = Path(path).resolve()
            if real.is_relative_to(complete) and real != complete:
                try:
                    shutil.rmtree(real)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    warn(f"Couldn't delete {real}: {e.strerror}")
                if real.exists():
                    done = False
        # Jellyfin must forget the test's files (recorded paths, or named
        # with the tag) before Seerr
        if self.jellyfin.token:
            library = set(record.get("library_paths", []))
            last_scan = 0.0

            def forgotten():
                # A scan requested while another runs is dropped, and one that
                # started before the files were deleted keeps them: ask again
                # every 30s until they're gone
                nonlocal last_scan
                if time.time() - last_scan >= 30:
                    last_scan = time.time()
                    try:
                        self.jellyfin.post("Library/Refresh")
                    except ApiError:
                        pass
                items = self.jellyfin.get("Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path")["Items"]
                return not any(TAG in (i.get("Path") or "") or i.get("Path") in library for i in items)
            if not wait_until(jellyfin_wait, forgotten):
                warn("Jellyfin still lists a test item")
                done = False
        # Deleting the media also deletes its requests; a request recorded
        # without media (Seerr answered but never added it) is deleted directly
        target = (f"media/{record['seerr_media_id']}" if record.get("seerr_media_id")
                  else f"request/{record['seerr_request_id']}" if record.get("seerr_request_id") else "")
        if target:
            status = c.request(f"{self.seerr.url}/api/v1/{target}", "DELETE", {"X-Api-Key": self.seerr.key}, timeout=30).status
            if not (200 <= status < 300 or status == 404):
                warn(f"Couldn't remove the test's Seerr entry (HTTP {status})")
                done = False
        if done:
            self.owned_file.unlink(missing_ok=True)
            shutil.rmtree(self.dir / "torrents", ignore_errors=True)
        else:
            warn(f"Some test items couldn't be removed; the next run retries (record: {self.owned_file})")
        return done

    def require_clean_slate(self) -> None:
        """Refuse to run if the test titles already exist and aren't the
        test's own: the test would change them and its cleanup would delete
        them. A failed check must not read as "not there"."""
        def read(app: App, path: str) -> Any:
            try:
                return app.call("GET", path)
            except ApiError:
                raise err(f"Couldn't reach {app.label} to check the test titles aren't in your library") from None
        movies, series, seerr = read(self.radarr, f"movie?tmdbId={MOVIE_TMDB}"), read(self.sonarr, "series"), read(self.seerr, f"movie/{MOVIE_TMDB}")
        found = ((" Tears of Steel is in Radarr;" if movies else "")
                 + (" Pioneer One is in Sonarr;" if any(s.get("tvdbId") == SERIES_TVDB for s in series or []) else "")
                 + (" Tears of Steel is in Seerr;" if ((seerr or {}).get("mediaInfo") or {}).get("id") else ""))
        if found:
            raise err(f"The test uses Tears of Steel and Pioneer One, and they're already in your library:{found} "
                      "it won't touch them. Remove them first to run the test.")

    # ─── The test ────────────────────────────────────────────────

    def link(self, src: Path, app: str, name: str) -> Path:
        """The source file as a finished download named <name>, with its torrent"""
        folder = self.complete / app / name
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{name}.mov"
        target.unlink(missing_ok=True)
        os.link(src, target)
        self.own("paths", folder)
        self.own("hashes", make_torrent(folder, self.dir / "torrents" / f"{name}.torrent"))
        return folder

    def queue_trouble(self, app: App) -> list:
        try:
            return [{"trackedDownloadState": r.get("trackedDownloadState"),
                     "msg": [m for s in r.get("statusMessages") or [] for m in s.get("messages") or []]}
                    for r in (app.call("GET", "queue") or {}).get("records", [])]
        except ApiError:
            return []

    def movie(self, src: Path) -> None:
        info("Movie: Tears of Steel (2012)")
        name = f"Tears.of.Steel.2012.1080p.WEB-DL.x264-{TAG}"
        self.link(src, "radarr", name)
        # Seerr has Radarr search as soon as the movie is added, and a public
        # release could win the race against the test release. Pause automatic
        # search and RSS on Radarr's indexers during the test; restored after
        # and on exit, whatever happens.
        self.pause_indexers()
        step("Requesting it in Seerr")
        try:
            request = self.seerr.call("POST", "request", {"mediaType": "movie", "mediaId": MOVIE_TMDB}) or {}
            # Recorded right away, so cleanup finds it even if Radarr never gets the movie
            self.own("seerr_request_id", request.get("id"))
            self.own("seerr_media_id", (request.get("media") or {}).get("id"))
            self.passes("Seerr accepted the request")
        except ApiError:
            self.fails("Seerr rejected the request (already in the library? run with a clean state)")
        movie_id = wait_until(90, lambda: self.radarr.call("GET", f"movie?tmdbId={MOVIE_TMDB}")[0]["id"])
        if not movie_id:
            self.fails("Seerr → Radarr: movie never appeared in Radarr")
            return
        self.own("movie_id", movie_id)
        self.passes(f"Seerr → Radarr: movie added ({self.radarr.call('GET', f'movie/{movie_id}').get('rootFolderPath')})")

        step("Handing Radarr a correctly named release")
        try:
            pushed = verdict(self.radarr.call("POST", "release/push", release(name)))
        except ApiError:
            pushed = "error"
        self.passes("Radarr approved the release") if pushed == "approved" else self.fails(f"Radarr: {pushed}")

        movie = wait_until(300, lambda: (m := self.radarr.call("GET", f"movie/{movie_id}")).get("hasFile") and m)
        if movie:
            # Recorded so cleanup can confirm Jellyfin dropped it (Radarr
            # renames imports, so the file doesn't carry the test tag)
            self.own("library_paths", movie["movieFile"]["path"])
            self.passes(f"qBittorrent → Radarr: imported automatically ({movie['movieFile'].get('relativePath')})")
        else:
            self.fails(f"Radarr did not import it within 5 minutes: {self.queue_trouble(self.radarr)}")

        if wait_until(180, lambda: any(i.get("ProviderIds", {}).get("Tmdb") == str(MOVIE_TMDB) for i in
                                       self.jellyfin.get("Items?Recursive=true&IncludeItemTypes=Movie&Fields=ProviderIds")["Items"])):
            self.passes("Radarr → Jellyfin: in the library")
        else:
            self.fails("Jellyfin didn't pick it up within 3 minutes")

        self.subtitles(movie_id)

        def available():
            # Starting a scan aborts one in progress, so only start one when idle
            jobs = self.seerr.call("GET", "settings/jobs") or []
            if not any(j.get("id") == "jellyfin-recently-added-scan" and j.get("running") for j in jobs):
                self.seerr.call("POST", "settings/jobs/jellyfin-recently-added-scan/run")
            return ((self.seerr.call("GET", f"movie/{MOVIE_TMDB}") or {}).get("mediaInfo") or {}).get("status") == 5
        if wait_until(180, available):
            self.passes("Jellyfin → Seerr: shown as available")
        else:
            self.fails("Seerr doesn't show it as available within 3 minutes")

    def subtitles(self, movie_id: int) -> None:
        """Bazarr syncs the movie from Radarr, searches the enabled providers
        and saves an .srt next to the movie"""
        headers = {"X-API-KEY": c.bazarr_key(self.cfg.paths.config)}

        def call(method: str, path: str) -> Any:
            return api.call(method, f"{self.cfg.urls.bazarr}/api/{path}", headers)
        try:
            call("POST", "system/tasks?taskid=update_movies")
        except ApiError:
            pass
        if not wait_until(120, lambda: call("GET", f"movies?radarrid%5B%5D={movie_id}")["data"]):
            self.fails("Bazarr never picked the movie up from Radarr")
            return
        try:
            call("PATCH", f"movies?radarrid={movie_id}&action=search-missing")
        except ApiError:
            pass

        def english():
            return [p.name for p in self.cfg.paths.movies.rglob("*.srt") if "Tears of Steel" in str(p) and ".en" in p.name]
        if found := wait_until(240, english):
            self.passes(f"Radarr → Bazarr: English subtitles downloaded ({' '.join(found)})")
        else:
            try:
                providers = ", ".join(call("GET", "system/settings")["general"]["enabled_providers"])
            except (ApiError, KeyError, TypeError):
                providers = "unknown"
            self.fails(f"Bazarr found no English subtitles within 4 minutes (providers: {providers})")

    def tv(self, src: Path) -> None:
        info("TV: Pioneer One S01E01")
        name = f"Pioneer.One.S01E01.1080p.WEB-DL.x264-{TAG}"
        self.link(src, "sonarr", name)
        series_id = None
        try:
            profile = next((p["id"] for p in self.sonarr.call("GET", "qualityprofile") or [] if p.get("name") == self.cfg.sonarr_profile), None)
            lookup = (self.sonarr.call("GET", f"series/lookup?term=tvdb:{SERIES_TVDB}") or [None])[0]
            if profile and lookup:
                series_id = (self.sonarr.call("POST", "series", {
                    **lookup, "qualityProfileId": profile, "rootFolderPath": str(self.cfg.paths.tv), "monitored": True,
                    "seasonFolder": True, "addOptions": {"monitor": "none", "searchForMissingEpisodes": False}}) or {}).get("id")
        except ApiError:
            pass
        if not series_id:
            self.fails("Sonarr: could not add Pioneer One")
            return
        self.own("series_id", series_id)
        self.passes("Sonarr: series added (no indexer search)")

        def s01e01():
            return next(e["id"] for e in self.sonarr.call("GET", f"episode?seriesId={series_id}")
                        if e.get("seasonNumber") == 1 and e.get("episodeNumber") == 1)
        # Monitoring set before Sonarr finishes adding the series gets reset
        episode_id = wait_until(5, s01e01, 1) if series_settled(self.sonarr, series_id, 120) else None
        if not episode_id:
            self.fails("Sonarr never listed Pioneer One S01E01, so TV import wasn't tested")
            return
        # Added with monitor "none" (so Sonarr searches nothing), which also
        # unmonitors the series; monitor the series and just this episode
        try:
            self.sonarr.call("PUT", f"series/{series_id}", {**self.sonarr.call("GET", f"series/{series_id}"), "monitored": True})
            self.sonarr.call("PUT", "episode/monitor", {"episodeIds": [episode_id], "monitored": True})
            pushed = verdict(self.sonarr.call("POST", "release/push", release(name)))
        except ApiError:
            pushed = "error"
        self.passes("Sonarr approved the release") if pushed == "approved" else self.fails(f"Sonarr: {pushed}")

        episode_path = ""
        if wait_until(300, lambda: self.sonarr.call("GET", f"episode/{episode_id}").get("hasFile")):
            episode_file = self.sonarr.call("GET", f"episodefile?seriesId={series_id}")[0]
            episode_path = episode_file["path"]
            self.own("library_paths", episode_path)
            self.passes(f"qBittorrent → Sonarr: imported automatically ({episode_file.get('relativePath')})")
        else:
            self.fails(f"Sonarr did not import it within 5 minutes: {self.queue_trouble(self.sonarr)}")
        # Episodes are titled by name ("Earthfall"), so match on the file path
        if episode_path and wait_until(180, lambda: any(i.get("Path") == episode_path for i in
                                                        self.jellyfin.get("Items?Recursive=true&IncludeItemTypes=Episode&Fields=Path")["Items"])):
            self.passes("Sonarr → Jellyfin: episode in the library")
        else:
            self.fails("Jellyfin didn't pick the episode up within 3 minutes")

    def source(self) -> Path:
        src = self.dir / "tears_of_steel_720p.mov"
        if not src.exists() or src.stat().st_size != SOURCE_SIZE:
            step("Downloading Tears of Steel (CC BY, 372 MB) from download.blender.org...")
            if subprocess.run(["curl", "-fL", "--progress-bar", "--max-time", "3600", "-o", str(src), SOURCE_URL]).returncode:
                raise err("Download failed")
        return src

    def run(self, keep: bool = False) -> int:
        """The number of failed steps"""
        started = time.time()
        info("End-to-end import test")
        (self.dir / "torrents").mkdir(parents=True, exist_ok=True)
        src = self.source()
        if not self.jellyfin.login():
            raise err("Could not log in to Jellyfin")
        # Leftovers recorded by an interrupted earlier run are removed first;
        # anything else with the test titles makes the test refuse to run
        if self.owned_file.exists():
            step("Removing what an interrupted earlier run left behind")
            if not self.cleanup():
                raise err(f"Could not clean up the earlier run; see {self.owned_file}")
        self.require_clean_slate()
        (self.dir / "torrents").mkdir(parents=True, exist_ok=True)
        server = serve(self.dir / "torrents")
        cleaned = False
        try:
            self.movie(src)
            self.tv(src)
            if not self.resume_indexers():
                self.fails("Radarr's indexers couldn't all be re-enabled (retried at the next run)")
            if keep:
                warn("--keep: leaving the test movie, series and torrents in place")
            else:
                info("Removing what the test added...")
                cleaned = True
                if self.cleanup():
                    ok(f"Test movie, series, request and torrents removed (the source file stays cached in {self.dir})")
                else:
                    self.fails("Cleanup incomplete")
        finally:
            # On any exit (failure, Ctrl-C): stop the server, restore the
            # indexers and remove what the test created, unless --keep
            server.shutdown()
            server.server_close()
            self.resume_indexers()
            if not keep and not cleaned:
                self.cleanup()
        print()
        if self.failed == 0:
            print(f"\033[1;32m   End-to-end test passed: {self.passed} steps in {int(time.time() - started)}s\033[0m")
        else:
            print(f"\033[1;31m   End-to-end test: {self.failed} of {self.passed + self.failed} steps failed\033[0m")
        return self.failed


class Tee:
    """Output to the terminal and the log file"""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text: str) -> int:
        self.log.write(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()
        self.log.flush()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = Config.load()
    if argv == ["--resume-indexers"]:
        # Radarr indexers an interrupted test left paused (run by install)
        return 0 if E2E(cfg).resume_indexers() else 1
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    log = cfg.paths.logs / f"e2e-{time.strftime('%Y%m%d_%H%M%S')}.log"
    with log.open("w") as f:
        sys.stdout, sys.stderr = Tee(sys.__stdout__, f), Tee(sys.__stderr__, f)
        try:
            failed = E2E(cfg).run(keep="--keep" in argv)
        except SetupError:
            failed = 1
        finally:
            print(f"   Log: {log}")
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    return min(failed, 125)
