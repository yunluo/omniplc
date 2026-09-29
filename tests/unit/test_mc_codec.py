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
    """软元件编号进制:3E/1E 的 X 均按十六进制换算(SH-080008 §8.1)。"""
    assert codec_qna.device_number("X", "1F", 16) == 31
    assert codec_a.device_info("D")[0] == 0x4420
    assert codec_a.device_number("X", "1F", 16) == 31
    with pytest.raises(ValueError):
        codec_qna.device_number("D", "1F", 10)
    with pytest.raises(ValueError):
        codec_qna.device_info("XR")


def test_mc_device_radix_matches_manual() -> None:
    """MC 软元件进制按 SH-080008 §8.1:ZR/1E X/Y 为十六进制,其余十进制。"""
    from omniplc.core.constants import MC_1E_DEVICE_CODES, MC_DEVICE_CODES

    assert MC_DEVICE_CODES["ZR"][2] == 16
    assert MC_DEVICE_CODES["ZR"][0] == 0xB0
    # 抽查其余进制不变
    assert MC_DEVICE_CODES["D"][2] == 10
    assert MC_DEVICE_CODES["W"][2] == 16
    assert MC_1E_DEVICE_CODES["X"][2] == 16
    assert MC_1E_DEVICE_CODES["Y"][2] == 16
    assert MC_1E_DEVICE_CODES["D"][2] == 10


def test_1e_word_access_bit_device_requires_multiple_of_16() -> None:
    """1E 字单位访问位软元件:首编号须为 16 的倍数(SH-080008 §18.4)。"""
    with pytest.raises(ValueError):
        codec_a.build_request(
            0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M10"),
            1, False, False, None,
        )
    # 16 的倍数放行
    codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M16"),
        1, False, False, None,
    )


def test_build_random_read_response_budget() -> None:
    """0406 响应总量超 MC_MAX_RESPONSE_CONTENT → 入参期 ValueError(不发请求)。"""
    # 每块 900 点(≤单块上限),5 块 = 4500 点 → 9000 字节 > 8192 上限
    blocks = [(0xA8, i * 1000, 900) for i in range(5)]
    with pytest.raises(ValueError):
        codec_qna.build_random_read(
            "3E", 0, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, blocks, []
        )


def test_build_random_read_golden() -> None:
    """多块批量读请求字面字节(SH-080008 §8.4 印刷页 114 二进制通信例原样)。

    手册例 = 2 字块 + 3 位块:条目序为**编号 3B 小端 → 码 1B → 点数 2B 小端**
    (列头 Device number → code → Number of device points),字块
    ``(A8H, D0, 4)``/``(B4H, W100, 8)``,位块 ``(90H, M0, 2)``/
    ``(90H, M128, 2)``/``(A0H, B256, 3)``。
    """
    request = codec_qna.build_random_read(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, 0x000000, 4), (0xB4, 0x000100, 8)],
        [(0x90, 0x000000, 2), (0x90, 0x000080, 2), (0xA0, 0x000100, 3)],
    )
    core = (
        "06040000" "0200"
        "000000a80400" "000100b40800"
        "0300"
        "000000900200" "800000900200" "000100a00300"
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


def test_parse_random_read_response_trailing_bytes_rejected() -> None:
    """0406 响应尾部有多余字节(UDP 数据报边界/串包):按坏帧拒绝。"""
    data = bytes.fromhex("0100" "ffff" "3412" "0080" "0100")
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
        + b"\xaa\xbb"  # 多余 2 字节
    )
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec_qna.parse_random_read_response(frame, "3E", 3, 2)
    assert "多余字节" in exc_info.value.args[0]


def test_parse_response_trailing_bytes_rejected() -> None:
    """读响应声明的数据长大于请求点数:按坏帧拒绝(不再静默截断尾部)。"""
    data = bytes.fromhex("010000")  # 声明 3 字节,但请求只读 1 字(2 字节)
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
    )
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec_qna.parse_response(frame, "3E", 1, False, True)
    assert "长度不符" in exc_info.value.args[0]


# ----------------------------------------------------------------------
# 位软元件位号后缀校验:各帧型组帧层统一防线(含串口 3C/4C 与 1C)
# ----------------------------------------------------------------------


def test_qna_bit_suffix_rejected() -> None:
    """3E/4E 组帧层:位软元件带位号后缀直接拒绝(M10.5 不得发成 M10)。"""
    with pytest.raises(ValueError):
        codec_qna.build_request(
            "3E", 0, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
            parse_mc_address("M10.5"), 1, True, False,
        )


def test_serial_bit_suffix_rejected() -> None:
    """3C/1C 串口组帧层同款校验:位号后缀不得静默丢弃。"""
    from omniplc.plc.melsec import codec_serial, codec_serial_a

    with pytest.raises(ValueError):
        codec_serial.build_3c_request(
            0, 0, 0xFF, 0, parse_mc_address("M10.5"), 1, True, False
        )
    with pytest.raises(ValueError):
        codec_serial_a.build_1c_request(
            0, 0xFF, 0, parse_mc_address("M10.5"), 1, True, False
        )


