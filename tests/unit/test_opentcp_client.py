"""通用自定义 TCP 客户端测试:分隔符成帧、缓冲、超时/重连契约、异步镜像。

覆盖:跨分片拼帧与多帧缓冲、strip/append 行为、超时不断线、连接错误
惰性重连且缓冲清空、帧超限断线、解码失败断线、空数据拒绝、点位方法
不可用提示、自定义分隔符、异步镜像。
"""
from __future__ import annotations

import asyncio
import socket
from typing import List

import pytest

from omniplc import OpenTcpClient
from omniplc.aio import AOpenTcpClient
from omniplc.core.constants import OPEN_TCP_DEFAULT_PORT
from omniplc.transport import TcpTransport
from omniplc.transport.base import BaseTransport
from scripted import ScriptedTransport


class _TimeoutTransport(BaseTransport):
    """recv 恒超时的假传输(验证超时不断线)。"""

    def __init__(self) -> None:
        self.sent = bytearray()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        raise socket.timeout("timed out")


class _ScriptedThenDeadTransport(BaseTransport):
    """先按分片应答、随后连接复位报错的假传输。"""

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent = bytearray()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        raise ConnectionResetError("connection reset by peer")


def _attach(client: OpenTcpClient, transport: BaseTransport) -> None:
    """挂载假传输并置为已连接(初始化超时属性,等价 connect() 的准备工作)。"""
    transport.receive_timeout = 5.0
    client._transport = transport
    client._connected = True


def test_defaults_and_transport() -> None:
    """默认端口 9000、分隔符 CRLF、utf-8;走线为 TcpTransport。"""
    client = OpenTcpClient()
    assert client._ip_address == "192.168.0.10"
    assert client._port == OPEN_TCP_DEFAULT_PORT == 9000
    assert client.delimiter == b"\r\n"
    assert client.encoding == "utf-8"
    assert client.append_delimiter is True
    assert client.strip_delimiter is True
    assert client.max_frame == 4096
    assert isinstance(client._create_transport(), TcpTransport)


def test_constructor_validation() -> None:
    """构造校验:空分隔符/非法编码/非法帧上限/非法端点。"""
    with pytest.raises(ValueError):
        OpenTcpClient(delimiter="")
    with pytest.raises(ValueError):
        OpenTcpClient(delimiter=b"")
    with pytest.raises(ValueError):
        OpenTcpClient(encoding="no-such-codec")
    with pytest.raises(ValueError):
        OpenTcpClient(max_frame=0)
    with pytest.raises(ValueError):
        OpenTcpClient(ip_address="")
    with pytest.raises(ValueError):
        OpenTcpClient(port=0)


def test_receive_frame_across_chunks() -> None:
    """跨分片到达的帧自动拼接,strip 默认去分隔符。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    scripted = ScriptedTransport([b"HE", b"LLO\r", b"\n"])
    _attach(client, scripted)
    assert client.receive() == (True, b"HELLO")
    assert bytes(scripted.sent) == b""


def test_receive_two_frames_buffered() -> None:
    """一次到达多帧:内部缓冲逐次返回;无分隔符的残余不成帧。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    scripted = ScriptedTransport([b"A\r\nB\r\nC"])
    _attach(client, scripted)
    assert client.receive() == (True, b"A")
    assert client.receive() == (True, b"B")
    assert client.receive() == (False, None)
    assert client.connected is False


def test_keep_delimiter_when_not_stripped() -> None:
    """strip_delimiter=False:返回帧含末尾分隔符。"""
    client = OpenTcpClient("127.0.0.1", 9000, strip_delimiter=False)
    _attach(client, ScriptedTransport([b"A\r\n"]))
    assert client.receive() == (True, b"A\r\n")


def test_empty_frame_is_valid() -> None:
    """空帧(立即出现分隔符)是合法帧,返回 b""。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    _attach(client, ScriptedTransport([b"\r\n"]))
    assert client.receive() == (True, b"")


def test_receive_timeout_keeps_connection() -> None:
    """接收超时:DeviceError 契约,链路完好不断线。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    _attach(client, _TimeoutTransport())
    assert client.receive() == (False, None)
    assert "接收超时" in (client.last_error or "")
    assert client.connected is True
    with pytest.raises(ValueError):
        client.receive(timeout=0)


def test_reconnect_clears_stale_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接错误标记断开;惰性重连后缓冲已清空,残字节不串入新会话。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    dead = _ScriptedThenDeadTransport([b"XX"])
    fresh = ScriptedTransport([b"OK\r\n"])
    transports: List[BaseTransport] = [dead, fresh]
    monkeypatch.setattr(client, "_create_transport", lambda: transports.pop(0))
    assert client.receive() == (False, None)
    assert client.connected is False
    assert client.receive() == (True, b"OK")
    assert bytes(fresh.sent) == b""


