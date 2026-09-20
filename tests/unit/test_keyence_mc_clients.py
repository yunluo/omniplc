"""基恩士 MC 协议兼容(SLMP)客户端测试:帧复用三菱 3E,仅软元件码不同。

覆盖:码表换码(R=90h/DM=A8h/B=A0h/W=B4h/ZR=B0h)、进制(R/DM/ZR 十进制,
B/W 十六进制)、三菱记号拒绝、位写、字软元件位写读-改-写、TCP/UDP 两走线
(UDP 一问一答一数据报)、异步镜像。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Union

import pytest

from omniplc import KeyenceMcTcpClient, KeyenceMcUdpClient
from omniplc.aio import AKeyenceMcTcpClient, AKeyenceMcUdpClient
from omniplc.core.constants import (
    KEYENCE_MC_DEFAULT_PORT,
    KEYENCE_MC_DEVICE_CODES,
    MC_DEFAULT_MONITOR_TIMER,
)
from omniplc.plc.melsec import codec_qna
from omniplc.plc.melsec.address import parse_mc_address
from omniplc.transport import UdpTransport
from omniplc.types import McFrame
from scripted import ScriptedTransport


def _word_read_response(values: list) -> bytes:
    """构造 3E 字读响应(测试脚手架)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    return _frame_tail(data)


