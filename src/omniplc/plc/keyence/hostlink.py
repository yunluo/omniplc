"""基恩士 KV Host Link 客户端(TCP/UDP)。

ASCII 行式协议:命令帧以 CR 结束,响应为一行以 CR/LF 结束的 ASCII 文本。
TCP 走线按行收包(流式),UDP 走线一问一答一数据报。
**帧面待核**(2026-09-30 降级):KV Host Link 通信命令手册待补
(docs/protocol/README.md「待补」);端口 8000 与无 ``##`` 帧头/无 FCS 的
帧形态须真机抓包核证(公开参照实现为 8001 + ``##`` + BCC,互斥),
判据见 docs/real-machine-checklist.md 的 KV Host Link 条目。

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

from ...core import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    KV_DEFAULT_PORT,
    KV_MAX_DATAGRAM,
    KV_MAX_LINE,
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
from .address import KvAddress, is_bit_device, parse_kv_address
from ...core.i18n import _

# 字软元件可承载的数值类型(位软元件只走 BOOL)
_WORD_DATA_TYPES = frozenset(
    {
        DataType.SHORT,
        DataType.USHORT,
        DataType.INT,
        DataType.UINT,
        DataType.FLOAT,
        DataType.LONG,
        DataType.ULONG,
        DataType.DOUBLE,
    }
)


class _KeyenceHostLinkBase(BaseClient):
    """KV Host Link 客户端基类:类型分发与行式事务。"""

    # ------------------------------------------------------------------
    # 行式事务(由走线子类决定收包方式)
    # ------------------------------------------------------------------

    def _transact(self, body: bytes, check_errors: bool = True) -> str:
        """发送命令帧并返回一行响应文本(已去除 CR/LF,内部方法)。

        :param check_errors: 是否把形如 ``E0``~``E9`` 的整条响应判为出错
            代码。**读路径应保持默认**;唯一例外是读数据时显式跳过——
            ``.H`` 十六进制读的合法数据(值 0xE0~0xE9,如 ``"E5"`` = 229)
            与出错代码形状重合,协议层无法区分,按"读请求无出错回显语义
            之外的保护"处理:数据令牌交由 parse 层按格式校验,形状不符
            一样报错(ProtocolFrameError),不会静默错值。其余格式
            (.U/.S/.L/.D)一律查错,真实出错响应用 DeviceError 上抛。
        :raises ProtocolFrameError: 响应无效(结束符/行长/令牌形状不符;
            消息带**收到的原始数据**十六进制转储,便于现场与抓包比对)
        :raises DeviceError: PLC 返回出错代码(E0~E9,``check_errors=True`` 时)
        """
        transport = self._require_transport()
        if transport.datagram:
            # 数据报走线发送前排空陈旧帧:上一事务超时后迟到的响应会被
            # 本轮 recv 误当应答;Host Link 无事务号,同命令迟到帧可绕过
            # 校验造成静默错值,排空是唯一防线(陈旧帧防护)
            transport.drain()
        transport.send(body)
        if transport.datagram:
            raw = transport.recv(KV_MAX_DATAGRAM)
            if not raw or raw[-1] not in (10, 13):
                raise ProtocolFrameError(
                    _("UDP 响应缺少 CR/LF 结束符(收到的原始数据:{})").format(
                        _truncate_hex(raw)
                    )
                )
            text = codec.parse_response(raw)
        else:
            # 块读收行(review-1019 P1-2):recv_some 单次收尽已到达字节,
            # 替代逐字节 recv(1)(每字节 = 超时下发 + deadline 计算 + 系统
            # 调用,40 字符行 ≈80 次);recv_some 是流式成帧原语——阻塞上限
            # 为当前超时,有数据即整批返回,超时内无任何数据抛 socket.timeout
            # (与逐字节版同语义)。超时整体 deadline 保留:行未收完时循环
            # 顶部按剩余时间下发。行终止符之后的同块剩余字节一并丢弃:Host
            # Link 一命令一响应行,残留只可能来自协议异常——丢弃比留在
            # 缓冲污染下一事务更稳(逐字节版会把它当下一行行首)。
            line = bytearray()
            started = False
            terminated = False
            previous_timeout = transport.receive_timeout
            deadline = time.monotonic() + previous_timeout
            try:
                while not terminated:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout(
                            _("KV Host Link 收行超时({}s)").format(previous_timeout)
                        )
                    transport.receive_timeout = remaining
                    chunk = transport.recv_some(KV_MAX_LINE + 2 - len(line))
                    for byte in chunk:
                        if byte == 13 or byte == 10:
                            if not started:
                                continue  # 丢弃行首多余分隔符
                            terminated = True
                            break
                        started = True
                        line.append(byte)
                        if len(line) > KV_MAX_LINE:
                            raise ProtocolFrameError(
                                _(
                                    "KV Host Link 响应行超过 {} 字节上限(头部字节:{})"
                                ).format(
                                    KV_MAX_LINE,
                                    _truncate_hex(bytes(line)),
                                )
                            )
            finally:
                transport.receive_timeout = previous_timeout
            text = codec.parse_response(bytes(line))
        if check_errors:
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
            raise ValueError(_("仅布尔类型支持字软元件位访问:{!r}").format(address))
        if not is_bit_device(parsed.device) and data_type in _WORD_DATA_TYPES:
            return self._read_word(parsed, data_type)
        raise ValueError(
            _("KV Host Link 不支持的数据类型或软元件:{!r}({})").format(
                address, data_type
            )
        )

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到位/字写入原语。"""
        parsed = parse_kv_address(address)
        if data_type is DataType.BOOL:
            self._write_bool_impl(parsed, require_bool(value))
            return
        if parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持字软元件位访问:{!r}").format(address))
        if not is_bit_device(parsed.device) and data_type in _WORD_DATA_TYPES:
            self._write_word(parsed, data_type, value)
            return
        raise ValueError(
            _("KV Host Link 不支持的数据类型或软元件:{!r}({})").format(
                address, data_type
            )
        )

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:连续 .U 字 → 小端拼字节 → 解码。"""
        parsed = _require_word(address)
        words = self._read_consecutive(parsed, (length + 1) // 2)
        data = convert.words_to_bytes(words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码 → 补齐偶数字节 → 小端拆字 → WRS 连续写。"""
        parsed = _require_word(address)
        # 编码一次后直接补 \\x00 到偶数字节(encode_string 目标长度 = 向上取偶,
        # 长度校验恒不触发,review-1019 P3-2)
        raw = value.encode(encoding)
        if len(raw) % 2:
            raw += b"\x00"
        words = convert.bytes_to_words(raw)
        self._write_consecutive_words(parsed, words)
        return value

    def _read_bool_impl(self, parsed: KvAddress) -> bool:
        """读布尔:位软元件直读;字软元件读字后提位。"""
        if is_bit_device(parsed.device):
            response = self._transact(codec.build_read(parsed.to_text()))
            tokens = codec.split_tokens(response)
            if len(tokens) != 1:
                raise ProtocolFrameError(
                    _("位读响应应为 1 个令牌,收到 {}:{}(收到的原始响应:{!r})").format(
                        len(tokens), response, response
                    )
                )
            return codec.parse_bit_token(tokens[0])
        if parsed.bit is None:
            raise ValueError(
                _("位软元件布尔读取需要位软元件或字软元件位访问,如 DM100.5")
            )
        word = self._read_word_token(parsed, ".U")
        return bool((word >> parsed.bit) & 1)

    def _write_bool_impl(self, parsed: KvAddress, value: bool) -> None:
        """写布尔:位软元件直写;字软元件读-改-写(锁内原子)。"""
        if is_bit_device(parsed.device):
            self._write_single(parsed, "", "1" if value else "0")
            return
        if parsed.bit is None:
            raise ValueError(
                _("位软元件布尔写入需要位软元件或字软元件位访问,如 DM100.5")
            )
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

    def _write_word(
        self, parsed: KvAddress, data_type: DataType, value: PrimitiveValue
    ) -> None:
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
            number = check_range(require_int(value), INT32_MIN, INT32_MAX, "int")
            self._write_single(parsed, ".L", codec.format_value(number, ".L"))
            return
        if data_type is DataType.UINT:
            number = check_range(require_int(value), 0, UINT32_MAX, "uint")
            self._write_single(parsed, ".D", codec.format_value(number, ".D"))
            return
        if data_type is DataType.FLOAT:
            number_f = require_float(value)
            try:
                raw = struct.pack("<f", number_f)
            except (OverflowError, ValueError) as exc:
                raise ValueError(_("float 超出 float32 范围:{}").format(value)) from exc
            self._write_consecutive_words(parsed, convert.bytes_to_words(raw))
            return
        if data_type in (DataType.LONG, DataType.ULONG):
            number = require_int(value)
            if data_type is DataType.LONG:
                check_range(number, INT64_MIN, INT64_MAX, "long")
                raw = number.to_bytes(8, "little", signed=True)
            else:
                check_range(number, 0, UINT64_MAX, "ulong")
                raw = number.to_bytes(8, "little", signed=False)
            self._write_consecutive_words(parsed, convert.bytes_to_words(raw))
            return
        self._write_consecutive_words(
            parsed, convert.bytes_to_words(struct.pack("<d", require_float(value)))
        )

    def _read_word_token(self, parsed: KvAddress, data_format: str) -> int:
        """单字读并解析为整数(内部方法)。

        ``check_errors`` 仅对 ``.H``(十六进制读)豁免:其数据值 0xE0~0xE9
        与出错代码形状重合(协议固有歧义),读数据令牌交
        :func:`codec.parse_word_token` 按格式校验。其余格式(.U/.S/.L/.D)
        **必须查错**——PLC 回 E0~E9 时按 DeviceError 上抛(链路完好不断线、
        错误码保留),原"全格式豁免"会把真实出错响应当坏帧拆连并丢错误码
        (公开读路径只有 .U/.S/.L/.D;.H 无公开入口,数据后缀由调用方显式传入)。
        """
        response = self._transact(
            codec.build_read(_token(parsed, data_format)),
            check_errors=(data_format != ".H"),
        )
        tokens = codec.split_tokens(response)
        if len(tokens) != 1:
            raise ProtocolFrameError(
                _("字读响应应为 1 个令牌,收到 {}:{}(收到的原始响应:{!r})").format(
                    len(tokens), response, response
                )
            )
        return codec.parse_word_token(tokens[0], data_format)

    def _write_single(
        self, parsed: KvAddress, data_format: str, value_text: str
    ) -> None:
        """单点写并校验 OK 应答(内部方法)。"""
        _expect_ok(
            self._transact(codec.build_write(_token(parsed, data_format), value_text))
        )

    def _read_consecutive(self, parsed: KvAddress, count: int) -> List[int]:
        """RDS 连续读 .U 字,返回 0~65535 原始字列表(内部方法)。"""
        response = self._transact(codec.build_read(_token(parsed, ".U"), count))
        tokens = codec.split_tokens(response)
        if len(tokens) != count:
            raise ProtocolFrameError(
                _(
                    "连续读响应令牌数不符:期望 {},收到 {}:{}(收到的原始响应:{!r})"
                ).format(count, len(tokens), response, response)
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
        raise ValueError(_("字符串只能从字软元件存取,收到:{!r}").format(address))
    return parsed


def _expect_ok(response: str) -> None:
    """校验写命令应答为 OK(内部函数)。

    :raises ProtocolFrameError: 应答既不是 OK、也不是 :func:`codec.check_error_code`
        已处理的 E0~E9 出错码(命令回显/形状不符 → 归 PROTOCOL,与 ``_transact``
        docstring 的承诺一致)
    """
    if response.strip().upper() != "OK":
        raise ProtocolFrameError(_("写命令应答异常:期望 OK,收到 {!r}").format(response))


def _truncate_hex(data: bytes, limit: int = 64) -> str:
    """十六进制转储截断(内部函数):超长只显示前 ``limit`` 字节,防刷屏。

    有意本地薄封装:复用 :func:`omniplc.core.debug.format_hex`,仅多一层
    64B 预截断(KV 行短,先截再转省一跳)——review-1005 §4.2 裁决不
    上移不合并,勿再报重复。
    """
    if len(data) <= limit:
        return format_hex(data)
    return "{}...(共 {} 字节,其余省略)".format(format_hex(data[:limit]), len(data))
