"""管理认证：独立随机 read/admin Bearer 令牌；不使用默认密码或环境自动附带的 Cookie。"""
import hashlib
import json
import time


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def equal(left, right):
    # 比较固定长度摘要，避免直接字符串比较暴露有效令牌前缀。
    different = len(left) ^ len(right)
    for a, b in zip(left, right):
        different |= a ^ b
    return different == 0


class Auth:
    def __init__(self, config=None):
        if config is None:
            with open("/security/access.json") as source:
                config = json.load(source)
        if any(not isinstance(config.get(k), str) or len(config[k]) != 64
               or any(c not in "0123456789abcdef" for c in config[k]) for k in ("admin", "read")):
            raise ValueError("Access tokens must be independently generated 256-bit secrets")
        if config["admin"] == config["read"]:
            raise ValueError("Separate read and admin credentials required")
        self.admin, self.read = digest(config["admin"]), digest(config["read"])
        self.origins = config["origins"]
        self.last_write = None
        self.window = time.ticks_ms()
        self.requests = 0

    def role(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) != 71:
            return None
        value = digest(header[7:])
        if equal(value, self.admin):
            return "admin"
        if equal(value, self.read):
            return "read"
        return None

    def authorize(self, request, write=False):
        role = self.role(request)
        if role is None:
            return 401, "unauthorized"
        if write and role != "admin":
            return 403, "admin_required"
        # 浏览器写请求检查 Origin；外部脚本可无 Origin，但必须显式携带管理令牌。
        origin = request.headers.get("Origin")
        if write and ((origin is not None and origin not in self.origins) or
                      request.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")):
            return 403, "origin_rejected"
        return None

    def rate(self, write=False):
        now = time.ticks_ms()
        if time.ticks_diff(now, self.window) >= 1000:
            self.window, self.requests = now, 0
        if not write:
            self.requests += 1
        if self.requests > 5:
            return False
        if write:
            if self.last_write is not None and time.ticks_diff(now, self.last_write) < 1000:
                return False
            self.last_write = now
        return True
