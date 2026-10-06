"""The optional VPN kill switch (mediaserver/vpn.py keep, run by netwatch):
qBittorrent bound to the VPN's interface, kept bound (to nothing) while it's
down, SABnzbd paused meanwhile; off undoes it all.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mediaserver import common as c
from mediaserver import vpn


class FakeClients:
    def __init__(self):
        self.interface, self.sab, self.answer = "", [], True

    def bound(self):
        return self.interface if self.answer else None

    def bind(self, interface):
        self.interface = interface
        return True

    def sabnzbd(self, mode):
        self.sab.append(mode)
        return True


class KillSwitch(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        self.clients = FakeClients()
        self.route = "utun4"
        for patcher in (mock.patch.object(c, "notify"), mock.patch.object(c, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, **kw):
        c.write_json(self.state / "vpn.json", dict({"enabled": True, "interface": ""}, **kw))

    def round(self):
        return vpn.keep(self.state, self.clients, detect=lambda: self.route)  # type: ignore[arg-type]

    def test_bound_while_up_blocked_while_down_followed_on_reconnect(self):
        self.settings()
        self.assertEqual(self.round(), "up")
        self.assertEqual(self.clients.interface, "utun4")
        self.route = "en0"   # the VPN dropped: traffic would leave through Wi-Fi
        self.assertEqual(self.round(), "down")
        self.assertEqual(self.clients.interface, "utun4")   # still bound to the (gone) tunnel: no traffic
        self.assertEqual(self.clients.sab, ["pause"])
        self.round()
        self.assertEqual(self.clients.sab, ["pause"])   # paused once
        self.route = "utun7"   # back, under another name
        self.assertEqual(self.round(), "up")
        self.assertEqual(self.clients.interface, "utun7")
        self.assertEqual(self.clients.sab, ["pause", "resume"])

    def test_never_connected_means_no_network_for_torrents(self):
        self.settings()
        self.route = "en0"
        self.assertEqual(self.round(), "down")
        self.assertEqual(self.clients.interface, vpn.NONE_YET)

    def test_a_fixed_interface(self):
        self.settings(interface="ppp0")
        self.route = "en0"
        self.assertEqual(self.round(), "up")
        self.assertEqual(self.clients.interface, "ppp0")

    def test_switched_off_undoes_it(self):
        self.settings()
        self.route = "en0"
        self.round()
        self.settings(enabled=False)
        self.assertEqual(self.round(), "off")
        self.assertEqual(self.clients.interface, "")
        self.assertEqual(self.clients.sab, ["pause", "resume"])
        self.assertFalse((self.state / "vpn-status.json").exists())

    def test_off_by_default_touches_nothing(self):
        self.clients.interface = "en0"   # set by hand in qBittorrent
        self.assertEqual(self.round(), "off")
        self.assertEqual(self.clients.interface, "en0")

    def test_qbittorrent_not_answering(self):
        self.settings()
        self.clients.answer = False
        self.assertEqual(self.round(), "")

    def test_route_parsing(self):
        out = "   route to: 1.1.1.1\ndestination: default\n  interface: utun3\n      flags: <UP,DONE>\n"
        with mock.patch.object(vpn.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(vpn.route_interface(), "utun3")


if __name__ == "__main__":
    unittest.main()
