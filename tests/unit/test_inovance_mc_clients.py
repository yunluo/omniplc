"""汇川 MC 协议兼容客户端测试:帧复用三菱 3E,验证汇川记号换算。

覆盖:码表(S=三菱L码 92h)、R 与 D 统一编址(R100→D8100)、
X/Y 八进制命名转帧内十六进制(X17→0x0F)、X/Y 八进制非法拒绝、
三菱专有记号(ZR/SM)拒绝、位写、字软元件位写读-改-写、异步镜像。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

import pytest

from omniplc import InovanceMcTcpClient
from omniplc.aio import AInovanceMcTcpClient
from omniplc.core.constants import (
    INOVANCE_MC_DEFAULT_PORT,
    INOVANCE_MC_DEVICE_CODES,
    INOVANCE_MC_R_BASE,
    MC_DEFAULT_MONITOR_TIMER,
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
    monkeypatch: pytest.MonkeyPatch, client: InovanceMcTcpClient, scripted: ScriptedTransport
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
    """按汇川码表与换算后的三菱记号构造期望请求帧。"""
    return codec_qna.build_request(
        "3E", serial, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address(address), points, is_bit, is_write, data,
        INOVANCE_MC_DEVICE_CODES,
    )


def test_table_codes() -> None:
    """码表本身:S 用三菱 L 码 92h,X/Y 帧内十六进制,D/W 同三菱。"""
    assert INOVANCE_MC_DEVICE_CODES["X"] == (0x9C, 1, 16)
    assert INOVANCE_MC_DEVICE_CODES["Y"] == (0x9D, 1, 16)
    assert INOVANCE_MC_DEVICE_CODES["M"] == (0x90, 1, 10)
    assert INOVANCE_MC_DEVICE_CODES["S"] == (0x92, 1, 10)
    assert INOVANCE_MC_DEVICE_CODES["B"] == (0xA0, 1, 16)
    assert INOVANCE_MC_DEVICE_CODES["D"] == (0xA8, 0, 10)
    assert INOVANCE_MC_DEVICE_CODES["W"] == (0xB4, 0, 16)
    assert INOVANCE_MC_R_BASE == 8000
    assert "R" not in INOVANCE_MC_DEVICE_CODES


def test_defaults_and_frame_fixed_3e() -> None:
    """默认 IP/端口(H5U 出厂 IP + 沿用三菱惯例端口)、帧型固定 3E。"""
    client = InovanceMcTcpClient()
    assert client._ip_address == "192.168.1.88"
    assert client._port == INOVANCE_MC_DEFAULT_PORT == 2000
    assert client.frame is McFrame.FRAME_3E


def test_read_bool_m100(monkeypatch: pytest.MonkeyPatch) -> None:
    """M100 位读:码 90h、位单位子命令 01 00。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    frame = _bit_read_response([1])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M100") == (True, True)
    sent = bytes(scripted.sent)
    assert sent == _expected("M100", 1, True, False)
    assert sent[13:15] == b"\x01\x00"
    assert sent[15:18] == b"\x64\x00\x00"
    assert sent[18] == 0x90