def test_mc_device_code_table_l_is_92_and_no_collisions() -> None:
    """MC 设备码表:L=0x92(锁存继电器),且任一设备码不得两区共用。

    回归:设备码表扩容时 ``L`` 曾被误写为 ``0xA0``(与 ``B`` 链接继电器同码),
    导致 3E/4E/4C 路径下所有 ``L`` 读写静默打到 ``B`` 空间。
    """
    from collections import defaultdict

    from omniplc.core.constants import MC_DEVICE_CODES

    assert MC_DEVICE_CODES["L"] == (0x92, 1, 10)
    by_code = defaultdict(list)
    for device, (code, _words, _base) in MC_DEVICE_CODES.items():
        by_code[code].append(device)
    collisions = {hex(code): devices for code, devices in by_code.items() if len(devices) > 1}
    assert not collisions, "MC 设备码重码:{}".format(collisions)


def test_mc_timer_counter_device_codes_match_manual() -> None:
    """MC 定时器/计数器设备码按 SH080008 §8.1:TS=C1H/TC=C0H/TN=C2H,CS=C4H/CC=C3H/CN=C5H。

    回归:v0.41.0 扩容时把六者误排成连续 C1..C6,导致 3E/4E/4C 下
    ``TN``/``CN``(当前值,字)静默落到 ``CC``/``STC`` 线圈区。
    """
    from omniplc.core.constants import MC_DEVICE_CODES

    assert MC_DEVICE_CODES["TS"][0] == 0xC1
    assert MC_DEVICE_CODES["TC"][0] == 0xC0
    assert MC_DEVICE_CODES["TN"][0] == 0xC2
    assert MC_DEVICE_CODES["CS"][0] == 0xC4
    assert MC_DEVICE_CODES["CC"][0] == 0xC3
    assert MC_DEVICE_CODES["CN"][0] == 0xC5
    # 字宽:接点/线圈(TS/TC/CS/CC)=位,当前值(TN/CN)=字
    assert MC_DEVICE_CODES["TC"][1] == 1
    assert MC_DEVICE_CODES["TN"][1] == 0
    assert MC_DEVICE_CODES["CC"][1] == 1
    assert MC_DEVICE_CODES["CN"][1] == 0


def test_mc_1e_step_relay_code_matches_manual() -> None:
    """1E 的 M/L/S 共用内部继电器码 4D20H(手册);S 不得为凭空值。"""
    from omniplc.core.constants import MC_1E_DEVICE_CODES

    assert MC_1E_DEVICE_CODES["M"][0] == 0x4D20
    assert MC_1E_DEVICE_CODES["S"][0] == 0x4D20


def test_check_byte_field_rejects_non_int() -> None:
    """路由字节字段校验:bool/float/str 不再被 int() 静默收窄。"""
    from omniplc.core.validation import check_byte_field

    assert check_byte_field("pc", 5) == 5
    for bad in (True, 1.5, "5"):
        with pytest.raises(ValueError):
            check_byte_field("pc", bad)


def test_parse_response_1e_trailing_bytes_rejected() -> None:
    """1E 读响应尾部多余字节 → ProtocolFrameError(与 3E/4E 口径一致)。"""
    from omniplc.core.constants import MC_1E_READ_WORD

    frame = bytes([MC_1E_READ_WORD + 0x80, 0x00]) + (20).to_bytes(2, "little") + b"\x00"
    with pytest.raises(ProtocolFrameError):
        codec_a.parse_response(frame, 1, False, True)


# ----------------------------------------------------------------------
# 随机读/写(0403/1402)与 CPU 型号(0101)——SH-080008 §8.3/§11.2 黄金样本
# ----------------------------------------------------------------------

# 手册软元件码:D=0xA8 T(N)=0xC2 M=0x90 X=0x9C Y=0x9D
_D, _TN, _M, _X, _Y = 0xA8, 0xC2, 0x90, 0x9C, 0x9D


def test_build_random_read_devices_golden() -> None:
    """随机读请求字面字节(SH-080008 §8.3 印刷页 101 二进制通信例原样)。

    条目序为**编号 3B 小端 → 码 1B**:字块 D0/T0/M100/X20、双字块
    D1500/Y160/M1111(X/Y 编号十六进制,故 X20 → 0x20、Y160 → 0x160)。
    """
    request = codec_qna.build_random_read_devices(
        "3E", 0, 0, 0xFF, 0,
        [(_D, 0), (_TN, 0), (_M, 100), (_X, 0x20)],
        [(_D, 1500), (_Y, 0x160), (_M, 1111)],
    )
    core = (
        "03040000" "0400" "0300"
        "000000a8" "000000c2" "64000090" "2000009c"
        "dc0500a8" "6001009d" "57040090"
    )
    # 副头部 5000 + 网络 00 + PC ff + IO ff03 + 局 00 + 长度 2600 + 定时器 0000
    assert request == bytes.fromhex("5000" "00" "ff" "ff03" "00" "2600" "0000" + core)


