"""黄金报文样本测试:三菱 MC 编解码双向验证。

样本来自 ``tests/golden/mc_*.json``,由同目录 ``generate_mc_samples.py``
用独立实现计算生成,保证本库编解码与标准帧逐字节一致。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniplc.core.constants import MC_DEFAULT_MONITOR_TIMER
from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.plc.melsec import codec_a, codec_qna
from omniplc.plc.melsec.address import parse_mc_address

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

# (文件名, 帧型, 序列号, 网络, PC, 地址, 点数, 位单位?, 期望值)
_READ_CASES: List[Tuple[str, str, int, int, int, str, int, bool, List[int]]] = [
    ("mc_3e_batch_read_001", "3E", 0, 0, 0xFF, "D100", 2, False, [20, 10]),
    ("mc_3e_bit_read_001", "3E", 0, 0, 0xFF, "M10", 3, True, [1, 0, 1]),
    ("mc_4e_batch_read_001", "4E", 1, 0, 0xFF, "D100", 2, False, [20, 10]),
    ("mc_1e_batch_read_001", "1E", 0, 0, 0x00, "D100", 2, False, [20, 10]),
]
# (文件名, 帧型, 序列号, 网络, PC, 地址, 写入值, 位单位?)
_WRITE_CASES: List[Tuple[str, str, int, int, int, str, List[int], bool]] = [
    ("mc_3e_batch_write_001", "3E", 0, 0, 0xFF, "D100", [10, 258], False),
    ("mc_1e_batch_write_001", "1E", 0, 0, 0x00, "D100", [1234], False),
]
# (文件名, 帧型, 期望结束代码)
_ERROR_CASES: List[Tuple[str, str, int]] = [
    ("mc_3e_error_001", "3E", 0xC059),
]


def _load(stem: str) -> Dict[str, Any]:
    """读取一个黄金样本 JSON。"""
    with (GOLDEN_DIR / "{}.json".format(stem)).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_request(
    frame: str, serial: int, network: int, pc: int, address: str, points: int, is_bit: bool
) -> bytes:
    """按用例参数构造读请求帧。"""
    parsed = parse_mc_address(address)
    if frame == "1E":
        return codec_a.build_request(
            pc, MC_DEFAULT_MONITOR_TIMER, parsed, points, is_bit, False
        )
    return codec_qna.build_request(
        frame, serial, network, pc, MC_DEFAULT_MONITOR_TIMER, parsed, points, is_bit, False
    )


def _parse_read(frame: str, serial: int, response: bytes, points: int, is_bit: bool) -> List[int]:
    """按帧型解析读响应。"""
    if frame == "1E":
        return codec_a.parse_response(response, points, is_bit, True)
    return codec_qna.parse_response(
        response, frame, points, is_bit, True, expected_serial=serial
    )


@pytest.mark.parametrize(
    ("stem", "frame", "serial", "network", "pc", "address", "points", "is_bit", "values"),
    _READ_CASES,
)
def test_golden_read_roundtrip(
    stem: str,
    frame: str,
    serial: int,
    network: int,
    pc: int,
    address: str,
    points: int,
    is_bit: bool,
    values: List[int],
) -> None:
    """读样本:编码方向逐字节一致,解码方向值一致。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    assert _build_request(frame, serial, network, pc, address, points, is_bit) == request
    assert _parse_read(frame, serial, response, points, is_bit) == values


@pytest.mark.parametrize(
    ("stem", "frame", "serial", "network", "pc", "address", "values", "is_bit"),
    _WRITE_CASES,
)
def test_golden_write_roundtrip(
    stem: str,
    frame: str,
    serial: int,
    network: int,
    pc: int,
    address: str,
    values: List[int],
    is_bit: bool,
) -> None:
    """写样本:请求帧逐字节一致,写响应(结束码 0)校验通过。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    parsed = parse_mc_address(address)
    if frame == "1E":
        built = codec_a.build_request(
            pc, MC_DEFAULT_MONITOR_TIMER, parsed, len(values), is_bit, True, values
        )
        codec_a.parse_response(response, 0, is_bit, False)
    else:
        built = codec_qna.build_request(
            frame,
            serial,
            network,
            pc,
            MC_DEFAULT_MONITOR_TIMER,
            parsed,
            len(values),
            is_bit,
            True,
            values,
        )
        codec_qna.parse_response(
            response, frame, 0, is_bit, False, expected_serial=serial
        )
    assert built == request


@pytest.mark.parametrize(("stem", "frame", "code"), _ERROR_CASES)
def test_golden_error_response(stem: str, frame: str, code: int) -> None:
    """异常样本:抛 DeviceError 且携带原始结束代码。"""
    data = _load(stem)
    response = bytes.fromhex(data["response_hex"])
    with pytest.raises(DeviceError) as exc_info:
        codec_qna.parse_response(response, frame, 2, False, True)
    assert exc_info.value.code == code


def test_device_number_radix() -> None:
    """软元件编号进制:3E 的 X 按十六进制、1E 的 X 按八进制换算。"""
    assert codec_qna.device_number("X", "1F", 16) == 31
    assert codec_a.device_info("D")[0] == 0x4420
    assert codec_a.device_number("X", "17", 8) == 15
    with pytest.raises(ValueError):
        codec_qna.device_number("D", "1F", 10)
    with pytest.raises(ValueError):
        codec_qna.device_info("XR")


def test_build_random_read_golden() -> None:
    """多块批量读请求字面字节(SH-080008 §8.4 二进制通信例:2 字块 + 3 位块)。"""
    request = codec_qna.build_random_read(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        [(0x03, 0x000000, 4), (0xA8, 0x000100, 8)],
        [(0x90, 0x000080, 2), (0x90, 0x000100, 2), (0xA0, 0x000100, 3)],
    )
    core = (
        "06040000" "0200"
        "030000000400" "a80001000800"
        "0300"
        "908000000200" "900001000200" "a00001000300"
    )
    expected = "5000" "00" "ff" "ff03" "00" "2800" "0a00" + core
    assert request == bytes.fromhex(expected)


def test_build_random_read_validation() -> None:
    """多块批量读构造校验:空块/总块数超限/点数非法。"""
    with pytest.raises(ValueError):
        codec_qna.build_random_read("3E", 0, 0, 0xFF, 10, [], [])
    with pytest.raises(ValueError):
        codec_qna.build_random_read(
            "3E", 0, 0, 0xFF, 10, [(0xA8, index * 100, 1) for index in range(121)], []
        )
    with pytest.raises(ValueError):
        codec_qna.build_random_read("3E", 0, 0, 0xFF, 10, [(0xA8, 0, 0)], [])


def test_parse_random_read_response() -> None:
    """多块批量读响应:字块扁平列表;位块逐点 16 位字(点内首软元件 bit15)。"""
    data = bytes.fromhex("0100" "ffff" "3412" "0080" "0100")
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
    )
    assert codec_qna.parse_random_read_response(frame, "3E", 3, 2) == (
        [1, 0xFFFF, 0x1234],
        [0x8000, 0x0001],
    )


def test_parse_random_read_response_short_data() -> None:
    """多块批量读响应数据不足:按坏帧拒绝。"""
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + 2).to_bytes(2, "little")
        + b"\x00\x00"
        + b"\x00\x00"
    )
    with pytest.raises(ProtocolFrameError):
        codec_qna.parse_random_read_response(frame, "3E", 3, 2)
