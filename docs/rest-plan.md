# REST 服务方案（讨论稿）

日期：2026-10-11。本文件保留完整接口规划。
2026-10-12 已实现首版 info/status/health/diagnostics、MQTT 设置 GET/PATCH 和受认证校时。
实际部署为单连接 HTTPS、显式 Bearer 认证、1KiB 请求正文；历史、控制与 OTA 管理路由未挂载。
具体契约、配置字段与限制以 [当前服务说明](services.md) 为准，下文为原始规划，不是全部实现承诺。

## 1. 定位与现状

REST 提供按需查询、配置管理和明确的管理操作；MQTT 提供周期状态推送和事件通知。
内置 Dashboard 与外部客户端通过同一组业务服务读写，避免 Web、MQTT、REST 各自维护硬件状态。

- 已实现：Microdot HTTP 服务、只读 `GET /api/status`、静态管理页面。
- 已有但尚未挂载：鉴权 OTA 路由 `/api/ota/*`，需要接入认证与复位前处理。
- 尚未实现：真实传感器/风扇驱动、控制算法、历史记录、用户设置持久化、MQTT 服务和统一故障服务。
- 新接口基路径为 `/api/v1`；旧 `/api/status` 保持现有字段兼容，页面迁移后再考虑弃用。
- 所有 API 路由注册在静态文件通配路由之前，防止被当作资源文件处理。

全局周期：启动立即采样一次，然后每 60 秒采样；常规 MQTT 发布和历史记录每 600 秒一次。
历史保留 7 天，正常周期最多约 1008 条。REST GET 读取内存或已保存历史，不额外采样、
不推动发布、不写 Flash，也不等待下一次分钟采样。页面可继续每 2 秒读取缓存以显示网络状态。

## 2. 模块与共享服务

| 模块 | 职责 |
| --- | --- |
| `rest_api.py` | 路由、参数解析、HTTP 状态码和响应；不直接访问 GPIO |
| `api_models.py` | JSON 序列化、字段校验、错误格式；复用 MQTT 的共享领域字段定义 |
| `auth.py` | 浏览器会话/外部令牌校验、权限、Origin/CSRF 校验 |
| `settings_service.py` | 设置校验、并发版本检查、原子持久化、运行配置应用 |
| `history_service.py` | 每 10 分钟记录、7 天保留、游标分页和清理；与 MQTT 是否联网无关 |
| `operation_service.py` | 有界后台操作、忙状态、进度和互斥；接入既有 OTA 能力 |
| 既有及拟新增共享服务 | 采样快照、网卡状态、MQTT 状态、设备身份、控制状态和故障集合 |

前三项构成 HTTP 适配层；后面几项是整个应用共用的业务能力，不能为了 REST 再实现一套采样器。
已有 `webapp.py` 负责页面及旧接口适配；共享身份与状态构造应移出 Web 私有函数。

## 3. 资源与接口

权限分为“只读监测”和“管理”。为兼容现有局域网页面，info/status/health 可允许匿名读取；
历史、设置、诊断、操作和 OTA 要求认证。部署策略可进一步要求所有 API 鉴权。

| 方法与路径 | 用途 | 阶段 |
| --- | --- | --- |
| `GET /api/v1/info` | 设备身份、版本、能力、已确定周期与单位 | 首版 |
| `GET /api/v1/status` | 一次返回温湿度、6 路 RPM、2 组 PWM、网络、MQTT 和健康状态 | 首版 |
| `GET /api/v1/health` | 本地核心任务/采样是否可用；供监控探测 | 首版 |
| `GET /api/v1/diagnostics` | 内存、运行时间、重连次数、脱敏错误等 | 首版，可按实际能力提供 |
| `GET /api/v1/history` | 按来源、时间范围分页读取 10 分钟历史点 | 历史服务完成后 |
| `GET /api/v1/settings/mqtt` | 脱敏 MQTT 配置和配置版本 | 设置与 MQTT 服务完成后 |
| `PATCH /api/v1/settings/mqtt` | 局部修改 MQTT 配置 | 同上 |
| `GET /api/v1/settings/alarms` | 当前温度告警阈值及恢复条件 | 告警策略完成后 |
| `PUT /api/v1/settings/alarms` | 完整替换告警配置 | 同上 |
| `GET /api/v1/settings/fan-groups/{id}` | 读取组 1 或 2 的曲线及输出限制 | 控制策略完成后 |
| `PUT /api/v1/settings/fan-groups/{id}` | 完整替换该组控制配置 | 同上 |
| `DELETE /api/v1/history?source=hardware` | 清除明确来源的历史，异步操作 | 历史服务及管理认证完成后 |
| `POST /api/v1/system/reboot` | 普通维护重启，执行复位前钩子 | 管理阶段 |
| `GET /api/v1/operations/{id}` | 查询清理/维护等后台操作状态 | 管理阶段 |

首版不拆分六个风扇轮询接口，单次 status 已能保证它们来自同一个完整采样。
不提供 `GET` 改配置、强制即时采样、任意 GPIO 写入、任意下载 URL 或执行 Python 的接口。
远程手动调速不包含在本轮；以后若加入，只能按两组控制，并先定义手动超时恢复与故障保护优先级。

未实现能力在 info 中明确标记，相关规划路由不挂载（404），不能返回模拟成功。
接口已实现但当前硬件/控制模式不支持时返回 409 `capability_unavailable`。

## 4. 通用响应与当前状态

使用 UTF-8 JSON，数值采用 Celsius、相对湿度百分比、PWM 百分比和整数 RPM。
新 API 成功响应采用 `schema`、`data`、`meta`；错误响应采用 `schema`、`error`、`meta`。
`meta` 提供 `request_id`、`device_id`、`boot_id`，查询快照时再加采样时间、年龄和序号。
API 版本与消息 schema 独立于固件版本；新增可选字段允许兼容演进，重命名/改单位需新版本。

`GET /api/v1/info` 返回产品/硬件型号、固件版本、完整设备 ID、风扇与 PWM 分组，
以及 `sample_interval_seconds: 60`、`publish_interval_seconds: 600`、
`history_interval_seconds: 600`、`history_retention_days: 7`。
`capabilities` 区分功能已实现、已启用、硬件就绪；例如 MQTT planned 与 disabled 是不同状态。

status 的 data 与 MQTT state 共用以下业务字段语义：

- `environment`：`temperature_c`、`humidity_percent`、`quality`。
- `groups`：2 项，包含 `id`、`pwm_percent`、控制模式；未实现策略时为 `unconfigured`。
- `fans`：6 项，包含 `id`、`group`、`rpm`、`quality`、`status`；同组 PWM 不伪装为独立调速。
- `source`、`simulated`、`hardware_ready`、`control_policy_ready`、`stale`：来源及有效性。
- `health`、`sys_code`、`active_faults`：统一健康定义；未知码/未实现故障评估为 `null`。
- `network`：W5500 的阶段、是否获取地址和 IP；`mqtt`：能力/启用/连接阶段与脱敏错误。

REST meta 使用 `sample_seq`、`sampled_at`、`sample_age_ms`、`time_synced` 和 `uptime_ms`；
不生成 MQTT publish_seq。没有可信 UTC 时 sampled_at 为 null，单调样本年龄仍可用。
同一采样轮次的 Web、REST、MQTT 温湿度/转速及质量必须一致；网络状态可以独立更新。

本地样本年龄 ≥90 秒时标记 stale，当前测量值输出 null；无效数据不转换为 0。
模拟数据保留数值但 quality=simulated、hardware_ready=false，不能返回真实风扇正常。
即使数据过期或有风扇故障，status 仍返回 200 并如实描述状态；“读到了故障状态”不是 HTTP 故障。
核心服务尚未初始化、根本无法构造状态时才返回 503。所有运行状态响应使用 `Cache-Control: no-store`，
避免客户端因 304 复用而错过样本年龄或故障变化。

health 是本地软件可用性探测：核心任务存活、采样未过期时返回 200；核心不可用返回 503。
Broker 不可达不单独使它返回 503。响应明确保留 source/hardware_ready；模拟健康不等于真实硬件健康。
消费者要监控设备业务故障时，应同时读取 status 的 health/active_faults。

## 5. 历史查询与记录

示例：`GET /api/v1/history?source=hardware&from=2026-10-10T00:00:00Z&to=2026-10-11T00:00:00Z&limit=16`。

- `source` 限定为 hardware / simulated，默认 hardware；两者独立存储、不得混合。
- `from` 包含、`to` 不包含，均为 UTC；最大查询范围 7 天，参数不合法返回 400。
- 每页默认 16 条、最大 64 条；同时限制编码响应不超过 16 KiB，因此实际一页可能更少。
- 返回 `items`、`next_cursor`、`interval_seconds: 600`、`retention_days: 7`；无数据为 200 + 空数组。
- 游标绑定来源、过滤条件、存储 generation、最后记录 ID 和第一页的上界 ID，分页按记录 ID 升序。
  遍历时新记录不进入已有分页；记录 ID 不因 NTP 校时倒退，清理后旧游标返回 410。
- 后续请求保持同一过滤条件；游标是经过严格校验的不透明值，不能解释成文件路径。
- 每条记录包含 record_id、boot_id、sample_seq、sampled_at、recorded_at、样本年龄、来源/质量和业务值。
  每 10 分钟记录最新样本，不是该窗口平均值；无效值记录为 null，不伪造缺失采样。
