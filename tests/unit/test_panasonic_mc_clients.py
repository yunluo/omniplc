"""松下 MC 协议兼容客户端测试:帧复用三菱 3E,验证松下记号换算。

覆盖:码表(与三菱 Q/L 同码)、字号+位号 → 线性编号(R1F→31)、
R≥900 字号映射 SM(R9005→SM5)、D≥90000 映射 SD、TN/CN/TS/CS/LD 字软元件、
三菱专有记号拒绝、位写、字软元件位写读-改-写、异步镜像。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from omniplc import PanasonicMcTcpClient
from omniplc.aio import APanasonicMcTcpClient
from omniplc.core.constants import (
    MC_DEFAULT_MONITOR_TIMER,
    PANASONIC_MC_DEFAULT_PORT,
    PANASONIC_MC_DEVICE_CODES,
    PANASONIC_MC_SD_BASE,
    PANASONIC_MC_SM_LINEAR_BASE,
)
from omniplc.plc.melsec import codec_qna
from omniplc.plc.melsec.address import parse_mc_address
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
    monkeypatch: pytest.MonkeyPatch, client: PanasonicMcTcpClient, scripted: ScriptedTransport
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
    """按松下码表与换算后的三菱记号构造期望请求帧。"""
    return codec_qna.build_request(
        "3E", serial, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address(address), points, is_bit, is_write, data,
        PANASONIC_MC_DEVICE_CODES,
    )


def test_table_codes() -> None:
    """码表本身:与三菱 Q/L 同码;SM/SD 分界常量。"""
    assert PANASONIC_MC_DEVICE_CODES["X"] == (0x9C, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["Y"] == (0x9D, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["L"] == (0xA0, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["R"] == (0x90, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["SM"] == (0x91, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["TS"] == (0xC1, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["CS"] == (0xC4, 1, 10)
    assert PANASONIC_MC_DEVICE_CODES["D"] == (0xA8, 0, 10)
    assert PANASONIC_MC_DEVICE_CODES["LD"] == (0xB4, 0, 10)
    assert PANASONIC_MC_DEVICE_CODES["SD"] == (0xA9, 0, 10)
    assert PANASONIC_MC_DEVICE_CODES["TN"] == (0xC2, 0, 10)
    assert PANASONIC_MC_DEVICE_CODES["CN"] == (0xC5, 0, 10)
    assert PANASONIC_MC_SM_LINEAR_BASE == 14400
    assert PANASONIC_MC_SD_BASE == 90000


def test_defaults_and_frame_fixed_3e() -> None:
    """默认端口 2000(三菱惯例占位)、帧型固定 3E。"""
    client = PanasonicMcTcpClient()
    assert client._ip_address == "192.168.0.10"
    assert client._port == PANASONIC_MC_DEFAULT_PORT == 2000
    assert client.frame is McFrame.FRAME_3E


def test_read_r_word_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """R000F 字号+位号:线性编号 字号×16+位号,R1F → 31,码 90h。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    frame = _bit_read_response([1])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R1F") == (True, True)
    sent = bytes(scripted.sent)
    assert sent == _expected("R31", 1, True, False)
    assert sent[15:18] == b"\x1f\x00\x00"  # 1×16 + F = 31
    assert sent[18] == 0x90


