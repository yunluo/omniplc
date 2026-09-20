"""FINS 客户端帧收发测试:脚本化传输验证 TCP(握手)与 UDP 全链路。"""
from __future__ import annotations

import pytest

from omniplc import OmronFinsTcpClient, OmronFinsUdpClient
from omniplc.plc.omron import codec
from omniplc.plc.omron.address import parse_fins_address
from scripted import ScriptedTransport

_FINS_ECHO_HEAD = b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00"


def _fins_read_response(values: list) -> bytes:
    """构造区域读响应(测试脚手架,回显节点与 SID=1)。"""
    data = b"".join(value.to_bytes(2, "big") for value in values)
    return _FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + b"\x00\x00" + data


def _fins_write_response() -> bytes:
    """构造区域写响应(测试脚手架,SID=2)。"""
    return b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00\x02" + b"\x01\x02" + b"\x00\x00"


def _fins_error_response(end_code: int) -> bytes:
    """构造带结束码的读响应(测试脚手架)。"""
    return _FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + end_code.to_bytes(2, "big")


def _handshake_response() -> bytes:
    """构造握手响应(本地节点 11,PLC 节点 5)。"""
    return (
        b"FINS"
        + (16).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00\x0b"
        + b"\x00\x00\x00\x05"
    )


def _tcp_wrap(fins_frame: bytes) -> bytes:
    """FINS/TCP 封帧(测试脚手架)。"""
    body = (2).to_bytes(4, "big") + (0).to_bytes(4, "big") + fins_frame
    return b"FINS" + len(body).to_bytes(4, "big") + body


def test_udp_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:请求帧组帧正确(节点/SID 进帧),响应解析出字数据。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    expected = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("D100"), 1, False
    )
    assert bytes(scripted.sent) == expected


def test_tcp_handshake_and_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:握手 → 节点自动补齐(SA1/DA1)→ 读事务。"""
    client = OmronFinsTcpClient("127.0.0.1")
    hs = _handshake_response()
    tx = _tcp_wrap(_fins_read_response([20]))
    scripted = ScriptedTransport([hs[:8], hs[8:], tx[:8], tx[8:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.local_node == 11
    assert client.read_ushort("D100") == (True, 20)
    sent = bytes(scripted.sent)
    assert sent[:20] == codec.build_handshake(0)
    fins_request = sent[20 + 16:]  # 跳过握手请求(20)与 FINS/TCP 头(16)
    assert fins_request[4] == 5  # DA1 = 握手分配的 PLC 节点
    assert fins_request[7] == 11  # SA1 = 握手分配的本地节点


def test_udp_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:D 区位写 = 读字 →改位→ 写字两段事务。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([0x0004]), _fins_write_response()])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("D100"), 1, False
    ) + codec.build_area_write(
        0, 5, 0, 0, 10, 0, 2, parse_fins_address("D100"), [0x000C], False
    )
    assert bytes(scripted.sent) == expected


def test_udp_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:结束码非 0 → DeviceError,不断线。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_error_response(1)])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "结束码 0x0001" in client.last_error


def test_udp_timer_counter_word_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:T/C 区:字访问为当前值 PV(操作码 0x89),可读写。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([150]), _fins_write_response()])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("T0") == (True, 150)
    expected_read = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("T0"), 1, False
    )
    assert bytes(scripted.sent[: len(expected_read)]) == expected_read
    assert bytes(scripted.sent)[12] == 0x89  # T/C 字操作码
    assert client.write_ushort("C10", 200) is True
    expected_write = codec.build_area_write(
        0, 5, 0, 0, 10, 0, 2, parse_fins_address("C10"), [200], False
    )
    assert bytes(scripted.sent)[len(expected_read):] == expected_write


def test_udp_timer_flag_read_and_write_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:T/C 完成标志:位读(操作码 0x09)可用;位写与位号后缀拒绝。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + b"\x00\x00" + b"\x01"])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_bool("T0") == (True, True)
    flag_read = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("T0"), 1, True
    )
    assert bytes(scripted.sent) == flag_read
    assert bytes(scripted.sent)[12] == 0x09
    with pytest.raises(ValueError):
        client.write_bool("T0", True)  # 完成标志只读
    with pytest.raises(ValueError):
        client.read_bool("T0.3")  # 不带位号
    with pytest.raises(ValueError):
        client.write_bool("C10.2", True)


def test_fins_end_code_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:全表结束码文本进 last_error(0x1101 存储区码非法)。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_error_response(0x1101)])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.last_error is not None and "存储区码非法" in client.last_error


def test_tcp_bad_magic_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:响应魔数非法按坏帧处理,标记断开。"""
    client = OmronFinsTcpClient("127.0.0.1")
    hs = _handshake_response()
    scripted = ScriptedTransport([hs[:8], hs[8:], b"\x00" * 8])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "帧头非法" in client.last_error