- UTC 未确认时仍可记录单调运行时间及 null 时间。无时间过滤的查询可读这些记录；带 UTC 过滤时
  无法定位的记录不进入结果，并在响应中说明 `time_unknown_records_excluded=true`。
- 按可信年龄删除超过 7 天记录，同时设置独立容量上限（1008 个正常周期点）。无可靠时间时只能
  保证容量上限，不能声称精确覆盖最近 7 个日历日；断电停机也不会补造历史。

读取必须逐页/分块，不能把全部历史载入内存后再切片；Flash 写入/清理和 HTTP 读取采用短临界区，
不得在持有全局锁时等待网络发送。内存快照读取不依赖历史存储锁。

## 6. 配置修改与持久化

每个 settings GET 返回强 ETag 和脱敏配置。PATCH/PUT 必须带 `If-Match`：
缺少返回 428，版本不符返回 412；避免两个页面读取同一旧值后相互覆盖。
ETag 对应完整存储版本（包括隐藏密钥的变更），不能只对脱敏显示值求哈希。
校验版本、写入 NVS 和推进 revision 在同一受保护事务内进行，不能先检查再无锁写入。

MQTT 更新示例（拟定）：

```http
PATCH /api/v1/settings/mqtt
Authorization: Bearer <admin-token>
If-Match: "mqtt-7"
Content-Type: application/json

{"enabled":true,"host":"192.168.1.10","port":1883,"tls":false}
```

配置校验先于持久化：类型、范围、必填字段、topic 格式、证书引用等任一不合法，整次失败。
拒绝未知字段，布尔值不当作整数。默认发布周期为已确定的 600 秒；首版不通过 REST 暴露采样/记录
周期的随意调整，防止三种接口出现不同口径。TLS 模式不隐式改变或降级。
MQTT 密码在 GET 中仅表示为 `password_configured`；PATCH 未带 password 表示保留，
传新字符串表示替换，null 表示明确清除，空字符串不等于“保留旧值”。

设置使用独立 NVS 命名空间/版本化 blob，随业务 A/B 更新保留。保存语义为：
校验及版本检查 → 原子持久化 → 更新内存中的期望配置 → 通知对应服务应用。
成功后返回 200、新 ETag 和 `apply_status`（applied/pending/failed），明确“保存成功”和
“Broker 已连通/硬件已执行”不是同一件事；磁盘/NVS 提交失败返回 500 或空间不足时 507，
运行配置及 revision 不变。保存相同内容不重复写 Flash 或推进 revision。
启动时重新从已提交配置恢复，不用 HTTP 请求是否收到响应判断保存是否成功。

告警设置和曲线设置需要其共享业务服务先确定语义。曲线目标到底是 PWM 还是 RPM 当前尚未决定，
因此此阶段不固定可执行曲线 JSON；正式开放时必须显式 `target_unit`，校验温度点严格递增、
数值有限、目标范围/最低输出限制符合真实硬件策略。API 不把需求中的“最低转速 20%”擅自等同于 20% PWM。
真实硬件未就绪时拒绝真实控制配置的生效操作，模拟测试配置必须放在独立命名空间。

## 7. 后台操作与已有 OTA

清理历史和维护重启采用后台操作：鉴权/参数/忙检查通过后，返回 202、operation_id 和
`Location: /api/v1/operations/<id>`。状态包括 queued/running/succeeded/failed，带进度或阶段及脱敏错误。
初版只允许一个冲突性管理操作运行；清理、OTA 写入和重启应共用互斥规则，忙时返回 409。
查询状态不能等待操作完成。正常管理操作不得阻塞采样；OTA 使用下述明确的计划内暂停。

OTA 检查/安装统一进入 [维护模式](ota-maintenance.md)：暂停采样、MQTT、普通 REST 及历史写入，
保留轻量维护页面、必要认证入口、鉴权 OTA status 与有条件的 OTA reboot。
Web 与 REST 共用监听器，不停止整个 HTTP server；普通接口（包括旧 status 和 v1 health）返回
503 `maintenance`、no-store 和 Retry-After，重复 OTA 操作返回 409。
维护状态通过 OTA status 的 maintenance 对象查询，Dashboard 停止普通轮询并切换进度展示。
结束、失败及下载完成等待重启时恢复新鲜采样后重新开放普通接口，不能永久停留在无采样状态。

删除历史必须显式指定 source（hardware/simulated），不接受缺省来源或 all；使用历史 generation
作为清理前置条件：历史 GET 返回该来源的 generation ETag，DELETE 通过 If-Match 提交，
缺失返回 428，不匹配返回 412，防止旧清理请求删除刚开始的新记录集。日常追加不推进 generation。
清理过程中暂停该来源写入，
完成后递增 generation、使旧游标失效并从下一周期继续记录；不能删除 NVS 配置或 OTA 文件。

