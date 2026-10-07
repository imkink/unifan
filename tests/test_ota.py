"""主机端 OTA 故障测试：模拟网络、NVS 与复位，不触碰设备和真实 GitHub Release。"""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware/lib"))
sys.path.insert(0, str(ROOT / "tools"))

from unifan_ota import config
from unifan_ota.boot import BootManager, BootContext, _trial_deadline
from unifan_ota.manifest import validate, safe_path
from unifan_ota.storage import NVSState, validate_state, write_marker, slot_version
from unifan_ota.transport import HTTPSClient, parse_url, tls_context
from unifan_ota.updater import Updater, BusyError
from unifan_ota.web import register_routes
from build_release import build


def release(version="1.0.0", files=None):
    if files is None:
        files = {"app.py": b"async def main(boot):\n    pass\n",
                 "static/index.html": b"<h1>UNIFAN</h1>"}
    manifest = {"schema": 1, "board": "stamps3", "runtime_api": 1,
                "version": version, "files": []}
    payloads = {}
    for index, (path, data) in enumerate(files.items()):
        url = f"https://github.com/imkink/unifan/releases/download/v{version}/file-{index:03d}.bin"
        manifest["files"].append({"path": path, "url": url, "size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()})
        payloads[url] = data
    payloads[config.MANIFEST_URL] = json.dumps(manifest).encode()
    return manifest, payloads


class MemoryState:
    # 深拷贝模拟持久存储边界，避免修改内存字典就意外“保存”了尚未提交的状态。
    def __init__(self):
        self.value = None
        self.history = []

    def load(self):
        return copy.deepcopy(self.value)

    def save(self, state):
        validate_state(state)
        self.value = copy.deepcopy(state)
        self.history.append(copy.deepcopy(state))


class FakeClient:
    # 小块分批返回可控制的内容，用于验证中途断线、哈希损坏和任务取消。
    def __init__(self, payloads):
        self.payloads = payloads
        self.failed_url = None

    async def get(self, url, sink, limit, expected_size=None):
        data = self.payloads[url]
        if len(data) > limit:
            raise ValueError("Response exceeds size limit")
        for offset in range(0, len(data), 7):
            sink(data[offset:offset + 7])
            await asyncio.sleep(0)
            if url == self.failed_url:
                raise OSError("Disconnected")
        if expected_size is not None and len(data) != expected_size:
            raise ValueError("Truncated")
        return len(data)


class OTAFixture:
    # 每个测试使用独立临时 A/B 目录及历史数据，断言升级只修改非活动目录。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.slots = tuple(str(self.root / name) for name in ("app_a", "app_b"))
        Path(self.slots[0]).mkdir()
        (Path(self.slots[0]) / "app.py").write_text("# confirmed code\n")
        write_marker(self.slots[0], "0.0.0")
        (self.root / "data").mkdir()
        (self.root / "data/history.bin").write_bytes(b"KEEP HISTORY")
        self.store = MemoryState()
        self.manager = BootManager(self.store, self.slots)
        self.manager.select()
        self.manifest, payloads = release()
        self.client = FakeClient(payloads)
        self.updater = Updater(self.manager, self.client)


class UpdateTests(OTAFixture, unittest.IsolatedAsyncioTestCase):
    # 覆盖下载、提交、试运行和确认各阶段；主机模拟不能替代真实 Flash 断电验证。
    async def test_install_preserves_running_code_and_data_until_confirm(self):
        result = await self.updater.check()
        self.assertTrue(result["available"])
        result = await self.updater.install("1.0.0")
        self.assertTrue(result["reboot_required"])
        self.assertEqual(self.store.value["active"], 0)
        self.assertEqual(self.store.value["pending"], 1)
        self.assertEqual(self.store.value["attempts"], 0)
        self.assertEqual(slot_version(self.slots[1]), "1.0.0")
        self.assertEqual((self.root / "data/history.bin").read_bytes(), b"KEEP HISTORY")
        self.assertEqual((Path(self.slots[0]) / "app.py").read_text(), "# confirmed code\n")
        rebooted = BootManager(self.store, self.slots)
        self.assertEqual(rebooted.select(), 1)
        self.assertEqual(self.store.value["attempts"], 1)
        rebooted.confirm()
        self.assertEqual(self.store.value["active"], 1)
        self.assertEqual(self.store.value["version"], "1.0.0")
        self.assertEqual(self.store.value["pending"], -1)
        writes = len(self.store.history)
        rebooted.confirm()
        self.assertEqual(len(self.store.history), writes)

    async def test_two_unconfirmed_boots_roll_back_on_next_boot(self):
        await self.updater.install("1.0.0")
        for attempt in (1, 2):
            rebooted = BootManager(self.store, self.slots)
            self.assertEqual(rebooted.select(), 1)
            self.assertEqual(self.store.value["attempts"], attempt)
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)
        self.assertEqual(self.store.value["pending"], -1)

    async def test_explicit_trial_failure_immediately_rolls_back(self):
        await self.updater.install("1.0.0")
        rebooted = BootManager(self.store, self.slots)
        rebooted.select()
        rebooted.reject()
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)

    async def test_trial_deadline_rejects_even_if_application_keeps_feeding(self):
        await self.updater.install("1.0.0")
        rebooted = BootManager(self.store, self.slots)
        rebooted.select()
        context = BootContext(rebooted, unittest.mock.Mock())
        machine = unittest.mock.Mock()
        with patch.dict(sys.modules, {"machine": machine}), patch.object(config, "TRIAL_TIMEOUT_MS", 1):
            context.feed_watchdog()
            await _trial_deadline(context)
        machine.reset.assert_called_once()
        self.assertEqual(self.store.value["pending"], -1)

    async def test_confirmation_prevents_deadline_reset(self):
        await self.updater.install("1.0.0")
        rebooted = BootManager(self.store, self.slots)
        rebooted.select()
        context = BootContext(rebooted, unittest.mock.Mock())
        context.confirm_boot()
        machine = unittest.mock.Mock()
        with patch.dict(sys.modules, {"machine": machine}), patch.object(config, "TRIAL_TIMEOUT_MS", 1):
            await _trial_deadline(context)
        machine.reset.assert_not_called()

    async def test_incomplete_candidate_marker_rolls_back(self):
        await self.updater.install("1.0.0")
        (Path(self.slots[1]) / config.COMPLETE_FILE).write_text("{")
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)

    async def test_structurally_corrupt_candidate_marker_rolls_back(self):
        await self.updater.install("1.0.0")
        (Path(self.slots[1]) / config.COMPLETE_FILE).write_text("[]")
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)

    async def test_network_failure_can_retry_without_touching_active(self):
        self.client.failed_url = self.manifest["files"][0]["url"]
        with self.assertRaises(OSError):
            await self.updater.install("1.0.0")
        self.assertEqual(self.store.value["pending"], -1)
        self.assertFalse((Path(self.slots[1]) / config.COMPLETE_FILE).exists())
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)
        self.assertFalse(self.updater.busy)
        self.client.failed_url = None
        await self.updater.install("1.0.0")
        self.assertEqual(self.store.value["pending"], 1)

    async def test_hash_mismatch_never_commits(self):
        url = self.manifest["files"][0]["url"]
        self.client.payloads[url] = b"x" * len(self.client.payloads[url])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            await self.updater.install("1.0.0")
        self.assertEqual(self.store.value["pending"], -1)

    async def test_cut_after_marker_before_nvs_commit_leaves_old_version_active(self):
        original = self.store.save
        def fail(state):
            if state["pending"] != -1:
                raise OSError("Power cut at commit")
            original(state)
        self.store.save = fail
        with self.assertRaises(OSError):
            await self.updater.install("1.0.0")
        self.assertEqual(slot_version(self.slots[1]), "1.0.0")
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)

    async def test_cut_before_marker_cannot_select_partial_slot(self):
        with patch("unifan_ota.updater.write_marker", side_effect=OSError("Power cut")):
            with self.assertRaises(OSError):
                await self.updater.install("1.0.0")
        self.assertFalse((Path(self.slots[1]) / config.COMPLETE_FILE).exists())
        self.assertEqual(BootManager(self.store, self.slots).select(), 0)

    async def test_no_free_space_rejects_without_activation(self):
        with patch("unifan_ota.updater.os.statvfs", return_value=(4096, 4096, 0, 0, 0)):
            with self.assertRaisesRegex(OSError, "space"):
                await self.updater.install("1.0.0")
        self.assertEqual(self.store.value["pending"], -1)

    async def test_latest_change_and_downgrade_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            await self.updater.install("2.0.0")
        manifest, payloads = release("0.0.0")
        self.client.payloads = payloads
        self.assertFalse((await self.updater.check())["available"])
        with self.assertRaisesRegex(ValueError, "newer"):
            await self.updater.install("0.0.0")

    async def test_overlapping_operations_rejected(self):
        self.updater.busy = True
        with self.assertRaises(BusyError):
            await self.updater.check()
        self.updater.busy = False
        await self.updater.install("1.0.0")
        with self.assertRaises(BusyError):
            await self.updater.install("1.0.0")
        trial = BootManager(self.store, self.slots)
        trial.select()
        with self.assertRaises(BusyError):
            await Updater(trial, self.client).check()

    async def test_second_release_targets_other_slot_and_removes_stale_files(self):
        await self.updater.install("1.0.0")
        rebooted = BootManager(self.store, self.slots)
        rebooted.select()
        rebooted.confirm()
        (Path(self.slots[0]) / "stale.py").write_text("old")
        _, payloads = release("1.0.1")
        await Updater(rebooted, FakeClient(payloads)).install("1.0.1")
        self.assertEqual(self.store.value["active"], 1)
        self.assertEqual(self.store.value["pending"], 0)
        self.assertFalse((Path(self.slots[0]) / "stale.py").exists())

    async def test_task_cancellation_unlocks_updater_without_activation(self):
        started = asyncio.Event()
        original = self.client.get
        async def pause(url, *args):
            if url != config.MANIFEST_URL:
                started.set()
                await asyncio.Event().wait()
            return await original(url, *args)
        self.client.get = pause
        task = asyncio.create_task(self.updater.install("1.0.0"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.updater.busy)
        self.assertEqual(self.store.value["pending"], -1)


class ManifestTests(unittest.TestCase):
    # 清单在任何目录清理之前必须通过校验，重点防止路径越界和版本/下载来源混用。
    def test_rejects_path_traversal_and_reserved_marker(self):
        for path in ("../main.py", "/main.py", "x/../../data/a", "x//a", "x\\a", ".hidden",
                     "x/./a", "a%2fb", config.COMPLETE_FILE, config.COMPLETE_FILE + "/a"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(path)

    def test_rejects_external_or_mutable_asset_url(self):
        manifest, _ = release()
        for url in ("https://evil.test/app.py", config.MANIFEST_URL,
                    "https://github.com/other/unifan/releases/download/v1.0.0/file-000.bin"):
            manifest["files"][0]["url"] = url
            with self.assertRaises(ValueError):
                validate(manifest)

    def test_rejects_bad_metadata_and_duplicate_paths(self):
        for field, value in (("board", "other"), ("runtime_api", 2), ("schema", True),
                             ("version", "1.0"), ("version", "01.0.0"), ("files", [])):
            manifest, _ = release()
            manifest[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(manifest)
        manifest, _ = release()
        manifest["files"][1]["path"] = "app.py"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate(manifest)

    def test_rejects_file_directory_collisions(self):
        manifest, _ = release(files={"app.py": b"", "static": b"", "static/a": b""})
        with self.assertRaisesRegex(ValueError, "conflict"):
            validate(manifest)

    def test_rejects_invalid_digest_and_oversized_release(self):
        manifest, _ = release()
        manifest["files"][0]["sha256"] = "x" * 64
        with self.assertRaises(ValueError):
            validate(manifest)
        manifest, _ = release()
        manifest["files"][0]["size"] = config.MAX_RELEASE_BYTES + 1
        with self.assertRaises(ValueError):
            validate(manifest)


class NVSTests(unittest.TestCase):
    # 区分键不存在与数据损坏；只允许前者触发首次注册。
    def test_only_missing_key_initializes_fresh_state(self):
        nvs = unittest.mock.Mock()
        nvs.get_blob.side_effect = OSError(-4354)
        self.assertIsNone(NVSState(nvs).load())
        nvs.get_blob.side_effect = OSError(-4359)
        with self.assertRaises(OSError):
            NVSState(nvs).load()

    def test_entire_state_commits_as_single_blob(self):
        nvs = unittest.mock.Mock()
        state = {"schema": 1, "active": 0, "version": "0.0.0", "pending": -1,
                 "pending_version": None, "attempts": 0}
        store = NVSState(nvs)
        store.save(state)
        nvs.set_blob.assert_called_once()
        nvs.commit.assert_called_once()
        key, value = nvs.set_blob.call_args.args
        self.assertEqual(key, "state")
        self.assertEqual(json.loads(value), state)

    def test_corrupt_nvs_does_not_fall_back_to_defaults(self):
        nvs = unittest.mock.Mock()
        def read(key, data):
            data[:3] = b"bad"
            return 3
        nvs.get_blob.side_effect = read
        with self.assertRaises(ValueError):
            NVSState(nvs).load()


class FakeStream:
    def __init__(self, data, fragment=31):
        self.data = data
        self.fragment = fragment
        self.requests = []
        self.closed = False

    async def read(self, size):
        await asyncio.sleep(0)
        size = min(size, self.fragment)
        part, self.data = self.data[:size], self.data[size:]
        return part

    def write(self, data):
        self.requests.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class TransportTests(unittest.IsolatedAsyncioTestCase):
    # 使用伪 HTTP 流覆盖协议分帧、重定向和 TLS 失败处理，不依赖互联网。
    def client(self, *responses):
        self.streams = [FakeStream(data) for data in responses]
        self.connections = []
        async def connect(host, port, **kwargs):
            self.connections.append((host, port, kwargs))
            stream = self.streams[len(self.connections) - 1]
            return stream, stream
        return HTTPSClient(connector=connect, context_factory=lambda _: "VERIFIED TLS")

    async def test_redirect_and_exact_body(self):
        client = self.client(
            b"HTTP/1.1 302 Found\r\nLocation: https://release-assets.githubusercontent.com/a?sig=abc\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
        data = bytearray()
        self.assertEqual(await client.get(config.MANIFEST_URL, data.extend, 5, 5), 5)
        self.assertEqual(data, b"hello")
        self.assertTrue(all(s.closed for s in self.streams))
        self.assertEqual(self.connections[1][2]["server_hostname"], "release-assets.githubusercontent.com")

    async def test_chunked_with_trailers(self):
        client = self.client(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                             b"2\r\nhe\r\n3;ext=x\r\nllo\r\n0\r\nDigest: unused\r\n\r\n")
        data = bytearray()
        await client.get(config.MANIFEST_URL, data.extend, 5, 5)
        self.assertEqual(data, b"hello")

    async def test_eof_delimited_body(self):
        client = self.client(b"HTTP/1.0 200 OK\r\n\r\nhello")
        data = bytearray()
        await client.get(config.MANIFEST_URL, data.extend, 5, 5)
        self.assertEqual(data, b"hello")

    async def test_rejects_bad_framing_sizes_and_untrusted_redirects(self):
        responses = (
            b"HTTP/1.1 302 Found\r\nLocation: http://github.com/a\r\n\r\n",
            b"HTTP/1.1 302 Found\r\nLocation: https://evil.test/a\r\n\r\n",
            b"HTTP/1.1 404 Not Found\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhi",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\na",
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n6\r\nabcdef\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\n\r\nabcdef",
            b"HTTP/1.1 200 OK\r\nX: " + b"a" * 4096 + b"\r\n\r\n",
        )
        for response in responses:
            with self.subTest(response=response[:60]):
                client = self.client(response)
                with self.assertRaises((ValueError, OSError)):
                    await client.get(config.MANIFEST_URL, lambda _: None, 5, 5)
                self.assertTrue(self.streams[0].closed)

    async def test_network_timeout_closes_stream(self):
        client = self.client(b"")
        async def hang(size):
            await asyncio.Event().wait()
        self.streams[0].read = hang
        with patch.object(config, "IO_TIMEOUT", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await client.get(config.MANIFEST_URL, lambda _: None, 5)
        self.assertTrue(self.streams[0].closed)

    async def test_tls_error_never_retries_without_verification(self):
        connect = unittest.mock.AsyncMock(side_effect=OSError("TLS validation failed"))
        client = HTTPSClient(connector=connect, context_factory=lambda _: "VERIFIED TLS")
        with self.assertRaises(OSError):
            await client.get(config.MANIFEST_URL, lambda _: None, 5)
        connect.assert_awaited_once()
        self.assertEqual(connect.call_args.kwargs["ssl"], "VERIFIED TLS")

    def test_unsafe_urls_rejected(self):
        for url in ("http://github.com/a", "https://github.com.evil.test/a",
                    "https://github.com:443/a", "https://github.com/a\r\nInjected: x",
                    "https://user@github.com/a", "https://github.com/a#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_url(url)

    def test_wrong_clock_refuses_tls(self):
        with patch("unifan_ota.transport.time.gmtime", return_value=(2000,)):
            with self.assertRaisesRegex(ValueError, "clock"):
                tls_context("missing.pem")


class BuilderTests(unittest.TestCase):
    def test_package_roundtrip_and_basename_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "app"
            source.mkdir()
            (source / "app.py").write_text("async def main(boot):\n    pass\n")
            (source / "static").mkdir()
            (source / "static/app.py").write_text("# same basename\n")
            (source / config.COMPLETE_FILE).write_text("ignored")
            (source / ".DS_Store").write_text("ignored")
            output = root / "out"
            manifest = build(source, output, "1.2.0")
            self.assertEqual(len(manifest["files"]), 2)
            for index, entry in enumerate(manifest["files"]):
                data = (output / f"file-{index:03d}.bin").read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
                self.assertEqual(data, (source / entry["path"]).read_bytes())
            self.assertEqual(json.loads((output / "manifest.json").read_text()), manifest)
            with self.assertRaisesRegex(ValueError, "exists"):
                build(source, output, "1.2.0")

    def test_cannot_package_outside_slot_or_recursive_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            (source / "app.py").write_text("pass\n")
            with self.assertRaisesRegex(ValueError, "outside"):
                build(source, source / "dist", "1.0.0")


class FakeApp:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        return self._route("GET", path)

    def post(self, path):
        return self._route("POST", path)

    def _route(self, method, path):
        def register(fn):
            self.routes[(method, path)] = fn
            return fn
        return register


class WebTests(OTAFixture, unittest.IsolatedAsyncioTestCase):
    async def test_all_endpoints_require_authorization(self):
        app = FakeApp()
        authorize = unittest.mock.AsyncMock(return_value=False)
        register_routes(app, self.updater, authorize)
        for route in app.routes.values():
            self.assertEqual((await route(object()))[1], 401)
        self.assertEqual(self.store.value["pending"], -1)

    async def test_install_is_background_exclusive_and_reboot_is_separate(self):
        app = FakeApp()
        authorize = unittest.mock.AsyncMock(return_value=True)
        reset = unittest.mock.Mock()
        jobs = register_routes(app, self.updater, authorize, reset=reset)
        request = unittest.mock.Mock(json={"version": "1.0.0"})
        install = app.routes[("POST", "/api/ota/install")]
        self.assertEqual((await install(request))[1], 202)
        self.assertEqual((await install(request))[1], 409)
        await jobs["task"]
        self.assertEqual(self.store.value["pending"], 1)
        reset.assert_not_called()
        self.assertEqual((await install(request))[1], 409)

    async def test_reboot_waits_for_safe_state_hook(self):
        await self.updater.install("1.0.0")
        app = FakeApp()
        events = []
        async def safe():
            events.append("safe")
        def reset():
            events.append("reset")
        jobs = register_routes(app, self.updater, unittest.mock.AsyncMock(return_value=True),
                               before_reboot=safe, reset=reset)
        reboot = app.routes[("POST", "/api/ota/reboot")]
        self.assertEqual((await reboot(object()))[1], 202)
        self.assertEqual((await reboot(object()))[1], 409)
        with patch("unifan_ota.web.asyncio.sleep", new=unittest.mock.AsyncMock()):
            await jobs["task"]
        self.assertEqual(events, ["safe", "reset"])

    async def test_bad_install_body_and_arbitrary_url_rejected(self):
        app = FakeApp()
        register_routes(app, self.updater, unittest.mock.AsyncMock(return_value=True))
        for body in (None, [], {"version": "1.0"}, {"version": "1.0.0", "url": "https://evil.test"}):
            result = await app.routes[("POST", "/api/ota/install")](unittest.mock.Mock(json=body))
            self.assertEqual(result[1], 400)


if __name__ == "__main__":
    unittest.main()
