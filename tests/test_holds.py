"""Downloads held for space (diskwatch) or a VPN that's down (vpn.py),
through one shared record (holds.py): reconciled with what the clients
actually do every round, resumed only when nothing holds them, a pause by
hand kept; and the VPN's interface checks and binding read-back.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import diskwatch, holds, vpn


class Clients:
    """qBittorrent and SABnzbd as they'd answer, with ways to fail"""

    def __init__(self):
        self.list = [{"hash": "a", "state": "downloading", "progress": 0.3},
                     {"hash": "b", "state": "stalledDL", "progress": 0.1},
                     {"hash": "s", "state": "uploading", "progress": 1.0}]
        self.sab_paused, self.interface = False, ""
        self.ignore_stop = self.ignore_bind = False
        self.down = False

    def torrents(self):
        return None if self.down else [dict(t) for t in self.list]

    def torrent_action(self, action, hashes):
        if action == "stop" and self.ignore_stop:
            return True   # says yes, does nothing
        for t in self.list:
            if t["hash"] in hashes:
                t["state"] = "stoppedDL" if action == "stop" else "downloading"
        return True

    def has_sabnzbd(self):
        return True

    def sabnzbd_paused(self):
        return self.sab_paused

    def sabnzbd(self, mode):
        self.sab_paused = mode == "pause"
        return True

    def bound(self):
        return None if self.down else self.interface

    def bind(self, name):
        if not self.ignore_bind:
            self.interface = name
        return True

    def states(self):
        return {t["hash"]: t["state"] for t in self.list}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "netwatch").mkdir()
        self.clients = Clients()
        for patcher in (mock.patch.object(c, "notify"), mock.patch.object(c, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def disk(self, free, enabled=True):
        return diskwatch.protect(self.root, self.root, 10, 20, enabled, self.clients, free_gb=free)  # type: ignore[arg-type]

    def vpn_round(self, route="utun4", up=True, **settings):
        c.write_json(self.root / "netwatch/vpn.json", dict({"enabled": True, "interface": ""}, **settings))
        return vpn.keep(self.root / "netwatch", self.clients, detect=lambda: route, is_up=lambda name: up)  # type: ignore[arg-type]


class Disk(Base):
    def test_paused_then_resumed_with_room_to_spare(self):
        self.assertEqual(self.disk(100), "")
        self.assertEqual(self.disk(25), "paused")
        self.assertEqual(self.clients.states(), {"a": "stoppedDL", "b": "stoppedDL", "s": "uploading"})   # seeding untouched
        self.assertTrue(self.clients.sab_paused)
        self.assertEqual(self.disk(35), "held")
        self.assertEqual(self.disk(41), "resumed")
        self.assertEqual(self.clients.states()["a"], "downloading")
        self.assertFalse(self.clients.sab_paused)
        self.disk(41)   # confirmed on the next check: nothing left
        self.assertFalse(holds.file(self.root).exists())

    def test_a_pause_that_didnt_take_is_retried(self):
        self.clients.ignore_stop = True
        self.disk(25)
        self.assertEqual(self.clients.states()["a"], "downloading")
        self.clients.ignore_stop = False
        self.assertEqual(self.disk(25), "held")   # still moving: stopped again
        self.assertEqual(self.clients.states()["a"], "stoppedDL")

    def test_new_downloads_while_held_are_paused_too(self):
        self.disk(25)
        self.clients.list.append({"hash": "new", "state": "downloading", "progress": 0})
        self.disk(24)
        self.assertEqual(self.clients.states()["new"], "stoppedDL")
        self.disk(50)
        self.assertEqual(self.clients.states()["new"], "downloading")

    def test_sabnzbd_paused_by_hand_stays_paused(self):
        self.clients.sab_paused = True
        self.disk(25)
        self.disk(50)
        self.assertTrue(self.clients.sab_paused)

    def test_qbittorrent_not_answering_keeps_trying(self):
        self.disk(25)
        self.clients.down = True
        self.assertEqual(self.disk(50), "resumed")   # released, not yet confirmed
        self.clients.down = False
        self.disk(50)
        self.assertEqual(self.clients.states()["a"], "downloading")


class Shared(Base):
    def test_disk_and_vpn_dont_undo_each_other(self):
        self.disk(25)                        # disk holds torrents and SABnzbd
        self.vpn_round(route="en0")          # VPN down: holds SABnzbd too
        self.disk(50)                        # space is back…
        self.assertTrue(self.clients.sab_paused)   # …but the VPN still holds SABnzbd
        self.assertEqual(self.clients.states()["a"], "downloading")   # torrents: only disk held them
        self.vpn_round(route="utun4")        # VPN back: nothing holds SABnzbd
        self.assertFalse(self.clients.sab_paused)

    def test_vpn_back_while_disk_still_low(self):
        self.vpn_round(route="en0")
        self.disk(25)
        self.vpn_round(route="utun4")
        self.assertTrue(self.clients.sab_paused)   # disk still holds it


class Vpn(Base):
    def test_bound_while_up_blocked_while_down(self):
        self.assertEqual(self.vpn_round(), "up")
        self.assertEqual(self.clients.interface, "utun4")
        status = c.read_json(self.root / "netwatch/vpn-status.json")
        self.assertTrue(status["protected"])
        self.assertEqual(self.vpn_round(route="en0"), "down")
        self.assertEqual(self.clients.interface, "utun4")   # bound to the gone tunnel: no traffic
        self.assertTrue(self.clients.sab_paused)
        self.assertEqual(self.vpn_round(route="utun7"), "up")
        self.assertEqual(self.clients.interface, "utun7")
        self.assertFalse(self.clients.sab_paused)

    def test_a_configured_interface_must_exist(self):
        self.assertEqual(self.vpn_round(route="en0", up=False, interface="utun4"), "down")
        self.assertTrue(self.clients.sab_paused)
        self.assertEqual(self.clients.interface, "utun4")
        self.assertEqual(self.vpn_round(route="en0", up=True, interface="utun4"), "up")

    def test_a_tunnel_without_an_address_is_down(self):
        self.assertEqual(self.vpn_round(route="utun4", up=False), "down")

    def test_never_connected_means_no_network_for_torrents(self):
        self.vpn_round(route="en0")
        self.assertEqual(self.clients.interface, vpn.NONE_YET)

    def test_a_binding_that_didnt_take_isnt_reported_protected(self):
        self.clients.ignore_bind = True
        self.vpn_round()
        self.assertFalse(c.read_json(self.root / "netwatch/vpn-status.json")["protected"])

    def test_switched_off_undoes_it(self):
        self.vpn_round(route="en0")
        c.write_json(self.root / "netwatch/vpn.json", {"enabled": False})
        self.assertEqual(vpn.keep(self.root / "netwatch", self.clients), "off")  # type: ignore[arg-type]
        self.assertEqual(self.clients.interface, "")
        self.assertFalse(self.clients.sab_paused)

    def test_off_by_default_asks_nothing(self):
        self.clients.down = True   # would fail if asked
        self.assertEqual(vpn.keep(self.root / "netwatch", self.clients), "off")  # type: ignore[arg-type]

    def test_route_and_interface_parsing(self):
        with mock.patch.object(vpn.subprocess, "run", return_value=mock.Mock(stdout="  interface: utun3\n", returncode=0)):
            self.assertEqual(vpn.route_interface(), "utun3")
        up = "utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380\n\tinet 10.64.0.2 --> 10.64.0.2 netmask 0xff000000\n"
        with mock.patch.object(vpn.subprocess, "run", return_value=mock.Mock(stdout=up, returncode=0)):
            self.assertTrue(vpn.interface_up("utun3"))
        bare = "utun3: flags=8051<UP> mtu 1380\n\tinet6 fe80::1%utun3 prefixlen 64 scopeid 0x10\n"
        with mock.patch.object(vpn.subprocess, "run", return_value=mock.Mock(stdout=bare, returncode=0)):
            self.assertFalse(vpn.interface_up("utun3"))   # only a link-local address: carries nothing
        with mock.patch.object(vpn.subprocess, "run", return_value=mock.Mock(stdout="", returncode=1)):
            self.assertFalse(vpn.interface_up("utun9"))


if __name__ == "__main__":
    unittest.main()
