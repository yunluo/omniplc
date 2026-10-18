"""Modbus 报文编解码(纯函数)。

本模块所有函数都是 bytes ↔ 结构的纯函数,不接触 socket,可完全
用黄金报文样本(:doc:`tests/golden/README`)做单元测试。

依据(docs/protocol/modbus/ 三份官方规范归档,页码均为手册自印页,
与 PDF 页一致——页脚 N/50·N/46·N/44 已抽样直证,review-1023 §八):

- 应用协议:Modbus Application Protocol V1.1b3——功能码语义与 PDU 布局
  §6.1~§6.21(印刷页 11~44:FC01 p.11 / FC02 p.12 / FC03 p.15 /
  FC04 p.16 / FC05 p.17 / FC06 p.19 / FC07 p.20 / FC08 p.21~22 /
  FC11 p.25 / FC12 p.26~27 / FC15 p.29 / FC16 p.30 / FC17 p.31 /
  FC20 p.32~33 / FC21 p.34~35 / FC22 p.36~37 / FC23 p.38~39 /
  FC24 p.40 / FC43(设备标识)p.43~44),异常响应与异常码表 §7
  (印刷页 47~49:§7 标题在 47,响应格式在 48,码表 48~49),
  数据编码大端 §4.2(印刷页 5),数据模型
  §4.3~4.4(地址 0~65535);
- TCP 实施指南:MODBUS Messaging Implementation Guide V1.0b——MBAP
  帧头逐字段 §3.1.3(印刷页 5~6,长度域含 Unit Identifier);
- 串口链路:MODBUS over Serial Line V1.02——地址规则 §2.2(印刷页 8,
  0 = 广播、1~247 = 从站、248~255 = Reserved)、RTU 帧布局与 256 字节
  上限 §2.5.1(印刷页 13)、RTU 帧 CRC 校验 §2.5.1.2(印刷页 14~15)
  与 CRC 算法 §6.2.2(印刷页 39~41,初值 FFFF / 多项式 0xA001 /
  低字节在前——高低字节交换说明见 p.42 的 C 函数说明)。

各函数 docstring 就近标注具体小节与页码(格式:``依据:…``)。
"""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from ...core.convert import crc16
from ...core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_ADDRESS_MAX,
    MODBUS_COMMAND_DIAGNOSTICS,
    MODBUS_COMMAND_GET_COMM_EVENT_COUNTER,
    MODBUS_COMMAND_GET_COMM_EVENT_LOG,
    MODBUS_COMMAND_MASK_WRITE,
    MODBUS_COMMAND_READ_DEVICE_ID,
    MODBUS_COMMAND_READ_EXCEPTION_STATUS,
    MODBUS_COMMAND_READ_FIFO,
    MODBUS_COMMAND_READ_FILE_RECORD,
    MODBUS_COMMAND_READ_WRITE_MULTIPLE,
    MODBUS_COMMAND_REPORT_SERVER_ID,
    MODBUS_COMMAND_WRITE_FILE_RECORD,
    MODBUS_COIL_OFF,
    MODBUS_COIL_ON,
    MODBUS_DEVICE_ID_CODE_BASIC,
    MODBUS_DEVICE_ID_CODE_INDIVIDUAL,
    MODBUS_DEVICE_ID_FIXED_HEAD_SIZE,
    MODBUS_DEVICE_ID_PDU_HEAD_SIZE,
    MODBUS_DEVICE_ID_RESERVED_MAX,
    MODBUS_DEVICE_ID_RESERVED_MIN,
    MODBUS_DIAGNOSTICS_PDU_SIZE,
    MODBUS_EVENT_COUNTER_PDU_SIZE,
    MODBUS_EVENT_LOG_MAX_BYTE_COUNT,
    MODBUS_EXCEPTION_FLAG,
    MODBUS_EXCEPTION_TEXT,
    MODBUS_FILE_REFERENCE_TYPE,
    MODBUS_MASK_WRITE_PDU_SIZE,
    MODBUS_MAX_ADU_SIZE,
    MODBUS_MAX_FIFO_REGISTERS,
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
    MODBUS_RTU_MAX_ADU_SIZE,
    MODBUS_SERVER_ID_MIN_BYTE_COUNT,
    MODBUS_STATION_MAX,
)
from ...core.debug import format_hex
from ...core.errors import DeviceError, ProtocolFrameError
from ...core.i18n import _


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
    READ_FIFO_QUEUE = MODBUS_COMMAND_READ_FIFO
    DIAGNOSTICS = MODBUS_COMMAND_DIAGNOSTICS
    READ_EXCEPTION_STATUS = MODBUS_COMMAND_READ_EXCEPTION_STATUS
    REPORT_SERVER_ID = MODBUS_COMMAND_REPORT_SERVER_ID
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
    依据:Modbus 应用协议 V1.1b3 §6.1~§6.4(印刷页 11~16):请求 = 功能码(1)+起始地址(2)+数量(2);    数量上限 2000 位(p.11)/125 字(p.15)。
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
        raise ValueError(_("读功能码必须是 1/2/3/4,收到:{}").format(function_code))
    _check_offset(offset)
    _check_span(offset, count)
    if not 1 <= count <= limit:
        raise ValueError(_("读数量超出范围 1~{}:{}").format(limit, count))
    return struct.pack(">BHH", function_code, offset, count)


