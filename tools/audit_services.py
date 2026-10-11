#!/usr/bin/env python3
"""最终 USB 审计并重启：核对确认槽、部署哈希及固定运行库，随后校时并验证 MQTT 恢复。"""
import argparse
import hashlib
import json
from pathlib import Path
import time
from https_probe import connect_raw, inventory, save_json
from service_client import ServicesClient


def audit(args):
    folder, deployment = Path(args.provision).resolve(), Path(args.deployment).resolve()
    saved = json.loads((deployment / "deployment.json").read_text())
    original = json.loads((Path(args.original) / "before.json").read_text())
    client = ServicesClient(folder)
    result = {"passed": False}
    transport = None
    try:
        transport = connect_raw(args.port)
        transport.exec("import machine; usb_wdt=machine.WDT(timeout=30000); usb_wdt.feed()")
        actual_id = transport.exec("print(machine.unique_id().hex().upper())").decode().strip()
        if actual_id != saved["device_id"]:
            raise ValueError("Device identity mismatch")
        current = inventory(transport, "")
        result["ota"] = current["ota"]
        slot = ("/app_a", "/app_b")[current["ota"]["active"]]
        if current["ota"]["pending"] != -1 or current["ota"]["version"] != saved["version"] or slot != saved["target_slot"]:
            raise AssertionError("Confirmed boot state mismatch")
        for source in (deployment / "application").rglob("*"):
            if source.is_file():
                remote = slot + "/" + str(source.relative_to(deployment / "application"))
                if current["files"][remote][1] != hashlib.sha256(source.read_bytes()).hexdigest():
                    raise AssertionError("Application hash mismatch: " + remote)
        for remote, value in original["files"].items():
            if not remote.startswith("/app_a/") and not remote.startswith("/app_b/"):
                if current["files"].get(remote) != value:
                    raise AssertionError("Fixed runtime changed: " + remote)
        result["application_hashes_match"] = True
        result["original_fixed_runtime_unchanged"] = True
    finally:
        if transport is not None:
            try:
                transport.exec_raw_no_follow("import machine; machine.reset()")
            finally:
                transport.serial.close()
    deadline = time.monotonic() + 80
    while time.monotonic() < deadline:
        try:
            status = client.request("/api/v1/status")
            if status["status"] == 200 and status["body"]["data"]["mqtt"]["connected"]:
                result["status_after_reboot"] = status["body"]
                break
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise TimeoutError("MQTT recovery after restart not confirmed")
    sync = client.request("/api/v1/system/time", method="PUT", role="admin", data={"unix_seconds": int(time.time())})
    if sync["status"] != 200:
        raise RuntimeError("Time synchronization failed")
    result["time_sync"] = sync["body"]["data"]
    result["passed"] = True
    save_json(folder / "final-audit.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--provision", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--original", required=True)
    audit(parser.parse_args())
