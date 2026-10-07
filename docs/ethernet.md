# StampS3 + W5500 有线网络

项目使用 MicroPython 初始化和管理 W5500，底层通过官方固件的 `network.LAN`
接入 ESP32 的 lwIP 网络栈。Python 的标准 `socket`、`asyncio`、`ssl` 和 Microdot
可以沿用，不需要为 OTA 单独编写以太网 HTTPS 协议栈。

这不是纯 Python SPI 寄存器驱动：原生 W5500 支持必须已经编译进 MicroPython 固件。
没有该支持时，仅复制本项目的 `.py` 文件不能补齐。

## 接线与配置

接线来自项目文档 `REF-2.STAMP-S3管脚定义.md` 及其中的更正记录：

| W5500 信号 | StampS3 GPIO | 说明 |
| --- | --- | --- |
| SCLK | G41 | SPI 时钟 |
| MOSI | G42 | StampS3 发往 W5500，已按更正记录使用 G42 |
| MISO | G14 | W5500 发往 StampS3 |
| CSN | G40 | 低电平片选 |
| INTN | G39 | 低电平中断，当前实现要求接好 |
| RSTN | G12 | 低电平复位，原生驱动控制时序 |

网络信号应为 3.3V 逻辑；模块供电按所使用 W5500 模块的实际电路选择并共地。
运行中不要用其他代码读写 W5500 寄存器或重新配置同一 SPI 控制器。

所有板级设置集中在 `firmware/network_config.py`，每个参数都附有中文含义、单位和约束。
默认 DHCP；若需要静态地址，把 `USE_DHCP` 改为 `False`，同时填写 `STATIC_IPV4` 的
地址、掩码、网关和 DNS。示例 IP 仅作格式说明，不应未经核对直接用于现场网络。
设备名 `HOSTNAME` 不保证自动注册成可解析的 `.local` 名称。

`SPI_BAUDRATE` 是 Python SPI 初始化参数。当前官方 ESP32 LAN 实现为原生以太网设备
单独设置 SPI 时钟，可能使用编译宏 `MICROPY_PY_NETWORK_LAN_SPI_CLOCK_SPEED_MZ`；
若要改变实际以太网通信频率，需核对所使用固件的实现，而不是仅修改 Python 值。

## 首次启动前检查固件

在串口 REPL 中检查：

```python
import sys, os, network
print(sys.implementation)
print(os.uname())
print('LAN:', hasattr(network, 'LAN'))
print('W5500:', hasattr(network, 'PHY_W5500'))
print('hostname:', hasattr(network, 'hostname'))
```

这些是必要条件，最终还要实际执行 W5500 初始化。不同官方版本/构建选项可能不同，
不能仅凭“官方固件”推断模块一定可用。缺失时选择启用了 W5500 的 ESP32-S3 固件，
或基于官方源码构建：核对 LAN 支持、`CONFIG_ETH_USE_SPI_ETHERNET`、
`CONFIG_ETH_SPI_ETHERNET_W5500` 和 TLS 功能。升级前备份设备数据。

DHCP 配置使用非阻塞的 `LAN.ipconfig(dhcp4=True)`，当前实现不调用某些旧版本中可能
阻塞等待地址的 `ifconfig('dhcp')`。如果固件缺少该 API，会明确报错并要求更新。

## 启动与断线恢复

`app.main(boot)` 创建 `W5500Ethernet` 并调用 `start()`：

1. 检查配置和固件能力，关闭可能遗留的 Wi-Fi STA/AP，避免错误的默认路由。
2. 用指定 GPIO 创建硬件 SPI，把 SPI、CS、INT 和 RST 交给 `network.LAN`。
3. 设置 DHCP 或静态 IPv4，然后启用网卡；不在此处等待互联网。
4. 后台 `run()` 监测链路与地址，网线恢复和 DHCP 续租由原生驱动处理。

网线未插、路由器未开或 DHCP 无响应时，Python 使用异步等待，本地任务继续运行。
连接等待超时不会重建 SPI、不复位整个设备，也不会因此拒绝 OTA 候选版本。
配置错误、缺少原生模块、硬件初始化失败则会暴露为本地初始化异常，需查看串口日志。

后续网页可读取 `ethernet.status()` 显示 `phase`、`ready`、IPv4 四元组和最近错误；
发起 OTA 前可以 `await ethernet.wait_ready()`。其中 `ready` 只代表有线接口和 IPv4
就绪，不验证 DNS、网关、GitHub、证书或系统时间。

## HTTPS 与校时

OTA 使用现有严格验证证书的 HTTPS 下载器，网络流量通过 W5500 默认路由发送。
W5500 不负责 TLS 加解密，也不会自动提供正确日期。首次安装工具 `tools/usb_install.py`
会经 USB 明确写入 UTC；`mpremote rtc --set` 自身默认使用电脑本地时间，不能直接假定为 UTC。
也可以在已有以太网连接后使用固件/已安装库中的 `ntptime.settime()` 校时。
当前没有内置自动 NTP 调度；`ntptime` 的 DNS/UDP 调用可能阻塞，正式控制任务中需
另外规划校时调度并测量延迟。RTC 保存 UTC，网页显示时再转换成本地时区。

## 验证边界

主机测试涵盖正确 GPIO、禁用 Wi-Fi、DHCP/静态 IP、缺失原生驱动、重复初始化、
拔插网线、无 IP 超时、异步调度和断网启动确认；MicroPython 交叉编译只检查语法。
2026-10-08 已在连接的 StampS3/W5500 上验证正常初始化、DHCP 和严格验证证书的 GitHub
HTTPS，见 [首次部署记录](deployment-2026-10-08.md)。实际 SPI 时钟、断线恢复、升级断电
以及加入真实温控后的峰值内存和控制延迟仍需进一步测量。

参考：[MicroPython ESP32 LAN 文档](https://docs.micropython.org/en/latest/esp32/quickref.html#lan)、
[官方 ESP32 LAN 实现](https://github.com/micropython/micropython/blob/master/ports/esp32/network_lan.c)。
