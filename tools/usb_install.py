#!/usr/bin/env python3
"""向已备份、已刷好 MicroPython 的设备首次安装项目，并逐文件验证 SHA-256。

仅首次安装使用：若设备已有 main.py 则拒绝覆盖，日常升级请走 A/B OTA。
不擦除 Flash、不刷底层固件；显式指定串口以避免操作其他设备。
依赖电脑上的 mpremote。main.py 最后上传，校验全部通过后才复位运行。
"""

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from mpremote.transport_serial import SerialTransport

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    args = parser.parse_args()
    source = ROOT / "firmware"
    files = [path for path in source.rglob("*")
             if path.is_file() and path.suffix in (".py", ".json", ".pem")
             and not any(part.startswith(".") or part == "__pycache__"
                         for part in path.relative_to(source).parts)]
    if not (source / "certs/github-roots.pem").is_file():
        raise RuntimeError("先运行 tools/prepare_ca.py 生成根证书")
    files.sort(key=lambda path: (path == source / "main.py", str(path)))
    transport = SerialTransport(args.port)
    try:
        transport.enter_raw_repl()
        existing = transport.fs_listdir("/")
        if any(entry.name == "main.py" for entry in existing):
            raise RuntimeError("设备已有 main.py；拒绝用首次安装工具覆盖，请使用 OTA")
        for path in files:
            remote = "/" + path.relative_to(source).as_posix()
            parent = ""
            for part in remote.split("/")[1:-1]:
                parent += "/" + part
                if not transport.fs_isdir(parent):
                    transport.fs_mkdir(parent)
            content = path.read_bytes()
            transport.fs_writefile(remote, content)
            digest = hashlib.sha256(content).digest()
            if transport.fs_hashfile(remote, "sha256", chunk_size=1024) != digest:
                raise RuntimeError("设备文件校验失败: " + remote)
            print("VERIFIED", remote, len(content), flush=True)
        # mpremote rtc --set 默认取电脑本地时间；这里显式用 UTC，避免上海时区产生 +8h 偏差。
        now = datetime.now(timezone.utc)
        rtc = (now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0)
        transport.exec("import machine, os; machine.RTC().datetime(%r); os.sync()" % (rtc,))
        print("UTC_SET", now.isoformat(), flush=True)
        print("INSTALL_VERIFIED", len(files), flush=True)
        transport.exec_raw_no_follow("import machine; machine.reset()")
    finally:
        transport.close()


if __name__ == "__main__":
    main()
