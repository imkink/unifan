"""发布清单校验：设备安装器与电脑打包工具共用同一组规则。"""

from . import RUNTIME_API, config


def version_tuple(value):
    # 三段数字按数值比较，避免字符串比较把 1.10.0 错排在 1.9.0 之前。
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("Version must be X.Y.Z")
    parts = value.split(".")
    if len(parts) != 3:
        raise ValueError("Version must be X.Y.Z")
    for part in parts:
        if not part or any(c not in "0123456789" for c in part):
            raise ValueError("Version must be X.Y.Z")
        if len(part) > 1 and part[0] == "0":
            raise ValueError("Version components cannot have leading zeroes")
    return tuple(int(p) for p in parts)


def safe_path(path):
    # 禁止绝对路径、隐藏目录、..、转义等，确保下载内容不能越出非活动业务目录。
    if not isinstance(path, str) or not path or len(path) > 160:
        raise ValueError("Invalid release path")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./"
    if any(c not in allowed for c in path):
        raise ValueError("Unsupported character in release path")
    if any(not p or p.startswith(".") for p in path.split("/")):
        raise ValueError("Unsafe release path")
    if path == config.COMPLETE_FILE or path.startswith(config.COMPLETE_FILE + "/"):
        raise ValueError("Reserved release path")
    return path


def asset_name(index):
    # GitHub 附件是扁平结构；使用编号避免不同目录内同名文件相互覆盖。
    return "file-%03d.bin" % index


def validate(manifest):
    # 先验证完整清单再清理非活动目录，不允许更换仓库或混用不同版本的下载 URL。
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    if type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise ValueError("Unsupported manifest schema")
    if manifest.get("board") != config.BOARD:
        raise ValueError("Release targets another board")
    if type(manifest.get("runtime_api")) is not int or manifest["runtime_api"] != RUNTIME_API:
        raise ValueError("Release requires another OTA runtime")
    version_tuple(manifest.get("version"))
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= config.MAX_FILES:
        raise ValueError("Invalid file count")
    prefix = ("https://github.com/" + config.REPOSITORY + "/releases/download/v"
              + manifest["version"] + "/")
    paths = set()
    total = 0
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError("Invalid file entry")
        path = safe_path(entry.get("path"))
        if path in paths:
            raise ValueError("Duplicate release path")
        paths.add(path)
        size = entry.get("size")
        if type(size) is not int or size < 0:
            raise ValueError("Invalid file size")
        total += size
        digest = entry.get("sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Invalid SHA-256")
        if entry.get("url") != prefix + asset_name(index):
            raise ValueError("Asset URL must belong to the pinned repository and version")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError("File/directory path conflict")
    if "app.py" not in paths:
        raise ValueError("Release must contain app.py")
    if total > config.MAX_RELEASE_BYTES:
        raise ValueError("Release exceeds size limit")
    return manifest
