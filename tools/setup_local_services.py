#!/usr/bin/env python3
"""生成本机专用 TLS Broker 配置与设备凭据；不修改系统信任库或 Homebrew 默认配置。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def setup(device_ip, device_id):
    os.umask(0o077)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((device_ip, 443))
        host_ip = route.getsockname()[0]
    folder = ROOT / "backups" / ("services-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(mode=0o700)
    def openssl(*args):
        subprocess.run(["openssl", *args], cwd=folder, check=True, capture_output=True)
    openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-keyout", "ca.key", "-out", "ca.pem", "-days", "3650", "-sha256",
            "-subj", "/CN=UNIFAN local managed CA " + device_id,
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    for name, address in (("server", device_ip), ("broker", host_ip)):
        openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                "-keyout", name + ".key", "-out", name + ".csr", "-subj", "/CN=UNIFAN " + name,
                "-addext", "subjectAltName=IP:" + address,
                "-addext", "basicConstraints=critical,CA:FALSE",
                "-addext", "keyUsage=critical,digitalSignature",
                "-addext", "extendedKeyUsage=serverAuth")
        openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                "-CAcreateserial", "-out", name + ".pem", "-days", "365", "-sha256", "-copy_extensions", "copy")
    publisher = "unifan-" + device_id.lower()
    observer = "unifan-observer"
    password, observer_password = secrets.token_hex(24), secrets.token_hex(24)
    # 用权限 0600 的输入文件交给 passwd 转换，密码不放在进程参数或控制台中。
    passwords = folder / "passwords"
    passwords.write_text(publisher + ":" + password + "\n" + observer + ":" + observer_password + "\n")
    subprocess.run(["/opt/homebrew/bin/mosquitto_passwd", "-U", str(passwords)], check=True, capture_output=True)
    base = "unifan/sim/" + device_id
    (folder / "acl").write_text("user " + publisher + "\ntopic write " + base + "/#\n\nuser " + observer + "\ntopic read " + base + "/#\n")
    (folder / "mosquitto.conf").write_text(
        "# UNIFAN 独立测试 Broker；只绑定指定局域网地址，禁止匿名与明文连接。\n" +
        "listener 8883 " + host_ip + "\nallow_anonymous false\n" +
        "password_file " + str(passwords) + "\nacl_file " + str(folder / "acl") + "\n" +
        "certfile " + str(folder / "broker.pem") + "\nkeyfile " + str(folder / "broker.key") + "\n" +
        "tls_version tlsv1.2\nmax_connections 8\nmax_packet_size 8192\n" +
        "persistence true\npersistence_location " + str(folder) + "/\n" +
        "autosave_interval 60\npid_file " + str(folder / "broker.pid") + "\n" +
        "log_dest file " + str(folder / "broker.log") + "\nlog_type all\n")
    access = {"admin": secrets.token_hex(32), "read": secrets.token_hex(32),
              "origins": ["https://" + device_ip]}
    mqtt = {"enabled": True, "host": host_ip, "port": 8883, "tls": True,
            "server_name": host_ip, "username": publisher, "password": password}
    for filename, data in (("device-access.json", access), ("device-mqtt.json", mqtt),
                           ("observer.json", {"username": observer, "password": observer_password}),
                           ("deployment.json", {"device_ip": device_ip, "device_id": device_id,
                                                "broker_ip": host_ip, "base_topic": base})):
        (folder / filename).write_text(json.dumps(data, indent=2) + "\n")
    for role in ("admin", "read"):
        (folder / (role + ".curl")).write_text('cacert = "' + str(folder / "ca.pem") + '"\nheader = "Authorization: Bearer ' + access[role] + '"\n')
    subprocess.run(["/opt/homebrew/sbin/mosquitto", "-c", str(folder / "mosquitto.conf"), "--test-config"], check=True)
    print("SERVICE_DIRECTORY", folder)
    print("BROKER", host_ip + ":8883", "DEVICE", device_ip, "TOPIC", base)
    return folder


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-ip", required=True)
    parser.add_argument("--device-id", required=True)
    args = parser.parse_args()
    setup(args.device_ip, args.device_id)
