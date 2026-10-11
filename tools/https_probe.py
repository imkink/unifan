#!/usr/bin/env python3
"""Run a reversible HTTPS experiment on one explicitly selected StampS3.

Requires mpremote, mpy-cross and openssl on the host. Does not flash firmware,
alter application slots/NVS, trust a CA globally, or install a firmware update.
The unique temporary device directory is removed and the original app reset.
Private test keys/logs/results remain in ignored backups/https-probe-*.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import socket
import ssl
import statistics
import subprocess
import threading
import time
import traceback

import mpy_cross
from mpremote.transport_serial import SerialTransport

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("/", "/app.css", "/app.js", "/Rajdhani-Bold.woff2",
          "/Rajdhani-SemiBold.woff2", "/noun_Fan_8413191.svg")


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def connect_raw(port):
    """Bounded reconnects: application shutdown/reset can invalidate native USB."""
    for attempt in range(4):
        transport = None
        try:
            transport = SerialTransport(port, timeout=1)
            # Give asyncio cancellation/traceback output time before requesting REPL.
            transport.serial.write(b"\r\x03\x03")
            time.sleep(.5)
            transport.enter_raw_repl(timeout_overall=5)
            return transport
        except Exception:
            if transport is not None:
                transport.serial.close()
            if attempt == 3:
                raise
            print("SERIAL_RECONNECT", attempt + 1, flush=True)
            time.sleep(2)


def generate_certificates(folder, device_ip, host_ip):
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=folder, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
            "-subj", "/CN=UNIFAN temporary HTTPS test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name, address in (("server", device_ip), ("host", host_ip)):
        openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                "-nodes", "-keyout", name + ".key", "-out", name + ".csr",
                "-subj", "/CN=UNIFAN HTTPS probe " + name,
                "-addext", "subjectAltName=IP:" + address,
                "-addext", "basicConstraints=critical,CA:FALSE",
                "-addext", "keyUsage=critical,digitalSignature",
                "-addext", "extendedKeyUsage=serverAuth")
        openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.pem",
                "-CAkey", "ca.key", "-CAcreateserial", "-out", name + ".pem",
                "-days", "2", "-sha256", "-copy_extensions", "copy")


def inventory(transport, exclude):
    code = """
import os, json, hashlib, binascii
from unifan_ota.storage import NVSState
items = {}
def walk(path):
    for name in os.listdir(path):
        full = path.rstrip('/') + '/' + name
        if full == %r:
            continue
        st = os.stat(full)
        if st[0] & 0x4000:
            walk(full)
        else:
            digest = hashlib.sha256()
            with open(full, 'rb') as f:
                while True:
                    data = f.read(1024)
                    if not data:
                        break
                    digest.update(data)
            items[full] = [st[6], binascii.hexlify(digest.digest()).decode()]
