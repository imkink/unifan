"""业务启动骨架：真实 W5500 网络 + 可替换的模拟温湿度/风扇接口。"""
import asyncio
import app_config
from ethernet import W5500Ethernet
from hardware import create_hardware
from telemetry import TelemetryService

# 后续网页从共享服务读取状态，避免每次 HTTP 请求都重新采样或创建硬件对象。
telemetry = None


async def main(boot):
    global telemetry
    if not 0 < app_config.HEALTH_INTERVAL_SECONDS * 1000 < app_config.SAMPLE_STALE_MS:
        raise ValueError("Health interval must be shorter than stale timeout")
    print("UNIFAN bootstrap", boot.version)
    ethernet = W5500Ethernet()
    # 固件能力/接线配置错误应暴露；缺网线和 DHCP 超时则留给后台任务持续等待。
    ethernet.start()
    sensor, fans = create_hardware()
    telemetry = TelemetryService(sensor, fans)
    await telemetry.initialize()
    state = telemetry.snapshot()
    print("TELEMETRY", state["source"], "hardware_ready=", state["hardware_ready"], "control policy pending")
    network_task = asyncio.create_task(ethernet.run())
    sampling_task = asyncio.create_task(telemetry.run())
    try:
        # 模拟模式只确认软件链路可运行，绝不代表真实硬件自检通过。
        boot.confirm_boot()
        while True:
            for task in (network_task, sampling_task):
                if task.done():
                    await task
                    raise RuntimeError("Background service stopped unexpectedly")
            if not telemetry.healthy():
                raise RuntimeError("Telemetry sample is stale")
            boot.feed_watchdog()
            await asyncio.sleep(app_config.HEALTH_INTERVAL_SECONDS)
    finally:
        network_task.cancel()
        sampling_task.cancel()
