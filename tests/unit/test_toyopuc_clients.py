"""丰田 TOYOPUC 计算机链接客户端测试:脚本化传输验证 TCP/UDP 全链路。

覆盖:

- 地址解析(十六进制编号、L/H/W 后缀、位软元件编号段、越界)
- 二进制帧编解码黄金样本(CMD=1C/1D/1E/1F/20/21 + 帧长校验)
- 出错代码(RC=10,详细码在 CMD 或数据末字节)→ DeviceError 不断线
- 坏响应(帧长不符/FT 非法/命令字不符)→ 标记断开
- TCP 分段收包与 UDP 整包接收;异步镜像;字符串字节读写
"""
from __future__ import annotations

import struct

import pytest

from omniplc import ToyopucTcpClient, ToyopucUdpClient
from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.plc.toyopuc import codec
from omniplc.plc.toyopuc.address import (
    encode_bit_address,
    encode_byte_address,
    encode_word_address,
    parse_toyopuc_address,
)
from scripted import ScriptedTransport


def _chunks(frame: bytes) -> list:
    """把响应帧切成 [帧头 4 字节, 帧体] 两段(匹配 TCP 按长收包契约)。"""
    return [frame[:4], frame[4:]]


def _response(cmd: int, data: bytes = b"", rc: int = 0x00) -> bytes:
    """按响应格式构造一帧(帧长 = CMD + 数据)。"""
    length = 1 + len(data)
    return bytes((0x80, rc, length & 0xFF, length >> 8, cmd)) + data


# ----------------------------------------------------------------------
# 地址解析
# ----------------------------------------------------------------------

def test_parse_address() -> None:
    """地址解析:十六进制编号、单位判定与 L/H/W 后缀。"""
    assert parse_toyopuc_address("d0100").number == 0x100
    assert parse_toyopuc_address("D0100").area == "D"
    assert parse_toyopuc_address("D0100").unit == "word"
    assert parse_toyopuc_address("M0201").unit == "bit"
    assert parse_toyopuc_address("X0010H").unit == "byte"
    assert parse_toyopuc_address("X0010H").high is True
    assert parse_toyopuc_address("M0201W").unit == "word"
    assert parse_toyopuc_address("L0100L").area == "L"  # 软元件 L + 低字节后缀
    assert encode_word_address(parse_toyopuc_address("D0100")) == 0x1100
    assert encode_bit_address(parse_toyopuc_address("M0201")) == 0x1A01
    assert encode_byte_address(parse_toyopuc_address("X0010H")) == 0x221
    assert encode_byte_address(parse_toyopuc_address("D0100L")) == 0x2200


def test_parse_address_errors() -> None:
    """地址解析:越界与非法后缀。"""
    for bad in (
        "ZZ100",      # 未知软元件
        "D0100W",     # 字软元件不支持 W 后缀
        "M2000",      # 位编号越界(不在 0x000~0x7FF / 0x1000~0x17FF)
        "K300",       # 位编号越界(K 上限 0x2FF)
        "D10000",     # 编号超出 16 位
        "D0100Z",     # 非法后缀
        "",
    ):
        with pytest.raises(ValueError):
            parse_toyopuc_address(bad)


# ----------------------------------------------------------------------
# 帧编解码
# ----------------------------------------------------------------------

def test_codec_build_frames() -> None:
    """帧编码黄金样本:帧长 = CMD + 数据,地址/点数小端。"""
    assert codec.build_word_read(0x1100, 1) == b"\x00\x00\x05\x00\x1c\x00\x11\x01\x00"
    assert codec.build_word_write(0x1100, [0x1234, 5]) == \
        b"\x00\x00\x07\x00\x1d\x00\x11\x34\x12\x05\x00"
    assert codec.build_byte_read(0x2200, 4) == b"\x00\x00\x05\x00\x1e\x00\x22\x04\x00"
    assert codec.build_byte_write(0x2200, b"AB") == b"\x00\x00\x05\x00\x1f\x00\x22\x41\x42"
    assert codec.build_bit_read(0x1A01) == b"\x00\x00\x03\x00\x20\x01\x1a"
    assert codec.build_bit_write(0x1A01, True) == b"\x00\x00\x04\x00\x21\x01\x1a\x01"
    assert codec.pack_u16(0x1234) == b"\x34\x12"
    assert codec.unpack_u16(b"\x34\x12\x00\x80") == [0x1234, 0x8000]


