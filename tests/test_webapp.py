"""Read-only dashboard payload and route tests without network access."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware/app_a"))
from webapp import build_status_payload, register_routes

try:
    from microdot import Microdot
    from microdot.test_client import TestClient
except ImportError:
    Microdot = None


class FakeTelemetry:
    def snapshot(self):
        return {
            "source": "simulated", "simulated": True, "hardware_ready": False,
            "control_policy_ready": False, "stale": False, "sequence": 7,
            "environment": {"temperature_c": 26.5, "humidity_percent": 55},
            "fans": [{"id": 1, "group": 1, "rpm": 600, "pwm_percent": 30}],
        }


class FakeEthernet:
    def status(self):
        return {"interface": "w5500", "phase": "online", "ready": True,
                "ipv4": ("192.168.1.20", "255.255.255.0", "192.168.1.1", "192.168.1.1"),
                "error": None}


class FakeApp:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        def decorator(handler):
            self.routes[path] = handler
            return handler
        return decorator


class DashboardTests(unittest.TestCase):
    def test_dashboard_declares_both_supported_languages(self):
        static = Path(__file__).resolve().parents[1] / "firmware/app_a/static"
        html = static.joinpath("index.html").read_text()
        script = static.joinpath("app.js").read_text()
        self.assertIn('option value="en"', html)
        self.assertIn('option value="zh-CN"', html)
        self.assertIn('class="details-grid"', html)
        self.assertIn('"zh-CN": {', script)
        self.assertIn('noun_Fan_8413191.svg', static.joinpath("app.css").read_text())
        self.assertIn('localStorage.setItem(LANGUAGE_STORAGE_KEY, language)', script)

    def test_status_payload_exposes_stable_browser_fields(self):
        value = build_status_payload(FakeTelemetry(), FakeEthernet(), "1.2.3", "ABC123")
        self.assertEqual(value["schema"], 1)
        self.assertEqual(value["environment"]["temperature_c"], 26.5)
        self.assertEqual(value["fans"][0]["rpm"], 600)
        self.assertEqual(value["network"]["ip"], "192.168.1.20")
        self.assertEqual(value["network"]["gateway"], "192.168.1.1")
        self.assertEqual(value["system"]["device_id"], "ABC123")
        self.assertTrue(value["system"]["simulated"])

    def test_missing_address_is_represented_without_index_errors(self):
        ethernet = FakeEthernet()
        ethernet.status = lambda: {"interface": "w5500", "phase": "waiting_ip",
                                   "ready": False, "ipv4": None, "error": "waiting"}
        value = build_status_payload(FakeTelemetry(), ethernet, "0.1.0", "TEST")
        self.assertIsNone(value["network"]["ip"])
        self.assertEqual(value["network"]["error"], "waiting")

    def test_routes_serve_dashboard_and_reject_unknown_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "index.html").write_bytes(b"<h1>UniFan</h1>")
            Path(directory, "app.css").write_bytes(b":root{}")
            Path(directory, "noun_Fan_8413191.svg").write_bytes(b"<svg></svg>")
            app = FakeApp()
            register_routes(app, FakeTelemetry(), FakeEthernet(),
                            SimpleNamespace(version="0.1.0"), directory, "TEST")

            index = asyncio.run(app.routes["/"](object()))
            self.assertEqual(index[0], b"<h1>UniFan</h1>")
            self.assertEqual(index[2]["Cache-Control"], "no-store")

            css = asyncio.run(app.routes["/<path:path>"](object(), "app.css"))
            self.assertEqual(css[0], b":root{}")
            self.assertEqual(css[2]["Content-Type"], "text/css; charset=utf-8")

            svg = asyncio.run(app.routes["/<path:path>"](object(), "noun_Fan_8413191.svg"))
            self.assertEqual(svg[0], b"<svg></svg>")
            self.assertEqual(svg[2]["Content-Type"], "image/svg+xml")

            missing = asyncio.run(app.routes["/<path:path>"](object(), "secret.py"))
            self.assertEqual(missing[1], 404)

            status = asyncio.run(app.routes["/api/status"](object()))
            self.assertEqual(status[0]["system"]["version"], "0.1.0")
            self.assertEqual(status[2]["Cache-Control"], "no-store")


@unittest.skipIf(Microdot is None, "Install requirements-dev.txt for real Microdot tests")
class MicrodotDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_route_wins_over_static_catch_all(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "index.html").write_bytes(b"<h1>UniFan</h1>")
            Path(directory, "app.css").write_bytes(b":root{}")
            app = Microdot()
            register_routes(app, FakeTelemetry(), FakeEthernet(),
                            SimpleNamespace(version="0.1.0"), directory, "TEST")
            client = TestClient(app)

            response = await client.get("/api/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["system"]["device_id"], "TEST")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

            response = await client.get("/app.css")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.body, b":root{}")

            response = await client.get("/not-allowed")
            self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
