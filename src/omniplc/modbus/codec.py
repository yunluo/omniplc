"""Modbus 报文编解码(纯函数)。

本模块所有函数都是 bytes ↔ 结构的纯函数,不接触 socket,可完全
用黄金报文样本(:doc:`tests/golden/README`)做单元测试。

帧布局依据《Modbus 通信协议规范》(MODBUS over Serial Line /
MODBUS TCP/IP Application Protocol,2026-09 复审修正;主参考
[Modbus 应用协议 V1.1b3](https://www.modbus.cn/modbus-specifications)):

- PDU:功能码(1) + 数据;读请求 = 功能码 + 起始地址(2,大端) + 数量(2,大端)
- MBAP:事务号(2,大端) + 协议号(2,恒 0) + 长度(2,= 站号+PDU 字节数) + 站号(1)
- RTU:站号(1) + PDU + CRC16(2,低字节在前)
- 异常响应:功能码 | 0x80 + 异常码(1),由 :func:`check_response_exception` 识别
"""
from __future__ import annotations

import struct
from enum import IntEnum
from typing import Dict, List, NamedTuple, Sequence, Tuple

from ..convert import crc16
from ..core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_ADDRESS_MAX,
    MODBUS_COMMAND_DIAGNOSTICS,
    MODBUS_COMMAND_GET_COMM_EVENT_COUNTER,
    MODBUS_COMMAND_GET_COMM_EVENT_LOG,
    MODBUS_COMMAND_MASK_WRITE,
    MODBUS_COMMAND_READ_DEVICE_ID,
    MODBUS_COMMAND_READ_FILE_RECORD,
    MODBUS_COMMAND_READ_WRITE_MULTIPLE,
    MODBUS_COMMAND_WRITE_FILE_RECORD,
    MODBUS_COIL_OFF,
    MODBUS_COIL_ON,
    MODBUS_DEVICE_ID_CODE_BASIC,
    MODBUS_DEVICE_ID_CODE_INDIVIDUAL,
    MODBUS_DEVICE_ID_FIXED_HEAD_SIZE,
    MODBUS_DEVICE_ID_PDU_HEAD_SIZE,
    MODBUS_DIAGNOSTICS_PDU_SIZE,
    MODBUS_EVENT_COUNTER_PDU_SIZE,
    MODBUS_EXCEPTION_FLAG,
    MODBUS_EXCEPTION_TEXT,
    MODBUS_FILE_REFERENCE_TYPE,
    MODBUS_MASK_WRITE_PDU_SIZE,
    MODBUS_MAX_ADU_SIZE,
    MODBUS_MAX_FILE_RECORDS,
    MODBUS_MAX_READ_BITS,
    MODBUS_MAX_READ_FILE_BYTES,
    MODBUS_MAX_READ_REGISTERS,
    MODBUS_MAX_RW_WRITE_REGISTERS,
    MODBUS_MAX_WRITE_BITS,
    MODBUS_MAX_WRITE_FILE_BYTES,
    MODBUS_MAX_WRITE_REGISTERS,
    MODBUS_MBAP_LENGTH_MAX,
    MODBUS_MEI_TYPE_DEVICE_ID,
    MODBUS_PROTOCOL_ID,
)
from ..core.debug import format_hex
from ..core.errors import DeviceError, ProtocolFrameError


class ModbusFunction(IntEnum):
    """Modbus 功能码(枚举化,便于 IDE 补全与类型检查)。

    值与 :mod:`omniplc.core.constants` 的常量同源(单一来源,避免漂移)。
    """

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    MASK_WRITE_REGISTER = MODBUS_COMMAND_MASK_WRITE
    READ_WRITE_MULTIPLE_REGISTERS = MODBUS_COMMAND_READ_WRITE_MULTIPLE
    READ_FILE_RECORD = MODBUS_COMMAND_READ_FILE_RECORD
    WRITE_FILE_RECORD = MODBUS_COMMAND_WRITE_FILE_RECORD
    DIAGNOSTICS = MODBUS_COMMAND_DIAGNOSTICS
    GET_COMM_EVENT_COUNTER = MODBUS_COMMAND_GET_COMM_EVENT_COUNTER
    GET_COMM_EVENT_LOG = MODBUS_COMMAND_GET_COMM_EVENT_LOG
    READ_DEVICE_IDENTIFICATION = MODBUS_COMMAND_READ_DEVICE_ID


_READ_BIT_FUNCTIONS = (ModbusFunction.READ_COILS, ModbusFunction.READ_DISCRETE_INPUTS)
_READ_REGISTER_FUNCTIONS = (
    ModbusFunction.READ_HOLDING_REGISTERS,
    ModbusFunction.READ_INPUT_REGISTERS,
)


# ----------------------------------------------------------------------
# 请求 PDU 构造(参数非法抛 ValueError,符合公共 API 约定)
# ----------------------------------------------------------------------

def build_read_pdu(function_code: int, offset: int, count: int) -> bytes:
    """构造读请求 PDU(FC 01/02/03/04)。

    :param function_code: 功能码(枚举 :class:`ModbusFunction` 或整数)
    :param offset: 0 基起始地址
    :param count: 读取数量(位或寄存器)
    :return: PDU 字节(功能码 + 起始地址 + 数量,均大端)
    :raises ValueError: 功能码/地址/数量非法
    """
    if function_code in _READ_BIT_FUNCTIONS:
        limit = MODBUS_MAX_READ_BITS
    elif function_code in _READ_REGISTER_FUNCTIONS:
        limit = MODBUS_MAX_READ_REGISTERS
    else:
        raise ValueError(f"读功能码必须是 1/2/3/4,收到:{function_code}")
    _check_offset(offset)
    _check_span(offset, count)
    if not 1 <= count <= limit:
        raise ValueError(f"读数量超出范围 1~{limit}:{count}")
    return struct.pack(">BHH", function_code, offset, count)


