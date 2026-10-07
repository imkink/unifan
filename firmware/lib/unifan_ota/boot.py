"""固定启动器：候选版本自检确认之前，始终保留已确认版本作为回退目标。"""

import asyncio
import sys
from . import config
from .storage import NVSState, slot_version


def discard_pending(state):
    # 只取消候选状态，不删除其文件；下次下载可以重新覆盖非活动目录。
    state["pending"] = -1
    state["pending_version"] = None
    state["attempts"] = 0


class BootManager:
    def __init__(self, store, slots=config.SLOTS):
        self.store = store
        self.slots = slots
        self.running_slot = None

    def state(self):
        # 仅在 NVS 明确返回“键不存在”时注册首次安装；损坏状态不能当成全新设备。
        state = self.store.load()
        if state is None:
            state = {"schema": 1, "active": 0, "version": slot_version(self.slots[0]),
                     "pending": -1, "pending_version": None, "attempts": 0}
            self.store.save(state)
        return state

    def select(self):
        # 先判断候选是否完整以及试运行次数，再决定启动候选还是已确认版本。
        state = self.state()
        pending = state["pending"]
        if pending != -1:
            try:
                valid = slot_version(self.slots[pending]) == state["pending_version"]
            except (OSError, ValueError):
                valid = False
            if not valid or state["attempts"] >= config.MAX_TRIAL_ATTEMPTS:
                discard_pending(state)
                self.store.save(state)
            else:
                state["attempts"] += 1
                self.store.save(state)  # 执行候选代码前先落盘计数，卡死或断电也能计入失败次数。
                self.running_slot = pending
                return pending
        if slot_version(self.slots[state["active"]]) != state["version"]:
            raise ValueError("Confirmed slot is damaged; USB recovery required")
        self.running_slot = state["active"]
        return self.running_slot

    def confirm(self):
        # 已确认指针和候选状态作为同一个 NVS blob 提交，避免逐字段更新产生中间状态。
        state = self.state()
        if state["pending"] == self.running_slot:
            state["active"] = self.running_slot
            state["version"] = state["pending_version"]
            discard_pending(state)
            self.store.save(state)

    def reject(self):
        state = self.state()
        if state["pending"] == self.running_slot:
            discard_pending(state)
            self.store.save(state)


class BootContext:
    def __init__(self, manager, watchdog):
        self.manager = manager
        self.watchdog = watchdog
        self.slot = manager.running_slot
        self.app_dir = manager.slots[self.slot]
        state = manager.state()
        self.is_trial = state["pending"] == self.slot
        self.version = state["pending_version"] if self.is_trial else state["version"]
        self.confirmed = not self.is_trial

    def confirm_boot(self):
        """传感器、控制任务等本地自检通过后调用；不要依赖网线、DHCP 或 GitHub 可达。"""
        self.manager.confirm()
        self.confirmed = True

    def feed_watchdog(self):
        """仅供本地健康监督任务调用；业务异常时停止喂狗，让硬件复位恢复。"""
        self.watchdog.feed()


async def _trial_deadline(context):
    # 即使事件循环还活着且应用持续喂狗，也不能无限拖延新版本确认。
    await asyncio.sleep(config.TRIAL_TIMEOUT_MS / 1000)
    if not context.confirmed:
        context.manager.reject()
        import machine
        machine.reset()


async def _run_app(context):
    task = asyncio.create_task(_trial_deadline(context)) if context.is_trial else None
    try:
        import app
        await app.main(context)
        raise RuntimeError("app.main() must keep running")
    finally:
        if task is not None:
            task.cancel()


def run():
    import machine
    manager = BootManager(NVSState())
    manager.select()
    context = BootContext(manager, machine.WDT(timeout=config.WDT_TIMEOUT_MS))
    sys.path.insert(0, context.app_dir)
    try:
        asyncio.run(_run_app(context))
    except Exception as exc:
        sys.print_exception(exc)
        manager.reject()
        machine.reset()  # 用全新解释器启动旧版本，避免混用新旧模块缓存。
