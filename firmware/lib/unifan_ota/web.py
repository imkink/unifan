"""Microdot 2.x OTA 路由适配；调用方必须提供异步鉴权函数。"""

import asyncio
from .manifest import version_tuple
from .updater import BusyError


def register_routes(app, updater, authorize, before_reboot=None, reset=None):
    """authorize(request) 必须验证会话/令牌；浏览器还需检查 Origin/CSRF。

    不内置默认凭据，不接受任意下载地址。before_reboot 是可选异步回调，
    供应用在复位前保存状态并把风扇切到合适的安全状态。
    """
    if not callable(authorize):
        raise ValueError("An async authorization callback is required")
    jobs = {"task": None, "rebooting": False}

    def occupied():
        return jobs["task"] is not None or jobs["rebooting"] or updater.busy

    async def allowed(request):
        return await authorize(request)

    async def install_job(version):
        # 下载放到后台任务，接口先返回 202，页面再轮询状态；不在这里自动复位。
        try:
            await updater.install(version)
        except Exception as exc:
            updater._error(exc)
        finally:
            jobs["task"] = None

    async def reboot_job():
        try:
            await asyncio.sleep(2)  # 留出时间发完 HTTP 202 响应，再执行复位前钩子。
            if before_reboot is not None:
                await before_reboot()
            if reset is None:
                import machine
                machine.reset()
            else:
                reset()
        except Exception as exc:
            updater._error(exc)
        finally:
            jobs["rebooting"] = False

    @app.get("/api/ota/status")
    async def status(request):
        if not await allowed(request):
            return {"error": "Unauthorized"}, 401
        value = updater.status()
        value["busy"] = occupied()
        value["rebooting"] = jobs["rebooting"]
        return value, 200, {"Cache-Control": "no-store"}

    @app.post("/api/ota/check")
    async def check(request):
        if not await allowed(request):
            return {"error": "Unauthorized"}, 401
        if occupied():
            return {"error": "OTA busy"}, 409
        try:
            return await updater.check()
        except BusyError as exc:
            return {"error": str(exc)}, 409
        except Exception:
            return {"error": updater.progress["error"]}, 502

    @app.post("/api/ota/install")
    async def install(request):
        if not await allowed(request):
            return {"error": "Unauthorized"}, 401
        if occupied() or updater.manager.state()["pending"] != -1:
            return {"error": "OTA busy or awaiting reboot"}, 409
        try:
            body = request.json
            if not isinstance(body, dict) or set(body) != {"version"}:
                raise ValueError("Provide only the selected version")
            version_tuple(body["version"])
        except (ValueError, TypeError):
            return {"error": "Expected JSON: {version: X.Y.Z}"}, 400
        jobs["task"] = asyncio.create_task(install_job(body["version"]))
        return {"accepted": True, "version": body["version"]}, 202

    @app.post("/api/ota/reboot")
    async def reboot(request):
        if not await allowed(request):
            return {"error": "Unauthorized"}, 401
        if occupied():
            return {"error": "OTA busy"}, 409
        if updater.manager.state()["pending"] == -1:
            return {"error": "No pending update"}, 409
        jobs["rebooting"] = True
        jobs["task"] = asyncio.create_task(reboot_job_wrapper())
        return {"rebooting": True}, 202

    async def reboot_job_wrapper():
        try:
            await reboot_job()
        finally:
            jobs["task"] = None

    return jobs
