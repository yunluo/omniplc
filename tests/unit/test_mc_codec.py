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
    ("mc_1e_batch_read_001", "1E", 0, 0, 0xFF, "D100", 2, False, [20, 10]),
]
# (文件名, 帧型, 序列号, 网络, PC, 地址, 写入值, 位单位?)
_WRITE_CASES: List[Tuple[str, str, int, int, int, str, List[int], bool]] = [
    ("mc_3e_batch_write_001", "3E", 0, 0, 0xFF, "D100", [10, 258], False),
    ("mc_1e_batch_write_001", "1E", 0, 0, 0xFF, "D100", [1234], False),
]
# (文件名, 帧型, 期望结束代码)
_ERROR_CASES: List[Tuple[str, str, int]] = [
    ("mc_3e_error_001", "3E", 0xC059),
]


def _load(stem: str) -> Dict[str, Any]:
    """读取一个黄金样本 JSON。"""
    with (GOLDEN_DIR / f"{stem}.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_request(
    frame: str,
    serial: int,
    network: int,
    pc: int,
    address: str,
    points: int,
    is_bit: bool,
) -> bytes:
    """按用例参数构造读请求帧。"""
    parsed = parse_mc_address(address)
    if frame == "1E":
        return codec_a.build_request(
            pc, MC_DEFAULT_MONITOR_TIMER, parsed, points, is_bit, False
        )
    return codec_qna.build_request(
        frame,
        serial,
        network,
        pc,
        MC_DEFAULT_MONITOR_TIMER,
        parsed,
        points,
        is_bit,
        False,
    )


def _parse_read(
    frame: str, serial: int, response: bytes, points: int, is_bit: bool
) -> List[int]:
    """按帧型解析读响应。"""
    if frame == "1E":
        return codec_a.parse_response(response, points, is_bit, True)
    return codec_qna.parse_response(
        response, frame, points, is_bit, True, expected_serial=serial
    )


@pytest.mark.parametrize(
    (
        "stem",
        "frame",
        "serial",
        "network",
        "pc",
        "address",
        "points",
        "is_bit",
        "values",
    ),
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
    assert (
        _build_request(frame, serial, network, pc, address, points, is_bit) == request
    )
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
            0xFF,
            MC_DEFAULT_MONITOR_TIMER,
            parse_mc_address("M10"),
            1,
            False,
            False,
            None,
        )
    # 16 的倍数放行
    codec_a.build_request(
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("M16"),
        1,
        False,
        False,
        None,
    )


def test_1e_256_points_encode_as_zero_plus_fixed_zero() -> None:
    """1E 256 点编码:低字节回绕 00 + 恒零域 00(SH-080008 印刷页 402/403)。

    点数线上为「1 字节点数 + 1 字节恒零」:256 点指定 00H、Fixed value 恒 0,
    印刷页 404 算例 12 点尾域 ``0C 00``。1~255 点两种写法逐字节巧合一致,
    唯恰 256(位读上限)暴露语义分歧(review-1020 P1-6,「字段宽度巧合
    一致」盲区)——旧 2 字节小端在 256 点发 ``00 01`` 与手册相悖。
    """
    frame = codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M0"), 256, True, False
    )
    # 尾两字节 = 点数(00)+ 恒零(00);旧实现发 00 01
    assert frame[-2:] == b"\x00\x00"
    # 12 点对照手册算例尾域 0C 00
    frame12 = codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M100"), 12, True, False
    )
    assert frame12[-2:] == b"\x0c\x00"
    assert frame12.endswith(b"\x64\x00\x00\x00\x20\x4d\x0c\x00")


def test_1e_word_read_max_points_constant_matches_manual() -> None:
    """1E 字单位成批读上限 = 256(SH-080008 Appendix 5 自印 470:
    Commands for 1E frame,Batch read Word units 01H Word device = 256 points)。

    review-1024 §8.1 P1:历史常量值 64 注释引「印刷页 466」系用错表——
    该页是 4C/3C/4E/3E 命令表(A 列 = A 系列模块经该帧访问)而非 1E 命令表。
    修正后 driver 层 65~256 字的合法成批读不再被入参期拒绝。
    """
    from omniplc.core.constants import (
        MC_1E_MAX_POINTS,
        MC_1E_MAX_WORD_READ_POINTS,
    )

    assert MC_1E_MAX_WORD_READ_POINTS == 256
    assert MC_1E_MAX_POINTS == 256  # 位单位上限(成批读 00H = 256 points)


