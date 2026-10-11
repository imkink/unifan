# HTTPS、REST 与 MQTT 使用和运维说明

适用：0.2.x 业务版本，StampS3 / MicroPython 1.29.0 / W5500。2026-10-12。
硬件输入仍为模拟值；本次不启用真实温控、PWM 输出或 RPM 测速。

## 1. 已实现范围与运行模型

- 每 60 秒采样，启动立即采样一次；GET 查询只读取共享快照。
- 一个 Microdot 应用、一个 TCP 443 HTTPS 监听器，共用 Web 与 REST 路由。
- 最多一个已接纳的入站管理连接；另一个独立的出站 MQTT TLS 长连接。
- 页面、CSS、JS 和图标打包为单个 HTML，使用系统字体，按 1KiB 流式发送。
- 页面请求串行化，读取完正文后留出连接关闭间隔；普通状态请求结束后再等 5 秒轮询。
  设置弹窗打开时暂停状态轮询，避免竞争设备的唯一管理连接。
- MQTT 3.1.1、QoS 1、retain、遗嘱、10 秒 PING、30 秒 keepalive、指数退避重连。
- REST 首版提供信息、状态、健康、诊断、MQTT 配置和受认证 UTC 校时。
- 管理配置独立存放在 `unifan_mqtt` NVS 中，不随 A/B 业务更新清理。

未实现：历史写盘/查询、故障事件与数字状态码映射、HA Discovery、真实控制与告警、
网络 OTA 检查/安装/重启接口。`info.capabilities` 明确标记 false，相关路由不挂载。
历史周期仍规划为 600 秒、保留 7 天，但当前不生成历史文件，不要把元数据当成已在记录。
现有固定 OTA 库与 A/B 启动器保留；业务升级目前使用受控 USB 非活动槽部署。
MQTT 已提供暂停/恢复钩子，HTTPS 提供维护拒绝开关，**尚未形成可从网络触发的 OTA 维护流程**。

主要模块：

| 文件 | 职责 |
| --- | --- |
| `device_state.py` | 设备 ID、boot_id、单调 uptime、共享有效性/质量/时间模型 |
| `mqtt_config.py` | 配置校验、单 blob NVS 提交、revision/ETag 与密码脱敏 |
| `mqtt_transport.py` | 有界 MQTT 3.1.1 发布协议和严格 TLS，不支持订阅命令 |
| `mqtt_service.py` | 周期发布、初始化顺序、离线、退避及故障隔离 |
| `auth.py` | read/admin 令牌、浏览器 Origin 检查和速率限制 |
| `https_service.py` | 单连接接纳、TLS 包装、请求长度/期限、流式响应 |
| `rest_api.py` | REST v1 契约与鉴权路由，不访问 GPIO |
| `app.py` | 采样/网络健康监督与任务生命周期，独立于 Broker 可达性 |

## 2. 当前这台设备的访问方式

设备 ID：`48CA43B5FC00`。当前管理地址：`https://192.168.2.3/`。
当前本机 Broker：`192.168.2.1:8883`，仅 TLS，无 1883 明文监听。
地址来自本轮实测；网络切换、DHCP 变化后不能继续假定这些 IP 不变。
建议在实际长期使用的 UniFi 网络中为设备和 Broker 固定 DHCP 租约。

本机凭据目录（被 Git 忽略，目录权限 0700、凭据文件 0600）：

```text
backups/services-20261011T171150Z/
  ca.pem / ca.key              公开 CA / CA 私钥（私钥不上传设备）
  server.pem / server.key      设备 HTTPS 证书及私钥
  broker.pem / broker.key      本机 Broker 证书及私钥
  device-access.json          独立随机 read/admin 管理令牌
  device-mqtt.json            设备 Broker 账号和密码
  observer.json               只读订阅测试账号和密码
  admin.curl / read.curl      已引用 CA 和令牌的 curl 配置
  passwords                  Mosquitto 单向密码哈希
  acl / mosquitto.conf        精确 topic 权限和独立 Broker 配置
  broker.log / mosquitto.db   本机日志和 retained 状态持久化
```

CA 有效期 10 年，叶证书 365 天，ECDSA P-256，证书 SAN 包含本次真实 IP。
本次叶证书到期时间为 2027-10-12 01:11:50（Asia/Shanghai）。
本次公开 CA 的 SHA-256 指纹：