def _bit_read_response(flags: list) -> bytes:
    """构造 3E 位读响应(每字节 2 位,高位在前)。"""
    packed = bytearray((len(flags) + 1) // 2)
    for index, flag in enumerate(flags):
        if flag:
            packed[index // 2] |= 0x10 if index % 2 == 0 else 0x01
    return _frame_tail(bytes(packed))


def _write_response() -> bytes:
    """构造 3E 写响应(仅结束码)。"""
    return _frame_tail(b"")


def _frame_tail(data: bytes) -> bytes:
    head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + (0).to_bytes(2, "little") + data


def _mount(
    monkeypatch: pytest.MonkeyPatch,
    client: Union[KeyenceMcTcpClient, KeyenceMcUdpClient],
    scripted: ScriptedTransport,
) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def _expected(
    address: str,
    points: int,
    is_bit: bool,
    is_write: bool,
    serial: int = 1,
    data: Optional[List[int]] = None,
) -> bytes:
    """按基恩士码表构造期望请求帧。"""
    return codec_qna.build_request(
        "3E", serial, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address(address), points, is_bit, is_write, data,
        KEYENCE_MC_DEVICE_CODES,
    )


def test_table_codes() -> None:
    """码表本身:五个软元件的码/位属性/进制。"""
    assert KEYENCE_MC_DEVICE_CODES["R"] == (0x90, 1, 10)
    assert KEYENCE_MC_DEVICE_CODES["B"] == (0xA0, 1, 16)
    assert KEYENCE_MC_DEVICE_CODES["W"] == (0xB4, 0, 16)
    assert KEYENCE_MC_DEVICE_CODES["DM"] == (0xA8, 0, 10)
    assert KEYENCE_MC_DEVICE_CODES["ZR"] == (0xB0, 0, 10)
    assert codec_qna.device_info("DM", KEYENCE_MC_DEVICE_CODES) == (0xA8, False, 10)


def test_defaults_and_frame_fixed_3e() -> None:
    """默认端口 5000、帧型固定 3E。"""
    client = KeyenceMcTcpClient()
    assert client._ip_address == "192.168.1.22"
    assert client._port == KEYENCE_MC_DEFAULT_PORT == 5000
    assert client.frame is McFrame.FRAME_3E


def test_read_dm100_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """DM100 字读:软元件码 A8h、编号 100 十进制、按长收包往返。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    frame = _word_read_response([20])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("DM100") == (True, 20)
    sent = bytes(scripted.sent)
    assert sent == _expected("DM100", 1, False, False)
    assert sent[15:18] == b"\x64\x00\x00"
    assert sent[18] == 0xA8
    assert sent[13:15] == b"\x00\x00"


def test_read_bool_r5_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5 位读:软元件码 90h、位单位子命令、点位往返。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    frame = _bit_read_response([1])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R5") == (True, True)
    sent = bytes(scripted.sent)
    assert sent == _expected("R5", 1, True, False)
    assert sent[15:18] == b"\x05\x00\x00"
    assert sent[18] == 0x90
    assert sent[13:15] == b"\x01\x00"


def test_write_dm100(monkeypatch: pytest.MonkeyPatch) -> None:
    """DM100 写 int16:-5 编码 FFFB 小端,写回包仅校验结束码。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    response = _write_response()
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("DM100", -5) is True
    assert bytes(scripted.sent) == _expected("DM100", 1, False, True, data=[0xFFFB])


def test_hex_addressing_b_w(monkeypatch: pytest.MonkeyPatch) -> None:
    """B/W 十六进制编号:B1F→0x1F、W10→0x10,码 A0h/B4h。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    bit_frame = _bit_read_response([0])
    word_frame = _word_read_response([1234])
    scripted = ScriptedTransport([bit_frame[:9], bit_frame[9:], word_frame[:9], word_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("B1F") == (True, False)
    assert client.read_ushort("W10") == (True, 1234)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("B1F", 1, True, False)
    assert sent[21:] == _expected("W10", 1, False, False, serial=2)
    assert sent[15:18] == b"\x1f\x00\x00" and sent[18] == 0xA0
    assert sent[36:39] == b"\x10\x00\x00" and sent[39] == 0xB4


def test_zr_word_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """ZR100 文件寄存器字读:码 B0h、十进制编号。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    frame = _word_read_response([7])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("ZR100") == (True, 7)
    sent = bytes(scripted.sent)
    assert sent[15:18] == b"\x64\x00\x00" and sent[18] == 0xB0


def test_write_bool_r5(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5 位写:位单位写帧,数据 1 打包进高半字节。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    response = _write_response()
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("R5", True) is True
    sent = bytes(scripted.sent)
    assert sent == _expected("R5", 1, True, True, data=[1])
    assert sent[18] == 0x90
    assert sent[21] == 0x10


def test_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """DM100.3 位写:字软元件走读-改-写两帧,序列号递增。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    read_frame = _word_read_response([0x0004])
    write_frame = _write_response()
    scripted = ScriptedTransport([read_frame[:9], read_frame[9:], write_frame[:9], write_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("DM100.3", True) is True
    expected = _expected("DM100", 1, False, False) + _expected(
        "DM100", 1, False, True, serial=2, data=[0x000C]
    )
    assert bytes(scripted.sent) == expected


def test_mitsubishi_names_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """三菱记号(D/M)不在基恩士码表:参数错误抛 ValueError。"""
    client = KeyenceMcTcpClient("127.0.0.1", 5000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_ushort("M100")
    assert "不支持的 MC 软元件" in str(exc_info.value)
    with pytest.raises(ValueError):
        client.read_ushort("D100")
    assert bytes(scripted.sent) == b""


def test_udp_defaults_and_transport() -> None:
    """UDP 版:默认端口 5000、帧型固定 3E、走线为 UdpTransport。"""
    client = KeyenceMcUdpClient()
    assert client._ip_address == "192.168.1.22"
    assert client._port == KEYENCE_MC_DEFAULT_PORT == 5000
    assert client.frame is McFrame.FRAME_3E
    assert isinstance(client._create_transport(), UdpTransport)


def test_udp_read_dm100_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP DM100 字读:一问一答一数据报,应答整包解析,帧与 TCP 版一致。"""
    client = KeyenceMcUdpClient("127.0.0.1", 5000)
    frame = _word_read_response([20])
    scripted = ScriptedTransport([frame], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("DM100") == (True, 20)
    sent = bytes(scripted.sent)
    assert sent == _expected("DM100", 1, False, False)
    assert sent[15:18] == b"\x64\x00\x00"
    assert sent[18] == 0xA8


def test_udp_write_dm100(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP DM100 写:请求帧与 TCP 版逐字节一致,写回包仅校验结束码。"""
    client = KeyenceMcUdpClient("127.0.0.1", 5000)
    scripted = ScriptedTransport([_write_response()], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("DM100", -5) is True
    assert bytes(scripted.sent) == _expected("DM100", 1, False, True, data=[0xFFFB])


def test_udp_read_bool_r5_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP R5 位读:码 90h、位单位子命令,一数据报应答。"""
    client = KeyenceMcUdpClient("127.0.0.1", 5000)
    scripted = ScriptedTransport([_bit_read_response([1])], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R5") == (True, True)
    sent = bytes(scripted.sent)
    assert sent == _expected("R5", 1, True, False)
    assert sent[18] == 0x90
    assert sent[13:15] == b"\x01\x00"


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返。"""

    async def scenario() -> None:
        client = AKeyenceMcTcpClient("127.0.0.1", 5000)
        assert client.frame is McFrame.FRAME_3E
        sync = client._sync
        frame = _word_read_response([20])
        write_frame = _write_response()
        scripted = ScriptedTransport([frame[:9], frame[9:], write_frame[:9], write_frame[9:]])
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("DM100") == (True, 20)
        assert await client.write_short("DM100", 20) is True
        await client.close()

    asyncio.run(scenario())


def test_async_mirror_udp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:UDP 客户端单工作线程往返,一问一答一数据报。"""

    async def scenario() -> None:
        client = AKeyenceMcUdpClient("127.0.0.1", 5000)
        assert client.frame is McFrame.FRAME_3E
        sync = client._sync
        frame = _word_read_response([20])
        write_frame = _write_response()
        scripted = ScriptedTransport([frame, write_frame], datagram=True)
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("DM100") == (True, 20)
        assert await client.write_short("DM100", 20) is True
        await client.close()

    asyncio.run(scenario())
