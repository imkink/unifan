"""独立 NVS 配置；隐藏凭据也参与 revision，保存失败不能更新内存配置。"""
import json

DEFAULTS = {"enabled": False, "host": "", "port": 8883, "tls": True,
            "server_name": "", "username": "", "password": None}


def validate(value):
    if set(value) != set(DEFAULTS):
        raise ValueError("unknown_field")
    if type(value["enabled"]) is not bool or type(value["tls"]) is not bool:
        raise ValueError("invalid_boolean")
    if type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("invalid_port")
    for key, limit in (("host", 15), ("server_name", 128), ("username", 64)):
        item = value[key]
        if (not isinstance(item, str) or len(item) > limit or
                any(ord(c) < 33 or ord(c) > 126 for c in item)):
            raise ValueError("invalid_" + key)
    if value["server_name"] and any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in value["server_name"]):
        raise ValueError("invalid_server_name")
    # 固定 IPv4 避免 MicroPython 的同步 DNS 卡住整个事件循环；证书名称单独指定。
    if value["host"]:
        parts = value["host"].split(".")
        if (len(parts) != 4 or any(not p.isdigit() or len(p) > 3 or int(p) > 255
                                  or (len(p) > 1 and p[0] == "0") for p in parts)
                or parts[0] in ("0", "127") or int(parts[0]) >= 224):
            raise ValueError("invalid_host")
    password = value["password"]
    if password is not None and (not isinstance(password, str) or len(password.encode()) > 256 or "\x00" in password):
        raise ValueError("invalid_password")
    if value["enabled"] and (not value["host"] or not value["username"] or password is None):
        raise ValueError("credentials_required")
    # 正式首版不开放明文模式，避免误操作把已有账号经网络明文发送。
    if not value["tls"]:
        raise ValueError("tls_required")
    return value


class RevisionError(Exception):
    pass


class MQTTSettings:
    def __init__(self, nvs=None):
        if nvs is None:
            import esp32
            nvs = esp32.NVS("unifan_mqtt")
        self.nvs = nvs
        self.revision = 0
        self.value = dict(DEFAULTS)
        data = bytearray(2048)
        try:
            size = nvs.get_blob("config", data)
        except OSError as exc:
            if exc.args[0] not in (-4354, 4354):
                raise
        else:
            saved = json.loads(bytes(data[:size]))
            if saved.get("schema") != 1 or type(saved.get("revision")) is not int or saved["revision"] < 1:
                raise ValueError("invalid_saved_config")
            self.value = validate(saved["value"])
            self.revision = saved["revision"]

    def etag(self):
        return '"mqtt-%d"' % self.revision

    def public(self):
        value = {k: v for k, v in self.value.items() if k != "password"}
        value.update({"password_configured": self.value["password"] is not None,
                      "revision": self.revision, "publish_interval_seconds": 600})
        return value

    def patch(self, changes, expected):
        # 此函数不含 await：版本比较、NVS 提交、内存切换在事件循环中不可交错。
        if expected != self.etag():
            raise RevisionError()
        if not isinstance(changes, dict) or not set(changes).issubset(DEFAULTS):
            raise ValueError("unknown_field")
        candidate = dict(self.value)
        candidate.update(changes)
        validate(candidate)
        if candidate == self.value:
            return False
        encoded = json.dumps({"schema": 1, "revision": self.revision + 1, "value": candidate}).encode()
        if len(encoded) > 2048:
            raise ValueError("config_too_large")
        self.nvs.set_blob("config", encoded)
        self.nvs.commit()
        self.value = candidate
        self.revision += 1
        return True
