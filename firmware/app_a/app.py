"""业务入口：W5500、遥测采样与只读管理页面。"""
import asyncio
import app_config
from ethernet import W5500Ethernet
from hardware import create_hardware
from telemetry import TelemetryService

# 后续网页从共享服务读取状态，避免每次 HTTP 请求都重新采样或创建硬件对象。
telemetry = None
web_app = None


async def main(boot):
    global telemetry, web_app
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

    # Microdot 是首次部署到 /lib 的固定依赖；页面和路由随 A/B 业务版本更新。
    from microdot import Microdot
    from webapp import register_routes
    web_app = Microdot()
    register_routes(web_app, telemetry, ethernet, boot)

    network_task = asyncio.create_task(ethernet.run())
    sampling_task = asyncio.create_task(telemetry.run())
    web_task = asyncio.create_task(web_app.start_server(port=80, debug=False))
    try:
        # 必须等监听端口真正建立后再确认候选槽；仅仅创建 task 不能证明 Web 服务可用。
        # 这里确认的是模拟采样与管理页的软件链路，绝不代表真实硬件已经自检通过。
        for _ in range(50):
            if web_task.done():
                await web_task
                raise RuntimeError("Web service stopped during startup")
            if getattr(web_app, "server", None) is not None:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Web service did not start within 5 seconds")
        boot.confirm_boot()
        while True:
            for task in (network_task, sampling_task, web_task):
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
        web_task.cancel()