```text
55:2F:AC:80:3F:BC:1A:8A:B8:0E:11:31:00:38:0A:0F:C4:8F:B3:03:4C:DF:A2:FB:95:4C:07:BA:57:23:65:66
```

CA 没有被自动加入系统全局信任库。不要分享整个目录；只把 `ca.pem` 分发给需要访问的客户端。
`admin.curl` 也包含秘密，不能提交或粘贴到公开问题报告中。

命令示例均从项目根目录运行：

```sh
UNIFAN_PROVISION="$PWD/backups/services-20261011T171150Z"
curl --cacert "$UNIFAN_PROVISION/ca.pem" https://192.168.2.3/api/v1/info
curl --config "$UNIFAN_PROVISION/read.curl" https://192.168.2.3/api/v1/settings/mqtt
curl --config "$UNIFAN_PROVISION/admin.curl" https://192.168.2.3/api/v1/diagnostics
```

浏览器需手动信任这个专用 CA 才能无警告打开页面。macOS 可通过“钥匙串访问”导入 `ca.pem`，
核对来源/指纹后按自己的信任策略启用；不需要导入 CA 私钥。信任 CA 是用户的显式操作，
不是点击忽略证书警告。只使用上述 curl `--cacert` 不会扩大系统全局信任。
证书 IP 不符时应更新证书或恢复地址，不能关闭证书验证来绕过。

设置按钮打开 MQTT 弹窗。输入 `device-access.json` 中的 read 令牌可读取，admin 令牌可保存；
令牌仅存在当前弹窗和内存中，不写 localStorage，不放 URL，不使用 Cookie。
关闭弹窗清除输入凭据；语言偏好仍可单独存入 localStorage。
密码输入留空表示保留；勾选清除表示传 null。已启用配置不能清除必需密码而仍保持启用。
保存成功表示配置已持久化，不表示 Broker 已连通，应观察面板的 MQTT 阶段。

## 3. 认证与资源安全边界

所有服务端流量都经 HTTPS；80 端口不提供页面、不做携带凭据的明文重定向。
只公开 `/api/status`、`/api/v1/info`、`/api/v1/status`、`/api/v1/health` 的只读信息。
诊断和配置读取要求 read 或 admin，任何写入必须 admin。两种令牌各为独立 256 位随机秘密。

浏览器写入必须来自部署配置中的精确 Origin，不接受跨站/同站不同源写入；不开放 CORS。
外部脚本可以不带 Origin，但仍必须显式带 `Authorization: Bearer ...`。
没有 Cookie 或其他自动附带的管理认证，因此本版采用“显式 Bearer＋Origin”而非 Cookie 会话 CSRF 模型。
以后若改为 Cookie 登录，必须另外实现会话期限、撤销和 CSRF token，不能直接复用当前假设。

- 请求正文最多 1024 字节，单行最多 1024 字节，总请求行/头部最多 4096 字节、25 行。
- 拒绝重复头字段、Transfer-Encoding、负 Content-Length 和超限正文声明，不先完整读取再检查。
- 每连接从 TLS 到处理结束最多 12 秒，关闭等待最多 1 秒。
- 忙时在分配 TLS 对象之前直接关闭额外 TCP 连接，客户端得到连接错误；此时不能发送明文 429。
- 接纳间隔至少 250ms；API 全局每秒最多 5 次、管理写入至少间隔 1 秒，超限返回 429。
- TLS 包装失败只关闭连接，无 HTTP 明文降级；适配固定使用 MicroPython 1.29 `Stream.s`。
- 已接纳的慢客户端仍可能独占 12 秒，单连接和超时不能消除局域网 DoS。

建议 UniFi 管理 VLAN / ACL 仅允许可信管理主机到设备 TCP 443，设备到 Broker TCP 8883；
不要将设备端口映射到互联网。HTTPS 不替代授权，也不保护物理读取 Flash 的攻击：
设备证书私钥、NVS 密码目前没有硬件 Flash 加密保护。

## 4. REST 契约

