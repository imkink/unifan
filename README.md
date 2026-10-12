# UNIFAN

Rack Fan Module & Control System — StampS3 / official MicroPython.

基于 M5Stack StampS3、官方 MicroPython 和 W5500 的机架风扇监测与控制项目。
设备只使用有线以太网，不使用 Wi-Fi。当前完成安全管理与遥测首版，
温湿度、PWM 和 RPM 仍使用模拟数据，**尚未启用真实风扇控制**。

最近实机验收：2026-10-12，业务版本 **0.2.2**，MicroPython **1.29.0 / ESP32_GENERIC_S3**，
A 槽已确认启动。详细结果见 [服务部署记录](docs/deployment-services-2026-10-12.md)。

## 功能状态

| 模块 | 当前能力与边界 |
| --- | --- |
| 采样 | 启动立即采样，之后每 60 秒更新共享快照；90 秒过期阈值；查询不触发采样 |
| Web | 中英双语 Dashboard，显示温湿度、六路风扇、网络、系统与 MQTT 状态；支持 MQTT 配置 |
| HTTPS | 单个 TCP 443 服务承载 Web＋REST，最多一个已接纳的入站管理连接；不提供明文 HTTP |
| REST v1 | 信息、状态、健康、诊断、MQTT 配置读取／修改、受认证 UTC 校时 |
| MQTT | 独立出站 TLS 连接，MQTT 3.1.1、QoS 1、retained 消息、离线遗嘱与退避重连；仅发布、不接收控制命令 |
| 配置 | MQTT 配置独立存放于 NVS，密码脱敏、原子保存、ETag 防止旧配置覆盖 |
| A/B 更新基础 | 固定启动器、启动确认与失败回退、GitHub Releases 文件级 OTA 库及打包工具；当前通过受控 USB 部署业务 |
| 待实现 | 真实硬件驱动与控制策略、历史记录／查询、告警、HA Discovery、网络 OTA 管理路由与完整维护流程 |

采样周期为 **1 分钟**，MQTT 常规发布周期为 **10 分钟**。
历史记录规划为每 10 分钟一次、保留 7 天，但当前尚不生成历史文件。
核心健康监督每秒运行，硬件看门狗超时为 30 秒；Broker 离线不会单独导致设备重启。

## 服务与安全模型

Web 与 REST 共用一个 Microdot HTTPS 服务，不分别启动监听器。
唯一入站管理连接之外，允许一个独立出站 MQTT TLS 长连接。
忙时在分配 TLS 对象前关闭额外连接，因此多个浏览器或脚本同时访问可能遇到连接失败。

设备端页面打包为单个 HTML 并流式发送，不依赖 CDN。浏览器请求串行执行，
状态请求完成后等待 5 秒再轮询；打开设置弹窗时暂停轮询。
页面默认英文，可切换简体中文并保存语言偏好。

- HTTPS 使用专用本地 CA 签发的设备证书；客户端必须验证 CA 和 IP／主机名，不关闭证书校验。
- 公开只读状态与健康信息；诊断、配置读取要求 read 或 admin Bearer 令牌，所有写入要求 admin。
- 浏览器写入还校验精确 Origin，不开放 CORS、不使用 Cookie；管理令牌只保存在页面内存中。
- MQTT 强制 TLS 与 Broker 身份验证，使用独立账号和 topic ACL；不允许通过 REST 关闭 TLS。
- 请求大小、处理时间和速率均有限制，但不保证抵御局域网 DoS；建议配合 UniFi 管理 VLAN／ACL，勿映射到公网。
- 设备 Flash 尚未启用硬件加密，物理读取设备可能暴露私钥与凭据。

REST 入口为 `/api/v1/info`、`/api/v1/status`、`/api/v1/health`、`/api/v1/diagnostics`、
`/api/v1/settings/mqtt` 和 `/api/v1/system/time`；旧页面接口 `/api/status` 保留。
配置修改采用 `PATCH`＋`If-Match`，校时采用 `PUT`。权限、字段和错误码见 [完整服务说明](docs/services.md)。

