"""通过 mpremote run 在设备执行的首次部署检查，不启用业务看门狗。

先上传 firmware 中除 main.py 外的文件，并设置 RTC。
探测会初始化 W5500、等待 DHCP，并通过严格 TLS 下载公开仓库页面（不保存正文）。
不会安装升级包或修改启动指针；完成后应复位设备，再正常启动业务。
"""

import asyncio
import gc
import os
import sys
import time

sys.path.insert(0, "/app_a")
from ethernet import W5500Ethernet
from unifan_ota.transport import HTTPSClient


async def main():
    print("FIRMWARE", sys.implementation)
    print("FILESYSTEM", os.statvfs("/"))
    print("UTC", time.gmtime())
    gc.collect()
    print("HEAP_BEFORE_NETWORK", gc.mem_free())
    ethernet = W5500Ethernet()
    ethernet.start()
    if not await ethernet.wait_ready():
        print("NETWORK_NOT_READY", ethernet.status())
        return
    print("NETWORK", ethernet.status())
    gc.collect()
    print("HEAP_BEFORE_TLS", gc.mem_free())
    count = 0

    def discard(data):
        # 分块读取后丢弃网页正文，只统计字节数；不把整个页面放入设备 RAM。
        nonlocal count
        count += len(data)

    client = HTTPSClient()
    await client.get("https://github.com/imkink/unifan", discard, 1024 * 1024)
    print("GITHUB_TLS_OK", count)
    gc.collect()
    print("HEAP_AFTER_TLS", gc.mem_free())


asyncio.run(main())
