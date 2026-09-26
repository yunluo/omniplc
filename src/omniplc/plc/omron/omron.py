"""欧姆龙 FINS 协议客户端(TCP/UDP 走线)。

类继承::

    BaseClient
    ├── OmronFinsTcpClient   FINS 帧 + FINS/TCP 握手(默认端口 9600)
    └── OmronFinsUdpClient   FINS 帧 over UDP(默认端口 9600,无握手)

两走线共享同一套 FINS 帧编解码(:mod:`.codec`),区别仅在 TCP 需要
先做 FINS/TCP 节点分配握手,且每帧外层多一个 FINS/TCP 头。
FINS 多字数据为大端字序。
"""
from __future__ import annotations

import socket
from abc import abstractmethod
from typing import List, Optional, Sequence, Tuple, Union

from . import codec
from .address import FinsAddress, parse_fins_address
from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    FINS_BIT_WRITABLE_AREAS,
    FINS_DEFAULT_DESTINATION_NETWORK,
    FINS_DEFAULT_DESTINATION_UNIT,
    FINS_DEFAULT_PORT,
    FINS_DEFAULT_SOURCE_NETWORK,
    FINS_DEFAULT_SOURCE_UNIT,
    FINS_MAX_DATAGRAM,
    FINS_NETWORK_MAX,
    FINS_NODE_DERIVED_MAX,
    FINS_NODE_MAX,
    FINS_SID_BITS,
    FINS_TCP_HEADER_SIZE,
    FINS_TIMER_COUNTER_AREAS,
    FINS_UNIT_MAX,
)
from ...core.validation import check_int16, check_range, check_uint16, require_bool
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import ByteOrder, DataType, PrimitiveValue


def _node_from_host(host: str) -> int:
    """取 IPv4 地址末段作为 FINS 节点号;主机名先解析(内部函数)。

    Omron 以太网 FINS 节点号惯例 = IP 地址最后一段(如
    ``192.168.250.1`` → 节点 1);末段须在以太网 FINS 合法范围 **1~254**
    内(0 非法),超限抛 :class:`ValueError`,提示调用方改用
    显式 ``destination_node``/``source_node``(W342-E1-18「1 to 254」)。
    """
    text = host
    if not text.rsplit(".", 1)[-1].isdigit():
        text = socket.gethostbyname(host)
    node = int(text.rsplit(".", 1)[-1])
    return check_range(node, 1, FINS_NODE_DERIVED_MAX, "从 IP 末段推导的 FINS 节点号")


