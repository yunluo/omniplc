"""Modbus 报文编解码(纯函数)。

本模块所有函数都是 bytes ↔ 结构的纯函数,不接触 socket,可完全
用 :doc:`黄金报文样本 </tests/golden/README>` 做单元测试。

**当前为骨架**:签名与语义已定,实现将在下一阶段(Modbus 驱动)
完成,调用时抛出 :class:`NotImplementedError`。
"""
from __future__ import annotations

from typing import List, Tuple

from ..core.constants import MBAP_HEADER_SIZE  # noqa: F401  (供后续帧组装使用)


def build_read_pdu(function_code: int, offset: int, count: int) -> bytes:
    """构造读请求 PDU(FC 01/02/03/04)。

    :param function_code: 功能码
    :param offset: 0 基起始地址
    :param count: 读取数量(位或寄存器)
    :return: PDU 字节(功能码 + 起始地址 + 数量)
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def build_write_single_pdu(function_code: int, offset: int, value: int) -> bytes:
    """构造单点写请求 PDU(FC 05 写线圈 / FC 06 写单寄存器)。

    :param function_code: 5 或 6
    :param offset: 0 基地址
    :param value: 线圈(0/1)或寄存器原始值(0~65535)
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def build_write_multi_pdu(function_code: int, offset: int, values: List[int]) -> bytes:
    """构造批量写请求 PDU(FC 15 写多线圈 / FC 16 写多寄存器)。

    :param function_code: 15 或 16
    :param offset: 0 基起始地址
    :param values: 寄存器原始值序列(线圈打包由本函数完成)
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def parse_read_response(pdu: bytes, function_code: int, count: int) -> List[int]:
    """解析读响应 PDU。

    :param pdu: 响应 PDU
    :param function_code: 请求功能码(用于校验与异常响应识别)
    :param count: 请求的数量
    :return: 逐位/逐寄存器的原始值列表
    :raises omniplc.core.errors.DeviceError: PLC 返回异常码
    :raises omniplc.core.errors.ProtocolFrameError: 帧长度/格式不符
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def build_mbap(transaction_id: int, station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 MBAP 帧(Modbus TCP/UDP)。

    :param transaction_id: 事务标识(1~65535,回包校验用)
    :param station: 站号(Unit ID)
    :param pdu: 请求 PDU
    :return: 完整 MBAP 帧
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def parse_mbap(frame: bytes) -> Tuple[int, int, bytes]:
    """解析 MBAP 帧。

    :param frame: 完整帧字节
    :return: ``(transaction_id, station, pdu)``
    :raises omniplc.core.errors.ProtocolFrameError: 帧头/长度不符
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def build_rtu_frame(station: int, pdu: bytes) -> bytes:
    """把 PDU 封装为 RTU 帧:站号 + PDU + CRC16(低字节在前)。

    :param station: 站号
    :param pdu: 请求 PDU
    :return: 完整 RTU 帧
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def parse_rtu_frame(frame: bytes) -> Tuple[int, bytes]:
    """解析 RTU 帧(校验 CRC)。

    :param frame: 完整帧字节
    :return: ``(station, pdu)``
    :raises omniplc.core.errors.ProtocolFrameError: CRC 校验失败
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")


def expected_response_length(function_code: int, count: int) -> int:
    """按请求推算响应字节数(TCP 模式按长度读取用)。

    :param function_code: 请求功能码
    :param count: 请求的数量
    :return: 响应 PDU 的期望字节数
    """
    raise NotImplementedError("Modbus 编解码将在下一阶段实现")
