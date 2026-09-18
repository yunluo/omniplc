"""全局常量集中定义。

本模块是 omniplc **唯一的常量来源**:协议默认值、边界值、报文常量
全部在这里定义,业务代码禁止内联魔法数字。

约定:全部使用大写下划线命名(常量),运行期不允许修改;
Python 3.7 无 ``typing.Final``,以命名约定与 Code Review 约束只读性。
"""
from __future__ import annotations

from typing import Dict

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
MODBUS_PROTOCOL_ID: int = 0
"""MBAP 协议标识符,恒为 0。"""
MODBUS_EXCEPTION_FLAG: int = 0x80
"""异常响应功能码标志:请求功能码 | 0x80。"""
MODBUS_COIL_ON: int = 0xFF00
"""FC05 写线圈"ON"的线状态值。"""
MODBUS_COIL_OFF: int = 0x0000
"""FC05 写线圈"OFF"的线状态值。"""
MODBUS_MAX_READ_BITS: int = 2000
"""单次读位的数量上限(协议规定)。"""
MODBUS_MAX_READ_REGISTERS: int = 125
"""单次读寄存器的数量上限(协议规定)。"""
MODBUS_MAX_WRITE_BITS: int = 1968
"""单次写线圈的数量上限(协议规定)。"""
MODBUS_MAX_WRITE_REGISTERS: int = 123
"""单次写寄存器的数量上限(协议规定)。"""
MODBUS_MAX_ADU_SIZE: int = 260
"""MBAP 最大帧长 = 帧头 7 + 最大 PDU 253(UDP 整包接收缓冲)。"""
MODBUS_EXCEPTION_TEXT: Dict[int, str] = {
    0x01: "ILLEGAL FUNCTION(不支持的功能码)",
    0x02: "ILLEGAL DATA ADDRESS(地址越界)",
    0x03: "ILLEGAL DATA VALUE(数值非法)",
    0x04: "SERVER DEVICE FAILURE(设备故障)",
    0x05: "ACKNOWLEDGE(已受理,处理中)",
    0x06: "SERVER DEVICE BUSY(设备忙)",
    0x08: "MEMORY PARITY ERROR(存储区奇偶校验错)",
    0x0A: "GATEWAY PATH UNAVAILABLE(网关路径不可用)",
    0x0B: "GATEWAY TARGET DEVICE FAILED TO RESPOND(网关目标设备无响应)",
}
"""Modbus 标准异常码 → 可读描述(last_error 用)。"""

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
