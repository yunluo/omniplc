"""黄金报文样本测试:欧姆龙 FINS 编解码双向验证。

样本来自 ``tests/golden/fins_*.json``,由同目录 ``generate_fins_samples.py``
用独立实现计算生成。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniplc.core.errors import DeviceError
from omniplc.plc.omron import codec
from omniplc.plc.omron.address import parse_fins_address

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

# (文件名, 地址, 点数, 期望值)
_READ_CASES: List[Tuple[str, str, int, List[int]]] = [
    ("fins_udp_area_read_001", "D100", 2, [20, 10]),
    ("fins_tcp_area_read_001", "D100", 2, [20, 10]),
]
# (文件名, 地址, 写入值)
_WRITE_CASES: List[Tuple[str, str, List[int]]] = [
    ("fins_udp_area_write_001", "W10", [0x1234, 0x5678]),
]
# (文件名, 期望结束码)
_ERROR_CASES: List[Tuple[str, int]] = [
    ("fins_udp_error_001", 1),
]


def _load(stem: str) -> Dict[str, Any]:
    """读取一个黄金样本 JSON。"""
    with (GOLDEN_DIR / "{}.json".format(stem)).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _unwrap_tcp(frame: bytes) -> bytes:
    """剥离 FINS/TCP 头,返回内层 FINS 帧(走 codec 校验路径)。"""
    length = codec.parse_tcp_head(frame[:8])
    content = frame[8:8 + length]
    codec.extract_tcp_error(content)
    return codec.extract_tcp_payload(content)


@pytest.mark.parametrize(("stem", "address", "count", "values"), _READ_CASES)
def test_golden_area_read_roundtrip(
    stem: str, address: str, count: int, values: List[int]
) -> None:
    """区域读样本:请求帧逐字节一致,响应解析出字数据。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    setup = data["setup"]
    dst, src = setup["destination"], setup["source"]
    parsed = parse_fins_address(address)
    built = codec.build_area_read(
        dst[0], dst[1], dst[2], src[0], src[1], src[2],
        setup["sid"], parsed, count, False,
    )
    if setup["transport"] == "tcp":
        built = codec.build_tcp_frame(built)
    assert built == request
    fins = _unwrap_tcp(response) if setup["transport"] == "tcp" else response
    assert codec.parse_response(fins, count, False, True) == values


@pytest.mark.parametrize(("stem", "address", "values"), _WRITE_CASES)
def test_golden_area_write_roundtrip(
    stem: str, address: str, values: List[int]
) -> None:
    """区域写样本:请求帧逐字节一致,写响应校验通过。"""
    data = _load(stem)
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    setup = data["setup"]
    dst, src = setup["destination"], setup["source"]
    built = codec.build_area_write(
        dst[0], dst[1], dst[2], src[0], src[1], src[2],
        setup["sid"], parse_fins_address(address), values, False,
    )
    assert built == request
    codec.parse_response(response, 0, False, False)


@pytest.mark.parametrize(("stem", "code"), _ERROR_CASES)
def test_golden_error_response(stem: str, code: int) -> None:
    """异常样本:抛 DeviceError 且携带原始结束码。"""
    data = _load(stem)
    response = bytes.fromhex(data["response_hex"])
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_response(response, 2, False, True)
    assert exc_info.value.code == code


def test_golden_handshake() -> None:
    """握手样本:请求 20 字节逐字节一致,响应解析出节点分配。"""
    data = _load("fins_tcp_handshake_001")
    request = bytes.fromhex(data["request_hex"])
    response = bytes.fromhex(data["response_hex"])
    assert codec.build_handshake(0) == request
    assert codec.parse_handshake_response(response) == (11, 5)


def test_memory_codes_and_em_bank() -> None:
    """存储区码表:D/E 区换算与非法区校验。"""
    assert codec.memory_codes("D") == (0x02, 0x82)
    assert codec.memory_codes("CIO") == (0x30, 0xB0)
    assert codec.memory_codes("E", 3) == (0x23, 0xE3)
    with pytest.raises(ValueError):
        codec.memory_codes("X")
    with pytest.raises(ValueError):
        codec.memory_codes("E", 16)


def test_em_address_uses_bank_code() -> None:
    """EM 区地址 E0_100:字操作码 0xE0、bank 进帧。"""
    parsed = parse_fins_address("E0_100")
    assert (parsed.area, parsed.bank, parsed.offset) == ("E", 0, 100)
    frame = codec.build_area_read(0, 5, 0, 0, 10, 0, 1, parsed, 1, False)
    assert frame[12] == 0xE0
    with pytest.raises(ValueError):
        parse_fins_address("E0")