模拟模式 MQTT topic 前缀为 `unifan/sim/<device_id>/`：

| 后缀 | 发布内容与时机 |
| --- | --- |
| `info` | 身份、版本、能力与周期；每次连接初始化时发布 |
| `state` | 温湿度、风扇、网络和质量／时间／序号信息；连接初始化、每 600 秒及过期状态转换时发布 |
| `availability` | `online`／`offline`；初始化确认、主动停用或 Broker 遗嘱 |

三个 topic 均使用 QoS 1 和 retain。断线期间不积压全部采样，重连后发布最新快照。
模拟数据明确标记来源；REST v1／MQTT 中过期测量值为 `null`。
retained 消息不是实时保证，订阅端必须检查来源、新鲜度和在线状态。

## 本地开发与验证

在项目根目录创建开发环境，依赖只安装到电脑，不安装到设备：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python tools/check_micropython.py
node --check firmware/app_a/static/app.js
```

最后一项需要本机安装 Node.js；交叉编译检查不等同于硬件运行验证。

预览 Dashboard（固定模拟数据，不连接设备、不支持真实 MQTT 配置操作）：

```sh
python tools/preview_dashboard.py
```

打开 [本机预览](http://127.0.0.1:8765/)。响应式检查入口为
`/_preview/mobile`（402px）与 `/_preview/narrow`（300px）。
此 HTTP 服务仅监听电脑回环地址，与设备的 HTTPS 服务不同。

## 设备访问与部署

2026-10-12 验收环境的设备入口为 [https://192.168.2.3/](https://192.168.2.3/)，
本机 Mosquitto Broker 为 `192.168.2.1:8883`。这些是该次部署地址，不是固件默认地址；
换网或 DHCP 变化后需要重新核对，证书 SAN 也必须匹配。

现有部署的 CA、令牌、Broker 账号和运维配置保存在本机受限目录
`backups/services-20261011T171150Z/`，**不随仓库分发**。
浏览器访问前需由用户核对并信任公开 `ca.pem`；也可仅为 curl 指定 CA，不修改系统全局信任：

```sh
UNIFAN_PROVISION="$PWD/backups/services-20261011T171150Z"
curl --cacert "$UNIFAN_PROVISION/ca.pem" https://192.168.2.3/api/v1/info
curl --config "$UNIFAN_PROVISION/read.curl" https://192.168.2.3/api/v1/settings/mqtt
```

新克隆仓库不会包含上述目录，需按 [服务说明](docs/services.md) 准备独立身份与 Broker。
不得上传私钥、密码、令牌、包含认证头的 curl 配置或设备备份。
本机 Broker 未配置开机自启动，依赖电脑供电与网络；设备断电后可能需要受认证 UTC 校时，
否则 MQTT 严格证书验证可能失败。

正式业务使用 `.mpy` 与打包页面，不能只把源码目录复制到现有设备：

```sh
python tools/build_services.py --output dist/services-0.2.2
```

输出目录须不存在；该命令只构建，不写设备、不发布 GitHub Release。
USB 部署使用 `tools/deploy_services.py`，先完整备份与校验，再更新非活动 A/B 槽。
命令、凭据准备、Broker 管理和回退步骤见 [服务说明](docs/services.md)；
首次硬件准备另见 [W5500 有线联网](docs/ethernet.md) 和 [OTA 基础说明](docs/ota.md)。
现有设备不要重复执行首次安装工具。

文件级 OTA 基础设施只更新业务代码和静态页面，不更新 MicroPython 固件、固定启动器、公共依赖或设备配置。
当前应用未挂载网络 OTA 检查／安装／重启路由；暂停采样、MQTT 和普通 REST 的完整 OTA 维护流程仍待接入。

## 实机验证摘要

0.2.2 验收结果（2026-10-12）：

- 103 项主机测试通过，24 个设备 Python 文件通过交叉编译，JavaScript 语法检查通过。
- 正确 CA／IP 验证通过；未知 CA、错误主机名、未授权写入、跨源写入和 TLS 降级均被拒绝。
- MQTT 发布、ACL、主动离线、Broker 故障隔离及重连恢复通过。
- 620 秒真实周期测试中，相邻常规发布间隔为 600.838 秒；40/40 次 HTTPS 查询成功，完整响应 P95 约 972ms。

这仅验证一个入站 HTTPS＋一个出站 MQTT TLS 的受限资源模型，不代表高并发、长期稳定性或真实风扇安全性验收。
内存数据、备份索引与限制见 [完整部署记录](docs/deployment-services-2026-10-12.md)。

## 仓库结构

```text
firmware/                 设备源码与固定运行库；正式业务经构建后部署
  main.py                 固定启动入口
  network_config.py       固定 W5500 管脚和 IP 配置（逐项中文说明）
  lib/unifan_ota/          固定 OTA 运行库
  app_a/                  A/B 业务源码（有线网络、模拟硬件、采样与服务）
    app.py                后台任务生命周期、启动确认与健康监督
    device_state.py       Web/REST/MQTT 共用身份、状态与时间语义
    webapp.py             只读 Dashboard 路由与 `/api/status`
    https_service.py      唯一 HTTPS 监听器、单连接预算和有界请求解析
    rest_api.py           REST v1 状态、诊断、MQTT 设置和可信校时
    auth.py               read/admin 令牌、Origin 检查与限流
    mqtt_config.py        NVS 配置、校验、ETag 与密码脱敏
    mqtt_service.py       MQTT TLS 发布、在线遗嘱、周期与重连
    mqtt_transport.py     有界 MQTT 3.1.1 发布协议与 TLS 传输
    static/               无框架、无 CDN 的 HTML/CSS/JS 与本地字体
  certs/                  GitHub TLS 根证书，由准备工具生成