成功：`{"schema":1,"data":{...},"meta":{...}}`。
失败：`{"schema":1,"error":{"code":"...","message":"...","field":null},"meta":{...}}`。
meta 包含设备 ID、boot_id、request_id、sample_seq、sample_age_ms、uptime_ms、sampled_at、time_synced。
协议解析阶段拒绝的畸形请求可能只有最小 error，不保证业务 meta；客户端首先检查 HTTP 状态。

| 方法 / 路径 | 权限 | 行为 |
| --- | --- | --- |
| `GET /api/status` | 公开 | 兼容旧页面字段，附加脱敏 mqtt 状态 |
| `GET /api/v1/info` | 公开 | 身份、版本、能力与周期 |
| `GET /api/v1/status` | 公开 | 同一采样的温湿度、六路 RPM、两组 PWM、网络和 MQTT |
| `GET /api/v1/health` | 公开 | 本地采样健康；Broker 离线不会单独导致 503 |
| `GET /api/v1/diagnostics` | read/admin | Python/native 空闲堆、连接计数、MQTT 状态 |
| `GET /api/v1/settings/mqtt` | read/admin | 脱敏配置、revision 和强 ETag |
| `PATCH /api/v1/settings/mqtt` | admin | 版本保护的局部修改、原子保存、后台应用 |
| `PUT /api/v1/system/time` | admin | 可信管理端提供 Unix UTC 秒，不写 NVS |

状态和配置响应均 no-store。模拟数值标记 `quality=simulated`，风扇 status 为 unknown；
未完成统一故障评估时 sys_code/active_faults 为 null，不伪造正常码。
REST v1/MQTT 在样本 ≥90 秒过期时输出 null 测量值和 stale 标志；旧 `/api/status` 保留原结构及 stale。

### MQTT 设置

可写字段仅限：

| 字段 | 约束 |
| --- | --- |
| enabled | 布尔值 |
| host | 固定 IPv4，避免同步 DNS 阻塞；不接受域名、回环、组播、0.x 地址 |
| port | 1–65535 的整数，布尔值不是合法端口；默认 8883 |
| tls | 必须 true；本正式首版拒绝明文模式 |
| server_name | 可空，默认用 host 验证证书；也可指定证书 DNS 名，实际连接仍用 host IP |
| username | 最长 64 个可打印无空格 ASCII 字符 |
| password | null 或最长 256 字节字符串；未提供保留，空字符串代表真正的空密码 |

启用时 host、username、非 null password 必须齐全。CA 固定为 `/security/mqtt-ca.pem`，
REST 不允许任意文件路径或上传证书私钥；切换 CA 需受控 USB 运维。
publish interval 固定 600 秒，topic 从设备 ID/模拟模式派生，不能通过 REST 任意改动。

GET 不返回 password，只返回 `password_configured`；字段值变更（包括隐藏密码）都会推进 revision。
先 GET 获得 ETag，例如 `"mqtt-1"`，再执行：

```sh
curl --config "$UNIFAN_PROVISION/admin.curl" \
  -X PATCH https://192.168.2.3/api/v1/settings/mqtt \
  -H 'Content-Type: application/json' -H 'If-Match: "mqtt-1"' \
  --data '{"enabled":false}'
```

示例会实际停用 MQTT，勿直接复制为无影响查询。重新启用前读取最新 ETag。
无 If-Match 返回 428，旧版本返回 412，校验失败 422，错误 JSON 400，非 JSON 写入 415，
无凭据 401，权限/Origin 不符 403，限流 429，存储失败 507，未实现接口 404。
保存相同内容不写 Flash、不增加 revision。提交失败不切换当前内存配置。
`apply_status=pending` 表示服务稍后应用；`applied` 仅表示该配置版本已采用，不保证外部连接永远在线。

## 5. MQTT 消息、确认与故障行为

设备测试账号：`unifan-48ca43b5fc00`，只允许写 `unifan/sim/48CA43B5FC00/#`。
观察账号：`unifan-observer`，只允许读同一路径。密码为随机值，见本机对应凭据文件。
切换为真实硬件模式前需同步修改 Broker ACL，否则不能向真实数据路径发布。

| Topic 后缀 | 内容 | QoS / Retain | 时机 |
| --- | --- | --- | --- |
| info | 产品、版本、能力、分组、周期和来源 | 1 / true | 每次成功连接 |
| state | 共享状态＋设备/启动/采样/发布序号、样本年龄与时间 | 1 / true | 连接初始化、每 600 秒、stale 转换 |
| availability | online / offline | 1 / true | 初始化确认后、主动停用、Broker 遗嘱 |

