#!/usr/bin/env python3
"""使用只读测试账号订阅设备 topic；不在进程参数中传递密码。"""
import argparse
import json
import time
from service_client import MQTTObserver

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", required=True)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    observer = MQTTObserver(args.provision).subscribe()
    end, latest = time.monotonic() + args.seconds, 0
    try:
        while time.monotonic() < end:
            for message in list(observer.messages):
                if message["received_monotonic"] > latest:
                    print(json.dumps(message, ensure_ascii=False), flush=True)
                    latest = message["received_monotonic"]
            if observer.errors:
                raise RuntimeError("Observer disconnected: " + observer.errors[-1])
            time.sleep(.2)
    finally:
        observer.close()
