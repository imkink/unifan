"""仅供电脑端验证：使用私有 CA、从受限文件读取凭据，不把密码放在命令行。"""
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl
import threading
import time


def field(text):
    data = text.encode()
    return len(data).to_bytes(2, "big") + data


def packet(kind, data):
    size, length = len(data), bytearray()
    while True:
        digit = size % 128
        size //= 128
        length.append(digit | (128 if size else 0))
        if not size:
            return bytes([kind]) + length + data


class ServicesClient:
    def __init__(self, provision):
        self.folder = Path(provision)
        self.deployment = json.loads((self.folder / "deployment.json").read_text())
        self.access = json.loads((self.folder / "device-access.json").read_text())
        self.context = ssl.create_default_context(cafile=str(self.folder / "ca.pem"))
        self.last_request = 0

    def request(self, path, method="GET", role=None, data=None, headers=None):
        time.sleep(max(0, .35 - (time.monotonic() - self.last_request)))
        headers = dict(headers or {})
        if role:
            headers["Authorization"] = "Bearer " + self.access[role]
        if data is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(data).encode()
        connection = http.client.HTTPSConnection(self.deployment["device_ip"], context=self.context, timeout=18)
        start = time.monotonic()
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            body = response.read(100000)
            content = response.getheader("Content-Type", "")
            return {"status": response.status, "etag": response.getheader("ETag"),
                    "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
                    "body": json.loads(body) if "json" in content else body.decode(),
                    "bytes": len(body)}
        finally:
            connection.close()
            self.last_request = time.monotonic()


class MQTTObserver:
    def __init__(self, provision, password_override=None):
        self.folder = Path(provision)
        config = json.loads((self.folder / "deployment.json").read_text())
        credentials = json.loads((self.folder / "observer.json").read_text())
        self.base = config["base_topic"]
        context = ssl.create_default_context(cafile=str(self.folder / "ca.pem"))
        raw = socket.create_connection((config["broker_ip"], 8883), timeout=10)
        try:
            self.stream = context.wrap_socket(raw, server_hostname=config["broker_ip"])
        except BaseException:
            raw.close()
            raise
        self.stream.settimeout(2)
        self.messages, self.errors = [], []
        self.stop_event = threading.Event()
        self.thread = None
        self.stream.sendall(packet(0x10, b"\x00\x04MQTT\x04\xc2\x00\x1e" +
                                   field("verify-" + secrets.token_hex(4)) +
                                   field(credentials["username"]) +
                                   field(password_override if password_override is not None else credentials["password"])))
        _, answer = self.read_packet()
        if answer != b"\x00\x00":
            self.stream.close()
            raise PermissionError("MQTT connection rejected: " + str(answer[-1]))

    def exact(self, count):
        output = bytearray()
        while len(output) < count:
            part = self.stream.recv(count - len(output))
            if not part:
                raise ConnectionError("MQTT EOF")
            output.extend(part)
        return bytes(output)

    def read_packet(self):
        kind = self.exact(1)[0]
        size, scale = 0, 1
        for _ in range(4):
            digit = self.exact(1)[0]
            size += (digit & 127) * scale
            if not digit & 128:
                break
            scale *= 128
        else:
            raise ValueError("Invalid packet size")
        if size > 8192:
            raise ValueError("Packet too large")
        return kind, self.exact(size)

    def subscribe(self):
        self.stream.sendall(packet(0x82, b"\x00\x01" + field(self.base + "/#") + b"\x01"))
        kind, answer = self.read_packet()
        if kind != 0x90 or answer != b"\x00\x01\x01":
            raise RuntimeError("Subscribe rejected")
        def run():
            last_ping = time.monotonic()
            while not self.stop_event.is_set():
                try:
                    kind, body = self.read_packet()
                    if kind >> 4 == 3:
                        length = int.from_bytes(body[:2], "big")
                        topic = body[2:2 + length].decode()
                        offset = 2 + length
                        if (kind >> 1) & 3 == 1:
                            self.stream.sendall(b"\x40\x02" + body[offset:offset + 2])
                            offset += 2
                        payload = body[offset:].decode()
                        try:
                            payload = json.loads(payload)
                        except ValueError:
                            pass
                        self.messages.append({"topic": topic, "payload": payload,
                                              "retained": bool(kind & 1), "received_monotonic": time.monotonic()})
                        self.messages[:] = self.messages[-100:]
                except socket.timeout:
                    pass
                except Exception as exc:
                    if not self.stop_event.is_set():
                        self.errors.append(type(exc).__name__)
                    break
                if time.monotonic() - last_ping >= 10:
                    try:
                        self.stream.sendall(b"\xc0\x00")
                    except OSError:
                        break
                    last_ping = time.monotonic()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        try:
            self.stream.sendall(b"\xe0\x00")
        except OSError:
            pass
        self.stream.close()
