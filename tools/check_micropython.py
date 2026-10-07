#!/usr/bin/env python3
"""用 mpy-cross 检查全部设备源码；产物只放临时目录，不改变待上传的 .py 文件。"""

from pathlib import Path
import tempfile
import mpy_cross

ROOT = Path(__file__).resolve().parents[1]


def main():
    sources = sorted((ROOT / "firmware").rglob("*.py"))
    with tempfile.TemporaryDirectory(prefix="unifan-mpy-") as temp:
        # 交叉编译仅验证语法，不能证明设备固件包含 network.LAN 等原生模块。
        for index, source in enumerate(sources):
            result = mpy_cross.run("-o", str(Path(temp) / f"{index}.mpy"), str(source)).wait()
            if result:
                raise SystemExit(result)
    print(f"MicroPython syntax verified: {len(sources)} files (not a hardware runtime test)")


if __name__ == "__main__":
    main()