def build_write_single_pdu(function_code: int, offset: int, value: int) -> bytes:
    """构造单点写请求 PDU(FC 05 写线圈 / FC 06 写单寄存器)。
    依据:Modbus 应用协议 V1.1b3 §6.5~§6.6(印刷页 17~19):FC05 线圈值 0xFF00/0x0000、    FC06 寄存器 1 字。
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
            raise ValueError(_("寄存器写入值超出范围 0~65535:{}").format(value))
        payload = value
    else:
        raise ValueError(_("单点写功能码必须是 5 或 6,收到:{}").format(function_code))
    return struct.pack(">BHH", function_code, offset, payload)


def build_write_multi_pdu(
    function_code: int, offset: int, values: Sequence[int]
) -> bytes:
    """构造批量写请求 PDU(FC 15 写多线圈 / FC 16 写多寄存器)。
    依据:Modbus 应用协议 V1.1b3 §6.11~§6.12(印刷页 29~30):位打包 LSB 在前、    字节计数 = 数量 × 2;数量上限 1968 位(p.29)/123 字(p.30)。
    :param function_code: 15 或 16
    :param offset: 0 基起始地址
    :param values: 寄存器原始值序列;线圈传 0/1 序列,由本函数按 LSB 在前打包
      (``Sequence`` 协变:调用方直接传 ``List[bool]``,bool 本就是 int 子类)
    :raises ValueError: 功能码/地址/数量/数值非法
    """
    _check_offset(offset)
    if function_code == ModbusFunction.WRITE_MULTIPLE_COILS:
        if not 1 <= len(values) <= MODBUS_MAX_WRITE_BITS:
            raise ValueError(
                _("写线圈数量超出范围 1~{}:{}").format(
                    MODBUS_MAX_WRITE_BITS, len(values)
                )
            )
        _check_span(offset, len(values))
        packed = bytearray((len(values) + 7) // 8)
        for index, flag in enumerate(values):
            if flag:
                packed[index // 8] |= 1 << (index % 8)
        return struct.pack(
            ">BHHB", function_code, offset, len(values), len(packed)
        ) + bytes(packed)
    if function_code == ModbusFunction.WRITE_MULTIPLE_REGISTERS:
        if not 1 <= len(values) <= MODBUS_MAX_WRITE_REGISTERS:
            raise ValueError(
                _("写寄存器数量超出范围 1~{}:{}").format(
                    MODBUS_MAX_WRITE_REGISTERS, len(values)
                )
            )
        _check_span(offset, len(values))
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(_("寄存器写入值超出范围 0~65535:{}").format(value))
        return struct.pack(
            ">BHHB", function_code, offset, len(values), len(values) * 2
        ) + struct.pack(">{:d}H".format(len(values)), *values)
    raise ValueError(_("批量写功能码必须是 15 或 16,收到:{}").format(function_code))


# ----------------------------------------------------------------------
# 响应 PDU 解析(坏帧抛 ProtocolFrameError,PLC 异常码抛 DeviceError)
# ----------------------------------------------------------------------


def check_response_exception(pdu: bytes, request_function_code: int) -> None:
    """识别异常响应:PLC 返回异常码时抛 :class:`DeviceError`。
    依据:Modbus 应用协议 V1.1b3 §7(印刷页 47~49):异常响应 = 功能码|0x80 + 异常码(1),    异常码表逐条一致。
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
        raise ProtocolFrameError(_("响应 PDU 为空(未收到任何字节)"))
    if pdu[0] & MODBUS_EXCEPTION_FLAG:
        if len(pdu) < 2:
            raise ProtocolFrameError(
                _("异常响应缺少异常码(收到的原始数据:{})").format(format_hex(pdu))
            )
        if pdu[0] ^ MODBUS_EXCEPTION_FLAG != request_function_code:
            raise ProtocolFrameError(
                _(
                    "异常响应功能码不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})"
                ).format(request_function_code, pdu[0], format_hex(pdu))
            )
        code = pdu[1]
        text = _(MODBUS_EXCEPTION_TEXT.get(code, _("未知异常码(厂商自定义/保留码)")))
        raise DeviceError(
            _("Modbus 异常码 0x{:02X}({})(请求功能码 0x{:02X})").format(
                code, text, request_function_code
            ),
            code,
        )
    if pdu[0] != request_function_code:
        raise ProtocolFrameError(
            _("响应功能码不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})").format(
                request_function_code, pdu[0], format_hex(pdu)
            )
        )


def parse_read_response(pdu: bytes, function_code: int, count: int) -> List[int]:
    """解析读响应 PDU(FC 01/02/03/04)。
    依据:Modbus 应用协议 V1.1b3 §6.1~§6.4(印刷页 11~16):响应 = 功能码 + 字节计数(1)+数据    (位按 ⌈数量/8⌉ 打包,字按数量 × 2)。
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
            _("读响应 PDU 长度不足(收到的原始数据:{})").format(format_hex(pdu))
        )
    byte_count = pdu[1]
    expected = (count + 7) // 8 if function_code in _READ_BIT_FUNCTIONS else count * 2
    if byte_count != expected or len(pdu) != expected + 2:
        raise ProtocolFrameError(
            _(
                "读响应长度不符:字节计数域 {},期望 {},实际 PDU {} 字节(收到的原始数据:{})"
            ).format(byte_count, expected, len(pdu), format_hex(pdu))
        )
    if function_code in _READ_BIT_FUNCTIONS:
        return [
            1 if pdu[2 + index // 8] & (1 << (index % 8)) else 0
            for index in range(count)
        ]
    return list(struct.unpack(f">{count:d}H", pdu[2 : 2 + expected]))


def parse_write_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验写响应(FC 05/06/15/16):正常应答为请求 PDU 前 5 字节的回显。
    依据:Modbus 应用协议 V1.1b3 §6.5/§6.6/§6.11/§6.12(印刷页 17~30):正常应答为请求回显。
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 回显与请求不符
    """
    check_response_exception(pdu, request_pdu[0])
    if len(pdu) != 5 or pdu != request_pdu[:5]:
        raise ProtocolFrameError(
            _("写响应回显不符:期望 {},收到 {}").format(
                format_hex(request_pdu[:5]), format_hex(pdu)
            )
        )


def build_mask_write_pdu(
    offset: int, and_mask: int, or_mask: int, byte_order: str = "big"
) -> bytes:
    """构造掩码写请求 PDU(FC 22)。
    依据:Modbus 应用协议 V1.1b3 §6.16(印刷页 36~37):两掩码各 2 字节;数据编码大端 §4.2(印刷页 5)。
    设备侧原子执行 ``新值 = (当前值 AND and_mask) OR (or_mask AND NOT and_mask)``。

    :param offset: 0 基保持寄存器地址
    :param and_mask: AND 掩码(0~65535;置 0 的位被清零)
    :param or_mask: OR 掩码(0~65535;仅在 and_mask 为 1 的位上可置 1)
    :param byte_order: 掩码字节序(``"big"``/``"little"``)。规范为 big;
        部分施耐德机型按本机字序(little)解释掩码——**待核**(review-1023
        §8.2-5:施耐德手册不在档,该说法无页码级出处,不臆造依据;默认
        ``big`` 与规范一致,little 为现场实测兼容开关,已登记 protocol README
        待补表,拿到手册/真机抓包后核证)
    :return: PDU 字节(功能码 + 地址 + AND 掩码 + OR 掩码;地址恒大端)
    :raises ValueError: 地址/掩码/字节序非法
    """
    _check_offset(offset)
    if not 0 <= and_mask <= 0xFFFF:
        raise ValueError(_("and_mask 超出范围 0~65535:{}").format(and_mask))
    if not 0 <= or_mask <= 0xFFFF:
        raise ValueError(_("or_mask 超出范围 0~65535:{}").format(or_mask))
    order = str(byte_order).strip().lower()
    if order not in ("big", "little"):
        raise ValueError(_("byte_order 非法(仅 big/little):{!r}").format(byte_order))
    masks = (
        struct.pack(">HH", and_mask, or_mask)
        if order == "big"
        else struct.pack("<HH", and_mask, or_mask)
    )
    return struct.pack(">BH", MODBUS_COMMAND_MASK_WRITE, offset) + masks


