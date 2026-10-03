"""Why a request isn't arriving, and the bounded recovery
(mediaserver/stuck.py), against fake Sonarr/Radarr/Prowlarr.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from mediaserver import stuck

DAY = 86400


def release(approved=False, rejections=(), quality="HDTV-720p", seeders=10, protocol="torrent"):
    return {"approved": approved, "rejections": list(rejections), "seeders": seeders, "protocol": protocol,
            "quality": {"quality": {"name": quality}}}


class Classify(unittest.TestCase):
    def test_nothing_found(self):
        self.assertEqual(stuck.classify([], False)["code"], "no_results")
        self.assertEqual(stuck.classify([], True)["code"], "indexers_down")

    def test_quality_only(self):
        d = stuck.classify([release(rejections=["Quality HDTV-720p is not wanted in profile"]),
                            release(rejections=["720p is not wanted in profile"], quality="WEBDL-720p")], False)
        self.assertEqual(d["code"], "quality")
        self.assertTrue(d["only_quality"])
        self.assertIn("found: HDTV-720p, WEBDL-720p", d["text"])

    def test_the_most_common_reason_wins(self):
        d = stuck.classify([release(rejections=["Language is not wanted in profile"])] * 3
                           + [release(rejections=["Quality is not wanted in profile"])], False)
        self.assertEqual((d["code"], d["found"]), ("language", 4))
        self.assertIn("(4 found)", d["text"])
        self.assertFalse(d.get("only_quality"))

    def test_other_reasons(self):
        for reason, code in (("Release is blocklisted", "blocklisted"), ("Custom Formats x have score -10000 below minimum", "filters"),
                             ("Size 90 GB is larger than maximum allowed", "size"), ("Not enough seeders: 0", "peers"),
                             ("Unknown Movie", "mismatch")):
            self.assertEqual(stuck.classify([release(rejections=[reason])], False)["code"], code, reason)
        odd = stuck.classify([release(rejections=["Something new in Sonarr"])], False)
        self.assertEqual(odd["code"], "other")
        self.assertIn("Something new in Sonarr", odd["text"])

    def test_already_downloading_isnt_quality(self):
        """Radarr's 'Quality for release in queue already meets cutoff' means
        a release is downloading (seen live on Spectre)"""
        d = stuck.classify([release(rejections=["Quality for release in queue already meets cutoff: Remux-1080p v1"])] * 5
                           + [release(rejections=["Bluray-720p is not wanted in profile"])], False)
        self.assertEqual(d["code"], "queued")

    def test_approved(self):
        self.assertEqual(stuck.classify([release(approved=True)], False)["code"], "usable")
        self.assertEqual(stuck.classify([release(approved=True, seeders=0)], False)["code"], "peers")
        self.assertEqual(stuck.classify([release(approved=True, seeders=0, protocol="usenet")], False)["code"], "usable")


class FakeApp:
    def __init__(self, name, answers):
        self.name, self.calls = name, []
        self.answers: Any = answers

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        answer = self.answers(method, path, body)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Recovery(unittest.TestCase):
    def setUp(self):
        self.movies = [{"id": 1, "title": "Spectre", "year": 2015, "monitored": True, "hasFile": False, "isAvailable": True},
                       {"id": 2, "title": "Have", "year": 2020, "monitored": True, "hasFile": True, "isAvailable": True},
                       {"id": 3, "title": "Not Out", "year": 2027, "monitored": True, "hasFile": False, "isAvailable": False},
                       {"id": 4, "title": "Unwanted", "year": 2001, "monitored": False, "hasFile": False, "isAvailable": True},
                       {"id": 5, "title": "Casino Royale", "year": 2006, "monitored": True, "hasFile": False, "isAvailable": True},
                       {"id": 6, "title": "Third", "year": 2010, "monitored": True, "hasFile": False, "isAvailable": True},
                       {"id": 7, "title": "Fourth", "year": 2011, "monitored": True, "hasFile": False, "isAvailable": True}]
        self.episodes = [{"seriesId": 9, "seasonNumber": 1, "series": {"title": "Kaiji"}}] * 3 + \
                        [{"seriesId": 9, "seasonNumber": 2, "series": {"title": "Kaiji"}}]
        self.releases = {}
        self.radarr = FakeApp("Radarr", self.radarr_answers)
        self.sonarr = FakeApp("Sonarr", lambda m, p, b: {"records": self.episodes} if p.startswith("wanted/missing") else
                              self.releases.get(p, []) if p.startswith("release") else [] if p.startswith("indexer") else {})
        self.prowlarr = FakeApp("Prowlarr", lambda m, p, b: self.prowlarr_answer(m, p))
        self.statuses = []
        self.downloading = []
        self.profiles = [{"id": 1, "name": "HD-1080p", "cutoff": 7, "minFormatScore": 0, "formatItems": [{"format": 3, "score": -10000}],
                          "language": {"id": 1}, "items": [
                              {"quality": {"id": 4, "name": "HDTV-720p"}, "allowed": False},
                              {"name": "WEB 720p", "allowed": False, "items": [{"quality": {"id": 5, "name": "WEBDL-720p"}, "allowed": False}]},
                              {"quality": {"id": 7, "name": "Bluray-1080p"}, "allowed": True},
                              {"quality": {"id": 1, "name": "SDTV"}, "allowed": False}]}]
        self.state = {}
        self.settings = {"search_missing": True, "fallback_resolution": ""}
        self.logs = []
        patcher = mock.patch.object(stuck.c, "log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def radarr_answers(self, method, path, body):
        if path == "movie":
            return self.movies
        if path.startswith("queue"):
            return {"records": [{"movieId": m} for m in self.downloading]}
        if path.startswith("release"):
            return self.releases.get(path, [])
        if path == "qualityprofile" and method == "GET":
            return self.profiles
        if path == "qualityprofile" and method == "POST":
            self.profiles.append(dict(body, id=len(self.profiles) + 1))
            return self.profiles[-1]
        if path.startswith("movie/") and method == "GET":
            return {"id": int(path.split("/")[1]), "qualityProfileId": 1}
        if path.startswith("indexer"):
            return []
        return {}

    def prowlarr_answer(self, method, path):
        if path == "indexerstatus":
            return self.statuses
        if path == "indexer/testall":
            return urllib.error.HTTPError("x", 400, "some still fail", {}, None)  # type: ignore[arg-type]
        return {}

    def round(self, at):
        stuck.Stuck([self.sonarr, self.radarr, self.prowlarr], self.settings, self.state, now=at).run()

    def searches(self, app):
        return [b for m, p, b in app.calls if p == "command"]

    def test_whats_missing(self):
        missing = stuck.Stuck([self.sonarr, self.radarr], self.settings, self.state, now=0).missing()
        self.assertEqual(sorted(missing), ["radarr:1", "radarr:5", "radarr:6", "radarr:7", "sonarr:9:1", "sonarr:9:2"])
        self.assertEqual(missing["sonarr:9:1"]["episodes"], 3)
        self.assertEqual(missing["sonarr:9:1"]["search"], {"name": "SeasonSearch", "seriesId": 9, "seasonNumber": 1})

    def test_downloading_isnt_missing(self):
        self.downloading = [1]
        missing = stuck.Stuck([self.sonarr, self.radarr], self.settings, self.state, now=0).missing()
        self.assertNotIn("radarr:1", missing)

    def test_a_few_searches_an_hour_waiting_longer_each_time(self):
        self.round(0)
        first = self.searches(self.radarr) + self.searches(self.sonarr)
        self.assertEqual(len(first), stuck.SEARCHES_PER_RUN)
        self.round(1800)   # within the hour: nothing
        self.assertEqual(len(self.searches(self.radarr) + self.searches(self.sonarr)), stuck.SEARCHES_PER_RUN)
        # Over the next hours everything gets its turn, and each waits longer
        for hour in range(1, 30):
            self.round(hour * 3600)
        spectre = self.state["items"]["radarr:1"]
        self.assertGreaterEqual(spectre["searches"], 3)
        waits = [stuck.wait_after(n) for n in (1, 2, 3, 4, 5, 6, 7)]
        self.assertEqual(waits, [3600, 7200, 14400, 28800, 57600, 86400, 86400])

    def test_switched_off_asks_the_indexers_nothing(self):
        self.round(0)
        self.settings["search_missing"] = False
        for app in (self.radarr, self.sonarr, self.prowlarr):
            app.calls.clear()
        for hour in range(1, 40):
            self.round(hour * 3600)
        self.assertEqual(self.radarr.calls + self.sonarr.calls + self.prowlarr.calls, [])
        self.assertEqual(self.state["items"], {})   # no old schedule shown

    def test_downloading_keeps_its_history(self):
        for hour in range(0, 4):
            self.round(hour * 3600)
        searches = self.state["items"]["radarr:1"]["searches"]
        self.assertGreater(searches, 0)
        self.downloading = [1]   # grabbed: downloading now
        self.round(5 * 3600)
        self.assertTrue(self.state["items"]["radarr:1"]["downloading"])
        self.downloading = []   # the download failed: missing again, same schedule
        self.round(6 * 3600)
        self.assertEqual(self.state["items"]["radarr:1"]["searches"] >= searches, True)
        self.assertNotIn("downloading", self.state["items"]["radarr:1"])

    def test_what_arrived_is_forgotten(self):
        self.round(0)
        self.movies[0]["hasFile"] = True
        self.round(3600)
        self.assertNotIn("radarr:1", self.state["items"])

    def test_diagnosed_after_a_day_then_twice_a_day_at_most(self):
        self.releases["release?movieId=1"] = [release(rejections=["Quality is not wanted in profile"])]
        self.round(0)
        self.assertNotIn("diagnosis", self.state["items"]["radarr:1"])   # missing for under a day
        for hour in range(24, 36):
            self.round(hour * 3600)
        asked = [p for m, p, b in self.radarr.calls if p == "release?movieId=1"]
        self.assertEqual(len(asked), 1)   # once in those 12 hours
        self.assertEqual(self.state["items"]["radarr:1"]["diagnosis"]["code"], "quality")
        self.assertIn("Releases found, but none in a quality", stuck.explain(self.state["items"]["radarr:1"], now=36 * 3600))

    def test_missing_since_it_was_added_not_since_first_seen(self):
        self.movies[0]["added"] = "1970-01-01T00:00:00Z"   # long ago
        self.releases["release?movieId=1"] = [release()]
        self.round(5 * DAY)
        self.assertEqual(self.state["items"]["radarr:1"]["since"], 0)
        self.assertIn("diagnosis", self.state["items"]["radarr:1"])   # no extra day's wait

    def test_usable_release_is_searched_for_right_away(self):
        self.releases["release?movieId=5"] = [release(approved=True)]
        for hour in (0, 24):
            self.round(hour * 3600)
        self.assertEqual(self.state["items"]["radarr:5"]["diagnosis"]["code"], "usable")
        self.assertEqual(self.state["items"]["radarr:5"]["next_search"], 24 * 3600)

    def test_fallback_only_when_asked_and_after_a_week(self):
        self.releases["release?movieId=1"] = [release(rejections=["Quality is not wanted in profile"])]
        for day in range(0, 9):
            self.round(day * DAY)
        self.assertFalse([c for c in self.radarr.calls if c[0] in ("PUT", "POST") and c[1] != "command"])   # not set: never
        self.settings["fallback_resolution"] = "720p"
        self.round(9 * DAY)
        wider = next(p for p in self.profiles if p["name"] == "HD-1080p (+720p)")
        puts = [(p, b["qualityProfileId"]) for m, p, b in self.radarr.calls if m == "PUT" and p.startswith("movie/")]
        self.assertIn(("movie/1", wider["id"]), puts)
        self.assertEqual(self.state["items"]["radarr:1"]["fallback"], "HD-1080p (+720p)")
        self.assertIn("now also accepts lower resolutions", stuck.explain(self.state["items"]["radarr:1"]))

    def test_the_copy_only_adds_the_resolution(self):
        wider = stuck.widened(self.profiles[0], "720p")
        allowed = {(i.get("quality") or {}).get("name") or i.get("name"): i["allowed"] for i in wider["items"]}
        self.assertEqual(allowed, {"HDTV-720p": True, "WEB 720p": True, "Bluray-1080p": True, "SDTV": False})
        self.assertTrue(wider["items"][1]["items"][0]["allowed"])
        for kept in ("cutoff", "minFormatScore", "formatItems", "language"):
            self.assertEqual(wider[kept], self.profiles[0][kept], kept)
        self.assertNotIn("id", wider)
        self.assertFalse(self.profiles[0]["items"][0]["allowed"])   # the original is untouched

    def test_outdated_copy_brought_in_step(self):
        """The copy already exists but the original's minimum score changed:
        the copy follows, not only on qualities"""
        self.settings["fallback_resolution"] = "720p"
        old = stuck.widened(self.profiles[0], "720p")
        self.profiles.append(dict(old, id=2))
        self.profiles[0]["minFormatScore"] = 50
        self.releases["release?movieId=1"] = [release(rejections=["Quality is not wanted in profile"])]
        for day in range(0, 9):
            self.round(day * DAY)
        puts = [b for m, p, b in self.radarr.calls if m == "PUT" and p == "qualityprofile/2"]
        self.assertEqual(puts[-1]["minFormatScore"], 50)

    def test_copy_of_a_copy_keeps_the_origin_name(self):
        twice = stuck.widened(stuck.widened(self.profiles[0], "720p"), "720p")
        self.assertEqual(twice["name"], "HD-1080p (+720p)")

    def test_no_fallback_when_quality_isnt_the_only_reason(self):
        self.settings["fallback_resolution"] = "720p"
        self.releases["release?movieId=1"] = [release(rejections=["Quality is not wanted in profile"]),
                                              release(rejections=["Language is not wanted in profile"])]
        for day in range(0, 9):
            self.round(day * DAY)
        self.assertFalse([c for c in self.radarr.calls if c[0] in ("PUT", "POST") and c[1] != "command"])

    def test_indexers_retested_when_some_are_off(self):
        self.round(0)
        self.assertNotIn(("POST", "indexer/testall", None), self.prowlarr.calls)   # all fine: no test
        self.statuses = [{"indexerId": 1, "disabledTill": "2099-01-01T00:00:00Z"}]
        self.round(7 * 3600)
        self.round(8 * 3600)   # not again within 6 hours
        self.assertEqual([c for c in self.prowlarr.calls if c[1] == "indexer/testall"], [("POST", "indexer/testall", None)])

    def test_search_failing_is_tried_again(self):
        self.radarr.answers = lambda m, p, b: OSError("down") if p == "command" else self.radarr_answers(m, p, b)
        self.round(0)
        self.assertEqual(self.state["items"]["radarr:1"]["searches"], 0)

    def test_offline_and_state_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.assertIsNone(stuck.run([self.radarr], self.settings, root / "stuck.json", offline=True))
        self.assertEqual(self.radarr.calls, [])
        state = stuck.run([self.radarr], self.settings, root / "stuck.json", offline=False)
        self.assertIn("radarr:1", state["items"])
        self.assertTrue((root / "stuck.json").exists())


class Dashboard(unittest.TestCase):
    def test_search_stage_says_why(self):
        from mediaserver import dashmedia as dm
        item = {"season": 1, "searches": 2, "next_search": 10 ** 10,
                "diagnosis": {"text": "No releases found on your indexers", "action": "Searched again regularly"}}
        stages = dm.with_diagnosis([{"name": "searching", "state": "active", "detail": "looking for a good release"}], [item, dict(item, season=2)])
        self.assertTrue(stages[0]["diagnosed"])
        self.assertTrue(stages[0]["detail"].startswith("season 1: No releases found on your indexers · Searched again regularly"))
        self.assertIn("searched again 2×", stages[0]["detail"])
        upcoming = dm.with_diagnosis([{"name": "searching", "state": "active", "detail": "not out yet · digital Nov 15"}], [item])
        self.assertNotIn("diagnosed", upcoming[0])


if __name__ == "__main__":
    unittest.main()
