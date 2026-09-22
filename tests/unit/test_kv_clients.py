"""基恩士 KV Host Link 客户端测试:脚本化传输验证 TCP/UDP 全链路。

覆盖:

- ASCII 命令帧逐字节断言(RD/RDS/WR/WRS + CR 结束)
- 地址进制与规范文本(R 位组 / B、W 十六进制 / X 组+位 / DM 十进制)
- 响应令牌解析(位 0/1/ON/OFF、.U/.S/.D/.L 数值、出错代码 E0~E6)
- 出错代码 → DeviceError 不断线;坏响应 → 标记断开
- 字软元件位访问的"读-改-写"两段事务
"""
from __future__ import annotations

import struct
import time

import pytest

from omniplc import KeyenceHostLinkTcpClient, KeyenceHostLinkUdpClient
from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.plc.keyence import codec
from omniplc.plc.keyence.address import parse_kv_address
from scripted import ScriptedTransport


def _chunks(line: bytes) -> list:
    """把一行响应切成单字节分片(匹配 TCP recv(1) 逐字节收包契约)。"""
    return [line[i:i + 1] for i in range(len(line))]


def test_parse_address_bases() -> None:
    """地址解析:位组/十六进制/X 组位/十进制与规范文本还原。"""
    assert parse_kv_address("R515").to_text() == "R515"
    assert parse_kv_address("R515").number == (5 * 100 + 15)
    assert parse_kv_address("b1f").to_text() == "B1F"
    assert parse_kv_address("W100").number == 0x100
    assert parse_kv_address("X0F").number == 15
    assert parse_kv_address("X0F").to_text() == "X0F"
    assert parse_kv_address("Y10").to_text() == "Y10"
    assert parse_kv_address("DM100").to_text() == "DM100"
    assert parse_kv_address("dm100.5").bit == 5
    with pytest.raises(ValueError):
        parse_kv_address("R516")  # 位号 16 越界
    with pytest.raises(ValueError):
        parse_kv_address("M10.3")  # 位软元件不允许位号后缀
    with pytest.raises(ValueError):
        parse_kv_address("ZZ100")  # 未知软元件


def test_codec_frames() -> None:
    """帧编解码:命令 + CR;响应行去 CR/LF;出错代码转 DeviceError。"""
    assert codec.build_read("DM100.U") == b"RD DM100.U\r"
    assert codec.build_read("DM100.U", 2) == b"RDS DM100.U 2\r"
    assert codec.build_write("R10", "1") == b"WR R10 1\r"
    assert codec.build_write_consecutive("DM100.U", ["1", "2"]) == b"WRS DM100.U 2 1 2\r"
    assert codec.parse_response(b"OK\r\n") == "OK"
    assert codec.parse_bit_token("ON") is True
    assert codec.parse_word_token("65535", ".U") == 65535
    assert codec.parse_word_token("-32768", ".S") == -32768
    assert codec.parse_word_token("ABCD", ".H") == 0xABCD
    with pytest.raises(DeviceError) as exc_info:
        codec.check_error_code("E1")
    assert exc_info.value.code == 1
    assert "命令异常" in str(exc_info.value)
    with pytest.raises(ProtocolFrameError):
        codec.parse_word_token("99999", ".U")


def test_tcp_read_ushort_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:RDS 请求逐字节正确,响应解析出 .U 值。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    response = b"20\r\n"
    scripted = ScriptedTransport([response[i:i + 1] for i in range(len(response))])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("DM100") == (True, 20)
    assert bytes(scripted.sent) == b"RD DM100.U\r"


class _TrickleTransport(ScriptedTransport):
    """每 20ms 滴 1 字节、永不换行(验证收行受整事务 deadline 约束)。"""

    def __init__(self) -> None:
        super().__init__([])

    def recv(self, size: int) -> bytes:
        time.sleep(0.02)
        return b"x"


