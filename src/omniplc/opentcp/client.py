"""通用自定义 TCP/IP 客户端——分隔符/定长/长度前缀成帧的任意设备收发壳。

面向没有标准协议(或协议过简)的现场设备:称重仪表、传感器、自定义
上位机程序等。只做"连接 + 成帧 + 错误契约",报文内容由调用方解释:

- **成帧**(三选一):``delimiter`` 分隔符(默认 CR LF,可选
  ``start_marker`` 起始标记如 STX/ETX)、``frame_length`` 定长、或
  ``length_prefix`` 长度前缀(1/2/4 字节长度域 + 该长度载荷,字节序可配);
  带内部缓冲——一次到达多帧逐次返回,跨分片到达自动拼接;超过
  ``max_frame + 成帧开销`` 未见完整帧按坏帧断线惰性重连(流内失步兜底)
- **发送**:``send`` 原样字节;``send_text`` 编码后可自动补分隔符
  (``append_delimiter``;定长/长度前缀成帧强制不补)
- **重连/超时**:沿用 :class:`~omniplc.core.base_client.BaseClient`
  机制——断线在下一次收发时惰性重建(缓冲同步清空,旧连接的残字节
  不会串入新会话);``connect_timeout``/``receive_timeout``/``retries``
  属性运行期可改;``receive``/``transact*`` 支持 per-call ``timeout``
- **错误契约**:超时链路完好不断线(残字节留 ``last_partial_frame``
  供诊断);连接错误标记断开待重连;解码失败(按 ``encoding_fallback``
  回退后仍失败)/帧超限按坏帧断线;失败 ``(False, None)``/``False`` +
  last_error

无点位语义,``read``/``write`` 系列类型化方法不可用(返回失败并提示)。
"""
from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple, Union, cast

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    OPEN_TCP_DEFAULT_DELIMITER,
    OPEN_TCP_DEFAULT_ENCODING,
    OPEN_TCP_DEFAULT_PORT,
    OPEN_TCP_MAX_FRAME,
    OPEN_TCP_RECV_CHUNK,
)
from ..core.debug import format_hex
from ..core.errors import DeviceError, ProtocolFrameError
from ..transport import BaseTransport, TcpTransport
from ..types import DataType, PrimitiveValue

if TYPE_CHECKING:
    from typing import Literal

_LENGTH_PREFIX_SIZES = (1, 2, 4)
_LENGTH_PREFIX_BYTEORDERS = {"big": "big", "little": "little"}