tools/build_release.py    在电脑上生成发布附件
tools/build_services.py   交叉编译业务与打包单请求管理页面
tools/deploy_services.py  备份后通过 USB 安装非活动 A/B 槽
tools/verify_services.py  HTTPS/MQTT/REST 实机验收与十分钟周期验证
tools/setup_local_services.py  生成独立 CA、证书、令牌和 Broker 配置
tools/local_broker.py     本机测试 Broker 启停与状态
tools/prepare_ca.py       从电脑信任库导出 GitHub 所需根证书
tools/usb_install.py      首次 USB 部署、文件哈希验证和 UTC 校时
tools/device_probe.py     设备端 W5500/DHCP/HTTPS 诊断（通过 mpremote run 执行）
tests/                   主机端故障恢复、传输和接口测试
docs/                    使用说明、规划与实机验收记录
backups/                 本机敏感凭据及部署备份（Git 忽略，不分发）
```

## 文档导航

- [HTTPS / REST / MQTT 服务说明](docs/services.md)：当前实现的 API、安全、部署、运维与回退依据。
- [2026-10-12 服务部署记录](docs/deployment-services-2026-10-12.md)：0.2.2 设备状态与实测结果。
- [W5500 有线联网](docs/ethernet.md)、[模拟硬件接口](docs/simulation.md)、[OTA 基础说明](docs/ota.md)。
- [MQTT 规划](docs/mqtt-plan.md)、[REST 规划](docs/rest-plan.md)、[OTA 维护模式规划](docs/ota-maintenance.md)：包含尚未实现的目标，以服务说明为准。
- [HTTPS 原型](docs/https-prototype.md)、[HTTPS 原型测试记录](docs/https-test-2026-10-12.md)：资源边界探索，不是当前业务的并发承诺。
- [2026-10-08 首次部署记录](docs/deployment-2026-10-08.md)：初始烧录与网络验证。
