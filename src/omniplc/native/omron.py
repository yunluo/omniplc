"""原生 asyncio 欧姆龙 FINS 客户端(TCP 与 UDP 走线)。

:class:`AsyncOmronFinsTcpClient` / :class:`AsyncOmronFinsUdpClient` 是同步
:class:`~omniplc.plc.omron.OmronFinsTcpClient` / ``OmronFinsUdpClient`` 的原生
异步孪生:FINS 帧编解码、存储区码表、地址解析、节点号推导**全部复用**同步侧
实现(:mod:`omniplc.plc.omron.codec` / ``address`` 与既有的 ``_build_read`` /
``_build_write`` / ``_words_to_value`` / ``_value_to_words`` / ``_node_from_host``
/ ``_local_ip_for``),本模块只把 ``_transact`` / ``_after_connect`` 换成协程版。

能力面与同步层对齐:单点读/写(位、字、字符串)+ 类型化方法 + 点位表 + 批量
(0104 多存储区读;写按基类逐点,协议无跨存储区单事务写原语)。

:example::

    from omniplc.native import AsyncOmronFinsTcpClient

    async with AsyncOmronFinsTcpClient("192.168.250.1") as client:
        ok, value = await client.read_ushort("D100")
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport, AsyncUdpTransport
from ..core import convert
from ..core.base_client import validate_endpoint
from ..core.constants import (
    FINS_BIT_FALLBACK_AREAS,
    FINS_DEFAULT_DESTINATION_NETWORK,
    FINS_DEFAULT_DESTINATION_UNIT,
    FINS_DEFAULT_PORT,
    FINS_DEFAULT_SOURCE_NETWORK,
    FINS_DEFAULT_SOURCE_UNIT,
    FINS_MAX_DATAGRAM,
    FINS_MAX_READ_ELEMENTS,
    FINS_MAX_WRITE_ELEMENTS,
    FINS_NETWORK_MAX,
    FINS_NODE_MAX,
    FINS_SID_BITS,
    FINS_TCP_HEADER_SIZE,
    FINS_TIMER_COUNTER_AREAS,
    FINS_UNIT_MAX,
    FINS_UNSUPPORTED_AREA_CODE,
)
from ..core.errors import DeviceError
from ..core.validation import check_int16, check_uint16, check_range, require_bool
from ..plc.omron import codec
from ..plc.omron.address import FinsAddress, parse_fins_address
from ..plc.omron.omron import (
    FINS_BIT_WRITABLE_AREAS,
    _local_ip_for,
    _node_from_host,
    _value_to_words,
    _words_to_value,
)
from ..core.types import DataType, PrimitiveValue
from ..core.i18n import _


class AsyncOmronFinsBase(AsyncBaseClient):
    """FINS 原生异步基类:节点地址、SID 与软元件分发。

    走线子类只实现 :meth:`_create_transport`、:meth:`_transact` 与(需要握手的
    TCP 走线)：meth:`_after_connect`。路由字段范围校验与同步层同口径
    (network 0~127、node 0~254、unit 0~255,越界构造期拒绝,不做 ``& 0xFF``
    静默截断)。
    """

    # 探活:FINS 0601 CPU Unit Status Read(与同步基类同口径,见 _ping_probe)
    _has_ping = True

    def __init__(
        self,
        ip_address: str,
        port: int = FINS_DEFAULT_PORT,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: Optional[int] = None,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = FINS_DEFAULT_SOURCE_NETWORK,
        source_node: Optional[int] = None,
        source_unit: int = FINS_DEFAULT_SOURCE_UNIT,
    ) -> None:
        """初始化 FINS 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,FINS 默认 9600
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号;``None``/``0`` = 自动
            (TCP 经握手获取,UDP 从 PLC IP 末段推导);显式传值原样使用
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号;``None``/``0`` = 自动
            (TCP 经握手获取,UDP 从本机出口 IP 末段推导);显式传值原样使用
        :param source_unit: 源单元号(上位机为 0)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._destination_network = check_range(
            int(destination_network), 0, FINS_NETWORK_MAX, "目标网络号"
        )
        self._destination_node = (
            check_range(int(destination_node), 0, FINS_NODE_MAX, "目标节点号")
            if destination_node is not None
            else 0
        )
        self._destination_unit = check_range(
            int(destination_unit), 0, FINS_UNIT_MAX, "目标单元号"
        )
        self._source_network = check_range(
            int(source_network), 0, FINS_NETWORK_MAX, "源网络号"
        )
        self._source_node = (
            check_range(int(source_node), 0, FINS_NODE_MAX, "源节点号")
            if source_node is not None
            else 0
        )
        self._source_unit = check_range(int(source_unit), 0, FINS_UNIT_MAX, "源单元号")
        # 自动模式(None/0):每次连接刷新为最新握手/推导结果;显式值不被覆盖
        self._auto_destination_node = destination_node is None or destination_node == 0
        self._auto_source_node = source_node is None or source_node == 0
        self._sid = 0

    @property
    def destination_network(self) -> int:
        """目标网络号。"""
        return self._destination_network

    @property
    def destination_node(self) -> int:
        """目标节点号(自动模式为握手/推导后的最新值)。"""
        return self._destination_node

    @property
    def destination_unit(self) -> int:
        """目标单元号。"""
        return self._destination_unit

    @property
    def source_network(self) -> int:
        """源网络号。"""
        return self._source_network

    @property
    def source_node(self) -> int:
        """源节点号(自动模式为握手/推导后的最新值)。"""
        return self._source_node

    @property
    def source_unit(self) -> int:
        """源单元号。"""
        return self._source_unit

    def _next_sid(self) -> int:
        """SID 递增(0~255 回绕,事务标识,内部方法)。"""
        return self._bump_id("_sid", FINS_SID_BITS)

    # ------------------------------------------------------------------
    # 状态读与探活(FINS 0601)
    # ------------------------------------------------------------------

    async def read_cpu_unit_status(self) -> Tuple[bool, Optional[Dict[str, object]]]:
        """读 CPU 单元运行状态(FINS 0601,W342 §5-3-17 印刷页 194-196)。

        返回字段字典与同步侧 :meth:`~omniplc.OmronFinsTcpClient.read_cpu_unit_status`
        完全一致;命令帧仅命令码、无参数,零副作用——同时是 :meth:`ping`
        的探测命令。

        :return: ``(是否成功, 状态字典)``;失败为 ``(False, None)``
        """
        return await self._execute(self._cpu_unit_status_operation)

    async def _ping_probe(self) -> Dict[str, object]:
        """探活探测命令:0601 CPU Unit Status Read(内部方法)。"""
        return await self._cpu_unit_status_operation()

    async def _cpu_unit_status_operation(self) -> Dict[str, object]:
        """0601 状态读的协议操作(内部方法;公开方法与探活共用)。"""
        request = codec.build_cpu_unit_status_read(
            self._destination_network,
            self._destination_node,
            self._destination_unit,
            self._source_network,
            self._source_node,
            self._source_unit,
            self._next_sid(),
        )
        return codec.parse_cpu_unit_status_read(await self._transact(request), request)

    # ------------------------------------------------------------------
    # 协议原语(基类类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FINS 读原语:存储区地址 → Area Read(0101)→ 按类型解码(大端)。"""
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                _("T/C 完成标志为单点位,地址不带位号:{!r}(示例:T0)").format(address)
            )
        if data_type is DataType.BOOL:
            return await self._read_bit_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            return _words_to_value(await self._read_words(parsed, 1), data_type)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return _words_to_value(await self._read_words(parsed, 2), data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            return _words_to_value(await self._read_words(parsed, 4), data_type)
        raise ValueError(_("FINS 不支持的数据类型:{}").format(data_type))

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """FINS 写原语:Area Write(0102)。

        位区(CIO/W/H/A)直接位写;字区(D/EM)按位写用读-改-写。
        """
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                _("T/C 完成标志为单点位,地址不带位号:{!r}(示例:T0)").format(address)
            )
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            if parsed.area in FINS_TIMER_COUNTER_AREAS:
                raise ValueError(
                    _("T/C 完成标志由系统驱动,只读:{!r}(写当前值请用字访问,如 write_ushort({!r}, 100))").format(
                        address, parsed.area + str(parsed.offset)
                    )
                )
            if parsed.area in FINS_BIT_WRITABLE_AREAS:
                await self._write_bits(parsed, [1 if flag else 0])
            else:
                await self._write_bit_impl(parsed, flag)
            return
        if data_type is DataType.SHORT:
            await self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            await self._write_words(parsed, [check_uint16(value)])
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            await self._write_words(parsed, _value_to_words(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            await self._write_words(parsed, _value_to_words(value, data_type))
            return
        raise ValueError(_("FINS 不支持的数据类型:{}").format(data_type))

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """从字区读字符串:逐字大端拼字节后解码(FINS 字序约定)。"""
        parsed = parse_fins_address(address)
        if parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        words = await self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "big") for word in words)[:length]
        return convert.decode_string(data, encoding)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """向字区写字符串:编码 → 补齐偶数字节 → 逐字大端。"""
        parsed = parse_fins_address(address)
        if parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        await self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 批量读取(0104 多存储区读,单事务)
    # ------------------------------------------------------------------

    async def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:同存储区起连续 ``count`` 个元素,0101 Area Read 单事务。

        契约与同步 :meth:`~omniplc.plc.omron.OmronFinsTcpClient.read_range`
        一致:字区数值类型连续字读、位区(CIO/W/H/A)BOOL 连续位读;
        T/C 完成标志 / 字区 BOOL / STRING / 超单命令上限入参期拒绝。

        :param address: 起始存储区地址(如 ``"D100"``、``"CIO0"``)
        :param count: 元素个数(按 ``data_type`` 计,INT×10 = 20 字)
        :param data_type: 数据类型
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 字数超限
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        data_type_enum = DataType.coerce(data_type)
        if data_type_enum is DataType.STRING:
            raise ValueError(_("read_range 不支持 STRING,请用 read_string"))
        parsed = parse_fins_address(address)
        if data_type_enum is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        if data_type_enum is DataType.BOOL:
            if parsed.area in FINS_TIMER_COUNTER_AREAS:
                raise ValueError(
                    _("T/C 完成标志为单点位,不支持连续读:{!r}(示例:T0)").format(address)
                )
            if parsed.area not in FINS_BIT_WRITABLE_AREAS:
                raise ValueError(
                    _("FINS read_range 的 BOOL 连续读需要位存储区(CIO/W/H/A):{!r}").format(address)
                )
            if parsed.bit is not None:
                raise ValueError(
                    _("FINS read_range 位区读不带位号:{!r}(位号即点位,直接用 CIO/W/H/A)").format(address)
                )
            frame = self._build_read(parsed, count, is_bit=True)
            ok, raw_bits = await self._execute(
                lambda: self._read_bits_transaction(frame, count)
            )
            if not ok or raw_bits is None:
                return False, None
            return True, [bool(bit) for bit in raw_bits]
        width = 1
        if data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
            width = 2
        elif data_type_enum in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            width = 4
        if count * width > FINS_MAX_READ_ELEMENTS:
            raise ValueError(
                _("FINS read_range 字数超单命令上限 {}:{}×{}={}").format(
                    FINS_MAX_READ_ELEMENTS, count, width, count * width
                )
            )
        ok, raw_words = await self._execute(
            lambda: self._read_words(parsed, count * width)
        )
        if not ok or raw_words is None:
            return False, None
        values: List[PrimitiveValue] = []
        for index in range(count):
            chunk = raw_words[index * width:(index + 1) * width]
            values.append(_words_to_value(chunk, data_type_enum))
        return True, values

    async def _read_bits_transaction(self, frame: bytes, count: int) -> List[int]:
        """位区连续位读的单事务通道(内部方法,0101 位区码)。"""
        return codec.parse_response(
            await self._transact(frame), frame, count, is_bit=True, is_read=True
        )

    async def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 0104 多存储区读(单事务)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 ``last_error``);需要逐点容错请逐点调用 :meth:`read`。
        契约与同步 :meth:`~omniplc.plc.omron.OmronFinsTcpClient.read_many` 一致。

        :param addresses: 地址列表(存储区可各不相同)
        :param data_type: 统一数据类型
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = await self.read_batch(
            [(address, data_type_enum) for address in addresses]
        )
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多存储区批量读取:0104 单事务混读多个非连续字(TCP/UDP 通用)。

        每条目读 1 个字,软元件/类型可各不相同(W342 §5-3-5):32/64 位类型拆成
        相邻多条,BOOL 走包含字的字区码后本地提位(0104 仅字码);T/C 完成标志
        为位区,不支持批量(T/C 当前值可按字批量读)。条目上限 167(Ethernet /
        Controller Link 口径,由 ``codec.build_multiple_area_read`` 收口)。
        规划与解析**复用同步侧同一套助手**。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空/地址或类型非法/条目数超限
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        entries: List[Tuple[int, int]] = []
        plan: List[Tuple[str, int, int, DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            parsed = parse_fins_address(address)
            if data_type_enum is not DataType.BOOL and parsed.bit is not None:
                raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
            if data_type_enum is DataType.BOOL:
                if parsed.area in FINS_TIMER_COUNTER_AREAS:
                    raise ValueError(
                        _("T/C 完成标志不支持批量读取(0104 仅字区):{!r}").format(address)
                    )
                _unused, word_code = codec.memory_codes(parsed.area, parsed.bank)
                plan.append(("wordbit", len(entries), parsed.bit or 0, data_type_enum))
                entries.append((word_code, parsed.offset))
                continue
            if data_type_enum in (DataType.SHORT, DataType.USHORT):
                words = 1
            elif data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
                words = 2
            elif data_type_enum in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
                words = 4
            else:
                raise ValueError(_("FINS 批量读取不支持的数据类型:{}").format(data_type_enum))
            _unused, word_code = codec.memory_codes(parsed.area, parsed.bank)
            plan.append(("word", len(entries), words, data_type_enum))
            for index in range(words):
                entries.append((word_code, parsed.offset + index))
        codes = [code for code, _unused in entries]

        async def operation() -> List[PrimitiveValue]:
            frame = codec.build_multiple_area_read(
                self._destination_network,
                self._destination_node,
                self._destination_unit,
                self._source_network,
                self._source_node,
                self._source_unit,
                self._next_sid(),
                entries,
            )
            words = codec.parse_multiple_area_read(
                await self._transact(frame), frame, codes
            )
            values: List[PrimitiveValue] = []
            for kind, index, extra, item_type in plan:
                if kind == "wordbit":
                    values.append(bool(convert.get_bit(words[index], extra)))
                else:
                    values.append(_words_to_value(words[index:index + extra], item_type))
            return values

        return await self._execute(operation)

    # ------------------------------------------------------------------
    # 位/字原语(Area Read/Write 帧)
    # ------------------------------------------------------------------

    async def _read_bit_impl(self, parsed: FinsAddress) -> bool:
        """位读:位存储区码 + 位地址;老固件不支持 D/EM 区位码时回退字读提位。"""
        try:
            frame = self._build_read(parsed, 1, is_bit=True)
            values = codec.parse_response(
                await self._transact(frame), frame, 1, is_bit=True, is_read=True
            )
            return bool(values[0])
        except DeviceError as exc:
            if (
                exc.code == FINS_UNSUPPORTED_AREA_CODE
                and parsed.area in FINS_BIT_FALLBACK_AREAS
            ):
                # CP1E/部分 CS1 不支持 D/EM 区位码(结束码 0x1101):
                # 回退「字读 + 本地按位提取」,对调用方透明(镜像同步侧)
                words = await self._read_words(parsed._replace(bit=None), 1)
                return bool(convert.get_bit(words[0], parsed.bit or 0))
            raise

    async def _write_bit_impl(self, parsed: FinsAddress, flag: bool) -> None:
        """位写:位存储区码直写;老固件不支持 D/EM 位区码(0x1101)时回退
        读-改-写(镜像同步侧;D/EM 位直写依手册 §5-3-3 可写表)。

        **非原子披露**(第八轮 P2-20,与同步侧同口径):0x1101 回退是
        「字读 → 字写」两笔事务,扫描周期内同字其他位可能被覆盖。"""
        value = 1 if flag else 0
        try:
            await self._write_bits(parsed, [value])
        except DeviceError as exc:
            if (
                exc.code == FINS_UNSUPPORTED_AREA_CODE
                and parsed.area in FINS_BIT_FALLBACK_AREAS
            ):
                words = await self._read_words(parsed._replace(bit=None), 1)
                await self._write_words(
                    parsed._replace(bit=None),
                    [convert.set_bit(words[0], parsed.bit or 0, flag)],
                )
                return
            raise

    async def _read_words(self, parsed: FinsAddress, word_count: int) -> List[int]:
        """字读:字存储区码,返回 0~65535 逐字数据(大端)。

        Ethernet/Controller Link 单命令读上限 999 字(W342 §5-2-2 p.168),
        超限入参期拒绝。
        """
        if word_count > FINS_MAX_READ_ELEMENTS:
            raise ValueError(
                _("FINS 单命令读元素数超出 Ethernet/Controller Link 上限 {}:{}(W342 §5-2-2)").format(
                    FINS_MAX_READ_ELEMENTS, word_count
                )
            )
        frame = self._build_read(parsed, word_count, is_bit=False)
        return codec.parse_response(
            await self._transact(frame), frame, word_count, is_bit=False, is_read=True
        )

    async def _write_bits(self, parsed: FinsAddress, values: List[int]) -> None:
        """位写:位存储区码,每点 1 字节 0x00/0x01。"""
        frame = self._build_write(parsed, values, is_bit=True)
        codec.parse_response(
            await self._transact(frame), frame, len(values), is_bit=True, is_read=False
        )

    async def _write_words(self, parsed: FinsAddress, words: List[int]) -> None:
        """字写:字存储区码,逐字大端。

        Ethernet/Controller Link 单命令写上限 997 字(W342 §5-2-2 p.168),
        超限入参期拒绝。
        """
        if len(words) > FINS_MAX_WRITE_ELEMENTS:
            raise ValueError(
                _("FINS 单命令写元素数超出 Ethernet/Controller Link 上限 {}:{}(W342 §5-2-2)").format(
                    FINS_MAX_WRITE_ELEMENTS, len(words)
                )
            )
        frame = self._build_write(parsed, words, is_bit=False)
        codec.parse_response(
            await self._transact(frame), frame, len(words), is_bit=False, is_read=False
        )

    def _build_read(self, parsed: FinsAddress, count: int, is_bit: bool) -> bytes:
        """构造 Area Read 帧(内部方法)。"""
        return codec.build_area_read(
            self._destination_network,
            self._destination_node,
            self._destination_unit,
            self._source_network,
            self._source_node,
            self._source_unit,
            self._next_sid(),
            parsed,
            count,
            is_bit,
        )

    def _build_write(self, parsed: FinsAddress, data: List[int], is_bit: bool) -> bytes:
        """构造 Area Write 帧(内部方法)。"""
        return codec.build_area_write(
            self._destination_network,
            self._destination_node,
            self._destination_unit,
            self._source_network,
            self._source_node,
            self._source_unit,
            self._next_sid(),
            parsed,
            data,
            is_bit,
        )

    @abstractmethod
    async def _transact(self, fins_frame: bytes) -> bytes:
        """发送 FINS 帧并返回 FINS 帧响应(走线封装由子类实现)。"""


class AsyncOmronFinsTcpClient(AsyncOmronFinsBase):
    """欧姆龙 FINS/TCP 客户端(连接后先做 FINS/TCP 节点分配握手)。

    :example: ``client = AsyncOmronFinsTcpClient("192.168.250.1")``
    """

    def __init__(
        self,
        ip_address: str = "192.168.250.1",
        port: int = FINS_DEFAULT_PORT,
        local_node: Optional[int] = None,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: Optional[int] = None,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = FINS_DEFAULT_SOURCE_NETWORK,
        source_node: Optional[int] = None,
        source_unit: int = FINS_DEFAULT_SOURCE_UNIT,
    ) -> None:
        """初始化 FINS/TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,默认 9600
        :param local_node: 本地节点号;``None``/``0`` = 由 PLC 自动分配(握手获取)
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号;``None``/``0`` = 握手自动获取
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号;``None``/``0`` = 握手自动获取
        :param source_unit: 源单元号(上位机为 0)
        :raises ValueError: 参数非法
        """
        super().__init__(
            ip_address,
            port,
            destination_network,
            destination_node,
            destination_unit,
            source_network,
            source_node,
            source_unit,
        )
        self._local_node = (
            check_range(int(local_node), 0, FINS_NODE_MAX, "本地节点号")
            if local_node is not None
            else 0
        )
        self._auto_local_node = local_node is None or local_node == 0

    @property
    def local_node(self) -> int:
        """本地节点号(自动分配时在握手后可用)。"""
        return self._local_node

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncTcpTransport(self._ip_address, self._port)

    async def _after_connect(self) -> None:
        """FINS/TCP 握手:发送节点分配请求并解析响应(内部方法)。

        自动模式(构造时节点号传 None/0)每次重连都刷新为最新握手分配值;
        显式配置的节点号保持不被覆盖(与同步层同口径)。
        """
        transport = self._require_transport()
        await transport.send(codec.build_handshake(self._local_node))
        head = await transport.recv(FINS_TCP_HEADER_SIZE)
        frame = head + await transport.recv(codec.parse_tcp_head(head))
        local_node, plc_node = codec.parse_handshake_response(frame)
        if self._auto_local_node:
            self._local_node = local_node
        if self._auto_source_node:
            self._source_node = local_node
        if self._auto_destination_node:
            self._destination_node = plc_node

    async def _transact(self, fins_frame: bytes) -> bytes:
        """FINS/TCP 事务:封装 TCP 头 → 收 8 字节头 → 按长度收 → 校验错误域。"""
        transport = self._require_transport()
        await transport.send(codec.build_tcp_frame(fins_frame))
        head = await transport.recv(FINS_TCP_HEADER_SIZE)
        content = await transport.recv(codec.parse_tcp_head(head))
        codec.extract_tcp_error(content)
        return codec.extract_tcp_payload(content)


class AsyncOmronFinsUdpClient(AsyncOmronFinsBase):
    """欧姆龙 FINS/UDP 客户端,无握手,一问一答一数据报。

    节点号缺省自动从 IP 推导(Omron 以太网惯例:节点号 = IP 末段):
    目标节点 = PLC IP 末段,源节点 = 本机出口 IP 末段(连接时探测);
    显式传 ``destination_node``/``source_node`` 则原样使用。

    :example: ``client = AsyncOmronFinsUdpClient("192.168.250.1")``
    """

    def __init__(
        self,
        ip_address: str = "192.168.250.1",
        port: int = FINS_DEFAULT_PORT,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: Optional[int] = None,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = FINS_DEFAULT_SOURCE_NETWORK,
        source_node: Optional[int] = None,
        source_unit: int = FINS_DEFAULT_SOURCE_UNIT,
    ) -> None:
        """初始化 FINS/UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,默认 9600
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号;``None``/``0`` = 自动从 PLC IP
            末段推导;显式传值原样使用
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号;``None``/``0`` = 自动从本机出口 IP 末段
            推导;显式传值原样使用
        :param source_unit: 源单元号(上位机为 0)
        :raises ValueError: 参数非法
        """
        super().__init__(
            ip_address,
            port,
            destination_network,
            destination_node,
            destination_unit,
            source_network,
            source_node,
            source_unit,
        )

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncUdpTransport(self._ip_address, self._port)

    async def _after_connect(self) -> None:
        """UDP 无握手:节点号自动模式在连接时推导(内部方法)。

        目标节点 = PLC IP 末段;源节点 = 本机对 PLC 地址实际出口 IP 的末段
        (UDP connect 探测,与真实通信同一路由)。**推导一律用传输层已异步解析
        出的对端 IP 字面量**(:attr:`AsyncUdpTransport.peer_ip`),不把主机名
        交给 ``_node_from_host`` / ``_local_ip_for``——那两个助手对主机名会调
        ``socket.gethostbyname``(阻塞解析),在事件循环里会把整段 await 卡住
        (实测 ``localhost`` 目标下循环停顿 260ms,慢 DNS 更久);对 IP 字面量
        目标本就没有解析步骤,故取值与同步层**逐值一致**。自动模式每次连接都
        重新推导,显式配置的节点号不被覆盖。
        """
        transport = self._require_transport()
        peer_ip = transport.peer_ip or self._ip_address
        if self._auto_destination_node:
            self._destination_node = _node_from_host(peer_ip)
        if self._auto_source_node:
            self._source_node = _node_from_host(_local_ip_for(peer_ip, self._port))

    async def _transact(self, fins_frame: bytes) -> bytes:
        """FINS/UDP 事务:一帧一数据报,整包接收(长度校验交给 codec)。"""
        transport = self._require_transport()
        await transport.send(fins_frame)
        return await transport.recv(FINS_MAX_DATAGRAM)
