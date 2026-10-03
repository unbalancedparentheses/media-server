"""A service failing one request in the middle of an install: setup warns
or stops, never crashes, and never takes a failed read for "nothing there"
(which would add things twice). The next run, with the service answering
again, ends up exactly configured.

Each case breaks one endpoint of the fake services (HTTP 500), runs every
step, then runs them again with it working.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import re
from unittest import mock

from mediaserver.ui import SetupError
from tests.test_e2e import first
from tests.test_integration import Stack

# (service, method, path): one per thing setup reads or changes
CASES = [
    ("sonarr", "GET", r"/api/v3/rootfolder"), ("sonarr", "POST", r"/api/v3/rootfolder"),
    ("sonarr", "GET", r"/api/v3/downloadclient"), ("sonarr", "POST", r"/api/v3/downloadclient"),
    ("sonarr", "GET", r"/api/v3/notification"), ("sonarr", "GET", r"/api/v3/qualityprofile"),
    ("sonarr", "GET", r"/api/v3/config/naming"), ("sonarr", "PUT", r"/api/v3/config/naming(/\d+)?"),
    ("sonarr", "GET", r"/api/v3/config/mediamanagement"), ("sonarr", "GET", r"/api/v3/customformat"),
    ("sonarr", "GET", r"/api/v3/config/host"), ("sonarr", "GET", r"/api/v3/series"),
    ("radarr", "GET", r"/api/v3/rootfolder"), ("radarr", "GET", r"/api/v3/downloadclient"),
    ("radarr", "GET", r"/api/v3/customformat"), ("radarr", "POST", r"/api/v3/customformat"),
    ("radarr", "GET", r"/api/v3/qualityprofile"), ("radarr", "GET", r"/api/v3/movie"),
    ("prowlarr", "GET", r"/api/v1/applications"), ("prowlarr", "POST", r"/api/v1/applications"),
    ("prowlarr", "GET", r"/api/v1/indexer"), ("prowlarr", "POST", r"/api/v1/indexer"),
    ("prowlarr", "GET", r"/api/v1/tag"), ("prowlarr", "GET", r"/api/v1/indexerProxy"),
    ("prowlarr", "GET", r"/api/v1/indexer/schema"),
    ("seerr", "GET", r"/api/v1/settings/sonarr"), ("seerr", "GET", r"/api/v1/settings/radarr"),
    ("seerr", "POST", r"/api/v1/settings/radarr"), ("seerr", "GET", r"/api/v1/settings/main"),
    ("seerr", "GET", r"/api/v1/user"),
    ("jellyfin", "GET", r"/Library/VirtualFolders"), ("jellyfin", "GET", r"/Auth/Keys"),
    ("jellyfin", "GET", r"/Plugins"), ("jellyfin", "GET", r"/Repositories"),
    ("bazarr", "POST", r"/api/system/settings"), ("bazarr", "GET", r"/api/system/languages/profiles"),
    ("qbittorrent", "GET", r"/api/v2/app/preferences"), ("qbittorrent", "GET", r"/api/v2/torrents/categories"),
    ("sabnzbd", "POST", r"/api"),
    ("cleanuparr", "GET", r"/api/configuration/download_client"), ("cleanuparr", "GET", r"/api/configuration/sonarr"),
    ("cleanuparr", "GET", r"/api/configuration/queue_cleaner"),
]


class Broken(Stack):
    def setUp(self):
        super().setUp()
        # Retries after a failure pause; not here
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, service, method, pattern):
        svc = getattr(self.stack, service)
        broken = first(svc, method, pattern)(lambda req, *args: (500, {"error": "broken for the test"}))
        try:
            self.run_steps()
        except SetupError:
            pass   # stopping is fine; crashing (any other exception) isn't
        self.assertIn((method, True), [(m, bool(re.fullmatch(pattern + "/?", p))) for m, p in svc.requests],
                      f"{service} {method} {pattern} was never called, so the case tests nothing")
        svc.routes = [r for r in svc.routes if r[2] is not broken]
        out = self.run_steps()
        self.assertNotIn("✗", out)
        self.configured_once()

    def configured_once(self):
        s, p = self.stack, self.cfg.paths
        self.assertEqual(sorted(r["path"] for r in s.sonarr.resources["rootfolder"].items), sorted([str(p.tv), str(p.anime)]))
        self.assertEqual([r["path"] for r in s.radarr.resources["rootfolder"].items], [str(p.movies)])
        for arr in (s.sonarr, s.radarr):
            self.assertEqual(sorted(x["implementation"] for x in arr.resources["downloadclient"].items), ["QBittorrent", "Sabnzbd"])
            self.assertEqual([n["implementation"] for n in arr.resources["notification"].items], ["MediaBrowser"])
        names = [x["name"] for x in s.radarr.resources["customformat"].items]
        self.assertEqual(len(names), len(set(names)), "a custom format added twice")
        self.assertEqual([x["name"] for x in s.sonarr.resources["qualityprofile"].items].count("Anime"), 1)
        self.assertEqual(sorted(a["name"] for a in s.prowlarr.resources["applications"].items), ["Radarr", "Sonarr"])
        self.assertEqual(sorted(i["name"] for i in s.prowlarr.resources["indexer"].items), ["1337x", "Nyaa.si"])
        self.assertEqual(len(s.prowlarr.resources["tag"].items), 1)
        self.assertEqual((len(s.seerr.sonarr.items), len(s.seerr.radarr.items)), (1, 1))
        self.assertEqual(sorted(f["Name"] for f in s.jellyfin.folders), ["Anime", "Movies", "TV Shows"])
        self.assertEqual(len(s.jellyfin.keys), 1)


def case_test(service, method, pattern):
    return lambda self: self.check(service, method, pattern)


for _service, _method, _pattern in CASES:
    _slug = re.sub(r"\W+", "_", _pattern).strip("_")
    setattr(Broken, f"test_{_service}_{_method.lower()}_{_slug}", case_test(_service, _method, _pattern))
