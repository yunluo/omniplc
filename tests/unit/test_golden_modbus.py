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

from omniplc.core.debug import format_hex
from omniplc.core.errors import DeviceError, ProtocolFrameError
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


class TestFrameErrorMessageCarriesRawData:
    """坏帧/校验失败的异常文本必须带**收到的原始数据**(十六进制转储)。

    现场排查(线路噪声 / 收发错位 / 站号错配 / 网关语义不符)需要原始字节
    与抓包逐字节比对;只报"CRC 不符"无法区分成因。转储口径统一走
    :func:`omniplc.core.debug.format_hex`(大写、空格分隔)。
    """

    def test_rtu_crc_failure_message_has_raw_frame(self) -> None:
        frame = bytearray(codec.build_rtu_frame(1, bytes([3, 2, 0x00, 0x14])))
        frame[-1] ^= 0xFF  # 篡改 CRC 制造校验失败
        frame = bytes(frame)
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.parse_rtu_frame(frame)
        message = str(excinfo.value)
        assert "CRC 校验失败" in message
        assert format_hex(frame) in message  # 原始帧逐字节可见
        assert "计算 0x" in message and "收到 0x" in message  # 双 CRC 值仍在

    def test_rtu_short_frame_message_has_raw_data(self) -> None:
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.parse_rtu_frame(b"\x01\x03")
        message = str(excinfo.value)
        assert "帧过短" in message
        assert format_hex(b"\x01\x03") in message

    def test_mbap_protocol_id_message_has_raw_header(self) -> None:
        header = bytearray(codec.build_mbap(1, 1, bytes([3, 2, 0x00, 0x14]))[:7])
        header[2:4] = b"\x00\x01"  # 协议标识符非 0
        header = bytes(header)
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.parse_mbap_header(header)
        message = str(excinfo.value)
        assert "协议标识符" in message
        assert format_hex(header) in message

    def test_mbap_length_over_frame_message_has_raw_data(self) -> None:
        full = codec.build_mbap(1, 1, bytes([3, 2, 0x00, 0x14]))
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.parse_mbap(full[:7])  # 只有帧头,长度字段声明的数据缺失
        message = str(excinfo.value)
        assert "超出实际帧长" in message
        assert format_hex(full[:7]) in message

    def test_response_function_code_mismatch_has_raw_pdu(self) -> None:
        pdu = bytes([4, 2, 0x00, 0x14])  # 请求 FC03,响应回 FC04
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.check_response_exception(pdu, 3)
        message = str(excinfo.value)
        assert "功能码不符" in message
        assert format_hex(pdu) in message

    def test_exception_function_code_mismatch_has_raw_pdu(self) -> None:
        """异常响应也须校验回显功能码:FC03 请求回 FC05|0x80 属错配坏帧。

        不得降级成 DeviceError 把别的请求的异常码落到 ``last_error_code``。
        """
        pdu = bytes([0x85, 0x02])  # 请求 FC03,收到 FC05|0x80 + 异常码 02
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.check_response_exception(pdu, 3)
        message = str(excinfo.value)
        assert "异常响应功能码不符" in message
        assert format_hex(pdu) in message

    def test_read_response_length_mismatch_has_raw_pdu(self) -> None:
        pdu = bytes([3, 4, 0x00, 0x14, 0x00, 0x0A])  # 字节计数域谎报 4
        with pytest.raises(ProtocolFrameError) as excinfo:
            codec.parse_read_response(pdu, 3, 1)
        message = str(excinfo.value)
        assert "长度不符" in message
        assert format_hex(pdu) in message