def _local_ip_for(host: str, port: int) -> str:
    """取本机到目标地址实际出口 IP(UDP connect 探测,不发包,内部函数)。

    UDP socket ``connect`` 只登记对端与选路,不产生网络报文;
    ``getsockname`` 返回的即实际通信将使用的本机侧地址。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, port))
        return probe.getsockname()[0]


class _OmronFinsBase(BaseClient):
    """FINS 客户端公共基类:节点地址、SID 与软元件分发(私有)。"""

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
        # FINS 路由字段范围校验:network 0~127,node 0~254(0 留给"自动"
        # 标记),unit 0~255——越界在构造期显式拒绝,避免到组帧时被
        # ``& 0xFF`` 静默截断发出语义错位的报文。
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
        self._source_unit = check_range(
            int(source_unit), 0, FINS_UNIT_MAX, "源单元号"
        )
        # 记录节点号是否为自动模式(None/0 = 握手或 IP 推导):自动模式每次
        # 连接都刷新为最新推导/握手结果,显式配置的节点号不被覆盖
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
    # 协议原语(BaseClient 类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FINS 读原语:存储区地址 → Area Read(0101)→ 按类型解码(大端)。"""
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                f"T/C 完成标志为单点位,地址不带位号:{address!r}(示例:T0)"
            )
        if data_type is DataType.BOOL:
            return self._read_bit_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            return _words_to_value(self._read_words(parsed, 1), data_type)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return _words_to_value(self._read_words(parsed, 2), data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            return _words_to_value(self._read_words(parsed, 4), data_type)
        raise ValueError(f"FINS 不支持的数据类型:{data_type}")

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
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
                self._write_bits(parsed, [1 if flag else 0])
            else:
                words = self._read_words(parsed._replace(bit=None), 1)
                self._write_words(parsed._replace(bit=None), [convert.set_bit(words[0], parsed.bit or 0, flag)])
            return
        if data_type is DataType.SHORT:
            self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            self._write_words(parsed, [check_uint16(value)])
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            self._write_words(parsed, _value_to_words(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            self._write_words(parsed, _value_to_words(value, data_type))
            return
        raise ValueError(f"FINS 不支持的数据类型:{data_type}")

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """从字区读字符串:逐字大端拼字节后解码(FINS 字序约定)。"""
        parsed = parse_fins_address(address)
        words = self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "big") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """向字区写字符串:编码 → 补齐偶数字节 → 逐字大端。"""
        parsed = parse_fins_address(address)
        raw = convert.encode_string(value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding)
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 批量读取(0104 多存储区读,单事务)
    # ------------------------------------------------------------------

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 0104 多存储区读(单事务)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = self.read_batch([(address, data_type_enum) for address in addresses])
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多存储区批量读取:0104 单事务混读多个非连续字(TCP/UDP 通用)。

        利用 FINS 原生 Multiple Memory Area Read 能力(W342 §5-3-5):
        每条目读 1 个字,软元件/类型可各不相同——32/64 位类型拆成相邻
        多条,BOOL 走包含字的字区码后本地提位(0104 仅字码);T/C 完成
        标志为位区,不支持批量(T/C 当前值可按字批量读)。条目上限
        167(Ethernet/Controller Link 口径)。字符串请用
        :meth:`read_string`(变长不适合混读)。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空/地址或类型非法/条目数超限
        """
        if not items:
            raise ValueError("read_batch 至少需要一个 (地址, 数据类型) 项")
        entries: List[Tuple[int, int]] = []
        # 解码计划:(类别, 字索引, 位号或字数, 数据类型)
        plan: List[Tuple[str, int, int, DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            parsed = parse_fins_address(address)
            if data_type_enum is DataType.BOOL:
                if parsed.area in FINS_TIMER_COUNTER_AREAS:
                    raise ValueError(
                        f"T/C 完成标志不支持批量读取(0104 仅字区):{address!r}"
                    )
                _, word_code = codec.memory_codes(parsed.area, parsed.bank)
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
                raise ValueError(
                    f"FINS 批量读取不支持的数据类型:{data_type_enum}"
                )
            _, word_code = codec.memory_codes(parsed.area, parsed.bank)
            plan.append(("word", len(entries), words, data_type_enum))
            for index in range(words):
                entries.append((word_code, parsed.offset + index))
        codes = [code for code, _ in entries]

        def operation() -> List[PrimitiveValue]:
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
                self._transact(frame), frame, codes
            )
            values: List[PrimitiveValue] = []
            for kind, index, extra, item_type in plan:
                if kind == "wordbit":
                    values.append(bool(convert.get_bit(words[index], extra)))
                else:
                    values.append(_words_to_value(words[index:index + extra], item_type))
            return values

        return self._execute(operation)

    # ------------------------------------------------------------------
    # 位/字原语(0101/0102 命令)
    # ------------------------------------------------------------------

    def _read_bit_impl(self, parsed: FinsAddress) -> bool:
        """位读:位存储区码 + 位地址,CPU 直接返回点位值。"""
        frame = self._build_read(parsed, 1, is_bit=True)
        values = codec.parse_response(
            self._transact(frame), frame, 1, is_bit=True, is_read=True
        )
        return bool(values[0])

    def _read_words(self, parsed: FinsAddress, word_count: int) -> List[int]:
        """字读:字存储区码,返回 0~65535 逐字数据(大端)。"""
        frame = self._build_read(parsed, word_count, is_bit=False)
        return codec.parse_response(
            self._transact(frame), frame, word_count, is_bit=False, is_read=True
        )

    def _write_bits(self, parsed: FinsAddress, values: List[int]) -> None:
        """位写:位存储区码,每点 1 字节 0x00/0x01。"""
        frame = self._build_write(parsed, values, is_bit=True)
        codec.parse_response(
            self._transact(frame), frame, len(values), is_bit=True, is_read=False
        )

    def _write_words(self, parsed: FinsAddress, words: List[int]) -> None:
        """字写:字存储区码,逐字大端。"""
        frame = self._build_write(parsed, words, is_bit=False)
        codec.parse_response(
            self._transact(frame), frame, len(words), is_bit=False, is_read=False
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
    def _transact(self, fins_frame: bytes) -> bytes:
        """发送 FINS 帧并返回 FINS 帧响应(走线封装由子类处理)。"""

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """由走线子类实现。"""


class OmronFinsTcpClient(_OmronFinsBase):
    """欧姆龙 FINS/TCP 客户端。

    连接建立后先执行 FINS/TCP 握手(节点地址分配):未手工配置的
    本地节点/源节点/目标节点自动取握手分配值,随后与 UDP 共用同一套
    FINS 帧格式。
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
        :param local_node: 本地节点号;``None``/``0`` = 由 PLC 自动分配
            (握手时获取)
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号;``None``/``0`` = 握手自动获取;
            手工配置常用 PLC IP 地址末位
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号;``None``/``0`` = 握手自动获取;
            手工配置常用本机 IP 地址末位
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

    def _after_connect(self) -> None:
        """FINS/TCP 握手:发送节点分配请求并解析响应(内部方法)。

        自动模式(构造时节点号传 None/0)每次重连都刷新为最新握手分配值;
        显式配置的节点号保持不被覆盖。
        """
        transport = self._require_transport()
        transport.send(codec.build_handshake(self._local_node))
        head = transport.recv(FINS_TCP_HEADER_SIZE)
        frame = head + transport.recv(codec.parse_tcp_head(head))
        local_node, plc_node = codec.parse_handshake_response(frame)
        if self._auto_local_node:
            self._local_node = local_node
        if self._auto_source_node:
            self._source_node = local_node
        if self._auto_destination_node:
            self._destination_node = plc_node

    def _transact(self, fins_frame: bytes) -> bytes:
        """FINS/TCP 事务:封装 TCP 头 → 收 8 字节头 → 按长度收 → 校验错误域。"""
        transport = self._require_transport()
        transport.send(codec.build_tcp_frame(fins_frame))
        head = transport.recv(FINS_TCP_HEADER_SIZE)
        content = transport.recv(codec.parse_tcp_head(head))
        codec.extract_tcp_error(content)
        return codec.extract_tcp_payload(content)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class OmronFinsUdpClient(_OmronFinsBase):
    """欧姆龙 FINS/UDP 客户端,无握手,一问一答一数据报。

    节点号缺省自动从 IP 推导(Omron 以太网惯例:节点号 = IP 末段):
    目标节点 = PLC IP 末段,源节点 = 本机出口 IP 末段(连接时探测);
    显式传 ``destination_node``/``source_node`` 则原样使用。
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
        :param port: 端口,FINS 默认 9600
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号;``None``/``0`` = 自动从
            PLC IP 末段推导;显式传值原样使用
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号;``None``/``0`` = 自动从本机出口
            IP 末段推导;显式传值原样使用
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

    def _after_connect(self) -> None:
        """UDP 无握手:节点号自动模式在连接时从 IP 推导(内部方法)。

        目标节点 = PLC IP 末段(主机名先解析);源节点 = 本机对 PLC
        地址实际出口 IP 的末段(UDP connect 探测,与真实通信同一路由)。
        自动模式每次连接都重新推导,显式配置的节点号不被覆盖。
        """
        if self._auto_destination_node:
            self._destination_node = _node_from_host(self._ip_address)
        if self._auto_source_node:
            self._source_node = _node_from_host(
                _local_ip_for(self._ip_address, self._port)
            )

    def _transact(self, fins_frame: bytes) -> bytes:
        """FINS/UDP 事务:一帧一数据报,整包接收。"""
        transport = self._require_transport()
        transport.send(fins_frame)
        return transport.recv(FINS_MAX_DATAGRAM)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _words_to_value(words: List[int], data_type: DataType) -> PrimitiveValue:
    """大端字序列 → 按类型解码(FINS 为大端字序,内部函数)。"""
    return convert.words_to_value(words, data_type, ByteOrder.BIG)


def _value_to_words(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把值编码为大端字序列(内部函数)。"""
    return convert.value_to_words(value, data_type, ByteOrder.BIG)
