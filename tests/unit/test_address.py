"""Modbus 地址解析单元测试。"""
from __future__ import annotations

import pytest

from omniplc.modbus.address import ModbusArea, parse_address


class TestPrefixSyntax:
    """前缀语法。"""

    def test_areas(self) -> None:
        assert parse_address("hr0").area is ModbusArea.HOLDING_REGISTER
        assert parse_address("hr0").offset == 0
        assert parse_address("C7").area is ModbusArea.COIL
        assert parse_address("C7").offset == 7
        assert parse_address("di10").area is ModbusArea.DISCRETE_INPUT
        assert parse_address("ir3").area is ModbusArea.INPUT_REGISTER

    def test_bit_access(self) -> None:
        parsed = parse_address("hr0.15")
        assert parsed.bit == 15

    def test_bit_on_coil_invalid(self) -> None:
        with pytest.raises(ValueError):
            parse_address("c0.3")

    def test_bit_range_invalid(self) -> None:
        with pytest.raises(ValueError):
            parse_address("hr0.16")


class TestModiconSyntax:
    """Modicon 1 基风格。"""

    def test_ranges(self) -> None:
        assert parse_address("00001").area is ModbusArea.COIL
        assert parse_address("00001").offset == 0
        assert parse_address("10001").area is ModbusArea.DISCRETE_INPUT
        assert parse_address("10001").offset == 0
        assert parse_address("30001").area is ModbusArea.INPUT_REGISTER
        assert parse_address("30001").offset == 0
        assert parse_address("40001").area is ModbusArea.HOLDING_REGISTER
        assert parse_address("40001").offset == 0
        assert parse_address("40100").offset == 99

    def test_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            parse_address("50001")
        with pytest.raises(ValueError):
            parse_address("20001")  # 2xxxx 区段不存在


class TestErrors:
    """非法输入。"""

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "x1", "hr", "hr-1", "hr1.2.3", "dq0"],
    )
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_address(bad)


class TestFunctionCodes:
    """区域对应的功能码。"""

    def test_read(self) -> None:
        assert parse_address("c0").read_function_code == 1
        assert parse_address("di0").read_function_code == 2
        assert parse_address("hr0").read_function_code == 3
        assert parse_address("ir0").read_function_code == 4

    def test_write_ir_not_writable(self) -> None:
        with pytest.raises(ValueError):
            parse_address("ir0").write_single_function_code