def test_read_dot_form_and_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """点号形式 R2.11 → 线性 43;D100 字读码 A8h。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    bit_frame = _bit_read_response([0])
    word_frame = _word_read_response([1234])
    scripted = ScriptedTransport([bit_frame[:9], bit_frame[9:], word_frame[:9], word_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R2.11") == (True, False)
    assert client.read_ushort("D100") == (True, 1234)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("R43", 1, True, False)
    assert sent[15:18] == b"\x2b\x00\x00"  # 2×16 + 11 = 43
    assert sent[21:] == _expected("D100", 1, False, False, serial=2)
    assert sent[36:39] == b"\x64\x00\x00" and sent[39] == 0xA8


def test_r_maps_to_sm(monkeypatch: pytest.MonkeyPatch) -> None:
    """R 字号 ≥900 映射 SM:R9005 → SM5(码 91h);SM 直访同理。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    mapped_frame = _bit_read_response([1])
    direct_frame = _bit_read_response([0])
    scripted = ScriptedTransport(
        [mapped_frame[:9], mapped_frame[9:], direct_frame[:9], direct_frame[9:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R9005") == (True, True)
    assert client.read_bool("SM10") == (True, False)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("SM5", 1, True, False)
    assert sent[15:18] == b"\x05\x00\x00"
    assert sent[18] == 0x91
    assert sent[21:] == _expected("SM10", 1, True, False, serial=2)
    assert sent[36:39] == b"\x0a\x00\x00"
    assert sent[39] == 0x91


def test_d_maps_to_sd(monkeypatch: pytest.MonkeyPatch) -> None:
    """D 编号 ≥90000 映射 SD:D90000 → SD0、D90010 → SD10,码 A9h。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    first = _word_read_response([7])
    second = _word_read_response([8])
    scripted = ScriptedTransport([first[:9], first[9:], second[:9], second[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D90000") == (True, 7)
    assert client.read_ushort("D90010") == (True, 8)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("SD0", 1, False, False)
    assert sent[15:18] == b"\x00\x00\x00" and sent[18] == 0xA9
    assert sent[21:] == _expected("SD10", 1, False, False, serial=2)
    assert sent[36:39] == b"\x0a\x00\x00" and sent[39] == 0xA9


def test_word_devices_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """TN/CN 字与 TS/CS 位:纯十进制编号,码 C2h/C5h/C1h/C4h。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    tn_frame = _word_read_response([100])
    cs_frame = _bit_read_response([1])
    scripted = ScriptedTransport([tn_frame[:9], tn_frame[9:], cs_frame[:9], cs_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("TN5") == (True, 100)
    assert client.read_bool("CS3") == (True, True)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("TN5", 1, False, False)
    assert sent[15:18] == b"\x05\x00\x00" and sent[18] == 0xC2
    assert sent[21:] == _expected("CS3", 1, True, False, serial=2)
    assert sent[36:39] == b"\x03\x00\x00" and sent[39] == 0xC4


def test_invalid_address_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺字号(R0)与非十进制字号(RA1):参数错误抛 ValueError,不发报文。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_bool("R0")
    assert "字号" in str(exc_info.value)
    with pytest.raises(ValueError) as exc_info:
        client.read_bool("RA1")
    assert "不支持的 MC 软元件" in str(exc_info.value)
    assert bytes(scripted.sent) == b""


def test_melsec_only_names_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """三菱记号(ZR/M)不在松下码表:参数错误抛 ValueError。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_ushort("ZR100")
    assert "不支持的 MC 软元件" in str(exc_info.value)
    with pytest.raises(ValueError):
        client.read_bool("M100")
    assert bytes(scripted.sent) == b""


def test_write_bool_and_word_bit_rmw(monkeypatch: pytest.MonkeyPatch) -> None:
    """X000F 位写(数据 1 打包高半字节);D100.3 位写走读-改-写两帧。"""
    client = PanasonicMcTcpClient("127.0.0.1", 2000)
    write_frame = _write_response()
    read_frame = _word_read_response([0x0004])
    write2_frame = _write_response()
    scripted = ScriptedTransport(
        [write_frame[:9], write_frame[9:], read_frame[:9], read_frame[9:],
         write2_frame[:9], write2_frame[9:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("X000F", True) is True
    assert client.write_bool("D100.3", True) is True
    sent = bytes(scripted.sent)
    assert sent[:22] == _expected("X15", 1, True, True, data=[1])
    assert sent[18] == 0x9C
    assert sent[21] == 0x10
    assert sent[22:43] == _expected("D100", 1, False, False, serial=2)
    assert sent[43:] == _expected("D100", 1, False, True, serial=3, data=[0x000C])


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返。"""

    async def scenario() -> None:
        client = APanasonicMcTcpClient("127.0.0.1", 2000)
        assert client.frame is McFrame.FRAME_3E
        sync = client._sync
        frame = _word_read_response([20])
        write_frame = _write_response()
        scripted = ScriptedTransport([frame[:9], frame[9:], write_frame[:9], write_frame[9:]])
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("D100") == (True, 20)
        assert await client.write_short("D100", -5) is True
        await client.close()

    asyncio.run(scenario())
