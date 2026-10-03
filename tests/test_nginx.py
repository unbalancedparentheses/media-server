"""The dashboard's nginx config, rendered from the templates as setup does,
must be valid nginx (nginx -t). nix run .#unit provides nginx.

Run: nix run .#unit   (or: python3 -m unittest discover -s tests -t .)
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mediaserver.steps.host import REPO, render


@unittest.skipUnless(shutil.which("nginx"), "needs nginx")
class NginxConfig(unittest.TestCase):
    def test_templates_render_to_valid_config(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        (out / "temp").mkdir()
        (out / "www").mkdir()
        mime = Path(shutil.which("nginx") or "").resolve().parent.parent / "conf/mime.types"
        key = "0123456789abcdef"
        for admin_host in ("127.0.0.1", "192.168.1.10"):
            values = {"NGINX_MIME_TYPES": mime, "DASHBOARD_PORT": 8080, "ADMIN_HOST": admin_host,
                      "JELLYFIN_API_KEY": key, "SEERR_KEY": key, "SABNZBD_KEY": key}
            (out / "nginx.conf").write_text(render(REPO / "templates/nginx.conf.tpl", values))
            (out / "api-proxy.conf").write_text(render(REPO / "templates/nginx.api-proxy.conf.tpl", values))
            self.assertNotIn("{{", (out / "nginx.conf").read_text() + (out / "api-proxy.conf").read_text())
            r = subprocess.run(["nginx", "-t", "-e", "stderr", "-p", str(out), "-c", str(out / "nginx.conf")],
                               capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