def test_s_maps_to_l_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """S10 位读:按手册映射用三菱 L 码 92h,编号不变。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    frame = _bit_read_response([0])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("S10") == (True, False)
    sent = bytes(scripted.sent)
    assert sent[15:18] == b"\x0a\x00\x00"
    assert sent[18] == 0x92


def test_r_unified_with_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """R 与 D 统一编址:R100 → 帧 D8100(码 A8h、编号 8100),R200 写同理。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    read_frame = _word_read_response([1234])
    write_frame = _write_response()
    scripted = ScriptedTransport([read_frame[:9], read_frame[9:], write_frame[:9], write_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("R100") == (True, 1234)
    assert client.write_ushort("R200", 7) is True
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("D8100", 1, False, False)
    assert sent[15:18] == b"\xa4\x1f\x00"  # 8100 = 0x1FA4
    assert sent[18] == 0xA8
    assert sent[21:] == _expected("D8200", 1, False, True, serial=2, data=[7])
    assert sent[36:39] == b"\x08\x20\x00"  # 8200 = 0x2008


def test_xy_octal_to_hex_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """X/Y 八进制命名换算帧内十六进制:X17→0x0F、Y7→0x07。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    x_frame = _bit_read_response([1])
    y_frame = _bit_read_response([0])
    scripted = ScriptedTransport([x_frame[:9], x_frame[9:], y_frame[:9], y_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("X17") == (True, True)
    assert client.read_bool("Y7") == (True, False)
    sent = bytes(scripted.sent)
    assert sent[:21] == _expected("XF", 1, True, False)
    assert sent[15:18] == b"\x0f\x00\x00"  # 八进制 17 = 十进制 15
    assert sent[18] == 0x9C
    assert sent[21:] == _expected("Y7", 1, True, False, serial=2)
    assert sent[36:39] == b"\x07\x00\x00"
    assert sent[39] == 0x9D


def test_xy_invalid_octal_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """X/Y 编号含 8/9:八进制解析失败抛 ValueError,不发报文。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_bool("X38")
    assert "八进制" in str(exc_info.value)
    with pytest.raises(ValueError):
        client.read_bool("Y8")
    assert bytes(scripted.sent) == b""


def test_write_bool_x5(monkeypatch: pytest.MonkeyPatch) -> None:
    """X5 位写:位单位写帧,数据 1 打包进高半字节,码 9Ch。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    response = _write_response()
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("X5", True) is True
    sent = bytes(scripted.sent)
    assert sent == _expected("X5", 1, True, True, data=[1])
    assert sent[18] == 0x9C
    assert sent[21] == 0x10


def test_r_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """R100.3 位写:换算后对 D8100 走读-改-写两帧,序列号递增。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    read_frame = _word_read_response([0x0004])
    write_frame = _write_response()
    scripted = ScriptedTransport([read_frame[:9], read_frame[9:], write_frame[:9], write_frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("R100.3", True) is True
    expected = _expected("D8100", 1, False, False) + _expected(
        "D8100", 1, False, True, serial=2, data=[0x000C]
    )
    assert bytes(scripted.sent) == expected


def test_melsec_only_names_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """三菱专有记号(ZR)与 MC 范围外的 SM:参数错误抛 ValueError。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_ushort("ZR100")
    assert "不支持的 MC 软元件" in str(exc_info.value)
    with pytest.raises(ValueError):
        client.read_bool("SM10")
    assert bytes(scripted.sent) == b""


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返(R 统一编址生效)。"""

    async def scenario() -> None:
        client = AInovanceMcTcpClient("127.0.0.1", 2000)
        assert client.frame is McFrame.FRAME_3E
        sync = client._sync
        frame = _word_read_response([20])
        write_frame = _write_response()
        scripted = ScriptedTransport([frame[:9], frame[9:], write_frame[:9], write_frame[9:]])
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("R100") == (True, 20)
        assert await client.write_short("D100", -5) is True
        await client.close()

    asyncio.run(scenario())


def test_read_batch_translates_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量读经地址换算钩子:R n → D(8000+n)、X 八进制命名 → 帧内十六进制。"""
    client = InovanceMcTcpClient("127.0.0.1", 2000)
    data = (5).to_bytes(2, "little") + (0x8000).to_bytes(2, "little")
    frame = _frame_tail(data)
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_batch([("R0", "short"), ("X17", "bool")]) == (True, [5, True])
    x_code = INOVANCE_MC_DEVICE_CODES["X"][0]
    assert bytes(scripted.sent) == codec_qna.build_random_read(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, INOVANCE_MC_R_BASE, 1)],
        [(x_code, 0x0F, 1)],
    )
