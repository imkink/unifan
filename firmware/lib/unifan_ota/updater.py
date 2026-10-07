"""下载到非活动目录，逐文件校验并同步，最后提交待试运行版本；不自动重启。"""

import asyncio
import binascii
import hashlib
import json
import os
from . import config
from .manifest import validate, version_tuple
from .storage import clear_slot, mkdirs, sync, write_marker
from .transport import HTTPSClient


class BusyError(Exception):
    pass


class Updater:
    def __init__(self, manager, client=None):
        self.manager = manager
        self.client = client or HTTPSClient()
        self.busy = False
        self.progress = {"phase": "idle", "bytes": 0, "total": 0, "error": None}

    def status(self):
        state = self.manager.state()
        return {"current_version": state["version"], "active_slot": state["active"],
                "pending_version": state["pending_version"], "attempts": state["attempts"],
                "busy": self.busy, "progress": dict(self.progress)}

    def _begin(self):
        # 单设备共享一个 Updater；候选未确认时禁止继续升级，以保住现有回退目录。
        if self.busy:
            raise BusyError("Another OTA operation is running")
        state = self.manager.state()
        if state["pending"] != -1:
            raise BusyError("A release is already pending; reboot and confirm it first")
        if self.manager.running_slot != state["active"]:
            raise BusyError("OTA must run from the confirmed application")
        self.busy = True
        self.progress = {"phase": "checking", "bytes": 0, "total": 0, "error": None}
        return state

    async def _manifest(self):
        data = bytearray()
        await self.client.get(config.MANIFEST_URL, data.extend, config.MAX_MANIFEST_BYTES)
        return validate(json.loads(data.decode()))

    def _error(self, exc):
        # 限制错误文本长度；底层错误不得包含凭据或 GitHub 重定向中的签名 URL。
        self.progress["phase"] = "error"
        self.progress["error"] = str(exc)[:160]

    async def check(self):
        state = self._begin()
        try:
            manifest = await self._manifest()
            self.progress["phase"] = "idle"
            return {"current_version": state["version"], "version": manifest["version"],
                    "available": version_tuple(manifest["version"]) > version_tuple(state["version"]),
                    "size": sum(f["size"] for f in manifest["files"])}
        except BaseException as exc:
            self._error(exc)
            raise
        finally:
            self.busy = False

    async def install(self, expected_version):
        version_tuple(expected_version)
        state = self._begin()
        try:
            manifest = await self._manifest()
            if manifest["version"] != expected_version:
                raise ValueError("Latest release changed; check and approve the new version")
            if version_tuple(expected_version) <= version_tuple(state["version"]):
                raise ValueError("Only newer versions may be installed")
            target = 1 - state["active"]
            path = self.manager.slots[target]
            # 只清理非活动目录；当前代码和 /data 不动，中断后可以从头重试。
            clear_slot(path, self.manager.slots)
            total = sum(f["size"] for f in manifest["files"])
            stats = os.statvfs(path)
            block_size = stats[1] or stats[0]
            # 按块估算小文件开销，并预留元数据及 LittleFS 写时复制所需空间。
            allocated = sum(((f["size"] + block_size - 1) // block_size + 1) * block_size
                            for f in manifest["files"])
            if stats[4] * block_size < allocated + config.FREE_RESERVE_BYTES:
                raise OSError("Not enough free filesystem space for OTA")
            self.progress.update({"phase": "downloading", "total": total})
            for entry in manifest["files"]:
                destination = path + "/" + entry["path"]
                mkdirs(destination.rsplit("/", 1)[0])
                digest = hashlib.sha256()
                with open(destination, "wb") as stream:
                    # 边下载边写盘和计算哈希，避免把整个文件放进有限的 RAM。
                    def sink(data):
                        if stream.write(data) != len(data):
                            raise OSError("Short flash write")
                        digest.update(data)
                        self.progress["bytes"] += len(data)
                    await self.client.get(entry["url"], sink, entry["size"], entry["size"])
                actual = binascii.hexlify(digest.digest()).decode()
                if actual != entry["sha256"]:
                    raise ValueError("SHA-256 mismatch: " + entry["path"])
                await asyncio.sleep(0)
            self.progress["phase"] = "committing"
            sync()
            write_marker(path, expected_version)
            # 顺序不能颠倒：完整数据 -> 完成标记 -> NVS 候选指针。
            state.update({"pending": target, "pending_version": expected_version, "attempts": 0})
            self.manager.store.save(state)
            self.progress["phase"] = "ready"
            return {"version": expected_version, "reboot_required": True}
        except BaseException as exc:
            self._error(exc)
            raise
        finally:
            self.busy = False