def parse_mask_write_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验掩码写响应(FC 22):正常应答为 7 字节请求回显。
    依据:Modbus 应用协议 V1.1b3 §6.16(印刷页 36~37):正常应答为请求回显。
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 回显与请求不符
    """
    check_response_exception(pdu, request_pdu[0])
    if len(pdu) != MODBUS_MASK_WRITE_PDU_SIZE or pdu != request_pdu:
        raise ProtocolFrameError(
            _("掩码写响应回显不符:期望 {},收到 {}").format(
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
    依据:Modbus 应用协议 V1.1b3 §6.17(印刷页 38~39):单事务先写后读;读数量上限 125、写数量上限 121(p.38)。
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
            _("FC23 读数量超出范围 1~{}:{}").format(
                MODBUS_MAX_READ_REGISTERS, read_count
            )
        )
    if not 1 <= len(values) <= MODBUS_MAX_RW_WRITE_REGISTERS:
        raise ValueError(
            _("FC23 写数量超出范围 1~{}:{}").format(
                MODBUS_MAX_RW_WRITE_REGISTERS, len(values)
            )
        )
    _check_span(read_offset, read_count)
    _check_span(write_offset, len(values))
    for value in values:
        if not 0 <= value <= 0xFFFF:
            raise ValueError(_("寄存器写入值超出范围 0~65535:{}").format(value))
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
    依据:Modbus 应用协议 V1.1b3 §6.17(印刷页 38~39):响应 = 功能码 + 字节计数 + 读数据。
    响应 = 功能码(1) + 字节计数(1,= 2 × 读数量) + 读数据(2N,大端)。

    :param pdu: 响应 PDU
    :param read_count: 请求的读数量(用于校验字节计数)
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: 帧长度/字节计数不符
    """
    check_response_exception(pdu, ModbusFunction.READ_WRITE_MULTIPLE_REGISTERS)
    if len(pdu) < 2:
        raise ProtocolFrameError(
            _("FC23 响应 PDU 长度不足(收到的原始数据:{})").format(format_hex(pdu))
        )
    byte_count = pdu[1]
    expected = read_count * 2
    if byte_count != expected or len(pdu) != expected + 2:
        raise ProtocolFrameError(
            _(
                "FC23 响应长度不符:字节计数域 {},期望 {},实际 PDU {} 字节(收到的原始数据:{})"
            ).format(byte_count, expected, len(pdu), format_hex(pdu))
        )
    return list(struct.unpack(f">{read_count:d}H", pdu[2 : 2 + expected]))


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


def check_device_id_object(object_id: int) -> None:
    """校验设备标识对象号:0~255 且不落在规范保留区间 0x07~0x7F(内部/公共)。
    依据:Modbus 应用协议 V1.1b3 §6.21(印刷页 43~44):对象 0x00~0x06、保留 0x07~0x7F、私有 0x80~0xFF。
    标准对象 0x00~0x06、厂商私有对象 0x80~0xFF 合法;0x07~0x7F 为规范
    保留值,请求与响应均不应出现。

    :raises ValueError: 对象号越界或落在保留区间
    """
    if not 0 <= object_id <= 0xFF:
        raise ValueError(_("设备标识对象号超出范围 0~255:{}").format(object_id))
    if MODBUS_DEVICE_ID_RESERVED_MIN <= object_id <= MODBUS_DEVICE_ID_RESERVED_MAX:
        raise ValueError(
            _("设备标识对象号 0x{:02X} 落在规范保留区间 0x{:02X}~0x{:02X}").format(
                object_id, MODBUS_DEVICE_ID_RESERVED_MIN, MODBUS_DEVICE_ID_RESERVED_MAX
            )
        )


def build_device_id_pdu(read_device_id_code: int, object_id: int) -> bytes:
    """构造读设备标识请求 PDU(FC 43 / MEI 0x0E)。
    依据:Modbus 应用协议 V1.1b3 §6.21(印刷页 43~44):MEI 类型 0x0E + 读取码(1~4)+对象号。
    规范 §6.21:PDU = 功能码(1) + MEI 类型(1) + 读取码(1) + 对象号(1)。

    :param read_device_id_code: 访问类型 1~4(1 基本/2 常规/3 扩展为
        流式访问,4 为单个对象的个体访问)
    :param object_id: 起始对象号(0~255,且不落在保留区间 0x07~0x7F;
        首次流式访问传 0)
    :raises ValueError: 读取码/对象号非法
    """
    if (
        not MODBUS_DEVICE_ID_CODE_BASIC
        <= read_device_id_code
        <= MODBUS_DEVICE_ID_CODE_INDIVIDUAL
    ):
        raise ValueError(
            _("读设备标识访问码必须是 1~4,收到:{}").format(read_device_id_code)
        )
    check_device_id_object(object_id)
    return bytes(
        [
            ModbusFunction.READ_DEVICE_IDENTIFICATION,
            MODBUS_MEI_TYPE_DEVICE_ID,
            read_device_id_code,
            object_id,
        ]
    )


