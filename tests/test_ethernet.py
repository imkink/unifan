"""W5500 管理层主机测试：模拟 GPIO、SPI、原生 LAN 和 DHCP，不连接真实设备。"""

import asyncio
import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware"))
sys.path.insert(0, str(ROOT / "firmware/app_a"))
sys.path.insert(0, str(ROOT / "firmware/lib"))

import ethernet
import network_config


def settings(**overrides):
    values = {name: getattr(network_config, name) for name in dir(network_config) if name.isupper()}
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeLAN:
    def __init__(self):
        self.enabled = False
        self.connected = False
        self.address = ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")
        self.dhcp = None
        self.calls = []

    def active(self, value=None):
        if value is not None:
            self.enabled = value
            self.calls.append(("active", value))
        return self.enabled

    def ipconfig(self, **kwargs):
        self.dhcp = kwargs["dhcp4"]
        self.calls.append(("dhcp", self.dhcp))

    def ifconfig(self, value=None):
        if value is not None:
            self.address = value
            self.dhcp = False
            self.calls.append(("static", value))
        return self.address

    def isconnected(self):
        return self.connected

    def acquire_ip(self):
        self.connected = True
        self.address = ("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")


class EthernetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lan = FakeLAN()
        self.radios = {0: Mock(), 1: Mock()}
        self.network = SimpleNamespace(
            LAN=Mock(return_value=self.lan), PHY_W5500=5500, hostname=Mock(),
            STA_IF=0, AP_IF=1, WLAN=Mock(side_effect=lambda index: self.radios[index]))
        self.machine = SimpleNamespace(Pin=Mock(side_effect=lambda number: number), SPI=Mock())
        self.driver = ethernet.W5500Ethernet(
            settings(POLL_INTERVAL=0.001, CONNECT_TIMEOUT=0.015, RETRY_INTERVAL=0.001),
            self.machine, self.network)
        # 用单调时钟替代 MicroPython ticks；另有专门测试验证跨回绕时的计算。
        for name, value in (("ticks_ms", lambda: int(time.monotonic() * 1000)),
                            ("ticks_diff", lambda later, earlier: later - earlier)):
            context = patch.object(ethernet.time, name, value, create=True)
            context.start()
            self.addCleanup(context.stop)

    def test_documented_pins_native_driver_and_dhcp(self):
        self.driver.start()
        self.machine.SPI.assert_called_once_with(
            1, baudrate=20_000_000, polarity=0, phase=0, sck=41, mosi=42, miso=14)
        self.network.LAN.assert_called_once_with(
            spi=self.machine.SPI.return_value, phy_type=5500, phy_addr=0, cs=40, int=39, reset=12)
        self.assertEqual(self.lan.calls, [("active", False), ("dhcp", True), ("active", True)])
        self.network.hostname.assert_called_once_with("unifan")
        for radio in self.radios.values():
            radio.active.assert_called_once_with(False)

    def test_start_is_idempotent_and_does_not_recreate_spi(self):
        self.driver.start()
        self.driver.start()
        self.machine.SPI.assert_called_once()
        self.network.LAN.assert_called_once()

    def test_static_address_includes_gateway_and_dns(self):
        address = ("192.168.2.20", "255.255.255.0", "192.168.2.1", "192.168.2.1")
        self.driver.settings = settings(USE_DHCP=False, STATIC_IPV4=address)
        self.driver.start()
        self.assertEqual(self.lan.address, address)
        self.assertFalse(self.lan.dhcp)

    def test_missing_firmware_support_fails_before_touching_gpio(self):
        del self.network.PHY_W5500
        with self.assertRaisesRegex(RuntimeError, "W5500-enabled"):
            self.driver.start()
        self.machine.SPI.assert_not_called()

    def test_dhcp_requires_nonblocking_ipconfig_api(self):
        self.network.LAN.return_value = SimpleNamespace(active=Mock())
        with self.assertRaisesRegex(RuntimeError, "ipconfig"):
            self.driver.start()

    def test_bad_configuration_is_rejected(self):
        for override in ({"MOSI_PIN": 41}, {"INT_PIN": None}, {"USE_DHCP": "yes"},
                         {"POLL_INTERVAL": 0}, {"SPI_ID": 0}, {"HOSTNAME": "bad host"},
                         {"USE_DHCP": False},
                         {"USE_DHCP": False, "STATIC_IPV4": ("999.0.0.1",) * 4}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                ethernet.W5500Ethernet(settings(**override), self.machine, self.network)
        self.machine.SPI.assert_not_called()

    async def test_absent_cable_times_out_without_reset_or_busy_loop(self):
        self.driver.start()
        heartbeat = []
        async def local_work():
            for _ in range(3):
                heartbeat.append(True)
                await asyncio.sleep(0.001)
        await asyncio.gather(self.driver.wait_ready(), local_work())
        self.assertEqual(len(heartbeat), 3)
        self.assertEqual(self.driver.phase, "waiting_ip")
        self.assertFalse(self.driver.is_ready())

    async def test_link_up_with_no_address_is_not_ready(self):
        self.driver.start()
        self.lan.connected = True
        self.assertFalse(await self.driver.wait_ready(0))
        self.lan.acquire_ip()
        self.assertTrue(await self.driver.wait_ready(0))
        self.assertEqual(self.driver.status()["ipv4"][0], "192.168.1.50")

    async def test_reconnect_uses_same_lan_and_clears_error(self):
        self.driver.start()
        monitor = asyncio.create_task(self.driver.run())
        try:
            await asyncio.sleep(0.025)
            self.assertFalse(monitor.done())
            self.lan.acquire_ip()
            await asyncio.sleep(0.01)
            self.assertEqual(self.driver.phase, "online")
            self.assertIsNone(self.driver.error)
            self.lan.connected = False
            await asyncio.sleep(0.01)
            self.assertEqual(self.driver.phase, "waiting_ip")
            self.lan.acquire_ip()
            await asyncio.sleep(0.01)
            self.assertEqual(self.driver.phase, "online")
            self.network.LAN.assert_called_once()
            self.machine.SPI.assert_called_once()
        finally:
            monitor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await monitor

    async def test_interface_oserror_is_retried(self):
        self.driver.start()
        self.lan.isconnected = Mock(side_effect=OSError("temporary driver error"))
        monitor = asyncio.create_task(self.driver.run())
        try:
            await asyncio.sleep(0.005)
            self.assertFalse(monitor.done())
            self.assertEqual(self.driver.phase, "error")
            self.lan.isconnected = lambda: True
            self.lan.acquire_ip()
            await asyncio.sleep(0.01)
            self.assertEqual(self.driver.phase, "online")
        finally:
            monitor.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await monitor

    async def test_wait_uses_wrap_safe_ticks_diff(self):
        self.driver.start()
        # 模拟 ticks 在 1024 处回绕：1018 -> 2 的实际间隔为 8 ms。
        with patch.object(ethernet.time, "ticks_ms", side_effect=[1018, 2]), \
             patch.object(ethernet.time, "ticks_diff", side_effect=lambda a, b: (a - b + 512) % 1024 - 512):
            self.assertFalse(await self.driver.wait_ready(0.005))

    async def test_bootstrap_confirms_without_waiting_for_cable(self):
        class FakeMicrodot:
            def get(self, path):
                return lambda handler: handler

            async def start_server(self, **kwargs):
                # Mirror Microdot: the server attribute appears only after the
                # listening socket has been created successfully.
                self.server = object()
                while True:
                    await asyncio.sleep(1)

        spec = importlib.util.spec_from_file_location("wired_bootstrap", ROOT / "firmware/app_a/app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        boot = Mock(version="0.0.0")
        microdot = SimpleNamespace(Microdot=FakeMicrodot)
        with patch.object(app, "W5500Ethernet", return_value=self.driver), \
            patch.dict(sys.modules, {"microdot": microdot}):
            task = asyncio.create_task(app.main(boot))
            await asyncio.sleep(0.15)
            boot.confirm_boot.assert_called_once()
            boot.feed_watchdog.assert_called_once()
            self.assertFalse(self.driver.is_ready())
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