walk('/')
print(json.dumps({'files': items, 'ota': NVSState().load()}))
""" % exclude
    stdout, stderr = transport.exec_raw(code, timeout=30)
    if stderr:
        raise RuntimeError(stderr.decode(errors="replace"))
    return json.loads(stdout.decode().strip())


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True


def fixture_server(host_ip, folder, size):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/fixture":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Connection", "close")
            self.end_headers()
            block = b"UNIFAN-HTTPS-TEST" * 128
            try:
                for offset in range(0, size, len(block)):
                    self.wfile.write(block[:min(len(block), size - offset)])
                    self.wfile.flush()
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass

        def log_message(self, *args):
            pass

    server = FixtureServer((host_ip, 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / "host.pem", folder / "host.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Client:
    def __init__(self, address, ca, token):
        self.address = address
        self.token = token
        self.context = ssl.create_default_context(cafile=str(ca))
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def request(self, path="/api/status", tls=True, method="GET", authorize=True):
        started = time.perf_counter()
        port = 8443 if tls else 8080
        stream = socket.create_connection((self.address, port), timeout=12)
        connected = time.perf_counter()
        try:
            if tls:
                stream = self.context.wrap_socket(stream, server_hostname=self.address)
            handshaken = time.perf_counter()
            protocol = stream.version() if tls else None
            cipher = stream.cipher()[0] if tls else None
            reused = stream.session_reused if tls else None
            auth = "Authorization: Bearer " + self.token + "\r\n" if authorize else ""
            request = (method + " " + path + " HTTP/1.1\r\nHost: " + self.address +
                       "\r\n" + auth + "Connection: close\r\nContent-Length: 0\r\n\r\n")
            stream.sendall(request.encode())
            response = bytearray()
            while True:
                data = stream.recv(4096)
                if not data:
                    break
                response.extend(data)
                if len(response) > 65536:
                    raise ValueError("Response exceeds test limit")
            header, separator, body = bytes(response).partition(b"\r\n\r\n")
            if not separator:
                raise ValueError("No HTTP response headers")
            status = int(header.split(b" ", 2)[1])
            return {"status": status, "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "connect_ms": (connected - started) * 1000,
                    "handshake_ms": (handshaken - connected) * 1000,
                    "bytes": len(body), "body": body, "tls_version": protocol,
                    "cipher": cipher, "session_reused": reused}
        finally:
            stream.close()

    def json(self, path, method="GET"):
        value = self.request(path, method=method)
        if value["status"] not in (200, 202):
            raise RuntimeError("Probe API status " + str(value["status"]))
        return json.loads(value["body"])


def summarize(rows):
    okay = [r for r in rows if r.get("status") == 200]
    times = sorted(r["elapsed_ms"] for r in okay)
    handshakes = sorted(r["handshake_ms"] for r in okay)
    def p95(values):
        return round(values[max(0, math.ceil(len(values) * .95) - 1)], 2) if values else None
    return {"requests": len(rows), "successful": len(okay), "failures": len(rows) - len(okay),
            "response_p50_ms": round(statistics.median(times), 2) if times else None,
            "response_p95_ms": p95(times), "response_max_ms": round(max(times), 2) if times else None,
            "handshake_p95_ms": p95(handshakes),
            "errors": [r for r in rows if r.get("status") != 200][:8]}


def benchmark(client, tls, concurrency, count, paths):
    barrier = threading.Barrier(concurrency)
    def worker(worker_id):
        rows = []
        barrier.wait(timeout=15)
        for index in range(count):
            try:
                row = client.request(paths[(worker_id + index) % len(paths)], tls=tls)
                row.pop("body")
            except Exception as exc:
                row = {"error": type(exc).__name__ + ": " + str(exc)}
            rows.append(row)
        return rows
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return [row for group in pool.map(worker, range(concurrency)) for row in group]


def check_cert_rejection(client, context, hostname):
    with socket.create_connection((client.address, 8443), timeout=12) as raw:
        try:
            with context.wrap_socket(raw, server_hostname=hostname):
                return {"rejected": False}
        except ssl.SSLCertVerificationError as exc:
            return {"rejected": True, "reason": exc.verify_message}


def exercise(client, report, run_dir, soak_seconds, profile):
    first = client.request()
    if first["status"] != 200:
        raise RuntimeError("HTTPS smoke request failed")
    report["tls"] = {k: first[k] for k in ("tls_version", "cipher", "session_reused")}
    report["certificate_checks"] = {
        "unknown_ca": check_cert_rejection(client, ssl.create_default_context(), client.address),
        "wrong_identity": check_cert_rejection(client, client.context, "wrong.invalid"),
        "unauthorized_status": client.request(authorize=False)["status"]}
    time.sleep(1)
    report["phases"] = []
    phases = [("http_1", False, 1, 20, ["/api/status"]),
              ("http_2", False, 2, 15, ["/api/status"]),
              ("http_4", False, 4, 10, ["/api/status"]),
              ("https_1", True, 1, 20, ["/api/status"]),
              ("https_2", True, 2, 15, ["/api/status"]),
              ("https_4", True, 4, 10, ["/api/status"]),
              ("https_page_assets_2", True, 2, 12, ASSETS)]
    if profile == "followup":
        phases = [("http_1", False, 1, 5, ["/api/status"]),
                  ("https_1", True, 1, 5, ["/api/status"]),
                  ("https_2", True, 2, 5, ["/api/status"])]
    for name, tls, concurrency, count, paths in phases:
        print("PHASE_START", name, flush=True)
        client.json("/__probe/reset", "POST")
        rows = benchmark(client, tls, concurrency, count, paths)
        time.sleep(1)
        result = {"name": name, "concurrency": concurrency, **summarize(rows),
                  "device": client.json("/__probe/metrics?gc=1")}
        report["phases"].append(result)
        save_json(run_dir / (name + ".json"), rows)
        save_json(run_dir / "results.json", report)
        print("PHASE_RESULT", json.dumps(result), flush=True)

    print("PHASE_START slow_and_aborted_connections", flush=True)
    client.json("/__probe/reset", "POST")
    raw = socket.create_connection((client.address, 8443), timeout=12)
    try:
        # TCP connection with no ClientHello must not block a second valid client.
        during_slow = summarize(benchmark(client, True, 1, 4, ["/api/status"]))
    finally:
        raw.close()
    for _ in range(4):
        with socket.create_connection((client.address, 8443), timeout=12) as aborted:
            aborted.sendall(b"GET /api/status HTTP/1.1\r\n\r\n")
        time.sleep(.1)
    with socket.create_connection((client.address, 8443), timeout=12) as plain:
        plain.sendall(b"GET / HTTP/1.0\r\n\r\n")
        try:
            plaintext_reply = plain.recv(64)
        except (ConnectionResetError, TimeoutError):
            plaintext_reply = b""
    report["certificate_checks"]["no_plaintext_fallback"] = not plaintext_reply.startswith(b"HTTP/")
    time.sleep(9)
    report["slow_connections"] = {"valid_requests_during_slow": during_slow,
                                   "device": client.json("/__probe/metrics?gc=1")}
    print("PHASE_RESULT", json.dumps(report["slow_connections"]), flush=True)

    print("PHASE_START ota_tls_fixture", flush=True)
    client.json("/__probe/reset", "POST")
    client.json("/__probe/download", "POST")
    ota_rows = []
    deadline = time.monotonic() + 200
    while time.monotonic() < deadline:
        time.sleep(.5)
        value = client.request("/__probe/metrics")
        data = json.loads(value.pop("body"))
        ota_rows.append(value)
        if data["ota"]["phase"] in ("done", "error") and not data["maintenance"]:
            break
    else:
        raise TimeoutError("Device fixture download did not finish")
    report["ota_fixture"] = {**summarize(ota_rows), "device": client.json("/__probe/metrics?gc=1")}
    print("PHASE_RESULT", json.dumps(report["ota_fixture"]), flush=True)
    save_json(run_dir / "results.json", report)

    print("PHASE_START soak", soak_seconds, flush=True)
    client.json("/__probe/reset", "POST")
    soak_rows = []
    samples = []
    deadline = time.monotonic() + soak_seconds
    next_report = time.monotonic()
    while time.monotonic() < deadline:
        soak_rows.extend(benchmark(client, True, 2, 1, ["/api/status"]))
        if time.monotonic() >= next_report:
            samples.append(client.json("/__probe/metrics?gc=1"))
            print("SOAK_PROGRESS", json.dumps(samples[-1]), flush=True)
            next_report = time.monotonic() + 30
        time.sleep(2)
    report["soak"] = {"duration_seconds": soak_seconds, **summarize(soak_rows),
                      "samples": samples, "device": client.json("/__probe/metrics?gc=1")}
    save_json(run_dir / "soak-requests.json", soak_rows)
    save_json(run_dir / "results.json", report)
    print("PHASE_RESULT", json.dumps(report["soak"]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--device-ip", required=True)
    parser.add_argument("--soak-seconds", type=int, default=180)
    parser.add_argument("--profile", choices=("full", "followup"), default="full")
    parser.add_argument("--max-connections", type=int, choices=(1, 2, 3, 4), default=2)
    args = parser.parse_args()
    if not 0 <= args.soak_seconds <= 1800:
        parser.error("soak-seconds must be in 0..1800")
    os.umask(0o077)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    run_dir = ROOT / "backups" / ("https-probe-" + run_id)
    run_dir.mkdir(parents=True, mode=0o700)
    remote_root = "/https_probe_" + secrets.token_hex(6)
    token = secrets.token_hex(32)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((args.device_ip, 8443))
        host_ip = route.getsockname()[0]
    generate_certificates(run_dir, args.device_ip, host_ip)
    fixture_bytes = 512 * 1024
    fixture = fixture_server(host_ip, run_dir, fixture_bytes)
    cfg = {"ROOT": remote_root, "TOKEN": token, "HTTP_PORT": 8080, "HTTPS_PORT": 8443,
           "HOST_IP": host_ip, "HOST_PORT": fixture.server_address[1],
           "FIXTURE_BYTES": fixture_bytes, "MAX_CONNECTIONS": args.max_connections,
           "MAX_RUNTIME_SECONDS": max(1800, args.soak_seconds + 1200)}
    # Generated per-run configuration; never checked into the repository.
    config_source = run_dir / "probe_config.py"
    config_source.write_text("\n".join(k + " = " + repr(v) for k, v in cfg.items()) + "\n")
    sources = {"probe_config": config_source, "https_probe_device": ROOT / "tools/https_probe_device.py"}
    for name in ("app_config", "hardware", "telemetry", "ethernet", "webapp"):
        sources[name] = ROOT / "firmware/app_a" / (name + ".py")
    files = {}
    for name, source in sources.items():
        compiled = run_dir / (name + ".mpy")
        if mpy_cross.run("-o", str(compiled), str(source)).wait():
            raise RuntimeError("Cross compilation failed: " + name)
        files[name + ".mpy"] = compiled.read_bytes()
    for name in ("ca.pem", "server.pem", "server.key"):
        files[name] = (run_dir / name).read_bytes()
    for path in (ROOT / "firmware/app_a/static").iterdir():
        if path.is_file():
            files["static/" + path.name] = path.read_bytes()

    report = {"run_id": run_id, "device_ip": args.device_ip, "host_ip": host_ip,
              "profile": args.profile,
              "remote_root": remote_root, "test_only": True,
              "certificate": "temporary private CA, ECDSA P-256, 2-day validity",
              "settings": {k: v for k, v in cfg.items() if k != "TOKEN"},
              "restored": False}
    print("RUN_DIRECTORY", run_dir, flush=True)
    transport = None
    before = None
    uploaded = []
    reader_stop = threading.Event()
    ready = threading.Event()
    reader_thread = None
    client = Client(args.device_ip, run_dir / "ca.pem", token)
    try:
        transport = connect_raw(args.port)
        before = inventory(transport, remote_root)
        save_json(run_dir / "before.json", before)
        if before["ota"] is None or before["ota"]["pending"] != -1:
            raise RuntimeError("Refuse to test while OTA candidate is pending")
        now = datetime.now(timezone.utc)
        rtc = (now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0)
        transport.exec("import machine; machine.RTC().datetime(%r)" % (rtc,))
        transport.fs_mkdir(remote_root)
        transport.fs_mkdir(remote_root + "/static")
        for name, content in files.items():
            remote = remote_root + "/" + name
            uploaded.append(remote)
            transport.fs_writefile(remote, content)
            if transport.fs_hashfile(remote, "sha256", chunk_size=1024) != hashlib.sha256(content).digest():
                raise RuntimeError("Device upload verification failed: " + name)
        print("UPLOAD_VERIFIED", len(uploaded), "files; original application untouched", flush=True)
        transport.exec_raw_no_follow("import sys; sys.path.insert(0, %r); import https_probe_device; https_probe_device.run()" % remote_root)

        def read_serial():
            with (run_dir / "serial.log").open("w") as log:
                while not reader_stop.is_set():
                    try:
                        line = transport.serial.readline().decode(errors="replace").strip()
                    except Exception as exc:
                        log.write(repr(exc) + "\n")
                        break
                    if not line:
                        continue
                    log.write(line + "\n")
                    log.flush()
                    print("DEVICE", line, flush=True)
                    if line.startswith("PROBE_READY "):
                        report["startup"] = json.loads(line.split(" ", 1)[1])
                        ready.set()
        transport.serial.timeout = .5
        reader_thread = threading.Thread(target=read_serial, daemon=True)
        reader_thread.start()
        if not ready.wait(45):
            raise TimeoutError("Probe did not become ready; inspect serial.log")
        if report["startup"]["ip"] != args.device_ip:
            raise RuntimeError("DHCP address differs from explicitly selected device IP")
        exercise(client, report, run_dir, args.soak_seconds, args.profile)
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        print("PROBE_ERROR", report["error"], flush=True)
        traceback.print_exc()
    finally:
        if ready.is_set():
            try:
                client.json("/__probe/stop", "POST")
                time.sleep(1)
            except Exception:
                pass
        reader_stop.set()
        if reader_thread:
            reader_thread.join(timeout=3)
        if transport is not None:
            try:
                transport.serial.close()
                transport = connect_raw(args.port)
                # Only remove this run's exact temporary paths, never a slot or broad root.
                for name in [remote_root + "/download.bin"] + list(reversed(uploaded)):
                    transport.exec("import os\ntry:\n os.remove(%r)\nexcept OSError as e:\n if e.args[0] != 2: raise" % name)
                transport.exec("import os\nfor p in %r:\n try: os.rmdir(p)\n except OSError as e:\n  if e.args[0] != 2: raise" % ([remote_root + "/static", remote_root],))
                after = inventory(transport, remote_root)
                save_json(run_dir / "after.json", after)
                report["original_files_and_ota_unchanged"] = before == after
                if before != after:
                    raise RuntimeError("Original filesystem or OTA state changed")
                transport.exec_raw_no_follow("import machine; machine.reset()")
                # Native USB re-enumerates on reset: the old descriptor can return ENXIO.
                # Confirm recovery through the original app, not that stale descriptor.
                time.sleep(2)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    connection = http.client.HTTPConnection(args.device_ip, 80, timeout=4)
                    try:
                        connection.request("GET", "/api/status")
                        response = connection.getresponse()
                        payload = json.loads(response.read(16384))
                        if (response.status == 200 and
                                payload["system"]["version"] == before["ota"]["version"] and
                                not payload["system"]["stale"]):
                            report["restored"] = True
                            report["restored_status"] = payload
                            print("RESTORED_APPLICATION", payload["system"]["version"], flush=True)
                            break
                    except (OSError, ValueError, KeyError, http.client.HTTPException):
                        pass
                    finally:
                        connection.close()
                    time.sleep(.5)
                if not report["restored"]:
                    raise RuntimeError("Original application recovery not confirmed")
            except Exception as exc:
                report["restoration_error"] = repr(exc)
                print("RESTORATION_ERROR", repr(exc), flush=True)
                # Always attempt to restart normal firmware, even if cleanup failed.
                try:
                    transport.serial.close()
                    transport = connect_raw(args.port)
                    transport.exec_raw_no_follow("import machine; machine.reset()")
                except Exception:
                    pass
            finally:
                try:
                    transport.close()
                except OSError:
                    # USB descriptor already invalidated by reset; still save test results.
                    pass
        fixture.shutdown()
        fixture.server_close()
        save_json(run_dir / "results.json", report)
        print("RESULTS", run_dir / "results.json", flush=True)
    if "error" in report or not report["restored"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
