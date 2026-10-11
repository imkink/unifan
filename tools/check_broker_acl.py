#!/usr/bin/env python3
"""以 MQTT 5 的明确 PUBACK 原因码验证 ACL；设备自身仍使用 MQTT 3.1.1。"""
import argparse
import json
from pathlib import Path
import secrets
import socket
import ssl
from service_client import field, packet


def check(folder):
    folder = Path(folder)
    deployment = json.loads((folder / "deployment.json").read_text())
    context = ssl.create_default_context(cafile=str(folder / "ca.pem"))
    checks = []
    for account, topic in (("device-mqtt.json", "unifan/sim/NOT_THIS_DEVICE/state"),
                           ("observer.json", deployment["base_topic"] + "/state")):
        credentials = json.loads((folder / account).read_text())
        with socket.create_connection((deployment["broker_ip"], 8883), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=deployment["broker_ip"]) as stream:
                def exact(size):
                    result = bytearray()
                    while len(result) < size:
                        data = stream.recv(size - len(result))
                        if not data:
                            raise RuntimeError("Broker closed before acknowledgement")
                        result.extend(data)
                    return bytes(result)
                def read_packet():
                    kind, length, shift = exact(1)[0], 0, 0
                    while True:
                        digit = exact(1)[0]
                        length |= (digit & 127) << shift
                        if length > 8192 or shift > 21:
                            raise ValueError("Broker response size")
                        if not digit & 128:
                            return kind, exact(length)
                        shift += 7
                stream.sendall(packet(0x10, b"\x00\x04MQTT\x05\xc2\x00\x1e\x00" +
                                       field("acl-check-" + secrets.token_hex(4)) +
                                       field(credentials["username"]) + field(credentials["password"])))
                kind, body = read_packet()
                if kind != 0x20 or body[1] != 0:
                    raise RuntimeError("Account authentication failed")
                # 不设置 retain。正确 ACL 应拒绝这两种写入，不污染设备的保留状态。
                stream.sendall(packet(0x32, field(topic) + b"\x00\x01\x00acl-negative-check"))
                kind, body = read_packet()
                denied = kind == 0x40 and body[:2] == b"\x00\x01" and len(body) >= 3 and body[2] == 0x87
                checks.append({"account": credentials["username"], "denied": denied,
                               "reason_code": body[2] if len(body) >= 3 else None})
                stream.sendall(b"\xe0\x00")
    (folder / "acl-verification.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))
    if not all(row["denied"] for row in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", required=True)
    args = parser.parse_args()
    check(args.provision)