def test_tcp_line_deadline_bounds_dribble(monkeypatch: pytest.MonkeyPatch) -> None:
    """滴流对端不能逐字节重置超时:收行在预算内超时断线,不拖满 KV_MAX_LINE。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    client.receive_timeout = 0.05
    scripted = _TrickleTransport()
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    started = time.monotonic()
    assert client.read_ushort("DM100") == (False, None)
    assert time.monotonic() - started < 1.5  # 修复前:4096 × 0.05s ≈ 205s


def test_tcp_read_short_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:.S 有符号解析。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"-5\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_short("DM100") == (True, -5)


def test_tcp_read_bit_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:位软元件 RD 无后缀,ON/OFF 令牌解析。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"ON\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_bool("R515") == (True, True)
    assert bytes(scripted.sent) == b"RD R515\r"


def test_tcp_read_float_two_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:float32 = RDS 两字小端拼接。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    raw = struct.pack("<f", 3.14)
    words = struct.unpack("<HH", raw)
    scripted = ScriptedTransport(_chunks(bytes("{} {}\r\n".format(words[0], words[1]), "ascii")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, value = client.read_float("DM100")
    assert ok is True and value is not None and abs(value - 3.14) < 1e-6
    assert bytes(scripted.sent) == b"RDS DM100.U 2\r"


def test_tcp_read_int32_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:.L 有符号 32 位单令牌读取。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"-100000\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_int("DM200") == (True, -100000)
    assert bytes(scripted.sent) == b"RD DM200.L\r"


def test_tcp_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:WR 写 ushort,OK 应答。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"OK\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_ushort("DM100", 1234) is True
    assert bytes(scripted.sent) == b"WR DM100.U 1234\r"


def test_tcp_write_word_bit_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:字软元件位写 = RD .U → WR .U 两段事务。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"4\r\n") + _chunks(b"OK\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("DM100.5", True) is True
    assert bytes(scripted.sent) == b"RD DM100.U\rWR DM100.U 36\r"


def test_tcp_write_float_consecutive(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:float32 写 = WRS 两字。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"OK\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_float("DM100", 1.0) is True
    assert bytes(scripted.sent) == b"WRS DM100.U 2 0 16256\r"


def test_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """E4(禁止写入)→ DeviceError,不断线。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"E4\r\n") + _chunks(b"0\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_ushort("DM100", 1) is False
    assert client.connected is True
    assert client.last_error is not None and "E4" in client.last_error
    assert client.read_ushort("DM100") == (True, 0)
    assert client.connected is True


def test_bad_response_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 ASCII/缺结束符的坏响应 → 标记断开等待惰性重连。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport(_chunks(b"\xff\xfe\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("DM100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "ASCII" in client.last_error


def test_udp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:一请求一数据报,整包校验。"""
    client = KeyenceHostLinkUdpClient("127.0.0.1", 8000)
    scripted = ScriptedTransport([b"20\r\n"], datagram=True)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("DM100") == (True, 20)
    assert bytes(scripted.sent) == b"RD DM100.U\r"


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返。"""
    import asyncio

    from omniplc.aio import AKeyenceHostLinkTcpClient

    async def scenario() -> None:
        client = AKeyenceHostLinkTcpClient("127.0.0.1", 8000)
        sync = client._sync
        scripted = ScriptedTransport(_chunks(b"1234\r\n"))
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("DM100") == (True, 1234)
        await client.close()

    asyncio.run(scenario())


def test_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """字符串:WRS 写 → RDS 读,小端字节序。"""
    client = KeyenceHostLinkTcpClient("127.0.0.1", 8000)
    # "AB\0" 小端 → 字 0x4242? "A"=0x41,"B"=0x42 → 字 = 0x4241;"\0\0" → 0
    scripted = ScriptedTransport(_chunks(b"OK\r\n") + _chunks(b"16961 0\r\n"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_string("DM100", "AB") is True
    assert bytes(scripted.sent) == b"WRS DM100.U 1 16961\r"
    ok, value = client.read_string("DM100", 4)
    assert ok is True and value == "AB"
    assert bytes(scripted.sent).endswith(b"RDS DM100.U 2\r")
