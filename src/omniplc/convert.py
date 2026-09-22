"""纯帮助函数:字节序/字序/类型转换与校验和。

本模块全部是无副作用的纯函数,不依赖任何客户端或传输对象,
既供各协议 codec 内部使用,也可直接用于寄存器原始数据的后处理,
例如把 ``read_many`` 读到的寄存器按现场字序还原为浮点数。
"""
from __future__ import annotations

import struct
from typing import Any, List, Sequence, Tuple, Union, cast

from .core.constants import BIT_INDEX_MAX, CRC16_INIT, CRC16_POLY
from .types import ByteOrder, WordOrder

BytesLike = Union[bytes, bytearray, Sequence[int]]


def crc16(data: BytesLike) -> int:
    """计算 Modbus RTU 的 CRC-16(多项式 0xA001,反射输入/输出)。

    :param data: 参与校验的字节序列(不含 CRC 本身)
    :return: 16 位无符号校验值。RTU 帧按**低字节在前**追加到报文尾部
    """
    crc = CRC16_INIT
    for byte in data:
        crc ^= byte & 0xFF
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ CRC16_POLY
            else:
                crc >>= 1
    return crc & 0xFFFF


def lrc(data: BytesLike) -> int:
    """计算 Modbus ASCII 的 LRC 校验(纵向冗余校验)。

    :param data: 参与校验的字节序列(不含 LRC 本身)
    :return: 8 位无符号校验值

    .. note:: Modbus ASCII 走线尚未实现,本函数为走线预留的公共工具。
    """
    total = sum(byte & 0xFF for byte in data) & 0xFF
    return (-total) & 0xFF


def get_bit(value: int, bit: int) -> bool:
    """取整数 ``value`` 的第 ``bit`` 位(0 = 最低位)。

    :raises ValueError: bit 超出允许范围
    """
    if not 0 <= bit <= BIT_INDEX_MAX:
        raise ValueError(f"bit 必须在 0~{BIT_INDEX_MAX} 之间,收到:{bit}")
    return (int(value) >> bit) & 0x01 == 0x01


def set_bit(value: int, bit: int, on: bool) -> int:
    """置位/复位整数 ``value`` 的第 ``bit`` 位,返回新值(不修改原值)。

    :raises ValueError: bit 超出允许范围
    """
    if not 0 <= bit <= BIT_INDEX_MAX:
        raise ValueError(f"bit 必须在 0~{BIT_INDEX_MAX} 之间,收到:{bit}")
    value = int(value)
    if on:
        return value | (1 << bit)
    return value & ~(1 << bit)


def to_signed(raw: int, bits: int) -> int:
    """无符号原始值按位宽转有符号整数(补码语义)。

    :param raw: 0 ~ 2\\*\\*bits - 1 的无符号原始值
    :param bits: 位宽(16/32/64)
    :raises ValueError: raw 超出该位宽无符号范围
    """
    raw = int(raw)
    if not 0 <= raw < (1 << bits):
        raise ValueError(f"无符号原始值超出 {bits} 位范围:{raw}")
    return raw - (1 << bits) if raw >= 1 << (bits - 1) else raw


def words_to_bytes(
    words: Sequence[int], byteorder: Union[ByteOrder, str] = ByteOrder.LITTLE
) -> bytes:
    """把 0~65535 原始字序列按指定字节序拼为字节串(每字 2 字节,顺序拼接)。

    :param words: 原始字序列(0~65535),按传输顺序给出
    :param byteorder: 字内字节序,默认小端
    """
    order = _byteorder(byteorder)
    return b"".join((int(word) & 0xFFFF).to_bytes(2, order) for word in words)


def bytes_to_words(
    data: BytesLike, byteorder: Union[ByteOrder, str] = ByteOrder.LITTLE
) -> List[int]:
    """把字节串按指定字节序拆为 0~65535 原始字列表。

    :param data: 原始字节(长度必须为偶数)
    :param byteorder: 字内字节序,默认小端
    :raises ValueError: 长度为奇数
    """
    order = _byteorder(byteorder)
    if len(data) % 2 != 0:
        raise ValueError("字节串长度必须为偶数,收到:{}".format(len(data)))
    return [int.from_bytes(data[i:i + 2], order) for i in range(0, len(data), 2)]


