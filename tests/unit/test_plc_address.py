"""MC/FINS 地址解析单元测试。"""
from __future__ import annotations

import pytest

from omniplc.plc.melsec.address import parse_mc_address
from omniplc.plc.omron.address import parse_fins_address


class TestMcAddress:
    """三菱软元件地址。"""

    def test_basic(self) -> None:
        parsed = parse_mc_address("D100")
        assert parsed.device == "D"
        assert parsed.number == "100"
        assert parsed.bit is None

    def test_hex_number(self) -> None:
        """十六进制软元件编号保留数字原文(X/W 在 3E/4E 下为十六进制)。"""
        assert parse_mc_address("X1F").number == "1F"
        assert parse_mc_address("W20").number == "20"

    def test_case_insensitive(self) -> None:
        assert parse_mc_address("d100").device == "D"

    def test_bit_access(self) -> None:
        parsed = parse_mc_address("D100.3")
        assert parsed.bit == 3

    @pytest.mark.parametrize("bad", ["", "D", "100", "D1.2.3"])
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_mc_address(bad)


class TestFinsAddress:
    """欧姆龙存储区地址。"""

    def test_basic(self) -> None:
        parsed = parse_fins_address("D100")
        assert parsed.area == "D"
        assert parsed.offset == 100

    def test_cio_bit(self) -> None:
        parsed = parse_fins_address("cio0.5")
        assert parsed.area == "CIO"
        assert parsed.offset == 0
        assert parsed.bit == 5

    @pytest.mark.parametrize("bad", ["", "D", "D-1"])
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_fins_address(bad)
