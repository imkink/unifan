#!/usr/bin/env python3
"""生成可部署的 .mpy 业务槽；app.py 保留为固定启动器要求的轻量入口。"""
import argparse
from pathlib import Path
import shutil
import mpy_cross
from bundle_dashboard import bundle

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    source = ROOT / "firmware/app_a"
    for path in sorted(source.glob("*.py")):
        name = "app_main" if path.stem == "app" else path.stem
        if mpy_cross.run("-o", str(output / (name + ".mpy")), str(path)).wait():
            raise RuntimeError("mpy-cross failed: " + name)
    (output / "app.py").write_text('"""USB/OTA 构建入口，实际代码已预编译以降低 RAM 开销。"""\nfrom app_main import main\n')
    shutil.copytree(source / "static", output / "static")
    (output / "static/dashboard.html").write_bytes(bundle(source / "static"))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(build(args.output))
