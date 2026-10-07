"""OTA 固定配置：首次通过 USB 安装，不随 A/B 业务升级包更新。

容量单位为字节，时间单位见各参数注释。修改目录、状态格式或运行库接口时，
必须同时检查已有设备的兼容性；不能只更改新业务目录中的文件。
"""

# 发布仓库，格式为“所有者/仓库名”。清单中的附件 URL 必须属于此仓库。
REPOSITORY = "imkink/unifan"

# 最新稳定版清单地址；GitHub 发布时需上传同名 manifest.json 附件并设为 latest。
# 这里只查询版本，实际业务文件必须使用清单内固定 vX.Y.Z 的下载地址。
MANIFEST_URL = "https://github.com/" + REPOSITORY + "/releases/latest/download/manifest.json"

# 项目自定义的目标板标识，必须与升级清单 board 完全一致，避免误装其他板型程序。
BOARD = "stamps3"

# 设备文件系统中的可信根证书路径；由 tools/prepare_ca.py 在电脑上生成后上传。
# W5500 提供网络连接，TLS 验证仍由 ESP32/MicroPython 完成；不能关闭证书验证。
CA_FILE = "/certs/github-roots.pem"

# A/B 业务目录，索引 0 为 A、1 为 B；均为设备根目录下的绝对路径。
# 两者不是固件分区。配置和历史数据必须放在这些目录之外，否则会被升级清理。
SLOTS = ("/app_a", "/app_b")

# 单次发布允许的最大文件数量（含 Python、HTML、CSS 等），用于限制清单和目录规模。
MAX_FILES = 64

# manifest.json 最大下载体积：24 KiB。解析 JSON 会额外占用内存，并非仅需这点 RAM。
MAX_MANIFEST_BYTES = 24576

# 单次发布所有文件的原始大小之和上限：2 MiB；不代表设备实际一定有这么多空闲空间。
MAX_RELEASE_BYTES = 2 * 1024 * 1024

# 写入非活动目录后仍需预留的文件系统空间：64 KiB，供 LittleFS 元数据和写时复制使用。
# 安装器还会额外估算每个文件的块分配开销；保留 7 天历史记录时也要计入整体分区规划。
FREE_RESERVE_BYTES = 65536

# HTTPS 单次读取/写入的数据块大小：2 KiB。小块降低峰值 RAM，但会增加读写调用次数。
CHUNK_SIZE = 2048

# 单次连接、读或写操作的等待上限，单位秒。不能强行中断固件内的阻塞 DNS 调用。
IO_TIMEOUT = 20

# 一个文件（含其重定向）的总下载超时，单位秒；不是整个多文件升级的总超时。
DOWNLOAD_TIMEOUT = 300

# 硬件看门狗超时，单位毫秒。只有健康的本地业务监督任务可以喂狗，下载器不喂狗。
# 应大于正常本地任务的最大延迟，并在具体固件上确认允许的超时范围。
WDT_TIMEOUT_MS = 30000

# 新版本启动后必须完成本地自检并确认的期限，单位毫秒；不要求互联网连接成功。
# 断网不应被认定为温控程序故障。未确认且事件循环正常时，到期主动回退并复位。
TRIAL_TIMEOUT_MS = 60000

# 因卡死/断电等未确认的候选版本最多允许启动的次数；达到后，下次启动选回已确认版本。
# 明确的 Python 异常或确认超时会直接取消候选，不必耗尽次数。
MAX_TRIAL_ATTEMPTS = 2

# 每个业务目录的完成标记文件；全部文件校验且落盘后才生成，发布包禁止包含此保留路径。
COMPLETE_FILE = "_ota_complete.json"