def parse_device_id_response(
    pdu: bytes, expected_read_code: Optional[int] = None
) -> DeviceIdentification:
    """解析读设备标识响应 PDU(FC 43 / MEI 0x0E)。
    依据:Modbus 应用协议 V1.1b3 §6.21(印刷页 43~44):一致性级别/流式-个体语义、固定头与对象布局。
    响应 = 功能码(1) + MEI(1) + 读取码(1) + 符合级别(1) +
    MoreFollows(1) + 下一对象号(1) + 对象数(1)
    + 对象数 × [对象号(1) + 长度(1) + 值(长度)]。

    :param pdu: 响应 PDU
    :param expected_read_code: 期望回显的读取码(请求侧读取码);给定时校验
        响应第 3 字节,不符按坏帧拒绝
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: MEI/读取码回显不符、响应截断或对象长度自洽校验失败
    """
    check_response_exception(pdu, ModbusFunction.READ_DEVICE_IDENTIFICATION)
    if len(pdu) < MODBUS_DEVICE_ID_PDU_HEAD_SIZE:
        raise ProtocolFrameError(
            _("设备标识响应不完整:至少 {} 字节,实际 {}(收到的原始数据:{})").format(
                MODBUS_DEVICE_ID_PDU_HEAD_SIZE, len(pdu), format_hex(pdu)
            )
        )
    if pdu[1] != MODBUS_MEI_TYPE_DEVICE_ID:
        raise ProtocolFrameError(
            _(
                "设备标识响应 MEI 类型不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})"
            ).format(MODBUS_MEI_TYPE_DEVICE_ID, pdu[1], format_hex(pdu))
        )
    if expected_read_code is not None and pdu[2] != expected_read_code:
        raise ProtocolFrameError(
            _(
                "设备标识响应读取码回显不符:期望 0x{:02X},收到 0x{:02X}(收到的原始数据:{})"
            ).format(expected_read_code, pdu[2], format_hex(pdu))
        )
    conformity_level = pdu[3]
    more_follows = pdu[4] == 0xFF
    next_object_id = pdu[5]
    object_count = pdu[6]
    objects: List[Tuple[int, bytes]] = []
    seen: set = set()
    cursor = MODBUS_DEVICE_ID_PDU_HEAD_SIZE
    for index in range(object_count):
        if cursor + 2 > len(pdu):
            raise ProtocolFrameError(
                _(
                    "设备标识响应第 {} 个对象头被截断(期望 2 字节,剩余 {} 字节)"
                    "(收到的原始数据:{})"
                ).format(index + 1, len(pdu) - cursor, format_hex(pdu))
            )
        object_id = pdu[cursor]
        if object_id in seen:
            raise ProtocolFrameError(
                _("设备标识响应对象号 0x{:02X} 重复(收到的原始数据:{})").format(
                    object_id, format_hex(pdu)
                )
            )
        if MODBUS_DEVICE_ID_RESERVED_MIN <= object_id <= MODBUS_DEVICE_ID_RESERVED_MAX:
            raise ProtocolFrameError(
                _(
                    "设备标识响应对象号 0x{:02X} 落在规范保留区间 0x{:02X}~0x{:02X}"
                    "(收到的原始数据:{})"
                ).format(
                    object_id,
                    MODBUS_DEVICE_ID_RESERVED_MIN,
                    MODBUS_DEVICE_ID_RESERVED_MAX,
                    format_hex(pdu),
                )
            )
        seen.add(object_id)
        length = pdu[cursor + 1]
        start = cursor + 2
        if start + length > len(pdu):
            raise ProtocolFrameError(
                _(
                    "设备标识响应第 {} 个对象(号 0x{:02X})值被截断:声明 {} 字节,"
                    "实际剩余 {}(收到的原始数据:{})"
                ).format(
                    index + 1, object_id, length, len(pdu) - start, format_hex(pdu)
                )
            )
        objects.append((object_id, pdu[start : start + length]))
        cursor = start + length
    if cursor != len(pdu):
        raise ProtocolFrameError(
            _(
                "设备标识响应长度不符:解析 {} 字节,实际 PDU {} 字节(收到的原始数据:{})"
            ).format(cursor, len(pdu), format_hex(pdu))
        )
    return DeviceIdentification(
        conformity_level=conformity_level,
        more_follows=more_follows,
        next_object_id=next_object_id,
        objects=objects,
    )


def device_id_object_count(head: bytes) -> int:
    """取设备标识响应固定头中的对象个数(内部函数,RTU 增量收包用)。
    依据:Modbus 应用协议 V1.1b3 §6.21(印刷页 43~44):固定头含对象数域。
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
            _("设备标识响应头不足 {} 字节:{}").format(
                MODBUS_DEVICE_ID_FIXED_HEAD_SIZE, format_hex(head)
            )
        )
    return head[MODBUS_DEVICE_ID_FIXED_HEAD_SIZE - 1]


def build_mbap(transaction_id: int, station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 MBAP 帧(Modbus TCP;MBAP 为 TCP 封装头,本库无
    Modbus UDP 客户端——review-1023 §8.2-9 措辞订正)。
    依据:Modbus TCP 实施指南 V1.0b §3.1.3(印刷页 5~6):事务号(2)+协议号(2)+长度(2,
    **含 Unit Identifier**)+ Unit ID(1)。

    :param transaction_id: 事务标识(0~65535,回包校验用)
    :param station: 站号(Unit ID)
    :param pdu: 请求 PDU
    :return: 完整 MBAP 帧
    :raises ValueError: 参数非法
    """
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError(_("事务号超出范围 0~65535:{}").format(transaction_id))
    if not 0 <= station <= 0xFF:
        raise ValueError(_("站号超出范围 0~255:{}").format(station))
    max_pdu = MODBUS_MAX_ADU_SIZE - MBAP_HEADER_SIZE
    if not 1 <= len(pdu) <= max_pdu:
        raise ValueError(_("PDU 长度超出范围 1~{}:{}").format(max_pdu, len(pdu)))
    return (
        struct.pack(">HHHB", transaction_id, MODBUS_PROTOCOL_ID, len(pdu) + 1, station)
        + pdu
    )


def parse_mbap_header(header: bytes) -> Tuple[int, int]:
    """解析 MBAP 帧头(7 字节),供 TCP 按长度收包。
    依据:Modbus TCP 实施指南 V1.0b §3.1.3(印刷页 5~6):长度域覆盖 Unit ID + PDU。
    :param header: 帧头字节
    :return: ``(transaction_id, length)``,``length`` 为帧内剩余字节数 + 1
    :raises ProtocolFrameError: 帧头过短/协议号非 0/长度字段非法
    """
    if len(header) < MBAP_HEADER_SIZE:
        raise ProtocolFrameError(
            _("MBAP 帧头不足 {} 字节,实收 {}(收到的原始数据:{})").format(
                MBAP_HEADER_SIZE, len(header), format_hex(header)
            )
        )
    transaction_id, protocol_id, length = struct.unpack(">HHH", header[:6])
    if protocol_id != MODBUS_PROTOCOL_ID:
        raise ProtocolFrameError(
            _("MBAP 协议标识符必须为 0,收到:{}(收到的原始数据:{})").format(
                protocol_id, format_hex(header)
            )
        )
    if length < 2:
        raise ProtocolFrameError(
            _("MBAP 长度字段非法(至少含站号+功能码):{}(收到的原始数据:{})").format(
                length, format_hex(header)
            )
        )
    if length > MODBUS_MBAP_LENGTH_MAX:
        raise ProtocolFrameError(
            _(
                "MBAP 长度字段超出上限 {}(按长收包将挂死,按坏帧处理):{}(收到的原始数据:{})"
            ).format(MODBUS_MBAP_LENGTH_MAX, length, format_hex(header))
        )
    return transaction_id, length


def parse_mbap(frame: bytes) -> Tuple[int, int, bytes]:
    """解析完整 MBAP 帧。
    依据:Modbus TCP 实施指南 V1.0b §3.1.3(印刷页 5~6)。
    :param frame: 完整帧字节
    :return: ``(transaction_id, station, pdu)``
    :raises ProtocolFrameError: 帧头/长度字段非法
    """
    transaction_id, length = parse_mbap_header(frame)
    station = frame[6]
    end = MBAP_HEADER_SIZE + length - 1
    if end > len(frame):
        raise ProtocolFrameError(
            _("MBAP 长度字段 {} 超出实际帧长 {}(收到的原始数据:{})").format(
                length, len(frame), format_hex(frame)
            )
        )
    return transaction_id, station, frame[MBAP_HEADER_SIZE:end]


# ----------------------------------------------------------------------
# RTU 帧(串口)
# ----------------------------------------------------------------------