def test_1e_device_codes_match_manual_section_18_4() -> None:
    """1E 软元件码表按 SH-080008 §18.4 自印 398 全表查。

    表内 14 类(原始)== MC_1E_DEVICE_CODES 集合:
    X/Y/M/S/B/F/TN/TS/TC/CN/CS/CC/D/W/R;
    ``M/L/S`` 同码 4D20H,故 ``S``/``M`` 同项(``L`` 经 ``M`` 访问,§18.4
    自印 399/400 明示「For L and S, perform accessing by specifying 'M'」)。

    review-1024 §8.2-1 P2:补 F/TN/TS/TC/CN/CS/CC 7 类软元件。
    """
    from omniplc.core.constants import MC_1E_DEVICE_CODES

    expected_codes = {
        "X": (0x5820, 1, 16),
        "Y": (0x5920, 1, 16),
        "M": (0x4D20, 1, 10),
        "S": (0x4D20, 1, 10),
        "F": (0x4620, 1, 10),
        "B": (0x4220, 1, 16),
        "TS": (0x5453, 1, 10),
        "TC": (0x5443, 1, 10),
        "TN": (0x544E, 0, 10),
        "CS": (0x4353, 1, 10),
        "CC": (0x4343, 1, 10),
        "CN": (0x434E, 0, 10),
        "D": (0x4420, 0, 10),
        "W": (0x5720, 0, 16),
        "R": (0x5220, 0, 10),
    }
    assert expected_codes == MC_1E_DEVICE_CODES
    # S 与 M 同码
    assert MC_1E_DEVICE_CODES["S"][0] == MC_1E_DEVICE_CODES["M"][0]
    # 3E/4E 扩展项不应在 1E 表
    for sm in ("SM", "SD", "SB", "SW", "DX", "DY", "Z", "ZR", "V"):
        assert sm not in MC_1E_DEVICE_CODES


def test_1e_device_info_accepts_newly_added_devices() -> None:
    """补录后 codec_a.device_info 对新增 F/TN/TS/TC/CN/CS/CC 返回手册码。"""
    from omniplc.plc.melsec.codec_a import device_info

    assert device_info("F") == (0x4620, True, 10)
    assert device_info("TS") == (0x5453, True, 10)
    assert device_info("TC") == (0x5443, True, 10)
    assert device_info("TN") == (0x544E, False, 10)
    assert device_info("CS") == (0x4353, True, 10)
    assert device_info("CC") == (0x4343, True, 10)
    assert device_info("CN") == (0x434E, False, 10)


def test_1e_device_info_rejects_still_unsupported() -> None:
    """3E/4E 扩展软元件在 1E 表外,仍报 ValueError + 支持集包含新增项。"""
    from omniplc.plc.melsec.codec_a import device_info

    with pytest.raises(ValueError) as exc:
        device_info("SM")
    message = str(exc.value)
    # 支持集提示需含新加的 7 类(便于用户查错)
    for token in ("F", "TS", "TC", "TN", "CS", "CC", "CN"):
        assert token in message, f"支持集提示缺 {token!r}: {message}"


def test_build_random_read_response_budget() -> None:
    """0406 响应总量超 MC_MAX_RESPONSE_CONTENT → 入参期 ValueError(不发请求)。"""
    # 每块 900 点(≤单块上限),5 块 = 4500 点 → 9000 字节 > 8192 上限
    blocks = [(0xA8, i * 1000, 900) for i in range(5)]
    with pytest.raises(ValueError):
        codec_qna.build_random_read(
            "3E", 0, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, blocks, []
        )


