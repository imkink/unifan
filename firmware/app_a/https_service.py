"""唯一 HTTPS 入口；TLS 对象分配前检查单连接预算，永不启动明文监听器。"""
import asyncio
import gc
import ssl
import time
from microdot import Request, Response


class RequestLimit(Exception):
    pass


class BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.total = self.lines = 0
        self.seen = []

    async def readline(self):
        # Microdot 默认是在 readline 完成后检查长度；逐字节截断才能防止超长行耗尽堆。
        line = bytearray()
        while len(line) <= 1024:
            data = await self.stream.read(1)
            if not data:
                raise RequestLimit()
            line.extend(data)
            if data == b"\n":
                break
        self.total += len(line)
        self.lines += 1
        if len(line) > 1024 or self.total > 4096 or self.lines > 25:
            raise RequestLimit()
        if self.lines > 1 and line != b"\r\n":
            try:
                key, value = bytes(line).split(b":", 1)
                key, value = key.lower(), value.strip()
                if key in self.seen or key == b"transfer-encoding":
                    raise RequestLimit()
                self.seen.append(key)
                if key == b"content-length" and (not value.isdigit() or int(value) > 1024):
                    raise RequestLimit()
            except ValueError:
                raise RequestLimit()
        return bytes(line)

    async def readexactly(self, size):
        if not 0 <= size <= 1024:
            raise RequestLimit()
        return await self.stream.readexactly(size)


class HTTPSService:
    def __init__(self, app, root="/security", port=443):
        self.app, self.root, self.port = app, root, port
        self.context = self.server = None
        self.active = self.rejected = self.errors = self.requests = 0
        self.heap_min = gc.mem_free() if hasattr(gc, "mem_free") else None
        self.maintenance = False
        self.last_accept = None

    def status(self):
        return {"active": self.active, "max_connections": 1, "rejected": self.rejected,
                "errors": self.errors, "requests": self.requests, "heap_min": self.heap_min,
                "maintenance": self.maintenance}

    async def start(self):
        Request.max_content_length = Request.max_body_length = 1024
        Request.max_readline = 1024
        Response.send_file_buffer_size = 1024
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(self.root + "/server.pem", self.root + "/server.key")
        self.server = await asyncio.start_server(self.accept, "0.0.0.0", self.port, backlog=1)

    async def handle(self, reader, writer):
        try:
            request = await Request.create(self.app, BoundedReader(reader), writer,
                                           writer.get_extra_info("peername"), scheme="https")
        except (RequestLimit, ValueError, UnicodeError):
            response = Response({"schema": 1, "error": {"code": "request_limits"}}, 400)
        else:
            response = await self.app.dispatch_request(request)
        response.headers.update({"Connection": "close", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        # 页面文件流式输出，绝不把整个 HTML/字体读入 Python 堆。
        await response.write(writer)
        self.requests += 1

    async def accept(self, reader, writer):
        now = time.ticks_ms()
        # 忙时在握手之前关闭额外 socket；此时不能发送明文 HTTP 429/503。
        if self.active or (self.last_accept is not None and time.ticks_diff(now, self.last_accept) < 250):
            self.rejected += 1
            writer.close()
            # MicroPython Stream.close() 本身不关闭底层 socket，必须完成 wait_closed。
            # 否则拒绝路径会泄漏 TCP 连接，让客户端一直等到握手超时。
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass
            return
        self.active = 1
        self.last_accept = now
        try:
            gc.collect()
            wrapped = self.context.wrap_socket(writer.s, server_side=True, do_handshake_on_connect=False)
            wrapped.setblocking(False)
            reader.s = writer.s = wrapped
            await asyncio.wait_for(self.handle(reader, writer), 12)
        except Exception:
            self.errors += 1
        finally:
            if hasattr(gc, "mem_free"):
                self.heap_min = min(self.heap_min, gc.mem_free())
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass
            self.active = 0
            gc.collect()
