"""持久状态与目录操作：启动状态用单个 NVS blob 提交，下载过程不频繁写 NVS。"""

import json
import os
from . import config
from .manifest import version_tuple


def sync():
    # 文件关闭不等于所有底层缓存已落盘；在完成标记和状态切换之前请求同步。
    if hasattr(os, "sync"):
        os.sync()


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError as exc:
        if exc.args[0] != 2:  # 只忽略 ENOENT（不存在）；不能把读盘错误误当成空目录。
            raise
        return False


def mkdirs(path):
    current = ""
    for part in path.split("/"):
        if not part:
            continue
        current += "/" + part
        if not exists(current):
            os.mkdir(current)


def clear_slot(path, slots):
    if path not in slots or path in ("", "/"):
        raise ValueError("Refusing to clear a non-slot directory")
    if exists(path):
        # 先移除完成标记再清理文件，中断后该目录不能被当作完整的新版本。
        marker = path + "/" + config.COMPLETE_FILE
        if exists(marker):
            os.remove(marker)
            sync()
        _remove_tree(path)
    mkdirs(path)


def _remove_tree(path):
    for name in os.listdir(path):
        child = path + "/" + name
        if os.stat(child)[0] & 0x4000:
            _remove_tree(child)
        else:
            os.remove(child)
    os.rmdir(path)


def slot_version(path):
    # 完成标记只在全部文件校验并落盘后写入；启动读取它不等于重新校验全部文件哈希。
    if not exists(path + "/app.py"):
        raise ValueError("Slot has no app.py")
    with open(path + "/" + config.COMPLETE_FILE) as stream:
        raw = stream.read(257)
    if len(raw) > 256:
        raise ValueError("Invalid slot marker")
    marker = json.loads(raw)
    if (not isinstance(marker, dict) or type(marker.get("runtime_api")) is not int
            or marker["runtime_api"] != 1):
        raise ValueError("Incompatible slot marker")
    version_tuple(marker.get("version"))
    return marker["version"]


def write_marker(path, version):
    # 临时文件写完并同步后再重命名；最后才允许安装器提交 NVS 候选状态。
    temporary = path + "/" + config.COMPLETE_FILE + ".tmp"
    with open(temporary, "w") as stream:
        stream.write(json.dumps({"runtime_api": 1, "version": version}))
    sync()
    os.rename(temporary, path + "/" + config.COMPLETE_FILE)
    sync()


def validate_state(state):
    if (not isinstance(state, dict) or type(state.get("schema")) is not int
            or state["schema"] != 1):
        raise ValueError("Invalid OTA state; USB recovery required")
    if type(state.get("active")) is not int or state["active"] not in (0, 1):
        raise ValueError("Invalid active slot")
    pending = state.get("pending")
    if type(pending) is not int or pending not in (-1, 0, 1) or pending == state["active"]:
        raise ValueError("Invalid pending slot")
    attempts = state.get("attempts")
    if type(attempts) is not int or not 0 <= attempts <= config.MAX_TRIAL_ATTEMPTS:
        raise ValueError("Invalid trial count")
    version_tuple(state.get("version"))
    if pending != -1:
        version_tuple(state.get("pending_version"))
    elif attempts != 0 or state.get("pending_version") is not None:
        raise ValueError("Inconsistent idle state")
    return state


class NVSState:
    def __init__(self, nvs=None):
        if nvs is None:
            import esp32
            nvs = esp32.NVS("unifan_ota")
        self.nvs = nvs

    def load(self):
        data = bytearray(512)
        try:
            size = self.nvs.get_blob("state", data)
        except OSError as exc:
            # ESP_ERR_NVS_NOT_FOUND 表示首次安装；其他 NVS 错误必须暴露，不能重置状态。
            if exc.args[0] in (-4354, 4354):
                return None
            raise
        return validate_state(json.loads(bytes(data[:size]).decode()))

    def save(self, state):
        # 一个 blob 包含所有关联字段；commit 成功后才认为启动状态已持久化。
        validate_state(state)
        data = json.dumps(state).encode()
        if len(data) > 512:
            raise ValueError("OTA state too large")
        self.nvs.set_blob("state", data)
        self.nvs.commit()
