#!/usr/bin/env python3
"""备份后通过 USB 安装非活动 A/B 槽；保留原确认槽，不刷写固件，不发布 GitHub Release。"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
import time

from build_services import build
from https_probe import connect_raw, inventory, save_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware/lib"))
from unifan_ota.manifest import version_tuple


def deploy(args):
    os.umask(0o077)
    provision = args.provision.resolve()
    identity = json.loads((provision / "deployment.json").read_text())
    if identity["device_id"] != args.device_id or identity["device_ip"] != args.device_ip:
        raise ValueError("Provision identity/address mismatch")
    backup = ROOT / "backups" / ("deploy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o700)
    application = build(backup / "application")
    access = json.loads((provision / "device-access.json").read_text())
    mqtt = json.loads((provision / "device-mqtt.json").read_text())
    report = {"version": args.version, "device_id": args.device_id, "device_ip": args.device_ip,
              "provision": str(provision), "activated": False}
    save_json(backup / "deployment.json", report)
    print("DEPLOY_BACKUP", backup, flush=True)
    transport = None
    try:
        transport = connect_raw(args.port)
        actual_id = transport.exec("import machine; print(machine.unique_id().hex().upper())").decode().strip()
        if actual_id != args.device_id:
            raise ValueError("Connected device identity differs")
        # 已启动的硬件 WDT 可能跨软复位保留；USB 维护时每个有界文件操作前喂狗。
        # 这仅存在于部署工具，不放宽正式应用的本地健康监督要求。
        transport.exec("import machine; usb_wdt=machine.WDT(timeout=30000); usb_wdt.feed()")
        before = inventory(transport, "")
        if before["ota"]["pending"] != -1:
            raise RuntimeError("Pending OTA exists; refuse to overwrite")
        if version_tuple(args.version) <= version_tuple(before["ota"]["version"]):
            raise ValueError("Deployment version must advance the confirmed version")
        save_json(backup / "before.json", before)
        # 在任何设备写入前下载并验证每个原文件，包括非活动槽和既有凭据。
        for remote, (size, digest) in before["files"].items():
            transport.exec("usb_wdt.feed()")
            data = transport.fs_readfile(remote, chunk_size=1024)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("Backup verification failed: " + remote)
            local = backup / "original-files" / remote.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        raw = transport.exec("import esp32, binascii\nn=esp32.NVS('unifan_mqtt'); b=bytearray(2048)\ntry:\n s=n.get_blob('config',b); print(binascii.hexlify(b[:s]).decode())\nexcept OSError as e:\n if e.args[0] not in (-4354,4354): raise\n print('ABSENT')")
        (backup / "mqtt-nvs-before.txt").write_bytes(raw)
        print("BACKUP_VERIFIED", len(before["files"]), "files", flush=True)
        active = before["ota"]["active"]
        target = 1 - active
        slot = ("/app_a", "/app_b")[target]
        report["previous_ota"] = before["ota"]
        report["target_slot"] = slot
        save_json(backup / "deployment.json", report)
        security = {"server.pem": (provision / "server.pem").read_bytes(),
                    "server.key": (provision / "server.key").read_bytes(),
                    "mqtt-ca.pem": (provision / "ca.pem").read_bytes(),
                    "access.json": (json.dumps(access) + "\n").encode()}
        # 后续重新部署默认复用既有身份；拒绝静默轮换已有证书或令牌。
        for name, data in security.items():
            remote = "/security/" + name
            if remote in before["files"] and hashlib.sha256(data).hexdigest() != before["files"][remote][1]:
                raise RuntimeError("Existing security identity differs; explicit rotation required")
        transport.exec("from unifan_ota.storage import clear_slot, mkdirs; clear_slot(%r,('/app_a','/app_b')); mkdirs('/security'); mkdirs(%r)" % (slot, slot + "/static"))
        uploads = {slot + "/" + str(p.relative_to(application)): p.read_bytes()
                   for p in application.rglob("*") if p.is_file()}
        uploads.update({"/security/" + name: data for name, data in security.items()})
        for remote, data in uploads.items():
            transport.exec("usb_wdt.feed()")
            transport.fs_writefile(remote, data, chunk_size=1024)
            if transport.fs_hashfile(remote, "sha256", chunk_size=1024) != hashlib.sha256(data).digest():
                raise RuntimeError("Upload verification failed: " + remote)
            print("VERIFIED", remote, len(data), flush=True)
        transport.exec("import sys; sys.path.insert(0,%r)" % slot)
        if not args.keep_mqtt:
            # 凭据只经过受控 USB；字符串不输出至控制台或报告。
            transport.exec("from mqtt_config import MQTTSettings; s=MQTTSettings(); s.patch(%r,s.etag())" % mqtt)
        now = datetime.now(timezone.utc)
        rtc = (now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0)
        transport.exec("import machine; machine.RTC().datetime(%r)" % (rtc,))
        after = inventory(transport, "")
        # 只允许目标槽、安全目录变化；确认槽、固定库、网络配置必须逐个保持原哈希。
        for remote, value in before["files"].items():
            if not remote.startswith(slot + "/") and not remote.startswith("/security/"):
                if after["files"].get(remote) != value:
                    raise RuntimeError("Protected file changed: " + remote)
        report["confirmed_slot_and_runtime_unchanged"] = True
        transport.exec("from unifan_ota.storage import write_marker, NVSState; write_marker(%r,%r); n=NVSState(); s=n.load(); s.update({'pending':%d,'pending_version':%r,'attempts':0}); n.save(s)" % (slot, args.version, target, args.version))
        report["candidate_written"] = True
        transport.exec_raw_no_follow("import machine; machine.reset()")
        transport.serial.close()
        transport = None
        context = ssl.create_default_context(cafile=str(provision / "ca.pem"))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            connection = http.client.HTTPSConnection(args.device_ip, context=context, timeout=15)
            try:
                connection.request("GET", "/api/v1/info")
                response = connection.getresponse()
                data = json.loads(response.read())
                if response.status == 200 and data["data"]["version"] == args.version:
                    report["activated"] = True
                    report["info"] = data
                    print("HTTPS_APPLICATION_READY", args.version, flush=True)
                    break
            except (OSError, ValueError, KeyError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(2)
        if not report["activated"]:
            raise RuntimeError("HTTPS activation not confirmed; inspect serial and A/B rollback")
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if transport is not None:
            try:
                transport.exec_raw_no_follow("import machine; machine.reset()")
            except Exception:
                pass
            transport.serial.close()
        save_json(backup / "deployment.json", report)
        print("DEPLOY_REPORT", backup / "deployment.json", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--device-ip", required=True)
    parser.add_argument("--provision", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--keep-mqtt", action="store_true", help="后续升级保留现有 MQTT NVS 设置")
    args = parser.parse_args()
    version_tuple(args.version)
    deploy(args)
