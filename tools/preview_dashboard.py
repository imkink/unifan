#!/usr/bin/env python3
"""Serve the static dashboard with deterministic simulated API data."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "firmware" / "app_a" / "static"

STATUS = {
    "schema": 1,
    "environment": {"temperature_c": 46.0, "humidity_percent": 55.0},
    "fans": [
        {"id": 1, "group": 1, "rpm": 5230, "pwm_percent": 80},
        {"id": 2, "group": 1, "rpm": 5230, "pwm_percent": 80},
        {"id": 3, "group": 1, "rpm": 5230, "pwm_percent": 80},
        {"id": 4, "group": 1, "rpm": 3000, "pwm_percent": 55},
        {"id": 5, "group": 2, "rpm": 2980, "pwm_percent": 55},
        {"id": 6, "group": 2, "rpm": 0, "pwm_percent": 55},
    ],
    "network": {
        "interface": "w5500", "phase": "online", "ready": True,
        "ip": "192.168.1.223", "netmask": "255.255.255.0",
        "gateway": "192.168.1.1", "dns": "192.168.1.1", "error": None,
    },
    "system": {
        "device_id": "FC06NV", "version": "1.0.0", "source": "simulated",
        "simulated": True, "hardware_ready": False,
        "control_policy_ready": False, "stale": False, "sequence": 42,
    },
}


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        if self.path in ("/_preview/mobile", "/_preview/narrow"):
            width = 402 if self.path.endswith("mobile") else 300
            body = ("<!doctype html><meta charset=utf-8><title>UniFan %dpx Preview</title>"
                    "<style>html,body{margin:0;min-height:100%%;background:#e8e8e8}"
                    "iframe{display:block;width:%dpx;height:874px;margin:24px auto;border:0;"
                    "box-shadow:0 8px 32px #0003}</style>"
                    "<iframe src=/ aria-label='UniFan responsive preview'></iframe>" %
                    (width, width)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            body = json.dumps(STATUS, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print("Dashboard preview: http://127.0.0.1:%d/" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
