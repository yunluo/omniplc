"""omniplc 通用类型定义:数据类型、字序常量与公共类型别名。

数据类型名称与本库 ``read_*``/``write_*`` 方法后缀保持一致
(行业常见约定),便于调用方按统一命名切换数据类型。
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Union

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
    def coerce(cls, value: Union[DataType, str]) -> DataType:
        """把枚举成员或名称统一解析为枚举成员。

        :param value: :class:`DataType` 成员,或类型名称(如 ``"float"``)
        :return: :class:`DataType` 成员
        :raises ValueError: 入参既非 :class:`DataType` 也非名称字符串(如
            ``client.read("hr0", 3)``),或名称未知时抛出
        """
        if isinstance(value, DataType):
            return value
        if not isinstance(value, str):
            # review-1046 C6:本方法声明只抛 ValueError,旧实现直接把它交给
            # ``name.strip()`` → AttributeError('int' object has no attribute
            # 'strip'),且该异常不在 ``_execute`` 的转换集合里,会以"编程错误"
            # 姿态逃逸(契约错)。显式转成调用方错误。
            from .i18n import _

            raise ValueError(
                _("数据类型必须是 DataType 或名称字符串,收到:{!r}").format(value)
            )
        return cls.from_name(value)

    @classmethod
    def from_name(cls, name: str) -> DataType:
        """按名称解析数据类型。

        :param name: 类型名称,如 ``"float"``、``"short"``,不区分大小写
        :return: 对应的 :class:`DataType` 成员
        :raises ValueError: 名称未知时抛出
        """
        from .i18n import _

        try:
            return cls(name.strip().lower())
        except ValueError:
            valid = ", ".join(member.value for member in cls)
            raise ValueError(
                _("未知的数据类型 {0!r},支持:{1}").format(name, valid)
            ) from None

    @property
    def byte_size(self) -> int:
        """该类型的字节长度(STRING 返回 0,表示长度由调用方指定)。"""
        return _BYTE_SIZES[self]

    @property
    def register_size(self) -> int:
        """该类型占用的 16 位寄存器个数(BOOL 返回 1 占位;实际访问宽度由
        协议位寻址决定,如 Modbus 线圈 1 位、批量打包按 ⌈数量/8⌉ 字节)。"""
        return max(1, self.byte_size // 2)


# 各数据类型的字节长度:尺寸口径的**唯一来源**(``register_size``、
# ``convert._TYPE_BYTE_SIZES`` 均由此派生)。原先 ``byte_size`` 是 4 段
# if 链、``convert`` 另有一张同内容字面量表,两处各自维护容易漂移。
# 新增枚举成员而忘补此表时按 KeyError 立即暴露,不做 ``.get(..., 8)`` 兜底。
_BYTE_SIZES: Dict[DataType, int] = {
    DataType.BOOL: 1,
    DataType.SHORT: 2,
    DataType.USHORT: 2,
    DataType.INT: 4,
    DataType.UINT: 4,
    DataType.LONG: 8,
    DataType.ULONG: 8,
    DataType.FLOAT: 4,
    DataType.DOUBLE: 8,
    DataType.STRING: 0,
}


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
    - ``FRAME_1C``:A 兼容串口帧(C24 串口模块,ASCII 格式 4,
      命令 BR/WR/BW/WW,仅 :class:`~omniplc.plc.melsec.MelsecMcSerialClient` 使用)
    """

    FRAME_3E = "3E"
    FRAME_4E = "4E"
    FRAME_1E = "1E"
    FRAME_3C = "3C"
    FRAME_4C = "4C"
    FRAME_1C = "1C"


class S7Model(Enum):
    """西门子 S7 CPU 型号(客户端构造参数,对标三菱 MC 的 :class:`McFrame` 形态)。

    型号驱动连接预设(本端 TSAP / 连接类型 / 缺省 rack·slot / TPDU 尺寸),
    预设表见 :data:`~omniplc.core.constants.S7_MODEL_PRESETS`;rack/slot 可在
    构造时显式覆写。

    - ``S7_300``/``S7_400``:经典机架型(CPU 惯例槽位 2)
    - ``S7_1200``/``S7_1500``:紧凑型(槽位 1;须开启 PUT/GET 授权、DB 非优化块)
    - ``S7_200_SMART``:连接走 S7 基本资源类型(远端 TSAP 0x0300 口径),
      V 区经 ``V`` 地址记号访问(=DB1)
    - ``S7_200``:经典 S7-200 **无内置以太网**,仅适用经 CP243-1 以太网模块
      的 S7comm 接入(两侧 TSAP = "MW" 记号);无模块请走串口 PPI(未实现,
      见 README 的 Modbus 从站库替代路径)
    """

    S7_200 = "200"
    S7_200_SMART = "200Smart"
    S7_300 = "300"
    S7_400 = "400"
    S7_1200 = "1200"
    S7_1500 = "1500"
