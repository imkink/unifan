"""小内存 HTTPS 下载：支持证书验证、GitHub 重定向和分块响应。

通过标准 socket/asyncio 使用当前网络路由；W5500 经 network.LAN 接入后无需改写 TLS。
此处没有 Wi-Fi 初始化逻辑，也不适配第三方纯 Python W5500 socket 对象。
"""

import asyncio
import time
from . import config

# GitHub 及其发布附件域名白名单；不允许任意重定向目标或降级成明文 HTTP。
ALLOWED_HOSTS = ("github.com", "release-assets.githubusercontent.com",
                 "objects.githubusercontent.com", "github-releases.githubusercontent.com")


def parse_url(url):
    if (not isinstance(url, str) or len(url) > 8192 or not url.startswith("https://")
            or any(ord(c) <= 32 or ord(c) > 126 for c in url) or "#" in url):
        raise ValueError("Invalid HTTPS URL")
    rest = url[8:]
    host, separator, path = rest.partition("/")
    if host not in ALLOWED_HOSTS:
        raise ValueError("Download host is not allowed")
    return host, "/" + path if separator else "/"


def tls_context(cafile):
    import ssl
    if time.gmtime()[0] < 2025:
        raise ValueError("Set device UTC clock before HTTPS OTA")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=cafile)
    # 除信任链之外，还通过 server_hostname 验证目标域名；证书需要正确的 UTC 时间。
    # 固件或 CA 不支持时直接报错，绝不能退回 CERT_NONE。
    return context


class _Reader:
    # 有界缓冲同时供响应头和正文使用，不能丢失与头部一同读到的正文开头。
    def __init__(self, stream):
        self.stream = stream
        self.buffer = b""

    async def read(self, size):
        if not self.buffer:
            self.buffer = await asyncio.wait_for(
                self.stream.read(min(size, config.CHUNK_SIZE)), config.IO_TIMEOUT)
        result = self.buffer[:size]
        self.buffer = self.buffer[len(result):]
        return result

    async def line(self, limit=4096):
        line = b""
        while True:
            position = self.buffer.find(b"\n")
            count = position + 1 if position >= 0 else len(self.buffer)
            if len(line) + count > limit:
                raise ValueError("HTTP line too long")
            line += self.buffer[:count]
            self.buffer = self.buffer[count:]
            if position >= 0:
                if not line.endswith(b"\r\n"):
                    raise ValueError("Malformed HTTP line")
                return line[:-2]
            self.buffer = await asyncio.wait_for(
                self.stream.read(config.CHUNK_SIZE), config.IO_TIMEOUT)
            if not self.buffer:
                raise OSError("Truncated HTTP headers")


class HTTPSClient:
    def __init__(self, cafile=config.CA_FILE, connector=None, context_factory=None):
        self.cafile = cafile
        self.connector = connector or asyncio.open_connection
        self.context_factory = context_factory or tls_context

    async def get(self, url, sink, limit, expected_size=None):
        # 除单次 I/O 超时外，再限制单个文件及其重定向链的整体时间。
        return await asyncio.wait_for(
            self._get(url, sink, limit, expected_size), config.DOWNLOAD_TIMEOUT)

    async def _get(self, url, sink, limit, expected_size):
        context = self.context_factory(self.cafile)
        for redirect in range(6):
            host, path = parse_url(url)
            stream, writer = await asyncio.wait_for(
                self.connector(host, 443, ssl=context, server_hostname=host),
                config.IO_TIMEOUT)
            try:
                request = ("GET %s HTTP/1.1\r\nHost: %s\r\n"
                           "User-Agent: UNIFAN-OTA/1\r\nAccept-Encoding: identity\r\n"
                           "Connection: close\r\n\r\n") % (path, host)
                writer.write(request.encode())
                await asyncio.wait_for(writer.drain(), config.IO_TIMEOUT)
                reader = _Reader(stream)
                status = (await reader.line()).split(b" ")
                if len(status) < 2 or status[0] not in (b"HTTP/1.1", b"HTTP/1.0"):
                    raise ValueError("Invalid HTTP status")
                code = int(status[1])
                headers = {}
                header_bytes = 0
                while True:
                    line = await reader.line()
                    header_bytes += len(line) + 2
                    if header_bytes > 16384:
                        raise ValueError("HTTP headers too large")
                    if not line:
                        break
                    key, separator, value = line.partition(b":")
                    if not separator:
                        raise ValueError("Malformed HTTP header")
                    key = key.decode().lower()
                    if key in ("location", "content-length", "transfer-encoding", "content-encoding"):
                        if key in headers:
                            raise ValueError("Duplicate HTTP framing header")
                        headers[key] = value.strip().decode()
                if code in (301, 302, 303, 307, 308):
                    location = headers.get("location", "")
                    if location.startswith("/") and not location.startswith("//"):
                        location = "https://" + host + location
                    parse_url(location)
                    url = location
                    continue
                if code != 200:
                    raise OSError("GitHub HTTP status %d" % code)
                if headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed HTTP responses are not supported")
                transfer = headers.get("transfer-encoding")
                length = headers.get("content-length")
                if transfer is not None and (transfer.lower() != "chunked" or length is not None):
                    raise ValueError("Unsupported HTTP framing")
                if length is not None:
                    if not length or any(c not in "0123456789" for c in length):
                        raise ValueError("Invalid Content-Length")
                    length = int(length)
                    if length > limit or (expected_size is not None and length != expected_size):
                        raise ValueError("Content-Length does not match release")
                total = 0

                async def copy(count):
                    nonlocal total
                    if count > limit - total:
                        raise ValueError("Response exceeds size limit")
                    while count:
                        data = await reader.read(min(count, config.CHUNK_SIZE))
                        if not data:
                            raise OSError("Truncated HTTP body")
                        sink(data)
                        total += len(data)
                        count -= len(data)
                        await asyncio.sleep(0)

                if transfer is not None:
                    # 分块编码必须按长度读取；不能把 chunk 长度行或尾部信息写入业务文件。
                    while True:
                        token = (await reader.line(128)).split(b";", 1)[0]
                        if not token or any(c not in b"0123456789abcdefABCDEF" for c in token):
                            raise ValueError("Invalid HTTP chunk length")
                        size = int(token, 16)
                        if size == 0:
                            trailer_bytes = 0
                            while True:
                                trailer = await reader.line()
                                trailer_bytes += len(trailer) + 2
                                if trailer_bytes > 4096:
                                    raise ValueError("HTTP trailers too large")
                                if not trailer:
                                    break
                            break
                        await copy(size)
                        if await reader.line(2) != b"":
                            raise ValueError("Invalid HTTP chunk terminator")
                elif length is not None:
                    await copy(length)
                else:
                    while True:
                        data = await reader.read(config.CHUNK_SIZE)
                        if not data:
                            break
                        if len(data) > limit - total:
                            raise ValueError("Response exceeds size limit")
                        sink(data)
                        total += len(data)
                        await asyncio.sleep(0)
                if expected_size is not None and total != expected_size:
                    raise ValueError("Downloaded size does not match release")
                return total
            finally:
                # 失败、取消和重定向都释放连接，避免重复升级耗尽有限的网络资源。
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, asyncio.TimeoutError):
                    pass
        raise ValueError("Too many HTTP redirects")