Client ID 固定 `unifan-48CA43B5FC00`，clean_session=true。不要用同一 Client ID 启动桌面工具。
初始化必须依次完成 info ACK、state ACK、online ACK 才标记 online；online 不是硬件健康结论。
每次最多一条 QoS1 在途消息，不堆积断线期间的全部分钟数据。连接丢失后发送最新快照，
不是逐条重放；同一连接 ACK 不匹配/超时会断开并重连。QoS1 不保证订阅端 exactly-once，
消费者用 `(device_id,boot_id,publish_seq)` 去重，并检查来源/新鲜度。

常规采样变化不会提前发布。重连即时发布后重新计算 600 秒期限；stale 转换发布不重置常规期限。
任务每秒检查一次调度，另加网络 ACK 时间，因此周期允许调度误差，不承诺整分墙钟对齐。
消费者应把“连续约 30 分钟无新 state”视为不可用；设备本地 90 秒阈值针对采样，不是消费者刷新周期。
retained 是最后值，不是新值；时钟未知的 retained state 只能作为未确认历史，等待实时消息确认。

网络/握手/ACK 单次等待最多 8 秒；这是可取消异步等待的期限，不是任意底层同步 C 调用的绝对保证。
采用数值 IP 避免 DNS 阻塞，固定 W5500 与 MicroPython 版本并实机验证。
断线退避约 1、2、4、8…60 秒并加抖动；认证拒绝进入慢速重试。
Broker 不可达时状态 backoff，核心采样与管理服务继续；不打印可能含秘密的异常文本。
正常关闭尽力确认 retained offline 后 DISCONNECT；异常关闭不发送 DISCONNECT，让 Broker 遗嘱生效。
Broker 完全停机时无法即时对外发布遗嘱，重启/重连后需重新确认在线状态。
改变 Broker 地址不会自动删除旧 Broker 的 retained info/state，旧观察端应结合 offline 和新鲜度判断。

## 6. 时间与断电恢复

周期、样本年龄和重连均使用单调时钟，uptime 按差值累计处理 ticks 回绕。
设备 RTC 由部署工具设为 UTC，但固件不因年份看似正确就把 `time_synced` 设为 true。
通过管理端 HTTPS 校时后，当前启动内才提供 sampled_at / published_at：

```sh
python tools/sync_device_time.py --provision "$UNIFAN_PROVISION"
```

校时接口只接受 2026–2099 年范围内的整数 Unix 秒。管理电脑必须有正确时间。
重启会清除“已可信校时”的内存标志；完全断电可能丢失 RTC，导致 MQTT 严格证书验证失败。
此时 HTTPS 服务端仍可接受管理客户端校验证书，通过上述命令校时后 MQTT 会在退避后重试。
没有自动 NTP、自动降级证书验证、长期保存“时间已同步”假标志；长期无人值守前应增加可信校时机制。

## 7. 本机 Broker 运维

安装的是 Homebrew Mosquitto 2.1.2，使用项目自己的配置，不覆盖 Homebrew 默认 mosquitto.conf。
本轮没有安装开机自启动/LaunchAgent，没有改防火墙、UniFi 配置或系统 CA 信任。
Mac 必须保持运行且 `192.168.2.1` 网络地址可用；休眠、关机或换网会让设备进入重连等待。

```sh
python tools/local_broker.py status --provision "$UNIFAN_PROVISION"
python tools/local_broker.py start --provision "$UNIFAN_PROVISION"
python tools/watch_mqtt.py --provision "$UNIFAN_PROVISION" --seconds 30
python tools/check_broker_acl.py --provision "$UNIFAN_PROVISION"
python tools/local_broker.py stop --provision "$UNIFAN_PROVISION"
```

stop 是真实停机；管理工具先核对 PID 确实对应本目录配置，不操作其他 Broker。
密码文件使用 Mosquitto 哈希，CA/证书验证替代不了账号认证和 topic ACL。
生成工具 `setup_local_services.py` 只适合新的身份环境，不应反复运行来“修复”连接问题。
设备证书/令牌、Broker 密码/ACL 和配置必须作为同一运维记录管理。

