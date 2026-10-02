"""原生 asyncio Modbus TCP 客户端。

:class:`AsyncModbusTcpClient` 是 :class:`~omniplc.modbus.ModbusTcpClient` 的
原生异步孪生:MBAP 组帧、PDU 编解码、地址解析、字序处理**全部复用同步侧的纯
模块**(:mod:`omniplc.modbus.codec` / :mod:`omniplc.modbus.address` 与
``modbus.py`` 里既有的校验/编解码助手),本模块只重写"薄分发层"——把同步的
``_transact`` 与位/寄存器原语换成 ``await`` 版本。

首发能力面:单点读/写(位、寄存器、字符串)+ 类型化方法 + 点位表;
批量(``read_many``/``read_batch``/``write_many``/``write_batch``)与扩展方法
(FC 07/08/11/12/17/20/21/22/23/24、FC 43/14 设备标识)已与同步层同面,
合并算法复用同步侧的纯助手(``_classify`` / ``_coalesce_group`` /
``_encode_value_for_write``),只有事务循环是 ``await`` 版。

:example::

    from omniplc.native import AsyncModbusTcpClient

    async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = await client.read_float("hr0")
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, Union, cast

from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport
from ..core import convert
from ..core.base_client import validate_endpoint
from ..core.constants import (
    MBAP_HEADER_SIZE,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MODBUS_DEVICE_ID_CODE_INDIVIDUAL,
    MODBUS_DEVICE_ID_MAX_PAGES,
    MODBUS_DEVICE_ID_RESERVED_MAX,
    MODBUS_DEVICE_ID_RESERVED_MIN,
    MODBUS_MAX_READ_BITS,
    MODBUS_MAX_READ_REGISTERS,
    MODBUS_MAX_WRITE_BITS,
    MODBUS_MAX_WRITE_REGISTERS,
    MODBUS_STATION_MAX,
    MODBUS_STATION_MIN,
)
from ..core.debug import format_hex
from ..core.errors import DeviceError, ProtocolFrameError
from ..core.validation import (
    check_int16,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ..modbus import codec
from ..modbus.address import ModbusAddress, ModbusArea, parse_address
from ..modbus.modbus import (
    _check_address,
    _check_holding_register,
    _classify,
    _coalesce_group,
    _CoalesceEntry,
    _coerce_word_order,
    _decode_32bit,
    _decode_64bit,
    _device_id_code,
    _device_id_to_text,
    _encode_32bit,
    _encode_64bit,
    _encode_value_for_write,
)
from ..core.types import ByteOrder, DataType, PrimitiveValue, WordOrder
from ..core.i18n import _


class AsyncModbusTcpClient(AsyncBaseClient):
    """Modbus TCP 客户端(MBAP over TCP,默认端口 502)。

    语义与同步 :class:`~omniplc.modbus.ModbusTcpClient` 一致:站号(Unit ID)
    1~247、字序默认 ABCD、位与寄存器区域按地址前缀区分;区别只在 I/O 是原生
    ``asyncio``(属性读取不阻塞事件循环、``await`` 可被真取消)。
    TCP 无广播语义(Unit ID 0 部分网关要求路由),读操作照常收发——
    ``_BROADCAST_WITHOUT_RESPONSE=False`` 与同步基类同口径。

    :example: ``client = AsyncModbusTcpClient("192.168.0.10", 502, 1)``
    """

    _BROADCAST_WITHOUT_RESPONSE: bool = False
    """TCP 站号 0 为路由字段而非广播:读放行(与同步 ModbusTcpClient 一致)。"""

    # 探活:FC08 诊断回显(与同步基类同口径,见 _ping_probe)
    _has_ping = True

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
                _("站号必须在 {}~{} 之间,收到:{}").format(MODBUS_STATION_MIN, MODBUS_STATION_MAX, value)
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
        raise ValueError(_("Modbus 不支持的数据类型:{}").format(data_type))

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """按数据类型分发到位/寄存器写原语。"""
        parsed = _check_address(address, data_type, is_write=True)
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
        raise ValueError(_("Modbus 不支持的数据类型:{}").format(data_type))

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """从寄存器区读字符串:连续寄存器 → 按大端拼字节 → 解码。"""
        parsed = parse_address(address)
        if parsed.area not in (ModbusArea.HOLDING_REGISTER, ModbusArea.INPUT_REGISTER):
            raise ValueError(_("字符串只能从寄存器区域(hr/ir)读取,收到:{!r}").format(address))
        if parsed.bit is not None:
            raise ValueError(_("字符串地址不支持位号后缀:{!r}").format(address))
        registers = await self._read_registers(parsed, (length + 1) // 2)
        data = b"".join(reg.to_bytes(2, "big") for reg in registers)[:length]
        return convert.decode_string(data, encoding)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """向寄存器区写字符串:编码 → 补齐偶数字节 → 按大端拆寄存器。"""
        parsed = parse_address(address)
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            raise ValueError(_("字符串只能写入保持寄存器区域(hr),收到:{!r}").format(address))
        if parsed.bit is not None:
            raise ValueError(_("字符串地址不支持位号后缀:{!r}").format(address))
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
        """写一个布尔量:线圈走 FC5;保持寄存器位走"读-改-写"(同一事务锁内原子完成)。"""
        if parsed.area == ModbusArea.COIL:
            pdu = codec.build_write_single_pdu(
                parsed.write_single_function_code, parsed.offset, 1 if value else 0
            )
            await self._write_pdu(pdu)
            return
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            # 输入寄存器/离散输入不可写:在读-改-写之前拒绝,避免先发一次读
            # (与同步 :meth:`ModbusBaseClient._write_bool_impl` 同口径)
            raise ValueError(
                _("位写只支持线圈(c)与保持寄存器(hr)区域,收到:{!r}").format(
                    parsed.area.value
                )
            )
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

    async def _write_bools_impl(
        self, parsed: ModbusAddress, values: List[bool]
    ) -> None:
        """写连续多个线圈(FC 15,与 :meth:`_write_registers_impl` 的 FC 16 对称)。

        :param parsed: 起始地址(仅线圈区)
        :param values: 连续 ``count`` 个线圈的真值表(LSB first)
        """
        if parsed.area != ModbusArea.COIL:
            raise ValueError(_("FC 15 仅支持线圈区域,收到:{!r}").format(parsed.area.value))
        int_values: List[int] = [1 if flag else 0 for flag in values]
        await self._write_pdu(
            codec.build_write_multi_pdu(
                parsed.write_multi_function_code, parsed.offset, int_values
            )
        )

    # ------------------------------------------------------------------
    # 批量读写:与同步层同一套合并算法(共用纯助手),只把事务循环换成 await
    # ------------------------------------------------------------------

    async def read_many(
        self,
        addresses: Sequence[str],
        data_type: Union[DataType, str],
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """按数据类型批量读:同 (区, 类型) 连续地址合一笔 FC,事务最少化。

        合并口径与失败语义与同步
        :meth:`~omniplc.modbus.ModbusTcpClient.read_many` 逐条一致(共用
        ``_classify`` / ``_coalesce_group`` 两个纯助手,同 (区, 类型) 且偏移
        连续/相邻的条目合为一笔 FC,N 个点压到 K 笔):

        - 任一笔 FC 失败 → 整批失败,返回与 addresses 等长的 ``(False, None)``
        - 地址 / 类型非法 → 同步抛 :class:`ValueError`,不进入事务锁(零字节发送)

        :param addresses: 地址列表(全部使用同一 ``data_type``)
        :param data_type: 数据类型,推荐 :class:`omniplc.types.DataType` 枚举
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表
        :raises ValueError: ``data_type`` 非法 / 任一地址无法解析
        """
        data_type_enum = DataType.coerce(data_type)
        parsed = [
            (_check_address(addr, data_type_enum), data_type_enum)
            for addr in addresses
        ]
        ok, values = await self._execute(lambda: self._coalesce_and_read(parsed))
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    async def read_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str]]],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """混类型批量读:按 (区, 类型) 分组,组内连续地址合一笔 FC。

        Modbus 协议不支持跨 FC 单事务合并,故笔数 K 取决于跨区 / 跨类型 /
        地址空洞数;失败语义与同步 :meth:`~omniplc.modbus.ModbusTcpClient.read_batch`
        一致(任一笔失败即整批 ``(False, None)``,不放出部分值)。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空 / 地址或类型非法
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        parsed: List[Tuple[ModbusAddress, DataType]] = []
        for addr, data_type in items:
            coerced = DataType.coerce(data_type)
            parsed.append((_check_address(addr, coerced), coerced))
        return await self._execute(lambda: self._coalesce_and_read(parsed))

    async def _coalesce_and_read(
        self,
        items: List[Tuple[ModbusAddress, DataType]],
    ) -> List[PrimitiveValue]:
        """批量读核心:分类 → 分组 → 合并 → 逐 chunk 读 + 按原序回填(内部方法)。

        与同步 ``ModbusBaseClient._coalesce_and_read`` 的四步一致(见该处
        表格式说明);差异只有"每 chunk 发一笔 FC"这一步是 ``await``。
        """
        plan: List[_CoalesceEntry] = []
        for item_index, (parsed, dtype) in enumerate(items):
            kind, width = _classify(parsed, dtype)
            plan.append(
                _CoalesceEntry(
                    item_index=item_index,
                    parsed=parsed,
                    dtype=dtype,
                    kind=kind,
                    width=width,
                )
            )

        groups: Dict[Tuple[ModbusArea, str, int, DataType], List[_CoalesceEntry]] = {}
        for entry in plan:
            key = (entry.parsed.area, entry.kind, entry.width, entry.dtype)
            groups.setdefault(key, []).append(entry)

        result: List[Optional[PrimitiveValue]] = [None] * len(items)

        for (area, kind, _width, _dtype), group in groups.items():
            group.sort(key=lambda e: e.parsed.offset)
            max_unit = (
                MODBUS_MAX_READ_BITS if kind == "bit" else MODBUS_MAX_READ_REGISTERS
            )
            for chunk in _coalesce_group(group, kind, max_unit):
                await self._read_and_fill(area, kind, chunk, result)

        return cast(List[PrimitiveValue], result)

    async def _read_and_fill(
        self,
        area: ModbusArea,
        kind: str,
        chunk: List[_CoalesceEntry],
        result: List[Optional[PrimitiveValue]],
    ) -> None:
        """对合并区间执行单笔 FC 读,按各条目 dtype 解码并回填(内部方法)。

        与同步 ``_read_and_fill`` 同一解码映射(位区按 offset 取位;寄存器区
        按 dtype 提位 / ``to_signed`` / 原值 / 2 字 / 4 字,多字类型走字序感知
        的 ``_decode_32bit`` / ``_decode_64bit``)。
        """
        start_offset = chunk[0].parsed.offset
        end_offset = chunk[-1].parsed.offset + chunk[-1].width
        count = end_offset - start_offset

        if kind == "bit":
            probe = ModbusAddress(area=area, offset=start_offset)
            bits = await self._read_bits(probe, count)
            for entry in chunk:
                idx = entry.parsed.offset - start_offset
                result[entry.item_index] = bool(bits[idx])
            return

        probe = ModbusAddress(area=area, offset=start_offset)
        words = await self._read_registers(probe, count)
        for entry in chunk:
            word_idx = entry.parsed.offset - start_offset
            dtype = entry.dtype
            if dtype is DataType.BOOL:
                bit_index = entry.parsed.bit or 0
                result[entry.item_index] = bool(
                    convert.get_bit(words[word_idx], bit_index)
                )
                continue
            if dtype is DataType.SHORT:
                result[entry.item_index] = convert.to_signed(words[word_idx], 16)
                continue
            if dtype is DataType.USHORT:
                result[entry.item_index] = words[word_idx]
                continue
            if dtype in (DataType.INT, DataType.UINT, DataType.FLOAT):
                result[entry.item_index] = _decode_32bit(
                    words[word_idx : word_idx + 2], dtype, self._word_order
                )
                continue
            if dtype in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
                result[entry.item_index] = _decode_64bit(
                    words[word_idx : word_idx + 4], dtype, self._word_order
                )
                continue
            raise ValueError(_("Modbus 不支持的数据类型:{}").format(dtype))

    async def write_many(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> List[bool]:
        """按地址列表批量写:同 (区, 类型) 连续地址合一笔 FC,事务最少化。

        返回与基类一致(``List[bool]``,与 items 顺序对应):同一合并 chunk
        内的条目共享 ok,跨 chunk 各自独立;寄存器位写(``hr0.3``)不入合并,
        走"读-改-写"两段事务。逐 chunk / 逐 RMW 项各自一个事务
        (``is_write=True``,重试取 ``write_retries``,默认 0 防重复写入)。

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: 与 items 顺序对应的 ``bool`` 列表
        :raises ValueError: 地址 / 类型 / 值非法(零字节发送)
        """
        parsed_items: List[Tuple[ModbusAddress, DataType, PrimitiveValue]] = []
        for address, dtype, value in items:
            data_type_enum = DataType.coerce(dtype)
            parsed_items.append(
                (
                    _check_address(address, data_type_enum, is_write=True),
                    data_type_enum,
                    value,
                )
            )
        # 外层不套 _execute:每个 chunk / RMW 项各自成事务,否则外层会把内层
        # 刚记下的失败当成功 _clear_error(),失败原因丢失(与同步层同结构)
        return await self._coalesce_and_write(parsed_items, fail_fast=False)

    async def write_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> Tuple[bool, Optional[List[bool]]]:
        """混类型批量写:整批容错,任一 FC 失败 → ``(False, None)``。

        行为对齐 :meth:`read_batch` 与同步侧同名方法:利用 FC 15/16 写连续
        N 个位/字的协议能力把混类型条目压到 K 笔事务;寄存器位写走读-改-写
        (与同步层同一协议层竞态口径,不引入新限制)。

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: ``(是否成功, 与 items 顺序对应的 ok 列表)``
        :raises ValueError: 列表为空 / 地址 / 类型 / 值非法
        """
        if not items:
            raise ValueError(_("write_batch 至少需要一个 (地址, 数据类型, 值) 项"))
        parsed_items: List[Tuple[ModbusAddress, DataType, PrimitiveValue]] = []
        for address, dtype, value in items:
            data_type_enum = DataType.coerce(dtype)
            parsed_items.append(
                (
                    _check_address(address, data_type_enum, is_write=True),
                    data_type_enum,
                    value,
                )
            )
        ok, results = await self._execute(
            lambda: self._coalesce_and_write(parsed_items, True), is_write=True
        )
        if not ok or results is None:
            return False, None
        if not all(results):
            return False, None
        return True, results

    async def _coalesce_and_write(
        self,
        items: List[Tuple[ModbusAddress, DataType, PrimitiveValue]],
        fail_fast: bool,
    ) -> List[bool]:
        """批量写核心:寄存器位走 RMW,其余按 (区, 类型) 合并(内部方法)。

        与同步 ``ModbusBaseClient._coalesce_and_write`` 同四步(分流 → RMW →
        分组 → 逐 chunk 写 + 回填);``fail_fast`` 分流两种失败语义:

        - ``fail_fast=True``(:meth:`write_batch`):异常直接穿透到外层
          ``_execute`` → 整批 ``(False, None)``
        - ``fail_fast=False``(:meth:`write_many`):每个 RMW 项 / 每个 chunk
          各自一个 ``_execute`` → 该项 / 该 chunk 置 ``False`` 且继续
        """
        rmw_items: List[Tuple[int, ModbusAddress, bool]] = []
        plan: List[Tuple[int, _CoalesceEntry, List[int]]] = []
        for item_index, (parsed, dtype, value) in enumerate(items):
            if dtype is DataType.BOOL and parsed.area in (
                ModbusArea.HOLDING_REGISTER,
                ModbusArea.INPUT_REGISTER,
            ):
                rmw_items.append((item_index, parsed, require_bool(value)))
                continue
            kind, width = _classify(parsed, dtype)
            encoded = _encode_value_for_write(parsed, dtype, value, self._word_order)
            plan.append(
                (
                    item_index,
                    _CoalesceEntry(
                        item_index=item_index,
                        parsed=parsed,
                        dtype=dtype,
                        kind=kind,
                        width=width,
                    ),
                    encoded,
                )
            )

        result: List[Optional[bool]] = [None] * len(items)

        rmw_items.sort(key=lambda t: t[1].offset)
        for item_index, parsed, flag in rmw_items:
            if fail_fast:
                await self._write_bool_impl(parsed, flag)
                result[item_index] = True
            else:
                ok, _unused = await self._execute(
                    lambda: self._write_bool_impl(parsed, flag), is_write=True
                )
                result[item_index] = ok

        groups: Dict[
            Tuple[ModbusArea, str, int, DataType],
            List[Tuple[int, _CoalesceEntry, List[int]]],
        ] = {}
        for item_index, entry, encoded in plan:
            key = (entry.parsed.area, entry.kind, entry.width, entry.dtype)
            groups.setdefault(key, []).append((item_index, entry, encoded))

        for (area, kind, _width, _dtype), group in groups.items():
            group.sort(key=lambda t: t[1].parsed.offset)
            max_unit = (
                MODBUS_MAX_WRITE_BITS
                if kind == "bit"
                else MODBUS_MAX_WRITE_REGISTERS
            )
            for chunk_entries in _coalesce_group([t[1] for t in group], kind, max_unit):
                chunk_items = [item for item in group if item[1] in chunk_entries]
                if fail_fast:
                    await self._write_chunk(area, kind, chunk_items)
                    for item_index, _entry, _area in chunk_items:
                        result[item_index] = True
                else:
                    ok, _unused = await self._execute(
                        lambda: self._write_chunk(area, kind, chunk_items),
                        is_write=True,
                    )
                    for item_index, _entry, _area in chunk_items:
                        result[item_index] = ok

        return [bool(flag) for flag in result]

    async def _write_chunk(
        self,
        area: ModbusArea,
        kind: str,
        chunk: List[Tuple[int, _CoalesceEntry, List[int]]],
    ) -> None:
        """把单个合并 chunk 编码成 1 笔 FC 写下去(内部方法,**不捕获异常**)。

        与同步 ``_write_chunk`` 同结构:位 chunk 走 FC 15(空洞按 False 填),
        字 chunk 走 FC 16(空洞按 0 填);设备异常码与组帧期 ``ValueError``
        直接抛出,由调用方的事务边界收口(在 chunk 内吞异常会让事务层误判
        成功并 ``_clear_error()``,失败变成静默 False)。
        """
        if not chunk:
            return
        start_offset = chunk[0][1].parsed.offset
        end_offset = chunk[-1][1].parsed.offset + chunk[-1][1].width
        count = end_offset - start_offset

        if kind == "bit":
            values: List[bool] = [False] * count
            for _unused, entry, encoded in chunk:
                idx = entry.parsed.offset - start_offset
                values[idx] = bool(encoded[0])
            probe = ModbusAddress(area=area, offset=start_offset)
            await self._write_bools_impl(probe, values)
        else:
            words: List[int] = [0] * count
            for _unused, entry, encoded in chunk:
                idx = entry.parsed.offset - start_offset
                for offset_within, word in enumerate(encoded):
                    words[idx + offset_within] = word
            probe = ModbusAddress(area=area, offset=start_offset)
            await self._write_registers_impl(probe, words)

    # ------------------------------------------------------------------
    # 扩展功能码:掩码写 / 读写复合 / 诊断 / 文件记录 / FIFO / 设备标识
    # ------------------------------------------------------------------

    async def write_mask_register(
        self,
        address: str,
        and_mask: int,
        or_mask: int,
        byte_order: Union[ByteOrder, str] = "big",
    ) -> bool:
        """掩码写保持寄存器(FC 22,设备侧原子 AND/OR 位修改)。

        设备执行 ``新值 = (当前值 AND and_mask) OR (or_mask AND NOT and_mask)``。

        :param address: 保持寄存器地址,如 ``"hr100"``
        :param and_mask: AND 掩码(0~65535)
        :param or_mask: OR 掩码(0~65535)
        :param byte_order: 掩码字节序(``"big"``/``"little"`` 或
            :class:`~omniplc.types.ByteOrder`)
        :return: 是否成功
        :raises ValueError: 地址/掩码/字节序非法
        """
        parsed = parse_address(address)
        if parsed.area != ModbusArea.HOLDING_REGISTER:
            raise ValueError(_("掩码写只支持保持寄存器区域(hr),收到:{!r}").format(address))
        if parsed.bit is not None:
            raise ValueError(_("掩码写地址不支持位号后缀:{!r}").format(address))
        order = byte_order.value if isinstance(byte_order, ByteOrder) else str(byte_order)
        pdu = codec.build_mask_write_pdu(
            parsed.offset, require_int(and_mask), require_int(or_mask), order
        )

        async def operation() -> None:
            codec.parse_mask_write_response(await self._transact(pdu), pdu)

        ok, _unused = await self._execute(operation, is_write=True)
        return ok

    async def read_write_registers(
        self,
        read_address: str,
        read_count: int,
        write_address: str,
        values: Sequence[int],
    ) -> Tuple[bool, Optional[List[int]]]:
        """单事务「先写后读」多寄存器(FC 23,规范 §6.17)。

        读到的值是**写入生效后**的值("写控制字 + 读状态字"同一事务内完成,
        无中间态插入)。仅保持寄存器区域;读 1~125、写 1~121。

        :param read_address: 读起始地址,如 ``"hr0"``
        :param read_count: 读寄存器数量(1~125)
        :param write_address: 写起始地址,如 ``"hr100"``
        :param values: 写入的寄存器原始值序列(1~121 个,每个 0~65535)
        :return: ``(是否成功, 读到的寄存器原始值列表)``
        :raises ValueError: 地址非保持寄存器 / 带位号后缀 / 数量或值非法
        """
        read_parsed = _check_holding_register(read_address, "FC23 读地址")
        write_parsed = _check_holding_register(write_address, "FC23 写地址")
        self._reject_broadcast_read()
        data = [require_int(value) for value in values]
        pdu = codec.build_read_write_registers_pdu(
            read_parsed.offset, require_int(read_count), write_parsed.offset, data
        )

        async def operation() -> List[int]:
            try:
                return codec.parse_read_write_registers_response(
                    await self._transact(pdu), int(read_count)
                )
            except DeviceError as exc:
                if exc.code == 0x02:
                    # 与同步侧同口径(V1.1b3 §6.17):异常码 02 优先含义是
                    # "地址越界",跨段网关只是次要可能(第九轮 R9-4)
                    raise DeviceError(
                        _("{};若读/写地址均在设备合法范围内,可能是部分网关"
                        "不支持跨段 FC23,可改用 write_many + read_many").format(exc),
                        exc.code,
                    ) from exc
                raise

        return await self._execute(operation, is_write=True)

    async def read_device_id(
        self, level: Union[str, int] = "basic"
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读设备标识(FC 43 / MEI 0x0E,规范 §6.21),自动翻页至收敛。

        :param level: ``"basic"`` / ``"regular"`` / ``"extended"``;也接受
            整数 1/2/3
        :return: ``(是否成功, {对象名: 文本值})``;厂商私有对象用
            ``object_0xNN``。失败为 ``(False, None)``
        :raises ValueError: ``level`` 非法
        """
        code = _device_id_code(level)
        self._reject_broadcast_read()
        return await self._execute(lambda: self._read_device_id_pages(code))

    async def read_device_object(self, object_id: int) -> Tuple[bool, Optional[bytes]]:
        """读单个设备标识对象(FC 43/14 个体访问,读取码 04)。

        :param object_id: 对象号(0x00~0x06 标准、0x80~0xFF 厂商私有;
            0x07~0x7F 为规范保留值,拒绝)
        :return: ``(是否成功, 对象原始字节)``;失败为 ``(False, None)``
        :raises ValueError: 对象号非法(越界或落在保留区间)
        """
        codec.check_device_id_object(int(object_id))
        self._reject_broadcast_read()
        return await self._execute(
            lambda: self._read_device_object_once(int(object_id))
        )

    async def read_exception_status(self) -> Tuple[bool, Optional[int]]:
        """读异常状态(FC07,规范 §6.7):返回设备 1 字节异常状态字。

        状态位含义由设备厂商定义(串行子站常用作 8 位离散状态打包)。不支持
        FC07 的设备以异常码 01 应答(失败见 ``last_error``)。

        :return: ``(是否成功, 状态字节 0~255)``;失败为 ``(False, None)``
        """
        self._reject_broadcast_read()

        async def operation() -> int:
            return codec.parse_read_exception_status_response(
                await self._transact(codec.build_read_exception_status_pdu())
            )

        return await self._execute(operation)

    async def report_server_id(self) -> Tuple[bool, Optional[Tuple[int, int, bytes]]]:
        """报告从站 ID(FC17,规范 §6.13,印刷页 31)。

        :return: ``(是否成功, (从站 ID, 运行指示状态, 附加数据))``;
            失败为 ``(False, None)``
        """
        self._reject_broadcast_read()

        async def operation() -> Tuple[int, int, bytes]:
            return codec.parse_report_server_id_response(
                await self._transact(codec.build_report_server_id_pdu())
            )

        return await self._execute(operation)

    async def diagnostics(
        self, sub_function: int, data: int = 0x0000
    ) -> Tuple[bool, Optional[int]]:
        """诊断(FC08,规范 §6.8):返回设备回显的 2 字节数据域。

        :param sub_function: 子功能码(0~65535);``0x000A`` 清计数器为写语义
        :param data: 数据域(0~65535)
        :return: ``(是否成功, 2 字节数据值)``;失败为 ``(False, None)``
        :raises ValueError: 广播站号下的只读诊断子功能(仅 0x000A 允许广播)
        """
        if int(sub_function) != 0x000A:
            self._reject_broadcast_read()
        pdu = codec.build_diagnostics_pdu(int(sub_function), int(data))

        async def operation() -> int:
            return codec.parse_diagnostics_response(
                await self._transact(pdu), int(sub_function)
            )

        return await self._execute(operation, is_write=int(sub_function) == 0x000A)

    async def _ping_probe(self) -> int:
        """探活探测命令:FC08 子功能 0x0000 回显查询(内部方法,镜像同步侧)。"""
        self._reject_broadcast_read()
        return codec.parse_diagnostics_response(
            await self._transact(codec.build_diagnostics_pdu(0x0000, 0x0000)), 0x0000
        )

    async def get_comm_event_counter(self) -> Tuple[bool, Optional[int]]:
        """取通信事件计数器(FC11,规范 §6.9)。

        状态字非 0(``0xFFFF`` = 设备忙)抛 :class:`DeviceError`(设备侧条件,
        不断线)。
        """
        self._reject_broadcast_read()
        pdu = codec.build_get_comm_event_counter_pdu()

        async def operation() -> int:
            status, count = codec.parse_comm_event_counter_pdu(
                await self._transact(pdu)
            )
            if status != 0:
                raise DeviceError(
                    _("FC11 状态字非就绪:0x{:04X}").format(status), int(status)
                )
            return count

        return await self._execute(operation)

    async def get_comm_event_log(self) -> Tuple[bool, Optional[Dict[str, object]]]:
        """取通信事件日志(FC12,规范 §6.10)。

        :return: ``(是否成功, {status, event_count, message_count, events})``
        """
        self._reject_broadcast_read()
        pdu = codec.build_get_comm_event_log_pdu()

        async def operation() -> Dict[str, object]:
            return codec.parse_comm_event_log_pdu(await self._transact(pdu))

        return await self._execute(operation)

    async def read_file_record(
        self, requests: Sequence[Tuple[int, int, int]]
    ) -> Tuple[bool, Optional[List[List[int]]]]:
        """读文件记录(FC20,规范 §6.14)。

        :param requests: ``(文件号, 起始记录号, 记录长度[寄存器数])`` 序列
        :return: ``(是否成功, [[寄存器值...], ...])``,与 requests 顺序对应
        :raises ValueError: 入参非法
        """
        trimmed = [
            (require_int(file), require_int(record), require_int(length))
            for file, record, length in requests
        ]
        self._reject_broadcast_read()
        pdu = codec.build_read_file_record_pdu(trimmed)

        async def operation() -> List[List[int]]:
            return codec.parse_read_file_record_response(
                await self._transact(pdu), trimmed
            )

        return await self._execute(operation)

    async def write_file_record(
        self, records: Sequence[Tuple[int, int, Sequence[int]]]
    ) -> bool:
        """写文件记录(FC21,规范 §6.15);正常响应须为请求回显。

        :param records: ``(文件号, 起始记录号, 寄存器值序列[0~65535])`` 序列
        :return: 是否成功
        :raises ValueError: 入参非法
        """
        trimmed = [
            (require_int(file), require_int(record), [require_int(value) for value in values])
            for file, record, values in records
        ]
        pdu = codec.build_write_file_record_pdu(trimmed)

        async def operation() -> None:
            codec.parse_write_file_record_response(await self._transact(pdu), pdu)

        ok, _unused = await self._execute(operation, is_write=True)
        return ok

    async def read_fifo_queue(self, address: str) -> Tuple[bool, Optional[List[int]]]:
        """读 FIFO 队列(FC24,规范 §6.18),单次最多 31 个寄存器。

        :param address: FIFO 指针所指的保持寄存器地址,如 ``"hr1000"``
        :return: ``(是否成功, FIFO 寄存器值列表)``;失败为 ``(False, None)``
        :raises ValueError: 地址非保持寄存器 / 带位号后缀
        """
        parsed = _check_holding_register(address, "FC24 地址")
        self._reject_broadcast_read()
        pdu = codec.build_read_fifo_pdu(parsed.offset)

        async def operation() -> List[int]:
            return codec.parse_read_fifo_response(await self._transact(pdu))

        return await self._execute(operation)

    async def _read_device_id_pages(self, code: int) -> Dict[str, str]:
        """按流式读取码循环翻页,合并所有页的对象(内部方法,仅事务锁内)。

        :raises DeviceError: PLC 返回异常码
        :raises ProtocolFrameError: 响应结构非法或翻页不收敛
        """
        collected: Dict[int, bytes] = {}
        object_id = 0
        for _unused in range(MODBUS_DEVICE_ID_MAX_PAGES):
            if not 0 <= object_id <= 0xFF or (
                MODBUS_DEVICE_ID_RESERVED_MIN
                <= object_id
                <= MODBUS_DEVICE_ID_RESERVED_MAX
            ):
                # 设备侧坏指针按 DeviceError 分类(与同步层同口径),
                # 不以 ValueError 逃出 read_device_id
                raise DeviceError(
                    _("设备标识翻页指针非法:设备下发 next_object_id=0x{:02X}"
                    "(越界或落在规范保留区间)").format(object_id),
                    0,
                )
            pdu = codec.build_device_id_pdu(code, object_id)
            parsed = codec.parse_device_id_response(
                await self._transact(pdu), expected_read_code=code
            )
            for item_id, raw in parsed.objects:
                collected.setdefault(item_id, raw)
            if not parsed.more_follows:
                return _device_id_to_text(collected)
            if parsed.next_object_id == object_id:
                raise ProtocolFrameError(
                    _("设备标识翻页不收敛:对象号停在 0x{:02X}").format(object_id)
                )
            object_id = parsed.next_object_id
        raise ProtocolFrameError(
            _("设备标识翻页次数超过上限 {}:疑似设备重复下发同一页").format(
                MODBUS_DEVICE_ID_MAX_PAGES
            )
        )

    async def _read_device_object_once(self, object_id: int) -> bytes:
        """读单个标识对象(读取码 04),返回对象原始字节(内部方法)。

        :raises DeviceError: PLC 返回异常码(对象不存在为异常码 02)
        """
        pdu = codec.build_device_id_pdu(MODBUS_DEVICE_ID_CODE_INDIVIDUAL, object_id)
        parsed = codec.parse_device_id_response(
            await self._transact(pdu),
            expected_read_code=MODBUS_DEVICE_ID_CODE_INDIVIDUAL,
        )
        for item_id, raw in parsed.objects:
            if item_id == object_id:
                return raw
        raise ProtocolFrameError(
            _("设备标识个体访问未返回请求对象:期望 0x{:02X},收到 {}").format(
                object_id, [hex(item) for item, _ in parsed.objects]
            )
        )

    async def _write_pdu(self, pdu: bytes) -> None:
        """发送写 PDU 并校验正常响应回显(内部方法)。

        与同步 :meth:`~omniplc.modbus.ModbusBaseClient._write_pdu` 同口径:
        规范 §6.5/6.6/6.11/6.12 正常响应为请求 PDU 前 5 字节回显,不符按
        坏帧处理(TCP 无广播语义,站号 0 照常等待响应并校验)。
        """
        response = await self._transact(pdu)
        if response:
            codec.parse_write_response(response, pdu)

    def _reject_broadcast_read(self) -> None:
        """广播站号(0)上的读操作拒绝(与同步基类同口径,内部方法)。

        仅具备广播语义的走线(RTU 站号 0 = 广播)拒绝读;TCP 侧站号 0 是
        路由字段(Unit ID 0 部分网关要求),读放行——由 TCP 不覆写
        ``_BROADCAST_WITHOUT_RESPONSE``(False)实现,与同步
        ``ModbusTcpClient`` 一致。
        """
        if self._station == 0 and self._BROADCAST_WITHOUT_RESPONSE:
            raise ValueError(_("广播站号(station=0)仅支持写操作,读操作请指定实际站号"))

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
                _("MBAP 事务号不匹配:期望 {},收到 {}(收到的原始帧:{})").format(
                    sent_id, received_id, format_hex(frame)
                )
            )
        if station != self.station:
            raise ProtocolFrameError(
                _("MBAP 站号不匹配:期望 {},收到 {}(收到的原始帧:{})").format(
                    self.station, station, format_hex(frame)
                )
            )
        codec.check_response_exception(response_pdu, pdu[0])
        return response_pdu
