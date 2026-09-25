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
from typing import List, Tuple

from ..convert import crc16
from ..core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_COMMAND_MASK_WRITE,
    MODBUS_COIL_OFF,
    MODBUS_COIL_ON,
    MODBUS_EXCEPTION_FLAG,
    MODBUS_EXCEPTION_TEXT,
    MODBUS_MASK_WRITE_PDU_SIZE,
    MODBUS_MAX_ADU_SIZE,
    MODBUS_MAX_READ_BITS,
    MODBUS_MAX_READ_REGISTERS,
    MODBUS_MAX_WRITE_BITS,
    MODBUS_MAX_WRITE_REGISTERS,
    MODBUS_MBAP_LENGTH_MAX,
    MODBUS_PROTOCOL_ID,
)
from ..core.debug import format_hex
from ..core.errors import DeviceError, ProtocolFrameError


class ModbusFunction(IntEnum):
    """Modbus 功能码(枚举化,便于 IDE 补全与类型检查)。"""

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_HOLDING_REGISTERS = 0x03
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_SINGLE_REGISTER = 0x06
    WRITE_MULTIPLE_COILS = 0x0F
    WRITE_MULTIPLE_REGISTERS = 0x10
    MASK_WRITE_REGISTER = 0x16


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
# MBAP 帧(TCP/UDP 共用)
# ----------------------------------------------------------------------

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
    if function_code in (
        ModbusFunction.WRITE_SINGLE_COIL,
        ModbusFunction.WRITE_SINGLE_REGISTER,
        ModbusFunction.WRITE_MULTIPLE_COILS,
        ModbusFunction.WRITE_MULTIPLE_REGISTERS,
    ):
        return 5
    if function_code == ModbusFunction.MASK_WRITE_REGISTER:
        return MODBUS_MASK_WRITE_PDU_SIZE
    raise ProtocolFrameError(f"未知功能码 0x{function_code:02X}")


def _check_offset(offset: int) -> None:
    """校验 0 基地址偏移(内部函数)。"""
    if not 0 <= offset <= 0xFFFF:
        raise ValueError(f"地址偏移超出范围 0~65535:{offset}")
