"""原生 asyncio Modbus TCP 客户端。

:class:`AsyncModbusTcpClient` 是 :class:`~omniplc.modbus.ModbusTcpClient` 的
原生异步孪生:MBAP 组帧、PDU 编解码、地址解析、字序处理**全部复用同步侧的纯
模块**(:mod:`omniplc.modbus.codec` / :mod:`omniplc.modbus.address` 与
``modbus.py`` 里既有的校验/编解码助手),本模块只重写"薄分发层"——把同步的
``_transact`` 与位/寄存器原语换成 ``await`` 版本。

首发能力面:单点读/写(位、寄存器、字符串)+ 类型化方法 + 点位表;
批量与扩展方法(FC 15/16 合并写、FC 22 掩码写、FC 23、FC 43/14、批量读)
留后续批次(见 :mod:`omniplc.native` 包文档)。

:example::

    from omniplc.native import AsyncModbusTcpClient

    async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = await client.read_float("hr0")
"""
from __future__ import annotations

from typing import List, Union

from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport
from .. import convert
from ..core.base_client import validate_endpoint
from ..core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MODBUS_STATION_MAX,
    MODBUS_STATION_MIN,
)
from ..core.debug import format_hex
from ..core.errors import ProtocolFrameError
from ..core.validation import check_int16, check_uint16, require_bool, require_float
from ..modbus import codec
from ..modbus.address import ModbusAddress, ModbusArea, parse_address
from ..modbus.modbus import (
    _check_address,
    _coerce_word_order,
    _decode_32bit,
    _decode_64bit,
    _encode_32bit,
    _encode_64bit,
)
from ..types import DataType, PrimitiveValue, WordOrder