def test_max_frame_exceeded_marks_disconnected() -> None:
    """超过 max_frame 未见分隔符:坏帧断线(流内失步兜底)。"""
    client = OpenTcpClient("127.0.0.1", 9000, max_frame=16)
    _attach(client, ScriptedTransport([b"A" * 17]))
    assert client.receive() == (False, None)
    assert "失步" in (client.last_error or "")
    assert client.connected is False


def test_receive_text_decode_error_marks_disconnected() -> None:
    """解码失败按坏帧断线,不静默替换。"""
    client = OpenTcpClient("127.0.0.1", 9000, encoding="ascii")
    _attach(client, ScriptedTransport([b"\xff\xfe\r\n"]))
    assert client.receive_text() == (False, None)
    assert client.connected is False


def test_receive_text_roundtrip() -> None:
    """receive_text 按配置编码解码。"""
    client = OpenTcpClient("127.0.0.1", 9000, encoding="utf-8")
    _attach(client, ScriptedTransport(["温度25℃\r\n".encode("utf-8")]))
    assert client.receive_text() == (True, "温度25℃")


def test_send_and_send_text() -> None:
    """send 原样字节;send_text 默认补分隔符,append 可关;空数据拒绝。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    scripted = ScriptedTransport([])
    _attach(client, scripted)
    raw_client = OpenTcpClient("127.0.0.1", 9000, append_delimiter=False)
    raw_scripted = ScriptedTransport([])
    _attach(raw_client, raw_scripted)
    assert client.send(b"CMD") is True
    assert client.send_text("READ") is True
    assert raw_client.send_text("READ") is True
    assert bytes(scripted.sent) == b"CMD" + b"READ\r\n"
    assert bytes(raw_scripted.sent) == b"READ"
    with pytest.raises(ValueError):
        client.send(b"")
    # append 模式:空文本发送裸分隔符(空行)是合法;无分隔符且空才拒绝
    assert client.send_text("") is True
    assert bytes(scripted.sent).endswith(b"\r\n")
    with pytest.raises(ValueError):
        raw_client.send_text("")


def test_transact_and_transact_text() -> None:
    """transact 发送并收帧;transact_text 编解码走同一路径。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    scripted = ScriptedTransport([b"PONG\r\n"])
    _attach(client, scripted)
    assert client.transact(b"PING") == (True, b"PONG")
    assert bytes(scripted.sent) == b"PING"
    scripted2 = ScriptedTransport([b"1.0.3\r\n"])
    _attach(client, scripted2)
    assert client.transact_text("VER") == (True, "1.0.3")
    assert bytes(scripted2.sent) == b"VER\r\n"
    with pytest.raises(ValueError):
        client.transact(b"")


def test_read_write_unsupported() -> None:
    """无点位语义:read/write 返回失败并提示,连接保持。"""
    client = OpenTcpClient("127.0.0.1", 9000)
    _attach(client, ScriptedTransport([]))
    assert client.read_int("x") == (False, None)
    assert "不支持点位读取" in (client.last_error or "")
    assert client.write_float("x", 1.0) is False
    assert "不支持点位写入" in (client.last_error or "")
    assert client.connected is True


def test_custom_delimiter() -> None:
    """自定义分隔符(bytes/str 均可)。"""
    client = OpenTcpClient("127.0.0.1", 9000, delimiter=";")
    _attach(client, ScriptedTransport([b"V1;V2;"]))
    assert client.receive() == (True, b"V1")
    assert client.receive() == (True, b"V2")
    cr_client = OpenTcpClient("127.0.0.1", 9000, delimiter=b"\r")
    assert cr_client.delimiter == b"\r"


def test_async_mirror_roundtrip() -> None:
    """异步镜像:send_text + transact_text 往返,属性转发。"""

    async def scenario() -> None:
        client = AOpenTcpClient("127.0.0.1", 9000)
        assert client.delimiter == b"\r\n"
        assert client.max_frame == 4096
        scripted = ScriptedTransport([b"PONG\r\n", b"1.0.3\r\n"])
        scripted.receive_timeout = 5.0
        client._sync._transport = scripted
        client._sync._connected = True
        assert await client.send_text("READ") is True
        assert bytes(scripted.sent) == b"READ\r\n"
        assert await client.receive() == (True, b"PONG")
        assert await client.transact_text("VER") == (True, "1.0.3")
        await client.close()

    asyncio.run(scenario())
