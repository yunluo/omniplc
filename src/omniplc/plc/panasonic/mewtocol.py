"""松下 MEWTOCOL-COM 客户端(TCP/UDP 走线)。

类继承::

    BaseClient
    ├── _MewtocolBase(私有公共基类:命令组装/响应收切/类型编解码)
    │   ├── PanasonicMewtocolTcpClient   MEWTOCOL over TCP(默认端口 1024)
    │   └── PanasonicMewtocolUdpClient   MEWTOCOL over UDP(一问一答一数据报)

地址语法见 :mod:`.address`:接点区 ``X/Y/R/T/C/L``(位,字号+位号,如
``R000F``/``R1.15``),数据区 ``D/L/F/S/K``(字,十进制,如 ``D100``;
``D100.3`` 走读-改-写)。定时器/计数器:接点用 ``T``/``C``,当前值用
``K``(经过值 EV)/``S``(设定值 SV)。

帧格式见 :mod:`.codec_mewtocol`(ASCII 文本帧,BCC 异或校验,无 ETX)。
TCP 按响应头(4 字节)判断正常/错误后精确收齐;UDP 一次收整包校验。
"""
from __future__ import annotations

import struct
from abc import abstractmethod
from typing import List, Sequence

from . import codec_mewtocol
from .address import MewtocolAddress, parse_mewtocol_address
from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    MEWTOCOL_CONTACT_AREAS,
    MEWTOCOL_DEFAULT_PORT,
    MEWTOCOL_DEFAULT_STATION,
    MEWTOCOL_MAX_DATAGRAM,
)
from ...core.errors import ProtocolFrameError
from ...core.validation import check_int16, check_uint16, require_bool, require_float, require_int
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, PrimitiveValue


