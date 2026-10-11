#!/usr/bin/env python3
"""管理本任务专属 Mosquitto，不接管 Homebrew 默认服务或其他 Broker。"""
import argparse
import os
from pathlib import Path
import signal
import subprocess


def broker_pid(folder):
    try:
        pid = int((folder / "broker.pid").read_text())
    except FileNotFoundError:
        return None
    try:
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except subprocess.CalledProcessError:
        return None
    # 权限不足必须明确报错，不能误报 stopped 后又启动第二个 Broker。
    if "mosquitto" not in command or str(folder / "mosquitto.conf") not in command:
        raise RuntimeError("PID belongs to another process; refusing action")
    return pid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--provision", required=True, type=Path)
    args = parser.parse_args()
    folder = args.provision.resolve()
    pid = broker_pid(folder)
    if args.action == "start" and pid is None:
        subprocess.run(["/opt/homebrew/sbin/mosquitto", "-c", str(folder / "mosquitto.conf"), "-d"], check=True)
        print("Broker started")
    elif args.action == "stop" and pid is not None:
        os.kill(pid, signal.SIGTERM)
        print("Graceful stop requested")
    else:
        print("Broker running, PID " + str(pid) if pid else "Broker stopped")
