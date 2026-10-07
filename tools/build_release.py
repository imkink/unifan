#!/usr/bin/env python3
"""在电脑上生成 GitHub Release 附件；只打包，不自动上传或发布。"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

# 以项目位置定位源码，不受命令执行目录影响；复用设备端清单规则，避免两端标准不一致。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "lib"))
from unifan_ota import config
from unifan_ota.manifest import asset_name, safe_path, validate, version_tuple


def build(source, output, version):
    version_tuple(version)
    source = Path(source).resolve()
    output = Path(output).resolve()
    if not source.is_dir():
        raise ValueError("Source must be an application slot directory")
    if output == source or source in output.parents:
        raise ValueError("Output must be outside the application source")
    if output.exists():
        raise ValueError("Output already exists; use a new empty destination")
    sources = []
    entries = []
    prefix = f"https://github.com/{config.REPOSITORY}/releases/download/v{version}/"
    for path in sorted(source.rglob("*")):
        # 固定排序保证附件编号可复现；忽略本机缓存，不允许符号链接引用目录外的数据。
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if relative.as_posix() == config.COMPLETE_FILE or path.suffix == ".pyc":
            continue
        name = safe_path(relative.as_posix())
        content = path.read_bytes()
        if path.suffix == ".py":
            compile(content, name, "exec")  # 这里只检查 Python 语法；设备兼容性仍需 mpy-cross 和实机验证。
        entries.append({"path": name, "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "url": prefix + asset_name(len(entries))})
        sources.append(path)
    manifest = validate({"schema": 1, "board": config.BOARD, "runtime_api": 1,
                         "version": version, "files": entries})
    encoded = (json.dumps(manifest, indent=2) + "\n").encode()
    if len(encoded) > config.MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large for the device")
    output.mkdir(parents=True)
    for index, path in enumerate(sources):
        destination = output / asset_name(index)
        shutil.copyfile(path, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != entries[index]["sha256"]:
            raise ValueError("Source changed while packaging; rebuild to a new directory")
    # 清单最后写入；构建失败的目录不能发布，也不会自动覆盖已有版本附件。
    (output / "manifest.json").write_bytes(encoded)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "firmware" / "app_a")
    parser.add_argument("--version", required=True, help="Stable version X.Y.Z, without v")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "dist" / ("v" + args.version)
    manifest = build(args.source, output, args.version)
    print(f"Built {manifest['version']}: {len(manifest['files'])} files in {output}")
    print("Upload manifest.json and all file-*.bin assets to the matching vX.Y.Z GitHub Release.")


if __name__ == "__main__":
    main()
