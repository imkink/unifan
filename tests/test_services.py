"""正式 MQTT/REST 的协议、安全、持久化与资源边界回归；不访问真实设备。"""
import asyncio
import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware/app_a"))
from auth import Auth
from device_state import DeviceState
from mqtt_config import DEFAULTS, MQTTSettings, RevisionError, validate
from mqtt_transport import MQTTTransport, MQTTProtocolError, MQTTAuthError, field, packet
from mqtt_service import MQTTService
from https_service import BoundedReader, RequestLimit, HTTPSService
from rest_api import register_routes
from microdot import Microdot
from microdot.test_client import TestClient


class MemoryNVS:
    def __init__(self):
        self.saved = None
        self.staged = None
        self.commits = 0
        self.fail = False

    def get_blob(self, name, target):
        if self.saved is None:
            raise OSError(-4354)
        target[:len(self.saved)] = self.saved
        return len(self.saved)

    def set_blob(self, name, data):
        self.staged = bytes(data)

    def commit(self):
        if self.fail:
            raise OSError(28)
        self.saved = self.staged
        self.commits += 1


class ServiceFixture:
    def setup_services(self):
        for name, value in (("ticks_ms", lambda: int(time.monotonic() * 1000)),
                            ("ticks_diff", lambda a, b: a - b)):
            item = patch.object(time, name, value, create=True)
            item.start()
            self.addCleanup(item.stop)
        self.nvs = MemoryNVS()
        self.settings = MQTTSettings(self.nvs)
        self.snapshot = {"source": "simulated", "simulated": True, "stale": False,
                         "sequence": 4, "hardware_ready": False, "control_policy_ready": False,
                         "environment": {"temperature_c": 26.0, "humidity_percent": 55.0},
                         "fans": [{"id": i + 1, "group": 1 if i < 4 else 2, "rpm": 600,
                                   "pwm_percent": 30} for i in range(6)]}
        self.telemetry = SimpleNamespace(snapshot=lambda: copy.deepcopy(self.snapshot),
                                         healthy=lambda: not self.snapshot["stale"], sample_age_ms=lambda: 100)
        self.ethernet = SimpleNamespace(is_ready=lambda: True, status=lambda: {
            "phase": "online", "ready": True, "ipv4": ("192.168.2.3", "", "", "")})
        self.state = DeviceState(self.telemetry, self.ethernet, "0.2.0", "48CA43B5FC00")
        self.auth = Auth({"admin": "a" * 64, "read": "b" * 64, "origins": ["https://192.168.2.3"]})
        self.mqtt = MQTTService(self.settings, self.state)


