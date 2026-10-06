"""nix run .#trash-sync (mediaserver/trash.py), against a stand-in for
TRaSH Guides: rules taken by trash_id, ours kept.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mediaserver import trash


class Sync(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "custom-formats"
        self.addCleanup(shutil.rmtree, self.folder.parent)
        (self.folder / "sonarr").mkdir(parents=True)
        (self.folder / "radarr").mkdir()
        (self.folder / "README.md").write_text("From TRaSH Guides (MIT, commit aaaaaaaaaaaa),\n")
        self.write("sonarr/tier.json", {"trash_id": "t1", "name": "Anime BD Tier 01", "mediaServerScore": 1400,
                                         "mediaServerProfiles": "anime", "specifications": [{"name": "old"}]})
        self.write("sonarr/ours.json", {"name": "Prefer HEVC", "mediaServerScore": 100, "specifications": []})
        self.write("sonarr/gone.json", {"trash_id": "t9", "name": "Retired", "specifications": []})

    def write(self, rel, data):
        (self.folder / rel).write_text(json.dumps(data))

    def read(self, rel):
        return json.loads((self.folder / rel).read_text())

    def test_rules_from_upstream_ours_kept(self):
        upstream = {"sonarr": {"t1": {"trash_id": "t1", "name": "Anime BD Tier 01 (Top SeaDex Muxers)", "trash_scores": {"default": 1400},
                                      "specifications": [{"name": "new"}]}}, "radarr": {}}
        report = trash.sync(self.folder, "b" * 40, upstream)
        tier = self.read("sonarr/tier.json")
        self.assertEqual(tier["specifications"], [{"name": "new"}])
        self.assertEqual((tier["name"], tier["mediaServerScore"], tier["mediaServerProfiles"]), ("Anime BD Tier 01", 1400, "anime"))
        self.assertNotIn("trash_scores", tier)   # not added where the file didn't have it
        self.assertEqual(self.read("sonarr/ours.json")["mediaServerScore"], 100)   # ours: untouched
        self.assertIn("sonarr/tier.json: updated", report)
        self.assertTrue(any("calls it 'Anime BD Tier 01 (Top SeaDex Muxers)' now" in line for line in report))
        self.assertTrue(any("gone.json: no longer in TRaSH Guides" in line for line in report))
        self.assertIn("commit bbbbbbbbbbbb", (self.folder / "README.md").read_text())

    def test_no_change_touches_nothing(self):
        upstream = {"sonarr": {"t1": {"trash_id": "t1", "name": "Anime BD Tier 01", "specifications": [{"name": "old"}]},
                               "t9": {"trash_id": "t9", "name": "Retired", "specifications": []}}, "radarr": {}}
        before = (self.folder / "sonarr/tier.json").read_text()
        self.assertEqual(trash.sync(self.folder, "c" * 40, upstream), [])
        self.assertEqual((self.folder / "sonarr/tier.json").read_text(), before)
        self.assertIn("commit aaaaaaaaaaaa", (self.folder / "README.md").read_text())   # no change: no new commit

    def test_the_real_files_are_all_valid(self):
        repo = Path(__file__).resolve().parent.parent / "custom-formats"
        for f in repo.glob("*/*.json"):
            data = json.loads(f.read_text())
            self.assertIn("name", data, f)
            self.assertIsInstance(data.get("specifications"), list, f)


if __name__ == "__main__":
    unittest.main()
