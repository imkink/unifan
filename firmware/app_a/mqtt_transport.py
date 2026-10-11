"""有界 MQTT 3.1.1 发布客户端：仅 CONNECT、QoS1 PUBLISH、PING 和 DISCONNECT。

不实现订阅或远程命令；每次最多一条消息在途。所有网络等待有期限，TLS 失败绝不降级。
"""
import asyncio
import ssl


def field(value):
    value = value.encode() if isinstance(value, str) else value
    if len(value) > 65535:
        raise ValueError("mqtt_field_size")
    return bytes((len(value) >> 8, len(value) & 255)) + value


def packet(kind, data):
    if len(data) > 4096:
        raise ValueError("mqtt_packet_size")
    size, length = len(data), bytearray()
    while True:
        digit = size % 128
        size //= 128
        length.append(digit | (128 if size else 0))
        if not size:
            return bytes((kind,)) + bytes(length) + data


class MQTTProtocolError(Exception):
    pass


class MQTTAuthError(Exception):
    pass


class MQTTTransport:
    def __init__(self, ca_file="/security/mqtt-ca.pem"):
        self.ca_file = ca_file
        self.reader = self.writer = self.context = None
        self.pid = 0

    async def exchange(self, outgoing, expected, size):
        self.writer.write(outgoing)
        await asyncio.wait_for(self.writer.drain(), 8)
        header = await asyncio.wait_for(self.reader.readexactly(2), 8)
        # 发布端只应收到固定短控制包；在分配正文前拒绝大包/异常剩余长度。
        if header != bytes((expected, size)):
            raise MQTTProtocolError("unexpected_control_packet")
        return await asyncio.wait_for(self.reader.readexactly(size), 8) if size else b""

    async def connect(self, config, client_id, base):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.load_verify_locations(cafile=self.ca_file)
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(
            config["host"], config["port"], ssl=self.context,
            server_hostname=config["server_name"] or config["host"]), 8)
        # clean session + retained QoS1 遗嘱 + 用户名/密码；遗嘱仅代表发布通道离线。
        body = (b"\x00\x04MQTT\x04\xee\x00\x1e" + field(client_id) +
                field(base + "/availability") + field("offline") +
                field(config["username"]) + field(config["password"] or ""))
        answer = await self.exchange(packet(0x10, body), 0x20, 2)
        if answer[1] in (4, 5):
            raise MQTTAuthError("broker_auth_rejected")
        if answer != b"\x00\x00":
            raise MQTTProtocolError("connack_rejected")

    async def publish(self, topic, payload, retain=True):
        self.pid = self.pid % 65535 + 1
        pid = bytes((self.pid >> 8, self.pid & 255))
        body = field(topic) + pid + (payload.encode() if isinstance(payload, str) else payload)
        answer = await self.exchange(packet(0x33 if retain else 0x32, body), 0x40, 2)
        if answer != pid:
            raise MQTTProtocolError("puback_mismatch")

    async def ping(self):
        await self.exchange(b"\xc0\x00", 0xd0, 0)

    async def close(self, graceful=False):
        writer, self.writer = self.writer, None
        self.reader = None
        if writer is not None:
            try:
                if graceful:
                    writer.write(b"\xe0\x00")
                    await asyncio.wait_for(writer.drain(), 2)
            except Exception:
                pass
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass
        self.context = None