def bytes_to_short(data: bytes, byteorder: Union[ByteOrder, str] = ByteOrder.BIG) -> int:
    """按指定字节序把 2 字节解码为 16 位有符号整数。

    :param data: 原始字节
    :param byteorder: 字节序,推荐 :class:`omniplc.types.ByteOrder` 枚举
    """
    return int.from_bytes(data, _byteorder(byteorder), signed=True)


def bytes_to_ushort(data: bytes, byteorder: Union[ByteOrder, str] = ByteOrder.BIG) -> int:
    """按指定字节序把 2 字节解码为 16 位无符号整数。"""
    return int.from_bytes(data, _byteorder(byteorder), signed=False)


def short_to_bytes(value: int, byteorder: Union[ByteOrder, str] = ByteOrder.BIG) -> bytes:
    """把 16 位有符号整数编码为 2 字节。

    :raises ValueError: 超出范围 -32768~32767
    """
    number = int(value)
    if not -32768 <= number <= 32767:
        raise ValueError(f"short 超出范围 -32768~32767:{number}")
    return number.to_bytes(2, _byteorder(byteorder), signed=True)


def ushort_to_bytes(value: int, byteorder: Union[ByteOrder, str] = ByteOrder.BIG) -> bytes:
    """把 16 位无符号整数编码为 2 字节。

    :raises ValueError: 超出范围 0~65535
    """
    number = int(value)
    if not 0 <= number <= 65535:
        raise ValueError(f"ushort 超出范围 0~65535:{number}")
    return number.to_bytes(2, _byteorder(byteorder), signed=False)


def registers_to_int32(registers: Sequence[int], word_order: WordOrder = WordOrder.ABCD) -> int:
    """把 2 个寄存器按指定字序解码为 32 位有符号整数。"""
    return int.from_bytes(registers_to_canonical(registers, word_order), "big", signed=True)


def registers_to_uint32(registers: Sequence[int], word_order: WordOrder = WordOrder.ABCD) -> int:
    """把 2 个寄存器按指定字序解码为 32 位无符号整数。"""
    return int.from_bytes(registers_to_canonical(registers, word_order), "big", signed=False)


def int32_to_registers(value: int, word_order: WordOrder = WordOrder.ABCD) -> Tuple[int, int]:
    """把 32 位整数按指定字序编码为 2 个寄存器。"""
    return cast(
        Tuple[int, int], _canonical_to_registers(struct.pack(">i", int(value)), word_order)
    )


def uint32_to_registers(value: int, word_order: WordOrder = WordOrder.ABCD) -> Tuple[int, int]:
    """把 32 位无符号整数按指定字序编码为 2 个寄存器。"""
    return cast(
        Tuple[int, int], _canonical_to_registers(struct.pack(">I", int(value)), word_order)
    )


def registers_to_float32(registers: Sequence[int], word_order: WordOrder = WordOrder.ABCD) -> float:
    """把 2 个寄存器按指定字序解码为 32 位浮点数(float32)。"""
    return struct.unpack(">f", registers_to_canonical(registers, word_order))[0]


def float32_to_registers(value: float, word_order: WordOrder = WordOrder.ABCD) -> Tuple[int, int]:
    """把 32 位浮点数按指定字序编码为 2 个寄存器。"""
    return cast(
        Tuple[int, int], _canonical_to_registers(struct.pack(">f", float(value)), word_order)
    )


def registers_to_float64(registers: Sequence[int], word_order: WordOrder = WordOrder.ABCD) -> float:
    """把 4 个寄存器按指定字序解码为 64 位浮点数(float64)。

    字序按语义映射到 8 字节排列:ABCD→ABCDEFGH、CDAB→GHEFCDAB、
    BADC→BADCFEHG、DCBA→HGFEDCBA。
    """
    return struct.unpack(">d", registers_to_canonical(registers, word_order))[0]


def float64_to_registers(
    value: float, word_order: WordOrder = WordOrder.ABCD
) -> Tuple[int, int, int, int]:
    """把 64 位浮点数按指定字序编码为 4 个寄存器(字序映射同上)。"""
    return cast(
        Tuple[int, int, int, int],
        _canonical_to_registers(struct.pack(">d", float(value)), word_order),
    )


