#!/usr/bin/env python3
"""从电脑已有信任库中提取 GitHub 实际使用的少量根证书，供设备验证 HTTPS。

要求 CPython/OpenSSL 提供 get_verified_chain（公开或内部接口）。
先完成可信连接再导出根；电脑信任库失效时直接报错，不接受未经验证的证书。
"""

import argparse
from pathlib import Path
import socket
import ssl
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firmware" / "lib"))
from unifan_ota.transport import ALLOWED_HOSTS


def root_for(host, context):
    # server_hostname 同时启用 SNI 与主机名校验；只读取验证通过的证书链。
    with socket.create_connection((host, 443), timeout=20) as plain:
        with context.wrap_socket(plain, server_hostname=host) as tls:
            getter = getattr(tls, "get_verified_chain", None)
            if getter is None:
                getter = getattr(tls._sslobj, "get_verified_chain", None)
            if getter is None:
                raise RuntimeError("This Python/OpenSSL cannot export verified chains")
            chain = getter()
            if not chain:
                raise RuntimeError("TLS returned no verified chain")
            root = chain[-1]
            if hasattr(root, "public_bytes"):
                root = root.public_bytes()
            if isinstance(root, bytes):
                root = ssl.DER_cert_to_PEM_cert(root) if not root.startswith(b"-----") else root.decode()
            return root.strip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "firmware/certs/github-roots.pem")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output exists; inspect it or choose a new --output path")
    context = ssl.create_default_context()
    roots = []
    for host in ALLOWED_HOSTS:
        root = root_for(host, context)
        if root not in roots:
            roots.append(root)
        print("Verified:", host)
    bundle = "".join(roots)
    # 再用仅含导出根的全新上下文验证各下载域名，确认设备无需加载整套桌面 CA 库。
    minimal = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    minimal.load_verify_locations(cadata=bundle)
    for host in ALLOWED_HOSTS:
        root_for(host, minimal)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(bundle)
    print(f"Saved {len(roots)} trusted roots ({len(bundle)} bytes): {args.output}")


if __name__ == "__main__":
    main()
