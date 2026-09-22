"""黄金报文样本测试:欧姆龙 FINS 编解码双向验证。

样本来自 ``tests/golden/fins_*.json``,由同目录 ``generate_fins_samples.py``
用独立实现计算生成。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.plc.omron import codec
from omniplc.plc.omron.address import parse_fins_address

_FINS_ECHO_HEAD = b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00"

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
    """存储区码表:D/E/T/C 区换算与非法区校验。"""
    assert codec.memory_codes("D") == (0x02, 0x82)
    assert codec.memory_codes("CIO") == (0x30, 0xB0)
    assert codec.memory_codes("E", 3) == (0x23, 0xA3)  # EM 字码基址 0xA0(W340 5-2-2)
    assert codec.memory_codes("T") == (0x09, 0x89)  # 完成标志位 / 当前值字,与 C 共享
    assert codec.memory_codes("C") == (0x09, 0x89)
    with pytest.raises(ValueError):
        codec.memory_codes("X")
    with pytest.raises(ValueError):
        codec.memory_codes("E", 16)


def test_em_address_uses_bank_code() -> None:
    """EM 区地址 E0_100:字操作码 0xA0、bank 进帧。"""
    parsed = parse_fins_address("E0_100")
    assert (parsed.area, parsed.bank, parsed.offset) == ("E", 0, 100)
    frame = codec.build_area_read(0, 5, 0, 0, 10, 0, 1, parsed, 1, False)
    assert frame[12] == 0xA0
    with pytest.raises(ValueError):
        parse_fins_address("E0")


def test_build_multiple_area_read() -> None:
    """多存储区读请求:命令 0104,每条 = 区码 1 字节 + 字地址 2 字节大端 + 位 0。"""
    frame = codec.build_multiple_area_read(
        0, 5, 0, 0, 10, 0, 1, [(0x82, 100), (0xB0, 5)]
    )
    assert frame[10:12] == b"\x01\x04"
    assert frame[12:] == bytes.fromhex("82006400" "b0000500")


def test_build_multiple_area_read_validation() -> None:
    """多存储区读构造校验:空条目/条数超限/地址越界。"""
    with pytest.raises(ValueError):
        codec.build_multiple_area_read(0, 5, 0, 0, 10, 0, 1, [])
    with pytest.raises(ValueError):
        codec.build_multiple_area_read(
            0, 5, 0, 0, 10, 0, 1, [(0x82, index) for index in range(168)]
        )
    with pytest.raises(ValueError):
        codec.build_multiple_area_read(0, 5, 0, 0, 10, 0, 1, [(0x82, 0x10000)])


def test_parse_multiple_area_read() -> None:
    """多存储区读响应:每条 = 区码回显 1 字节 + 字数据 2 字节大端。"""
    frame = (
        _FINS_ECHO_HEAD + b"\x01" + b"\x01\x04" + b"\x00\x00"
        + b"\x82" + (0x1234).to_bytes(2, "big")
        + b"\xb0" + (0x0007).to_bytes(2, "big")
    )
    assert codec.parse_multiple_area_read(frame, [0x82, 0xB0]) == [0x1234, 0x0007]


def test_parse_multiple_area_read_errors() -> None:
    """多存储区读响应错误路径:数据不足与区码回显不符按坏帧,结束码按设备故障。"""
    base = _FINS_ECHO_HEAD + b"\x01" + b"\x01\x04"
    with pytest.raises(ProtocolFrameError):
        codec.parse_multiple_area_read(base + b"\x00\x00" + b"\x82", [0x82, 0xB0])
    bad_echo = base + b"\x00\x00" + b"\x83" + (1).to_bytes(2, "big") + b"\xb0" + (0).to_bytes(2, "big")
    with pytest.raises(ProtocolFrameError):
        codec.parse_multiple_area_read(bad_echo, [0x82, 0xB0])
    with pytest.raises(DeviceError):
        codec.parse_multiple_area_read(base + b"\x11\x01", [0x82])
