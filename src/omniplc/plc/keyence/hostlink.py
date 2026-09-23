"""基恩士 KV Host Link 客户端(TCP/UDP)。

ASCII 行式协议:命令帧以 CR 结束,响应为一行以 CR/LF 结束的 ASCII 文本。
TCP 走线按行收包(流式),UDP 走线一问一答一数据报。

数据类型映射:

============  ==============================================
DataType      访问方式
============  ==============================================
BOOL          位软元件直读/直写;字软元件位走读-改-写(.U)
SHORT/USHORT  ``RD DM100.S``/``.U``
INT/UINT      ``RD DM100.L``/``.D``(PLC 原生 32 位)
FLOAT         ``RDS DM100.U 2`` 两字小端拼 float32
LONG/ULONG    ``RDS DM100.U 4`` 四字小端拼 64 位整数
DOUBLE        ``RDS DM100.U 4`` 四字小端拼 float64
============  ==============================================
"""
from __future__ import annotations

import socket
import struct
import time
from typing import List

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    KV_DEFAULT_PORT,
    KV_MAX_DATAGRAM,
    KV_MAX_LINE,
)
from ...core.errors import OmniPLCInternalError
from ...core.validation import (
    check_int16,
    check_range,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, PrimitiveValue
from . import codec
from .address import KvAddress, is_bit_device, parse_kv_address


class _KeyenceHostLinkBase(BaseClient):
    """KV Host Link 客户端基类:类型分发与行式事务。"""

    # ------------------------------------------------------------------
    # 行式事务(由走线子类决定收包方式)
    # ------------------------------------------------------------------

    def _transact(self, body: bytes) -> str:
        """发送命令帧并返回一行响应文本(已去除 CR/LF,内部方法)。

        :raises ProtocolFrameError: 响应无效
        :raises DeviceError: PLC 返回出错代码(E0~E9)
        """
        transport = self._require_transport()
        transport.send(body)
        if transport.datagram:
            raw = transport.recv(KV_MAX_DATAGRAM)
            if not raw or raw[-1] not in (10, 13):
                raise OmniPLCInternalError("UDP 响应缺少 CR/LF 结束符")
            text = codec.parse_response(raw)
        else:
            chunks: List[bytes] = []
            received = 0
            started = False
            previous_timeout = transport.receive_timeout
            deadline = time.monotonic() + previous_timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout(
                            f"KV Host Link 收行超时({previous_timeout}s)"
                        )
                    transport.receive_timeout = remaining
                    byte = transport.recv(1)
                    if byte == b"\r" or byte == b"\n":
                        if not started:
                            continue  # 丢弃行首多余分隔符
                        break
                    started = True
                    chunks.append(byte)
                    received += 1
                    if received > KV_MAX_LINE:
                        raise OmniPLCInternalError(
                            f"KV Host Link 响应行超过 {KV_MAX_LINE} 字节上限"
                        )
            finally:
                transport.receive_timeout = previous_timeout
            text = codec.parse_response(b"".join(chunks))
        codec.check_error_code(text)
        return text

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到位/字读取原语。"""
        parsed = parse_kv_address(address)
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        if parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持字软元件位访问:{address!r}")
        if not is_bit_device(parsed.device) and data_type in (
            DataType.SHORT,
            DataType.USHORT,
            DataType.INT,
            DataType.UINT,
            DataType.FLOAT,
            DataType.LONG,
            DataType.ULONG,
            DataType.DOUBLE,
        ):
            return self._read_word(parsed, data_type)
        raise ValueError(f"KV Host Link 不支持的数据类型或软元件:{address!r}({data_type})")

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到位/字写入原语。"""
        parsed = parse_kv_address(address)
        if data_type is DataType.BOOL:
            self._write_bool_impl(parsed, require_bool(value))
            return
        if parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持字软元件位访问:{address!r}")
        if not is_bit_device(parsed.device) and data_type in (
            DataType.SHORT,
            DataType.USHORT,
            DataType.INT,
            DataType.UINT,
            DataType.FLOAT,
            DataType.LONG,
            DataType.ULONG,
            DataType.DOUBLE,
        ):
            self._write_word(parsed, data_type, value)
            return
        raise ValueError(f"KV Host Link 不支持的数据类型或软元件:{address!r}({data_type})")

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:连续 .U 字 → 小端拼字节 → 解码。"""
        parsed = _require_word(address)
        words = self._read_consecutive(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "little") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码 → 补齐偶数字节 → 小端拆字 → WRS 连续写。"""
        parsed = _require_word(address)
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        self._write_consecutive_words(parsed, words)
        return value

    def _read_bool_impl(self, parsed: KvAddress) -> bool:
        """读布尔:位软元件直读;字软元件读字后提位。"""
        if is_bit_device(parsed.device):
            response = self._transact(codec.build_read(parsed.to_text()))
            tokens = codec.split_tokens(response)
            if len(tokens) != 1:
                raise OmniPLCInternalError("位读响应应为 1 个令牌,收到 {}".format(len(tokens)))
            return codec.parse_bit_token(tokens[0])
        if parsed.bit is None:
            raise ValueError("位软元件布尔读取需要位软元件或字软元件位访问,如 DM100.5")
        word = self._read_word_token(parsed, ".U")
        return bool((word >> parsed.bit) & 1)

    def _write_bool_impl(self, parsed: KvAddress, value: bool) -> None:
        """写布尔:位软元件直写;字软元件读-改-写(锁内原子)。"""
        if is_bit_device(parsed.device):
            self._write_single(parsed, "", "1" if value else "0")
            return
        if parsed.bit is None:
            raise ValueError("位软元件布尔写入需要位软元件或字软元件位访问,如 DM100.5")
        word = self._read_word_token(parsed, ".U")
        updated = (word | (1 << parsed.bit)) if value else (word & ~(1 << parsed.bit))
        self._write_single(parsed, ".U", codec.format_value(updated & 0xFFFF, ".U"))

    # ------------------------------------------------------------------
    # 字访问:16/32 位走格式后缀,64 位/浮点走连续 .U 字
    # ------------------------------------------------------------------

    def _read_word(self, parsed: KvAddress, data_type: DataType) -> PrimitiveValue:
        """按数据类型读取字软元件(.S/.L 令牌已按有符号范围校验,直接返回)。"""
        if data_type is DataType.SHORT:
            return self._read_word_token(parsed, ".S")
        if data_type is DataType.USHORT:
            return self._read_word_token(parsed, ".U")
        if data_type is DataType.INT:
            return self._read_word_token(parsed, ".L")
        if data_type is DataType.UINT:
            return self._read_word_token(parsed, ".D")
        if data_type is DataType.FLOAT:
            words = self._read_consecutive(parsed, 2)
            return struct.unpack("<f", convert.words_to_bytes(words)[:4])[0]
        if data_type in (DataType.LONG, DataType.ULONG):
            words = self._read_consecutive(parsed, 4)
            raw = convert.words_to_bytes(words)[:8]
            return int.from_bytes(raw, "little", signed=data_type is DataType.LONG)
        words = self._read_consecutive(parsed, 4)
        return struct.unpack("<d", convert.words_to_bytes(words)[:8])[0]

    def _write_word(self, parsed: KvAddress, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型写入字软元件。"""
        if data_type is DataType.SHORT:
            self._write_single(
                parsed, ".S", codec.format_value(check_int16(value), ".S")
            )
            return
        if data_type is DataType.USHORT:
            self._write_single(
                parsed, ".U", codec.format_value(check_uint16(value), ".U")
            )
            return
        if data_type is DataType.INT:
            number = check_range(require_int(value), -0x80000000, 0x7FFFFFFF, "int")
            self._write_single(parsed, ".L", codec.format_value(number, ".L"))
            return
        if data_type is DataType.UINT:
            number = check_range(require_int(value), 0, 0xFFFFFFFF, "uint")
            self._write_single(parsed, ".D", codec.format_value(number, ".D"))
            return
        if data_type is DataType.FLOAT:
            number_f = require_float(value)
            try:
                raw = struct.pack("<f", number_f)
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"float 超出 float32 范围:{value}") from exc
            self._write_consecutive_words(parsed, convert.bytes_to_words(raw))
            return
        if data_type in (DataType.LONG, DataType.ULONG):
            number = require_int(value)
            if data_type is DataType.LONG:
                check_range(number, -9223372036854775808, 9223372036854775807, "long")
                raw = number.to_bytes(8, "little", signed=True)
            else:
                check_range(number, 0, 18446744073709551615, "ulong")
                raw = number.to_bytes(8, "little", signed=False)
            self._write_consecutive_words(parsed, convert.bytes_to_words(raw))
            return
        self._write_consecutive_words(
            parsed, convert.bytes_to_words(struct.pack("<d", require_float(value)))
        )

    def _read_word_token(self, parsed: KvAddress, data_format: str) -> int:
        """单字读并解析为整数(内部方法)。"""
        response = self._transact(codec.build_read(_token(parsed, data_format)))
        tokens = codec.split_tokens(response)
        if len(tokens) != 1:
            raise OmniPLCInternalError(
                "字读响应应为 1 个令牌,收到 {}:{}".format(len(tokens), response)
            )
        return codec.parse_word_token(tokens[0], data_format)

    def _write_single(self, parsed: KvAddress, data_format: str, value_text: str) -> None:
        """单点写并校验 OK 应答(内部方法)。"""
        _expect_ok(self._transact(codec.build_write(_token(parsed, data_format), value_text)))

    def _read_consecutive(self, parsed: KvAddress, count: int) -> List[int]:
        """RDS 连续读 .U 字,返回 0~65535 原始字列表(内部方法)。"""
        response = self._transact(
            codec.build_read(_token(parsed, ".U"), count)
        )
        tokens = codec.split_tokens(response)
        if len(tokens) != count:
            raise OmniPLCInternalError(
                "连续读响应令牌数不符:期望 {},收到 {}:{}".format(count, len(tokens), response)
            )
        return [codec.parse_word_token(token, ".U") for token in tokens]

    def _write_consecutive_words(self, parsed: KvAddress, words: List[int]) -> None:
        """WRS 连续写 .U 字(内部方法)。"""
        texts = [codec.format_value(word, ".U") for word in words]
        response = self._transact(
            codec.build_write_consecutive(_token(parsed, ".U"), texts)
        )
        _expect_ok(response)


class KeyenceHostLinkTcpClient(_KeyenceHostLinkBase):
    """基恩士 KV Host Link TCP 客户端(默认端口 8000)。

    :example: ``client = KeyenceHostLinkTcpClient("192.168.0.10", 8000)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """初始化 KV Host Link TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: Host Link 端口,默认 8000
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__()
        self._ip_address = ip_address
        self._port = int(port)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class KeyenceHostLinkUdpClient(_KeyenceHostLinkBase):
    """基恩士 KV Host Link UDP 客户端(默认端口 8000,一问一答一数据报)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """初始化 KV Host Link UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: Host Link 端口,默认 8000
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__()
        self._ip_address = ip_address
        self._port = int(port)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _token(parsed: KvAddress, data_format: str) -> str:
    """软元件规范文本 + 数据格式后缀(如 ``"DM100.U"``,内部函数)。"""
    return "{}{}".format(parsed.to_text(), data_format)


def _require_word(address: str) -> KvAddress:
    """字符串存取只允许无位号的字软元件(内部函数)。"""
    parsed = parse_kv_address(address)
    if parsed.bit is not None or is_bit_device(parsed.device):
        raise ValueError(f"字符串只能从字软元件存取,收到:{address!r}")
    return parsed


def _expect_ok(response: str) -> None:
    """校验写命令应答为 OK(内部函数)。"""
    if response.strip().upper() != "OK":
        raise OmniPLCInternalError(
            f"写命令应答异常:期望 OK,收到 {response!r}"
        )
