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

import struct
from abc import abstractmethod
from typing import List

from . import codec
from .address import FinsAddress, parse_fins_address
from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    FINS_BIT_WRITABLE_AREAS,
    FINS_DEFAULT_DESTINATION_NETWORK,
    FINS_DEFAULT_DESTINATION_NODE,
    FINS_DEFAULT_DESTINATION_UNIT,
    FINS_DEFAULT_PORT,
    FINS_MAX_DATAGRAM,
    FINS_TCP_HEADER_SIZE,
    FINS_TIMER_COUNTER_AREAS,
)
from ...core.validation import (
    check_int16,
    check_range,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import ByteOrder, DataType, PrimitiveValue


class _OmronFinsBase(BaseClient):
    """FINS 客户端公共基类:节点地址、SID 与软元件分发(私有)。"""

    def __init__(
        self,
        ip_address: str,
        port: int = FINS_DEFAULT_PORT,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: int = FINS_DEFAULT_DESTINATION_NODE,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = 0,
        source_node: int = 0,
        source_unit: int = 0,
    ) -> None:
        """初始化 FINS 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,FINS 默认 9600
        :param destination_network: 目标网络号(0 = 本网络)
        :param destination_node: 目标节点号(0 = 握手自动获取;
            手工配置常用 PLC IP 地址末位)
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号(0 = 握手自动获取;手工配置常用
            本机 IP 地址末位)
        :param source_unit: 源单元号(上位机为 0)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._destination_network = int(destination_network)
        self._destination_node = int(destination_node)
        self._destination_unit = int(destination_unit)
        self._source_network = int(source_network)
        self._source_node = int(source_node)
        self._source_unit = int(source_unit)
        self._sid = 0

    def _next_sid(self) -> int:
        """SID 递增(0~255 回绕,事务标识,内部方法)。"""
        self._sid = (self._sid + 1) & 0xFF
        return self._sid

    # ------------------------------------------------------------------
    # 协议原语(BaseClient 类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FINS 读原语:存储区地址 → Area Read(0101)→ 按类型解码(大端)。"""
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                "T/C 完成标志为单点位,地址不带位号:{!r}(示例:T0)".format(address)
            )
        if data_type is DataType.BOOL:
            return self._read_bit_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            return _words_to_value(self._read_words(parsed, 1), data_type)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return _words_to_value(self._read_words(parsed, 2), data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            return _words_to_value(self._read_words(parsed, 4), data_type)
        raise ValueError("FINS 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """FINS 写原语:Area Write(0102)。

        位区(CIO/W/H/A)直接位写;字区(D/EM)按位写用读-改-写。
        """
        parsed = parse_fins_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if parsed.area in FINS_TIMER_COUNTER_AREAS and parsed.bit is not None:
            raise ValueError(
                "T/C 完成标志为单点位,地址不带位号:{!r}(示例:T0)".format(address)
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
        raise ValueError("FINS 不支持的数据类型:{}".format(data_type))

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
    # 位/字原语(0101/0102 命令)
    # ------------------------------------------------------------------

    def _read_bit_impl(self, parsed: FinsAddress) -> bool:
        """位读:位存储区码 + 位地址,CPU 直接返回点位值。"""
        frame = self._build_read(parsed, 1, is_bit=True)
        values = codec.parse_response(self._transact(frame), 1, is_bit=True, is_read=True)
        return bool(values[0])

    def _read_words(self, parsed: FinsAddress, word_count: int) -> List[int]:
        """字读:字存储区码,返回 0~65535 逐字数据(大端)。"""
        frame = self._build_read(parsed, word_count, is_bit=False)
        return codec.parse_response(
            self._transact(frame), word_count, is_bit=False, is_read=True
        )

    def _write_bits(self, parsed: FinsAddress, values: List[int]) -> None:
        """位写:位存储区码,每点 1 字节 0x00/0x01。"""
        frame = self._build_write(parsed, values, is_bit=True)
        codec.parse_response(self._transact(frame), len(values), is_bit=True, is_read=False)

    def _write_words(self, parsed: FinsAddress, words: List[int]) -> None:
        """字写:字存储区码,逐字大端。"""
        frame = self._build_write(parsed, words, is_bit=False)
        codec.parse_response(self._transact(frame), len(words), is_bit=False, is_read=False)

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
        local_node: int = 0,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: int = FINS_DEFAULT_DESTINATION_NODE,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = 0,
        source_node: int = 0,
        source_unit: int = 0,
    ) -> None:
        """初始化 FINS/TCP 客户端。

        :param local_node: 本地节点号,0 = 由 PLC 自动分配(握手时获取)
        :param ip_address: PLC 的 IP
        :param port: 端口,默认 9600
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
        self._local_node = int(local_node)

    @property
    def local_node(self) -> int:
        """本地节点号(自动分配时在握手后可用)。"""
        return self._local_node

    def _after_connect(self) -> None:
        """FINS/TCP 握手:发送节点分配请求并解析响应(内部方法)。"""
        transport = self._require_transport()
        transport.send(codec.build_handshake(self._local_node))
        head = transport.recv(FINS_TCP_HEADER_SIZE)
        frame = head + transport.recv(codec.parse_tcp_head(head))
        local_node, plc_node = codec.parse_handshake_response(frame)
        if self._local_node == 0:
            self._local_node = local_node
        if self._source_node == 0:
            self._source_node = local_node
        if self._destination_node == 0:
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
    """欧姆龙 FINS/UDP 客户端,无握手,一问一答一数据报。"""

    def __init__(
        self,
        ip_address: str = "192.168.250.1",
        port: int = FINS_DEFAULT_PORT,
        destination_network: int = FINS_DEFAULT_DESTINATION_NETWORK,
        destination_node: int = FINS_DEFAULT_DESTINATION_NODE,
        destination_unit: int = FINS_DEFAULT_DESTINATION_UNIT,
        source_network: int = 0,
        source_node: int = 0,
        source_unit: int = 0,
    ) -> None:
        """初始化 FINS/UDP 客户端,参数说明见 :class:`_OmronFinsBase`。"""
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
    raw = convert.words_to_bytes(words, ByteOrder.BIG)
    if data_type is DataType.FLOAT:
        return struct.unpack(">f", raw)[0]
    if data_type is DataType.DOUBLE:
        return struct.unpack(">d", raw)[0]
    signed = data_type in (DataType.SHORT, DataType.INT, DataType.LONG)
    return int.from_bytes(raw, "big", signed=signed)


def _value_to_words(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把值编码为大端字序列(内部函数)。"""
    if data_type is DataType.SHORT:
        return [check_int16(value)]
    if data_type is DataType.USHORT:
        return [check_uint16(value)]
    if data_type is DataType.FLOAT:
        raw = struct.pack(">f", require_float(value))
    elif data_type is DataType.DOUBLE:
        raw = struct.pack(">d", require_float(value))
    else:
        number = require_int(value)
        if data_type is DataType.INT:
            check_range(number, -2147483648, 2147483647, "int")
            raw = number.to_bytes(4, "big", signed=True)
        elif data_type is DataType.UINT:
            check_range(number, 0, 4294967295, "uint")
            raw = number.to_bytes(4, "big", signed=False)
        elif data_type is DataType.LONG:
            check_range(number, -9223372036854775808, 9223372036854775807, "long")
            raw = number.to_bytes(8, "big", signed=True)
        else:
            check_range(number, 0, 18446744073709551615, "ulong")
            raw = number.to_bytes(8, "big", signed=False)
    return convert.bytes_to_words(raw, ByteOrder.BIG)
