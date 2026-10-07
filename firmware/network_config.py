"""W5500 板级网络配置：设备根目录中的固定文件，不随 A/B 业务升级覆盖。

当前接线取自项目 REF-2.STAMP-S3管脚定义.md 的核对记录。
GPIO 使用芯片编号，不是排针序号；改变接线后应经 USB 修改这里并硬复位。
底层采用官方固件内置的 network.LAN/PHY_W5500，Python 负责初始化与连接管理。
"""

# ESP32-S3 硬件 SPI 控制器编号，MicroPython 通常使用 1 或 2；不能用 SoftSPI。
# 该总线交由 LAN 原生驱动使用，运行中不要重新初始化、deinit 或直接读写 W5500。
SPI_ID = 1

# machine.SPI 初始化的时钟参数，单位 Hz，此处为 20 MHz。
# 注意：官方 ESP32 LAN 驱动会创建自己的 SPI 设备，实际以太网时钟可能由固件宏
# MICROPY_PY_NETWORK_LAN_SPI_CLOCK_SPEED_MZ 决定，而非本参数。
# 若需降低实际 W5500 时钟以适应较长走线，应核对所用固件，必要时调整固件编译配置。
SPI_BAUDRATE = 20_000_000

# SPI 时钟：StampS3 G41 -> W5500 SCLK。
SCK_PIN = 41

# 主机发送、W5500 接收：StampS3 G42 -> W5500 MOSI；已更正旧记录中的 G43 误记。
MOSI_PIN = 42

# W5500 发送、主机接收：StampS3 G14 <- W5500 MISO。
MISO_PIN = 14

# 片选信号，低电平有效：StampS3 G40 -> W5500 CSN，由原生 LAN 驱动控制。
CS_PIN = 40

# 中断信号，低电平有效：StampS3 G39 <- W5500 INTN。
# 当前实现要求接好此管脚，不使用缺少中断线时的纯 Python 轮询方案。
INT_PIN = 39

# 硬件复位信号，低电平有效：StampS3 G12 -> W5500 RSTN；复位时序由 LAN 驱动执行。
RESET_PIN = 12

# LAN 构造器要求的 PHY 地址；W5500 通常填 0，和局域网 IP 地址没有关系。
PHY_ADDR = 0

# DHCP/局域网中公布的设备名；应只用字母、数字和连字符。
# 多台设备可以使用不同名称，例如 unifan-rack-01；不保证路由器自动提供 .local 解析。
HOSTNAME = "unifan"

# True：向路由器/DHCP 服务器自动申请 IPv4、掩码、网关和 DNS。
# False：使用下面的 STATIC_IPV4；DHCP 模式需要固件支持 LAN.ipconfig(dhcp4=True)。
USE_DHCP = True

# 静态地址四元组，顺序为（IPv4，子网掩码，默认网关，DNS服务器）。
# DHCP 模式下保持 None 即可；启用静态模式前按现场局域网填写，避免地址冲突。
# 示例：("192.168.1.50", "255.255.255.0", "192.168.1.1", "192.168.1.1")。
# 网关用于访问互联网，DNS 用于解析 github.com；仅有本机 IP 不代表能访问 GitHub。
STATIC_IPV4 = None

# 一轮等待链路和 IPv4 就绪的上限，单位秒。超时后继续后台等待，不抛错重启设备。
# 这是网络状态提示周期；不限制本地业务启动，也不作为 OTA 新版本确认条件。
CONNECT_TIMEOUT = 30

# 网络未就绪或原生接口临时报错后，再次尝试的间隔，单位秒；使用异步等待。
RETRY_INTERVAL = 5

# 连接状态轮询间隔，单位秒；决定拔线/插线后 Python 状态更新的速度。
# PHY 中断和 DHCP 由固件处理；此值不是 SPI 收包轮询周期。
POLL_INTERVAL = 1
