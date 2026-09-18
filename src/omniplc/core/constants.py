"""全局常量集中定义。

本模块是 omniplc **唯一的常量来源**:协议默认值、边界值、报文常量
全部在这里定义,业务代码禁止内联魔法数字。

约定:全部使用大写下划线命名(常量),运行期不允许修改;
Python 3.7 无 ``typing.Final``,以命名约定与 Code Review 约束只读性。
"""
from __future__ import annotations

from ..types import SerialParity

# ---------------------------------------------------------------- 默认超时(秒)
DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_RECEIVE_TIMEOUT: float = 3.0

# ---------------------------------------------------------------- 默认端口
MODBUS_DEFAULT_PORT: int = 502
MC_DEFAULT_PORT: int = 2000
FINS_DEFAULT_PORT: int = 9600

# ---------------------------------------------------------------- 串口默认值
SERIAL_DEFAULT_BAUD_RATE: int = 9600
SERIAL_DEFAULT_DATA_BITS: int = 8
SERIAL_DEFAULT_STOP_BITS: float = 1.0
SERIAL_DEFAULT_PARITY: SerialParity = SerialParity.NONE

# ---------------------------------------------------------------- Modbus
MODBUS_DEFAULT_STATION: int = 1
MODBUS_STATION_MIN: int = 0
MODBUS_STATION_MAX: int = 247
MODBUS_REGISTER_BIT_MAX: int = 15
"""寄存器位访问的位号上限(hr0.15)。"""
MBAP_HEADER_SIZE: int = 7
"""MBAP 帧头长度:事务号(2) + 协议号(2) + 长度(2) + 站号(1)。"""
CRC16_INIT: int = 0xFFFF
CRC16_POLY: int = 0xA001
"""Modbus RTU CRC-16 的初始值与反射多项式。"""

# ---------------------------------------------------------------- 三菱 MC
MC_DEFAULT_NETWORK_NUMBER: int = 0
MC_DEFAULT_PC_NUMBER: int = 0
MC_SUBHEADER_3E_READ: int = 0x5040
"""占位值,实现 3E/4E 编解码时按 MELSEC 手册校正。"""
MC_SUBHEADER_3E_WRITE: int = 0x5041
"""占位值,实现 3E/4E 编解码时按 MELSEC 手册校正。"""

# ---------------------------------------------------------------- 欧姆龙 FINS
FINS_DEFAULT_DESTINATION_NETWORK: int = 0
FINS_DEFAULT_DESTINATION_NODE: int = 0
FINS_DEFAULT_DESTINATION_UNIT: int = 0

# ---------------------------------------------------------------- 通用
BIT_INDEX_MAX: int = 63
"""位操作工具允许的最大位号。"""
READ_STRING_DEFAULT_LENGTH: int = 32
DEFAULT_STRING_ENCODING: str = "ascii"
