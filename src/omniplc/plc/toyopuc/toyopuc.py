"""丰田 TOYOPUC 计算机链接客户端(TCP/UDP)。

二进制帧协议:命令 ``00 00 LL LH CMD [数据...]``,响应
``80 RC LL LH CMD [数据...]``;TCP 走线按帧头长度分段收包,
UDP 走线一问一答一数据报。

类继承::

    BaseClient
    ├── ToyopucTcpClient   计算机链接 over TCP(默认端口 1025)
    └── ToyopucUdpClient   计算机链接 over UDP(默认端口 1025)

数据类型映射(多字数据小端、低字在前,TOYOPUC 原生序):

============  ==================================================
DataType      访问方式
============  ==================================================
BOOL          位软元件直读/直写(CMD=20/21)
SHORT/USHORT  连续字读/写 1 字(CMD=1C/1D)
INT/UINT      连续字读/写 2 字(32 位,低字在前)
FLOAT         连续字读/写 2 字拼 float32(低字在前)
LONG/ULONG    连续字读/写 4 字(64 位,低字在前)
DOUBLE        连续字读/写 4 字拼 float64(低字在前)
STRING        连续字节读/写(CMD=1E/1F,从字区编号低字节起)
============  ==================================================

v0.7 覆盖基础软元件区(字 S/N/R/D/B,位 P/K/V/T/C/L/X/Y/M);
扩展区(CMD=94~99)、PC10(CMD=C2~C6)与中继(CMD=60)留待后续版本。
"""
from __future__ import annotations

import struct
from typing import List, Optional

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    TOYOPUC_CMD_BIT_READ,
    TOYOPUC_CMD_BIT_WRITE,
    TOYOPUC_CMD_BYTE_READ,
    TOYOPUC_CMD_BYTE_WRITE,
    TOYOPUC_CMD_WORD_READ,
    TOYOPUC_CMD_WORD_WRITE,
    TOYOPUC_DEFAULT_PORT,
    TOYOPUC_FRAME_HEADER_SIZE,
    TOYOPUC_MAX_BYTE_COUNT,
    TOYOPUC_MAX_DATAGRAM,
    TOYOPUC_WORD_DEVICES,
)
from ...core.errors import OmniPLCInternalError, ProtocolFrameError
from ...core.validation import check_int16, check_uint16, require_bool, require_float, require_int
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, PrimitiveValue
from . import codec
from .address import (
    ToyopucAddress,
    encode_bit_address,
    encode_byte_address,
    encode_word_address,
    parse_toyopuc_address,
)


