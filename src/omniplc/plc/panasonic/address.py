"""MEWTOCOL 地址解析。

地址语法(不区分大小写):

============  ===========================================================
语法          含义
============  ===========================================================
``R000F``     接点区(位):区代码 X/Y/R/T/C/L + 十进制字号 + 位号
``R0.15``     同上,点号形式(字号.位号,位号十进制 0~15)
``D100``      数据区(字):区代码 D/L/F/S/K + 十进制编号
``D100.3``    数据区位访问(读-改-写,位号 0~15)
============  ===========================================================

接点号按松下手册口径是"字号(十进制)+位号(十六进制一位)":
``X000F`` = 字 0 位 F(第 15 号输入),``R21`` = 字 2 位 1。
在梯形图工具里以线性继电器号思考的用户请用 ``R1.5``(= 第 21 号)。
``L`` 兼作链接继电器(位)与链接寄存器 LT(字):位访问语境按接点、
字访问语境按 LT 解析,由 ``is_bit`` 参数决定。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple, Optional

from ...core.constants import MEWTOCOL_CONTACT_AREAS

# 接点:区代码 + 数字串(末位可为十六进制位号,可选 .十进制位号点号形式)
_CONTACT_RE = re.compile(r"^([XYRTCL])(\d*[0-9A-Fa-f])(?:\.(\d+))?$")
# 数据:区代码 + 十进制编号(+ 可选 .位号)
_DATA_RE = re.compile(r"^([DLFSK])(\d+)(?:\.(\d+))?$")


class MewtocolAddress(NamedTuple):
    """解析后的 MEWTOCOL 软元件地址。

    :ivar area: 区代码(大写),接点区 ``X/Y/R/T/C/L`` 或数据区 ``D/L/F/S/K``
    :ivar word: 字号(接点)或编号(数据),十进制
    :ivar bit: 位号 0~15(接点必有;数据区位访问时有值,字访问为 None)
    """

    area: str
    word: int
    bit: Optional[int]


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=4096)
def parse_mewtocol_address(address: str, is_bit: bool) -> MewtocolAddress:
    """解析 MEWTOCOL 软元件地址字符串。

    :param address: 地址,如 ``"R000F"``、``"R1.15"``、``"D100"``
    :param is_bit: 是否位访问语境(决定 ``L`` 按接点还是按 LT 解析)
    :return: :class:`MewtocolAddress`
    :raises ValueError: 语法错误、缺少位号或软元件不支持该访问方式
    """
    if not address or not address.strip():
        raise ValueError("MEWTOCOL 地址不能为空")
    text = address.strip().upper()

    # 位访问语境优先按接点区解析(L=链接继电器);字访问语境优先按数据区(L=LT)
    contact = _CONTACT_RE.match(text)
    data = _DATA_RE.match(text)
    if is_bit:
        matched_contact, matched_data = contact, data
    else:
        matched_contact, matched_data = None, data

    if matched_contact is not None:
        return _build_contact(matched_contact, address)
    if matched_data is not None:
        return _build_data(matched_data, address, is_bit)
    if not is_bit and contact is not None:
        raise ValueError(
            "MEWTOCOL 接点区软元件 {} 不支持字访问:{!r}"
            "(定时器/计数器当前值请用 S 区=设定值、K 区=经过值)".format(
                contact.group(1), address
            )
        )
    raise ValueError(
        f"无法解析 MEWTOCOL 地址:{address!r}(示例:R000F / R1.15 / D100)"
    )


def _build_contact(match: "re.Match[str]", address: str) -> MewtocolAddress:
    """构造接点地址(内部函数)。

    无点号时数字串**末位恒为位号**(十六进制 0~F),其余为十进制字号:
    ``R1F`` = 字 1 位 F,``L10`` = 字 1 位 0;至少需要字号+位号两位数字。
    """
    area = match.group(1)
    digits = match.group(2)
    if match.group(3) is not None:
        bit = int(match.group(3))
        if not 0 <= bit <= 15:
            raise ValueError(f"MEWTOCOL 接点位号必须在 0~15 之间,收到:{bit}")
        word = int(digits, 10) if digits else 0
        return MewtocolAddress(area=area, word=word, bit=bit)
    if not digits or len(digits) < 2:
        raise ValueError(
            "MEWTOCOL 接点地址缺少位号:{!r}"
            "(字号+位号形式如 R000F、R1F,点号形式如 R1.15)".format(address)
        )
    bit = int(digits[-1], 16)
    word = int(digits[:-1], 10)
    return MewtocolAddress(area=area, word=word, bit=bit)


def _build_data(match: "re.Match[str]", address: str, is_bit: bool) -> MewtocolAddress:
    """构造数据区地址(内部函数)。"""
    area = match.group(1)
    number = int(match.group(2), 10)
    bit = int(match.group(3)) if match.group(3) is not None else None
    if is_bit and bit is None:
        raise ValueError(
            "MEWTOCOL 字软元件 {} 位访问需要 .位号 后缀:{!r}(如 D100.3);"
            "接点区软元件为 {}".format(area, address, "/".join(MEWTOCOL_CONTACT_AREAS))
        )
    if bit is not None and not 0 <= bit <= 15:
        raise ValueError(f"MEWTOCOL 字软元件位号必须在 0~15 之间,收到:{bit}")
    return MewtocolAddress(area=area, word=number, bit=bit)
