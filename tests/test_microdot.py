"""真实 Microdot 路由集成测试；安装 requirements-dev.txt 后启用，仍使用模拟升级网络。"""

import unittest
from test_ota import OTAFixture, register_routes

try:
    from microdot import Microdot
    from microdot.test_client import TestClient
except ImportError:
    Microdot = None


@unittest.skipIf(Microdot is None, "Install requirements-dev.txt for real Microdot tests")
class MicrodotTests(OTAFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        app = Microdot()
        async def authorize(request):
            # 仅测试使用的令牌；不是设备默认密码，也不能复制到生产鉴权逻辑。
            return request.headers.get("Authorization") == "Bearer TEST-ONLY"
        self.jobs = register_routes(app, self.updater, authorize)
        self.http = TestClient(app)
        self.headers = {"Authorization": "Bearer TEST-ONLY"}

    async def test_real_json_check_install_and_status(self):
        response = await self.http.post("/api/ota/check", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["available"])
        response = await self.http.post("/api/ota/install", headers=self.headers,
                                        body={"version": "1.0.0"})
        self.assertEqual(response.status_code, 202)
        await self.jobs["task"]
        response = await self.http.get("/api/ota/status", headers=self.headers)
        self.assertEqual(response.json["pending_version"], "1.0.0")
        self.assertEqual(response.json["progress"]["phase"], "ready")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_framework_requests_are_authenticated(self):
        response = await self.http.post("/api/ota/install", body={"version": "1.0.0"})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.jobs["task"])

    async def test_malformed_json_returns_400_not_500(self):
        headers = dict(self.headers, **{"Content-Type": "application/json"})
        response = await self.http.post("/api/ota/install", headers=headers, body="{")
        self.assertEqual(response.status_code, 400)
