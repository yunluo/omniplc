"""原生 asyncio 欧姆龙 FINS 客户端(TCP 与 UDP 走线)。

:class:`AsyncOmronFinsTcpClient` / :class:`AsyncOmronFinsUdpClient` 是同步
:class:`~omniplc.plc.omron.OmronFinsTcpClient` / ``OmronFinsUdpClient`` 的原生
异步孪生:FINS 帧编解码、存储区码表、地址解析、节点号推导**全部复用**同步侧
实现(:mod:`omniplc.plc.omron.codec` / ``address`` 与既有的 ``_build_read`` /
``_build_write`` / ``_words_to_value`` / ``_value_to_words`` / ``_node_from_host``
/ ``_local_ip_for``),本模块只把 ``_transact`` / ``_after_connect`` 换成协程版。

首发能力面:单点读/写(位、字、字符串)+ 类型化方法 + 点位表。

**尚未包含**:``read_many``/``read_batch``(0104 多存储区读)——留后续批次。

:example::

    from omniplc.native import AsyncOmronFinsTcpClient

    async with AsyncOmronFinsTcpClient("192.168.250.1") as client:
        ok, value = await client.read_ushort("D100")
"""
from __future__ import annotations

from abc import abstractmethod
from typing import List, Optional

from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport, AsyncUdpTransport
from .. import convert
from ..core.base_client import validate_endpoint
from ..core.constants import (
    FINS_DEFAULT_DESTINATION_NETWORK,
    FINS_DEFAULT_DESTINATION_UNIT,
    FINS_DEFAULT_PORT,
    FINS_DEFAULT_SOURCE_NETWORK,
    FINS_DEFAULT_SOURCE_UNIT,
    FINS_MAX_DATAGRAM,
    FINS_NETWORK_MAX,
    FINS_NODE_MAX,
    FINS_SID_BITS,
    FINS_TCP_HEADER_SIZE,
    FINS_TIMER_COUNTER_AREAS,
    FINS_UNIT_MAX,
)
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
from ..types import DataType, PrimitiveValue


class AsyncOmronFinsBase(AsyncBaseClient):
    """FINS 原生异步基类:节点地址、SID 与软元件分发。

    走线子类只实现 :meth:`_create_transport`、:meth:`_transact` 与(需要握手的
    TCP 走线)：meth:`_after_connect`。路由字段范围校验与同步层同口径
    (network/node 0~127、unit 0~255,越界构造期拒绝,不做 ``& 0xFF`` 静默截断)。
    """

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
    # 协议原语(基类类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FINS 读原语:存储区地址 → Area Read(0101)→ 按类型解码(大端)。"""
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                f"T/C 完成标志为单点位,地址不带位号:{address!r}(示例:T0)"
            )
        if data_type is DataType.BOOL:
            return await self._read_bit_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            return _words_to_value(await self._read_words(parsed, 1), data_type)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return _words_to_value(await self._read_words(parsed, 2), data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            return _words_to_value(await self._read_words(parsed, 4), data_type)
        raise ValueError(f"FINS 不支持的数据类型:{data_type}")

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """FINS 写原语:Area Write(0102)。

        位区(CIO/W/H/A)直接位写;字区(D/EM)按位写用读-改-写。
        """
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                f"T/C 完成标志为单点位,地址不带位号:{address!r}(示例:T0)"
            )
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            if parsed.area in FINS_TIMER_COUNTER_AREAS:
                raise ValueError(
                    "T/C 完成标志由系统驱动,只读:{!r}(写当前值请用字访问,如 write_ushort({!r}, 100))".format(
                        address, parsed.area + str(parsed.offset)
                    )
                )
            if parsed.area in FINS_BIT_WRITABLE_AREAS:
                await self._write_bits(parsed, [1 if flag else 0])
            else:
                words = await self._read_words(parsed._replace(bit=None), 1)
                await self._write_words(
                    parsed._replace(bit=None),
                    [convert.set_bit(words[0], parsed.bit or 0, flag)],
                )
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
        raise ValueError(f"FINS 不支持的数据类型:{data_type}")

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """从字区读字符串:逐字大端拼字节后解码(FINS 字序约定)。"""
        parsed = parse_fins_address(address)
        words = await self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "big") for word in words)[:length]
        return convert.decode_string(data, encoding)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """向字区写字符串:编码 → 补齐偶数字节 → 逐字大端。"""
        parsed = parse_fins_address(address)
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        await self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 位/字原语(Area Read/Write 帧)
    # ------------------------------------------------------------------

    async def _read_bit_impl(self, parsed: FinsAddress) -> bool:
        """位读:位存储区码 + 位地址,CPU 直接返回点位值。"""
        frame = self._build_read(parsed, 1, is_bit=True)
        values = codec.parse_response(
            await self._transact(frame), frame, 1, is_bit=True, is_read=True
        )
        return bool(values[0])

    async def _read_words(self, parsed: FinsAddress, word_count: int) -> List[int]:
        """字读:字存储区码,返回 0~65535 逐字数据(大端)。"""
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
        """字写:字存储区码,逐字大端。"""
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
        """UDP 无握手:节点号自动模式在连接时从 IP 推导(内部方法)。

        目标节点 = PLC IP 末段(主机名先解析);源节点 = 本机对 PLC 地址实际
        出口 IP 的末段(UDP connect 探测,与真实通信同一路由)。自动模式每次
        连接都重新推导,显式配置的节点号不被覆盖。

        ``_node_from_host`` / ``_local_ip_for`` 是纯本机地址推导(不发报文、
        无网络等待),与同步层共用同一实现,这里按原样调用。
        """
        if self._auto_destination_node:
            self._destination_node = _node_from_host(self._ip_address)
        if self._auto_source_node:
            self._source_node = _node_from_host(
                _local_ip_for(self._ip_address, self._port)
            )

    async def _transact(self, fins_frame: bytes) -> bytes:
        """FINS/UDP 事务:一帧一数据报,整包接收(长度校验交给 codec)。"""
        transport = self._require_transport()
        await transport.send(fins_frame)
        return await transport.recv(FINS_MAX_DATAGRAM)