def test_build_random_read_golden() -> None:
    """多块批量读请求字面字节(SH-080008 §8.4 印刷页 114-116 二进制通信例原样)。

    手册例 = 2 字块 + 3 位块:条目序为**编号 3B 小端 → 码 1B → 点数 2B 小端**
    (列头 Device number → code → Number of device points),字块
    ``(A8H, D0, 4)``/``(B4H, W100, 8)``,位块 ``(90H, M0, 2)``/
    ``(90H, M128, 2)``/``(A0H, B256, 3)``。命令级块数域为 **1 字节**
    (印刷页 115-116 请求例「06H 04H | 00H 00H | 02H | 03H | …」;
    review-1009 P0-1:原 2 字节错编致帧整体错位,真机必拒)。
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
    core = "0604000002000000a80400000100b4080003000000900200800000900200000100a00300"
    expected = "500000ffff030026000a00" + core
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
    """多块批量读响应:字块扁平列表;位块逐点 16 位字(点内首软元件 bit0)。"""
    data = bytes.fromhex("0100ffff341200800100")
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
    data = bytes.fromhex("0100ffff341200800100")
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
            "3E",
            0,
            0,
            0xFF,
            MC_DEFAULT_MONITOR_TIMER,
            parse_mc_address("M10.5"),
            1,
            True,
            False,
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
    collisions = {
        hex(code): devices for code, devices in by_code.items() if len(devices) > 1
    }
    assert not collisions, f"MC 设备码重码:{collisions}"


# 跨表重码守卫白名单:同码=**手册明确的别名/共享码**,非缺陷
# (review-1025 §8.3 P1:全库 6 张码表原只有 MC_DEVICE_CODES 一张有守卫,
#  其余表的"值快照"用例在实现被改错时会跟着一起绿——P0 防线只覆盖 1/6)
_CODE_TABLE_ALIASES = {
    # SH-080008 §18.4 自印 398:内部继电器 M/L/S 归同一码 4D20H,L/S 经 M 访问
    ("MC_1E_DEVICE_CODES", 0x4D20): {"M", "S"},
    # W342 §5-2-1/§5-2-2:T/C 共享码表,按地址区分(位=完成标志 09,字=PV 89)
    ("FINS_MEMORY_CODES", 0x09): {"T", "C"},
    ("FINS_MEMORY_CODES", 0x89): {"T", "C"},
}


def _code_table_cases() -> list:
    """(表名, 表) 对——全部协议面软元件码表(review-1025 §8.3 P1)。"""
    from omniplc.core import constants

    names = (
        "MC_DEVICE_CODES",
        "MC_1E_DEVICE_CODES",
        "KEYENCE_MC_DEVICE_CODES",
        "INOVANCE_MC_DEVICE_CODES",
        "PANASONIC_MC_DEVICE_CODES",
        "FINS_MEMORY_CODES",
    )
    return [(name, getattr(constants, name)) for name in names]


@pytest.mark.parametrize(
    "name,table",
    _code_table_cases(),
    ids=[name for name, _table in _code_table_cases()],
)
def test_all_device_code_tables_have_no_duplicate_codes(name: str, table: dict) -> None:
    """全部软元件码表逐表查重码(白名单外的同码 = 静默错址,P0 形态)。

    review-1025 §8.3 P1:守卫改为表驱动覆盖全部码表;别名类重码
    (``MC_1E`` 的 M/S、FINS 的 T/C)走显式白名单并注明手册依据。
    本用例锁**表内一致性**——各族"值快照"用例在实现被改错时会跟着
    一起绿,覆盖不到该形态。
    """
    by_code: dict = {}
    for device, value in table.items():
        by_code.setdefault(value[0], []).append(device)
    bad = {
        hex(code): devices
        for code, devices in by_code.items()
        if len(devices) > 1 and _CODE_TABLE_ALIASES.get((name, code)) != set(devices)
    }
    assert not bad, f"{name} 设备码重码(非已登记别名):{bad}"


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


def test_parse_response_1e_5b_abnormal_code_in_message() -> None:
    """1E 结束码 5BH:异常细分码入错误消息,0x10 标注 PC 号错。

    依据 SH-080008 §18.2 印刷页 394 算例「5BH + abnormal code 10H
    (PC No. error)」(review-1020 §七乙2 话术层)。
    """
    from omniplc.core.constants import MC_1E_READ_WORD

    # 5BH + 10H(PC 号错):消息带标注
    frame = bytes([MC_1E_READ_WORD + 0x80, 0x5B, 0x10])
    with pytest.raises(DeviceError) as exc_info:
        codec_a.parse_response(frame, 1, False, True)
    assert "PC 号错" in str(exc_info.value)
    # 其他细分码:带码值不带标注
    frame = bytes([MC_1E_READ_WORD + 0x80, 0x5B, 0x24])
    with pytest.raises(DeviceError) as exc_info:
        codec_a.parse_response(frame, 1, False, True)
    assert "0x24" in str(exc_info.value)
    assert "PC 号错" not in str(exc_info.value)


# ----------------------------------------------------------------------
# 随机读/写(0403/1402)与 CPU 型号(0101)——SH-080008 §8.3/§11.2 黄金样本
# ----------------------------------------------------------------------

# 手册软元件码:D=0xA8 T(N)=0xC2 M=0x90 X=0x9C Y=0x9D
_D, _TN, _M, _X, _Y = 0xA8, 0xC2, 0x90, 0x9C, 0x9D


def test_build_random_read_devices_golden() -> None:
    """随机读请求字面字节(SH-080008 §8.3 印刷页 101-103 二进制通信例原样)。

    条目序为**编号 3B 小端 → 码 1B**:字块 D0/T0/M100/X20、双字块
    D1500/Y160/M1111(X/Y 编号十六进制,故 X20 → 0x20、Y160 → 0x160)。
    字访问点数/双字访问点数为 **1 字节**(印刷页 102-103 请求例
    「03H 04H | 00H 00H | 04H | 03H | …」;review-1009 P0-1)。
    """
    request = codec_qna.build_random_read_devices(
        "3E",
        0,
        0,
        0xFF,
        0,
        [(_D, 0), (_TN, 0), (_M, 100), (_X, 0x20)],
        [(_D, 1500), (_Y, 0x160), (_M, 1111)],
    )
    core = "030400000403000000a8000000c2640000902000009cdc0500a86001009d57040090"
    # 副头部 5000 + 网络 00 + PC ff + IO ff03 + 局 00 + 长度 2400 + 定时器 0000
    assert request == bytes.fromhex("500000ffff030024000000" + core)


def test_parse_random_read_devices_response_golden() -> None:
    """随机读响应:字数据逐字小端 + 双字数据 4 字节小端(§8.3 印刷页 100-101)。"""
    data = (
        bytes.fromhex("9519")
        + bytes.fromhex("0212")
        + bytes.fromhex("3412")
        + bytes.fromhex("7856")
        + bytes.fromhex("4e4f544c")
        + bytes.fromhex("11110000")
        + bytes.fromhex("efcdab89")
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
            "3E",
            0,
            0,
            0xFF,
            0,
            [(_D, index) for index in range(193)],
            [],
        )


def test_build_random_write_devices_golden() -> None:
    """随机写请求字面字节(SH-080008 §8.3 印刷页 107-109 二进制通信例原样)。

    4 字 + 3 双字,条目序**编号 3B 小端 → 码 1B → 写数据**(字 2B/双字 4B
    小端):字块 D0/D256/M100/X20,双字块 D1500/Y160/M1111。字访问点数/
    双字访问点数为 **1 字节**(印刷页 109 请求例
    「02H 14H | 00H 00H | 04H | 03H | …」;review-1009 P0-1)。
    """
    request = codec_qna.build_random_write_devices(
        "3E",
        0,
        0,
        0xFF,
        0,
        [
            (0xA8, 0, 0x0550),
            (0xA8, 0x100, 0x0575),
            (0x90, 100, 0x0540),
            (0x9C, 0x20, 0x0583),
        ],
        [(0xA8, 1500, 0x04391202), (0x9D, 0x160, 0x23752607), (0x90, 1111, 0x04250475)],
    )
    core = (
        "02140000"
        "04"
        "03"
        "000000a8"
        "5005"
        "000100a8"
        "7505"
        "64000090"
        "4005"
        "2000009c"
        "8305"
        "dc0500a8"
        "02123904"
        "6001009d"
        "07267523"
        "57040090"
        "75042504"
    )
    assert request == bytes.fromhex("500000ffff030038000000" + core)


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
            "3E",
            0,
            0,
            0xFF,
            0,
            [(_D, index, 1) for index in range(161)],
            [],
        )


def test_read_cpu_model_golden() -> None:
    """CPU 型号请求/响应(§11.2 印刷页 178:Q02UCPU → 名 + 码 0x0263,线上小端 63 02)。"""
    request = codec_qna.build_read_cpu_model("3E")
    assert request == bytes.fromhex("500000ffff030006000a0001010000")
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


# ---------------------------------------------------------------- §8.2-2/3/4 路由字段


def test_check_station_number_accepts_ff_for_global() -> None:
    """站号 0xFF = 全站广播(review-1024 §8.2-2 P2)放行。

    依据 SH-080008 §6.2 自印 50 PDF P52 Station No. 表:
    0~31(00H~1FH)= 多点连接访问目标;FFH = global function 全局功能。
    """
    from omniplc.plc.melsec.codec_serial import check_station_number

    assert check_station_number(0xFF) == 0xFF
    # 边界 0 / 31 仍放行
    assert check_station_number(0) == 0
    assert check_station_number(0x1F) == 0x1F
    # 0x20 仍拒绝
    with pytest.raises(ValueError, match="站号必须在"):
        check_station_number(0x20)


def test_check_pc_number_accepts_special_7d_7e_fe() -> None:
    """PC 号 7DH/7EH/FEH 放行;0 拒绝(review-1024 §8.2-3 P2)。

    依据 SH-080008 §6.2 自印 52 PDF P54 Network No., PC No. 表:
    - 7DH = 指定控制站/主站
    - 7EH = 当前控制站/主站
    - FEH = on-demand 功能
    - 01H~78H = 1~120 经网络访问他站
    - FFH = 访问连接站
    手册合法集不含 0。
    """
    from omniplc.plc.melsec.codec_serial import check_pc_number

    assert check_pc_number(0x7D) == 0x7D
    assert check_pc_number(0x7E) == 0x7E
    assert check_pc_number(0xFE) == 0xFE
    assert check_pc_number(0xFF) == 0xFF
    # 边界 1 / 120 仍放行
    assert check_pc_number(1) == 1
    assert check_pc_number(120) == 120
    # 0 无手册依据,拒绝
    with pytest.raises(ValueError, match="PC 编号必须是"):
        check_pc_number(0)
    # 121 仍拒绝
    with pytest.raises(ValueError, match="PC 编号必须是"):
        check_pc_number(121)


def test_build_request_module_io_and_station_propagated() -> None:
    """3E/4E build_request 的 module_io / module_station 透传(review-1024 §8.2-4 P2)。

    自印 487 给出多 CPU 03E0H / 冗余 03D0H,原硬编码 0x03FF 改可配。
    帧布局:副头(2 字节)+ 路由(5 字节:网络 + PC + 模块 I/O LE 2 + 局号)
    + 数据长(2 字节)+ 监视定时器(2 字节)+ 命令(2 字节)...
    路由索引 = [2:7]。
    """
    # 默认 0x03FF
    default_req = codec_qna.build_request(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"),
        1,
        False,
        False,
    )
    # 路由 = [网络, PC, 模块 I/O LE 2, 局号] = 5 字节,在副头 [0:2] 之后
    assert default_req[2:7] == bytes([0x00, 0xFF, 0xFF, 0x03, 0x00])
    # 多 CPU 0x03E0
    multi_cpu = codec_qna.build_request(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"),
        1,
        False,
        False,
        module_io=0x03E0,
        module_station=0,
    )
    assert multi_cpu[2:7] == bytes([0x00, 0xFF, 0xE0, 0x03, 0x00])
    # 冗余 0x03D0
    redundant = codec_qna.build_request(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"),
        1,
        False,
        False,
        module_io=0x03D0,
        module_station=1,
    )
    assert redundant[2:7] == bytes([0x00, 0xFF, 0xD0, 0x03, 0x01])


def test_build_random_read_module_io_propagated() -> None:
    """3E/4E build_random_read 的 module_io / module_station 透传。"""
    request = codec_qna.build_random_read(
        "3E",
        0,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, 100, 1)],
        [],
        module_io=0x03E0,
        module_station=2,
    )
    # 路由 5 字节,副头 [0:2] 之后:[2:7]
    assert request[2:7] == bytes([0x00, 0xFF, 0xE0, 0x03, 0x02])
