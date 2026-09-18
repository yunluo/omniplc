"""三菱 MELSEC MC 协议客户端(3E/4E/1E 帧 × TCP/UDP 走线)。

类继承::

    BaseClient
    ├── MelsecMcTcpClient   3E/4E/1E 帧 over TCP(默认端口 2000)
    └── MelsecMcUdpClient   3E/4E/1E 帧 over UDP(默认端口 2000)

两走线共享同一套帧编解码(:mod:`.codec_qna` 与 :mod:`.codec_a`)。
"""
from __future__ import annotations

from abc import abstractmethod
from typing import List, Sequence, Union

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, McFrame, PrimitiveValue
from .address import McAddress, parse_mc_address


class _MelsecMcBase(BaseClient):
    """MC 客户端公共基类:帧型校验、软元件地址分发(私有)。"""

    def __init__(
        self,
        ip_address: str,
        port: int,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._frame = _coerce_frame(frame)
        self._network_number = int(network_number)
        self._pc_number = int(pc_number)

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._frame

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """MC 读原语:软元件地址 → 成批读请求 → 按类型解码。"""
        parsed = parse_mc_address(address)
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            data = self._read_words(parsed, 1)
            return data[0] if data_type is DataType.USHORT else _to_int16(data[0])
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            data = self._read_words(parsed, 2)
            return _decode_32(data, data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            data = self._read_words(parsed, 4)
            return _decode_64(data, data_type)
        raise ValueError("MC 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """MC 写原语:软元件地址 + 编码 → 成批写请求。"""
        parse_mc_address(address)  # 先做地址校验
        if data_type is DataType.BOOL:
            raise NotImplementedError("MC 位写入将在下一阶段实现")
        raise NotImplementedError("MC 字写入将在下一阶段实现")

    def _read_bool_impl(self, parsed: McAddress) -> bool:
        """位软元件读(位软元件直接按点读;字软元件按位提取)。"""
        raise NotImplementedError("MC 位读取将在下一阶段实现")

    def _read_words(self, parsed: McAddress, word_count: int) -> List[int]:
        """成批读字软元件,返回 0~65535 逐字数据。"""
        raise NotImplementedError("MC 成批读将在下一阶段实现")

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """由走线子类实现。"""


class MelsecMcTcpClient(_MelsecMcBase):
    """三菱 MC 客户端(TCP 走线)。

    :example: ``client = MelsecMcTcpClient("192.168.3.39", 2000, frame=McFrame.FRAME_3E)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC TCP 客户端,参数说明见 :class:`_MelsecMcBase`。"""
        super().__init__(ip_address, port, frame, network_number, pc_number)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class MelsecMcUdpClient(_MelsecMcBase):
    """三菱 MC 客户端(UDP 走线),帧格式与 TCP 相同,一问一答一数据报。"""

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC UDP 客户端,参数说明见 :class:`_MelsecMcBase`。"""
        super().__init__(ip_address, port, frame, network_number, pc_number)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _coerce_frame(value: Union[McFrame, str]) -> McFrame:
    """把枚举成员或字符串统一解析为 McFrame(内部函数)。"""
    if isinstance(value, McFrame):
        return value
    try:
        return McFrame(str(value).strip().upper())
    except ValueError:
        supported = "/".join(member.value for member in McFrame)
        raise ValueError("不支持的 MC 帧型:{!r},支持:{}".format(value, supported))


def _to_int16(raw: int) -> int:
    """0~65535 原始字 → 有符号 16 位(内部函数)。"""
    return raw - 0x10000 if raw >= 0x8000 else raw


def _decode_32(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """两字数据按类型解码(MC 为小端字序:低字在前,内部函数)。"""
    raw = b"".join(word.to_bytes(2, "little") for word in data)
    if data_type is DataType.INT:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.UINT:
        return int.from_bytes(raw, "little", signed=False)
    return convert.registers_to_float32(
        [int.from_bytes(word.to_bytes(2, "little"), "big") for word in data],
    )


def _decode_64(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """四字数据按类型解码(小端字序,内部函数)。"""
    raw = b"".join(word.to_bytes(2, "little") for word in data)
    if data_type is DataType.LONG:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.ULONG:
        return int.from_bytes(raw, "little", signed=False)
    return convert.registers_to_float64(
        [int.from_bytes(word.to_bytes(2, "little"), "big") for word in data],
    )
