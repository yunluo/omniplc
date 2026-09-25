"""convert.py 纯转换函数的单元测试。"""
from __future__ import annotations

import struct

import pytest

from omniplc import convert
from omniplc.types import ByteOrder, DataType, WordOrder


class TestChecksum:
    """校验和函数。"""

    def test_crc16_known_vector(self) -> None:
        # 经典 Modbus 请求帧 01 03 00 00 00 02 的 CRC = 0x0BC4(线上低字节在前)
        assert convert.crc16(b"\x01\x03\x00\x00\x00\x02") == 0x0BC4

    def test_crc16_empty(self) -> None:
        assert convert.crc16(b"") == 0xFFFF

    def test_lrc_known_vector(self) -> None:
        assert convert.lrc(b"\x01\x03\x00\x00\x00\x02") == 0xFA


class TestBitOps:
    """位操作。"""

    def test_get_bit(self) -> None:
        assert convert.get_bit(0b1010, 1) is True
        assert convert.get_bit(0b1010, 0) is False
        assert convert.get_bit(0x8000, 15) is True

    def test_set_bit(self) -> None:
        assert convert.set_bit(0, 3, True) == 0b1000
        assert convert.set_bit(0b1000, 3, False) == 0
        assert convert.set_bit(0b1010, 0, True) == 0b1011

    def test_bit_range_error(self) -> None:
        with pytest.raises(ValueError):
            convert.get_bit(0, 64)
        with pytest.raises(ValueError):
            convert.set_bit(0, -1, True)


class TestIntBytes:
    """字节 ↔ 整数。"""

    def test_short_roundtrip(self) -> None:
        assert convert.bytes_to_short(b"\xff\xfe") == -2
        assert convert.short_to_bytes(-2) == b"\xff\xfe"
        assert convert.short_to_bytes(-2, "little") == b"\xfe\xff"

    def test_ushort(self) -> None:
        assert convert.bytes_to_ushort(b"\xff\xff") == 65535
        assert convert.ushort_to_bytes(65535) == b"\xff\xff"

    def test_byteorder_enum(self) -> None:
        # ByteOrder 枚举与字符串等价
        assert convert.bytes_to_short(b"\x00\x05", ByteOrder.BIG) == 5
        assert convert.bytes_to_short(b"\x05\x00", ByteOrder.LITTLE) == 5
        assert convert.short_to_bytes(5, ByteOrder.LITTLE) == b"\x05\x00"

    def test_byteorder_invalid(self) -> None:
        with pytest.raises(ValueError):
            convert.bytes_to_short(b"\x00\x05", "middle")


class TestWordOrder32:
    """32 位字序:ABCD/CDAB/BADC/DCBA。"""

    FLOAT = struct.pack(">f", 12.5)  # 0x41 48 00 00 → A=0x41 B=0x48 C=0x00 D=0x00

    def test_abcd(self) -> None:
        regs = convert.float32_to_registers(12.5, WordOrder.ABCD)
        assert regs == (0x4148, 0x0000)
        assert convert.registers_to_float32([0x4148, 0x0000], WordOrder.ABCD) == pytest.approx(12.5)

    def test_cdab(self) -> None:
        # 现场最常见的"高字在后":寄存器顺序颠倒
        regs = convert.float32_to_registers(12.5, WordOrder.CDAB)
        assert regs == (0x0000, 0x4148)
        assert convert.registers_to_float32(regs, WordOrder.CDAB) == pytest.approx(12.5)

    def test_badc(self) -> None:
        regs = convert.float32_to_registers(12.5, WordOrder.BADC)
        assert regs == (0x4841, 0x0000)
        assert convert.registers_to_float32(regs, WordOrder.BADC) == pytest.approx(12.5)

    def test_dcba(self) -> None:
        regs = convert.float32_to_registers(12.5, WordOrder.DCBA)
        assert regs == (0x0000, 0x4841)
        assert convert.registers_to_float32(regs, WordOrder.DCBA) == pytest.approx(12.5)

    def test_int32_cdab(self) -> None:
        assert convert.registers_to_int32([0x0001, 0x0000], WordOrder.ABCD) == 65536
        assert convert.registers_to_int32([0x0000, 0x0001], WordOrder.CDAB) == 65536
        assert convert.int32_to_registers(-1, WordOrder.ABCD) == (0xFFFF, 0xFFFF)

    def test_uint32(self) -> None:
        assert convert.registers_to_uint32([0xFFFF, 0xFFFF]) == 4294967295