def build_rtu_frame(station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 RTU 帧:站号 + PDU + CRC16(低字节在前)。

    依据:Modbus 串口链路 V1.02 §2.2(印刷页 8):地址 0 = 广播、
    1~247 = 从站、248~255 = Reserved;§2.5.1(印刷页 13):RTU 帧
    最大 256 字节(站号 1 + PDU ≤ 253 + CRC 2)。

    :param station: 站号(0 广播 / 1~247 从站)
    :param pdu: 请求 PDU
    :return: 完整 RTU 帧
    :raises ValueError: 站号/PDU 非法或超 ADU 上限
    """
    if not 0 <= station <= MODBUS_STATION_MAX:
        # 248~255 为规范保留地址(review-1023 §8.2-7:原放行 0~255)
        raise ValueError(
            _("RTU 站号超出范围 0~{}:{}").format(MODBUS_STATION_MAX, station)
        )
    if not pdu:
        raise ValueError(_("PDU 不能为空"))
    if len(pdu) + 3 > MODBUS_RTU_MAX_ADU_SIZE:
        raise ValueError(
            _("RTU 帧超出最大 {} 字节:站号 + PDU({}) + CRC = {}").format(
                MODBUS_RTU_MAX_ADU_SIZE, len(pdu), len(pdu) + 3
            )
        )
    body = bytes([station]) + pdu
    return body + crc16(body).to_bytes(2, "little")


def parse_rtu_frame(frame: bytes) -> Tuple[int, bytes]:
    """解析 RTU 帧(校验 CRC16)。
    依据:Modbus 串口链路 V1.02 §2.5.1(印刷页 13~14)与 §6.2.2(印刷页 39~41):    CRC 范围 = 站号 + PDU,低字节在前、初值 FFFF、多项式 0xA001。
    CRC 校验失败时异常文本带**收到的原始帧**十六进制转储:CRC 不符通常
    源于线路噪声误码或收发错位,现场需要逐字节比对才能判断是哪一类
    (帧完整但 CRC 错 = 噪声;帧长异常 = 错位)。

    :param frame: 完整帧字节
    :return: ``(station, pdu)``
    :raises ProtocolFrameError: 帧过短或 CRC 校验失败(消息含原始数据)
    """
    if len(frame) < 4:
        raise ProtocolFrameError(
            _("RTU 帧过短(至少 4 字节,实收 {} 字节)(收到的原始数据:{})").format(
                len(frame), format_hex(frame)
            )
        )
    body = frame[:-2]
    received = int.from_bytes(frame[-2:], "little")
    computed = crc16(body)
    if computed != received:
        raise ProtocolFrameError(
            _("RTU CRC 校验失败:计算 0x{:04X},收到 0x{:04X}(收到的原始帧:{})").format(
                computed, received, format_hex(frame)
            )
        )
    return body[0], body[1:]


# ----------------------------------------------------------------------
# 收包长度推算(RTU 按长度读取用)
# ----------------------------------------------------------------------


def expected_response_length(request_pdu: bytes) -> int:
    """按请求 PDU 推算正常响应 PDU 的字节数(RTU 收包用)。
    依据:各响应布局见 Modbus 应用协议 V1.1b3 §6.x 对应小节;总帧长受 Modbus 串口链路 V1.02 §2.5.1(印刷页 13)    256 字节上限约束。
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
        raise ProtocolFrameError(_("请求 PDU 为空"))
    function_code = request_pdu[0]
    if len(request_pdu) < 5 and function_code in (
        ModbusFunction.READ_COILS,
        ModbusFunction.READ_DISCRETE_INPUTS,
        ModbusFunction.READ_HOLDING_REGISTERS,
        ModbusFunction.READ_INPUT_REGISTERS,
    ):
        raise ProtocolFrameError(_("读请求 PDU 长度不足:{}").format(len(request_pdu)))
    if function_code in _READ_BIT_FUNCTIONS:
        count = struct.unpack(">H", request_pdu[3:5])[0]
        return 2 + (count + 7) // 8
    if function_code in _READ_REGISTER_FUNCTIONS:
        count = struct.unpack(">H", request_pdu[3:5])[0]
        return 2 + count * 2
    if function_code == ModbusFunction.READ_WRITE_MULTIPLE_REGISTERS:
        if len(request_pdu) < 5:
            raise ProtocolFrameError(
                _("FC23 请求 PDU 长度不足:{}").format(len(request_pdu))
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
    if function_code == ModbusFunction.READ_EXCEPTION_STATUS:
        return 2
    if function_code == ModbusFunction.GET_COMM_EVENT_COUNTER:
        return MODBUS_EVENT_COUNTER_PDU_SIZE
    if function_code == ModbusFunction.GET_COMM_EVENT_LOG:
        raise ProtocolFrameError(
            _(
                "FC12 响应长度随事件字节数变化,无法按请求推算:"
                "由走线层按 byte count 增量收包"
            )
        )
    if function_code == ModbusFunction.REPORT_SERVER_ID:
        raise ProtocolFrameError(
            _(
                "FC17 响应长度随附加数据变化,无法按请求推算:"
                "由走线层按 byte count 增量收包"
            )
        )
    if function_code == ModbusFunction.READ_FIFO_QUEUE:
        raise ProtocolFrameError(
            _("FC24 响应长度随 FIFO 计数变化,无法按请求推算:由走线层按字节计数增量收包")
        )
    if function_code == ModbusFunction.READ_FILE_RECORD:
        return _read_file_record_response_length(request_pdu)
    if function_code == ModbusFunction.WRITE_FILE_RECORD:
        return len(request_pdu)  # 响应回显请求
    if function_code == ModbusFunction.READ_DEVICE_IDENTIFICATION:
        raise ProtocolFrameError(
            _(
                "FC43 响应长度随标识对象数与对象长度变化,无法按请求推算:"
                "由走线层按对象头增量收包(codec.device_id_object_count)"
            )
        )
    raise ProtocolFrameError(_("未知功能码 0x{:02X}").format(function_code))


def build_diagnostics_pdu(sub_function: int, data: int = 0x0000) -> bytes:
    """构造诊断请求 PDU(FC08:功能码 + 子功能(2) + 数据(2))。
    依据:Modbus 应用协议 V1.1b3 §6.8(印刷页 21):请求 = 功能码 + 子功能(2)+数据(N×2);本库请求侧恒发单字数据。
    :raises ValueError: 子功能/数据超 16 位
    """
    if not 0 <= sub_function <= 0xFFFF:
        raise ValueError(_("FC08 子功能超出范围 0~65535:{}").format(sub_function))
    if not 0 <= data <= 0xFFFF:
        raise ValueError(_("FC08 数据超出范围 0~65535:{}").format(data))
    return struct.pack(">BHH", ModbusFunction.DIAGNOSTICS, sub_function, data)


def parse_diagnostics_response(pdu: bytes, sub_function: int) -> List[int]:
    """解析 FC08 响应,返回数据域全部 2 字节字(规范 §6.8:Data N × 2 Bytes)。

    历史实现硬编码单字(总长恒 5 字节),对 N=2 的合法回显误报坏帧
    (review-1023 §8.2-3);现按"整字数且 ≥1 字"校验,返回全部数据字。
    客户端请求恒为单字数据域(:func:`build_diagnostics_pdu`),调用方按
    请求数据字数取用——多字数据域仅对 N 字自定义请求合法。

    :return: 数据域按大端逐字解码的列表(N ≥ 1)
    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: 长度非整字数/功能码或子功能回显不符
    """
    check_response_exception(pdu, ModbusFunction.DIAGNOSTICS)
    if len(pdu) < MODBUS_DIAGNOSTICS_PDU_SIZE or (len(pdu) - 3) % 2 != 0:
        raise ProtocolFrameError(
            _("FC08 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    echoed = struct.unpack(">H", pdu[1:3])[0]
    if echoed != sub_function:
        raise ProtocolFrameError(
            _("FC08 子功能回显不符:期望 0x{:04X},收到 0x{:04X}").format(
                sub_function, echoed
            )
        )
    return [
        struct.unpack(">H", pdu[index : index + 2])[0]
        for index in range(3, len(pdu), 2)
    ]


def build_read_exception_status_pdu() -> bytes:
    """构造读异常状态请求 PDU(FC07:仅功能码)。

    依据:Modbus 应用协议 V1.1b3 §6.7(印刷页 20)。
    """
    return bytes([ModbusFunction.READ_EXCEPTION_STATUS])


def parse_read_exception_status_response(pdu: bytes) -> int:
    """解析读异常状态响应,返回 1 字节状态(FC07)。

    依据:Modbus 应用协议 V1.1b3 §6.7(印刷页 20):响应 = 功能码 + 1 字节状态。

    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1:
    异常响应不再被误报为坏帧)
    :raises ProtocolFrameError: PDU 非 2 字节或功能码不符
    """
    check_response_exception(pdu, ModbusFunction.READ_EXCEPTION_STATUS)
    if len(pdu) != 2 or pdu[0] != ModbusFunction.READ_EXCEPTION_STATUS:
        raise ProtocolFrameError(
            _("FC07 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    return pdu[1]


def build_report_server_id_pdu() -> bytes:
    """构造报告从站 ID 请求 PDU(FC17:仅功能码)。

    依据:Modbus 应用协议 V1.1b3 §6.13(印刷页 31)。
    """
    return bytes([ModbusFunction.REPORT_SERVER_ID])


def parse_report_server_id_response(pdu: bytes) -> Tuple[int, int, bytes]:
    """解析报告从站 ID 响应(FC17),返回 ``(从站 ID, 运行指示状态, 附加数据)``。

    布局依规范 §6.13(印刷页 31,Report Server ID):功能码(1) + 长度域(1)
    + 从站 ID(设备专有) + 运行指示状态(1,``0x00`` = OFF / ``0xFF`` = ON)
    + 附加数据(设备专有,可为空);长度域须与实收字节严格一致,下限 2。

    **待核**(review-1023 §8.2-4):从站 ID 栏规范标注 device specific、
    未定长度——本实现按"1 字节 ID + 1 字节运行指示 + 余下附加"的常见形态
    解析;≥2 字节 ID 的设备会把第 2 字节错位为运行指示,该形态无手册可依、
    不臆造,待真机核证(已登记真机清单)。

    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: 功能码不符 / 长度域与实收不符 / 长度域低于下限
    """
    check_response_exception(pdu, ModbusFunction.REPORT_SERVER_ID)
    if len(pdu) < 2 or pdu[0] != ModbusFunction.REPORT_SERVER_ID:
        raise ProtocolFrameError(
            _("FC17 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    byte_count = pdu[1]
    if byte_count < MODBUS_SERVER_ID_MIN_BYTE_COUNT or len(pdu) != 2 + byte_count:
        raise ProtocolFrameError(
            _("FC17 响应长度域不符:声明 {}、实收 {}(收到的原始 PDU:{})").format(
                byte_count, len(pdu) - 2, format_hex(pdu)
            )
        )
    return pdu[2], pdu[3], pdu[4:]


def build_get_comm_event_counter_pdu() -> bytes:
    """构造取通信事件计数器请求 PDU(FC11:仅功能码)。

    依据:Modbus 应用协议 V1.1b3 §6.9(印刷页 25)。
    """
    return bytes([ModbusFunction.GET_COMM_EVENT_COUNTER])


def parse_comm_event_counter_pdu(pdu: bytes) -> Tuple[int, int]:
    """解析 FC11 响应,返回 ``(状态字, 事件计数)``。
    依据:Modbus 应用协议 V1.1b3 §6.9(印刷页 25~26):状态字 + 事件计数各 2 字节。
    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: PDU 过短/功能码不符
    """
    check_response_exception(pdu, ModbusFunction.GET_COMM_EVENT_COUNTER)
    if (
        len(pdu) != MODBUS_EVENT_COUNTER_PDU_SIZE
        or pdu[0] != ModbusFunction.GET_COMM_EVENT_COUNTER
    ):
        raise ProtocolFrameError(
            _("FC11 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    status, count = struct.unpack(">HH", pdu[1:5])
    return status, count


def build_get_comm_event_log_pdu() -> bytes:
    """构造取通信事件日志请求 PDU(FC12:仅功能码)。

    依据:Modbus 应用协议 V1.1b3 §6.10(印刷页 26~27)。
    """
    return bytes([ModbusFunction.GET_COMM_EVENT_LOG])


def parse_comm_event_log_pdu(pdu: bytes) -> Dict[str, object]:
    """解析 FC12 响应。

    布局:功能码(1) + byte count(1) + 状态字(2) + 事件计数(2) + 报文计数(2)
    + 事件字节(byte count − 6)。返回 ``{status, event_count, message_count, events}``。

    规范 §6.10(印刷页 26-27):事件字节字段为 **0~64 字节**,即 byte count
    上限 ``0x46``(= 64 + 6);越界按坏帧拒绝。

    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: PDU 过短/功能码不符/长度与 byte count 不一致/byte count 超上限
    """
    check_response_exception(pdu, ModbusFunction.GET_COMM_EVENT_LOG)
    if len(pdu) < 2 or pdu[0] != ModbusFunction.GET_COMM_EVENT_LOG:
        raise ProtocolFrameError(
            _("FC12 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    byte_count = pdu[1]
    if byte_count < 6 or byte_count > MODBUS_EVENT_LOG_MAX_BYTE_COUNT:
        raise ProtocolFrameError(
            _("FC12 byte count 越界(规范 6~{}):{}").format(
                MODBUS_EVENT_LOG_MAX_BYTE_COUNT, byte_count
            )
        )
    if len(pdu) != 2 + byte_count:
        raise ProtocolFrameError(
            _("FC12 byte count 与长度不符:声明 {},实际 {}").format(
                byte_count, len(pdu) - 2
            )
        )
    status, event_count, message_count = struct.unpack(">HHH", pdu[2:8])
    return {
        "status": status,
        "event_count": event_count,
        "message_count": message_count,
        "events": bytes(pdu[8 : 2 + byte_count]),
    }


def _check_file_record_fields(
    file_number: int, record_number: int, record_length: int
) -> None:
    """文件记录子请求字段范围校验(内部函数)。

    依据:Modbus 应用协议 V1.1b3 §6.14(印刷页 32)/ §6.15(印刷页 34):
    File number 1~0xFFFF、Record number 0~0x270F(0~9999)、Record length
    为 2 字节寄存器数——规范对单条记录长度**未定上限**,上限由聚合
    byte count(FC20 ≤ 0xF5 / FC21 ≤ 0xFB)与响应 PDU 容量收口;旧实现
    硬编码 0x7D 系 FC03/04/23 语境的 125 寄存器误挪(review-1023 §8.2-2,
    实测 123~125 在任何路径都不可达),已删。

    :raises ValueError: 字段越界
    """
    if not 0x0001 <= file_number <= 0xFFFF:
        raise ValueError(_("文件号超出范围 1~65535:{}").format(file_number))
    if not 0x0000 <= record_number <= 0x270F:
        raise ValueError(_("记录号超出范围 0~9999:{}").format(record_number))
    if not 1 <= record_length <= 0xFFFF:
        # 2 字节字段宽守卫(防 struct 越界);真实上限由聚合校验收口
        raise ValueError(_("记录长度必须为 1~65535:{}").format(record_length))


def build_read_file_record_pdu(requests: "Sequence[Tuple[int, int, int]]") -> bytes:
    """构造读文件记录请求 PDU(FC20)。
    依据:Modbus 应用协议 V1.1b3 §6.14(印刷页 32~33):子请求 = 引用类型(1)+文件号(2)    +记录号(2)+记录长度(2);byte count 0x07~0xF5、子请求 ≤ 35    (= 0xF5 // 7,p.32)。
    :param requests: ``(文件号, 起始记录号, 记录长度[寄存器数])`` 序列
    :raises ValueError: 无子请求/超上限/字段越界
    """
    if not requests:
        raise ValueError(_("FC20 至少需要一个文件记录子请求"))
    if len(requests) > MODBUS_MAX_FILE_RECORDS:
        raise ValueError(
            _("FC20 子请求条数超出上限 {}:{}").format(
                MODBUS_MAX_FILE_RECORDS, len(requests)
            )
        )
    sub = bytearray()
    for file_number, record_number, record_length in requests:
        _check_file_record_fields(file_number, record_number, record_length)
        sub += struct.pack(
            ">BHHH",
            MODBUS_FILE_REFERENCE_TYPE,
            file_number,
            record_number,
            record_length,
        )
    if not 0x07 <= len(sub) <= MODBUS_MAX_READ_FILE_BYTES:
        raise ValueError(
            _("FC20 byte count 超出范围 0x07~0x{:02X}:{}").format(
                MODBUS_MAX_READ_FILE_BYTES, len(sub)
            )
        )
    # 规范 §6.14:聚合响应不得超过 MODBUS PDU 253 字节(响应 = 功能码 1 +
    # byte count 1 + 数据 ≤0xF5)。超限请求设备只能回异常码,入参期拦截。
    response_bytes = 2 + sum(2 + 2 * record_length for _, _, record_length in requests)
    if response_bytes > MODBUS_MAX_READ_FILE_BYTES + 2:
        raise ValueError(
            _("FC20 响应总长超出 PDU 上限:{} > {} 字节,请拆分请求").format(
                response_bytes, MODBUS_MAX_READ_FILE_BYTES + 2
            )
        )
    return bytes([ModbusFunction.READ_FILE_RECORD, len(sub)]) + bytes(sub)


def _read_file_record_response_length(request_pdu: bytes) -> int:
    """按 FC20 请求推算响应 PDU 字节数(内部函数)。
    布局依据:Modbus 应用协议 V1.1b3 §6.14 印刷页 32~33。
    每条子响应 = File resp. length(1) + 引用类型(1) + 数据(2×记录长度),
    其中 File resp. length = 1 + 2×记录长度;响应 PDU = 功能码(1) + 数据长(1)
    + Σ(2 + 2×记录长度)。
    """
    if len(request_pdu) < 2:
        raise ProtocolFrameError(_("FC20 请求 PDU 过短"))
    byte_count = request_pdu[1]
    sub = request_pdu[2 : 2 + byte_count]
    if len(sub) != byte_count or byte_count % 7 != 0:
        raise ProtocolFrameError(_("FC20 请求 byte count 非法:{}").format(byte_count))
    total = 0
    for index in range(0, byte_count, 7):
        length = struct.unpack(">H", sub[index + 5 : index + 7])[0]
        total += 2 + 2 * length
    return 2 + total


def parse_read_file_record_response(
    pdu: bytes, requests: "Sequence[Tuple[int, int, int]]"
) -> List[List[int]]:
    """解析 FC20 响应,按子请求顺序返回逐条寄存器列表。
    依据:Modbus 应用协议 V1.1b3 §6.14(印刷页 32~33):子响应 = File resp. length(1,= 1+2×记录长度)    + 引用类型(1,恒 0x06)+ 寄存器数据。
    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: 长度/File resp. length/引用类型不符或有冗余字节
    """
    check_response_exception(pdu, ModbusFunction.READ_FILE_RECORD)
    if len(pdu) < 2 or pdu[0] != ModbusFunction.READ_FILE_RECORD:
        raise ProtocolFrameError(
            _("FC20 响应非法:{}(收到的原始 PDU:{})").format(len(pdu), format_hex(pdu))
        )
    body = pdu[2:]
    if pdu[1] > MODBUS_MAX_READ_FILE_BYTES:
        raise ProtocolFrameError(
            _("FC20 响应数据长越界:0x{:02X} > 0x{:02X}(规范 §6.14 上限)").format(
                pdu[1], MODBUS_MAX_READ_FILE_BYTES
            )
        )
    if len(body) != pdu[1]:
        raise ProtocolFrameError(
            _("FC20 响应数据长与 byte count 不符:声明 {},实际 {}").format(
                pdu[1], len(body)
            )
        )
    result: List[List[int]] = []
    offset = 0
    for _unused, _unused, record_length in requests:
        if offset >= len(body):
            raise ProtocolFrameError(_("FC20 响应子响应不足"))
        file_resp_len = body[offset]
        if file_resp_len != 1 + 2 * record_length:
            raise ProtocolFrameError(
                _("FC20 File resp. length 不符:期望 {},收到 {}").format(
                    1 + 2 * record_length, file_resp_len
                )
            )
        if offset + 1 + file_resp_len > len(body):
            raise ProtocolFrameError(_("FC20 子响应长度超出响应体"))
        if body[offset + 1] != MODBUS_FILE_REFERENCE_TYPE:
            raise ProtocolFrameError(
                _("FC20 引用类型非 0x06:0x{:02X}").format(body[offset + 1])
            )
        chunk = body[offset + 2 : offset + 1 + file_resp_len]
        if len(chunk) != 2 * record_length:
            raise ProtocolFrameError(_("FC20 记录数据长度不足"))
        result.append(
            # 上一句已钉死 len(chunk) == 2 * record_length(必偶),
            # 一次 struct.unpack 替代逐字切片(review-1044)
            list(struct.unpack(">{}H".format(len(chunk) // 2), chunk))
        )
        offset += 1 + file_resp_len
    if offset != len(body):
        raise ProtocolFrameError(_("FC20 响应尾部有冗余字节:偏移 {}").format(offset))
    return result


def build_write_file_record_pdu(
    records: "Sequence[Tuple[int, int, List[int]]]",
) -> bytes:
    """构造写文件记录请求 PDU(FC21)。
    依据:Modbus 应用协议 V1.1b3 §6.15(印刷页 34~35):子请求 = 引用类型+文件号+记录号    +记录长度+寄存器数据;data length 0x09~0xFB(p.34)。
    :param records: ``(文件号, 起始记录号, 寄存器列表)`` 序列
    :raises ValueError: 无子请求/字段越界/寄存器值越界/超 PDU 上限
    """
    if not records:
        raise ValueError(_("FC21 至少需要一个文件记录子请求"))
    sub = bytearray()
    for file_number, record_number, values in records:
        if not values:
            raise ValueError(_("FC21 记录数据不能为空"))
        _check_file_record_fields(file_number, record_number, len(values))
        sub += struct.pack(
            ">BHHH", MODBUS_FILE_REFERENCE_TYPE, file_number, record_number, len(values)
        )
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(
                    _("FC21 寄存器写入值超出范围 0~65535:{}").format(value)
                )
        # 校验遍保留,装配一次 struct.pack(review-1044):校验通过后 >H 序列
        # 拼接与逐值 sub += 逐字节相同,且省去每值一次 pack 调用
        sub += struct.pack(">{}H".format(len(values)), *values)
    if not 0x09 <= len(sub) <= MODBUS_MAX_WRITE_FILE_BYTES:
        raise ValueError(
            _("FC21 请求数据长超出范围 0x09~0x{:02X}:{}").format(
                MODBUS_MAX_WRITE_FILE_BYTES, len(sub)
            )
        )
    return bytes([ModbusFunction.WRITE_FILE_RECORD, len(sub)]) + bytes(sub)


def parse_write_file_record_response(pdu: bytes, request_pdu: bytes) -> None:
    """校验 FC21 写响应(正常响应为请求回显,须逐字节一致)。

    依据:Modbus 应用协议 V1.1b3 §6.15(印刷页 34~35)。

    :raises DeviceError: PLC 返回异常码(与其余解析器同口径,review-1023 §8.2-1)
    :raises ProtocolFrameError: 响应与请求不符
    """
    check_response_exception(pdu, request_pdu[0])
    if pdu != request_pdu:
        raise ProtocolFrameError(
            _("FC21 响应应为请求回显:期望 {}(收到的原始 PDU:{})").format(
                format_hex(request_pdu), format_hex(pdu)
            )
        )


def build_read_fifo_pdu(offset: int) -> bytes:
    """构造读 FIFO 队列请求 PDU(FC 24,规范 §6.18 印刷页 40)。

    :param offset: FIFO 指针所指的保持寄存器 0 基地址
    :return: PDU 字节(功能码 + 地址,大端)
    :raises ValueError: 地址非法
    """
    _check_offset(offset)
    return struct.pack(">BH", ModbusFunction.READ_FIFO_QUEUE, offset)


def parse_read_fifo_response(pdu: bytes) -> List[int]:
    """解析读 FIFO 队列响应 PDU(FC 24,规范 §6.18 印刷页 40~41)。

    响应 = 功能码(1) + 字节计数(2,= 2 + 2×FIFO 数) + FIFO 计数(2)
    + FIFO 值(2×N,大端)。FIFO 计数上限 31。

    :param pdu: 响应 PDU
    :return: FIFO 中按先进先出排列的寄存器原始值列表(空队列返回 ``[]``)
    :raises DeviceError: PLC 返回异常码
    :raises ProtocolFrameError: PDU 过短、字节计数/长度自洽校验失败或 FIFO 数超上限
    """
    check_response_exception(pdu, ModbusFunction.READ_FIFO_QUEUE)
    if len(pdu) < 5:
        raise ProtocolFrameError(
            _("FC24 响应 PDU 长度不足:至少 5 字节,实际 {}(收到的原始数据:{})").format(
                len(pdu), format_hex(pdu)
            )
        )
    byte_count = int.from_bytes(pdu[1:3], "big")
    fifo_count = int.from_bytes(pdu[3:5], "big")
    if fifo_count > MODBUS_MAX_FIFO_REGISTERS:
        raise ProtocolFrameError(
            _("FC24 FIFO 计数超上限 {}:{}(收到的原始数据:{})").format(
                MODBUS_MAX_FIFO_REGISTERS, fifo_count, format_hex(pdu)
            )
        )
    expected_byte_count = 2 + fifo_count * 2
    if byte_count != expected_byte_count or len(pdu) != 3 + byte_count:
        raise ProtocolFrameError(
            _(
                "FC24 响应长度不符:字节计数域 {},期望 {},FIFO 数 {},实际 PDU {} 字节"
                "(收到的原始数据:{})"
            ).format(
                byte_count, expected_byte_count, fifo_count, len(pdu), format_hex(pdu)
            )
        )
    return list(struct.unpack(">{:d}H".format(fifo_count), pdu[5 : 5 + fifo_count * 2]))


def _check_offset(offset: int) -> None:
    """校验 0 基地址偏移(内部函数)。"""
    if not 0 <= offset <= MODBUS_ADDRESS_MAX:
        raise ValueError(
            _("地址偏移超出范围 0~{}:{}").format(MODBUS_ADDRESS_MAX, offset)
        )


def _check_span(offset: int, count: int) -> None:
    """校验起始地址 + 数量不越过地址空间顶端(内部函数)。

    规范的状态图(§6.1 Figure 11 等)把 ``Starting Address + Quantity``
    作为服务端校验项,越界返回异常码 02(ILLEGAL DATA ADDRESS)。客户端
    在组帧期直接拒绝,避免发出必然被拒的请求(如 ``hr65535`` 读 2 字)。
    """
    if offset + count > MODBUS_ADDRESS_MAX + 1:
        raise ValueError(
            _("起始地址 + 数量超出 Modbus 地址空间 0~{}:{}+{}={}").format(
                MODBUS_ADDRESS_MAX, offset, count, offset + count
            )
        )