class SettingsTests(ServiceFixture, unittest.TestCase):
    def setUp(self):
        self.setup_services()

    def test_defaults_disabled_and_tls_required(self):
        self.assertFalse(self.settings.value["enabled"])
        with self.assertRaises(ValueError):
            validate(dict(DEFAULTS, tls=False))

    def test_unknown_boolean_port_and_dns_rejected(self):
        for change in ({"port": True}, {"port": 0}, {"host": "example.com"}, {"enabled": 1},
                       {"host": "192.168.02.3"}, {"topic": "#"}, {"password": 123}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.settings.patch(change, self.settings.etag())
        self.assertEqual(self.nvs.commits, 0)

    def test_revision_includes_secret_and_survives_restart(self):
        self.settings.patch({"password": "first"}, '"mqtt-0"')
        self.assertNotIn("password", self.settings.public())
        self.assertTrue(self.settings.public()["password_configured"])
        self.settings.patch({"password": "second"}, '"mqtt-1"')
        restored = MQTTSettings(self.nvs)
        self.assertEqual(restored.value["password"], "second")
        self.assertEqual(restored.etag(), '"mqtt-2"')

    def test_noop_does_not_write_and_null_clears(self):
        self.assertFalse(self.settings.patch({}, '"mqtt-0"'))
        self.settings.patch({"password": ""}, '"mqtt-0"')
        self.assertTrue(self.settings.public()["password_configured"])
        self.settings.patch({"port": 8883}, '"mqtt-1"')
        self.assertEqual(self.nvs.commits, 1)
        self.settings.patch({"password": None}, '"mqtt-1"')
        self.assertFalse(self.settings.public()["password_configured"])

    def test_stale_etag_and_commit_failure_preserve_memory(self):
        with self.assertRaises(RevisionError):
            self.settings.patch({"port": 1234}, '"mqtt-8"')
        self.nvs.fail = True
        with self.assertRaises(OSError):
            self.settings.patch({"port": 1234}, '"mqtt-0"')
        self.assertEqual(self.settings.value, DEFAULTS)
        self.assertEqual(self.settings.revision, 0)

    def test_corrupt_storage_not_silently_reset(self):
        self.nvs.saved = b'{"schema":1,"revision":"invalid"}'
        with self.assertRaises(ValueError):
            MQTTSettings(self.nvs)

    def test_stale_fields_are_null_not_zero(self):
        self.snapshot["stale"] = True
        data = self.state.state()
        self.assertIsNone(data["environment"]["temperature_c"])
        self.assertTrue(all(fan["rpm"] is None for fan in data["fans"]))
        self.assertTrue(all(group["pwm_percent"] is None for group in data["groups"]))
        self.assertEqual(data["environment"]["quality"], "stale")

    def test_simulation_unknown_faults_and_shared_groups(self):
        data = self.state.state()
        self.assertEqual(len(data["groups"]), 2)
        self.assertEqual(data["health"], "simulated")
        self.assertIsNone(data["sys_code"])
        self.assertIsNone(data["active_faults"])
        self.assertEqual(self.state.meta()["sample_seq"], 4)
        self.assertFalse(self.state.meta()["time_synced"])


class RESTTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_services()
        self.https = SimpleNamespace(maintenance=False, status=lambda: {"max_connections": 1})
        app = Microdot()
        register_routes(app, self.state, self.mqtt, self.settings, self.auth, self.https)
        self.http = TestClient(app)
        self.admin = {"Authorization": "Bearer " + "a" * 64, "Content-Type": "application/json", "If-Match": '"mqtt-0"'}

    async def test_public_status_never_writes_or_samples(self):
        for path in ("info", "status", "health"):
            response = await self.http.get("/api/v1/" + path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.nvs.commits, 0)
        self.assertEqual(self.state.meta()["sample_seq"], 4)

    async def test_anonymous_write_denied(self):
        result = await self.http.patch("/api/v1/settings/mqtt", body={"enabled": False})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(self.nvs.commits, 0)

    async def test_read_token_cannot_write(self):
        headers = dict(self.admin, Authorization="Bearer " + "b" * 64)
        response = await self.http.patch("/api/v1/settings/mqtt", headers=headers, body={"port": 1234})
        self.assertEqual(response.status_code, 403)
        response = await self.http.get("/api/v1/settings/mqtt", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password", response.json["data"])

    async def test_wrong_origin_denied(self):
        response = await self.http.patch("/api/v1/settings/mqtt", headers=dict(self.admin, Origin="https://evil.invalid"), body={})
        self.assertEqual(response.status_code, 403)

    async def test_missing_etag(self):
        headers = dict(self.admin)
        del headers["If-Match"]
        response = await self.http.patch("/api/v1/settings/mqtt", headers=headers, body={})
        self.assertEqual(response.status_code, 428)

    async def test_revision_conflict(self):
        response = await self.http.patch("/api/v1/settings/mqtt", headers=dict(self.admin, **{"If-Match": '"mqtt-2"'}), body={})
        self.assertEqual(response.status_code, 412)

    async def test_persist_does_not_claim_broker_online(self):
        response = await self.http.patch("/api/v1/settings/mqtt", headers=self.admin, body={"password": "secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["ETag"], '"mqtt-1"')
        self.assertEqual(response.json["data"]["apply_status"], "pending")
        self.assertNotIn("secret", response.text)

    async def test_bad_json_and_unknown_fields(self):
        response = await self.http.patch("/api/v1/settings/mqtt", headers=dict(self.admin), body="{")
        self.assertEqual(response.status_code, 400)
        self.auth.last_write = None
        response = await self.http.patch("/api/v1/settings/mqtt", headers=dict(self.admin), body={"bad": True})
        self.assertEqual(response.status_code, 422)

    async def test_storage_failure(self):
        self.nvs.fail = True
        response = await self.http.patch("/api/v1/settings/mqtt", headers=self.admin, body={"port": 1234})
        self.assertEqual(response.status_code, 507)
        self.assertEqual(self.settings.revision, 0)

    async def test_maintenance_and_unknown_capability(self):
        self.https.maintenance = True
        response = await self.http.get("/api/v1/status")
        self.assertEqual(response.status_code, 503)
        self.https.maintenance = False
        response = await self.http.get("/api/v1/history", headers=self.admin)
        self.assertEqual(response.status_code, 404)

    async def test_broker_down_does_not_fail_health(self):
        self.mqtt.phase = "backoff"
        response = await self.http.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.snapshot["stale"] = True
        response = await self.http.get("/api/v1/health")
        self.assertEqual(response.status_code, 503)

    async def test_time_sync_requires_admin_and_valid_epoch(self):
        response = await self.http.put("/api/v1/system/time", body={"unix_seconds": 1791740000})
        self.assertEqual(response.status_code, 401)
        response = await self.http.put("/api/v1/system/time", headers=dict(self.admin), body={"unix_seconds": True})
        self.assertEqual(response.status_code, 422)
        self.auth.last_write = None
        machine = Mock()
        with patch.dict(sys.modules, {"machine": machine}):
            response = await self.http.put("/api/v1/system/time", headers=dict(self.admin), body={"unix_seconds": 1791740000})
        self.assertEqual(response.status_code, 200)
        machine.RTC.return_value.datetime.assert_called_once()
        self.assertTrue(self.state.time_synced)


class TransportTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_services()

    def test_packet_encoding_and_size_limits(self):
        self.assertEqual(field("a"), b"\x00\x01a")
        self.assertEqual(packet(0x30, b"a" * 128)[:3], b"\x30\x80\x01")
        with self.assertRaises(ValueError):
            packet(0x30, b"x" * 4097)

    async def test_qos_ack_id_is_checked(self):
        client = MQTTTransport()
        client.reader = asyncio.StreamReader()
        client.writer = Mock(drain=AsyncMock())
        client.reader.feed_data(b"\x40\x02\x00\x02")
        with self.assertRaises(MQTTProtocolError):
            await client.publish("test/topic", "value")

    async def test_oversized_broker_packet_rejected_before_body(self):
        client = MQTTTransport()
        client.reader = asyncio.StreamReader()
        client.writer = Mock(drain=AsyncMock())
        client.reader.feed_data(b"\x40\xff")
        with self.assertRaises(MQTTProtocolError):
            await client.publish("test/topic", "value")

    async def test_session_initial_order_and_offline_ack(self):
        client = Mock(connect=AsyncMock(), close=AsyncMock())
        sent = []
        async def publish(topic, value):
            sent.append((topic, value))
            if value == "online":
                self.settings.revision += 1
        client.publish = publish
        self.mqtt.transport_factory = lambda: client
        await self.mqtt.session(0, DEFAULTS)
        self.assertEqual([item[0].rsplit("/", 1)[1] for item in sent], ["info", "state", "availability", "availability"])
        self.assertEqual([sent[-2][1], sent[-1][1]], ["online", "offline"])
        self.assertTrue(all(item[0].startswith("unifan/sim/") for item in sent))
        self.assertEqual(json.loads(sent[1][1])["sample_seq"], 4)
        client.close.assert_awaited_once_with(True)

    async def test_connect_failure_closes_without_normal_disconnect(self):
        client = Mock(connect=AsyncMock(side_effect=OSError()), close=AsyncMock())
        self.mqtt.transport_factory = lambda: client
        with self.assertRaises(OSError):
            await self.mqtt.session(0, DEFAULTS)
        client.close.assert_awaited_once_with(False)

    async def test_tls_required_and_broker_identity_supplied(self):
        client = MQTTTransport()
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x20\x02\x00\x05")
        writer = Mock(drain=AsyncMock())
        context = Mock()
        with patch("mqtt_transport.ssl.SSLContext", return_value=context), \
                patch("mqtt_transport.asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))) as connect:
            with self.assertRaises(MQTTAuthError):
                await client.connect(dict(DEFAULTS, host="192.168.2.1", server_name="broker.example"), "test", "base")
        connect.assert_awaited_once_with("192.168.2.1", 8883, ssl=context, server_hostname="broker.example")
        context.load_verify_locations.assert_called_once()

    async def test_long_http_line_bounded(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 20000 + b"\n")
        with self.assertRaises(RequestLimit):
            await BoundedReader(stream).readline()
        self.assertGreater(len(stream._buffer), 18000)

    async def test_duplicate_length_and_chunked_rejected(self):
        for request in (b"GET / HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 0\r\n",
                        b"GET / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n",
                        b"POST / HTTP/1.1\r\nContent-Length: -1\r\n"):
            stream = asyncio.StreamReader()
            stream.feed_data(request)
            stream.feed_eof()
            reader = BoundedReader(stream)
            with self.assertRaises(RequestLimit):
                while True:
                    await reader.readline()

    async def test_second_connection_closed_before_tls_allocation(self):
        service = HTTPSService(Mock())
        service.active = 1
        service.context = Mock()
        writer = Mock(wait_closed=AsyncMock())
        await service.accept(Mock(), writer)
        service.context.wrap_socket.assert_not_called()
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()
        self.assertEqual(service.rejected, 1)

    async def test_missing_certificate_never_starts_plaintext(self):
        service = HTTPSService(Mock())
        context = Mock()
        context.load_cert_chain.side_effect = OSError()
        with patch("https_service.ssl.SSLContext", return_value=context), \
                patch("https_service.asyncio.start_server", new=AsyncMock()) as start:
            with self.assertRaises(OSError):
                await service.start()
        start.assert_not_called()