重启前完成必要存储收尾、尽力发布 MQTT offline，并调用已经验证的风扇安全状态钩子；
不能从 HTTP 处理函数直接复位导致响应丢失。普通重启不要求有 OTA 候选；OTA 重启仍要求候选已就绪。
重启后内存 operation 不再可查，客户端通过 boot_id 变化确认重启，不能假设原 job 会跨重启持久化。
若支持 Idempotency-Key，限定在本次 boot_id 的有界缓存内；不能宣称跨断电 exactly-once。

既有 OTA 继续使用其已定义接口，不在首版强行迁移固定 `/lib` 路由：

| 已有路径（尚未挂载） | 接入要求 |
| --- | --- |
| `GET /api/ota/status` | 认证后读取已有进度 |
| `POST /api/ota/check` | 检查发布版本；现有实现等待 check 结果，需验证对事件循环的影响 |
| `POST /api/ota/install` | 仅接受选定版本，202 后轮询 OTA status，不能传任意 URL |
| `POST /api/ota/reboot` | 有待运行候选时复位，执行同一安全钩子 |

上述旧接口保留现有响应格式；新 v1 适配或未来异步 check 应通过共享 Updater/操作协调器实现，
不得建两个独立 Updater 或两套 busy 状态互相绕过。现有固定库改动需独立部署，普通业务 OTA 不更新它。

## 8. 认证、错误与资源边界

外部自动化使用 Authorization Bearer 令牌，至少区分 read/admin；令牌首次由受控初始化生成，
没有通用默认密码，不能放在 URL 中。浏览器管理使用经过认证的会话，写请求检查 Origin 与 CSRF，
同源策略默认不开跨域写入；仅依赖 CORS 不是鉴权。凭据不出现在状态、日志或导出配置中。
当前设备为 HTTP，不把明文传输 token 称为安全加密；远程管理使用受信的 HTTPS 反向代理或 VPN，
设备直连写接口只在明确受控的管理网络启用。设备端 HTTPS 需额外验证内存及并发预算后再选定。

| 状态码 | 用途 |
| --- | --- |
| 200 / 202 | 查询或保存成功 / 后台操作已接受 |
| 400 / 415 / 422 | JSON或查询参数错误 / 非 JSON 媒体类型 / 配置值不合法 |
| 401 / 403 | 未认证 / 权限不足或浏览器请求校验失败 |
| 404 / 405 / 410 | 路由或对象不存在 / 方法不允许 / 历史游标失效 |
| 409 / 412 / 428 | 操作冲突或能力未就绪 / 配置版本冲突 / 缺少前置条件 |
| 413 / 429 | 请求体过大 / 超出请求频率或并发预算 |
| 500 / 503 / 507 | 内部失败 / 核心服务不可用或计划维护 / 持久化空间不足 |

错误体例如：`{"schema":1,"error":{"code":"revision_conflict","message":"Configuration changed","field":null},"meta":{"request_id":"..."}}`。
客户端依赖稳定 error.code，界面自行本地化；错误不返回堆栈、秘密或任意文件路径。

建议初始预算：请求体最多 4 KiB、历史响应最多 16 KiB、历史查询最多并发 1 个，
全设备 API 每秒最多 5 个请求（短时突发 10 个）；管理写入每秒最多 1 个。
超限返回 429 及 Retry-After，实机验证后调整。超过正文上限需在框架读取阶段拒绝，不能读完再检查。
限制与等待使用单调时钟，设置独立连接/读取超时；正常分页/JSON 与 MQTT TLS 并发峰值、
OTA 维护进入时的请求排空/资源释放和恢复峰值分别实测。

## 9. 实施顺序与验收

1. 抽出共享设备身份和状态模型，保持旧页面接口兼容，先实现 info/status/health/diagnostics。
2. 实现历史记录服务与分页读取，固定 60 秒采样、600 秒记录及 7 天保留。
3. 接入认证、配置存储和 ETag 并发控制，再开放 MQTT 设置读写。
4. 控制/告警语义确定后开放对应设置；最后接入清理、重启及既有 OTA 管理。

验收应覆盖：GET 不采样/不写 Flash、模拟与过期标志、分钟等待不误报、status 与 MQTT 口径一致、
匿名写入拒绝、密钥脱敏、并发旧版本写入拒绝、NVS 提交失败不部分生效、分页内存限制及游标失效、
无 UTC 历史语义、读写/清理竞争、操作忙冲突、OTA 不绕过鉴权，以及网络管理负载不饿死核心任务。
实现阶段还需对真实 Microdot、实际 StampS3/W5500 和掉电保存进行验证；本文为接口规划，不替代实机验收。
