"""W5500 的 MicroPython 管理层：原生 LAN + lwIP，兼容标准 socket/asyncio/TLS。

不是纯 Python SPI 寄存器驱动。只在本地初始化时创建一次 SPI/LAN；
断线恢复沿用同一个原生网卡，避免重复构造 LAN 单例或重置正在工作的 SPI 总线。
"""

import asyncio
import time
import network_config


def _ipv4(value):
    """检查静态 IPv4 文本，不允许把主机名误写进 IP 配置。"""
    if not isinstance(value, str):
        raise ValueError("IPv4 must be a dotted address")
    parts = value.split(".")
    if len(parts) != 4:
        raise ValueError("Invalid IPv4 address")
    for part in parts:
        if (not part or len(part) > 3 or any(c not in "0123456789" for c in part)
                or int(part) > 255 or (len(part) > 1 and part[0] == "0")):
            raise ValueError("Invalid IPv4 address")


def validate_config(settings):
    """触碰 GPIO 前检查编号和重复分配；与风扇等外设的冲突仍需结合实际接线核对。"""
    pins = [settings.SCK_PIN, settings.MOSI_PIN, settings.MISO_PIN,
            settings.CS_PIN, settings.INT_PIN, settings.RESET_PIN]
    if any(type(pin) is not int or not 0 <= pin <= 48 for pin in pins):
        raise ValueError("W5500 requires six valid ESP32-S3 GPIO numbers")
    if len(set(pins)) != len(pins):
        raise ValueError("W5500 GPIO assignments must be distinct")
    if type(settings.SPI_ID) is not int or settings.SPI_ID not in (1, 2):
        raise ValueError("W5500 requires hardware SPI 1 or 2")
    if type(settings.SPI_BAUDRATE) is not int or not 0 < settings.SPI_BAUDRATE <= 80_000_000:
        raise ValueError("Invalid SPI baudrate")
    if type(settings.PHY_ADDR) is not int or not 0 <= settings.PHY_ADDR <= 31:
        raise ValueError("Invalid PHY address")
    host = settings.HOSTNAME
    if (not isinstance(host, str) or not 1 <= len(host) <= 32
            or host.startswith("-") or host.endswith("-")
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in host)):
        raise ValueError("Invalid LAN hostname")
    if type(settings.USE_DHCP) is not bool:
        raise ValueError("USE_DHCP must be a boolean")
    if not settings.USE_DHCP:
        value = settings.STATIC_IPV4
        if not isinstance(value, (tuple, list)) or len(value) != 4:
            raise ValueError("Set STATIC_IPV4 before disabling DHCP")
        for address in value:
            _ipv4(address)
        if value[0] in ("0.0.0.0", "255.255.255.255"):
            raise ValueError("Static IP cannot be unspecified or broadcast")
    for value in (settings.CONNECT_TIMEOUT, settings.RETRY_INTERVAL, settings.POLL_INTERVAL):
        if type(value) not in (int, float) or not 0 < value <= 3600:
            raise ValueError("Network intervals must be between 0 and 3600 seconds")