class TestWordOrder64:
    """64 位字序。"""

    def test_double_roundtrip_all_orders(self) -> None:
        for order in WordOrder:
            regs = convert.float64_to_registers(-123.456, order)
            assert len(regs) == 4
            assert convert.registers_to_float64(regs, order) == pytest.approx(-123.456)

    def test_double_abcd_pattern(self) -> None:
        data = struct.pack(">d", 1.0)  # 3F F0 00 00 00 00 00 00
        regs = convert.float64_to_registers(1.0, WordOrder.ABCD)
        raw = b"".join(reg.to_bytes(2, "big") for reg in regs)
        assert raw == data
        regs_cdab = convert.float64_to_registers(1.0, WordOrder.CDAB)
        assert regs_cdab == (0x0000, 0x0000, 0x0000, 0x3FF0)


class TestString:
    """字符串编解码。"""

    def test_roundtrip(self) -> None:
        raw = convert.encode_string("OMNI", 8)
        assert raw == b"OMNI\x00\x00\x00\x00"
        assert convert.decode_string(raw) == "OMNI"

    def test_too_long(self) -> None:
        with pytest.raises(ValueError):
            convert.encode_string("123456789", 8)

    def test_utf8(self) -> None:
        raw = convert.encode_string("炉温", 8, encoding="utf-8")
        assert convert.decode_string(raw, encoding="utf-8") == "炉温"


class TestWordsValue:
    """字序列 ↔ 值的通用转换(数值类型)。"""

    def test_roundtrip_16(self) -> None:
        assert convert.words_to_value([0xFFFE], DataType.SHORT) == -2
        assert convert.value_to_words(-2, DataType.SHORT) == [0xFFFE]

    def test_word_count_mismatch(self) -> None:
        with pytest.raises(ValueError):
            convert.words_to_value([1], DataType.FLOAT)

    def test_unknown_type_rejected(self) -> None:
        """非 DataType 入参显式 ValueError(不抛 KeyError,不静默解码)。"""
        with pytest.raises(ValueError):
            convert.words_to_value([1], "float")  # type: ignore[arg-type]

    @pytest.mark.parametrize("data_type", [DataType.BOOL, DataType.STRING])
    def test_non_numeric_type_rejected_by_value_to_words(self, data_type: DataType) -> None:
        """编码方向需外部给定目标长度,故 BOOL/STRING 显式 ValueError(不抛 KeyError)。"""
        with pytest.raises(ValueError):
            convert.value_to_words(1, data_type)


class TestWordsToValueBool:
    """BOOL 解码:1 个字,字值非 0 即 True。"""

    @pytest.mark.parametrize(
        "word,expected", [(0, False), (1, True), (2, True), (0xFFFF, True)]
    )
    def test_nonzero_is_true(self, word: int, expected: bool) -> None:
        assert convert.words_to_value([word], DataType.BOOL) is expected

    def test_word_count_mismatch(self) -> None:
        with pytest.raises(ValueError):
            convert.words_to_value([1, 0], DataType.BOOL)
        with pytest.raises(ValueError):
            convert.words_to_value([], DataType.BOOL)

    def test_byteorder_ignored(self) -> None:
        """单字只看数值:两种字节序同解。"""
        assert convert.words_to_value([1], DataType.BOOL, ByteOrder.BIG) is True
        assert convert.words_to_value([1], DataType.BOOL, ByteOrder.LITTLE) is True


class TestWordsToValueString:
    """STRING 解码:字数由入参长度决定(不固定 1/2/4),编码与截断可配。"""

    def test_big_endian_ascii(self) -> None:
        assert convert.words_to_value([0x4F4D, 0x4E49], DataType.STRING, ByteOrder.BIG) == "OMNI"

    def test_roundtrip_with_encode_string(self) -> None:
        """与 write_string 一路(encode_string → 大端拆字)互逆。"""
        raw = convert.encode_string("OMNI", 8)
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        assert convert.words_to_value(words, DataType.STRING, ByteOrder.BIG) == "OMNI"

    def test_little_endian_default(self) -> None:
        """默认字内小端:同一批字按字节序不同解出不同文本(read_string 走大端)。"""
        assert convert.words_to_value([0x4F4D], DataType.STRING) == "MO"

    def test_nul_truncated(self) -> None:
        assert convert.words_to_value([0x4142, 0x0000], DataType.STRING, ByteOrder.BIG) == "AB"

    def test_empty_words(self) -> None:
        """0 个字得空串(不视为错误:读取长度本就由调用方给出)。"""
        assert convert.words_to_value([], DataType.STRING, ByteOrder.BIG) == ""

    def test_utf8_encoding(self) -> None:
        words = [0xE782, 0x89E6, 0xB8A9]
        assert (
            convert.words_to_value(words, DataType.STRING, ByteOrder.BIG, encoding="utf-8")
            == "炉温"
        )

    def test_reverse_words(self) -> None:
        """reverse_words 对文本同样生效(低字在前协议,如 MEWTOCOL)。"""
        words = [0x4E49, 0x4F4D]  # "NI" + "OM"
        assert (
            convert.words_to_value(words, DataType.STRING, ByteOrder.BIG, reverse_words=True)
            == "OMNI"
        )
