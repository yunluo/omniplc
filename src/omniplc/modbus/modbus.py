"""Modbus 客户端:基类 + TCP/RTU 两个走线实现。

类继承::

    BaseClient
    └── ModbusBaseClient          寄存器级公共逻辑(字序/类型分发)
        ├── ModbusTcpClient       MBAP over TCP(默认端口 502)
        └── ModbusRtuClient       站号+PDU+CRC16 over 串口(需要 pyserial)

帧格式与功能码语义以《Modbus 通信协议规范》V1.1b3(应用协议)与
V1.02(串行线实现指南)为准;中文资源见
`modbus.cn 规范页 <https://www.modbus.cn/modbus-specifications>`。
地址语法见 :mod:`omniplc.modbus.address`。
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, Union, cast

from . import codec
from .address import ModbusAddress, ModbusArea, parse_address
from .. import convert
from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    MBAP_HEADER_SIZE,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MODBUS_EXCEPTION_FLAG,
    MODBUS_MAX_WRITE_BITS,
    MODBUS_MAX_WRITE_REGISTERS,
    MODBUS_STATION_MAX,
    MODBUS_STATION_MIN,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
    UINT32_MAX,
    UINT64_MAX,
)
from ..core.debug import format_hex
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
    # 批量读取:按 (area, kind, width, dtype) 分组合并连续地址
    # ------------------------------------------------------------------

    def read_many(
        self,
        addresses: Sequence[str],
        data_type: Union[DataType, str],
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """按数据类型批量读:同 (区, 类型) 连续地址合一笔 FC,事务最少化。

        与 :class:`BaseClient` 基类逐点循环相比——同 FC + 同数据类型 +
        偏移连续/相邻的条目合一笔 FC 事务,把 N 个点从 N 笔事务压到 K
        笔(K ≤ N,典型 1 笔);跨区 / 跨类型 / 地址空洞各起一笔。Modbus
        协议本身不支持跨 FC 单事务合并,这是协议上限——比 MC 0406 /
        FINS 0104 / AB 0x0A 的"单事务多区"目标弱一档,但远比基类逐点
        循环高效。

        失败语义:

        - 任一笔 FC 失败 → 整批失败,返回与 addresses 等长的 ``(False, None)`` 列表
        - 地址 / 类型非法 → 同步抛 :class:`ValueError`,不进入事务锁(零字节发送)

        :param addresses: 地址列表(全部使用同一 ``data_type``)
        :param data_type: 数据类型,推荐 :class:`omniplc.types.DataType` 枚举
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表;失败为全
            ``(False, None)``
        :raises ValueError: ``data_type`` 非法 / 任一地址无法解析
        """
        data_type_enum = DataType.coerce(data_type)
        # 入参合法性前置校验:任一地址非法即同步抛出,不进事务
        parsed = [(parse_address(addr), data_type_enum) for addr in addresses]
        ok, values = self._execute(lambda: self._coalesce_and_read(parsed))
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str]]],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """混类型批量读:按 (区, 类型) 分组,组内连续地址合一笔 FC。

        利用 Modbus 单条 FC 01/02/03/04 可读连续 N 个位/字的协议能力,
        把 N 个混类型条目压缩到 K 笔 FC 事务(K ≤ N),与 MC 0406 / FINS
        0104 / AB 0x0A / OPC-UA UA Read 的"单事务多条目"目标对齐。
        Modbus 协议本身**不支持跨 FC 单事务合并**,所以 K 取决于跨区 /
        跨类型 / 地址空洞数(同 (区, 类型) 且连续地址→合一笔)。

        失败语义:

        - 任一笔 FC 失败 → ``(False, None)``(整批失败,不放出部分值)
        - 空列表 / 任意地址非法 / 类型非法 → 同步 :class:`ValueError`,
          不进入事务锁(零字节发送)

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``;失败为
            ``(False, None)``
        :raises ValueError: 列表为空 / 地址或类型非法
        """
        if not items:
            raise ValueError("read_batch 至少需要一个 (地址, 数据类型) 项")
        parsed = [(parse_address(addr), DataType.coerce(dt)) for addr, dt in items]
        return self._execute(lambda: self._coalesce_and_read(parsed))

    def _coalesce_and_read(
        self,
        items: List[Tuple["ModbusAddress", DataType]],
    ) -> List[PrimitiveValue]:
        """批量读核心算法(内部方法):分组合并 → 读 → 切片回填。

        算法分四步:

        1. **分类**:每个 ``(parsed, dtype)`` 按 :func:`_classify` 得到
           ``(kind, width)``;同组内 ``width`` 统一(决定 FC 与单条 PDU
           宽度)。
        2. **分组**:key = ``(area, kind, width, dtype)`` —— 决定走哪个
           FC(01/02/03/04)、读多少个字 / 位、怎么解码。
        3. **合并**:每组内按 offset 升序,同 offset(同字 / 同位)与相邻
           offset(gap = 0)合,留空洞立即切块;合并后单 chunk 范围超
           FC 上限(位 2000 / 字 125)在连续区内二次切片。
        4. **读 + 回填**:每个 chunk 发 1 笔 FC,按条目各自的 dtype 解码,
           用 :attr:`_CoalesceEntry.item_index` 写回入参原位。

        5 类分组(每类 FC 与字宽统一):

        =====  ========================  =========  =========
        组      区域 × 数据类型            kind       width
        =====  ========================  =========  =========
        A      线圈/离散 × BOOL           ``bit``    1 bit
        B      寄存器 × BOOL              ``word``   1 word
        C      寄存器 × SHORT/USHORT      ``word``   1 word
        D      寄存器 × INT/UINT/FLOAT    ``word``   2 words
        E      寄存器 × LONG/ULONG/DOUBLE ``word``   4 words
        =====  ========================  =========  =========
        """
        # 1) 分类:每个条目登记到 _CoalesceEntry,记录原序索引
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

        # 2) 分组:area × kind × width × dtype 决定 FC 与字宽
        groups: Dict[Tuple[ModbusArea, str, int, DataType], List[_CoalesceEntry]] = {}
        for entry in plan:
            key = (entry.parsed.area, entry.kind, entry.width, entry.dtype)
            groups.setdefault(key, []).append(entry)

        # 用 None 占位便于按 item_index 回填;末次断言全填
        result: List[Optional[PrimitiveValue]] = [None] * len(items)

        # 3) 每组:排序 → 合并 → 逐 chunk 读 + 切片回填
        for (area, kind, _width, _dtype), group in groups.items():
            group.sort(key=lambda e: e.parsed.offset)
            max_unit = 2000 if kind == "bit" else 125
            chunks = _coalesce_group(group, kind, max_unit)
            for chunk in chunks:
                _read_and_fill(self, area, kind, chunk, result)

        return cast(List[PrimitiveValue], result)

    # ------------------------------------------------------------------
    # 批量写入:与读侧对称的"按 (area, kind, width, dtype) 分组合并连续地址"
    # ------------------------------------------------------------------

    def write_many(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> List[bool]:
        """按地址列表批量写:同 (区, 类型) 连续地址合一笔 FC,事务最少化。

        与 :class:`BaseClient` 基类逐点循环相比——同 FC + 同数据类型 +
        偏移连续/相邻的条目合一笔 FC 事务(FC 15 多线圈 / FC 16 多寄存
        器),把 N 个点从 N 笔事务压到 K 笔(K ≤ N,典型 1 笔)。Modbus
        协议本身不支持跨 FC 单事务合并,这是协议上限。

        返回与基类一致:`List[bool]`,与 items 顺序对应——同一合并
        chunk 内的条目共享 ok(全部成功或全部失败),跨 chunk 各自独立。
        寄存器位写(``hr0.3`` 这类位号后缀)**不入合并**,走"读-改-写"
        两段事务(FC 03 + FC 06),与基类逐点行为一致。

        失败语义:

        - 任一笔 FC 失败 → 该 chunk 内所有条目 ok 为 ``False``(对应槽
          位 ``False``);其他 chunk 各自独立
        - 地址 / 类型 / 值非法 → 同步抛 :class:`ValueError`,不进入
          事务锁(零字节发送)

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: 与 items 顺序对应的 ``bool`` 列表;成功 ``True``
        """
        # 1) 入参合法性前置校验:任一非法即同步抛出,不进事务
        parsed_items: List[Tuple[ModbusAddress, DataType, PrimitiveValue]] = []
        for address, dtype, value in items:
            data_type_enum = DataType.coerce(dtype)
            parsed_items.append(
                (_check_address(address, data_type_enum), data_type_enum, value)
            )
        ok, ok_list = self._execute(lambda: self._coalesce_and_write(parsed_items))
        if not ok or ok_list is None:
            return [False] * len(items)
        return ok_list

    def write_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> Tuple[bool, Optional[List[bool]]]:
        """混类型批量写:整批容错,任一 FC 失败 → ``(False, None)``。

        行为对齐 :meth:`read_batch` 与 MX Component ``write_batch``:
        利用 Modbus 单条 FC 15/16 可写连续 N 个位/字的协议能力,把 N 个
        混类型条目压缩到 K 笔 FC 事务(K ≤ N);Modbus 协议不支持跨 FC
        合并,K 取决于跨区 / 跨类型 / 地址空洞数。

        寄存器位写(``hr0.3`` 这类位号后缀)**不入合并**:走"读-改-写"
        两段事务;同一批次内若 ``hr0.3`` 与 ``hr0`` 同时写入,后者
        FC 16 会清零前者的位修改结果(协议层竞态,与基类逐点行为
        一致——本实现不引入新限制)。

        失败语义:

        - 任一笔 FC 失败 → ``(False, None)``(整批失败,不放出部分 ok)
        - 空列表 / 任意非法 → 同步 :class:`ValueError`,不进入事务锁

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: ``(是否成功, 与 items 顺序对应的 ok 列表)``
        :raises ValueError: 列表为空 / 地址 / 类型 / 值非法
        """
        if not items:
            raise ValueError("write_batch 至少需要一个 (地址, 数据类型, 值) 项")
        # 1) 入参合法性前置校验
        parsed_items: List[Tuple[ModbusAddress, DataType, PrimitiveValue]] = []
        for address, dtype, value in items:
            data_type_enum = DataType.coerce(dtype)
            parsed_items.append(
                (_check_address(address, data_type_enum), data_type_enum, value)
            )
        # 整批容错:任一 FC 失败 → (False, None)
        ok, results = self._execute(lambda: self._coalesce_and_write(parsed_items))
        if not ok or results is None:
            return False, None
        if not all(results):
            return False, None
        return True, results

    def _coalesce_and_write(
        self,
        items: List[Tuple["ModbusAddress", DataType, PrimitiveValue]],
    ) -> List[bool]:
        """批量写核心算法(内部方法):寄存器位走 RMW,其余按组合并。

        算法分四步:

        1. **分类 + 编码**:每个 ``(parsed, dtype, value)`` 按
           :func:`_classify` 得到 ``(kind, width)``;同时按 dtype 把
           value 编码为字序列(写入用)。寄存器位(``hr0.3`` 这类位号
           后缀)单独走"读-改-写"路径,**不入合并**(FC 16 写 1 字会冲
           掉其他位)。
        2. **分组**:非寄存器位的 key = ``(area, kind, width, dtype)``;
           同组合并后走 FC 15(线圈)/ FC 16(寄存器)。
        3. **合并**:复用 :func:`_coalesce_group`,``max_unit`` 按写
           上限参数化(位 1968 / 字 123)。
        4. **写 + 回填**:寄存器位逐项 RMW;合并后 chunk 各发 1 笔 FC,
           按 chunk 内各条目 ok 写回入参原位(同 chunk 全成功 / 全失败)。

        5 类分组(写侧,与读侧对称):

        =====  ==========================  =========  =======  ==========
        组      区域 × 类型                  kind       width   协议路径
        =====  ==========================  =========  =======  ==========
        A      线圈 × BOOL                  ``bit``    1 bit   FC 15
        B      寄存器 × BOOL(位号后缀)      ``bitword``1 word  FC 03+06 RMW
        C      寄存器 × SHORT/USHORT        ``word``   1 word  FC 16
        D      寄存器 × INT/UINT/FLOAT      ``word``   2 words FC 16
        E      寄存器 × LONG/ULONG/DOUBLE   ``word``   4 words FC 16
        =====  ==========================  =========  =======  ==========

        :return: 与 items 顺序对应的 ok 列表(同 chunk 内条目共享 ok)
        """
        # 1) 分流:寄存器位走 RMW,其余走合并
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
            # 编码 value → 字序列
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

        # 2) 寄存器位 RMW:offset 升序,逐项"读-改-写"
        rmw_items.sort(key=lambda t: t[1].offset)
        for item_index, parsed, flag in rmw_items:
            try:
                self._write_bool_impl(parsed, flag)
                result[item_index] = True
            except Exception:
                result[item_index] = False
                # RMW 单项失败不中断(逐点容错),整体失败语义由 write_batch 的 _execute 收口

        # 3) 合并:按 (area, kind, width, dtype) 分组
        groups: Dict[Tuple[ModbusArea, str, int, DataType], List[Tuple[int, _CoalesceEntry, List[int]]]] = {}
        for item_index, entry, encoded in plan:
            key = (entry.parsed.area, entry.kind, entry.width, entry.dtype)
            groups.setdefault(key, []).append((item_index, entry, encoded))

        # 4) 每组:排序 → 合并 → 逐 chunk 写 + 回填
        for (area, kind, _width, _dtype), group in groups.items():
            group.sort(key=lambda t: t[1].parsed.offset)
            max_unit = MODBUS_MAX_WRITE_BITS if kind == "bit" else MODBUS_MAX_WRITE_REGISTERS
            chunks = _coalesce_group(
                [t[1] for t in group], kind, max_unit
            )
            for chunk_entries in chunks:
                chunk_items = [
                    item
                    for item in group
                    if item[1] in chunk_entries
                ]
                ok = _write_and_apply(self, area, kind, chunk_items, result)
                if not ok:
                    # 任一 chunk 失败 → 该 chunk 内所有槽位已置 False
                    # write_batch 整批失败语义由 _execute 收口:
                    # 这里保证不再继续后面的写入,避免越界副作用
                    break

        return cast(List[bool], result)

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

    def _write_bools_impl(self, parsed: ModbusAddress, values: List[bool]) -> None:
        """写连续多个线圈(FC 15,与 :meth:`_write_registers_impl` 的 FC 16 对称)。

        :param parsed: 起始地址(仅线圈区)
        :param values: 连续 ``count`` 个线圈的真值表(LSB first)
        """
        if parsed.area != ModbusArea.COIL:
            raise ValueError(f"FC 15 仅支持线圈区域,收到:{parsed.area.value!r}")
        # codec 内部按位打包,期望 List[int];此处 bool 是 int 的子类可直接传
        int_values: List[int] = [1 if flag else 0 for flag in values]
        self._write_pdu(
            codec.build_write_multi_pdu(parsed.write_multi_function_code, parsed.offset, int_values)
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
        self._station = self._check_station(station)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    def _transact(self, pdu: bytes, expect_response: bool = True) -> bytes:
        """MBAP 事务:组帧→发送→按长度收→校验事务号/站号→返回 PDU。

        TCP 无广播语义(Unit ID 为路由字段),站号 0 照常等待响应。
        事务号/站号不匹配属坏帧,异常文本带收到的原始帧(便于判断是
       迟到的上一条响应、串口/网关错配还是对端语义不符)。
        """
        transport = self._require_transport()
        sent_id = self._bump_id("_transaction_id", 16)
        transport.send(codec.build_mbap(sent_id, self.station, pdu))
        header = transport.recv(MBAP_HEADER_SIZE)
        transaction_id, length = codec.parse_mbap_header(header)
        frame = header + transport.recv(length - 1)
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
        self._station = self._check_station(station)
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
        站号不匹配属坏帧(多为总线上其他从站的迟到响应),异常文本带
        收到的原始帧。
        """
        transport = self._require_transport()
        station = self.station
        transport.send(codec.build_rtu_frame(station, pdu))
        if not expect_response:
            return b""
        head = transport.recv(2)
        if head[1] & MODBUS_EXCEPTION_FLAG:
            frame = head + transport.recv(3)
        else:
            frame = head + transport.recv(codec.expected_response_length(pdu) + 1)
        received_station, response_pdu = codec.parse_rtu_frame(frame)
        if received_station != station:
            raise ProtocolFrameError(
                "RTU 站号不匹配:期望 {},收到 {}(收到的原始帧:{})".format(
                    station, received_station, format_hex(frame)
                )
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


class _CoalesceEntry(NamedTuple):
    """批量读合并条目(内部类型,不可变):保存单条 (地址, 类型) 在合并
    阶段所需的全部信息,用于按 ``item_index`` 回填到入参原序。

    字段:

    - ``item_index``:入参 ``items`` 中的位置(用于结果按原序回填)
    - ``parsed``:已解析的 Modbus 地址(含 area / offset / bit)
    - ``dtype``:数据目标类型
    - ``kind``:``"bit"``(位设备,FC 01/02)或 ``"word"``(寄存器,FC 03/04)
    - ``width``:单条占用的协议单位数——位设备恒为 1 bit,寄存器为
      1/2/4 word(由 dtype 决定)
    """

    item_index: int
    parsed: "ModbusAddress"
    dtype: DataType
    kind: str
    width: int


def _classify(parsed: "ModbusAddress", dtype: DataType) -> Tuple[str, int]:
    """按 (区, 类型) 决定读取粒度:``(kind, width)``(内部函数)。

    返回 ``(kind, width)``:

    =====  ========================  =========  =========
    区域 × 类型                      kind       width
    =====  ========================  =========  =========
    线圈 / 离散 × BOOL               ``bit``    1 bit
    寄存器 × BOOL                    ``word``   1 word(读字后提位)
    寄存器 × SHORT / USHORT          ``word``   1 word
    寄存器 × INT / UINT / FLOAT      ``word``   2 words
    寄存器 × LONG / ULONG / DOUBLE   ``word``   4 words
    =====  ========================  =========  =========

    :raises ValueError: Modbus 不支持的数据类型
    """
    if dtype is DataType.BOOL:
        if parsed.area in (ModbusArea.COIL, ModbusArea.DISCRETE_INPUT):
            return "bit", 1
        return "word", 1
    if dtype in (DataType.SHORT, DataType.USHORT):
        return "word", 1
    if dtype in (DataType.INT, DataType.UINT, DataType.FLOAT):
        return "word", 2
    if dtype in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
        return "word", 4
    raise ValueError(f"Modbus 不支持的数据类型:{dtype}")


def _coalesce_group(
    group: List[_CoalesceEntry],
    kind: str,
    max_unit: int,
) -> List[List[_CoalesceEntry]]:
    """把同组条目按"相邻偏移"合并为多个 chunk(内部函数)。

    同组内宽度统一(``kind=="bit"`` 全部宽 1 bit;``kind=="word"`` 全部
    宽 ``width`` words),按 offset 升序遍历。三条规则:

    1. **同 offset 合并**:同字位 / 同位(位设备),属于同一 FC 覆盖区
    2. **相邻 offset 合并**(``offset == current_end``):gap = 0,合并不切
    3. **空洞立即切块**(``offset > current_end``):协议只能读连续地址,
       空洞必须独立 FC

    合并后单 chunk 范围超 FC 上限(``max_unit``)再按上限切为多段(连续
    区内二次切片)。读 / 写按协议上下限传不同 ``max_unit``:读位 2000 /
    写字 125;写位 1968 / 写字 123。

    :param max_unit: 单 chunk 最大协议单位数(位设备按 bit 计,寄存器按 word 计)
    :return: 每个子列表是一个连续地址区间内的所有条目,合并为 1 笔 FC
    """
    chunks: List[List[_CoalesceEntry]] = []
    current: List[_CoalesceEntry] = []
    current_end = 0  # 当前 chunk 的"独占上界"(offset+width,半开)
    for entry in group:
        offset = entry.parsed.offset
        if not current:
            current = [entry]
            current_end = offset + entry.width
            continue
        if offset > current_end:
            # 规则 3:留空洞 → 切块,新开 chunk
            chunks.append(current)
            current = [entry]
            current_end = offset + entry.width
            continue
        # 规则 1 + 2:同字 / 相邻 offset,合并不切
        current.append(entry)
        current_end = max(current_end, offset + entry.width)
        # 连续区内若累计跨 FC 上限,二次切片
        if current_end - current[0].parsed.offset > max_unit:
            chunks.append(current)
            current = []
            current_end = 0
    if current:
        chunks.append(current)
    return chunks


def _read_and_fill(
    client: "ModbusBaseClient",
    area: ModbusArea,
    kind: str,
    chunk: List[_CoalesceEntry],
    result: List[Optional[PrimitiveValue]],
) -> None:
    """对合并区间执行单笔 FC 读,按各条目 dtype 解码并回填 result(内部函数)。

    流程:

    1. 计算 chunk 覆盖范围 ``[start_offset, end_offset)`` 与 ``count``
    2. 按 ``kind`` 发单笔 FC:
       - ``"bit"`` → 调 :meth:`_read_bits`,按条目 offset 取位
       - ``"word"`` → 调 :meth:`_read_registers`,按条目 dtype 解码字数据
    3. 解码按 dtype 分派,所有解码共用 ``word_order`` 处理多字类型
    4. 用 :attr:`_CoalesceEntry.item_index` 把值写到 result 原位

    解码映射:

    - BOOL(寄存器位)→ :func:`convert.get_bit` 提位
    - SHORT → :func:`convert.to_signed`(16 位有符号)
    - USHORT → 原值(0~65535)
    - INT/UINT/FLOAT → :func:`_decode_32bit` × 2 字,字序感知
    - LONG/ULONG/DOUBLE → :func:`_decode_64bit` × 4 字,字序感知
    """
    start_offset = chunk[0].parsed.offset
    # end_offset 是最后条目的 offset + width(位设备 width=1)
    end_offset = chunk[-1].parsed.offset + chunk[-1].width
    count = end_offset - start_offset

    if kind == "bit":
        # 位设备:占位 COIL/DISCRETE_INPUT 地址,跑一遍 _read_bits
        probe = ModbusAddress(area=area, offset=start_offset)
        bits = client._read_bits(probe, count)
        for entry in chunk:
            idx = entry.parsed.offset - start_offset
            result[entry.item_index] = bool(bits[idx])
        return

    # 寄存器访问:1 笔 FC 03/04 读 count 个字
    probe = ModbusAddress(area=area, offset=start_offset)
    words = client._read_registers(probe, count)
    for entry in chunk:
        word_idx = entry.parsed.offset - start_offset
        dtype = entry.dtype
        if dtype is DataType.BOOL:
            # 寄存器位:从对应字提位
            bit_index = entry.parsed.bit or 0
            result[entry.item_index] = bool(convert.get_bit(words[word_idx], bit_index))
            continue
        if dtype is DataType.SHORT:
            result[entry.item_index] = convert.to_signed(words[word_idx], 16)
            continue
        if dtype is DataType.USHORT:
            result[entry.item_index] = words[word_idx]
            continue
        if dtype in (DataType.INT, DataType.UINT, DataType.FLOAT):
            slice_words = words[word_idx : word_idx + 2]
            result[entry.item_index] = _decode_32bit(slice_words, dtype, client.word_order)
            continue
        if dtype in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            slice_words = words[word_idx : word_idx + 4]
            result[entry.item_index] = _decode_64bit(slice_words, dtype, client.word_order)
            continue
        raise ValueError(f"Modbus 不支持的数据类型:{dtype}")


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
        return convert.registers_to_int64(registers, word_order)
    if data_type is DataType.ULONG:
        return convert.registers_to_uint64(registers, word_order)
    return convert.registers_to_float64(registers, word_order)


def _encode_32bit(value: PrimitiveValue, data_type: DataType, word_order: WordOrder) -> List[int]:
    """按类型编码 32 位整数为 2 寄存器。"""
    number = require_int(value)
    if data_type is DataType.INT:
        if not INT32_MIN <= number <= INT32_MAX:
            raise ValueError(f"int 超出 32 位范围:{number}")
        return list(convert.int32_to_registers(number, word_order))
    if not 0 <= number <= UINT32_MAX:
        raise ValueError(f"uint 超出 32 位范围:{number}")
    return list(convert.uint32_to_registers(number, word_order))


def _encode_64bit(value: PrimitiveValue, data_type: DataType, word_order: WordOrder) -> List[int]:
    """按类型编码 64 位整数为 4 寄存器。"""
    number = require_int(value)
    if data_type is DataType.LONG:
        if not INT64_MIN <= number <= INT64_MAX:
            raise ValueError(f"long 超出 64 位范围:{number}")
        return list(convert.int64_to_registers(number, word_order))
    if not 0 <= number <= UINT64_MAX:
        raise ValueError(f"ulong 超出 64 位范围:{number}")
    return list(convert.uint64_to_registers(number, word_order))


def _encode_value_for_write(
    parsed: "ModbusAddress",
    dtype: DataType,
    value: PrimitiveValue,
    word_order: WordOrder,
) -> List[int]:
    """把入参 value 按 dtype 编码为字/位序列(写合并使用,内部函数)。

    编码映射:

    =====  ========================  =============  =============
    dtype                           序列元素类型    长度
    =====  ========================  =============  =============
    BOOL(COIL)                      int 0/1         1(由
                                                     :meth:`_write_bools_impl`
                                                     按位打包)
    SHORT                           int 0~65535     1
    USHORT                          int 0~65535     1
    INT/UINT                        int 0~65535     2(字序感知)
    LONG/ULONG                      int 0~65535     4(字序感知)
    FLOAT                           int 0~65535     2(字序感知)
    DOUBLE                          int 0~65535     4(字序感知)
    =====  ========================  =============  =============

    寄存器位(``hr0.3`` 这类位号后缀)在调用方已分流到 RMW 路径,不在此
    函数处理范围内。
    """
    if dtype is DataType.BOOL:
        flag = require_bool(value)
        return [1 if flag else 0]
    if dtype is DataType.SHORT:
        return [check_int16(value) & 0xFFFF]
    if dtype is DataType.USHORT:
        return [check_uint16(value)]
    if dtype in (DataType.INT, DataType.UINT):
        return _encode_32bit(value, dtype, word_order)
    if dtype in (DataType.LONG, DataType.ULONG):
        return _encode_64bit(value, dtype, word_order)
    if dtype is DataType.FLOAT:
        return list(convert.float32_to_registers(require_float(value), word_order))
    if dtype is DataType.DOUBLE:
        return list(convert.float64_to_registers(require_float(value), word_order))
    raise ValueError(f"Modbus 不支持的数据类型:{dtype}")


def _write_and_apply(
    client: "ModbusBaseClient",
    area: ModbusArea,
    kind: str,
    chunk: List[Tuple[int, _CoalesceEntry, List[int]]],
    result: List[Optional[bool]],
) -> bool:
    """对合并区间执行单笔 FC 写,把 chunk 内条目 ok 写回 result(内部函数)。

    流程:

    1. 计算 chunk 覆盖范围 ``[start_offset, end_offset)`` 与 ``count``
    2. 按 ``kind`` 发单笔 FC:
       - ``"bit"``(COIL)→ 调 :meth:`_write_bools_impl` 写 ``count`` 位
       - ``"word"``(寄存器)→ 调 :meth:`_write_registers_impl` 写
         ``count`` 字(各条目按 offset 拼入,中间空洞按 0 填)
    3. 写成功 → chunk 内所有 ``item_index`` 槽位置 ``True``;失败 → 全
       置 ``False`` 并返回 ``False``

    :returns: chunk 写入是否成功(失败时调用方用以触发整批失败语义)
    """
    if not chunk:
        return True
    start_offset = chunk[0][1].parsed.offset
    end_offset = chunk[-1][1].parsed.offset + chunk[-1][1].width
    count = end_offset - start_offset

    try:
        if kind == "bit":
            values: List[bool] = [False] * count
            for _, entry, encoded in chunk:
                idx = entry.parsed.offset - start_offset
                values[idx] = bool(encoded[0])
            probe = ModbusAddress(area=area, offset=start_offset)
            client._write_bools_impl(probe, values)
        else:
            words: List[int] = [0] * count
            for _, entry, encoded in chunk:
                idx = entry.parsed.offset - start_offset
                for offset_within, word in enumerate(encoded):
                    words[idx + offset_within] = word
            probe = ModbusAddress(area=area, offset=start_offset)
            client._write_registers_impl(probe, words)
    except Exception:
        for _, entry, _ in chunk:
            result[entry.item_index] = False
        return False

    for _, entry, _ in chunk:
        result[entry.item_index] = True
    return True