class OpenTcpClient(BaseClient):
    """通用自定义 TCP/IP 客户端(分隔符/定长/长度前缀成帧,收发行为可配)。

    :example::

        client = OpenTcpClient("192.168.0.10", 9000, delimiter="\\r\\n")
        client.receive_timeout = 2.0
        client.connect()
        ok = client.send_text("READ")            # 自动补分隔符
        ok, raw = client.receive()               # 收一帧(bytes)
        ok, text = client.transact_text("VER")   # 发送并收一帧(str)

    二进制定长帧设备::

        client = OpenTcpClient("192.168.0.10", 9000,
                               delimiter=None, frame_length=8,
                               append_delimiter=False)

    STX/ETX 成帧(``start_marker`` 标记起始,``delimiter`` 标记结束)::

        client = OpenTcpClient("192.168.0.10", 9000,
                               start_marker="\\x02", delimiter="\\x03")

    长度前缀成帧(``length_prefix`` 字节大端长度域,后跟该长度字节的载荷)::

        client = OpenTcpClient("192.168.0.10", 9000,
                               delimiter=None, length_prefix=2)
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = OPEN_TCP_DEFAULT_PORT,
        delimiter: Optional[Union[str, bytes]] = OPEN_TCP_DEFAULT_DELIMITER,
        encoding: str = OPEN_TCP_DEFAULT_ENCODING,
        append_delimiter: bool = True,
        strip_delimiter: bool = True,
        max_frame: int = OPEN_TCP_MAX_FRAME,
        frame_length: Optional[int] = None,
        encoding_fallback: Optional[Sequence[str]] = None,
        recv_chunk_size: int = OPEN_TCP_RECV_CHUNK,
        start_marker: Optional[Union[str, bytes]] = None,
        length_prefix: Optional[int] = None,
        length_prefix_byteorder: str = "big",
    ) -> None:
        """初始化通用 TCP 客户端。

        :param ip_address: 设备 IP 或主机名
        :param port: TCP 端口(自定义设备无统一标准,按现场配置)
        :param delimiter: 帧分隔符(bytes 或 str;str 按 UTF-8 编码);
            定长/长度前缀成帧时传 ``None``
        :param encoding: ``send_text``/``receive_text``/``transact_text``
            的首选字符编码,默认 UTF-8
        :param append_delimiter: ``send_text``/``transact_text`` 发送时
            自动补分隔符(定长/长度前缀成帧必须为 False)
        :param strip_delimiter: ``receive``/``transact*`` 返回帧时是否
            去掉末尾分隔符(仅分隔符成帧生效)
        :param max_frame: 帧内容字节上限(不含分隔符/长度域),超限判流内失步
        :param frame_length: 定长成帧的每帧字节数(≥1,不超过
            ``max_frame``);与 ``delimiter``/``length_prefix`` 互斥
        :param encoding_fallback: 解码回退编码序列(依次尝试;全部失败按
            坏帧断线)——中文工业设备常见 ASCII 命令 + GBK 状态文本
        :param recv_chunk_size: 单次 ``recv`` 读取字节数(≥1,默认 256)
        :param start_marker: 帧起始标记(bytes 或 str;如 STX ``"\\x02"``);
            仅与 ``delimiter`` 同用,标记前的噪声字节被丢弃
        :param length_prefix: 长度域字节数(1/2/4);帧 = 长度域 + 该长度字节
            的载荷;与 ``delimiter``/``frame_length`` 互斥
        :param length_prefix_byteorder: 长度域字节序(``"big"``/``"little"``)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._encoding = self._coerce_encoding(encoding)
        self._encoding_fallback: List[str] = [
            self._coerce_encoding(item) for item in (encoding_fallback or [])
        ]
        if int(max_frame) < 1:
            raise ValueError(f"max_frame 必须大于 0,收到:{max_frame}")
        self._max_frame = int(max_frame)
        if int(recv_chunk_size) < 1:
            raise ValueError(f"recv_chunk_size 必须大于 0,收到:{recv_chunk_size}")
        self._recv_chunk = int(recv_chunk_size)
        self._start_marker: Optional[bytes] = (
            self._coerce_marker(start_marker) if start_marker is not None else None
        )
        self._length_prefix: Optional[int] = None
        self._length_prefix_byteorder = self._coerce_byteorder(length_prefix_byteorder)
        self._delimiter: Optional[bytes] = None
        self._frame_length: Optional[int] = None
        self._resolve_framing(delimiter, frame_length, length_prefix, append_delimiter)
        self._append_delimiter = bool(append_delimiter)
        self._strip_delimiter = bool(strip_delimiter)
        self._buffer = bytearray()
        self._last_partial_frame: Optional[bytes] = None

    def _resolve_framing(
        self,
        delimiter: Optional[Union[str, bytes]],
        frame_length: Optional[int],
        length_prefix: Optional[int],
        append_delimiter: bool,
    ) -> None:
        """校验并落地成帧方式(定长 / 长度前缀 / 分隔符[+起始标记],内部方法)。"""
        if frame_length is not None and length_prefix is not None:
            raise ValueError("frame_length 与 length_prefix 互斥,只能二选一")
        if length_prefix is not None:
            if delimiter is not None:
                raise ValueError("length_prefix 成帧不能带 delimiter(请传 delimiter=None)")
            if self._start_marker is not None:
                raise ValueError("length_prefix 成帧不能带 start_marker")
            if int(length_prefix) not in _LENGTH_PREFIX_SIZES:
                raise ValueError(
                    f"length_prefix 必须为 {_LENGTH_PREFIX_SIZES} 之一:{length_prefix}"
                )
            if append_delimiter:
                raise ValueError("length_prefix 成帧没有分隔符:append_delimiter 必须为 False")
            self._length_prefix = int(length_prefix)
            return
        if frame_length is not None:
            if delimiter is not None:
                raise ValueError(
                    "delimiter 与 frame_length 互斥:定长成帧请传 delimiter=None"
                )
            if self._start_marker is not None:
                raise ValueError("frame_length 成帧不能带 start_marker")
            if int(frame_length) < 1:
                raise ValueError(f"frame_length 必须大于 0,收到:{frame_length}")
            if int(frame_length) > self._max_frame:
                raise ValueError(
                    "frame_length({})不得超过 max_frame({})".format(
                        frame_length, self._max_frame
                    )
                )
            if append_delimiter:
                raise ValueError("定长成帧没有分隔符:append_delimiter 必须为 False")
            self._frame_length = int(frame_length)
            return
        # 分隔符成帧(可选起始标记)
        if delimiter is None:
            raise ValueError(
                "必须提供 delimiter(分隔符成帧)/ frame_length(定长)/ "
                "length_prefix(长度前缀)之一"
            )
        self._delimiter = self._coerce_delimiter(delimiter)

    # ------------------------------------------------------------------
    # 可配参数(构造期定,只读属性)
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_delimiter(delimiter: Union[str, bytes]) -> bytes:
        """分隔符统一为 bytes 并校验非空(内部方法)。"""
        if isinstance(delimiter, str):
            delimiter = delimiter.encode("utf-8")
        delimiter = bytes(delimiter)
        if not delimiter:
            raise ValueError("delimiter 不能为空")
        return delimiter

    @staticmethod
    def _coerce_encoding(encoding: str) -> str:
        """校验编码名合法(内部方法)。"""
        try:
            "x".encode(encoding)
        except LookupError:
            raise ValueError(f"encoding 非法:{encoding!r}")
        return encoding

    @staticmethod
    def _coerce_marker(marker: Union[str, bytes]) -> bytes:
        """起始标记统一为 bytes 并校验非空(内部方法)。"""
        if isinstance(marker, str):
            marker = marker.encode("utf-8")
        marker = bytes(marker)
        if not marker:
            raise ValueError("start_marker 不能为空")
        return marker

    @staticmethod
    def _coerce_byteorder(value: str) -> Literal["big", "little"]:
        """校验长度域字节序(内部方法)。"""
        key = str(value).strip().lower()
        if key not in _LENGTH_PREFIX_BYTEORDERS:
            raise ValueError(f"length_prefix_byteorder 非法:{value!r}(big/little)")
        return cast("Literal['big', 'little']", key)

    @property
    def delimiter(self) -> Optional[bytes]:
        """帧分隔符(bytes);定长成帧为 ``None``。"""
        return self._delimiter

    @property
    def frame_length(self) -> Optional[int]:
        """定长成帧的每帧字节数;分隔符成帧为 ``None``。"""
        return self._frame_length

    @property
    def encoding(self) -> str:
        """文本收发的字符编码。"""
        return self._encoding

    @property
    def append_delimiter(self) -> bool:
        """发送文本时是否自动补分隔符。"""
        return self._append_delimiter

    @property
    def strip_delimiter(self) -> bool:
        """收帧返回时是否去掉末尾分隔符。"""
        return self._strip_delimiter

    @property
    def max_frame(self) -> int:
        """帧内容字节上限(不含分隔符)。"""
        return self._max_frame

    @property
    def encoding_fallback(self) -> Tuple[str, ...]:
        """解码回退编码序列(只读)。"""
        return tuple(self._encoding_fallback)

    @property
    def recv_chunk_size(self) -> int:
        """单次 recv 读取字节数(只读)。"""
        return self._recv_chunk

    @property
    def start_marker(self) -> Optional[bytes]:
        """帧起始标记(bytes);未配置为 ``None``(只读)。"""
        return self._start_marker

    @property
    def length_prefix(self) -> Optional[int]:
        """长度域字节数;非长度前缀成帧为 ``None``(只读)。"""
        return self._length_prefix

    @property
    def last_partial_frame(self) -> Optional[bytes]:
        """最近一次接收超时时缓冲里的**部分帧**字节(诊断用;成功后清空)。"""
        return self._last_partial_frame

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> bool:
        """原样发送字节(不补分隔符)。

        :param data: 待发送字节(非空)
        :return: 是否成功;失败 ``False`` + :attr:`last_error`
        :raises ValueError: 数据为空
        """
        data = bytes(data)
        if not data:
            raise ValueError("待发送数据为空")
        def operation() -> bool:
            self._require_transport().send(data)
            return True
        ok, _ = self._execute(operation, is_write=True)
        return ok

    def send_text(self, text: str) -> bool:
        """按配置编码发送文本;``append_delimiter`` 为 True 时补分隔符。

        :param text: 待发送文本(编码并补分隔符后须非空)
        :return: 是否成功
        :raises ValueError: 编码后数据为空
        """
        return self._send_payload(self._encode_outgoing(text))

    def _encode_outgoing(self, text: str) -> bytes:
        """文本 → 发送字节(编码 + 可选分隔符,内部方法)。"""
        payload = text.encode(self._encoding)
        if self._append_delimiter:
            delimiter = self._delimiter
            assert delimiter is not None  # append_delimiter 仅分隔符成帧可用(构造期保证)
            payload += delimiter
        return payload

    def _send_payload(self, payload: bytes) -> bool:
        """发送字节载荷(空载荷拒绝,内部方法)。"""
        if not payload:
            raise ValueError("待发送数据为空")
        def operation() -> bool:
            self._require_transport().send(payload)
            return True
        ok, _ = self._execute(operation, is_write=True)
        return ok

    # ------------------------------------------------------------------
    # 接收
    # ------------------------------------------------------------------

    def receive(self, timeout: Optional[float] = None) -> Tuple[bool, Optional[bytes]]:
        """收一帧(bytes;分隔符或定长切分,跨分片自动拼接,多帧逐次返回)。

        :param timeout: 本次接收超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否成功, 帧字节)``;超时不断线,连接错误标记断开
        :raises ValueError: timeout 非法
        """
        read_timeout = self._coerce_timeout(timeout)
        def operation() -> bytes:
            return self._receive_frame(self._require_transport(), read_timeout)
        return self._execute(operation)

    def receive_text(self, timeout: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """收一帧并按配置编码解码为文本。

        解码失败按坏帧断线(流内大概率已失步,重连后重新同步)。

        :param timeout: 本次接收超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否成功, 帧文本)``
        :raises ValueError: timeout 非法
        """
        read_timeout = self._coerce_timeout(timeout)
        def operation() -> str:
            frame = self._receive_frame(self._require_transport(), read_timeout)
            return self._decode_bytes(frame)
        return self._execute(operation)

    def transact(self, data: bytes, timeout: Optional[float] = None) -> Tuple[bool, Optional[bytes]]:
        """发送字节并收一帧应答(同一事务锁内)。

        :param data: 待发送字节(原样,不补分隔符;非空)
        :param timeout: 接收超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否成功, 帧字节)``
        :raises ValueError: 数据为空或 timeout 非法
        """
        data = bytes(data)
        if not data:
            raise ValueError("待发送数据为空")
        read_timeout = self._coerce_timeout(timeout)
        def operation() -> bytes:
            transport = self._require_transport()
            transport.send(data)
            return self._receive_frame(transport, read_timeout)
        return self._execute(operation)

    def transact_text(self, text: str, timeout: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """发送文本(可自动补分隔符)并收一帧应答解码为文本。

        :param text: 待发送文本
        :param timeout: 接收超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否成功, 帧文本)``
        :raises ValueError: 编码后数据为空或 timeout 非法
        """
        payload = self._encode_outgoing(text)
        if not payload:
            raise ValueError("待发送数据为空")
        read_timeout = self._coerce_timeout(timeout)
        def operation() -> str:
            transport = self._require_transport()
            transport.send(payload)
            frame = self._receive_frame(transport, read_timeout)
            return self._decode_bytes(frame)
        return self._execute(operation)

    def _decode_bytes(self, frame: bytes) -> str:
        """按首选编码 + 回退链解码;全部失败按坏帧抛错(内部方法)。

        :raises ProtocolFrameError: 所有候选编码均解码失败
        """
        last_exc: Optional[BaseException] = None
        for codec_name in [self._encoding, *self._encoding_fallback]:
            try:
                return frame.decode(codec_name)
            except (UnicodeDecodeError, LookupError) as exc:
                last_exc = exc
        candidates = "/".join([self._encoding, *self._encoding_fallback])
        raise ProtocolFrameError(
            "应答不是合法 {}:{!r}(收到的原始帧:{})".format(
                candidates, frame, format_hex(frame)
            )
        ) from last_exc

    def _coerce_timeout(self, timeout: Optional[float]) -> float:
        """per-call 超时校验:None 用 receive_timeout(内部方法)。"""
        read_timeout = self._receive_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError(f"timeout 必须大于 0,收到:{read_timeout}")
        return read_timeout

    def _receive_frame(self, transport: BaseTransport, timeout: float) -> bytes:
        """从缓冲/流中取一帧(分隔符或定长切分;整帧受 timeout 总预算,内部方法)。

        :raises DeviceError: 接收超时(链路完好,不断线)
        :raises ProtocolFrameError: 超过 max_frame 未见完整帧(失步断线)
        :raises OSError: 连接错误(标记断开惰性重连)
        """
        previous_timeout = transport.receive_timeout
        transport.receive_timeout = timeout
        deadline = time.monotonic() + timeout
        limit = self._buffer_limit()
        try:
            while True:
                frame = self._cut_frame()
                if frame is not None:
                    self._last_partial_frame = None
                    return frame
                if len(self._buffer) >= limit:
                    raise ProtocolFrameError(
                        "接收超过 {} 字节未成帧,判定流内失步(当前缓冲前段:{})".format(
                            limit, format_hex(bytes(self._buffer))
                        )
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._last_partial_frame = bytes(self._buffer)
                    raise DeviceError(
                        f"接收超时({timeout}s),已收 {len(self._buffer)} 字节未成帧",
                        0,
                    )
                transport.receive_timeout = remaining
                chunk = transport.recv_some(self._recv_chunk)
                if not chunk:
                    raise OSError("连接被对端关闭")
                self._buffer.extend(chunk)
        except socket.timeout:
            self._last_partial_frame = bytes(self._buffer)
            raise DeviceError(
                f"接收超时({timeout}s),已收 {len(self._buffer)} 字节未成帧",
                0,
            )
        finally:
            transport.receive_timeout = previous_timeout

    def _buffer_limit(self) -> int:
        """未成帧时的缓冲硬上限(max_frame + 成帧开销,内部方法)。"""
        overhead = 0
        if self._length_prefix is not None:
            overhead = self._length_prefix
        elif self._start_marker is not None:
            overhead = len(self._start_marker)
        if self._delimiter is not None:
            overhead += len(self._delimiter)
        return self._max_frame + overhead

    def _cut_frame(self) -> Optional[bytes]:
        """从缓冲头部切出一帧;不足一帧返回 ``None``(内部方法)。

        三种成帧:定长按 ``frame_length`` 硬切;长度前缀读 ``length_prefix``
        字节长度域后取该长度载荷;分隔符模式找 ``delimiter``(可选
        ``start_marker`` 起始标记,标记前噪声丢弃)后按 ``strip_delimiter``
        决定是否保留分隔符。

        :raises ProtocolFrameError: 长度前缀声明的长度超过 ``max_frame``
        """
        if self._frame_length is not None:
            if len(self._buffer) < self._frame_length:
                return None
            frame = bytes(self._buffer[:self._frame_length])
            del self._buffer[:self._frame_length]
            return frame
        if self._length_prefix is not None:
            prefix = self._length_prefix
            if len(self._buffer) < prefix:
                return None
            value = int.from_bytes(
                self._buffer[:prefix], self._length_prefix_byteorder
            )
            if value > self._max_frame:
                raise ProtocolFrameError(
                    "长度前缀声明 {} 字节,超过 max_frame({})".format(
                        value, self._max_frame
                    )
                )
            total = prefix + value
            if len(self._buffer) < total:
                return None
            frame = bytes(self._buffer[prefix:total])
            del self._buffer[:total]
            return frame
        delimiter = self._delimiter
        assert delimiter is not None  # 构造期保证三选一
        marker = self._start_marker
        if marker is not None:
            index = self._buffer.find(marker)
            if index < 0:
                # 丢弃标记前的噪声,仅保留可能是标记前缀的尾部字节
                keep = len(marker) - 1
                if len(self._buffer) > keep:
                    del self._buffer[:len(self._buffer) - keep]
                return None
            if index > 0:
                del self._buffer[:index]
        content_start = len(marker) if marker is not None else 0
        index = self._buffer.find(delimiter, content_start)
        if index < 0:
            return None
        end = index + len(delimiter)
        if self._strip_delimiter:
            frame = bytes(self._buffer[content_start:index])
        else:
            frame = bytes(self._buffer[content_start:end])
        del self._buffer[:end]
        return frame

    # ------------------------------------------------------------------
    # 连接与基类契约
    # ------------------------------------------------------------------

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    def _after_connect(self) -> None:
        """连接建立后清空接收缓冲:旧连接的残字节不得串入新会话(内部方法)。"""
        self._buffer.clear()
        self._last_partial_frame = None

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """通用 TCP 无点位语义(内部方法)。"""
        raise DeviceError("通用 TCP 客户端不支持点位读取,请使用 receive/transact", 0)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """通用 TCP 无点位语义(内部方法)。"""
        raise DeviceError("通用 TCP 客户端不支持点位写入,请使用 send/transact", 0)
