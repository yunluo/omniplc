"""公共参数校验纯函数(各协议驱动共用)。

数值范围校验失败抛 ``ValueError``(参数错误约定,直接抛给调用方);
与"通信失败转 (False, None)/False"的内部异常约定相区分。
"""
from __future__ import annotations

from .constants import INT16_MAX, INT16_MIN, UINT16_MAX, UINT8_MAX
from ..types import PrimitiveValue


def require_bool(value: PrimitiveValue) -> bool:
    """校验布尔参数。"""
    if not isinstance(value, bool):
        raise ValueError("布尔量必须是 bool,收到:{}".format(type(value).__name__))
    return value


def require_int(value: PrimitiveValue) -> int:
    """校验整数参数(排除 bool)。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("整数必须是 int,收到:{}".format(type(value).__name__))
    return value


def require_float(value: PrimitiveValue) -> float:
    """校验浮点参数(接受 int/float,排除 bool)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("浮点量必须是数字,收到:{}".format(type(value).__name__))
    return float(value)


def check_int16(value: PrimitiveValue) -> int:
    """校验 16 位有符号整数范围,返回 0~65535 原始字。"""
    number = require_int(value)
    if not INT16_MIN <= number <= INT16_MAX:
        raise ValueError(f"short 超出范围 {INT16_MIN}~{INT16_MAX}:{number}")
    return number & 0xFFFF


def check_uint16(value: PrimitiveValue) -> int:
    """校验 16 位无符号整数范围。"""
    number = require_int(value)
    if not 0 <= number <= UINT16_MAX:
        raise ValueError(f"ushort 超出范围 0~{UINT16_MAX}:{number}")
    return number


def check_range(value: int, low: int, high: int, name: str) -> int:
    """校验整数在 ``[low, high]`` 范围内,越界抛 :class:`ValueError`,合法原值返回。

    供各驱动 32/64 位等定点范围校验复用(16 位请用 :func:`check_int16`/
    :func:`check_uint16`,它们还带换算语义)。
    """
    if not low <= value <= high:
        raise ValueError(f"{name} 超出范围 {low}~{high}:{value}")
    return value


def check_byte_field(name: str, value: int, maximum: int = UINT8_MAX) -> int:
    """校验单字节路由字段(0~maximum),非法抛 :class:`ValueError`,合法原值返回。

    供 MC 以太网帧路由字段(网络号/PC 号/模块 I/O/局号等)与串口帧
    站号字段共用。
    """
    if not 0 <= int(value) <= maximum:
        raise ValueError(f"{name} 必须在 0~{maximum} 之间,收到:{value}")
    return int(value)