class W5500Ethernet:
    def __init__(self, settings=network_config, machine_module=None, network_module=None):
        validate_config(settings)
        self.settings = settings
        self._machine = machine_module
        self._network = network_module
        self.spi = None
        self.nic = None
        self.phase = "stopped"
        self.error = None

    def start(self):
        """只初始化本机 SPI/LAN，不等待网线、DHCP、DNS 或互联网。"""
        if self.nic is not None:
            return self.nic
        if self._machine is None:
            import machine
            self._machine = machine
        if self._network is None:
            import network
            self._network = network
        network = self._network
        machine = self._machine
        if not hasattr(network, "LAN") or not hasattr(network, "PHY_W5500"):
            raise RuntimeError("Firmware needs network.LAN and PHY_W5500; install a W5500-enabled build")
        if not hasattr(network, "hostname"):
            raise RuntimeError("Firmware needs network.hostname; use a recent official ESP32 build")

        # 确保没有遗留 Wi-Fi 默认路由：设备只通过有线网络访问局域网和互联网。
        if hasattr(network, "WLAN"):
            for old_name, new_name in (("STA_IF", "IF_STA"), ("AP_IF", "IF_AP")):
                interface = getattr(network, old_name, getattr(network.WLAN, new_name, None))
                if interface is None:
                    raise RuntimeError("Unsupported WLAN interface constants")
                network.WLAN(interface).active(False)
        network.hostname(self.settings.HOSTNAME)
        self.spi = machine.SPI(
            self.settings.SPI_ID, baudrate=self.settings.SPI_BAUDRATE,
            polarity=0, phase=0, sck=machine.Pin(self.settings.SCK_PIN),
            mosi=machine.Pin(self.settings.MOSI_PIN), miso=machine.Pin(self.settings.MISO_PIN))
        nic = network.LAN(
            spi=self.spi, phy_type=network.PHY_W5500, phy_addr=self.settings.PHY_ADDR,
            cs=machine.Pin(self.settings.CS_PIN), int=machine.Pin(self.settings.INT_PIN),
            reset=machine.Pin(self.settings.RESET_PIN))
        # 停止后再设置地址方式，也覆盖软复位前可能遗留的静态地址/DHCP 状态。
        nic.active(False)
        if self.settings.USE_DHCP:
            if not hasattr(nic, "ipconfig"):
                raise RuntimeError("DHCP requires LAN.ipconfig(dhcp4=True); update MicroPython firmware")
            nic.ipconfig(dhcp4=True)
        else:
            nic.ifconfig(tuple(self.settings.STATIC_IPV4))
        nic.active(True)
        self.nic = nic
        self.phase = "waiting_ip"
        self.error = None
        return nic

    def is_ready(self):
        # isconnected 在 ESP32 表示已获得 IP；再检查地址可避免误把 0.0.0.0 当作可用。
        return (self.nic is not None and self.nic.active() and self.nic.isconnected()
                and self.nic.ifconfig()[0] not in ("0.0.0.0", ""))

    def status(self):
        ready = self.is_ready()
        return {"interface": "w5500", "phase": self.phase, "ready": ready,
                "ipv4": self.nic.ifconfig() if self.nic is not None else None,
                "error": self.error}

    async def wait_ready(self, timeout=None):
        """有界等待本地网络；超时返回 False，调用方自行决定是否暂缓 OTA。"""
        if self.nic is None:
            raise RuntimeError("Call start() before waiting for Ethernet")
        if timeout is None:
            timeout = self.settings.CONNECT_TIMEOUT
        if type(timeout) not in (int, float) or not 0 <= timeout <= 3600:
            raise ValueError("Invalid network wait timeout")
        # ticks_diff 正确处理计时器回绕；绝不能使用会被 NTP 校时改变的墙上时间。
        started = time.ticks_ms()
        limit = int(timeout * 1000)
        while True:
            if self.is_ready():
                self.phase = "online"
                self.error = None
                return True
            remaining = limit - time.ticks_diff(time.ticks_ms(), started)
            if remaining <= 0:
                self.phase = "waiting_ip"
                self.error = "Waiting for Ethernet link and IPv4 address"
                return False
            self.phase = "waiting_ip"
            await asyncio.sleep(min(self.settings.POLL_INTERVAL, remaining / 1000))

    async def run(self):
        """后台监测插拔和 DHCP 恢复；不喂看门狗，不把断网升级为整个应用故障。"""
        if self.nic is None:
            raise RuntimeError("Call start() before starting the network monitor")
        while True:
            try:
                if not self.nic.active():
                    self.nic.active(True)
                if not await self.wait_ready():
                    await asyncio.sleep(self.settings.RETRY_INTERVAL)
                    continue
                while self.is_ready():
                    await asyncio.sleep(self.settings.POLL_INTERVAL)
            except OSError as exc:
                # 普通接口故障稍后重试；不捕获编程错误或任务取消，便于健康监督发现异常。
                self.phase = "error"
                self.error = str(exc)[:160]
                await asyncio.sleep(self.settings.RETRY_INTERVAL)
            # 网线恢复和 DHCP 续租由原生驱动处理，不循环重建 SPI 或切断网卡。