def int64_to_registers(
    value: int, word_order: WordOrder = WordOrder.ABCD
) -> Tuple[int, int, int, int]:
    """把 64 位有符号整数按指定字序编码为 4 个寄存器。

    :raises ValueError: 超出 64 位有符号范围(struct 编码失败)
    """
    return cast(
        Tuple[int, int, int, int],
        _canonical_to_registers(struct.pack(">q", int(value)), word_order),
    )


def uint64_to_registers(
    value: int, word_order: WordOrder = WordOrder.ABCD
) -> Tuple[int, int, int, int]:
    """把 64 位无符号整数按指定字序编码为 4 个寄存器。

    :raises ValueError: 超出 64 位无符号范围(struct 编码失败)
    """
    return cast(
        Tuple[int, int, int, int],
        _canonical_to_registers(struct.pack(">Q", int(value)), word_order),
    )


def decode_string(data: bytes, encoding: str = "ascii") -> str:
    """把寄存器字节解码为字符串,自动截断 ``\\x00`` 填充。

    :param data: 原始字节
    :param encoding: 字符编码,默认 ASCII
    """
    return data.split(b"\x00", 1)[0].decode(encoding, errors="replace")


def encode_string(value: str, length: int, encoding: str = "ascii") -> bytes:
    """把字符串按指定编码编码为定长字节串,不足补 ``\\x00``。

    :param value: 待编码字符串
    :param length: 目标字节长度
    :param encoding: 字符编码,默认 ASCII
    :raises ValueError: 编码后超出目标长度
    """
    raw = value.encode(encoding)
    if len(raw) > length:
        raise ValueError("字符串编码后 {} 字节,超出目标长度 {}".format(len(raw), length))
    return raw.ljust(length, b"\x00")


def _byteorder(byteorder: Union[ByteOrder, str]) -> Any:
    """校验并透传字节序参数(内部函数)。

    ``int.from_bytes`` 的类型签名为 ``Literal["little", "big"]``,3.7
    无 ``typing.Literal``,这里统一校验后用 ``cast`` 透传。
    """
    if isinstance(byteorder, ByteOrder):
        return cast(Any, byteorder.value)
    if byteorder in ("big", "little"):
        return cast(Any, byteorder)
    raise ValueError(f"byteorder 必须是 ByteOrder.BIG/LITTLE 或 big/little,收到:{byteorder!r}")


def registers_to_canonical(
    registers: Sequence[int], word_order: WordOrder = WordOrder.ABCD
) -> bytes:
    """把寄存器序列按字序还原为"大端规范序"字节串。

    输入寄存器按设备实际顺序给出(每个寄存器内部恒为大端),
    输出为该数值标准大端表示,可直接交给 struct 解码。
    """
    raw = b"".join((int(reg) & 0xFFFF).to_bytes(2, "big") for reg in registers)
    return _reorder_bytes(raw, word_order)


def _canonical_to_registers(data: bytes, word_order: WordOrder) -> Tuple[int, ...]:
    """把大端规范字节串按字序打散为寄存器元组(内部函数)。"""
    raw = _reorder_bytes(data, word_order)
    return tuple(int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2))


def _reorder_bytes(data: bytes, word_order: WordOrder) -> bytes:
    """在"大端规范序"与"设备实际字节排列"之间互相转换(内部函数)。

    四种字序变换都是对合变换(做两次回到自身),因此编解码共用。
    """
    if word_order is WordOrder.ABCD:
        return data
    if word_order is WordOrder.DCBA:
        return data[::-1]
    if word_order is WordOrder.CDAB:
        # 2 字节字整字交换,字序反转:ABCD → CDAB,ABCDEFGH → GHEFCDAB
        return b"".join(data[i:i + 2] for i in range(len(data) - 2, -1, -2))
    # BADC:每个 2 字节字内部字节交换:ABCD → BADC,ABCDEFGH → BADCFEHG
    out = bytearray(len(data))
    for i in range(0, len(data) - 1, 2):
        out[i] = data[i + 1]
        out[i + 1] = data[i]
    return bytes(out)
