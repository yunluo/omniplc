"""三菱 MC 串口客户端(3C/4C 帧)测试:手册 Appendix 7 黄金向量 + 全链路往返。

黄金向量取自 SH-080008《MELSEC Communication Protocol Reference Manual》
Appendix 7 设置示例(3C 格式 1 样例 + CR LF = 格式 4;4C 格式 5 原样),
覆盖:组帧逐字节断言、DLE 附加码、和校验、错误代码 DeviceError 不断线、
坏帧断线、字软元件位"读-改-写"。
"""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from omniplc import MelsecMcSerialClient, MelsecMcTcpClient, MelsecMcUdpClient
from omniplc.aio import AMelsecMcSerialClient
from omniplc.plc.melsec import codec_serial
from omniplc.plc.melsec.address import parse_mc_address
from omniplc.types import McFrame
from scripted import ScriptedTransport

_ROUTE = "0000FF00"
"""连接站默认路由文本:站号 00 + 网络号 00 + PC 号 FF + 本站号 00。"""


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------


def _stuff(payload: bytes) -> bytes:
    """DLE 附加码(与 codec_serial._stuff 同规则,测试本地实现)。"""
    out = bytearray()
    for value in payload:
        if value == 0x10:
            out.append(0x10)
        out.append(value)
    return bytes(out)


def _resp_3c(data_text: str, route: str = _ROUTE) -> bytes:
    """构造 3C 读响应(STX + 数据 + ETX + 和校验 + CR LF)。"""
    body = b"F9" + route.encode("ascii") + data_text.encode("ascii") + b"\x03"
    return (
        b"\x02"
        + body
        + "{:02X}".format(codec_serial.checksum(body)).encode("ascii")
        + b"\r\n"
    )


def _ack_3c(route: str = _ROUTE) -> bytes:
    """构造 3C 写正常响应(ACK + 路由 + CR LF)。"""
    return b"\x06F9" + route.encode("ascii") + b"\r\n"


def _nak_3c(code: str, route: str = _ROUTE) -> bytes:
    """构造 3C 异常响应(NAK + 路由 + 错误代码 + CR LF)。"""
    return b"\x15F9" + route.encode("ascii") + code.encode("ascii") + b"\r\n"