class _MewtocolBase(BaseClient):
    """MEWTOCOL 客户端公共基类:站号管理与软元件地址分发(私有)。"""

    def __init__(
        self,
        ip_address: str,
        port: int,
        station: int = MEWTOCOL_DEFAULT_STATION,
    ) -> None:
        """初始化 MEWTOCOL 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(以太网 MEWTOCOL 默认 1024,以模块设置为准)
        :param station: 站号(1~99,编程口直连场景 0xEE)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._station_text = codec_mewtocol.station_text(station)
        self._station = int(station)

    @property
    def station(self) -> int:
        """当前站号(0xEE = 直连)。"""
        return self._station

    # ------------------------------------------------------------------
    # 协议原语(BaseClient 类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """MEWTOCOL 读原语:接点走 RCS,数据区走 RD。"""
        parsed = parse_mewtocol_address(address, data_type is DataType.BOOL)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
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
        raise ValueError("MEWTOCOL 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """MEWTOCOL 写原语:接点走 WCS,数据区走 WD(位写用读-改-写)。"""
        parsed = parse_mewtocol_address(address, data_type is DataType.BOOL)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            if parsed.area in MEWTOCOL_CONTACT_AREAS:
                self._transact(
                    codec_mewtocol.build_write_contact(
                        self._station_text, parsed.area, parsed.word, parsed.bit or 0, flag
                    ),
                    0,
                    "WC",
                )
            else:
                words = self._read_words(parsed, 1)
                self._write_words(
                    parsed, [_set_bit(words[0], parsed.bit or 0, flag)]
                )
            return
        if data_type is DataType.SHORT:
            self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            self._write_words(parsed, [check_uint16(value)])
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            self._write_words(parsed, _encode_32(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            self._write_words(parsed, _encode_64(value, data_type))
            return
        raise ValueError("MEWTOCOL 不支持的数据类型:{}".format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """从数据区读字符串:逐字高字节在前拼字节后解码(MEWTOCOL 字内字节序)。"""
        parsed = parse_mewtocol_address(address, False)
        if parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        words = self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "big") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """向数据区写字符串:编码 → 补齐偶数字节 → 逐字高字节在前。"""
        parsed = parse_mewtocol_address(address, False)
        if parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 位/字原语(命令组装 + 收发)
    # ------------------------------------------------------------------

    def _read_bool_impl(self, parsed: MewtocolAddress) -> bool:
        """接点区 RCS 单点读;数据区读 1 字后按位提取。"""
        if parsed.area in MEWTOCOL_CONTACT_AREAS:
            data_text = self._transact(
                codec_mewtocol.build_read_contact(
                    self._station_text, parsed.area, parsed.word, parsed.bit or 0
                ),
                1,
                "RC",
            )
            if data_text not in ("0", "1"):
                raise ProtocolFrameError(
                    "MEWTOCOL 接点读响应非法:{!r}".format(data_text)
                )
            return data_text == "1"
        words = self._read_words(parsed, 1)
        return _get_bit(words[0], parsed.bit or 0)

    def _read_words(self, parsed: MewtocolAddress, word_count: int) -> List[int]:
        """RD 成批读字软元件,返回 0~65535 逐字数据(高字节在前编码)。"""
        data_text = self._transact(
            codec_mewtocol.build_read_words(self._station_text, parsed.area, parsed.word, word_count),
            word_count * 4,
            "RD",
        )
        if len(data_text) != word_count * 4:
            raise ProtocolFrameError(
                "MEWTOCOL 读响应数据不足:期望 {} 字符,实际 {}".format(
                    word_count * 4, len(data_text)
                )
            )
        return [int(data_text[i:i + 4], 16) for i in range(0, len(data_text), 4)]

    def _write_words(self, parsed: MewtocolAddress, words: List[int]) -> None:
        """WD 成批写字软元件(逐字 4 位十六进制、高字节在前)。"""
        self._transact(
            codec_mewtocol.build_write_words(self._station_text, parsed.area, parsed.word, words),
            0,
            "WD",
        )

    def _transact(self, request: bytes, data_chars: int, command: str) -> str:
        """发送请求并接收完整响应,返回数据文本(内部方法)。

        TCP:先收 4 字节头判断正常/错误,再精确收齐余量;
        UDP:一次 recv 整包,长度由解析层校验。
        """
        transport = self._require_transport()
        transport.send(request)
        if transport.datagram:
            response = transport.recv(MEWTOCOL_MAX_DATAGRAM)
        else:
            head = transport.recv(4)
            if head[3:4] == b"!":
                response = head + transport.recv(5)
            else:
                total = codec_mewtocol.parse_expected_size(command, data_chars)
                response = head + transport.recv(total - 4)
        return codec_mewtocol.parse_response(response, self._station_text, command)

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """由走线子类实现。"""


class PanasonicMewtocolTcpClient(_MewtocolBase):
    """松下 MEWTOCOL 客户端(TCP 走线)。

    :example: ``client = PanasonicMewtocolTcpClient("192.168.0.10", 1024, station=1)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MEWTOCOL_DEFAULT_PORT,
        station: int = MEWTOCOL_DEFAULT_STATION,
    ) -> None:
        """初始化 MEWTOCOL TCP 客户端,参数说明见 :class:`_MewtocolBase`。"""
        super().__init__(ip_address, port, station)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class PanasonicMewtocolUdpClient(_MewtocolBase):
    """松下 MEWTOCOL 客户端(UDP 走线),帧格式与 TCP 相同,一问一答一数据报。

    :example: ``client = PanasonicMewtocolUdpClient("192.168.0.10", 1024, station=1)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MEWTOCOL_DEFAULT_PORT,
        station: int = MEWTOCOL_DEFAULT_STATION,
    ) -> None:
        """初始化 MEWTOCOL UDP 客户端,参数说明见 :class:`_MewtocolBase`。"""
        super().__init__(ip_address, port, station)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _to_int16(raw: int) -> int:
    """0~65535 原始字 → 有符号 16 位(内部函数)。"""
    return raw - 0x10000 if raw >= 0x8000 else raw


def _get_bit(word: int, bit: int) -> bool:
    """取字的指定位(内部函数)。"""
    return bool((word >> bit) & 0x01)


def _set_bit(word: int, bit: int, flag: bool) -> int:
    """置/清字的指定位(内部函数)。"""
    return (word | (1 << bit)) if flag else (word & ~(1 << bit) & 0xFFFF)


def _decode_32(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """两字数据按类型解码:低字在前、字内高字节在前(内部函数)。"""
    raw = data[1].to_bytes(2, "big") + data[0].to_bytes(2, "big")
    if data_type is DataType.INT:
        return int.from_bytes(raw, "big", signed=True)
    if data_type is DataType.UINT:
        return int.from_bytes(raw, "big", signed=False)
    return struct.unpack(">f", raw)[0]


def _decode_64(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """四字数据按类型解码:低字在前、字内高字节在前(内部函数)。"""
    raw = b"".join(word.to_bytes(2, "big") for word in reversed(data))
    if data_type is DataType.LONG:
        return int.from_bytes(raw, "big", signed=True)
    if data_type is DataType.ULONG:
        return int.from_bytes(raw, "big", signed=False)
    return struct.unpack(">d", raw)[0]


def _encode_32(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 32 位值编码为 2 个字:低字在前、字内高字节在前(内部函数)。"""
    if data_type is DataType.FLOAT:
        raw = struct.pack(">f", require_float(value))
    else:
        number = require_int(value)
        if data_type is DataType.INT:
            if not -2147483648 <= number <= 2147483647:
                raise ValueError("int 超出 32 位范围:{}".format(number))
            raw = number.to_bytes(4, "big", signed=True)
        else:
            if not 0 <= number <= 4294967295:
                raise ValueError("uint 超出 32 位范围:{}".format(number))
            raw = number.to_bytes(4, "big", signed=False)
    return [int.from_bytes(raw[2:4], "big"), int.from_bytes(raw[0:2], "big")]


def _encode_64(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 64 位值编码为 4 个字:低字在前、字内高字节在前(内部函数)。"""
    if data_type is DataType.DOUBLE:
        raw = struct.pack(">d", require_float(value))
    else:
        number = require_int(value)
        if data_type is DataType.LONG:
            if not -9223372036854775808 <= number <= 9223372036854775807:
                raise ValueError("long 超出 64 位范围:{}".format(number))
            raw = number.to_bytes(8, "big", signed=True)
        else:
            if not 0 <= number <= 18446744073709551615:
                raise ValueError("ulong 超出 64 位范围:{}".format(number))
            raw = number.to_bytes(8, "big", signed=False)
    return [int.from_bytes(raw[i:i + 2], "big") for i in (6, 4, 2, 0)]