def test_codec_build_limits() -> None:
    """点数与数值边界校验。"""
    with pytest.raises(ValueError):
        codec.build_word_read(0, 0)
    with pytest.raises(ValueError):
        codec.build_word_read(0, 0x201)  # 超出 0x200 上限
    with pytest.raises(ValueError):
        codec.build_byte_write(0, b"")  # 空数据
    with pytest.raises(ValueError):
        codec.pack_u16(0x10000)  # 数值越界
    with pytest.raises(ProtocolFrameError):
        codec.unpack_u16(b"\x01")  # 奇数字节


def test_codec_parse_response() -> None:
    """响应解码:正常帧、出错帧(详细码在 CMD 或数据末字节)、坏帧。"""
    assert codec.parse_response(_response(0x1C, b"\x14\x00")) == (0x1C, 0x00, b"\x14\x00")
    # RC=10 无数据:详细出错代码在 CMD 字节
    assert codec.parse_response(bytes((0x80, 0x10, 0x01, 0x00, 0x40))) == (0x40, 0x10, b"")
    for bad in (b"\x80\x00\x03\x00", b"\x00\x00\x03\x00\x1c", _response(0x1C, b"\x00") + b"\x00"):
        with pytest.raises(ProtocolFrameError):
            codec.parse_response(bad)


def test_codec_check_response() -> None:
    """响应校验:出错码转 DeviceError,命令字/长度不符转 ProtocolFrameError。"""
    assert codec.check_response(0x1C, 0x00, b"\x14\x00", 0x1C, 2) == b"\x14\x00"
    with pytest.raises(DeviceError) as exc_info:
        codec.check_response(0x40, 0x10, b"", 0x1C, None)
    assert exc_info.value.code == 0x40
    assert "越界" in str(exc_info.value)
    with pytest.raises(DeviceError) as exc_info:
        codec.check_response(0x1C, 0x10, b"\x41", 0x1C, None)
    assert exc_info.value.code == 0x41  # 详细码在数据末字节
    with pytest.raises(DeviceError):
        codec.check_response(0x1C, 0x66, b"", 0x1C, None)
    with pytest.raises(ProtocolFrameError):
        codec.check_response(0x1D, 0x00, b"", 0x1C, 0)  # 命令字不符
    with pytest.raises(ProtocolFrameError):
        codec.check_response(0x1C, 0x00, b"\x00", 0x1C, 2)  # 数据长度不符


# ----------------------------------------------------------------------
# TCP 全链路(脚本化传输)
# ----------------------------------------------------------------------

def test_tcp_read_ushort_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:连续字读(CMD=1C)请求与响应全链路。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1C, b"\x14\x00")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D0100") == (True, 20)
    assert bytes(scripted.sent) == b"\x00\x00\x05\x00\x1c\x00\x11\x01\x00"


def test_tcp_read_short_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:16 位有符号解析。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1C, b"\xfb\xff")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_short("D0100") == (True, -5)


def test_tcp_read_int_and_uint(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:32 位低字在前,有符号/无符号。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(
        _chunks(_response(0x1C, b"\x60\x79\xfe\xff"))
        + _chunks(_response(0x1C, b"\x60\x79\xfe\xff"))
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_int("D0100") == (True, -100000)
    assert client.read_uint("D0100") == (True, 0xFFFE7960)


def test_tcp_read_long_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:64 位四字低字在前。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    raw = struct.pack("<Q", 0x123456789ABCDEF0)
    scripted = ScriptedTransport(_chunks(_response(0x1C, raw)))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ulong("D0100") == (True, 0x123456789ABCDEF0)


def test_tcp_read_float_two_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:float32 两字小端拼接(低字在前)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1C, struct.pack("<f", 3.14))))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, value = client.read_float("D0100")
    assert ok is True and value is not None and abs(value - 3.14) < 1e-6
    assert bytes(scripted.sent) == codec.build_word_read(0x1100, 2)


def test_tcp_read_bit_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:位软元件单位读(CMD=20)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x20, b"\x01")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_bool("M0201") == (True, True)
    assert bytes(scripted.sent) == b"\x00\x00\x03\x00\x20\x01\x1a"


