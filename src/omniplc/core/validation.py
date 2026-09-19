"""公共参数校验纯函数(各协议驱动共用)。

数值范围校验失败抛 ``ValueError``(参数错误约定,直接抛给调用方);
与"通信失败转 (False, None)/False"的内部异常约定相区分。
"""
from __future__ import annotations

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
    if not -32768 <= number <= 32767:
        raise ValueError("short 超出范围 -32768~32767:{}".format(number))
    return number & 0xFFFF


def check_uint16(value: PrimitiveValue) -> int:
    """校验 16 位无符号整数范围。"""
    number = require_int(value)
    if not 0 <= number <= 65535:
        raise ValueError("ushort 超出范围 0~65535:{}".format(number))
    return number
