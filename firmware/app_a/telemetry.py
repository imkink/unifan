"""可运行的采样骨架：维护内存快照，供后续网页/历史记录共享，不负责写 Flash。"""

import asyncio
import time
import app_config


class TelemetryService:
    def __init__(self, sensor, fans, settings=app_config):
        self.sensor = sensor
        self.fans = fans
        self.settings = settings
        if not 0 < settings.SAMPLE_INTERVAL_SECONDS * 1000 < settings.SAMPLE_STALE_MS:
            raise ValueError("Sampling interval must be shorter than stale timeout")
        self._snapshot = None
        self._last_sample = None
        self._sequence = 0

    async def initialize(self):
        # 自检确认只依赖本地接口，不能等待 DHCP 或 GitHub；真实占位实现会在此明确失败。
        await self.sensor.initialize()
        await self.fans.initialize()
        await self.sample_once()

    async def sample_once(self):
        environment = await self.sensor.read()
        fans = await self.fans.read()
        simulated = self.sensor.simulated or self.fans.simulated
        self._sequence += 1
        # 完成两类读取后才整体替换快照，避免页面看到一次采样的新旧数据混合。
        self._snapshot = {
            "source": "simulated" if simulated else "hardware",
            "simulated": simulated,
            "hardware_ready": not simulated,
            "control_policy_ready": False,
            "sequence": self._sequence,
            "environment": environment,
            "fans": fans,
        }
        self._last_sample = time.ticks_ms()

    def healthy(self):
        # 表示软件采样链路在工作，不等于物理硬件已验证；模拟标志必须保留。
        return (self._last_sample is not None
                and 0 <= time.ticks_diff(time.ticks_ms(), self._last_sample) < self.settings.SAMPLE_STALE_MS)

    def snapshot(self):
        if self._snapshot is None:
            return {"source": "uninitialized", "hardware_ready": False, "control_policy_ready": False}
        # 返回独立的小型结构，防止上层序列化/展示时修改采样服务内部缓存。
        value = dict(self._snapshot)
        value["environment"] = dict(value["environment"])
        value["fans"] = [dict(fan) for fan in value["fans"]]
        value["stale"] = not self.healthy()
        return value

    async def run(self):
        while True:
            await asyncio.sleep(self.settings.SAMPLE_INTERVAL_SECONDS)
            await self.sample_once()
