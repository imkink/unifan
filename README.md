# UNIFAN

Rack Fan Module & Control System — StampS3 / official MicroPython.

当前实现：GitHub Releases 文件级 OTA、A/B 业务目录、启动确认与失败回退，
W5500 有线以太网管理、单连接 HTTPS Web＋REST、MQTT TLS 发布，以及待接入的鉴权 OTA 路由。
设备只通过 W5500 接入局域网和互联网，不使用 Wi-Fi。温湿度与风扇已有可运行的
模拟接口，真实驱动和控制算法保留伪代码。管理页显示温湿度、6 路风扇、网络和
系统状态与 MQTT 阶段；设置按钮可使用 read/admin 令牌读取或修改持久化 MQTT 配置。
完整部署、API、安全和 Broker 运维说明见 [HTTPS / REST / MQTT 服务](docs/services.md)。
本轮设备写入、账号权限与真实十分钟周期验证见 [服务部署记录](docs/deployment-services-2026-10-12.md)。
页面支持简体中文和英文，首次访问默认英文，可在页脚中间切换，并在本机保存用户选择。

```text
firmware/                 上传到设备文件系统根目录的内容
  main.py                 固定启动入口
  network_config.py       固定 W5500 管脚和 IP 配置（逐项中文说明）
  lib/unifan_ota/          固定 OTA 运行库
  app_a/                  业务目录（有线网络、模拟硬件、采样与 Web 服务）
    webapp.py             只读 Dashboard 路由与 `/api/status`
    https_service.py      唯一 HTTPS 监听器、单连接预算和有界请求解析
    rest_api.py           REST v1 状态、诊断、MQTT 设置和可信校时
    mqtt_service.py       MQTT TLS 发布、在线遗嘱、周期与重连
    static/               无框架、无 CDN 的 HTML/CSS/JS 与本地字体
  certs/                  GitHub TLS 根证书，由准备工具生成
tools/build_release.py    在电脑上生成发布附件
tools/build_services.py   交叉编译业务与打包单请求管理页面
tools/deploy_services.py  备份后通过 USB 安装非活动 A/B 槽
tools/verify_services.py  HTTPS/MQTT/REST 实机验收与十分钟周期验证
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

在电脑上预览 Dashboard（使用固定模拟数据，不连接设备）：

```sh
python3 tools/preview_dashboard.py
```

打开 `http://127.0.0.1:8765/`；响应式检查页为 `/_preview/mobile`（402px）和
`/_preview/narrow`（300px）；该桌面预览不提供真实 MQTT 配置写入。
设备运行时通过受信任 HTTPS 访问；页面每次 `GET /api/status` 完成后再等待 5 秒，
全部请求串行，设置弹窗打开时暂停轮询。页面轮询只读取缓存，温湿度及六路风扇
状态在启动时采样一次，之后每 1 分钟更新；采样超过 90 秒未更新则视为过期。
MQTT 常规发布已实现，每 10 分钟一次；历史记录仍规划为每 10 分钟、保留 7 天，尚未实现。
MQTT 原始规划见 [MQTT 发布规划](docs/mqtt-plan.md)。REST v1 首版已实现，历史/控制/告警接口
仍保留在 [REST 服务规划](docs/rest-plan.md)；实际路由与限制以 [服务说明](docs/services.md) 为准。
OTA 暂停采样、MQTT 和普通 REST 的生命周期见 [维护模式规划](docs/ota-maintenance.md)，尚未接入固件。
独立 HTTPS 原型、证书校验与串口实机测试方式见 [HTTPS 原型验证](docs/https-prototype.md)。
并发内存边界与维护下载实测见 [2026-10-12 HTTPS 测试记录](docs/https-test-2026-10-12.md)。

首次部署先看 [W5500 有线联网](docs/ethernet.md) 和 [OTA 使用说明](docs/ota.md)。
底层 W5500 驱动由官方固件的 `network.LAN` 提供，初始化和后台管理使用 MicroPython。
这套 OTA 更新 Python 业务代码和静态页面，
不更新 MicroPython 固件、固定启动器、公共依赖或设备配置。

已完成一台 StampS3 的首次烧录与 W5500/HTTPS 实机验证，见
[2026-10-08 部署记录](docs/deployment-2026-10-08.md)。
