"""丰田 TOYOPUC 计算机链接地址解析。

支持的地址语法(不区分大小写),编号为**十六进制**(与 TOYOPUC 手册
示例一致,如 ``D0100`` = 0x0100):

===========  =====================================  ================
语法          含义                                    协议访问
===========  =====================================  ================
``D0100``    字软元件(S/N/R/D/B)                     字(CMD=1C/1D)
``D0100L``   字软元件低字节(``H`` 为高字节)           字节(CMD=1E/1F)
``M0100``    位软元件(P/K/V/T/C/L/X/Y/M)              位(CMD=20/21)
``M0100L``   位软元件单字节(低字节;``H`` 为高字节)    字节
``M0100W``   位软元件打包字                           字
===========  =====================================  ================

软元件基地址与编号段范围来自 TOYOPUC 计算机链接手册:

- 字访问 ``地址 = 字基地址 + 编号``,全表线性;
- 位访问 ``地址 = 位基地址 + 编号``,编号必须在
  :data:`_BIT_SEGMENTS` 的段内(L/M 有 0x1000 起的第二段);
- 字节访问 ``地址 = 字节基地址 + 编号×2 + (H ? 1 : 0)``。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, NamedTuple, Tuple

from ...core.constants import ADDRESS_CACHE_MAXSIZE, TOYOPUC_BIT_DEVICES, TOYOPUC_WORD_DEVICES

_TOYOPUC_ADDRESS_RE = re.compile(r"^([A-Z]{1,2})([0-9A-F]+)(L|H|W)?$")

_WORD_BASE: Dict[str, int] = {
    "P": 0x0000, "K": 0x0020, "V": 0x0050, "T": 0x0060, "C": 0x0060,
    "L": 0x0080, "X": 0x0100, "Y": 0x0100, "M": 0x0180,
    "S": 0x0200, "N": 0x0600, "R": 0x0800, "D": 0x1000, "B": 0x6000,
}
"""字访问基地址(CMD=1C/1D 地址 = 基地址 + 编号)。"""

_BYTE_BASE: Dict[str, int] = {
    "P": 0x0000, "K": 0x0040, "V": 0x00A0, "T": 0x00C0, "C": 0x00C0,
    "L": 0x0100, "X": 0x0200, "Y": 0x0200, "M": 0x0300,
    "S": 0x0400, "N": 0x0C00, "R": 0x1000, "D": 0x2000, "B": 0xC000,
}
"""字节访问基地址(CMD=1E/1F 地址 = 基地址 + 编号×2 + 高字节位)。"""

_BIT_BASE: Dict[str, int] = {
    "P": 0x0000, "K": 0x0200, "V": 0x0500, "T": 0x0600, "C": 0x0600,
    "L": 0x0800, "X": 0x1000, "Y": 0x1000, "M": 0x1800,
}
"""位访问基地址(CMD=20/21 地址 = 基地址 + 编号)。"""

_BIT_SEGMENTS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "P": ((0x000, 0x1FF),),
    "K": ((0x000, 0x2FF),),
    "V": ((0x000, 0x0FF),),
    "T": ((0x000, 0x1FF),),
    "C": ((0x000, 0x1FF),),
    "L": ((0x000, 0x7FF), (0x1000, 0x2FFF)),
    "X": ((0x000, 0x7FF),),
    "Y": ((0x000, 0x7FF),),
    "M": ((0x000, 0x7FF), (0x1000, 0x17FF)),
}
"""位软元件合法编号段(位访问校验用);L/M 存在 0x1000 起的第二段。"""

_PACKED_SEGMENTS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    area: tuple((start >> 4, end >> 4) for start, end in segments)
    for area, segments in _BIT_SEGMENTS.items()
}
"""位软元件打包编号段(W/L/H 字节·打包字访问校验用,编号右移 4 位)。"""


class ToyopucAddress(NamedTuple):
    """解析后的 TOYOPUC 软元件地址。

    :ivar area: 软元件记号(大写),如 ``"D"``、``"M"``
    :ivar number: 编号整数(十六进制换算,如 ``"D0100"`` → 0x0100)
    :ivar suffix: 后缀,``""``(位/字)、``"L"``/``"H"``(字节)、``"W"``(打包字)
    """

    area: str
    number: int
    suffix: str

    @property
    def unit(self) -> str:
        """访问单位:``"bit"``(位软元件无后缀)/ ``"byte"``(L/H)/ ``"word"``。"""
        if self.suffix in ("L", "H"):
            return "byte"
        if self.suffix == "W":
            return "word"
        return "bit" if self.area in TOYOPUC_BIT_DEVICES else "word"

    @property
    def high(self) -> bool:
        """是否为高字节(仅 ``"H"`` 后缀为 True)。"""
        return self.suffix == "H"


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)
def parse_toyopuc_address(address: str) -> ToyopucAddress:
    """解析 TOYOPUC 软元件地址字符串。

    :param address: 地址,如 ``"D0100"``、``"M0201"``、``"X0010H"``、``"M0201W"``
    :return: :class:`ToyopucAddress`
    :raises ValueError: 语法错误、软元件不支持或编号越界
    """
    if not address or not address.strip():
        raise ValueError("TOYOPUC 地址不能为空")
    match = _TOYOPUC_ADDRESS_RE.match(address.strip().upper())
    if match is None:
        raise ValueError(
            f"无法解析 TOYOPUC 地址:{address!r}(示例:D0100 / M0201 / X0010H / M0201W)"
        )
    area = match.group(1)
    if area not in _WORD_BASE:
        raise ValueError(
            "不支持的 TOYOPUC 软元件:{!r}(支持:{})".format(
                address, " ".join(TOYOPUC_WORD_DEVICES + TOYOPUC_BIT_DEVICES)
            )
        )
    number_text = match.group(2)
    number = int(number_text, 16)
    if number > 0xFFFF:
        raise ValueError(f"TOYOPUC 软元件编号超出 16 位范围:{address!r}")
    suffix = match.group(3) or ""
    if suffix == "W" and area not in TOYOPUC_BIT_DEVICES:
        raise ValueError(f"W 后缀仅支持位软元件打包字访问:{address!r}")
    if suffix == "" and area in TOYOPUC_BIT_DEVICES:
        _require_in_segments(address, number, _BIT_SEGMENTS[area])
    if suffix in ("L", "H", "W") and area in TOYOPUC_BIT_DEVICES:
        _require_in_segments(address, number >> 4, _PACKED_SEGMENTS[area])
    return ToyopucAddress(area=area, number=number, suffix=suffix)


def encode_word_address(parsed: ToyopucAddress) -> int:
    """把字访问地址编码为协议地址(CMD=1C/1D,内部函数)。"""
    if parsed.unit != "word":
        raise ValueError(f"期望字访问地址,收到:{parsed!r}")
    return _WORD_BASE[parsed.area] + parsed.number


def encode_byte_address(parsed: ToyopucAddress) -> int:
    """把字节访问地址编码为协议地址(CMD=1E/1F,内部函数)。"""
    if parsed.unit != "byte":
        raise ValueError(f"期望字节访问地址,收到:{parsed!r}")
    return _BYTE_BASE[parsed.area] + parsed.number * 2 + (1 if parsed.high else 0)


def encode_bit_address(parsed: ToyopucAddress) -> int:
    """把位访问地址编码为协议地址(CMD=20/21,内部函数)。"""
    if parsed.unit != "bit":
        raise ValueError(f"期望位访问地址,收到:{parsed!r}")
    return _BIT_BASE[parsed.area] + parsed.number


def _require_in_segments(address: str, index: int, segments: Tuple[Tuple[int, int], ...]) -> None:
    """校验编号落在任一合法段内(内部函数)。"""
    if not any(start <= index <= end for start, end in segments):
        raise ValueError(f"TOYOPUC 软元件编号越界:{address!r}")
