"""业务入口：分钟采样、单连接 HTTPS Web/REST 与独立 MQTT 发布任务。"""
import asyncio
import app_config
from ethernet import W5500Ethernet
from hardware import create_hardware
from telemetry import TelemetryService

# 后续网页从共享服务读取状态，避免每次 HTTP 请求都重新采样或创建硬件对象。
telemetry = None
web_app = None


def create_management(boot, telemetry, ethernet):
    # 延迟导入并使用 .mpy 部署，降低启动编译及 TLS 重叠的内存峰值。
    from microdot import Microdot
    from device_state import DeviceState
    from mqtt_config import MQTTSettings
    from mqtt_service import MQTTService
    from auth import Auth
    from https_service import HTTPSService
    from rest_api import register_routes as register_rest
    from webapp import register_routes as register_web
    state = DeviceState(telemetry, ethernet, boot.version)
    settings = MQTTSettings()
    mqtt = MQTTService(settings, state)
    app = Microdot()
    https = HTTPSService(app)
    register_rest(app, state, mqtt, settings, Auth(), https)
    register_web(app, telemetry, ethernet, boot, boot.app_dir + "/static", state.device_id, secure=True, mqtt=mqtt)
    return app, https, mqtt, state


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

    web_app, https, mqtt, state = create_management(boot, telemetry, ethernet)
    network_task = asyncio.create_task(ethernet.run())
    sampling_task = asyncio.create_task(telemetry.run())
    mqtt_task = None
    try:
        # TLS 证书、认证和配置都必须成功加载，且监听建立后才确认候选槽。
        # 确认不等待网线、DHCP 或 Broker；外部服务不可达不能触发业务回退。
        await https.start()
        boot.confirm_boot()
        mqtt_task = asyncio.create_task(mqtt.run())
        while True:
            for task in (network_task, sampling_task):
                if task.done():
                    await task
                    raise RuntimeError("Background service stopped unexpectedly")
            if not telemetry.healthy():
                raise RuntimeError("Telemetry sample is stale")
            if mqtt_task is not None and mqtt_task.done():
                # MQTT 非核心任务意外退出后隔离停用，保留本地采样/管理，诊断可见。
                try:
                    await mqtt_task
                except Exception:
                    pass
                mqtt.phase, mqtt.error = "failed", "service_stopped"
                mqtt_task = None
            state.tick()
            boot.feed_watchdog()
            await asyncio.sleep(app_config.HEALTH_INTERVAL_SECONDS)
    finally:
        network_task.cancel()
        sampling_task.cancel()
        if mqtt_task is not None:
            mqtt_task.cancel()
        if https.server is not None:
            https.server.close()
