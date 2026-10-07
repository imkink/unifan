"""USB 首次安装的固定启动入口；只负责选择并运行 A/B 业务版本。

以太网初始化由业务层完成。不要在这里等待网络，否则断网会阻止本地控制启动。
"""
from unifan_ota.boot import run

run()