def test_tcp_read_packed_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:位软元件打包字访问(M0201W → CMD=1C,字地址 = 0x180 + 0x201)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1C, b"\x05\x00")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("M0201W") == (True, 5)
    assert bytes(scripted.sent) == b"\x00\x00\x05\x00\x1c\x81\x03\x01\x00"


def test_tcp_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:连续字写(CMD=1D),响应无数据。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1D)))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_ushort("D0100", 1234) is True
    assert bytes(scripted.sent) == b"\x00\x00\x05\x00\x1d\x00\x11\xd2\x04"


def test_tcp_write_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:位软元件单位写(CMD=21)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x21)) + _chunks(_response(0x21)))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("M0201", True) is True
    assert client.write_bool("M0201", False) is True
    assert bytes(scripted.sent) == b"\x00\x00\x04\x00\x21\x01\x1a\x01" \
                                    b"\x00\x00\x04\x00\x21\x01\x1a\x00"


def test_tcp_write_float_consecutive(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:float32 写 = 连续字写两字(低字在前)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1D)))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_float("D0100", 1.0) is True
    assert bytes(scripted.sent) == b"\x00\x00\x07\x00\x1d\x00\x11\x00\x00\x80\x3f"


def test_tcp_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """字符串:连续字节写/读(CMD=1F/1E),从字区编号低字节起。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(
        _chunks(_response(0x1F)) + _chunks(_response(0x1E, b"AB\x00\x00"))
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_string("D0100", "AB") is True
    assert bytes(scripted.sent) == b"\x00\x00\x05\x00\x1f\x00\x22\x41\x42"
    ok, value = client.read_string("D0100", 4)
    assert ok is True and value == "AB"
    assert bytes(scripted.sent).endswith(b"\x00\x00\x05\x00\x1e\x00\x22\x04\x00")


def test_typed_address_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """类型与地址单位不匹配直接抛 ValueError(参数校验约定)。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    monkeypatch.setattr(client, "_create_transport", lambda: ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.read_bool("D0100")  # 字软元件布尔读写不支持
    with pytest.raises(ValueError):
        client.write_bool("D0100", True)
    with pytest.raises(ValueError):
        client.read_ushort("D0100L")  # 字节地址不能做数值访问
    with pytest.raises(ValueError):
        client.write_double("M0201", 1.0)  # 位软元件无后缀为位访问


def test_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """出错代码(0x40 地址越界)→ DeviceError 不断线。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(
        _chunks(bytes((0x80, 0x10, 0x01, 0x00, 0x40))) + _chunks(_response(0x1C, b"\x14\x00"))
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D0100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "0x40" in client.last_error
    assert client.read_ushort("D0100") == (True, 20)


def test_bad_response_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """帧长不符的坏响应 → 标记断开等待惰性重连。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1C, b"\x00") + b"\x00"))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D0100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "帧长" in client.last_error


def test_command_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应命令字与请求不符 → 坏帧处理,标记断开。"""
    client = ToyopucTcpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport(_chunks(_response(0x1E, b"\x14\x00")))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D0100") == (False, None)
    assert client.connected is False


# ----------------------------------------------------------------------
# UDP 与异步
# ----------------------------------------------------------------------

def test_udp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:一请求一数据报,整包校验。"""
    client = ToyopucUdpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport([_response(0x1C, b"\x14\x00")], datagram=True)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D0100") == (True, 20)
    assert bytes(scripted.sent) == b"\x00\x00\x05\x00\x1c\x00\x11\x01\x00"


def test_udp_bit_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:位写一问一答。"""
    client = ToyopucUdpClient("127.0.0.1", 1025)
    scripted = ScriptedTransport([_response(0x21)], datagram=True)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("X0010", True) is True
    assert bytes(scripted.sent) == b"\x00\x00\x04\x00\x21\x10\x10\x01"


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返。"""
    import asyncio

    from omniplc.aio import AToyopucTcpClient

    async def scenario() -> None:
        client = AToyopucTcpClient("127.0.0.1", 1025)
        sync = client._sync
        scripted = ScriptedTransport(
            _chunks(_response(0x1C, b"\x14\x00")) + _chunks(_response(0x21))
        )
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("D0100") == (True, 20)
        assert await client.write_bool("M0201", True) is True
        await client.close()

    asyncio.run(scenario())
