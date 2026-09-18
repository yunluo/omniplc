"""黄金报文样本测试:Modbus 编解码双向验证。

样本来自 ``tests/golden/modbus_*.json``,由同目录
``generate_modbus_samples.py`` 用独立实现计算生成,保证本库编解码
与标准报文逐字节一致(编码方向 + 解码方向 + 异常路径)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniplc.core.errors import DeviceError
from omniplc.modbus import codec

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

# (文件名, 功能码, 起始地址, 数量, 期望值)
_READ_CASES: List[Tuple[str, int, int, int, List[int]]] = [
    ("modbus_tcp_read_holding_001", 3, 0, 2, [20, 10]),
    ("modbus_tcp_read_input_001", 4, 10, 1, [65518]),
    ("modbus_rtu_read_holding_001", 3, 0, 2, [20, 10]),
    ("modbus_rtu_read_coils_001", 1, 0, 8, [1, 0, 1, 1, 0, 0, 1, 1]),
]
# (文件名, 功能码, 起始地址, 写入值)
_WRITE_CASES: List[Tuple[str, int, int, List[int]]] = [
    ("modbus_tcp_write_single_register_001", 6, 5, [3]),
    ("modbus_tcp_write_single_coil_001", 5, 0, [1]),
    ("modbus_tcp_write_multi_registers_001", 16, 0, [10, 258]),
    ("modbus_rtu_write_single_coil_001", 5, 0, [1]),
    ("modbus_rtu_write_multi_coils_001", 15, 0, [1, 0, 1, 1, 0, 0, 1, 1, 1, 0]),
]
# (文件名, 请求功能码, 期望异常码)
_EXCEPTION_CASES: List[Tuple[str, int, int]] = [
    ("modbus_tcp_exception_001", 3, 2),
    ("modbus_rtu_exception_001", 3, 2),
]


def _load(stem: str) -> Dict[str, Any]:
    """读取一个黄金样本 JSON。"""
    with (GOLDEN_DIR / "{}.json".format(stem)).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_request_pdu(function_code: int, offset: int, count: int, values: List[int]) -> bytes:
    """按用例参数构造请求 PDU。"""
    if function_code in (5, 6):
        return codec.build_write_single_pdu(function_code, offset, values[0])
    if function_code in (15, 16):
        return codec.build_write_multi_pdu(function_code, offset, values)
    return codec.build_read_pdu(function_code, offset, count)


def _wrap(stem: str, pdu: bytes) -> bytes:
    """按样本走线封装完整帧(MBAP 或 RTU)。"""
    setup = _load(stem).get("setup", {})
    if stem.startswith("modbus_rtu"):
        return codec.build_rtu_frame(int(setup.get("station", 1)), pdu)
    return codec.build_mbap(
        int(setup.get("transaction_id", 1)), int(setup.get("station", 1)), pdu
    )


def _unwrap(stem: str, frame: bytes) -> Tuple[int, bytes]:
    """按样本走线解出 (站号, PDU)。"""
    if stem.startswith("modbus_rtu"):
        return codec.parse_rtu_frame(frame)
    _, station, pdu = codec.parse_mbap(frame)
    return station, pdu


@pytest.mark.parametrize(
    ("stem", "function_code", "offset", "count", "values"), _READ_CASES
)
def test_golden_read_roundtrip(
    stem: str, function_code: int, offset: int, count: int, values: List[int]
) -> None:
    """读样本:编码方向逐字节一致,解码方向值一致。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    assert _wrap(stem, _build_request_pdu(function_code, offset, count, values)) == request
    station, pdu = _unwrap(stem, response)
    assert station == int(data.get("setup", {}).get("station", 1))
    assert codec.parse_read_response(pdu, function_code, count) == values


@pytest.mark.parametrize(("stem", "function_code", "offset", "values"), _WRITE_CASES)
def test_golden_write_roundtrip(
    stem: str, function_code: int, offset: int, values: List[int]
) -> None:
    """写样本:请求帧逐字节一致,响应回显校验通过。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    request_pdu = _build_request_pdu(function_code, offset, 0, values)
    assert _wrap(stem, request_pdu) == request
    _, response_pdu = _unwrap(stem, response)
    codec.parse_write_response(response_pdu, request_pdu)  # 不抛异常即通过


@pytest.mark.parametrize(("stem", "function_code", "code"), _EXCEPTION_CASES)
def test_golden_exception_response(stem: str, function_code: int, code: int) -> None:
    """异常样本:抛 DeviceError 且携带原始异常码。"""
    data = _load(stem)
    response = bytes.fromhex(data["response_hex"])
    _, pdu = _unwrap(stem, response)
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_read_response(pdu, function_code, 2)
    assert exc_info.value.code == code


def test_mbap_header_and_response_length_helpers() -> None:
    """辅助函数:帧头解析与响应长度推算。"""
    request = bytes.fromhex(_load("modbus_tcp_read_holding_001")["request_hex"])
    assert codec.parse_mbap_header(request[:7]) == (1, 6)
    assert codec.expected_response_length(bytes([3, 0, 0, 0, 2])) == 6
    assert codec.expected_response_length(bytes([1, 0, 0, 0, 8])) == 3
    assert codec.expected_response_length(bytes([6, 0, 5, 0, 3])) == 5
    with pytest.raises(DeviceError):
        codec.check_response_exception(bytes([0x83, 0x02]), 3)