def _wire_4c(values: List[int], end_code: int = 0) -> bytes:
    """构造 4C 读/写响应线缆帧(values 为字数据,空列表 = 写响应)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    body = (
        b"\xf8"
        + bytes.fromhex("0000FFFF030000")
        + b"\xff\xff"
        + end_code.to_bytes(2, "little")
        + data
    )
    length = len(body)
    payload = _stuff(length.to_bytes(2, "little")) + _stuff(body)
    total = codec_serial.checksum(length.to_bytes(2, "little") + body)
    return (
        b"\x10\x02" + payload + b"\x10\x03" + "{:02X}".format(total).encode("ascii")
    )


def _chunks_4c(wire: bytes) -> List[bytes]:
    """按客户端 4C 收包步长切分线缆帧(recv 尺寸逐段对齐)。"""
    chunks: List[bytes] = [wire[0:2]]
    index = 2
    if wire[index] == 0x10:
        chunks += [wire[index:index + 1], wire[index + 1:index + 2], wire[index + 2:index + 3]]
        index += 3
    else:
        chunks += [wire[index:index + 1], wire[index + 1:index + 2]]
        index += 2
    chunks.append(wire[index:index + 1])
    index += 1
    body_end = len(wire) - 4
    while index < body_end:
        if wire[index] == 0x10:
            chunks.append(wire[index:index + 1])
            chunks.append(wire[index + 1:index + 2])
            index += 2
        else:
            chunks.append(wire[index:index + 1])
            index += 1
    chunks.append(wire[body_end:])
    return chunks


def _mount(monkeypatch: pytest.MonkeyPatch, client: object, scripted: ScriptedTransport) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


# ----------------------------------------------------------------------
# 手册 Appendix 7 黄金向量(逐字节)
# ----------------------------------------------------------------------


def test_3c_read_request_golden_vector() -> None:
    """3C 帧:读 M100 起 2 字,与手册设置示例逐字节一致(格式 1 + CR LF)。"""
    request = codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("M100"), 2, False, False
    )
    assert request == (
        b"\x05"
        + b"F9"
        + b"0000FF00"
        + b"0401"
        + b"0000"
        + b"M*"
        + b"000100"
        + b"0002"
        + b"0A"
        + b"\r\n"
    )


def test_3c_write_request_golden_vector() -> None:
    """3C 帧:写 M100 起 2 字(2347H/AB96H),与手册设置示例逐字节一致。"""
    request = codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("M100"), 2, False, True, [0x2347, 0xAB96]
    )
    assert request == (
        b"\x05"
        + b"F9"
        + b"0000FF00"
        + b"1401"
        + b"0000"
        + b"M*"
        + b"000100"
        + b"0002"
        + b"2347AB96"
        + b"CD"
        + b"\r\n"
    )


def test_3c_read_response_golden_vector() -> None:
    """3C 帧:手册读响应样例(ETX 在和校验之前,和校验范围含 ETX)。"""
    response = (
        b"\x02"
        + b"F9"
        + b"0000FF00"
        + b"12340002"
        + b"\x03"
        + b"BA"
        + b"\r\n"
    )
    assert codec_serial.parse_3c_response(response, 2, False, True) == [0x1234, 0x0002]


def test_4c_read_request_golden_vector() -> None:
    """4C 帧:读 M100 起 2 字,与手册格式 5 设置示例逐字节一致。"""
    request = codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("M100"), 2, False, False
    )
    assert request == bytes.fromhex(
        "1002"
        "1200"
        "F8"
        "0000FFFF030000"
        "0104"
        "0000"
        "640000"
        "90"
        "0200"
        "1003"
        "3036"
    )


def test_4c_write_request_golden_vector() -> None:
    """4C 帧:写 M100 起 2 字,与手册格式 5 设置示例逐字节一致。"""
    request = codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("M100"), 2, False, True,
        [0x2347, 0xAB96],
    )
    assert request == bytes.fromhex(
        "1002"
        "1600"
        "F8"
        "0000FFFF030000"
        "0114"
        "0000"
        "640000"
        "90"
        "0200"
        "4723"
        "96AB"
        "1003"
        "4335"
    )


def test_4c_read_response_golden_vector() -> None:
    """4C 帧:手册读响应样例(数据长 10H 触发附加码,线缆层三字节)。"""
    wire = bytes.fromhex(
        "1002"
        "101000"
        "F8"
        "0000FFFF030000"
        "FFFF"
        "0000"
        "34120200"
        "1003"
        "3446"
    )
    assert _chunks_4c(wire)  # 收包步长切分自检:附加码长度域为 3 字节
    logical = b"\x10\x00" + wire[5:]
    assert codec_serial.parse_4c_response(logical, 2, False, True) == [0x1234, 0x0002]


def test_4c_bit_write_data_stuffed() -> None:
    """4C 帧:位写数据字节 10H(1 点 ON = 高半字节)必须附加码为 10 10。"""
    request = codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("M100"), 1, True, True, [1]
    )
    assert b"\x10\x10\x10\x03" in request  # 数据 10H 附加码 + DLE ETX
    assert request[:4] == b"\x10\x02\x13\x00"  # 数据长 19(0x13,未附加码计数)


# ----------------------------------------------------------------------
# 3C 客户端往返
# ----------------------------------------------------------------------


def test_3c_read_ushort_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:读请求组帧正确,STX 响应按控制码分流后解析出字数据。"""
    client = MelsecMcSerialClient()
    response = _resp_3c("2710")
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.configure_serial("COM3")
    client.connect()
    assert client.read_ushort("D100") == (True, 10000)
    assert bytes(scripted.sent) == codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("D100"), 1, False, False
    )


def test_3c_write_short_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:字写请求与 ACK 回包校验。"""
    client = MelsecMcSerialClient()
    response = _ack_3c()
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(scripted.sent) == codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("D100"), 1, False, True, [300]
    )


def test_3c_read_bool_bit_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:位软元件按位单位读,ASCII 数据每点 1 字符。"""
    client = MelsecMcSerialClient()
    response = _resp_3c("1")
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M100") == (True, True)
    request = bytes(scripted.sent).decode("ascii")
    assert "04010001M*0001000001" in request  # 子命令 0001 + 1 点


