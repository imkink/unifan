#!/usr/bin/env python3
"""实机验收：认证、配置版本、TLS、单连接、Broker 重连及真实 600 秒周期。

会短暂关闭/恢复 MQTT；--restart-broker 仅重启 provision 目录中已验证的本机 Mosquitto。
不会修改风扇输出、网络配置或 OTA 指针。敏感值不进入报告。
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import time
from service_client import ServicesClient, MQTTObserver


def verify(args):
    folder = Path(args.provision).resolve()
    report_path = folder / ("verification-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    client = ServicesClient(folder)
    ip = client.deployment["device_ip"]
    report = {"checks": [], "soak": [], "passed": False}
    observer = None
    changed = False

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    def check(name, condition, detail=None):
        report["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        print("CHECK", name, "PASS" if condition else "FAIL", flush=True)
        save()
        if not condition:
            raise AssertionError(name)

    def request(path, expected=200, **options):
        result = client.request(path, **options)
        check(path + " " + options.get("method", "GET") + " " + str(expected), result["status"] == expected,
              {"status": result["status"], "elapsed_ms": result["elapsed_ms"]})
        time.sleep(.4)
        return result

    def patch_mqtt(changes):
        current = client.request("/api/v1/settings/mqtt", role="admin")
        time.sleep(1.1)
        return request("/api/v1/settings/mqtt", method="PATCH", role="admin", data=changes,
                       headers={"If-Match": current["etag"]})

    def wait_phase(phase, timeout=80):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = client.request("/api/v1/status")
            if result["status"] == 200 and result["body"]["data"]["mqtt"]["phase"] == phase:
                return result["body"]
            time.sleep(2)
        raise TimeoutError("MQTT did not reach " + phase)

    try:
        info = request("/api/v1/info")["body"]
        report["info"] = info
        initial_boot = info["meta"]["boot_id"]
        request("/api/v1/system/time", method="PUT", role="admin", data={"unix_seconds": int(time.time())})
        check("mqtt_initially_online", wait_phase("online")["data"]["mqtt"]["connected"])
        page = request("/")
        check("dashboard_single_document", "<style>" in page["body"] and '<script src=' not in page["body"] and "mqtt-dialog" in page["body"])
        request("/api/v1/health")
        request("/api/v1/diagnostics", role="admin")
        request("/api/v1/settings/mqtt", expected=401)
        request("/api/v1/settings/mqtt", expected=401, method="PATCH", data={})
        request("/api/v1/settings/mqtt", expected=403, method="PATCH", role="read", data={})
        request("/api/v1/settings/mqtt", expected=403, method="PATCH", role="admin", data={}, headers={"Origin": "https://evil.invalid"})
        settings = request("/api/v1/settings/mqtt", role="read")
        check("password_not_returned", "password" not in settings["body"]["data"])
        request("/api/v1/settings/mqtt", expected=428, method="PATCH", role="admin", data={})
        time.sleep(1.1)
        request("/api/v1/settings/mqtt", expected=412, method="PATCH", role="admin", data={}, headers={"If-Match": '"mqtt-999999"'})
        time.sleep(1.1)
        request("/api/v1/settings/mqtt", expected=422, method="PATCH", role="admin", data={"tls": False}, headers={"If-Match": settings["etag"]})
        time.sleep(1.1)
        same = request("/api/v1/settings/mqtt", method="PATCH", role="admin", data={}, headers={"If-Match": settings["etag"]})
        check("noop_keeps_revision", same["etag"] == settings["etag"])

        for name, context, hostname in (("untrusted_ca", ssl.create_default_context(), ip),
                                        ("wrong_identity", client.context, "wrong.invalid")):
            rejected = False
            with socket.create_connection((ip, 443), timeout=5) as raw:
                try:
                    with context.wrap_socket(raw, server_hostname=hostname):
                        pass
                except ssl.SSLCertVerificationError:
                    rejected = True
            check(name, rejected)
            time.sleep(.5)
        with socket.create_connection((ip, 443), timeout=5) as raw:
            raw.sendall(b"GET / HTTP/1.0\r\n\r\n")
            try:
                reply = raw.recv(64)
            except (ConnectionResetError, TimeoutError):
                reply = b""
            check("no_plaintext_downgrade", not reply.startswith(b"HTTP/"))
        time.sleep(.5)
        refused = False
        try:
            with socket.create_connection((ip, 80), timeout=2):
                pass
        except OSError:
            refused = True
        check("http_port_80_closed", refused)

        with socket.create_connection((ip, 443), timeout=5) as held:
            time.sleep(.7)
            rejected = False
            try:
                client.request("/api/v1/health")
            except (OSError, ssl.SSLError):
                rejected = True
            check("second_connection_rejected_before_tls", rejected)
        time.sleep(1)
        request("/api/v1/health")

        bad_auth_rejected = False
        try:
            bad = MQTTObserver(folder, password_override="deliberately-invalid-test-password")
            bad.close()
        except PermissionError:
            bad_auth_rejected = True
        check("broker_bad_password_rejected", bad_auth_rejected)
        observer = MQTTObserver(folder).subscribe()
        time.sleep(2)
        check("three_retained_topics", {m["topic"].rsplit("/", 1)[1] for m in observer.messages} >= {"info", "state", "availability"})
        changed = True
        patch_mqtt({"enabled": False})
        wait_phase("disabled")
        time.sleep(1)
        check("graceful_offline", any(m["payload"] == "offline" for m in observer.messages))
        request("/api/v1/health")
        patch_mqtt({"enabled": True})
        wait_phase("online")
        changed = False

        if args.restart_broker:
            # 限定到本工具生成且命令行确实引用该配置的进程，禁止对未知 PID 发信号。
            pid = int((folder / "broker.pid").read_text())
            command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
            if str(folder / "mosquitto.conf") not in command or "mosquitto" not in command:
                raise RuntimeError("Broker PID identity mismatch")
            observer.close()
            observer = None
            os.kill(pid, signal.SIGTERM)
            try:
                time.sleep(12)
                check("broker_down_local_health_ok", client.request("/api/v1/health")["status"] == 200)
            finally:
                subprocess.run(["/opt/homebrew/sbin/mosquitto", "-c", str(folder / "mosquitto.conf"), "-d"], check=True)
            wait_phase("online")
            check("broker_restart_recovered", True)

        observer = observer or MQTTObserver(folder).subscribe()
        time.sleep(2)
        baseline = client.request("/api/v1/status")["body"]
        report["soak_baseline"] = baseline
        started = time.monotonic()
        print("SOAK_START", args.soak_seconds, flush=True)
        while time.monotonic() - started < args.soak_seconds:
            status = client.request("/api/v1/status")
            diagnostics = client.request("/api/v1/diagnostics", role="admin")
            row = {"elapsed_seconds": round(time.monotonic() - started, 1), "status": status, "diagnostics": diagnostics}
            report["soak"].append(row)
            if status["status"] != 200 or diagnostics["status"] != 200:
                raise AssertionError("soak_http_failed")
            meta = status["body"]["meta"]
            if meta["boot_id"] != initial_boot or status["body"]["data"]["stale"]:
                raise AssertionError("device_restarted_or_sample_stale")
            if (not status["body"]["data"]["mqtt"]["connected"] or
                    status["body"]["data"]["mqtt"]["connections"] != baseline["data"]["mqtt"]["connections"]):
                raise AssertionError("mqtt_disconnected_during_soak")
            print("SOAK_PROGRESS", json.dumps({"seconds": row["elapsed_seconds"], "sample_seq": meta["sample_seq"],
                   "publish_seq": status["body"]["data"]["mqtt"]["publish_seq"],
                   "heap_free": diagnostics["body"]["data"]["python_heap_free"], "mqtt": status["body"]["data"]["mqtt"]["phase"]}), flush=True)
            save()
            time.sleep(min(30, max(0, args.soak_seconds - (time.monotonic() - started))))
        report["mqtt_messages"] = observer.messages
        report["observer_errors"] = observer.errors
        live_states = [m["payload"] for m in observer.messages if not m["retained"] and m["topic"].endswith("/state")]
        if args.soak_seconds >= 610:
            latest = live_states[-1] if live_states else {}
            check("actual_600_second_publish", latest.get("publish_seq", 0) > baseline["data"]["mqtt"]["publish_seq"],
                  {"latest_publish_seq": latest.get("publish_seq"), "sample_seq": latest.get("sample_seq")})
            check("minute_sampling_progress", latest.get("sample_seq", 0) - baseline["meta"]["sample_seq"] >= 9)
        check("no_observer_disconnect", not observer.errors)
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if changed:
            try:
                patch_mqtt({"enabled": True})
                wait_phase("online")
                report["mqtt_restored"] = True
            except Exception as exc:
                report["restore_error"] = type(exc).__name__
        if observer:
            observer.close()
        save()
        print("VERIFICATION_REPORT", report_path, flush=True)


def analyze_report(path):
    """从原始报告独立复核周期，防止把重连的即时发布误认为十分钟周期通过。"""
    path = Path(path)
    value = json.loads(path.read_text())
    if not value["passed"]:
        raise ValueError("Report did not finish successfully")
    baseline = value["soak_baseline"]
    states = [item["payload"] for item in value["mqtt_messages"]
              if item["topic"].endswith("/state") and isinstance(item["payload"], dict)]
    anchor = next(item for item in states if item["publish_seq"] == baseline["data"]["mqtt"]["publish_seq"])
    following = next(item for item in states if item["publish_seq"] == anchor["publish_seq"] + 1)
    interval = following["uptime_ms"] - anchor["uptime_ms"]
    rows = value["soak"]
    statuses = [row["status"]["body"] for row in rows]
    diagnostics = [row["diagnostics"]["body"]["data"] for row in rows]
    if not (600000 <= interval < 630000 and following["boot_id"] == anchor["boot_id"]):
        raise AssertionError("Not a genuine 600-second same-boot publication")
    if any(not s["data"]["mqtt"]["connected"] or s["data"]["mqtt"]["connections"] != baseline["data"]["mqtt"]["connections"] for s in statuses):
        raise AssertionError("MQTT reconnected during observation")
    latencies = sorted(row[which]["elapsed_ms"] for row in rows for which in ("status", "diagnostics"))
    result = {"passed": True, "source": path.name, "period_ms": interval,
              "http_queries": len(latencies), "response_p95_ms": latencies[(len(latencies) * 95 + 99) // 100 - 1],
              "samples_start": baseline["meta"]["sample_seq"], "samples_end": statuses[-1]["meta"]["sample_seq"],
              "publish_seq_start": anchor["publish_seq"], "publish_seq_end": following["publish_seq"],
              "python_heap_free_min": min(item["python_heap_free"] for item in diagnostics),
              "python_heap_free_max": max(item["python_heap_free"] for item in diagnostics),
              "https_sampled_heap_min": min(item["https"]["heap_min"] for item in diagnostics),
              "native_free_min": min(item["native_free"] for item in diagnostics),
              "native_largest_min": min(item["native_largest"] for item in diagnostics),
              "https_errors_during_soak": diagnostics[-1]["https"]["errors"] - diagnostics[0]["https"]["errors"]}
    (path.parent / "period-analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", required=True)
    parser.add_argument("--soak-seconds", type=int, default=620)
    parser.add_argument("--restart-broker", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.soak_seconds <= 1800:
        parser.error("soak-seconds must be in 0..1800")
    verify(args)
