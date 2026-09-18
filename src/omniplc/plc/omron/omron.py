"""欧姆龙 FINS 协议客户端(TCP/UDP 走线)。

类继承::

    BaseClient
    ├── OmronFinsTcpClient   FINS 帧 + FINS/TCP 握手(默认端口 9600)
    └── OmronFinsUdpClient   FINS 帧 over UDP(默认端口 9600,无握手)

两走线共享同一套 FINS 帧编解码(:mod:`.codec`),区别仅在 TCP 需要
先做 FINS/TCP 节点分配握手。
"""
from __future__ import annotations

from abc import abstractmethod
from typing import List

from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    FINS_DEFAULT_DESTINATION_NETWORK,
    FINS_DEFAULT_DESTINATION_NODE,
    FINS_DEFAULT_DESTINATION_UNIT,
    FINS_DEFAULT_PORT,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, PrimitiveValue
from .address import FinsAddress, parse_fins_address


class _OmronFinsBase(BaseClient):
    """FINS 客户端公共基类:节点地址与软元件分发(私有)。"""

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
        :param destination_node: 目标节点号(0 = PLC 侧,常用)
        :param destination_unit: 目标单元号(0 = CPU)
        :param source_network: 源网络号(上位机侧,一般 0)
        :param source_node: 源节点号(上位机侧,一般 0)
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

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FINS 读原语:存储区地址 → Area Read(0101)→ 按类型解码。"""
        parsed = parse_fins_address(address)
        if data_type is DataType.BOOL:
            return self._read_bit_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            data = self._read_words(parsed, 1)
            return data[0] if data_type is DataType.USHORT else _to_int16(data[0])
        raise NotImplementedError("FINS 多字节数据类型读取将在下一阶段实现")

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """FINS 写原语:Area Write(0102)。"""
        raise NotImplementedError("FINS 写入将在下一阶段实现")

    def _read_bit_impl(self, parsed: FinsAddress) -> bool:
        """位读(CIO/W/H 位区)。"""
        raise NotImplementedError("FINS 位读取将在下一阶段实现")

    def _read_words(self, parsed: FinsAddress, word_count: int) -> List[int]:
        """字读(D/W/H 等),返回 0~65535 逐字数据。"""
        raise NotImplementedError("FINS 字读取将在下一阶段实现")

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """由走线子类实现。"""


class OmronFinsTcpClient(_OmronFinsBase):
    """欧姆龙 FINS/TCP 客户端。

    连接建立后先执行 FINS/TCP 握手(节点地址分配),随后与 UDP
    共用同一套 FINS 帧格式。
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
        """FINS/TCP 握手:发送节点分配请求并解析响应。"""
        raise NotImplementedError("FINS/TCP 握手将在下一阶段实现")

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

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


def _to_int16(raw: int) -> int:
    """0~65535 原始字 → 有符号 16 位(FINS 为大端,内部函数)。"""
    return raw - 0x10000 if raw >= 0x8000 else raw