class _ToyopucBase(BaseClient):
    """TOYOPUC 计算机链接客户端基类:类型分发与帧事务(私有)。"""

    # ------------------------------------------------------------------
    # 帧事务(由走线子类决定收包方式)
    # ------------------------------------------------------------------

    def _transact(self, cmd: int, data: bytes, expected_size: Optional[int]) -> bytes:
        """发送命令帧并返回校验后的响应数据(内部方法)。

        :param expected_size: 期望响应数据长度;None 表示不校验
        :raises ProtocolFrameError: 响应无效
        :raises DeviceError: PLC 返回出错代码
        """
        transport = self._require_transport()
        transport.send(codec.build_command(cmd, data))
        if transport.datagram:
            raw = transport.recv(TOYOPUC_MAX_DATAGRAM)
        else:
            header = transport.recv(TOYOPUC_FRAME_HEADER_SIZE)
            length = header[2] | (header[3] << 8)
            if length < 1:
                raise ProtocolFrameError("TOYOPUC 响应帧长非法:{}".format(length))
            raw = header + transport.recv(length)
        resp_cmd, rc, resp_data = codec.parse_response(raw)
        return codec.check_response(resp_cmd, rc, resp_data, cmd, expected_size)

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到位/字读取原语(TOYOPUC 字序:低字在前)。"""
        parsed = parse_toyopuc_address(address)
        if data_type is DataType.BOOL:
            return self._read_bit(parsed)
        if parsed.unit != "word":
            raise ValueError(
                "{!r} 为字节访问地址(L/H 后缀),数值类型需要字访问(无后缀或 W)".format(address)
            )
        if data_type in (DataType.SHORT, DataType.USHORT):
            words = self._read_words(parsed, 1)
            return words[0] if data_type is DataType.USHORT else _to_signed(words[0], 16)
        if data_type in (DataType.INT, DataType.UINT):
            raw = self._read_raw(parsed, 4)
            return _to_signed(raw, 32) if data_type is DataType.INT else raw
        if data_type is DataType.FLOAT:
            return struct.unpack("<f", _words_to_bytes(self._read_words(parsed, 2)))[0]
        if data_type in (DataType.LONG, DataType.ULONG):
            raw = self._read_raw(parsed, 8)
            return _to_signed(raw, 64) if data_type is DataType.LONG else raw
        if data_type is DataType.DOUBLE:
            return struct.unpack("<d", _words_to_bytes(self._read_words(parsed, 4)))[0]
        raise ValueError("TOYOPUC 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到位/字写入原语。"""
        parsed = parse_toyopuc_address(address)
        if data_type is DataType.BOOL:
            self._write_bit(parsed, require_bool(value))
            return
        if parsed.unit != "word":
            raise ValueError(
                "{!r} 为字节访问地址(L/H 后缀),数值类型需要字访问(无后缀或 W)".format(address)
            )
        if data_type is DataType.SHORT:
            self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            self._write_words(parsed, [check_uint16(value)])
            return
        if data_type is DataType.INT:
            self._write_raw(parsed, require_int(value) & 0xFFFFFFFF, 4)
            return
        if data_type is DataType.UINT:
            number = require_int(value)
            if not 0 <= number <= 0xFFFFFFFF:
                raise ValueError("uint 超出范围 0~4294967295:{}".format(number))
            self._write_raw(parsed, number, 4)
            return
        if data_type is DataType.FLOAT:
            self._write_words(parsed, _bytes_to_words(struct.pack("<f", require_float(value))))
            return
        if data_type is DataType.LONG:
            self._write_raw(parsed, require_int(value) & 0xFFFFFFFFFFFFFFFF, 8)
            return
        if data_type is DataType.ULONG:
            number = require_int(value)
            if not 0 <= number <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError("ulong 超出 64 位无符号范围:{}".format(number))
            self._write_raw(parsed, number, 8)
            return
        if data_type is DataType.DOUBLE:
            self._write_words(parsed, _bytes_to_words(struct.pack("<d", require_float(value))))
            return
        raise ValueError("TOYOPUC 不支持的数据类型:{}".format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:从字区编号起连续字节读(CMD=1E,``H`` 后缀从高字节起)。"""
        parsed = self._require_byte_range(address, length)
        data = self._transact(
            TOYOPUC_CMD_BYTE_READ,
            codec.pack_u16(encode_byte_address(parsed)) + codec.pack_u16(length),
            length,
        )
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码后从字区编号起连续字节写(CMD=1F)。"""
        try:
            raw = value.encode(encoding)
        except UnicodeEncodeError as exc:
            raise ValueError("字符串按 {} 编码失败:{}".format(encoding, exc)) from exc
        if not 1 <= len(raw) <= TOYOPUC_MAX_BYTE_COUNT:
            raise ValueError("字符串编码后必须在 1~{} 字节,收到:{}".format(TOYOPUC_MAX_BYTE_COUNT, len(raw)))
        parsed = self._require_byte_range(address, len(raw))
        self._transact(
            TOYOPUC_CMD_BYTE_WRITE,
            codec.pack_u16(encode_byte_address(parsed)) + raw,
            0,
        )
        return value

    # ------------------------------------------------------------------
    # 位/字原语(核心命令 + 帧封装)
    # ------------------------------------------------------------------

    def _read_bit(self, parsed: ToyopucAddress) -> bool:
        """单位读(CMD=20),仅位软元件。"""
        if parsed.unit != "bit":
            raise ValueError("布尔读写在 TOYOPUC 上仅支持位软元件(P/K/V/T/C/L/X/Y/M)")
        data = self._transact(TOYOPUC_CMD_BIT_READ, codec.pack_u16(encode_bit_address(parsed)), 1)
        return data[0] != 0

    def _write_bit(self, parsed: ToyopucAddress, value: bool) -> None:
        """单位写(CMD=21),仅位软元件。"""
        if parsed.unit != "bit":
            raise ValueError("布尔读写在 TOYOPUC 上仅支持位软元件(P/K/V/T/C/L/X/Y/M)")
        self._transact(TOYOPUC_CMD_BIT_WRITE, codec.pack_u16(encode_bit_address(parsed)) +
                       bytes((1 if value else 0,)), 0)

    def _read_words(self, parsed: ToyopucAddress, count: int) -> List[int]:
        """连续字读(CMD=1C),返回 0~65535 原始字列表(内部方法)。"""
        data = self._transact(
            TOYOPUC_CMD_WORD_READ,
            codec.pack_u16(encode_word_address(parsed)) + codec.pack_u16(count),
            count * 2,
        )
        return codec.unpack_u16(data)

    def _write_words(self, parsed: ToyopucAddress, words: List[int]) -> None:
        """连续字写(CMD=1D,内部方法)。"""
        payload = codec.pack_u16(encode_word_address(parsed)) + b"".join(
            codec.pack_u16(word) for word in words
        )
        self._transact(TOYOPUC_CMD_WORD_WRITE, payload, 0)

    def _read_raw(self, parsed: ToyopucAddress, byte_count: int) -> int:
        """连续字读并拼为小端原始整数(32/64 位,内部方法)。"""
        words = self._read_words(parsed, byte_count // 2)
        return int.from_bytes(_words_to_bytes(words), "little")

    def _write_raw(self, parsed: ToyopucAddress, raw: int, byte_count: int) -> None:
        """原始整数按小端拆字后连续字写(32/64 位,内部方法)。"""
        self._write_words(parsed, _bytes_to_words(raw.to_bytes(byte_count, "little")))

    @staticmethod
    def _require_byte_range(address: str, length: int) -> ToyopucAddress:
        """字符串存取的地址与长度校验,返回字节访问地址(内部方法)。

        无后缀按低字节(``L``)起;``H`` 后缀从高字节起。
        """
        if length > TOYOPUC_MAX_BYTE_COUNT:
            raise ValueError("字符串长度超出 {} 字节上限:{}".format(TOYOPUC_MAX_BYTE_COUNT, length))
        parsed = parse_toyopuc_address(address)
        if parsed.area not in TOYOPUC_WORD_DEVICES:
            raise ValueError("字符串只能从字软元件(S/N/R/D/B)存取,收到:{!r}".format(address))
        return ToyopucAddress(parsed.area, parsed.number, parsed.suffix or "L")


class ToyopucTcpClient(_ToyopucBase):
    """丰田 TOYOPUC 计算机链接 TCP 客户端(默认端口 1025)。

    :example: ``client = ToyopucTcpClient("192.168.0.10", 1025)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """初始化 TOYOPUC TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 计算机链接端口,默认 1025
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class ToyopucUdpClient(_ToyopucBase):
    """丰田 TOYOPUC 计算机链接 UDP 客户端(默认端口 1025,一问一答一数据报)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """初始化 TOYOPUC UDP 客户端,参数同 :class:`ToyopucTcpClient`。"""
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _to_signed(raw: int, bits: int) -> int:
    """无符号原始值 → 有符号整数(内部函数)。"""
    return raw - (1 << bits) if raw >= 1 << (bits - 1) else raw


def _words_to_bytes(words: List[int]) -> bytes:
    """原始字列表按小端拼字节(内部函数)。"""
    return b"".join(word.to_bytes(2, "little") for word in words)


def _bytes_to_words(raw: bytes) -> List[int]:
    """小端字节串拆为 0~65535 原始字列表(内部函数)。"""
    if len(raw) % 2 != 0:
        raise OmniPLCInternalError("字编码字节数必须为偶数:{}".format(len(raw)))
    return [int.from_bytes(raw[index:index + 2], "little") for index in range(0, len(raw), 2)]
