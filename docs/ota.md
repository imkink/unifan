# StampS3 OTA

## 范围与固件要求

仓库固定为公开仓库 `imkink/unifan`。清单地址：

```text
https://github.com/imkink/unifan/releases/latest/download/manifest.json
```

官方 ESP32-S3 MicroPython，要求支持 W5500 的 `network.LAN`/`network.PHY_W5500`、
`network.hostname`、DHCP 模式下的 `LAN.ipconfig`，以及 `asyncio`、`esp32.NVS`、`machine.WDT`、
`ssl.SSLContext`、`CERT_REQUIRED`、`load_verify_locations(cafile=...)`、
`asyncio.open_connection(..., ssl=context, server_hostname=host)`。
建议使用近期稳定版；目前尚未对用户设备的具体版本做实机验证。
HTTPS 不支持或证书不匹配时会报错，不会降低验证强度。

先通过串口确认 `sys.implementation`、`os.uname()`、`os.statvfs('/')`。
推荐使用 LittleFS；必须在实际固件上验证断电恢复，不要假设默认文件系统一定是 LittleFS。
这里的 A/B 是同一文件系统内的两个目录，不是 ESP32 固件 OTA 分区。

## 设备布局

```text
/main.py                   固定启动器
/network_config.py         固定的 W5500 接线和地址配置
/lib/unifan_ota/            固定 OTA 库
/lib/microdot.py            后续安装的固定 Microdot 依赖
/certs/github-roots.pem     固定信任根
/app_a/app.py               async def main(boot)
/app_a/ethernet.py          MicroPython 有线网络管理，随业务版本升级
/app_a/static/              与业务版本配套的网页
/app_b/                    下一版本（自动创建）
/data/                     历史数据（OTA 不操作）
NVS namespace unifan_ota    单个 state blob：已确认版本、候选版本、启动次数
```

OTA 仅能清理和写入非活动 app 目录。配置和历史数据应放在独立 NVS 命名空间或 `/data`，
不可放进 app 目录。数据格式的更改必须兼容旧版，不能在试运行时做不可逆的数据迁移。
回退成功后，旧文件格式也要能读，才算业务恢复成功。

## 首次部署

1. 电脑上运行 `python3 tools/prepare_ca.py`。它使用电脑已有信任库验证 GitHub 及其
   下载域名，导出所需根证书，并用仅包含这些根的上下文再次验证。不要从未验证的连接
   提取服务器证书当作可信根。证书生成失败时，先修复电脑的 Python CA 配置。
   生成文件被 Git 忽略；设备首次安装必须包含它。GitHub 换根后可能需要通过 USB 更新。
2. 首次安装时，将 `firmware/` 内的文件复制到设备根目录，保持目录结构。注意不是复制
   一个 `/firmware` 目录。已有安装不要盲目覆盖 `/app_a`，它可能是当前或回退版本。
3. 按 [W5500 部署说明](ethernet.md) 核对固件能力和 `/network_config.py` 接线，接入有线
   局域网。启动示例会关闭 Wi-Fi 并初始化 W5500，默认通过 DHCP 获取地址。
   HTTPS 前还需设置正确 UTC 时间（通过 W5500 联网 NTP 或首次安装脚本 `tools/usb_install.py`）。
   注意 `mpremote rtc --set` 默认使用电脑本地时区；本机单独使用时须加 `TZ=UTC`，
   例如 `TZ=UTC mpremote connect /dev/cu.usbmodem201201 rtc --set`。
   当前未自动执行 NTP；仅获得 LAN 地址不代表已校时或能访问互联网。
4. 正常复位，固定启动器会将 bootstrap `0.0.0` 注册为已确认的 A 版本。

`firmware/app_a/app.py` 是包含 W5500 初始化的运行框架，不驱动任何风扇，也没有 Web 服务。
它在本地初始化成功后确认启动，不等待网线和 DHCP；这不能替代实际温控健康检查。

## 业务接入

业务入口必须是 `async def main(boot)` 并持续运行。建立传感器、风扇与必要任务后，
通过本地自检再调用 `boot.confirm_boot()`。由健康监督任务在所有关键任务运行正常时
调用 `boot.feed_watchdog()`，不要由无条件定时器或 OTA 下载器喂狗。

看门狗默认 30 秒，候选版本确认期限默认 60 秒；见 `unifan_ota/config.py`。
新版本异常退出会立即取消候选；卡死由看门狗复位，最多尝试两次后回到已确认版本。
候选在期限内未确认也会被取消并复位。确认之后的普通业务故障不会自动降级。
启动器、NVS 或已确认目录本身损坏需要 USB 恢复；这不属于目录 A/B 能覆盖的故障。

构造 OTA 服务：