def build_write_single_pdu(function_code: int, offset: int, value: int) -> bytes:
    """构造单点写请求 PDU(FC 05 写线圈 / FC 06 写单寄存器)。

    :param function_code: 5 或 6
    :param offset: 0 基地址
    :param value: 线圈(真值置位/假值复位)或寄存器原始值(0~65535)
    :raises ValueError: 功能码/地址/数值非法
    """
    _check_offset(offset)
    if function_code == ModbusFunction.WRITE_SINGLE_COIL:
        payload = MODBUS_COIL_ON if value else MODBUS_COIL_OFF
    elif function_code == ModbusFunction.WRITE_SINGLE_REGISTER:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"寄存器写入值超出范围 0~65535:{value}")
        payload = value
    else:
        raise ValueError(f"单点写功能码必须是 5 或 6,收到:{function_code}")
    return struct.pack(">BHH", function_code, offset, payload)


def build_write_multi_pdu(function_code: int, offset: int, values: List[int]) -> bytes:
    """构造批量写请求 PDU(FC 15 写多线圈 / FC 16 写多寄存器)。

    :param function_code: 15 或 16
    :param offset: 0 基起始地址
    :param values: 寄存器原始值序列;线圈传 0/1 序列,由本函数按 LSB 在前打包
    :raises ValueError: 功能码/地址/数量/数值非法
    """
    _check_offset(offset)
    if function_code == ModbusFunction.WRITE_MULTIPLE_COILS:
        if not 1 <= len(values) <= MODBUS_MAX_WRITE_BITS:
            raise ValueError(
                "写线圈数量超出范围 1~{}:{}".format(MODBUS_MAX_WRITE_BITS, len(values))
            )
        _check_span(offset, len(values))
        packed = bytearray((len(values) + 7) // 8)
        for index, flag in enumerate(values):
            if flag:
                packed[index // 8] |= 1 << (index % 8)
        return struct.pack(">BHHB", function_code, offset, len(values), len(packed)) + bytes(packed)
    if function_code == ModbusFunction.WRITE_MULTIPLE_REGISTERS:
        if not 1 <= len(values) <= MODBUS_MAX_WRITE_REGISTERS:
            raise ValueError(
                "写寄存器数量超出范围 1~{}:{}".format(MODBUS_MAX_WRITE_REGISTERS, len(values))
            )
        _check_span(offset, len(values))
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"寄存器写入值超出范围 0~65535:{value}")
        return struct.pack(">BHHB", function_code, offset, len(values), len(values) * 2) + struct.pack(
            ">{:d}H".format(len(values)), *values
        )
    raise ValueError(f"批量写功能码必须是 15 或 16,收到:{function_code}")


# ----------------------------------------------------------------------
# 响应 PDU 解析(坏帧抛 ProtocolFrameError,PLC 异常码抛 DeviceError)
# ----------------------------------------------------------------------

def check_response_exception(pdu: bytes, request_function_code: int) -> None:
    """识别异常响应:PLC 返回异常码时抛 :class:`DeviceError`。

    DeviceError 表示"链路正常但 PLC 拒绝了操作",由基类记录到
    ``last_error`` 即可,**不断线、不重试**。

    所有坏帧类失败的异常文本都带**收到的原始数据**十六进制转储
    (:func:`omniplc.core.debug.format_hex`),便于现场与抓包比对定位
    是线路噪声、站号错配还是对端语义不符。

    异常响应的功能码须为请求功能码 | 0x80;与请求不符属**错配/迟到帧**
    (如 RTU 总线上同站号前一条请求的异常回包晚到),按坏帧处理
    (断线重连 + 重试),不得当作本次请求的设备错误误落 ``last_error_code``。

    :param pdu: 响应 PDU
    :param request_function_code: 请求功能码
    :raises DeviceError: PLC 返回异常码(功能码与请求一致)
    :raises ProtocolFrameError: 空帧、异常响应缺少异常码、或功能码与请求不符
    """
    if not pdu:
        raise ProtocolFrameError("响应 PDU 为空(未收到任何字节)")
    if pdu[0] & MODBUS_EXCEPTION_FLAG:
        if len(pdu) < 2:
            raise ProtocolFrameError(
                "异常响应缺少异常码(收到的原始数据:{})".format(format_hex(pdu))
            )
        if pdu[0] ^ MODBUS_EXCEPTION_FLAG != request_function_code:
            raise ProtocolFrameError(
                "异常响应功能码不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})".format(
                    request_function_code, pdu[0], format_hex(pdu)
                )
            )
        code = pdu[1]
        text = MODBUS_EXCEPTION_TEXT.get(code, "未知异常码(厂商自定义/保留码)")
        raise DeviceError(
            "Modbus 异常码 0x{:02X}({})(请求功能码 0x{:02X})".format(
                code, text, request_function_code
            ),
            code,
        )
    if pdu[0] != request_function_code:
        raise ProtocolFrameError(
            "响应功能码不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})".format(
                request_function_code, pdu[0], format_hex(pdu)
            )
        )


def parse_read_response(pdu: bytes, function_code: int, count: int) -> List[int]:
    """解析读响应 PDU(FC 01/02/03/04)。

    :param pdu: 响应 PDU
    :param function_code: 请求功能码(用于异常识别与回包校验)
    :param count: 请求的数量
    :return: 逐位/逐寄存器的原始值列表(位返回 0/1)
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 帧长度/字节计数不符
    """
    check_response_exception(pdu, function_code)
    if len(pdu) < 2:
        raise ProtocolFrameError(
            "读响应 PDU 长度不足(收到的原始数据:{})".format(format_hex(pdu))
        )
    byte_count = pdu[1]
    expected = (count + 7) // 8 if function_code in _READ_BIT_FUNCTIONS else count * 2
    if byte_count != expected or len(pdu) != expected + 2:
        raise ProtocolFrameError(
            "读响应长度不符:字节计数域 {},期望 {},实际 PDU {} 字节(收到的原始数据:{})".format(
                byte_count, expected, len(pdu), format_hex(pdu)
            )
        )
    if function_code in _READ_BIT_FUNCTIONS:
        return [1 if pdu[2 + index // 8] & (1 << (index % 8)) else 0 for index in range(count)]
    return list(struct.unpack(f">{count:d}H", pdu[2:2 + expected]))


def parse_write_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验写响应(FC 05/06/15/16):正常应答为请求 PDU 前 5 字节的回显。

    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 回显与请求不符
    """
    check_response_exception(pdu, request_pdu[0])
    if len(pdu) != 5 or pdu != request_pdu[:5]:
        raise ProtocolFrameError(
            "写响应回显不符:期望 {},收到 {}".format(
                format_hex(request_pdu[:5]), format_hex(pdu)
            )
        )


def build_mask_write_pdu(offset: int, and_mask: int, or_mask: int) -> bytes:
    """构造掩码写请求 PDU(FC 22)。

    设备侧原子执行 ``新值 = (当前值 AND and_mask) OR (or_mask AND NOT and_mask)``。

    :param offset: 0 基保持寄存器地址
    :param and_mask: AND 掩码(0~65535;置 0 的位被清零)
    :param or_mask: OR 掩码(0~65535;仅在 and_mask 为 1 的位上可置 1)
    :return: PDU 字节(功能码 + 地址 + AND 掩码 + OR 掩码,均大端)
    :raises ValueError: 地址/掩码非法
    """
    _check_offset(offset)
    if not 0 <= and_mask <= 0xFFFF:
        raise ValueError(f"and_mask 超出范围 0~65535:{and_mask}")
    if not 0 <= or_mask <= 0xFFFF:
        raise ValueError(f"or_mask 超出范围 0~65535:{or_mask}")
    return struct.pack(">BHHH", MODBUS_COMMAND_MASK_WRITE, offset, and_mask, or_mask)


def parse_mask_write_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验掩码写响应(FC 22):正常应答为 7 字节请求回显。

    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 回显与请求不符
    """
    check_response_exception(pdu, request_pdu[0])
    if len(pdu) != MODBUS_MASK_WRITE_PDU_SIZE or pdu != request_pdu:
        raise ProtocolFrameError(
            "掩码写响应回显不符:期望 {},收到 {}".format(
                format_hex(request_pdu), format_hex(pdu)
            )
        )

# ----------------------------------------------------------------------
# FC 23 读写多寄存器(单事务"先写后读")
# ----------------------------------------------------------------------

def build_read_write_registers_pdu(
    read_offset: int,
    read_count: int,
    write_offset: int,
    values: List[int],
) -> bytes:
    """构造读写多寄存器请求 PDU(FC 23)。

    规范 §6.17:一个事务内先执行写、再执行读,仅适用于保持寄存器。
    PDU = 功能码(1) + 读起始地址(2) + 读数量(2) + 写起始地址(2)
    + 写数量(2) + 写字节数(1) + 写数据(2N,大端)。

    :param read_offset: 读起始地址(0 基)
    :param read_count: 读数量(1~125)
    :param write_offset: 写起始地址(0 基)
    :param values: 写入的寄存器原始值序列(1~121 个,每个 0~65535)
    :raises ValueError: 地址/数量/数值非法
    """
    _check_offset(read_offset)
    _check_offset(write_offset)
    if not 1 <= read_count <= MODBUS_MAX_READ_REGISTERS:
        raise ValueError(
            "FC23 读数量超出范围 1~{}:{}".format(MODBUS_MAX_READ_REGISTERS, read_count)
        )
    if not 1 <= len(values) <= MODBUS_MAX_RW_WRITE_REGISTERS:
        raise ValueError(
            "FC23 写数量超出范围 1~{}:{}".format(MODBUS_MAX_RW_WRITE_REGISTERS, len(values))
        )
    _check_span(read_offset, read_count)
    _check_span(write_offset, len(values))
    for value in values:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"寄存器写入值超出范围 0~65535:{value}")
    head = struct.pack(
        ">BHHHHB",
        ModbusFunction.READ_WRITE_MULTIPLE_REGISTERS,
        read_offset,
        read_count,
        write_offset,
        len(values),
        len(values) * 2,
    )
    return head + struct.pack(">{:d}H".format(len(values)), *values)


def parse_read_write_registers_response(pdu: bytes, read_count: int) -> List[int]:
    """解析读写多寄存器响应 PDU(FC 23),返回读到的寄存器原始值。

    响应 = 功能码(1) + 字节计数(1,= 2 × 读数量) + 读数据(2N,大端)。

    :param pdu: 响应 PDU
    :param read_count: 请求的读数量(用于校验字节计数)
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 帧长度/字节计数不符
    """
    check_response_exception(pdu, ModbusFunction.READ_WRITE_MULTIPLE_REGISTERS)
    if len(pdu) < 2:
        raise ProtocolFrameError(
            "FC23 响应 PDU 长度不足(收到的原始数据:{})".format(format_hex(pdu))
        )
    byte_count = pdu[1]
    expected = read_count * 2
    if byte_count != expected or len(pdu) != expected + 2:
        raise ProtocolFrameError(
            "FC23 响应长度不符:字节计数域 {},期望 {},实际 PDU {} 字节(收到的原始数据:{})".format(
                byte_count, expected, len(pdu), format_hex(pdu)
            )
        )
    return list(struct.unpack(f">{read_count:d}H", pdu[2:2 + expected]))


# ----------------------------------------------------------------------
# FC 43 / 14 读设备标识(MEI 隧道)
# ----------------------------------------------------------------------

class DeviceIdentification(NamedTuple):
    """设备标识响应(FC 43/14,不可变值对象)。

    :ivar conformity_level: 符合级别(0x01/0x02/0x03 = 仅流式访问;
        0x81/0x82/0x83 = 流式 + 个体访问)
    :ivar more_follows: 是否还有后续对象(需再发一次请求,对象号用
        :attr:`next_object_id`)
    :ivar next_object_id: 下一批对象的起始对象号(``more_follows`` 为
        假时无意义)
    :ivar objects: ``[(对象号, 对象原始字节)]``,按响应顺序
    """

    conformity_level: int
    more_follows: bool
    next_object_id: int
    objects: List[Tuple[int, bytes]]


def build_device_id_pdu(read_device_id_code: int, object_id: int) -> bytes:
    """构造读设备标识请求 PDU(FC 43 / MEI 0x0E)。

    规范 §6.21:PDU = 功能码(1) + MEI 类型(1) + 读取码(1) + 对象号(1)。

    :param read_device_id_code: 访问类型 1~4(1 基本/2 常规/3 扩展为
        流式访问,4 为单个对象的个体访问)
    :param object_id: 起始对象号(0~255;首次流式访问传 0)
    :raises ValueError: 读取码/对象号非法
    """
    if (
        not MODBUS_DEVICE_ID_CODE_BASIC
        <= read_device_id_code
        <= MODBUS_DEVICE_ID_CODE_INDIVIDUAL
    ):
        raise ValueError(
            f"读设备标识访问码必须是 1~4,收到:{read_device_id_code}"
        )
    if not 0 <= object_id <= 0xFF:
        raise ValueError(f"设备标识对象号超出范围 0~255:{object_id}")
    return bytes(
        [
            ModbusFunction.READ_DEVICE_IDENTIFICATION,
            MODBUS_MEI_TYPE_DEVICE_ID,
            read_device_id_code,
            object_id,
        ]
    )


def parse_device_id_response(pdu: bytes) -> DeviceIdentification:
    """解析读设备标识响应 PDU(FC 43 / MEI 0x0E)。

    响应 = 功能码(1) + MEI(1) + 读取码(1) + 符合级别(1) +
    MoreFollows(1) + 下一对象号(1) + 对象数(1)
    + 对象数 × [对象号(1) + 长度(1) + 值(长度)]。

    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: MEI 回显不符、响应截断或对象长度自洽校验失败
    """
    check_response_exception(pdu, ModbusFunction.READ_DEVICE_IDENTIFICATION)
    if len(pdu) < MODBUS_DEVICE_ID_PDU_HEAD_SIZE:
        raise ProtocolFrameError(
            "设备标识响应不完整:至少 {} 字节,实际 {}(收到的原始数据:{})".format(
                MODBUS_DEVICE_ID_PDU_HEAD_SIZE, len(pdu), format_hex(pdu)
            )
        )
    if pdu[1] != MODBUS_MEI_TYPE_DEVICE_ID:
        raise ProtocolFrameError(
            "设备标识响应 MEI 类型不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})".format(
                MODBUS_MEI_TYPE_DEVICE_ID, pdu[1], format_hex(pdu)
            )
        )
    conformity_level = pdu[3]
    more_follows = pdu[4] == 0xFF
    next_object_id = pdu[5]
    object_count = pdu[6]
    objects: List[Tuple[int, bytes]] = []
    cursor = MODBUS_DEVICE_ID_PDU_HEAD_SIZE
    for index in range(object_count):
        if cursor + 2 > len(pdu):
            raise ProtocolFrameError(
                "设备标识响应第 {} 个对象头被截断(期望 2 字节,剩余 {} 字节)"
                "(收到的原始数据:{})".format(
                    index + 1, len(pdu) - cursor, format_hex(pdu)
                )
            )
        object_id = pdu[cursor]
        length = pdu[cursor + 1]
        start = cursor + 2
        if start + length > len(pdu):
            raise ProtocolFrameError(
                "设备标识响应第 {} 个对象(号 0x{:02X})值被截断:声明 {} 字节,"
                "实际剩余 {}(收到的原始数据:{})".format(
                    index + 1, object_id, length, len(pdu) - start, format_hex(pdu)
                )
            )
        objects.append((object_id, pdu[start:start + length]))
        cursor = start + length
    if cursor != len(pdu):
        raise ProtocolFrameError(
            "设备标识响应长度不符:解析 {} 字节,实际 PDU {} 字节(收到的原始数据:{})".format(
                cursor, len(pdu), format_hex(pdu)
            )
        )
    return DeviceIdentification(
        conformity_level=conformity_level,
        more_follows=more_follows,
        next_object_id=next_object_id,
        objects=objects,
    )


def device_id_object_count(head: bytes) -> int:
    """取设备标识响应固定头中的对象个数(内部函数,RTU 增量收包用)。

    ``head`` 为功能码之后的固定头(MEI/读取码/符合级别/MoreFollows/
    下一对象号/对象数共 6 字节)。因对象长度随响应变化,RTU 须逐个对象
    头读长度:走线层先按本函数取对象个数,再逐个读「对象号 + 长度」与
    其后的值。

    :param head: 功能码之后的 6 字节固定头
    :return: 响应中的对象个数
    :raises ProtocolFrameError: 头部长度不足
    """
    if len(head) < MODBUS_DEVICE_ID_FIXED_HEAD_SIZE:
        raise ProtocolFrameError(
            "设备标识响应头不足 {} 字节:{}".format(
                MODBUS_DEVICE_ID_FIXED_HEAD_SIZE, format_hex(head)
            )
        )
    return head[MODBUS_DEVICE_ID_FIXED_HEAD_SIZE - 1]


def build_mbap(transaction_id: int, station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 MBAP 帧(Modbus TCP/UDP)。

    :param transaction_id: 事务标识(0~65535,回包校验用)
    :param station: 站号(Unit ID)
    :param pdu: 请求 PDU
    :return: 完整 MBAP 帧
    :raises ValueError: 参数非法
    """
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError(f"事务号超出范围 0~65535:{transaction_id}")
    if not 0 <= station <= 0xFF:
        raise ValueError(f"站号超出范围 0~255:{station}")
    max_pdu = MODBUS_MAX_ADU_SIZE - MBAP_HEADER_SIZE
    if not 1 <= len(pdu) <= max_pdu:
        raise ValueError("PDU 长度超出范围 1~{}:{}".format(max_pdu, len(pdu)))
    return struct.pack(">HHHB", transaction_id, MODBUS_PROTOCOL_ID, len(pdu) + 1, station) + pdu


def parse_mbap_header(header: bytes) -> Tuple[int, int]:
    """解析 MBAP 帧头(7 字节),供 TCP 按长度收包。

    :param header: 帧头字节
    :return: ``(transaction_id, length)``,``length`` 为帧内剩余字节数 + 1
    :raises ProtocolFrameError: 帧头过短/协议号非 0/长度字段非法
    """
    if len(header) < MBAP_HEADER_SIZE:
        raise ProtocolFrameError(
            "MBAP 帧头不足 {} 字节,实收 {}(收到的原始数据:{})".format(
                MBAP_HEADER_SIZE, len(header), format_hex(header)
            )
        )
    transaction_id, protocol_id, length = struct.unpack(">HHH", header[:6])
    if protocol_id != MODBUS_PROTOCOL_ID:
        raise ProtocolFrameError(
            "MBAP 协议标识符必须为 0,收到:{}(收到的原始数据:{})".format(
                protocol_id, format_hex(header)
            )
        )
    if length < 2:
        raise ProtocolFrameError(
            "MBAP 长度字段非法(至少含站号+功能码):{}(收到的原始数据:{})".format(
                length, format_hex(header)
            )
        )
    if length > MODBUS_MBAP_LENGTH_MAX:
        raise ProtocolFrameError(
            "MBAP 长度字段超出上限 {}(按长收包将挂死,按坏帧处理):{}(收到的原始数据:{})".format(
                MODBUS_MBAP_LENGTH_MAX, length, format_hex(header)
            )
        )
    return transaction_id, length


def parse_mbap(frame: bytes) -> Tuple[int, int, bytes]:
    """解析完整 MBAP 帧。

    :param frame: 完整帧字节
    :return: ``(transaction_id, station, pdu)``
    :raises ProtocolFrameError: 帧头/长度字段非法
    """
    transaction_id, length = parse_mbap_header(frame)
    station = frame[6]
    end = MBAP_HEADER_SIZE + length - 1
    if end > len(frame):
        raise ProtocolFrameError(
            "MBAP 长度字段 {} 超出实际帧长 {}(收到的原始数据:{})".format(
                length, len(frame), format_hex(frame)
            )
        )
    return transaction_id, station, frame[MBAP_HEADER_SIZE:end]


# ----------------------------------------------------------------------
# RTU 帧(串口)
# ----------------------------------------------------------------------

def build_rtu_frame(station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 RTU 帧:站号 + PDU + CRC16(低字节在前)。

    :param station: 站号
    :param pdu: 请求 PDU
    :return: 完整 RTU 帧
    :raises ValueError: 站号/PDU 非法
    """
    if not 0 <= station <= 0xFF:
        raise ValueError(f"站号超出范围 0~255:{station}")
    if not pdu:
        raise ValueError("PDU 不能为空")
    body = bytes([station]) + pdu
    return body + crc16(body).to_bytes(2, "little")


def parse_rtu_frame(frame: bytes) -> Tuple[int, bytes]:
    """解析 RTU 帧(校验 CRC16)。

    CRC 校验失败时异常文本带**收到的原始帧**十六进制转储:CRC 不符通常
    源于线路噪声误码或收发错位,现场需要逐字节比对才能判断是哪一类
    (帧完整但 CRC 错 = 噪声;帧长异常 = 错位)。

    :param frame: 完整帧字节
    :return: ``(station, pdu)``
    :raises ProtocolFrameError: 帧过短或 CRC 校验失败(消息含原始数据)
    """
    if len(frame) < 4:
        raise ProtocolFrameError(
            "RTU 帧过短(至少 4 字节,实收 {} 字节)(收到的原始数据:{})".format(
                len(frame), format_hex(frame)
            )
        )
    body = frame[:-2]
    received = int.from_bytes(frame[-2:], "little")
    computed = crc16(body)
    if computed != received:
        raise ProtocolFrameError(
            "RTU CRC 校验失败:计算 0x{:04X},收到 0x{:04X}(收到的原始帧:{})".format(
                computed, received, format_hex(frame)
            )
        )
    return body[0], body[1:]


# ----------------------------------------------------------------------
# 收包长度推算(RTU 按长度读取用)
# ----------------------------------------------------------------------

def expected_response_length(request_pdu: bytes) -> int:
    """按请求 PDU 推算正常响应 PDU 的字节数(RTU 收包用)。

    异常响应恒为 2 字节(功能码|0x80 + 异常码),由走线层在读到
    功能码后先行分支处理,不经本函数。

    FC 43(读设备标识)响应长度随对象数与对象长度变化,**无法**事先
    推算——走线层对 FC 43 走增量收包路径,不会调用本函数;此处直接
    抛错而非静默返回错误长度。FC 23(读写多寄存器)长度由读数量决定,
    正常推算。

    :param request_pdu: 请求 PDU
    :return: 响应 PDU 的期望字节数
    :raises ProtocolFrameError: 请求 PDU 非法或功能码未知
    """
    if not request_pdu:
        raise ProtocolFrameError("请求 PDU 为空")
    function_code = request_pdu[0]
    if len(request_pdu) < 5 and function_code in (
        ModbusFunction.READ_COILS,
        ModbusFunction.READ_DISCRETE_INPUTS,
        ModbusFunction.READ_HOLDING_REGISTERS,
        ModbusFunction.READ_INPUT_REGISTERS,
    ):
        raise ProtocolFrameError("读请求 PDU 长度不足:{}".format(len(request_pdu)))
    if function_code in _READ_BIT_FUNCTIONS:
        count = struct.unpack(">H", request_pdu[3:5])[0]
        return 2 + (count + 7) // 8
    if function_code in _READ_REGISTER_FUNCTIONS:
        count = struct.unpack(">H", request_pdu[3:5])[0]
        return 2 + count * 2
    if function_code == ModbusFunction.READ_WRITE_MULTIPLE_REGISTERS:
        if len(request_pdu) < 5:
            raise ProtocolFrameError(
                "FC23 请求 PDU 长度不足:{}".format(len(request_pdu))
            )
        read_count = struct.unpack(">H", request_pdu[3:5])[0]
        return 2 + read_count * 2
    if function_code in (
        ModbusFunction.WRITE_SINGLE_COIL,
        ModbusFunction.WRITE_SINGLE_REGISTER,
        ModbusFunction.WRITE_MULTIPLE_COILS,
        ModbusFunction.WRITE_MULTIPLE_REGISTERS,
    ):
        return 5
    if function_code == ModbusFunction.MASK_WRITE_REGISTER:
        return MODBUS_MASK_WRITE_PDU_SIZE
    if function_code == ModbusFunction.DIAGNOSTICS:
        return MODBUS_DIAGNOSTICS_PDU_SIZE
    if function_code == ModbusFunction.GET_COMM_EVENT_COUNTER:
        return MODBUS_EVENT_COUNTER_PDU_SIZE
    if function_code == ModbusFunction.GET_COMM_EVENT_LOG:
        raise ProtocolFrameError(
            "FC12 响应长度随事件字节数变化,无法按请求推算:"
            "由走线层按 byte count 增量收包"
        )
    if function_code == ModbusFunction.READ_FILE_RECORD:
        return _read_file_record_response_length(request_pdu)
    if function_code == ModbusFunction.WRITE_FILE_RECORD:
        return len(request_pdu)  # 响应回显请求
    if function_code == ModbusFunction.READ_DEVICE_IDENTIFICATION:
        raise ProtocolFrameError(
            "FC43 响应长度随标识对象数与对象长度变化,无法按请求推算:"
            "由走线层按对象头增量收包(codec.device_id_object_count)"
        )
    raise ProtocolFrameError(f"未知功能码 0x{function_code:02X}")


def build_diagnostics_pdu(sub_function: int, data: int = 0x0000) -> bytes:
    """构造诊断请求 PDU(FC08:功能码 + 子功能(2) + 数据(2))。

    :raises ValueError: 子功能/数据超 16 位
    """
    if not 0 <= sub_function <= 0xFFFF:
        raise ValueError(f"FC08 子功能超出范围 0~65535:{sub_function}")
    if not 0 <= data <= 0xFFFF:
        raise ValueError(f"FC08 数据超出范围 0~65535:{data}")
    return struct.pack(">BHH", ModbusFunction.DIAGNOSTICS, sub_function, data)


def parse_diagnostics_response(pdu: bytes, sub_function: int) -> int:
    """解析 FC08 响应,返回 2 字节数据域(子功能回显须一致)。

    :raises ProtocolFrameError: PDU 过短/功能码或子功能回显不符
    """
    if len(pdu) < MODBUS_DIAGNOSTICS_PDU_SIZE or pdu[0] != ModbusFunction.DIAGNOSTICS:
        raise ProtocolFrameError(
            "FC08 响应非法:{}(收到的原始 PDU:{})".format(len(pdu), format_hex(pdu))
        )
    echoed = struct.unpack(">H", pdu[1:3])[0]
    if echoed != sub_function:
        raise ProtocolFrameError(
            "FC08 子功能回显不符:期望 0x{:04X},收到 0x{:04X}".format(sub_function, echoed)
        )
    return struct.unpack(">H", pdu[3:5])[0]


def build_get_comm_event_counter_pdu() -> bytes:
    """构造取通信事件计数器请求 PDU(FC11:仅功能码)。"""
    return bytes([ModbusFunction.GET_COMM_EVENT_COUNTER])


def parse_comm_event_counter_pdu(pdu: bytes) -> Tuple[int, int]:
    """解析 FC11 响应,返回 ``(状态字, 事件计数)``。

    :raises ProtocolFrameError: PDU 过短/功能码不符
    """
    if (
        len(pdu) < MODBUS_EVENT_COUNTER_PDU_SIZE
        or pdu[0] != ModbusFunction.GET_COMM_EVENT_COUNTER
    ):
        raise ProtocolFrameError(
            "FC11 响应非法:{}(收到的原始 PDU:{})".format(len(pdu), format_hex(pdu))
        )
    status, count = struct.unpack(">HH", pdu[1:5])
    return status, count


def build_get_comm_event_log_pdu() -> bytes:
    """构造取通信事件日志请求 PDU(FC12:仅功能码)。"""
    return bytes([ModbusFunction.GET_COMM_EVENT_LOG])


def parse_comm_event_log_pdu(pdu: bytes) -> Dict[str, object]:
    """解析 FC12 响应。

    布局:功能码(1) + byte count(1) + 状态字(2) + 事件计数(2) + 报文计数(2)
    + 事件字节(byte count − 6)。返回 ``{status, event_count, message_count, events}``。

    :raises ProtocolFrameError: PDU 过短/功能码不符/长度与 byte count 不一致
    """
    if len(pdu) < 2 or pdu[0] != ModbusFunction.GET_COMM_EVENT_LOG:
        raise ProtocolFrameError(
            "FC12 响应非法:{}(收到的原始 PDU:{})".format(len(pdu), format_hex(pdu))
        )
    byte_count = pdu[1]
    if byte_count < 6 or len(pdu) != 2 + byte_count:
        raise ProtocolFrameError(
            "FC12 byte count 与长度不符:声明 {},实际 {}".format(byte_count, len(pdu) - 2)
        )
    status, event_count, message_count = struct.unpack(">HHH", pdu[2:8])
    return {
        "status": status,
        "event_count": event_count,
        "message_count": message_count,
        "events": bytes(pdu[8:2 + byte_count]),
    }


def _check_file_record_fields(
    file_number: int, record_number: int, record_length: int
) -> None:
    """文件记录子请求字段范围校验(内部函数;规范 §6.14)。

    File number 1~0xFFFF、Record number 0~0x270F、Record length ≥1(上限由
    整体 PDU 长度约束)。
    """
    if not 0x0001 <= file_number <= 0xFFFF:
        raise ValueError(f"文件号超出范围 1~65535:{file_number}")
    if not 0x0000 <= record_number <= 0x270F:
        raise ValueError(f"记录号超出范围 0~9999:{record_number}")
    if record_length < 1:
        raise ValueError(f"记录长度必须大于 0:{record_length}")


def build_read_file_record_pdu(requests: "Sequence[Tuple[int, int, int]]") -> bytes:
    """构造读文件记录请求 PDU(FC20)。

    :param requests: ``(文件号, 起始记录号, 记录长度[寄存器数])`` 序列
    :raises ValueError: 无子请求/超上限/字段越界
    """
    if not requests:
        raise ValueError("FC20 至少需要一个文件记录子请求")
    if len(requests) > MODBUS_MAX_FILE_RECORDS:
        raise ValueError(
            f"FC20 子请求条数超出上限 {MODBUS_MAX_FILE_RECORDS}:{len(requests)}"
        )
    sub = bytearray()
    for file_number, record_number, record_length in requests:
        _check_file_record_fields(file_number, record_number, record_length)
        sub += struct.pack(
            ">BHHH", MODBUS_FILE_REFERENCE_TYPE, file_number, record_number, record_length
        )
    if not 0x07 <= len(sub) <= MODBUS_MAX_READ_FILE_BYTES:
        raise ValueError(
            "FC20 byte count 超出范围 0x07~0x{:02X}:{}".format(
                MODBUS_MAX_READ_FILE_BYTES, len(sub)
            )
        )
    return bytes([ModbusFunction.READ_FILE_RECORD, len(sub)]) + bytes(sub)


def _read_file_record_response_length(request_pdu: bytes) -> int:
    """按 FC20 请求推算响应 PDU 字节数(内部函数)。

    每条子响应 = File resp. length(1) + 引用类型(1) + 数据(2×记录长度),
    其中 File resp. length = 1 + 2×记录长度;响应 PDU = 功能码(1) + 数据长(1)
    + Σ(2 + 2×记录长度)。
    """
    if len(request_pdu) < 2:
        raise ProtocolFrameError("FC20 请求 PDU 过短")
    byte_count = request_pdu[1]
    sub = request_pdu[2:2 + byte_count]
    if len(sub) != byte_count or byte_count % 7 != 0:
        raise ProtocolFrameError("FC20 请求 byte count 非法:{}".format(byte_count))
    total = 0
    for index in range(0, byte_count, 7):
        length = struct.unpack(">H", sub[index + 5:index + 7])[0]
        total += 2 + 2 * length
    return 2 + total


def parse_read_file_record_response(
    pdu: bytes, requests: "Sequence[Tuple[int, int, int]]"
) -> List[List[int]]:
    """解析 FC20 响应,按子请求顺序返回逐条寄存器列表。

    :raises ProtocolFrameError: 长度/File resp. length/引用类型不符或有冗余字节
    """
    if len(pdu) < 2 or pdu[0] != ModbusFunction.READ_FILE_RECORD:
        raise ProtocolFrameError(
            "FC20 响应非法:{}(收到的原始 PDU:{})".format(len(pdu), format_hex(pdu))
        )
    body = pdu[2:]
    if len(body) != pdu[1]:
        raise ProtocolFrameError(
            "FC20 响应数据长与 byte count 不符:声明 {},实际 {}".format(pdu[1], len(body))
        )
    result: List[List[int]] = []
    offset = 0
    for _, _, record_length in requests:
        if offset >= len(body):
            raise ProtocolFrameError("FC20 响应子响应不足")
        file_resp_len = body[offset]
        if file_resp_len != 1 + 2 * record_length:
            raise ProtocolFrameError(
                "FC20 File resp. length 不符:期望 {},收到 {}".format(
                    1 + 2 * record_length, file_resp_len
                )
            )
        if body[offset + 1] != MODBUS_FILE_REFERENCE_TYPE:
            raise ProtocolFrameError(
                "FC20 引用类型非 0x06:0x{:02X}".format(body[offset + 1])
            )
        chunk = body[offset + 2:offset + 1 + file_resp_len]
        if len(chunk) != 2 * record_length:
            raise ProtocolFrameError("FC20 记录数据长度不足")
        result.append(
            [struct.unpack(">H", chunk[i:i + 2])[0] for i in range(0, len(chunk), 2)]
        )
        offset += 1 + file_resp_len
    if offset != len(body):
        raise ProtocolFrameError("FC20 响应尾部有冗余字节:偏移 {}".format(offset))
    return result


def build_write_file_record_pdu(records: "Sequence[Tuple[int, int, List[int]]]") -> bytes:
    """构造写文件记录请求 PDU(FC21)。

    :param records: ``(文件号, 起始记录号, 寄存器列表)`` 序列
    :raises ValueError: 无子请求/字段越界/寄存器值越界/超 PDU 上限
    """
    if not records:
        raise ValueError("FC21 至少需要一个文件记录子请求")
    sub = bytearray()
    for file_number, record_number, values in records:
        if not values:
            raise ValueError("FC21 记录数据不能为空")
        _check_file_record_fields(file_number, record_number, len(values))
        sub += struct.pack(
            ">BHHH", MODBUS_FILE_REFERENCE_TYPE, file_number, record_number, len(values)
        )
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"FC21 寄存器写入值超出范围 0~65535:{value}")
            sub += struct.pack(">H", value)
    if not 0x09 <= len(sub) <= MODBUS_MAX_WRITE_FILE_BYTES:
        raise ValueError(
            "FC21 请求数据长超出范围 0x09~0x{:02X}:{}".format(
                MODBUS_MAX_WRITE_FILE_BYTES, len(sub)
            )
        )
    return bytes([ModbusFunction.WRITE_FILE_RECORD, len(sub)]) + bytes(sub)


def parse_write_file_record_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验 FC21 写响应(规范:正常响应为请求回显,须逐字节一致)。

    :raises ProtocolFrameError: 响应与请求不符
    """
    if pdu != request_pdu:
        raise ProtocolFrameError(
            "FC21 响应应为请求回显:期望 {}(收到的原始 PDU:{})".format(
                format_hex(request_pdu), format_hex(pdu)
            )
        )


def _check_offset(offset: int) -> None:
    """校验 0 基地址偏移(内部函数)。"""
    if not 0 <= offset <= MODBUS_ADDRESS_MAX:
        raise ValueError(f"地址偏移超出范围 0~{MODBUS_ADDRESS_MAX}:{offset}")


def _check_span(offset: int, count: int) -> None:
    """校验起始地址 + 数量不越过地址空间顶端(内部函数)。

    规范的状态图(§6.1 Figure 11 等)把 ``Starting Address + Quantity``
    作为服务端校验项,越界返回异常码 02(ILLEGAL DATA ADDRESS)。客户端
    在组帧期直接拒绝,避免发出必然被拒的请求(如 ``hr65535`` 读 2 字)。
    """
    if offset + count > MODBUS_ADDRESS_MAX + 1:
        raise ValueError(
            "起始地址 + 数量超出 Modbus 地址空间 0~{}:{}+{}={}".format(
                MODBUS_ADDRESS_MAX, offset, count, offset + count
            )
        )
