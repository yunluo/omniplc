"""Modbus 客户端:基类 + TCP/RTU 两个走线实现。

类继承::

    BaseClient
    └── ModbusBaseClient          寄存器级公共逻辑(字序/类型分发)
        ├── ModbusTcpClient       MBAP over TCP(默认端口 502)
        └── ModbusRtuClient       站号+PDU+CRC16 over 串口

地址语法见 :mod:`omniplc.modbus.address`。
"""
from __future__ import annotations

from abc import abstractmethod
from typing import List, Optional, Union

from . import codec
from .address import ModbusAddress, ModbusArea, parse_address
from .. import convert
from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MODBUS_EXCEPTION_FLAG,
    MODBUS_STATION_MAX,
    MODBUS_STATION_MIN,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
)
from ..core.errors import ProtocolFrameError
from ..core.validation import (
    check_int16,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ..transport import BaseTransport, SerialConfig, SerialTransport, TcpTransport
from ..types import DataType, SerialParity, WordOrder, PrimitiveValue


class ModbusBaseClient(BaseClient):
    """Modbus 客户端基类:类型分发、字序处理与参数校验。

    走线子类只需实现 :meth:`_create_transport`(传输挂载)与
    :meth:`_transact`(帧装拆:MBAP 或 RTU)。

    广播语义:RTU 站号 0 为广播地址,设备不回包——写操作发送后
    不等响应直接成功,读操作直接拒绝;TCP 无广播概念(Unit ID 为
    路由字段),站号 0 照常收发。
    """

    _BROADCAST_WITHOUT_RESPONSE: bool = False
    """走线是否具备广播语义(RTU 为 True:站号 0 写不等响应)。"""

    def __init__(self) -> None:
        """初始化 Modbus 公共配置(字序默认 ABCD,站号默认 1)。"""
        super().__init__()
        self._word_order = WordOrder.ABCD
        self._station = MODBUS_DEFAULT_STATION
        self._transaction_id = 0

    @property
    def station(self) -> int:
        """Modbus 站号(0~247,0 为广播,仅用于写)。"""
        return self._station

    @station.setter
    def station(self, value: int) -> None:
        if not MODBUS_STATION_MIN <= value <= MODBUS_STATION_MAX:
            raise ValueError(
                f"站号必须在 {MODBUS_STATION_MIN}~{MODBUS_STATION_MAX} 之间,收到:{value}"
            )
        self._station = int(value)

    @property
    def word_order(self) -> WordOrder:
        """多寄存器值的字序(默认 ABCD 大端,现场可按需改 CDAB 等)。"""
        return self._word_order

    @word_order.setter
    def word_order(self, value: Union[WordOrder, str]) -> None:
        self._word_order = _coerce_word_order(value)

    # ------------------------------------------------------------------
    # 协议原语:按数据类型分发(由走线子类的 _transact 落地)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到位/寄存器读原语。"""
        parsed = _check_address(address, data_type)
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            registers = self._read_registers(parsed, 1)
            raw = registers[0].to_bytes(2, "big")
            if data_type is DataType.SHORT:
                return convert.bytes_to_short(raw)
            return convert.bytes_to_ushort(raw)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            registers = self._read_registers(parsed, 2)
            return _decode_32bit(registers, data_type, self._word_order)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            registers = self._read_registers(parsed, 4)
            return _decode_64bit(registers, data_type, self._word_order)
        raise ValueError(f"Modbus 不支持的数据类型:{data_type}")

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到位/寄存器写原语。"""
        parsed = _check_address(address, data_type)
        if data_type is DataType.BOOL:
            self._write_bool_impl(parsed, require_bool(value))
            return
        if data_type is DataType.SHORT:
            self._write_single_register(parsed, check_int16(value))
            return
        if data_type is DataType.USHORT:
            self._write_single_register(parsed, check_uint16(value))
            return
        if data_type in (DataType.INT, DataType.UINT):
            registers = _encode_32bit(value, data_type, self._word_order)
            self._write_registers_impl(parsed, registers)
            return
        if data_type in (DataType.LONG, DataType.ULONG):
            registers = _encode_64bit(value, data_type, self._word_order)
            self._write_registers_impl(parsed, registers)
            return
        if data_type is DataType.FLOAT:
            registers = list(convert.float32_to_registers(require_float(value), self._word_order))
            self._write_registers_impl(parsed, registers)
            return
        if data_type is DataType.DOUBLE:
            registers = list(convert.float64_to_registers(require_float(value), self._word_order))
            self._write_registers_impl(parsed, registers)
            return
        raise ValueError(f"Modbus 不支持的数据类型:{data_type}")

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """从寄存器区读字符串:连续寄存器 → 按大端拼字节 → 解码。"""
        parsed = parse_address(address)
        if parsed.area not in (ModbusArea.HOLDING_REGISTER, ModbusArea.INPUT_REGISTER):
            raise ValueError(f"字符串只能从寄存器区域(hr/ir)读取,收到:{address!r}")
        registers = self._read_registers(parsed, (length + 1) // 2)
        data = b"".join(reg.to_bytes(2, "big") for reg in registers)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """向寄存器区写字符串:编码 → 补齐偶数字节 → 按大端拆寄存器。"""
        parsed = parse_address(address)
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            raise ValueError(f"字符串只能写入保持寄存器区域(hr),收到:{address!r}")
        raw = convert.encode_string(value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding)
        registers = [
            int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)
        ]
        self._write_registers_impl(parsed, registers)
        return value

    # ------------------------------------------------------------------
    # 位与寄存器原语(基于 PDU 编解码 + 走线事务)
    # ------------------------------------------------------------------

    def _read_bool_impl(self, parsed: ModbusAddress) -> bool:
        """读取一个布尔量:线圈/离散输入走位功能码,寄存器走位提取。"""
        if parsed.area in (ModbusArea.COIL, ModbusArea.DISCRETE_INPUT):
            bits = self._read_bits(parsed, 1)
            return bits[0]
        registers = self._read_registers(parsed, 1)
        return convert.get_bit(registers[0], parsed.bit or 0)

    def _read_bits(self, parsed: ModbusAddress, count: int) -> List[bool]:
        """读取连续位(FC 01/02)。"""
        self._reject_broadcast_read()
        pdu = codec.build_read_pdu(parsed.read_function_code, parsed.offset, count)
        response = self._transact(pdu)
        raw_bits = codec.parse_read_response(response, parsed.read_function_code, count)
        return [bool(raw) for raw in raw_bits]

    def _read_registers(self, parsed: ModbusAddress, count: int) -> List[int]:
        """读取连续寄存器(FC 03/04),返回 0~65535 原始值列表。"""
        self._reject_broadcast_read()
        pdu = codec.build_read_pdu(parsed.read_function_code, parsed.offset, count)
        response = self._transact(pdu)
        return codec.parse_read_response(response, parsed.read_function_code, count)

    def _write_bool_impl(self, parsed: ModbusAddress, value: bool) -> None:
        """写一个布尔量:线圈走 FC5;寄存器位走"读-改-写"(同一事务锁内原子完成)。"""
        if parsed.area == ModbusArea.COIL:
            pdu = codec.build_write_single_pdu(parsed.write_single_function_code, parsed.offset, 1 if value else 0)
            self._write_pdu(pdu)
            return
        bit = parsed.bit or 0
        registers = self._read_registers(parsed, 1)
        updated = convert.set_bit(registers[0], bit, value)
        self._write_pdu(
            codec.build_write_single_pdu(parsed.write_single_function_code, parsed.offset, updated)
        )

    def _write_single_register(self, parsed: ModbusAddress, value: int) -> None:
        """写单个保持寄存器(FC 6)。"""
        self._write_pdu(codec.build_write_single_pdu(parsed.write_single_function_code, parsed.offset, value))

    def _write_registers_impl(self, parsed: ModbusAddress, registers: List[int]) -> None:
        """批量写保持寄存器(FC 16)。"""
        self._write_pdu(codec.build_write_multi_pdu(parsed.write_multi_function_code, parsed.offset, registers))

    def write_mask_register(self, address: str, and_mask: int, or_mask: int) -> bool:
        """掩码写保持寄存器(FC 22,设备侧原子 AND/OR 位修改)。

        设备执行 ``新值 = (当前值 AND and_mask) OR (or_mask AND NOT and_mask)``:
        and_mask 置 0 的位被清零,and_mask 置 1 的位取 or_mask 对应位。
        相比"读-改-写"两段事务,单笔事务且设备侧原子,适合位级修改;
        需设备支持 FC 22(部分老设备/网关不支持,失败见 last_error)。

        :param address: 保持寄存器地址,如 ``"hr100"``
        :param and_mask: AND 掩码(0~65535)
        :param or_mask: OR 掩码(0~65535)
        :return: 是否成功
        :raises ValueError: 地址/掩码非法
        """
        parsed = parse_address(address)
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            raise ValueError(f"掩码写只支持保持寄存器区域(hr),收到:{address!r}")
        if parsed.bit is not None:
            raise ValueError(f"掩码写地址不支持位号后缀:{address!r}")
        pdu = codec.build_mask_write_pdu(parsed.offset, int(and_mask), int(or_mask))
        ok, _ = self._execute(
            lambda: codec.parse_mask_write_response(self._transact(pdu), pdu),
            is_write=True,
        )
        return ok

    def _write_pdu(self, pdu: bytes) -> None:
        """发送写 PDU;具备广播语义的走线在广播站号下不等响应(内部方法)。"""
        if self._station == 0 and self._BROADCAST_WITHOUT_RESPONSE:
            self._transact(pdu, expect_response=False)
            return
        self._transact(pdu)

    def _reject_broadcast_read(self) -> None:
        """广播站号禁止读操作(设备不回包,等待只会超时,内部方法)。"""
        if self._station == 0 and self._BROADCAST_WITHOUT_RESPONSE:
            raise ValueError("广播站号(station=0)仅支持写操作,读操作请指定实际站号")

    @abstractmethod
    def _transact(self, pdu: bytes, expect_response: bool = True) -> bytes:
        """发送请求 PDU 并返回响应 PDU(由走线子类实现帧装拆)。

        :param expect_response: False 时不等响应直接返回空字节
            (广播写;仅具备广播语义的走线会收到该标志)。
        """


class ModbusTcpClient(ModbusBaseClient):
    """Modbus TCP 客户端(默认端口 502)。

    :example: ``client = ModbusTcpClient("192.168.0.10", 502, 1)``
    """

    def __init__(self, ip_address: str = "127.0.0.1", port: int = MODBUS_DEFAULT_PORT, station: int = MODBUS_DEFAULT_STATION) -> None:
        """初始化 Modbus TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,默认 502
        :param station: 站号(Unit ID),默认 1
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__()
        self._ip_address = ip_address
        self._port = int(port)
        self.station = station

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    def _transact(self, pdu: bytes, expect_response: bool = True) -> bytes:
        """MBAP 事务:组帧→发送→按长度收→校验事务号/站号→返回 PDU。

        TCP 无广播语义(Unit ID 为路由字段),站号 0 照常等待响应。
        """
        transport = self._require_transport()
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        transport.send(codec.build_mbap(self._transaction_id, self.station, pdu))
        header = transport.recv(MBAP_HEADER_SIZE)
        transaction_id, length = codec.parse_mbap_header(header)
        received_id, station, response_pdu = codec.parse_mbap(header + transport.recv(length - 1))
        if received_id != self._transaction_id:
            raise ProtocolFrameError(
                f"MBAP 事务号不匹配:期望 {self._transaction_id},收到 {received_id}"
            )
        if station != self.station:
            raise ProtocolFrameError(
                f"MBAP 站号不匹配:期望 {self.station},收到 {station}"
            )
        codec.check_response_exception(response_pdu, pdu[0])
        return response_pdu


class ModbusRtuClient(ModbusBaseClient):
    """Modbus RTU 客户端(串口,需要 pyserial)。

    站号 0 为广播地址:写操作发送后不等响应(设备不回包),
    读操作直接拒绝;详见 :class:`ModbusBaseClient`。

    :example::

        client = ModbusRtuClient(station=1)
        client.configure_serial("COM3", baud_rate=9600)
        client.connect()
    """

    _BROADCAST_WITHOUT_RESPONSE = True

    def __init__(self, station: int = MODBUS_DEFAULT_STATION) -> None:
        """初始化 Modbus RTU 客户端。

        :param station: 站号,默认 1
        :raises ValueError: 站号非法
        """
        super().__init__()
        self.station = station
        self._serial_config: Optional[SerialConfig] = None

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(必须在 connect 之前调用)。

        :param port_name: 串口名,如 ``"COM3"``
        :param baud_rate: 波特率,默认 9600
        :param data_bits: 数据位 5~8
        :param stop_bits: 停止位 1/1.5/2
        :param parity: 校验位,推荐 :class:`omniplc.types.SerialParity` 枚举,
            也兼容 ``"N"``/``"E"``/``"O"`` 字符串
        :raises ValueError: 参数非法
        """
        self._serial_config = SerialConfig(
            port_name=port_name,
            baud_rate=baud_rate,
            data_bits=data_bits,
            stop_bits=stop_bits,
            parity=parity,
        )

    def _create_transport(self) -> BaseTransport:
        if self._serial_config is None:
            raise ValueError("请先调用 configure_serial() 配置串口参数")
        return SerialTransport(self._serial_config)

    def _transact(self, pdu: bytes, expect_response: bool = True) -> bytes:
        """RTU 事务:站号+PDU+CRC16 → 发送 → 按功能码推算长度收 → 校验 CRC。

        异常响应(功能码 | 0x80)恒为 2 字节 PDU,读到功能码后先行分支;
        广播写(expect_response=False)发送后不等响应,设备不回包。
        """
        transport = self._require_transport()
        station = self.station
        transport.send(codec.build_rtu_frame(station, pdu))
        if not expect_response:
            return b""
        head = transport.recv(2)
        if head[1] & MODBUS_EXCEPTION_FLAG:
            received_station, response_pdu = codec.parse_rtu_frame(head + transport.recv(3))
        else:
            received_station, response_pdu = codec.parse_rtu_frame(
                head + transport.recv(codec.expected_response_length(pdu) + 1)
            )
        if received_station != station:
            raise ProtocolFrameError(
                f"RTU 站号不匹配:期望 {station},收到 {received_station}"
            )
        codec.check_response_exception(response_pdu, pdu[0])
        return response_pdu


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _coerce_word_order(value: Union[WordOrder, str]) -> WordOrder:
    """把 str/WordOrder 统一转换为 WordOrder。"""
    if isinstance(value, WordOrder):
        return value
    try:
        return WordOrder(str(value).strip().upper())
    except ValueError:
        valid = ", ".join(order.value for order in WordOrder)
        raise ValueError(f"未知字序:{value!r},支持:{valid}")


def _check_address(address: str, data_type: DataType) -> ModbusAddress:
    """地址校验:解析 + 类型与位访问的匹配检查。"""
    parsed = parse_address(address)
    if data_type is not DataType.BOOL and parsed.bit is not None:
        raise ValueError(f"仅布尔类型支持位访问:{address!r}")
    return parsed


def _decode_32bit(registers: List[int], data_type: DataType, word_order: WordOrder) -> PrimitiveValue:
    """按类型解码 2 寄存器值。"""
    if data_type is DataType.INT:
        return convert.registers_to_int32(registers, word_order)
    if data_type is DataType.UINT:
        return convert.registers_to_uint32(registers, word_order)
    return convert.registers_to_float32(registers, word_order)


def _decode_64bit(registers: List[int], data_type: DataType, word_order: WordOrder) -> PrimitiveValue:
    """按类型解码 4 寄存器值。"""
    if data_type is DataType.LONG:
        return int.from_bytes(
            convert.registers_to_canonical(registers, word_order), "big", signed=True
        )
    if data_type is DataType.ULONG:
        return int.from_bytes(
            convert.registers_to_canonical(registers, word_order), "big", signed=False
        )
    return convert.registers_to_float64(registers, word_order)


def _encode_32bit(value: PrimitiveValue, data_type: DataType, word_order: WordOrder) -> List[int]:
    """按类型编码 32 位整数为 2 寄存器。"""
    number = require_int(value)
    if data_type is DataType.INT:
        if not -2147483648 <= number <= 2147483647:
            raise ValueError(f"int 超出 32 位范围:{number}")
        return list(convert.int32_to_registers(number, word_order))
    if not 0 <= number <= 4294967295:
        raise ValueError(f"uint 超出 32 位范围:{number}")
    return list(convert.uint32_to_registers(number, word_order))


def _encode_64bit(value: PrimitiveValue, data_type: DataType, word_order: WordOrder) -> List[int]:
    """按类型编码 64 位整数为 4 寄存器。"""
    number = require_int(value)
    if data_type is DataType.LONG:
        if not -9223372036854775808 <= number <= 9223372036854775807:
            raise ValueError(f"long 超出 64 位范围:{number}")
        return list(convert.int64_to_registers(number, word_order))
    if not 0 <= number <= 18446744073709551615:
        raise ValueError(f"ulong 超出 64 位范围:{number}")
    return list(convert.uint64_to_registers(number, word_order))
