"""core/validation.py 纯函数直测(review-1022 P3-4)。

这些函数是**全部驱动的参数校验闸**:口径漂移(如 ``require_int`` 开始
接受 bool、``require_count`` 开始接受 0)会同时污染 18 族协议的错误面;
此前仅经驱动路径间接覆盖,本文件逐函数锁定 bool 排除、边界值(inclusive)、
异常类型与文案关键字。
"""

from __future__ import annotations

import pytest

from omniplc.core.validation import (
    check_byte_field,
    check_int16,
    check_range,
    check_uint16,
    require_bool,
    require_count,
    require_float,
    require_int,
)

# ----------------------------------------------------------------------
# require_* 类型闸
# ----------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_require_bool_accepts_strict_bool(value: bool) -> None:
    assert require_bool(value) is value


@pytest.mark.parametrize("value", [1, 0, "True", None])
def test_require_bool_rejects_non_bool(value: object) -> None:
    with pytest.raises(ValueError, match="bool"):
        require_bool(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, False, 5.0, "5", None])
def test_require_int_rejects_bool_and_non_int(value: object) -> None:
    """bool 是 int 子类,必须显式排除(review-1008:静默收窄废止口径)。"""
    with pytest.raises(ValueError, match="int"):
        require_int(value)  # type: ignore[arg-type]


def test_require_int_accepts_int() -> None:
    assert require_int(5) == 5
    assert require_int(-3) == -3


@pytest.mark.parametrize("value", [True, False, "5", None])
def test_require_float_rejects_non_numeric(value: object) -> None:
    with pytest.raises(ValueError, match="数字"):
        require_float(value)  # type: ignore[arg-type]


def test_require_float_accepts_int_and_float() -> None:
    assert require_float(5) == 5.0
    assert require_float(-1.5) == -1.5


# ----------------------------------------------------------------------
# 范围闸(边界 inclusive)
# ----------------------------------------------------------------------


def test_check_int16_boundary_and_raw_word() -> None:
    """16 位有符号:边界 inclusive,越界拒绝;返回 0~65535 原始字。"""
    assert check_int16(-32768) == 0x8000
    assert check_int16(32767) == 0x7FFF
    assert check_int16(-1) == 0xFFFF
    for bad in (-32769, 32768):
        with pytest.raises(ValueError, match="short"):
            check_int16(bad)


def test_check_uint16_boundary() -> None:
    assert check_uint16(0) == 0
    assert check_uint16(65535) == 65535
    for bad in (-1, 65536):
        with pytest.raises(ValueError, match="ushort"):
            check_uint16(bad)


def test_check_range_inclusive_boundary() -> None:
    assert check_range(1, 1, 10, "x") == 1
    assert check_range(10, 1, 10, "x") == 10
    for bad in (0, 11):
        with pytest.raises(ValueError, match="x"):
            check_range(bad, 1, 10, "x")


def test_check_range_rejects_non_int() -> None:
    """review-1046 C7:范围闸先过 ``require_int``——真值比较不是类型校验。

    旧实现只比大小,``1.5``/``True`` 原样通过(``1.5`` 随后落到
    ``struct.pack``/``to_bytes`` 才炸 ``struct.error``/``OverflowError``,
    ``True`` 被当 1 静默收下),与 :func:`require_int`/:func:`check_byte_field`
    的类型口径不一致。
    """
    for bad in (1.5, True, "5", None):
        with pytest.raises(ValueError, match="int"):
            check_range(bad, 0, 10, "x")  # type: ignore[arg-type]
    # 合法 int 行为不变(含负数区间)
    assert check_range(-5, -10, 0, "x") == -5


def test_check_byte_field_excludes_bool_and_overflow() -> None:
    """单字节路由字段:0~255;bool/float 不再被 int() 静默收窄。"""
    assert check_byte_field("pc", 0) == 0
    assert check_byte_field("pc", 255) == 255
    with pytest.raises(ValueError, match="pc"):
        check_byte_field("pc", 256)
    # bool 由 require_int 闸拒绝(文案为类型闸的「整数必须是 int」)
    with pytest.raises(ValueError, match="int"):
        check_byte_field("pc", True)  # type: ignore[arg-type]


def test_require_count_rejects_zero_and_bool() -> None:
    """数量参数:≥1 整数;0/负数/bool/浮点全拒,文案带参数名。"""
    assert require_count(1) == 1
    assert require_count(7, name="length") == 7
    for bad in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError, match="count"):
            require_count(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="length"):
        require_count(0, name="length")
