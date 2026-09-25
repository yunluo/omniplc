"""Modbus 报文编解码单测:FC 23(读写多寄存器)与 FC 43/14(读设备标识)。

帧级字节向量由 :mod:`tests.golden` 的黄金样本覆盖(独立实现生成);
本文件覆盖**构造期参数校验**与**响应解析的坏帧路径**(结构不符、
长度不自治、MEI 回显不符等),以及"起始地址 + 数量"地址空间校验。
"""
from __future__ import annotations

from typing import List

import pytest

from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.modbus import codec


# ----------------------------------------------------------------------
# 地址空间校验(规范 §6.1 状态图:Starting Address + Quantity)
# ----------------------------------------------------------------------

def test_read_span_over_address_space_rejected() -> None:
    """读请求:起始地址 + 数量 越界(hr65535 读 2 字)在构造期拒绝。"""
    with pytest.raises(ValueError) as exc_info:
        codec.build_read_pdu(3, 0xFFFF, 2)
    assert "地址空间" in exc_info.value.args[0]


def test_read_span_at_address_space_edge_accepted() -> None:
    """边界值:hr65534 读 2 字恰好占满地址空间顶端,合法。"""
    assert codec.build_read_pdu(3, 0xFFFE, 2) == bytes.fromhex("03fffe0002")
    assert codec.build_read_pdu(3, 0xFFFF, 1) == bytes.fromhex("03ffff0001")


def test_write_span_over_address_space_rejected() -> None:
    """写请求:FC15/16 同样校验起始地址 + 数量。"""
    with pytest.raises(ValueError):
        codec.build_write_multi_pdu(16, 0xFFFF, [1, 2])
    with pytest.raises(ValueError):
        codec.build_write_multi_pdu(15, 0xFFFF, [1, 1])
    # 边界合法
    assert len(codec.build_write_multi_pdu(16, 0xFFFF, [1])) > 0


# ----------------------------------------------------------------------
# FC 23 读写多寄存器
# ----------------------------------------------------------------------

def test_build_read_write_registers_pdu_bytes() -> None:
    """FC23 请求组帧:规范 §6.17 示例逐字节一致(读 6 @3、写 3 @14)。"""
    pdu = codec.build_read_write_registers_pdu(3, 6, 14, [0x00FF, 0x00FF, 0x00FF])
    assert pdu == bytes.fromhex("170003000" "60" "00e" "0003" "06" "00ff00ff00ff".replace(" ", ""))


def test_parse_read_write_registers_response() -> None:
    """FC23 响应解析:字节计数 2×读数量,数据逐字大端。"""
    pdu = bytes([0x17, 12]) + bytes.fromhex("00fe0acd0001000300 0d00ff".replace(" ", ""))
    assert codec.parse_read_write_registers_response(pdu, 6) == [
        0x00FE, 0x0ACD, 0x0001, 0x0003, 0x000D, 0x00FF,
    ]


def test_read_write_registers_validation() -> None:
    """FC23 构造校验:读数量 1~125、写数量 1~121(比 FC16 的 123 小 2)。"""
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 0, 0, [1])
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 126, 0, [1])
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 1, 0, [])
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 1, 0, [0] * 122)
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 1, 0, [0x10000])
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0xFFFF, 2, 0, [1])
    with pytest.raises(ValueError):
        codec.build_read_write_registers_pdu(0, 1, 0xFFFF, [1, 2])
    # 边界合法:读 125、写 121
    codec.build_read_write_registers_pdu(0, 125, 0, [0] * 121)


def test_read_write_registers_response_bad_frames() -> None:
    """FC23 响应坏帧:字节计数不符、PDU 长度不符 → ProtocolFrameError。"""
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_read_write_registers_response(bytes([0x17, 4, 0, 1]), 6)
    assert "长度不符" in exc_info.value.args[0]
    with pytest.raises(ProtocolFrameError):
        codec.parse_read_write_registers_response(bytes([0x17]), 1)
    with pytest.raises(ProtocolFrameError):
        codec.parse_read_write_registers_response(bytes([0x99, 0x02]), 1)