## 8. 构建、部署与回退

先安装 `requirements-dev.txt`。验证：

```sh
python -m unittest discover -s tests -v
python tools/check_micropython.py
node --check firmware/app_a/static/app.js
```

正式业务需先交叉编译并生成单文件页面。USB 部署工具会自动执行这一步：

```sh
python tools/deploy_services.py \
  --port /dev/cu.usbmodem101 --device-id 48CA43B5FC00 --device-ip 192.168.2.3 \
  --provision "$UNIFAN_PROVISION" --version 0.2.3 --keep-mqtt
```

上例 0.2.3 仅演示下一版本，运行前确认当前设备/版本；不能用来反复覆盖同版本。
工具在任何覆盖之前备份全部原文件和 OTA/MQTT 状态并校验哈希，仅清理非活动槽，
上传 `.mpy`、轻量 app.py 和静态资源，逐文件复验；共享固定库与网络配置保持原哈希。
最后写完成标记、提交候选、UTC 校时并复位，HTTPS info 成功后才报告激活。
应用需在证书、鉴权、配置和监听器就绪且模拟采样初始化后确认候选；不等待外部 Broker。
硬件看门狗维持 30 秒，健康监督维持每秒；USB 维护期间单独进行有界喂狗，正式业务不沿用此逻辑。

既有 `/security` 内容不一致时工具拒绝静默替换；`--keep-mqtt` 保留已有 NVS。
首次安装不要给 `--keep-mqtt`，工具才从本机受限文件导入 Broker 配置。
证书私钥不放入业务槽或版本包；不要对现有设备运行首次安装 `usb_install.py`。

若未来要生成 Release 附件，先构建已编译目录，再交给原发布工具，不直接打包未生成页面的源码：

```sh
python tools/build_services.py --output dist/services-0.2.3
python tools/build_release.py --source dist/services-0.2.3 --version 0.2.3 --output dist/release-0.2.3
```

这些命令只构建，不发布或执行网络 OTA。`.mpy` 与 MicroPython 1.29 运行时绑定，升级底层固件必须重新验证。
启动前失败可由 A/B 启动器回退；启动已确认后的回退需受控 USB 操作，先备份当前状态，
检查目标槽完成标记和版本，再用固定库设置新的确认目标，不能随意删除 main.py 或清空 NVS。
0.1.2 的完整原文件与 OTA 状态保存在 `backups/deploy-20261011T171735Z/`，
后续每次部署均有独立备份；备份也可能包含秘密，不可整体上传。

## 9. 证书与令牌轮换

当前不提供匿名初始化、网络证书上传或令牌自助重置。
叶证书到期前，在受控电脑使用既有 CA 签发新证书（保持正确 SAN），备份设备后通过 USB 更新
`/security/server.pem` 与 `/security/server.key` 并复位验证；同时更换 Broker 证书时需重启本机 Broker。
改变 Broker CA 必须同步更新设备 `/security/mqtt-ca.pem`；改变 HTTPS CA 必须分发新的公开 CA。
令牌轮换需更新设备 `/security/access.json` 和本机对应 curl/凭据文件，并重新加载应用。
这些是需要显式执行的运维操作，当前部署工具会拒绝偷偷覆盖不同安全身份。
丢失 CA 私钥不能从设备恢复；安全备份 CA 私钥，勿只备份公开 ca.pem。

## 10. 实机验收

```sh
python tools/verify_services.py --provision "$UNIFAN_PROVISION" --soak-seconds 620 --restart-broker
```

该测试会真实停用/恢复 MQTT，并重启本任务的本机 Broker，运行时请不要同时进行配置写入。
覆盖证书拒绝、匿名/只读/跨源写入拒绝、ETag 冲突、TLS 降级拒绝、单连接、retained 消息、
主动 offline、Broker 失联时本地健康、重连，以及不加速的真实十分钟发布与分钟采样。
结果保存在凭据目录的 `verification-*.json`，只记录脱敏状态；错误测试和全部成功结果须区分。
更长时间稳定性、断电过程、恶意流量、外部 Broker 黑洞和真实硬件控制仍需独立验收。
具体本轮结果、备份目录与中间修正见 [2026-10-12 服务部署记录](deployment-services-2026-10-12.md)。
