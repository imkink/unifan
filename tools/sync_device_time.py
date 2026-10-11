#!/usr/bin/env python3
"""通过严格校验证书的 HTTPS 管理接口校准设备 UTC；不开放未认证 NTP 信任。"""
import argparse
import time
from service_client import ServicesClient

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", required=True)
    args = parser.parse_args()
    result = ServicesClient(args.provision).request("/api/v1/system/time", "PUT", role="admin",
                                                  data={"unix_seconds": int(time.time())})
    if result["status"] != 200:
        raise SystemExit("Time sync failed: " + str(result["status"]))
    print("DEVICE_UTC", result["body"]["data"]["utc"])