```python
from unifan_ota.updater import Updater

# 在 app.main(boot) 中创建，并供管理接口共享；一个设备只创建一个 Updater。
updater = Updater(boot.manager)
# 在已初始化的 W5500 管理对象上等待局域网就绪；失败时保留本地控制并稍后重试。
# 这段检查不能作为新版本启动确认的前提；HTTPS 还要求 RTC 已正确校时。
if not await ethernet.wait_ready():
    raise OSError('有线局域网暂未就绪')
result = await updater.check()
if result['available']:
    # 实际页面先向用户显示版本，确认后传入该版本。
    await updater.install(result['version'])
# 此时仍运行旧版本；准备好风扇安全状态后由用户触发复位。
```

下载器每次读写 2KB，检查长度和 SHA-256。清单最大 24KB，最多 64 个文件，
业务文件合计最大 2MB；这些是拒绝异常包的上限，不是内存/存储可用量保证。
TLS 和 JSON 解析仍会占用额外内存，首次实机运行需要测量 `gc.mem_free()`。
下载不能并发；失败后重新完整下载，不做断点续传。DNS 解析在 MicroPython 中仍可能
阻塞，TLS 和 Flash 操作也不保证硬实时。风扇必须有合适的硬件/控制安全默认值，
并在实机上确认 OTA 与控制任务同时运行时的最大延迟。

## Microdot 2.x 接入接口

```python
from unifan_ota.web import register_routes

# authenticate 是项目提供的 async 函数。
# 它必须验证会话或凭据；浏览器场景还须校验 Origin/CSRF。
register_routes(web_app, updater, authorize=authenticate,
                before_reboot=prepare_fan_for_reboot)
```

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/ota/status` | 当前已确认版本、候选版本、进度和错误 |
| `POST /api/ota/check` | 获取 latest 清单，返回版本和 available |
| `POST /api/ota/install` | JSON `{"version":"1.2.0"}`，后台下载，返回 202 |
| `POST /api/ota/reboot` | 有待运行候选时，返回 202，约 2 秒后复位 |

所有接口均鉴权；没有默认密码或开放升级接口。安装接口只接收已选版本，不能注入
下载 URL。检查后如果 GitHub latest 已改变，会拒绝安装并要求重新检查。
接口暂未挂载到实际 Web 服务；后续 Microdot 页面任务负责实现认证和连接这些路由。
不要对公网暴露未加密的设备 HTTP 管理服务。

## 构建与发布

在电脑上从完整业务目录打包（包含 app.py、依赖于版本的模块和 static 文件）：

```sh
python3 tools/build_release.py --source firmware/app_a --version 0.1.0
```

生成 `dist/v0.1.0/manifest.json` 和 `file-000.bin` 等附件。附件虽然使用 `.bin`
扩展名，内容仍是原始 Python/静态文件；清单负责将其映射回设备路径。
这样不会因 GitHub Release 附件只支持扁平名称而丢失目录或重名文件。

在 GitHub 创建 tag 为 **v0.1.0** 的 draft Release，上传全部构建附件，核对后再发布
并标记为 latest。不要直接使用 GitHub 自动生成的 Source code ZIP；设备不解压 ZIP。
稳定版本号固定为三段数字 X.Y.Z，不支持 prerelease。发布后的同版本附件不可替换；
修复请使用更高版本，设备拒绝相同版本或降级安装。

示例清单结构：

```json
{
  "schema": 1,
  "board": "stamps3",
  "runtime_api": 1,
  "version": "0.1.0",
  "files": [{
    "path": "app.py",
    "url": "https://github.com/imkink/unifan/releases/download/v0.1.0/file-000.bin",
    "size": 1234,
    "sha256": "由构建工具生成的64位小写十六进制摘要"
  }]
}
```

SHA-256 检测文件完整性，发布者身份依赖 HTTPS 和 GitHub 仓库权限；当前不提供离线签名。
私有仓库鉴权、底层固件 OTA、启动器/CA/公共库/固定网络配置远程升级均不在本版本范围内。
从早期 USB 安装升级到此有线网络版本前，必须先通过 USB 放入 `/network_config.py`，
并确认固件支持 W5500；只发布业务附件不能补齐固件原生驱动或固定配置文件。

## 验证与恢复

主机测试：`python3 -m unittest discover -s tests -v`。测试使用假的 NVS/网络/复位，
不会访问设备或发布 Release。主机通过不等于硬件断电测试通过。
在电脑虚拟环境安装 `requirements-dev.txt` 后，会额外运行真实 Microdot 2.7.0 路由测试；
还可以运行 `python3 tools/check_micropython.py`，用 mpy-cross 检查所有设备文件的
MicroPython 语法。未安装开发依赖时，真实框架测试明确标记为跳过。

上线前在 StampS3 上依次验证：正确升级、自检确认、Python 异常、死循环看门狗、
确认超时、下载断网、下载中断电、提交候选时断电、空间不足、错误哈希、TLS 验证失败，
以及 `/data` 和配置始终保留。升级复位期间实际 PWM/风扇状态需要单独测量。

NVS 状态异常时不会默默选择某个目录。USB 恢复时先备份 `/data` 和配置，再检查
`unifan_ota` 命名空间。只有确认 A 目录是有效恢复版本后，才手动清除该命名空间的
`state` 键并提交，让首次启动重新注册 A；不要清空整个 NVS 或格式化文件系统。
