"""omniplc 通用类型定义:数据类型、字序常量与公共类型别名。

数据类型名称与 pyhsl 的 ``read_*``/``write_*`` 方法后缀保持一致,
便于熟悉 pyhsl 的用户无缝切换。
"""
from __future__ import annotations

from enum import Enum
from typing import Union

PrimitiveValue = Union[bool, int, float, str]
"""读写方法接受的 Python 原生值类型。"""


class DataType(Enum):
    """PLC 数据类型(成员名即 :meth:`BaseClient.read` 的 ``data_type`` 参数值)。

    - ``SHORT``/``USHORT``:16 位有/无符号整数
    - ``INT``/``UINT``:32 位有/无符号整数
    - ``LONG``/``ULONG``:64 位有/无符号整数
    - ``FLOAT``/``DOUBLE``:32/64 位浮点数
    - ``STRING``:字符串(长度与编码由具体读写方法的参数决定)
    """

    BOOL = "bool"
    SHORT = "short"
    USHORT = "ushort"
    INT = "int"
    UINT = "uint"
    LONG = "long"
    ULONG = "ulong"
    FLOAT = "float"
    DOUBLE = "double"
    STRING = "string"

    @classmethod
    def coerce(cls, value: "Union[DataType, str]") -> "DataType":
        """把枚举成员或名称统一解析为枚举成员。

        :param value: :class:`DataType` 成员,或类型名称(如 ``"float"``)
        :return: :class:`DataType` 成员
        :raises ValueError: 名称未知时抛出
        """
        if isinstance(value, DataType):
            return value
        return cls.from_name(value)

    @classmethod
    def from_name(cls, name: str) -> "DataType":
        """按名称解析数据类型。

        :param name: 类型名称,如 ``"float"``、``"short"``,不区分大小写
        :return: 对应的 :class:`DataType` 成员
        :raises ValueError: 名称未知时抛出
        """
        try:
            return cls(name.strip().lower())
        except ValueError:
            valid = ", ".join(member.value for member in cls)
            raise ValueError(f"未知的数据类型 {name!r},支持:{valid}")

    @property
    def byte_size(self) -> int:
        """该类型的字节长度(STRING 返回 0,表示长度由调用方指定)。"""
        if self is DataType.STRING:
            return 0
        if self is DataType.BOOL:
            return 1
        if self in (DataType.SHORT, DataType.USHORT):
            return 2
        if self in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return 4
        return 8

    @property
    def register_size(self) -> int:
        """该类型占用的 16 位寄存器个数(BOOL 按 1 个寄存器/位计)。"""
        return max(1, self.byte_size // 2)


class WordOrder(Enum):
    """多寄存器值的字序(仅 Modbus 等字序可配置的协议使用)。

    以 32 位值为例,四个字节按从高位到低位记作 A、B、C、D:

    - ``ABCD``:大端(标准网络字节序,Modbus 默认)
    - ``CDAB``:字交换(寄存器顺序颠倒,现场最常见的"高字在后")
    - ``BADC``:字节交换(寄存器内两字节颠倒)
    - ``DCBA``:小端
    """

    ABCD = "ABCD"
    CDAB = "CDAB"
    BADC = "BADC"
    DCBA = "DCBA"


class ByteOrder(Enum):
    """单值内的字节序(整数 ↔ 字节转换使用,对应 ``int.from_bytes``)。"""

    BIG = "big"
    LITTLE = "little"


class SerialParity(Enum):
    """串口校验位。"""

    NONE = "N"
    EVEN = "E"
    ODD = "O"


class McFrame(Enum):
    """三菱 MC 协议帧型。

    - ``FRAME_3E``/``FRAME_4E``:QnA 兼容帧(Q/L/R/iQ-R/iQ-F 系列,以太网)
    - ``FRAME_1E``:A 兼容帧(A 系列,以太网)
    - ``FRAME_3C``/``FRAME_4C``:QnA 兼容串口帧(C24 串口模块;
      3C 为 ASCII 帧(格式 4),4C 为二进制帧(格式 5),仅 :class:`~omniplc.plc.melsec.MelsecMcSerialClient` 使用)
    """

    FRAME_3E = "3E"
    FRAME_4E = "4E"
    FRAME_1E = "1E"
    FRAME_3C = "3C"
    FRAME_4C = "4C"
