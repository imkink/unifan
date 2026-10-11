"""Web、REST 与 MQTT 的共享身份和快照模型，不访问硬件、不触发采样。"""
import os
import time
import binascii


class DeviceState:
    def __init__(self, telemetry, ethernet, version, device_id=None):
        if device_id is None:
            import machine
            device_id = binascii.hexlify(machine.unique_id()).decode().upper()
        if not device_id or device_id == "UNKNOWN":
            raise ValueError("Device identity unavailable")
        self.device_id = device_id
        self.boot_id = binascii.hexlify(os.urandom(8)).decode()
        self.telemetry, self.ethernet, self.version = telemetry, ethernet, version
        self._ticks = time.ticks_ms()
        self.uptime_ms = 0
        # RTC 年份看起来正确不等于经过可信校时；首版时间未知时明确输出 null。
        self.time_synced = False

    def tick(self):
        now = time.ticks_ms()
        self.uptime_ms += max(0, time.ticks_diff(now, self._ticks))
        self._ticks = now

    def meta(self):
        self.tick()
        age = self.telemetry.sample_age_ms()
        return {"device_id": self.device_id, "boot_id": self.boot_id,
                "uptime_ms": self.uptime_ms, "time_synced": self.time_synced,
                "sample_seq": self.telemetry.snapshot().get("sequence", 0),
                "sample_age_ms": age, "sampled_at": self.utc(age // 1000) if age is not None else None}

    def utc(self, age_seconds=0):
        if not self.time_synced:
            return None
        value = time.gmtime(time.time() - age_seconds)
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % value[:6]

    def set_time(self, unix_seconds):
        # 只允许受认证管理端提供 UTC；显式处理 MicroPython 的 2000/1970 epoch 差异。
        if type(unix_seconds) is not int or not 1767225600 <= unix_seconds <= 4102444799:
            raise ValueError("invalid_unix_seconds")
        offset = 946684800 if time.gmtime(0)[0] == 2000 else 0
        value = time.gmtime(unix_seconds - offset)
        import machine
        machine.RTC().datetime((value[0], value[1], value[2], value[6], value[3], value[4], value[5], 0))
        self.time_synced = True

    def state(self):
        snapshot = self.telemetry.snapshot()
        stale = snapshot.get("stale", True)
        simulated = snapshot.get("simulated", False)
        quality = "stale" if stale else ("simulated" if simulated else "valid")
        environment = snapshot.get("environment", {})
        network = self.ethernet.status()
        ipv4 = network.get("ipv4")
        fans, groups = [], []
        seen = []
        for fan in snapshot.get("fans", []):
            group = fan["group"]
            fans.append({"id": fan["id"], "group": group,
                         "rpm": None if stale else fan.get("rpm"),
                         "quality": quality, "status": "unknown"})
            if group not in seen:
                seen.append(group)
                groups.append({"id": group, "pwm_percent": None if stale else fan.get("pwm_percent"),
                               "mode": "unconfigured"})
        return {"source": snapshot.get("source"), "simulated": simulated, "stale": stale,
                "hardware_ready": snapshot.get("hardware_ready", False),
                "control_policy_ready": snapshot.get("control_policy_ready", False),
                "environment": {"temperature_c": None if stale else environment.get("temperature_c"),
                                "humidity_percent": None if stale else environment.get("humidity_percent"),
                                "quality": quality}, "groups": groups, "fans": fans,
                "health": "stale" if stale else ("simulated" if simulated else "unknown"),
                "sys_code": None, "active_faults": None,
                "network": {"interface": "w5500", "phase": network.get("phase"),
                            "ready": network.get("ready", False), "ip": ipv4[0] if ipv4 else None}}

    def info(self):
        return {"product": "UNIFAN", "board": "STAMPS3", "version": self.version,
                "device_id": self.device_id, "sample_interval_seconds": 60,
                "publish_interval_seconds": 600, "history_interval_seconds": 600,
                "history_retention_days": 7, "fan_groups": [1, 1, 1, 1, 2, 2],
                "capabilities": {"https": True, "rest_v1": True, "mqtt": True,
                                 "history": False, "control": False, "ota_http": False, "clock_sync": True},
                "source": self.telemetry.snapshot().get("source")}
