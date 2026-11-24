from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from spectra_alpha2 import desktop


class Alpha2DesktopTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), desktop.DesktopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health_endpoint_is_loopback_dashboard_only(self):
        with urlopen(self.base + "/api/health", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertTrue(payload["paper_only"])

    def test_dashboard_asset_is_served(self):
        with urlopen(self.base + "/", timeout=2) as response:
            html = response.read().decode()
        self.assertIn("Spectra Finance Alpha 2.0", html)
        self.assertIn("Run one paper cycle", html)
        self.assertIn("Update release", html)
        self.assertIn("data-action=\"update\"", html)
        self.assertIn("LIVE EXECUTION DISABLED", html)
        self.assertIn("const esc=", html)


    def test_host_header_blocks_dns_rebinding_reads(self):
        request = Request(self.base + "/api/health", headers={"Host": "attacker.invalid"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)

    def test_cross_origin_action_is_rejected_before_dispatch(self):
        request = Request(
            self.base + "/api/action",
            data=b'{"action":"once"}',
            headers={"Content-Type": "application/json", "Origin": "https://example.invalid"},
            method="POST",
        )
        with patch.object(desktop, "run_action") as run_action:
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 403)
            run_action.assert_not_called()

    def test_same_origin_action_uses_only_allowlisted_dispatch(self):
        request = Request(
            self.base + "/api/action",
            data=b'{"action":"once"}',
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
            },
            method="POST",
        )
        expected = {"ok": True, "action": "once", "returncode": 0, "output": "paper cycle"}
        with patch.object(desktop, "run_action", return_value=expected) as run_action:
            with urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(payload, expected)
            run_action.assert_called_once_with("once")

    def test_update_is_an_allowlisted_operator_action(self):
        self.assertIn("update", desktop.ALLOWED_ACTIONS)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            desktop.run_action("place-live-order")

    def test_dashboard_refuses_non_loopback_bind(self):
        with self.assertRaises(SystemExit):
            desktop.main(["--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
