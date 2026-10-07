# UNIFAN

Rack Fan Module & Control System — StampS3 / official MicroPython.

当前实现：GitHub Releases 文件级 OTA、A/B 业务目录、启动确认与失败回退，
W5500 有线以太网管理，以及待接入的 Microdot 2.x OTA 路由。
设备只通过 W5500 接入局域网和互联网，不使用 Wi-Fi。温湿度与风扇已有可运行的
模拟接口，真实驱动和控制算法保留伪代码，管理网页尚未实现。

```text
firmware/                 上传到设备文件系统根目录的内容
  main.py                 固定启动入口
  network_config.py       固定 W5500 管脚和 IP 配置（逐项中文说明）
  lib/unifan_ota/          固定 OTA 运行库
  app_a/                  业务目录（有线网络、模拟硬件与采样骨架）
  certs/                  GitHub TLS 根证书，由准备工具生成
tools/build_release.py    在电脑上生成发布附件
tools/prepare_ca.py       从电脑信任库导出 GitHub 所需根证书
tools/usb_install.py      首次 USB 部署、文件哈希验证和 UTC 校时
tools/device_probe.py     设备端 W5500/DHCP/HTTPS 诊断（通过 mpremote run 执行）
tests/                   主机端故障恢复、传输和接口测试
docs/ota.md              部署、接口与发布说明
docs/ethernet.md         W5500 接线、固件能力检查和联网说明
docs/simulation.md       电路板完成前的模拟接口与真实驱动替换说明
```

运行验证：

```sh
python3 -m unittest discover -s tests -v
```

首次部署先看 [W5500 有线联网](docs/ethernet.md) 和 [OTA 使用说明](docs/ota.md)。
底层 W5500 驱动由官方固件的 `network.LAN` 提供，初始化和后台管理使用 MicroPython。
这套 OTA 更新 Python 业务代码和静态页面，
不更新 MicroPython 固件、固定启动器、公共依赖或设备配置。

已完成一台 StampS3 的首次烧录与 W5500/HTTPS 实机验证，见
[2026-10-08 部署记录](docs/deployment-2026-10-08.md)。
