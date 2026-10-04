"""汇川 H3U/H5U 地址解析与 Modbus 地址换算。

汇川小型 PLC 的 TCP/串口通信本质是标准 Modbus(网口 Modbus TCP 502,
串口 Modbus RTU),但软元件记号按汇川习惯(M/S/X/Y/D/R...)给出,
需按下表换算为 Modbus 线圈/保持寄存器地址:

=========  ==========  ============================  ==========
软元件     类型        Modbus 换算                   备注
=========  ==========  ============================  ==========
``M0~``    线圈        编号即偏移                    H5U 到 M7999;H3U M8000~ 从 0x1F40 连续
``SM0~``   线圈        基址 0x2400 + 编号            H3U
``S0~``    线圈        基址 0xE000 + 编号
``T0~``    线圈        基址 0xF000 + 编号            定时器接点(位访问)
``C0~``    线圈        基址 0xF400 + 编号            计数器接点(位访问)
``X0~``    线圈        基址 0xF800 + 编号(八进制)    H5U X0~X1777;H3U 仅 X0~X377
``Y0~``    线圈        基址 0xFC00 + 编号(八进制)    H5U Y0~Y1777;H3U 仅 Y0~Y377
``B0~``    线圈        基址 0x3000 + 编号            H5U
``D0~``    保持寄存器  基址 0x0000 + 编号
``SD0~``   保持寄存器  基址 0x2400 + 编号            H3U
``R0~``    保持寄存器  基址 0x3000 + 编号
``T0~``    保持寄存器  基址 0xF000 + 编号            定时器当前值(字访问,0~255)
``C0~``    保持寄存器  基址 0xF400 + 编号            C0~C199 为 16 位当前值
``C200~``  保持寄存器  基址 0xF700 + 2×(编号-200)    32 位计数器,每只占两个 16 位寄存器
=========  ==========  ============================  ==========

T/C 为位/字双性质软元件:位访问(BOOL)取接点,字访问取当前值,
因此 :func:`to_modbus_address` 需要 ``is_bool`` 区分。C200~C255 为 32 位
计数器:双寄存器展开 + 类型门控见 :func:`check_counter_word_type`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Union

from ...core.constants import (
    ADDRESS_CACHE_MAXSIZE,
    INOVANCE_BIT_DEVICES,
    INOVANCE_C32_BASE,
    INOVANCE_C32_FIRST,
    INOVANCE_C32_LAST,
    INOVANCE_OCTAL_DEVICES,
    INOVANCE_WORD_DEVICES,
    MODBUS_REGISTER_BIT_MAX,
)
from ...core.types import DataType
from ...core.i18n import _

_INOVANCE_ADDRESS_RE = re.compile(
    r"^(SM|SD|M|S|T|C|X|Y|B|D|R)(\d+)(?:\.(\d+))?$", re.IGNORECASE
)

_C32_ALLOWED_TYPES = frozenset((DataType.INT, DataType.UINT, DataType.FLOAT))
"""32 位计数器(C200~C255)可用的数据类型:32 位整型 + 32 位浮点。"""


@dataclass(frozen=True)
class InovanceAddress:
    """解析后的汇川软元件地址。

    :ivar device: 软元件记号(大写),如 ``"D"``、``"M"``、``"SM"``
    :ivar number: 编号(X/Y 已从八进制换算为十进制)
    :ivar bit: 位号(0~15,仅字软元件位访问时有值)
    """

    device: str
    number: int
    bit: Optional[int] = None


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)
def parse_inovance_address(address: str) -> InovanceAddress:
    """解析汇川软元件地址字符串。

    :param address: 地址,如 ``"D100"``、``"M8000"``、``"X17"``、``"D100.3"``
    :return: :class:`InovanceAddress`(编号范围校验在换算阶段按访问类型进行)
    :raises ValueError: 语法错误、八进制编号非法或位号非法
    """
    if not address or not address.strip():
        raise ValueError(_("汇川地址不能为空"))
    match = _INOVANCE_ADDRESS_RE.match(address.strip())
    if match is None:
        raise ValueError(
            _("无法解析汇川地址:{!r}(示例:D100 / M10 / X17 / SD10 / D100.3)").format(
                address
            )
        )
    device = match.group(1).upper()
    number_text = match.group(2)
    bit = int(match.group(3)) if match.group(3) is not None else None
    if device in INOVANCE_OCTAL_DEVICES:
        if "8" in number_text or "9" in number_text:
            raise ValueError(
                _("软元件 {} 编号为八进制,不能包含 8/9:{!r}").format(device, address)
            )
        number = int(number_text, 8)
        _check_bitless(device, bit, address)
    elif device in INOVANCE_BIT_DEVICES:
        _check_bitless(device, bit, address)
        number = int(number_text)
    else:
        number = int(number_text)
        if bit is not None and not 0 <= bit <= MODBUS_REGISTER_BIT_MAX:
            raise ValueError(
                _("寄存器位号必须在 0~{} 之间,收到:{}").format(
                    MODBUS_REGISTER_BIT_MAX, bit
                )
            )
    return InovanceAddress(device=device, number=number, bit=bit)


def to_modbus_address(
    address: Union[InovanceAddress, str], is_bool: bool = False
) -> str:
    """把汇川地址换算为本库标准 Modbus 地址文本。

    位访问(BOOL):位软元件 → 线圈(如 ``M10`` → ``c10``、``X17`` → 八进制
    换算后的线圈偏移);字软元件不定位号的 BOOL 走保持寄存器提取 bit0。
    字访问:字软元件 → 保持寄存器(``D100`` → ``hr100``,``D100.3`` →
    ``hr100.3``);纯位软元件不支持字访问;**32 位计数器** ``C200~C255`` →
    ``hr{0xF700 + 2×(编号-200)}``(每只占两个 16 位寄存器空间,H3U 9.4.3
    印刷页 575-576 算例 C205 → 0xF70A)。

    :param address: :class:`InovanceAddress` 或地址字符串
    :param is_bool: 是否按位访问(T/C 双性质软元件据此选择接点/当前值)
    :return: 标准 Modbus 地址文本,交由 Modbus 客户端解析
    :raises ValueError: 地址非法或编号越界
    """
    parsed = (
        address
        if isinstance(address, InovanceAddress)
        else parse_inovance_address(address)
    )
    if is_bool and parsed.device in INOVANCE_BIT_DEVICES:
        base, limit = INOVANCE_BIT_DEVICES[parsed.device]
        _check_number(parsed.device, parsed.number, limit)
        return "c{}".format(base + parsed.number)
    if parsed.device in INOVANCE_WORD_DEVICES:
        base, limit = INOVANCE_WORD_DEVICES[parsed.device]
        _check_number(parsed.device, parsed.number, limit)
        if parsed.device == "C" and parsed.number >= INOVANCE_C32_FIRST:
            # 32 位计数器:双寄存器展开,位号后缀未定义(接点位访问请走 BOOL)
            if parsed.bit is not None:
                raise ValueError(
                    _(
                        "C{}(32 位计数器)不支持位号后缀:{!r}(接点位访问请用 BOOL 读)"
                    ).format(parsed.number, address)
                )
            return "hr{}".format(
                INOVANCE_C32_BASE + 2 * (parsed.number - INOVANCE_C32_FIRST)
            )
        if parsed.bit is not None:
            return "hr{}.{}".format(base + parsed.number, parsed.bit)
        return "hr{}".format(base + parsed.number)
    raise ValueError(_("软元件 {} 为位软元件,不支持字访问").format(parsed.device))


def check_counter_word_type(parsed: InovanceAddress, data_type: DataType) -> None:
    """32 位计数器(C200~C255)访问类型门控(公开辅助函数)。

    H3U 手册 9.4.3(印刷页 575-576):C200~C255 为 **32 位**寄存器,每只占
    两个 16 位寄存器空间(算例 C205~C208 → Modbus 地址 0xF70A、数量 8),
    且**32 位寄存器不支持写单个寄存器(FC06)**。按 16 位类型读到的只是
    计数器半字、64 位会跨两只计数器,故本库只放行 32 位类型
    (INT/UINT/FLOAT)——双寄存器展开与 FC16 写由 Modbus 层按类型自动选择。

    :param parsed: 已解析的汇川地址
    :param data_type: 本次访问的数据类型
    :raises ValueError: 以 16/64 位或字符串类型访问 32 位计数器
    """
    if parsed.device != "C" or parsed.number < INOVANCE_C32_FIRST:
        return
    if data_type not in _C32_ALLOWED_TYPES:
        raise ValueError(
            _(
                "C{} 为 32 位计数器(C{}~{}),仅支持 32 位类型(INT/UINT/FLOAT),收到:{}"
            ).format(parsed.number, INOVANCE_C32_FIRST, INOVANCE_C32_LAST, data_type)
        )


def _check_number(device: str, number: int, limit: int) -> None:
    """按软元件编号上限校验(内部函数)。"""
    if not 0 <= number < limit:
        raise ValueError(
            _("软元件 {} 编号超出范围 0~{}:{}").format(device, limit - 1, number)
        )


def _check_bitless(device: str, bit: Optional[int], address: str) -> None:
    """位软元件不允许位号后缀(内部函数)。"""
    if bit is not None:
        raise ValueError(
            _("软元件 {} 本身就是位地址,不支持位号后缀:{!r}").format(device, address)
        )
