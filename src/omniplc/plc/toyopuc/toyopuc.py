"""丰田 TOYOPUC 计算机链接客户端(TCP/UDP)。

依据状态:**TOYOPUC PC Link 官方手册未收录**(`docs/protocol/README.md`
「待补」),帧格式/命令码/软元件基址表经**同源参考实现 `同源参考实现`
4.2.0 双向裁决**逐项一致(帧 `00 00 LL LH CMD` / `80 RC LL LH CMD`、
`FT_COMMAND=0x00`/`FT_RESPONSE=0x80`、CMD 1C/1D/1E/1F/20/21、位/字/字节
基址表均相同;裁决记入 `docs/architecture.md` §8.1),官方手册拿到后可再核。
已知未实现面(均因缺官方手册未做,不臆造):

- 扩展区命令 CMD=0x94/0x95、PC10 CMD=0xC2~0xC6;
- 多站/中继命令 CMD=0x60/0x61;
- PLC 状态与错误日志查询 CMD=0x70/0x7E。

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

v0.7 覆盖基础软元件区(字 S/N/R/D/B,位 P/K/V/T/C/L/X/Y/M)。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple, Union

from ...core import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    TOYOPUC_DEFAULT_PORT,
    TOYOPUC_FRAME_HEADER_SIZE,
    TOYOPUC_MAX_BYTE_COUNT,
    TOYOPUC_MAX_DATAGRAM,
    TOYOPUC_MAX_WORD_COUNT,
    TOYOPUC_WORD_DEVICES,
    UINT32_MAX,
    UINT64_MAX,
)
from ...core.debug import format_hex
from ...core.errors import ProtocolFrameError
from ...core.validation import (
    check_int16,
    check_range,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...core.types import DataType, PrimitiveValue
from . import codec
from .address import (
    ToyopucAddress,
    encode_bit_address,
    encode_byte_address,
    encode_word_address,
    parse_toyopuc_address,
)
from ...core.i18n import _


class _ToyopucBase(BaseClient):
    """TOYOPUC 计算机链接客户端基类:类型分发与帧事务(私有)。"""

    # ------------------------------------------------------------------
    # 帧事务(由走线子类决定收包方式)
    # ------------------------------------------------------------------

    def _transact(self, frame: bytes, expected_size: Optional[int] = None) -> bytes:
        """发送命令帧并返回校验后的响应数据(内部方法)。

        :param frame: 完整命令帧(:mod:`.codec` 的 ``build_*`` 系列构造,
            帧内下标 4 为命令字,响应校验据此回显)
        :param expected_size: 期望响应数据长度;None 表示不校验
        :raises ProtocolFrameError: 响应无效
        :raises DeviceError: PLC 返回出错代码
        """
        transport = self._require_transport()
        if transport.datagram:
            # 数据报走线发送前排空陈旧帧:上一事务超时后迟到的响应会被
            # 本轮 recv 误当应答;TOYOPUC 无事务号,同命令迟到帧可绕过
            # 命令回显校验造成静默错值,排空是唯一防线(陈旧帧防护)
            transport.drain()
        transport.send(frame)
        if transport.datagram:
            raw = transport.recv(TOYOPUC_MAX_DATAGRAM)
        else:
            header = transport.recv(TOYOPUC_FRAME_HEADER_SIZE)
            length = header[2] | (header[3] << 8)
            if length < 1:
                raise ProtocolFrameError(
                    _("TOYOPUC 响应帧长非法:{}(收到的原始帧头:{})").format(
                        length, format_hex(header)
                    )
                )
            if length > TOYOPUC_MAX_DATAGRAM:
                raise ProtocolFrameError(
                    _("TOYOPUC 响应帧长超限:{} > {}(收到的原始帧头:{})").format(
                        length, TOYOPUC_MAX_DATAGRAM, format_hex(header)
                    )
                )
            raw = header + transport.recv(length)
        resp_cmd, rc, resp_data = codec.parse_response(raw)
        return codec.check_response(
            resp_cmd, rc, resp_data, frame[4], expected_size, raw
        )

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
                _(
                    "{!r} 为字节访问地址(L/H 后缀),数值类型需要字访问(无后缀或 W)"
                ).format(address)
            )
        if data_type in (DataType.SHORT, DataType.USHORT):
            words = self._read_words(parsed, 1)
            return (
                words[0]
                if data_type is DataType.USHORT
                else convert.to_signed(words[0], 16)
            )
        if data_type in (DataType.INT, DataType.UINT):
            raw = self._read_raw(parsed, 4)
            return convert.to_signed(raw, 32) if data_type is DataType.INT else raw
        if data_type is DataType.FLOAT:
            return struct.unpack(
                "<f", convert.words_to_bytes(self._read_words(parsed, 2))
            )[0]
        if data_type in (DataType.LONG, DataType.ULONG):
            raw = self._read_raw(parsed, 8)
            return convert.to_signed(raw, 64) if data_type is DataType.LONG else raw
        if data_type is DataType.DOUBLE:
            return struct.unpack(
                "<d", convert.words_to_bytes(self._read_words(parsed, 4))
            )[0]
        raise ValueError(_("TOYOPUC 不支持的数据类型:{}").format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到位/字写入原语。"""
        parsed = parse_toyopuc_address(address)
        if data_type is DataType.BOOL:
            self._write_bit(parsed, require_bool(value))
            return
        if parsed.unit != "word":
            raise ValueError(
                _(
                    "{!r} 为字节访问地址(L/H 后缀),数值类型需要字访问(无后缀或 W)"
                ).format(address)
            )
        if data_type is DataType.SHORT:
            self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            self._write_words(parsed, [check_uint16(value)])
            return
        if data_type is DataType.INT:
            number = check_range(require_int(value), INT32_MIN, INT32_MAX, "int")
            self._write_raw(parsed, number, 4)
            return
        if data_type is DataType.UINT:
            number = check_range(require_int(value), 0, UINT32_MAX, "uint")
            self._write_raw(parsed, number, 4)
            return
        if data_type is DataType.FLOAT:
            number_f = require_float(value)
            try:
                raw = struct.pack("<f", number_f)
            except (OverflowError, ValueError) as exc:
                raise ValueError(_("float 超出 float32 范围:{}").format(value)) from exc
            self._write_words(parsed, convert.bytes_to_words(raw))
            return
        if data_type is DataType.LONG:
            number = check_range(require_int(value), INT64_MIN, INT64_MAX, "long")
            self._write_raw(parsed, number, 8)
            return
        if data_type is DataType.ULONG:
            number = check_range(require_int(value), 0, UINT64_MAX, "ulong")
            self._write_raw(parsed, number, 8)
            return
        if data_type is DataType.DOUBLE:
            number_f = require_float(value)
            try:
                raw = struct.pack("<d", number_f)
            except (OverflowError, ValueError) as exc:
                raise ValueError(
                    _("double 超出 float64 范围:{}").format(value)
                ) from exc
            self._write_words(parsed, convert.bytes_to_words(raw))
            return
        raise ValueError(_("TOYOPUC 不支持的数据类型:{}").format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:从字区编号起连续字节读(CMD=1E,``H`` 后缀从高字节起)。"""
        parsed = self._require_byte_range(address, length)
        data = self._transact(
            codec.build_byte_read(encode_byte_address(parsed), length), length
        )
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码后从字区编号起连续字节写(CMD=1F)。"""
        try:
            raw = value.encode(encoding)
        except UnicodeEncodeError as exc:
            raise ValueError(
                _("字符串按 {} 编码失败:{}").format(encoding, exc)
            ) from exc
        if not 1 <= len(raw) <= TOYOPUC_MAX_BYTE_COUNT:
            raise ValueError(
                _("字符串编码后必须在 1~{} 字节,收到:{}").format(
                    TOYOPUC_MAX_BYTE_COUNT, len(raw)
                )
            )
        parsed = self._require_byte_range(address, len(raw))
        self._transact(codec.build_byte_write(encode_byte_address(parsed), raw))
        return value

    # ------------------------------------------------------------------
    # 位/字原语(核心命令 + 帧封装)
    # ------------------------------------------------------------------

    def _read_bit(self, parsed: ToyopucAddress) -> bool:
        """单位读(CMD=20),仅位软元件。"""
        if parsed.unit != "bit":
            raise ValueError(
                _("布尔读写在 TOYOPUC 上仅支持位软元件(P/K/V/T/C/L/X/Y/M)")
            )
        data = self._transact(codec.build_bit_read(encode_bit_address(parsed)), 1)
        return data[0] != 0

    def _write_bit(self, parsed: ToyopucAddress, value: bool) -> None:
        """单位写(CMD=21),仅位软元件。"""
        if parsed.unit != "bit":
            raise ValueError(
                _("布尔读写在 TOYOPUC 上仅支持位软元件(P/K/V/T/C/L/X/Y/M)")
            )
        self._transact(codec.build_bit_write(encode_bit_address(parsed), value))

    def _read_words(self, parsed: ToyopucAddress, count: int) -> List[int]:
        """连续字读(CMD=1C),返回 0~65535 原始字列表(内部方法)。"""
        data = self._transact(
            codec.build_word_read(encode_word_address(parsed), count), count * 2
        )
        return codec.unpack_u16(data)

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:字软元件起连续 ``count`` 个元素,连续字读 CMD=1C 单事务。

        TOYOPUC 计算机链接的连续读命令为 CMD=1C(字区,1~512 字,
        :data:`TOYOPUC_MAX_WORD_COUNT`);位软元件只有单位读 CMD=20(单点,
        无批量位读命令),BOOL 连续读不支持。16 位类型 1 字/元素、32 位
        2 字、64 位 4 字(多字数据小端、低字在前,TOYOPUC 原生序)。

        :param address: 起始字软元件地址(如 ``"D0100"``;``L/H/W`` 后缀
            访问不支持 range)
        :param count: 元素个数(按 ``data_type`` 计,FLOAT×10 = 20 字)
        :param data_type: 数据类型(数值类型)
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 位软元件或
            字节访问地址 / 字数超限
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        data_type_enum = DataType.coerce(data_type)
        if data_type_enum is DataType.STRING:
            raise ValueError(_("read_range 不支持 STRING,请用 read_string"))
        parsed = parse_toyopuc_address(address)
        if data_type_enum is DataType.BOOL or parsed.unit != "word":
            raise ValueError(
                _(
                    "TOYOPUC read_range 仅支持字软元件数值类型连续读(位软元件无批量"
                    "位读命令,L/H/W 后缀地址不支持),收到:{!r}"
                ).format(address)
            )
        width = 1
        if data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
            width = 2
        elif data_type_enum in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            width = 4
        if count * width > TOYOPUC_MAX_WORD_COUNT:
            raise ValueError(
                _("TOYOPUC read_range 字数超上限 {}:{}×{}={}").format(
                    TOYOPUC_MAX_WORD_COUNT, count, width, count * width
                )
            )

        def operation() -> List[PrimitiveValue]:
            words = self._read_words(parsed, count * width)
            values: List[PrimitiveValue] = []
            for index in range(count):
                chunk = words[index * width : (index + 1) * width]
                if data_type_enum in (DataType.SHORT, DataType.USHORT):
                    values.append(
                        chunk[0]
                        if data_type_enum is DataType.USHORT
                        else convert.to_signed(chunk[0], 16)
                    )
                elif data_type_enum in (DataType.INT, DataType.UINT):
                    raw = int.from_bytes(convert.words_to_bytes(chunk), "little")
                    values.append(
                        raw
                        if data_type_enum is DataType.UINT
                        else convert.to_signed(raw, 32)
                    )
                elif data_type_enum is DataType.FLOAT:
                    values.append(struct.unpack("<f", convert.words_to_bytes(chunk))[0])
                elif data_type_enum in (DataType.LONG, DataType.ULONG):
                    raw = int.from_bytes(convert.words_to_bytes(chunk), "little")
                    values.append(
                        raw
                        if data_type_enum is DataType.ULONG
                        else convert.to_signed(raw, 64)
                    )
                else:
                    values.append(struct.unpack("<d", convert.words_to_bytes(chunk))[0])
            return values

        ok, values = self._execute(operation)
        if not ok or values is None:
            return False, None
        return True, values

    def _write_words(self, parsed: ToyopucAddress, words: List[int]) -> None:
        """连续字写(CMD=1D,内部方法)。"""
        self._transact(codec.build_word_write(encode_word_address(parsed), words))

    def _read_raw(self, parsed: ToyopucAddress, byte_count: int) -> int:
        """连续字读并拼为小端原始整数(32/64 位,内部方法)。"""
        words = self._read_words(parsed, byte_count // 2)
        return int.from_bytes(convert.words_to_bytes(words), "little")

    def _write_raw(self, parsed: ToyopucAddress, raw: int, byte_count: int) -> None:
        """原始整数按小端拆字后连续字写(32/64 位,内部方法)。

        负值按二补数转为无符号后再编码(``to_bytes`` 拒绝负数)——调用点
        (:meth:`_write`)已按有符号类型界校验过范围,此处仅做等值编码。
        """
        unsigned = raw & ((1 << (byte_count * 8)) - 1)
        self._write_words(
            parsed, convert.bytes_to_words(unsigned.to_bytes(byte_count, "little"))
        )

    @staticmethod
    def _require_byte_range(address: str, length: int) -> ToyopucAddress:
        """字符串存取的地址与长度校验,返回字节访问地址(内部方法)。

        无后缀按低字节(``L``)起;``H`` 后缀从高字节起。
        """
        if length > TOYOPUC_MAX_BYTE_COUNT:
            raise ValueError(
                _("字符串长度超出 {} 字节上限:{}").format(
                    TOYOPUC_MAX_BYTE_COUNT, length
                )
            )
        parsed = parse_toyopuc_address(address)
        if parsed.area not in TOYOPUC_WORD_DEVICES:
            raise ValueError(
                _("字符串只能从字软元件(S/N/R/D/B)存取,收到:{!r}").format(address)
            )
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
        """初始化 TOYOPUC UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 计算机链接端口,默认 1025
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)