# ----------------------------------------------------------------------
# FC 43/14 读设备标识
# ----------------------------------------------------------------------

def test_build_device_id_pdu_bytes() -> None:
    """FC43 请求组帧:规范 §6.21 示例(基本标识、对象 0)。"""
    assert codec.build_device_id_pdu(0x01, 0x00) == bytes.fromhex("2b0e0100")


def test_build_device_id_pdu_validation() -> None:
    """FC43 构造校验:读取码 1~4、对象号 0~255。"""
    for code in (0x01, 0x02, 0x03, 0x04):
        assert len(codec.build_device_id_pdu(code, 0x00)) == 4
    with pytest.raises(ValueError):
        codec.build_device_id_pdu(0x00, 0x00)
    with pytest.raises(ValueError):
        codec.build_device_id_pdu(0x05, 0x00)
    with pytest.raises(ValueError):
        codec.build_device_id_pdu(0x01, 0x100)


def test_parse_device_id_response() -> None:
    """FC43 响应解析:符合级别/翻页标记/对象列表逐项还原。"""
    objects: List[tuple] = [(0x00, b"ACME"), (0x02, b"V1.0")]
    body = bytearray([0x2B, 0x0E, 0x01, 0x83, 0x00, 0x00, len(objects)])
    for object_id, raw in objects:
        body.append(object_id)
        body.append(len(raw))
        body += raw
    parsed = codec.parse_device_id_response(bytes(body))
    assert parsed.conformity_level == 0x83
    assert parsed.more_follows is False
    assert parsed.next_object_id == 0x00
    assert parsed.objects == objects


def test_parse_device_id_response_more_follows() -> None:
    """FC43 翻页标记:MoreFollows=0xFF 时回报后续对象号。"""
    body = bytes([0x2B, 0x0E, 0x01, 0x01, 0xFF, 0x02, 0x01, 0x00, 0x01]) + b"A"
    parsed = codec.parse_device_id_response(body)
    assert parsed.more_follows is True
    assert parsed.next_object_id == 0x02


def test_parse_device_id_response_bad_frames() -> None:
    """FC43 响应坏帧:头不足、MEI 回显不符、对象头/值截断、尾部多余字节。"""
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_device_id_response(bytes([0x2B, 0x0E, 0x01]))
    assert "不完整" in exc_info.value.args[0]

    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_device_id_response(bytes([0x2B, 0x0D, 0x01, 0x01, 0x00, 0x00, 0x00]))
    assert "MEI" in exc_info.value.args[0]

    # 声明 1 个对象但对象头被截断
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_device_id_response(bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 0x01]))
    assert "对象头被截断" in exc_info.value.args[0]

    # 对象值被截断(声明 4 字节,只给 2 字节)
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_device_id_response(
            bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x04, 0x41, 0x42])
        )
    assert "被截断" in exc_info.value.args[0]

    # 尾部多余字节(长度不自治)
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_device_id_response(
            bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x41, 0x42])
        )
    assert "长度不符" in exc_info.value.args[0]


def test_parse_device_id_exception_carries_code() -> None:
    """FC43 异常响应:DeviceError 携带设备返回的异常码。"""
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_device_id_response(bytes([0xAB, 0x01]))
    assert exc_info.value.code == 1


def test_device_id_object_count_helper() -> None:
    """RTU 增量收包辅助:按已有 6 字节固定头取对象个数。"""
    head = bytes([0x0E, 0x01, 0x01, 0x00, 0x00, 0x03])
    assert codec.device_id_object_count(head) == 3
    with pytest.raises(ProtocolFrameError):
        codec.device_id_object_count(b"\x0e\x01")


def test_expected_response_length_fc23_and_fc43() -> None:
    """响应长度推算:FC23 按读数量;FC43 无法推算须显式报错(RTU 走增量收包)。"""
    fc23 = codec.build_read_write_registers_pdu(3, 6, 14, [1, 2, 3])
    assert codec.expected_response_length(fc23) == 2 + 6 * 2
    fc43 = codec.build_device_id_pdu(0x01, 0x00)
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.expected_response_length(fc43)
    assert "增量收包" in exc_info.value.args[0]