def test_parse_random_read_devices_response_golden() -> None:
    """随机读响应:字数据逐字小端 + 双字数据 4 字节小端(§8.3 印刷页 100-101)。"""
    data = (
        bytes.fromhex("9519") + bytes.fromhex("0212")
        + bytes.fromhex("3412") + bytes.fromhex("7856")
        + bytes.fromhex("4e4f544c") + bytes.fromhex("11110000") + bytes.fromhex("efcdab89")
    )
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
    )
    words, dwords = codec_qna.parse_random_read_devices_response(frame, "3E", 4, 3)
    assert words == [0x1995, 0x1202, 0x1234, 0x5678]
    assert dwords[0] == 0x4C544F4E  # 手册 D1500/D1501 = 4F4EH/4C54H → 32 位小端
    assert dwords[2] == 0x89ABCDEF


def test_parse_random_read_devices_response_length_mismatch() -> None:
    """随机读响应数据不足/冗余:按坏帧拒绝。"""
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + 2).to_bytes(2, "little")
        + b"\x00\x00"
        + b"\x00\x00"
    )
    with pytest.raises(ProtocolFrameError):
        codec_qna.parse_random_read_devices_response(frame, "3E", 4, 3)


def test_build_random_read_devices_validation() -> None:
    """随机读构造校验:空列表/总点数超限。"""
    with pytest.raises(ValueError):
        codec_qna.build_random_read_devices("3E", 0, 0, 0xFF, 0, [], [])
    with pytest.raises(ValueError):
        codec_qna.build_random_read_devices(
            "3E", 0, 0, 0xFF, 0,
            [(_D, index) for index in range(193)],
            [],
        )


def test_build_random_write_devices_golden() -> None:
    """随机写请求字面字节(SH-080008 §8.3 印刷页 107 二进制通信例原样)。

    4 字 + 3 双字,条目序**编号 3B 小端 → 码 1B → 写数据**(字 2B/双字 4B
    小端):字块 D0/D256/M100/X20,双字块 D1500/Y160/M1111。
    """
    request = codec_qna.build_random_write_devices(
        "3E", 0, 0, 0xFF, 0,
        [(0xA8, 0, 0x0550), (0xA8, 0x100, 0x0575), (0x90, 100, 0x0540), (0x9C, 0x20, 0x0583)],
        [(0xA8, 1500, 0x04391202), (0x9D, 0x160, 0x23752607), (0x90, 1111, 0x04250475)],
    )
    core = (
        "02140000" "0400" "0300"
        "000000a8" "5005" "000100a8" "7505" "64000090" "4005" "2000009c" "8305"
        "dc0500a8" "02123904" "6001009d" "07267523" "57040090" "75042504"
    )
    assert request == bytes.fromhex("5000" "00" "ff" "ff03" "00" "3a00" "0000" + core)


def test_build_random_write_devices_validation() -> None:
    """随机写构造校验:双列表均空/数值越界/加权点数超限。"""
    with pytest.raises(ValueError):
        codec_qna.build_random_write_devices("3E", 0, 0, 0xFF, 0, [], [])
    with pytest.raises(ValueError):
        codec_qna.build_random_write_devices(
            "3E", 0, 0, 0xFF, 0, [(_D, 0, 0x10000)], []
        )
    with pytest.raises(ValueError):
        codec_qna.build_random_write_devices(
            "3E", 0, 0, 0xFF, 0,
            [(_D, index, 1) for index in range(161)],
            [],
        )


def test_read_cpu_model_golden() -> None:
    """CPU 型号请求/响应(§11.2 印刷页 178:Q02UCPU → 名 + 码 0x6302 大端)。"""
    request = codec_qna.build_read_cpu_model("3E")
    assert request == bytes.fromhex("5000" "00" "ff" "ff03" "00" "0600" "0a00" "01010000")
    data = b"Q02UCPU".ljust(16) + bytes.fromhex("6302")
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
    )
    assert codec_qna.parse_read_cpu_model_response(frame, "3E") == ("Q02UCPU", 0x0263)


def test_read_cpu_model_bad_name() -> None:
    """CPU 型号名含非 ASCII 字节(0x80+):按坏帧拒绝。"""
    data = bytes([0xD0] * 16) + bytes.fromhex("0000")
    frame = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (2 + len(data)).to_bytes(2, "little")
        + b"\x00\x00"
        + data
    )
    with pytest.raises(ProtocolFrameError):
        codec_qna.parse_read_cpu_model_response(frame, "3E")
