"""西门子 S7 地址解析(DB/I/Q/M,位与字节起点)。

支持的地址语法(不区分大小写):

======================  ==================================================
语法                    含义
======================  ==================================================
``DB1.DBX0.3``          DB 位(DB 号.字节.位,位 0~7)
``DB1.DBB4``            DB 字节起点(数据尺寸由 DataType 决定)
``DB1.DBW2``            同上(习惯记号,字)
``DB1.DBD6``            同上(习惯记号,双字)
``DB1.DBS20``           DB 内 S7 String 起点(供 read_string)
``M10.2``               Merker 位;``MB10``/``MW10``/``MD10`` 字节起点
``I0.0`` / ``IW64``     过程输入映像(PE)
``Q0.1`` / ``QW10``     过程输出映像(PA)
======================  ==================================================

尺寸语义:地址只定位**区域 + 字节起点**,读写字节数由显式 DataType 决定
(SHORT/USHORT 2 字节、INT/UINT 4 字节、LONG/ULONG 8 字节、FLOAT/DOUBLE
4/8 字节,大端序)——``DB1.DBD6`` 按 ``read_short`` 读即取 6~7 两字节。
解析失败统一抛 ``ValueError``(参数错误约定)。
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

_DB_RE = re.compile(r"^DB(\d+)\.DB([XBWDS])(\d+)(?:\.(\d+))?$", re.IGNORECASE)
_AREA_RE = re.compile(r"^([IQM])(?:(?:([BWD])(\d+))|(\d+)(?:\.(\d+))?)$", re.IGNORECASE)

_AREA_CODES = {"I": 0x81, "Q": 0x82, "M": 0x83, "DB": 0x84}
"""地址区域 → snap7 Cli_Area 码(PE/PA/MK/DB)。"""


class S7Address(NamedTuple):
    """S7 解析结果。

    :ivar area: 区域 ``"DB"``/``"I"``/``"Q"``/``"M"``
    :ivar db_number: DB 号(非 DB 区域为 0)
    :ivar byte_index: 字节起点
    :ivar bit: 位号(0~7,仅位访问;None = 字节起点访问)
    """

    area: str
    db_number: int
    byte_index: int
    bit: Optional[int]


def area_code(area: str) -> int:
    """把区域名换算为 snap7 Cli_Area 码(内部函数)。"""
    return _AREA_CODES[area]


def parse_s7_address(address: str) -> S7Address:
    """解析 S7 地址(内部函数,非法抛 ValueError)。"""
    text = address.strip() if isinstance(address, str) else ""
    match = _DB_RE.match(text)
    if match is not None:
        db_number = int(match.group(1))
        kind = match.group(2).upper()
        byte_index = int(match.group(3))
        bit_text = match.group(4)
        if kind == "X":
            if bit_text is None:
                raise ValueError(
                    f"DB 位地址需要位号:{address!r}(示例:DB1.DBX0.3)"
                )
            bit = int(bit_text)
            _check_bit(bit, address)
        else:
            if bit_text is not None:
                raise ValueError(
                    f"字节起点地址不带位号:{address!r}(位访问用 DBX,如 DB1.DBX0.3)"
                )
            bit = None
        return S7Address("DB", db_number, byte_index, bit)

    match = _AREA_RE.match(text)
    if match is not None:
        area = match.group(1).upper()
        if match.group(2) is not None:
            # B/W/D 记号(IW/QD/MB…)只定字节起点,不带位号
            return S7Address(area, 0, int(match.group(3)), None)
        byte_index = int(match.group(4))
        bit_text = match.group(5)
        bit = int(bit_text) if bit_text is not None else None
        if bit is not None:
            _check_bit(bit, address)
        return S7Address(area, 0, byte_index, bit)

    raise ValueError(
        f"S7 地址非法:{address!r}(示例:DB1.DBX0.3 / DB1.DBD6 / M10.2 / MW10 / IW64)"
    )


def _check_bit(bit: int, address: str) -> None:
    """位号范围校验 0~7(内部函数)。"""
    if not 0 <= bit <= 7:
        raise ValueError(f"S7 位号必须在 0~7 之间,收到:{address!r} 的 {bit}")