class AsyncModbusTcpClient(AsyncBaseClient):
    """Modbus TCP 客户端(MBAP over TCP,默认端口 502)。

    语义与同步 :class:`~omniplc.modbus.ModbusTcpClient` 一致:站号(Unit ID)
    1~247、字序默认 ABCD、位与寄存器区域按地址前缀区分;区别只在 I/O 是原生
    ``asyncio``(属性读取不阻塞事件循环、``await`` 可被真取消)。

    :example: ``client = AsyncModbusTcpClient("192.168.0.10", 502, 1)``
    """

    def __init__(
        self,
        ip_address: str = "127.0.0.1",
        port: int = MODBUS_DEFAULT_PORT,
        station: int = MODBUS_DEFAULT_STATION,
    ) -> None:
        """初始化 Modbus TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,默认 502
        :param station: 站号(Unit ID),默认 1
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._station = self._check_station(station)
        self._word_order = WordOrder.ABCD
        self._transaction_id = 0

    @property
    def station(self) -> int:
        """Modbus 站号(0~247,0 为广播,仅用于写;构造期定,只读)。"""
        return self._station

    @staticmethod
    def _check_station(value: int) -> int:
        """站号范围校验(内部方法)。"""
        if not MODBUS_STATION_MIN <= value <= MODBUS_STATION_MAX:
            raise ValueError(
                f"站号必须在 {MODBUS_STATION_MIN}~{MODBUS_STATION_MAX} 之间,收到:{value}"
            )
        return int(value)

    @property
    def word_order(self) -> WordOrder:
        """多寄存器值的字序(默认 ABCD 大端,现场可按需改 CDAB 等)。"""
        return self._word_order

    @word_order.setter
    def word_order(self, value: Union[WordOrder, str]) -> None:
        self._word_order = _coerce_word_order(value)

    # ------------------------------------------------------------------
    # 协议原语:按数据类型分发(由走线 _transact 落地)
    # ------------------------------------------------------------------

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到位/寄存器读原语。"""
        parsed = _check_address(address, data_type)
        if data_type is DataType.BOOL:
            return await self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            registers = await self._read_registers(parsed, 1)
            raw = registers[0].to_bytes(2, "big")
            if data_type is DataType.SHORT:
                return convert.bytes_to_short(raw)
            return convert.bytes_to_ushort(raw)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            registers = await self._read_registers(parsed, 2)
            return _decode_32bit(registers, data_type, self._word_order)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            registers = await self._read_registers(parsed, 4)
            return _decode_64bit(registers, data_type, self._word_order)
        raise ValueError(f"Modbus 不支持的数据类型:{data_type}")

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """按数据类型分发到位/寄存器写原语。"""
        parsed = _check_address(address, data_type)
        if data_type is DataType.BOOL:
            await self._write_bool_impl(parsed, require_bool(value))
            return
        if data_type is DataType.SHORT:
            await self._write_single_register(parsed, check_int16(value))
            return
        if data_type is DataType.USHORT:
            await self._write_single_register(parsed, check_uint16(value))
            return
        if data_type in (DataType.INT, DataType.UINT):
            registers = _encode_32bit(value, data_type, self._word_order)
            await self._write_registers_impl(parsed, registers)
            return
        if data_type in (DataType.LONG, DataType.ULONG):
            registers = _encode_64bit(value, data_type, self._word_order)
            await self._write_registers_impl(parsed, registers)
            return
        if data_type is DataType.FLOAT:
            registers = list(
                convert.float32_to_registers(require_float(value), self._word_order)
            )
            await self._write_registers_impl(parsed, registers)
            return
        if data_type is DataType.DOUBLE:
            registers = list(
                convert.float64_to_registers(require_float(value), self._word_order)
            )
            await self._write_registers_impl(parsed, registers)
            return
        raise ValueError(f"Modbus 不支持的数据类型:{data_type}")

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """从寄存器区读字符串:连续寄存器 → 按大端拼字节 → 解码。"""
        parsed = parse_address(address)
        if parsed.area not in (ModbusArea.HOLDING_REGISTER, ModbusArea.INPUT_REGISTER):
            raise ValueError(f"字符串只能从寄存器区域(hr/ir)读取,收到:{address!r}")
        registers = await self._read_registers(parsed, (length + 1) // 2)
        data = b"".join(reg.to_bytes(2, "big") for reg in registers)[:length]
        return convert.decode_string(data, encoding)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """向寄存器区写字符串:编码 → 补齐偶数字节 → 按大端拆寄存器。"""
        parsed = parse_address(address)
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            raise ValueError(f"字符串只能写入保持寄存器区域(hr),收到:{address!r}")
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        registers = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        await self._write_registers_impl(parsed, registers)
        return value

    # ------------------------------------------------------------------
    # 位与寄存器原语(基于 PDU 编解码 + 走线事务)
    # ------------------------------------------------------------------

    async def _read_bool_impl(self, parsed: ModbusAddress) -> bool:
        """读取一个布尔量:线圈/离散输入走位功能码,寄存器走位提取。"""
        if parsed.area in (ModbusArea.COIL, ModbusArea.DISCRETE_INPUT):
            bits = await self._read_bits(parsed, 1)
            return bits[0]
        registers = await self._read_registers(parsed, 1)
        return convert.get_bit(registers[0], parsed.bit or 0)

    async def _read_bits(self, parsed: ModbusAddress, count: int) -> List[bool]:
        """读取连续位(FC 01/02)。"""
        self._reject_broadcast_read()
        pdu = codec.build_read_pdu(parsed.read_function_code, parsed.offset, count)
        response = await self._transact(pdu)
        raw_bits = codec.parse_read_response(
            response, parsed.read_function_code, count
        )
        return [bool(raw) for raw in raw_bits]

    async def _read_registers(self, parsed: ModbusAddress, count: int) -> List[int]:
        """读取连续寄存器(FC 03/04),返回 0~65535 原始值列表。"""
        self._reject_broadcast_read()
        pdu = codec.build_read_pdu(parsed.read_function_code, parsed.offset, count)
        response = await self._transact(pdu)
        return codec.parse_read_response(response, parsed.read_function_code, count)

    async def _write_bool_impl(self, parsed: ModbusAddress, value: bool) -> None:
        """写一个布尔量:线圈走 FC5;寄存器位走"读-改-写"(同一事务锁内原子完成)。"""
        if parsed.area == ModbusArea.COIL:
            pdu = codec.build_write_single_pdu(
                parsed.write_single_function_code, parsed.offset, 1 if value else 0
            )
            await self._write_pdu(pdu)
            return
        bit = parsed.bit or 0
        registers = await self._read_registers(parsed, 1)
        updated = convert.set_bit(registers[0], bit, value)
        await self._write_pdu(
            codec.build_write_single_pdu(
                parsed.write_single_function_code, parsed.offset, updated
            )
        )

    async def _write_single_register(self, parsed: ModbusAddress, value: int) -> None:
        """写单个保持寄存器(FC 6)。"""
        await self._write_pdu(
            codec.build_write_single_pdu(
                parsed.write_single_function_code, parsed.offset, value
            )
        )

    async def _write_registers_impl(
        self, parsed: ModbusAddress, registers: List[int]
    ) -> None:
        """批量写保持寄存器(FC 16)。"""
        await self._write_pdu(
            codec.build_write_multi_pdu(
                parsed.write_multi_function_code, parsed.offset, registers
            )
        )

    async def _write_pdu(self, pdu: bytes) -> None:
        """发送写 PDU(内部方法)。TCP 无广播语义,站号 0 照常等待响应。"""
        await self._transact(pdu)

    def _reject_broadcast_read(self) -> None:
        """广播站号(0)上的读操作直接拒绝(与同步基类同口径)。"""
        if self._station == 0:
            raise ValueError("广播站号(station=0)仅支持写操作,读操作请指定实际站号")

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncTcpTransport(self._ip_address, self._port)

    async def _transact(self, pdu: bytes) -> bytes:
        """MBAP 事务:组帧→发送→按长度收→校验事务号/站号→返回 PDU。

        与同步 :meth:`~omniplc.modbus.ModbusTcpClient._transact` 逐字段同序:
        长度域含 Unit ID(故再收 ``length - 1`` 字节),事务号/站号不匹配属
        坏帧,异常文本带收到的原始帧(便于判断是迟到的上一条响应、串口/网关
        错配还是对端语义不符)。
        """
        transport = self._require_transport()
        sent_id = self._bump_id("_transaction_id", 16)
        await transport.send(codec.build_mbap(sent_id, self.station, pdu))
        header = await transport.recv(MBAP_HEADER_SIZE)
        transaction_id, length = codec.parse_mbap_header(header)
        frame = header + await transport.recv(length - 1)
        received_id, station, response_pdu = codec.parse_mbap(frame)
        if received_id != sent_id:
            raise ProtocolFrameError(
                "MBAP 事务号不匹配:期望 {},收到 {}(收到的原始帧:{})".format(
                    sent_id, received_id, format_hex(frame)
                )
            )
        if station != self.station:
            raise ProtocolFrameError(
                "MBAP 站号不匹配:期望 {},收到 {}(收到的原始帧:{})".format(
                    self.station, station, format_hex(frame)
                )
            )
        codec.check_response_exception(response_pdu, pdu[0])
        return response_pdu
