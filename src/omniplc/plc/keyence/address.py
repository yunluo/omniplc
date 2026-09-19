"""基恩士 KV Host Link 地址解析。

支持的地址语法(不区分大小写),与 KEYENCE KV 软元件记号一致:

=========  =============================================
语法        含义
=========  =============================================
``DM100``   数据存储器(字,十进制)
``W100``    链接寄存器(字,十六进制)
``R515``    中继电器(位组:组号十进制 + 位号两位 00~15)
``B1F``     工作位(位,十六进制)
``X0F``     输入(组号十进制 + 位 1 位十六进制)
``Y10``     输出(同 X)
``M100``    扩展中继(位,十进制;L 同)
``DM100.5`` 字软元件位访问(omniplc 约定位号为十进制 0~15)
=========  =============================================

编号进位规则与 ``McAddress`` 一致:本模块只做语法拆分,编号按
软元件族的进制换算为整数,组帧时经 :func:`format_kv_device` 还原。
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

from ...core.constants import (
    KV_BIT_BANK_DEVICES,
    KV_BIT_DEVICES,
    KV_HEX_NUMBER_DEVICES,
    KV_WORD_DEVICES,
)

_TYPE_PATTERN = "|".join(
    sorted(list(KV_BIT_DEVICES) + list(KV_WORD_DEVICES), key=len, reverse=True)
)
_KV_ADDRESS_RE = re.compile(r"^({})([0-9A-F]+)(?:\.(\d+))?$".format(_TYPE_PATTERN))


class KvAddress(NamedTuple):
    """解析后的 KV 软元件地址。

    :ivar device: 软元件记号(大写),如 ``"DM"``、``"R"``
    :ivar number: 编号整数(位组软元件为 ``组号*100+位号``,X/Y 为 ``组号*16+位号``)
    :ivar bit: 位号(0~15,仅字软元件位访问时有值)
    """

    device: str
    number: int
    bit: Optional[int]

    def to_text(self) -> str:
        """还原为规范软元件文本(如 ``"DM100"``、``"R515"``、``"X0F"``)。"""
        return format_kv_device(self.device, self.number)


def parse_kv_address(address: str) -> KvAddress:
    """解析 KV 软元件地址字符串。

    :param address: 地址,如 ``"DM100"``、``"R515"``、``"X0F"``、``"DM100.5"``
    :return: :class:`KvAddress`
    :raises ValueError: 语法错误、软元件不支持或位号越界
    """
    if not address or not address.strip():
        raise ValueError("KV 地址不能为空")
    match = _KV_ADDRESS_RE.match(address.strip().upper())
    if match is None:
        raise ValueError(
            "无法解析 KV 地址:{!r}(示例:DM100 / R515 / B1F / X0F / DM100.5)".format(address)
        )
    device = match.group(1)
    number_text = match.group(2)
    bit = _parse_bit(match.group(3))
    if device in KV_BIT_BANK_DEVICES:
        number = int(number_text, 10)
        if number % 100 > 15:
            raise ValueError("位组软元件编号低两位必须在 00~15,收到:{!r}".format(address))
    elif device in KV_HEX_NUMBER_DEVICES:
        number = int(number_text, 16)
    elif device in ("X", "Y"):
        bank_text = "0" if len(number_text) == 1 else number_text[:-1]
        if not bank_text.isdigit():
            raise ValueError("X/Y 组号必须为十进制数字,收到:{!r}".format(address))
        number = int(bank_text, 10) * 16 + int(number_text[-1], 16)
    else:
        number = int(number_text, 10)
    if bit is not None and device in KV_BIT_DEVICES:
        raise ValueError("位软元件不支持位号后缀:{!r}(示例:R515 或 DM100.5)".format(address))
    return KvAddress(device=device, number=number, bit=bit)


def format_kv_device(device: str, number: int) -> str:
    """把软元件与编号还原为规范文本(组帧用)。"""
    if device in KV_BIT_BANK_DEVICES:
        return "{}{}{:02d}".format(device, number // 100, number % 100)
    if device in ("X", "Y"):
        return "{}{}{:X}".format(device, number // 16, number % 16)
    if device in KV_HEX_NUMBER_DEVICES:
        return device + format(number, "X")
    return "{}{}".format(device, number)


def offset_device(address: KvAddress, offset: int) -> KvAddress:
    """按字/逻辑位偏移软元件(连续访问用)。

    位组软元件(R/MR/CR)先换算为 16 位一组的逻辑号偏移,再还原为组表示;
    其余软元件直接对编号加偏移。
    """
    if address.device in KV_BIT_BANK_DEVICES:
        logical = (address.number // 100) * 16 + (address.number % 100) + offset
        return KvAddress(address.device, (logical // 16) * 100 + (logical % 16), address.bit)
    return KvAddress(address.device, address.number + offset, address.bit)


def is_bit_device(device: str) -> bool:
    """判断是否为位软元件。"""
    return device in KV_BIT_DEVICES


def _parse_bit(text: Optional[str]) -> Optional[int]:
    """位号解析与范围校验(内部函数)。"""
    if text is None:
        return None
    bit = int(text)
    if not 0 <= bit <= 15:
        raise ValueError("字软元件位号必须在 0~15 之间,收到:{}".format(bit))
    return bit