def test_3c_write_bool_bit_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:位软元件按位单位写,数据 1 字符。"""
    client = MelsecMcSerialClient()
    response = _ack_3c()
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("M100", True) is True
    request = bytes(scripted.sent).decode("ascii")
    assert request.startswith("\x05F90000FF0014010001M*00010000011")
    assert request.endswith("\r\n")


def test_3c_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:字软元件位写 = 读一字 → 改位 → 写一字两段事务。"""
    client = MelsecMcSerialClient()
    read_response = _resp_3c("0004")
    write_response = _ack_3c()
    scripted = ScriptedTransport(
        [read_response[:1], read_response[1:], write_response[:1], write_response[1:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("D100"), 1, False, False
    ) + codec_serial.build_3c_request(
        0, 0, 0xFF, 0, parse_mc_address("D100"), 1, False, True, [0x000C]
    )
    assert bytes(scripted.sent) == expected


def test_3c_hex_number_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:十六进制编号软元件(X/W/B)编号域按十六进制 6 位发送。"""
    client = MelsecMcSerialClient()
    response = _resp_3c("1")
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("X17") == (True, True)
    request = bytes(scripted.sent).decode("ascii")
    assert "X*000017" in request  # X17 = 十六进制 0x17


def test_3c_nak_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:NAK 错误代码 → DeviceError,不断线。"""
    client = MelsecMcSerialClient()
    response = _nak_3c("7151")
    scripted = ScriptedTransport([response[:1], response[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "0x7151" in client.last_error


def test_3c_bad_checksum_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:和校验不符按坏帧处理,标记断开。"""
    client = MelsecMcSerialClient()
    response = _resp_3c("2710")
    corrupted = response[:-3] + b"XX" + b"\r\n"
    scripted = ScriptedTransport([corrupted[:1], corrupted[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False


def test_3c_missing_crlf_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:写响应缺 CR LF 按坏帧处理,标记断开。"""
    client = MelsecMcSerialClient()
    response = _ack_3c()
    truncated = response[:-2]
    scripted = ScriptedTransport([truncated[:1], truncated[1:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("D100", 1) is False
    assert client.connected is False


def test_3c_bad_control_code_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:响应控制码非法(非 STX/ACK/NAK)按坏帧处理。"""
    client = MelsecMcSerialClient()
    scripted = ScriptedTransport([b"\x00"])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "控制码" in client.last_error


# ----------------------------------------------------------------------
# 4C 客户端往返
# ----------------------------------------------------------------------


def test_4c_length_field_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:长度域超限(0xFFFF)在收包前快失败,按坏帧断线。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    evil = bytes([codec_serial.DLE, codec_serial.STX, 0xFF, 0xFF])
    scripted = ScriptedTransport([evil[:2], evil[2:3], evil[3:]])
    _mount(monkeypatch, client, scripted)
    client.configure_serial("COM3")
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "超限" in client.last_error


def test_4c_read_ushort_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:读请求组帧正确,响应按长度域分段收包并解析。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([10000])
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.configure_serial("COM3")
    client.connect()
    assert client.read_ushort("D100") == (True, 10000)
    assert bytes(scripted.sent) == codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("D100"), 1, False, False
    )


def test_4c_write_short_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:字写请求与写响应(无数据)校验。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([])
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(scripted.sent) == codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("D100"), 1, False, True, [300]
    )


def test_4c_read_int_length_field_stuffed(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:两字读的应答数据长 = 10H,长度域附加码后线缆为三字节。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([0x1234, 0x0000])
    assert wire[2:5] == b"\x10\x10\x00"  # 长度 0010H → 10 10 00
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("D100") == (True, 0x1234)


def test_4c_response_data_stuffed(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:响应数据含 10H 时附加码还原后数值正确。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([0x1010])
    assert b"\x10\x10\x10\x10" in wire  # 两字节 10 10 → 10 10 10 10
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 0x1010)


def test_4c_read_bool_bit_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:位软元件按位单位读,数据每点 1 个半字节(高半字节在前)。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    # 1 点位读:数据 1 字节 10H(ON)
    data = b"\x10"
    body = (
        b"\xf8"
        + bytes.fromhex("0000FFFF030000")
        + b"\xff\xff"
        + b"\x00\x00"
        + data
    )
    length = len(body)
    payload = _stuff(length.to_bytes(2, "little")) + _stuff(body)
    total = codec_serial.checksum(length.to_bytes(2, "little") + body)
    wire = (
        b"\x10\x02" + payload + b"\x10\x03" + "{:02X}".format(total).encode("ascii")
    )
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M100") == (True, True)


def test_4c_write_bool_bit_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:位软元件按位单位写(数据 10H 附加码),ACK 响应。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([])
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("M100", True) is True
    request = bytes(scripted.sent)
    assert b"\x10\x10\x10\x03" in request  # 写数据 10H 附加码 + DLE ETX


def test_4c_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:字软元件位写 = 读一字 → 改位 → 写一字两段事务。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    read_wire = _wire_4c([0x0004])
    write_wire = _wire_4c([])
    scripted = ScriptedTransport(_chunks_4c(read_wire) + _chunks_4c(write_wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("D100"), 1, False, False
    ) + codec_serial.build_4c_request(
        0, 0, 0xFF, 0x03FF, 0, 0, parse_mc_address("D100"), 1, False, True, [0x000C]
    )
    assert bytes(scripted.sent) == expected


def test_4c_end_code_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:结束代码非 0 → DeviceError,不断线。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([], end_code=0xC059)
    scripted = ScriptedTransport(_chunks_4c(wire))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "0xC059" in client.last_error


def test_4c_bad_checksum_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:和校验不符按坏帧处理,标记断开。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = _wire_4c([10000])
    corrupted = wire[:-2] + b"XX"
    scripted = ScriptedTransport(_chunks_4c(corrupted))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False


def test_4c_bad_frame_id_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """4C:响应帧识别码非 F8H 按坏帧处理,标记断开。"""
    client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
    wire = bytearray(_wire_4c([10000]))
    wire[4] = 0xF9  # 帧识别码位置(DLE STX 2 + 长度域 2 之后)
    scripted = ScriptedTransport(_chunks_4c(bytes(wire)))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "帧识别码" in client.last_error


# ----------------------------------------------------------------------
# 参数校验与走线约束
# ----------------------------------------------------------------------


def test_frame_walkline_constraints() -> None:
    """帧型与走线约束:TCP/UDP 不接受 3C/4C,串口不接受 3E。"""
    with pytest.raises(ValueError):
        MelsecMcTcpClient("127.0.0.1", 2000, frame="3C")
    with pytest.raises(ValueError):
        MelsecMcUdpClient("127.0.0.1", 2000, frame="4C")
    with pytest.raises(ValueError):
        MelsecMcSerialClient(frame="3E")


def test_route_parameter_validation() -> None:
    """路由参数校验:站号/PC 编号/目标模块 I/O 越界拒绝。"""
    with pytest.raises(ValueError):
        MelsecMcSerialClient(station_number=32)
    with pytest.raises(ValueError):
        MelsecMcSerialClient(pc_number=4)
    with pytest.raises(ValueError):
        MelsecMcSerialClient(module_io=0x10000)
    client = MelsecMcSerialClient(station_number=31, pc_number=3)
    assert client.station_number == 31 and client.pc_number == 3


def test_create_transport_requires_serial_config() -> None:
    """未配置串口参数时创建传输必须报错。"""
    client = MelsecMcSerialClient()
    with pytest.raises(ValueError):
        client._create_transport()


def test_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """3C:字符串读写按字软元件小端字序拼解码。"""
    client = MelsecMcSerialClient()
    encoded = "AB".encode("ascii")
    words = int.from_bytes(encoded, "little")
    read_response = _resp_3c("{:04X}".format(words))
    write_response = _ack_3c()
    scripted = ScriptedTransport(
        [write_response[:1], write_response[1:], read_response[:1], read_response[1:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_string("D100", "AB") is True
    assert client.read_string("D100", 2) == (True, "AB")


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:串口客户端单工作线程往返与 configure_serial 转发。"""

    async def scenario() -> None:
        client = AMelsecMcSerialClient(frame=McFrame.FRAME_4C)
        client.configure_serial("COM3", 9600)
        sync = client._sync
        if not isinstance(sync, MelsecMcSerialClient):
            raise TypeError("内部错误:sync 实例不是 MelsecMcSerialClient")
        assert sync.frame == McFrame.FRAME_4C
        assert sync.station_number == 0
        wire = _wire_4c([10000])
        scripted = ScriptedTransport(_chunks_4c(wire))
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("D100") == (True, 10000)
        assert client.frame == McFrame.FRAME_4C
        await client.close()

    asyncio.run(scenario())
