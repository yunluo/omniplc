"""MC 客户端帧收发测试:脚本化传输验证 TCP/UDP × 3E/4E/1E 全链路。

覆盖:组帧逐字节断言、按长收包、序列号校验、结束码 DeviceError 不断线、
坏帧断线重连、寄存器位"读-改-写"。
"""
from __future__ import annotations

import pytest

from omniplc import MelsecMcTcpClient, MelsecMcUdpClient
from omniplc.core.constants import MC_DEFAULT_MONITOR_TIMER
from omniplc.plc.melsec import codec_a, codec_qna
from omniplc.plc.melsec.address import parse_mc_address
from scripted import ScriptedTransport


def _qna_read_response(values: list, frame: str = "3E", serial: int = 0, end_code: int = 0) -> bytes:
    """构造 QnA 读响应(测试脚手架)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    if frame == "4E":
        head = b"\xd4\x00" + serial.to_bytes(2, "little") + b"\x00\x00" + b"\x00\xff\xff\x03\x00"
    else:
        head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + end_code.to_bytes(2, "little") + data


def _qna_write_response(frame: str = "3E", serial: int = 0) -> bytes:
    """构造 QnA 写响应(测试脚手架)。"""
    return _qna_read_response([], frame=frame, serial=serial)


def _one_e_read_response(values: list) -> bytes:
    """构造 1E 字读响应(测试脚手架)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    return bytes([0x81, 0x00]) + data


def _one_e_write_response() -> bytes:
    """构造 1E 字写响应(测试脚手架)。"""
    return bytes([0x83, 0x00])


def _mount(monkeypatch: pytest.MonkeyPatch, client: object, scripted: ScriptedTransport) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_tcp_3e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:请求组帧正确,响应解析出字数据。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([20])
    _mount(monkeypatch, client, ScriptedTransport([frame[:9], frame[9:]]))
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(client._transport.sent) == codec_qna.build_request(  # type: ignore[union-attr]
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_4e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 4E:13 字节头分段收包,序列号回包校验。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="4E")
    frame = _qna_read_response([20], frame="4E", serial=1)
    _mount(monkeypatch, client, ScriptedTransport([frame[:13], frame[13:]]))
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(client._transport.sent) == codec_qna.build_request(  # type: ignore[union-attr]
        "4E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_1e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:2 字节头 + 按点数收数据。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    frame = _one_e_read_response([20])
    _mount(monkeypatch, client, ScriptedTransport([frame[:2], frame[2:]]))
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(client._transport.sent) == codec_a.build_request(  # type: ignore[union-attr]
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_3e_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:字写请求与回包校验。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    response = _qna_write_response()
    _mount(monkeypatch, client, ScriptedTransport([response[:9], response[9:]]))
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(client._transport.sent) == codec_qna.build_request(  # type: ignore[union-attr]
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"),
        1, False, True, [300],
    )


def test_tcp_1e_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:写响应仅 2 字节(副头部+结束码)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    _mount(monkeypatch, client, ScriptedTransport([_one_e_write_response()]))
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(client._transport.sent) == codec_a.build_request(  # type: ignore[union-attr]
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, True, [300]
    )


def test_tcp_3e_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:结束代码非 0 → DeviceError,不断线。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([0], end_code=0xC059)
    _mount(monkeypatch, client, ScriptedTransport([frame[:9], frame[9:]]))
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "结束代码 0xC059" in client.last_error


def test_tcp_bad_subheader_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:响应副头部非法按坏帧处理,标记断开。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    bad = b"\x00\x00" + _qna_read_response([20])[2:]
    _mount(monkeypatch, client, ScriptedTransport([bad[:9], bad[9:]]))
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "副头部" in client.last_error


def test_tcp_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:字软元件位写 = 读(FC0104)→改位→写(0114)两段事务。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    read_frame = _qna_read_response([0x0004])
    write_frame = _qna_write_response()
    _mount(
        monkeypatch,
        client,
        ScriptedTransport(
            [read_frame[:9], read_frame[9:], write_frame[:9], write_frame[9:]]
        ),
    )
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    ) + codec_qna.build_request(
        "3E", 2, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"),
        1, False, True, [0x000C],
    )
    assert bytes(client._transport.sent) == expected  # type: ignore[union-attr]


def test_udp_3e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP 3E:一问一答一数据报,整包解析。"""
    client = MelsecMcUdpClient("127.0.0.1", 2000)
    frame = _qna_read_response([20])
    _mount(monkeypatch, client, ScriptedTransport([frame], datagram=True))
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(client._transport.sent) == codec_qna.build_request(  # type: ignore[union-attr]
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )
