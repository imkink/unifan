"""单任务 MQTT 调度；Broker 故障只进入退避，不能拖垮采样或触发整机回退。"""
import asyncio
import gc
import json
import os
import time
from mqtt_transport import MQTTTransport, MQTTAuthError


class MQTTService:
    def __init__(self, settings, state, transport_factory=MQTTTransport):
        self.settings, self.state = settings, state
        self.transport_factory = transport_factory
        self.phase, self.error = "disabled", None
        self.connected = False
        self.paused = False
        self.connections = self.publish_seq = 0
        self.applied_revision = -1
        self.last_ack_uptime_ms = None
        self.transport = None
        self.worker = None
        self.minimum_heap = gc.mem_free() if hasattr(gc, "mem_free") else None

    def status(self):
        return {"implemented": True, "enabled": self.settings.value["enabled"],
                "phase": self.phase, "connected": self.connected, "error": self.error,
                "connections": self.connections, "publish_seq": self.publish_seq,
                "applied_revision": self.applied_revision,
                "last_ack_uptime_ms": self.last_ack_uptime_ms, "heap_min": self.minimum_heap}

    def base(self):
        simulated = self.state.telemetry.snapshot().get("simulated", False)
        return "unifan/" + ("sim/" if simulated else "") + self.state.device_id

    async def send_state(self, client, base):
        self.publish_seq += 1
        value = self.state.state()
        value.update(self.state.meta())
        value.update({"schema": 1, "publish_seq": self.publish_seq, "published_at": self.state.utc()})
        encoded = json.dumps(value)
        if len(encoded) > 3584:
            raise ValueError("state_too_large")
        await client.publish(base + "/state", encoded)
        self.last_ack_uptime_ms = self.state.meta()["uptime_ms"]
        if hasattr(gc, "mem_free"):
            self.minimum_heap = min(self.minimum_heap, gc.mem_free())

    async def session(self, revision, config):
        client = self.transport_factory()
        self.transport = client
        base = self.base()
        graceful = False
        try:
            self.phase = "connecting"
            await client.connect(config, "unifan-" + self.state.device_id, base)
            await client.publish(base + "/info", json.dumps(dict(self.state.info(), schema=1)))
            await self.send_state(client, base)
            await client.publish(base + "/availability", "online")
            self.connected, self.phase, self.error = True, "online", None
            self.connections += 1
            self.applied_revision = revision
            last_publish = last_ping = time.ticks_ms()
            previous_stale = not self.state.telemetry.healthy()
            while not self.paused and revision == self.settings.revision:
                if not self.state.ethernet.is_ready():
                    raise OSError("network_offline")
                now = time.ticks_ms()
                stale = not self.state.telemetry.healthy()
                due = time.ticks_diff(now, last_publish) >= 600000
                if due or stale != previous_stale:
                    await self.send_state(client, base)
                    if due:
                        last_publish = now
                    previous_stale = stale
                    last_ping = time.ticks_ms()
                elif time.ticks_diff(now, last_ping) >= 10000:
                    await client.ping()
                    last_ping = time.ticks_ms()
                await asyncio.sleep(1)
            # 正常配置切换先确认 offline，再发 DISCONNECT；失败时保留 Broker 遗嘱行为。
            await client.publish(base + "/availability", "offline")
            graceful = True
        finally:
            self.connected = False
            await client.close(graceful)
            self.transport = None
            gc.collect()

    async def run(self):
        delay = 1
        while True:
            self.state.tick()
            if self.paused or not self.settings.value["enabled"]:
                self.phase = "paused" if self.paused else "disabled"
                self.error = None
                self.applied_revision = self.settings.revision
                await asyncio.sleep(1)
                continue
            if not self.state.ethernet.is_ready():
                self.phase = "waiting_network"
                await asyncio.sleep(1)
                continue
            revision = self.settings.revision
            start = time.ticks_ms()
            self.worker = asyncio.create_task(self.session(revision, dict(self.settings.value)))
            try:
                await self.worker
                delay = 1
                continue
            except asyncio.CancelledError:
                if not self.paused:
                    raise
                continue
            except MQTTAuthError:
                self.error = "broker_auth_rejected"
                delay = 60
            except (OSError, asyncio.TimeoutError):
                self.error = "tls_or_network_error"
            except Exception:
                # 不把异常文本（可能含主机信息/秘密）复制进 HTTP 或串口日志。
                self.error = "protocol_or_internal_error"
            finally:
                self.worker = None
            self.phase = "backoff"
            if time.ticks_diff(time.ticks_ms(), start) > 60000:
                delay = 1
            wait = delay * (80 + os.urandom(1)[0] % 41) // 100
            for _ in range(max(1, wait)):
                if self.paused or revision != self.settings.revision:
                    break
                await asyncio.sleep(1)
            delay = min(60, delay * 2)

    async def pause(self):
        """维护钩子：先禁止重连，再等连接有界释放；普通 REST 暂停由上层协调。"""
        self.paused = True
        for _ in range(12):
            if self.worker is None:
                return
            await asyncio.sleep(1)
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.sleep(0)

    def resume(self):
        self.paused = False
