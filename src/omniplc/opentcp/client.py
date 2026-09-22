"""通用自定义 TCP/IP 客户端——分隔符/定长成帧的任意设备收发壳。

面向没有标准协议(或协议过简)的现场设备:称重仪表、传感器、自定义
上位机程序等。只做"连接 + 成帧 + 错误契约",报文内容由调用方解释:

- **成帧**(两种模式二选一):接收按 ``delimiter`` 分隔符切分(默认
  CR LF),或按 ``frame_length`` 每帧定长切分(二进制固定帧设备);
  带内部缓冲——一次到达多帧逐次返回,跨分片到达自动拼接;超过
  ``max_frame`` 未见完整帧按坏帧断线惰性重连(流内失步的兜底恢复)
- **发送**:``send`` 原样字节;``send_text`` 编码后可自动补分隔符
  (``append_delimiter``;定长成帧强制不补)
- **重连/超时**:沿用 :class:`~omniplc.core.base_client.BaseClient`
  机制——断线在下一次收发时惰性重建(缓冲同步清空,旧连接的残字节
  不会串入新会话);``connect_timeout``/``receive_timeout``/``retries``
  属性运行期可改;``receive``/``transact*`` 支持 per-call ``timeout``
- **错误契约**:超时链路完好不断线;连接错误标记断开待重连;解码
  失败/帧超限按坏帧断线;失败 ``(False, None)``/``False`` + last_error

无点位语义,``read``/``write`` 系列类型化方法不可用(返回失败并提示)。
"""
from __future__ import annotations

import socket
from typing import Optional, Tuple, Union

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    OPEN_TCP_DEFAULT_DELIMITER,
    OPEN_TCP_DEFAULT_PORT,
    OPEN_TCP_MAX_FRAME,
    OPEN_TCP_RECV_CHUNK,
)
from ..core.errors import DeviceError, ProtocolFrameError
from ..transport import BaseTransport, TcpTransport
from ..types import DataType, PrimitiveValue


class OpenTcpClient(BaseClient):
    """通用自定义 TCP/IP 客户端(分隔符/定长成帧,收发行为可配)。

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
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = OPEN_TCP_DEFAULT_PORT,
        delimiter: Optional[Union[str, bytes]] = OPEN_TCP_DEFAULT_DELIMITER,
        encoding: str = "utf-8",
        append_delimiter: bool = True,
        strip_delimiter: bool = True,
        max_frame: int = OPEN_TCP_MAX_FRAME,
        frame_length: Optional[int] = None,
    ) -> None:
        """初始化通用 TCP 客户端。

        :param ip_address: 设备 IP 或主机名
        :param port: TCP 端口(自定义设备无统一标准,按现场配置)
        :param delimiter: 帧分隔符(bytes 或 str;str 按 UTF-8 编码);
            定长成帧时传 ``None``
        :param encoding: ``send_text``/``receive_text``/``transact_text``
            的字符编码,默认 UTF-8
        :param append_delimiter: ``send_text``/``transact_text`` 发送时
            自动补分隔符(定长成帧必须为 False)
        :param strip_delimiter: ``receive``/``transact*`` 返回帧时是否
            去掉末尾分隔符(仅分隔符成帧生效)
        :param max_frame: 帧内容字节上限(不含分隔符),超限判流内失步
        :param frame_length: 定长成帧的每帧字节数(≥1,不超过
            ``max_frame``);与 ``delimiter`` 互斥,二者必须提供其一
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._encoding = self._coerce_encoding(encoding)
        if int(max_frame) < 1:
            raise ValueError(f"max_frame 必须大于 0,收到:{max_frame}")
        self._max_frame = int(max_frame)
        self._delimiter: Optional[bytes]
        self._frame_length: Optional[int]
        if frame_length is None:
            if delimiter is None:
                raise ValueError(
                    "必须提供 delimiter(分隔符成帧)或 frame_length(定长成帧)之一"
                )
            self._delimiter = self._coerce_delimiter(delimiter)
            self._frame_length = None
        else:
            if delimiter is not None:
                raise ValueError(
                    "delimiter 与 frame_length 互斥:定长成帧请传 delimiter=None"
                )
            if int(frame_length) < 1:
                raise ValueError(
                    f"frame_length 必须大于 0,收到:{frame_length}"
                )
            if int(frame_length) > self._max_frame:
                raise ValueError(
                    "frame_length({})不得超过 max_frame({})".format(
                        frame_length, self._max_frame
                    )
                )
            if append_delimiter:
                raise ValueError("定长成帧没有分隔符:append_delimiter 必须为 False")
            self._delimiter = None
            self._frame_length = int(frame_length)
        self._append_delimiter = bool(append_delimiter)
        self._strip_delimiter = bool(strip_delimiter)
        self._buffer = bytearray()

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
            try:
                return frame.decode(self._encoding)
            except UnicodeDecodeError as exc:
                raise ProtocolFrameError(
                    f"应答不是合法 {self._encoding}:{frame!r}"
                ) from exc
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
            try:
                return frame.decode(self._encoding)
            except UnicodeDecodeError as exc:
                raise ProtocolFrameError(
                    f"应答不是合法 {self._encoding}:{frame!r}"
                ) from exc
        return self._execute(operation)

    def _coerce_timeout(self, timeout: Optional[float]) -> float:
        """per-call 超时校验:None 用 receive_timeout(内部方法)。"""
        read_timeout = self._receive_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError(f"timeout 必须大于 0,收到:{read_timeout}")
        return read_timeout

    def _receive_frame(self, transport: BaseTransport, timeout: float) -> bytes:
        """从缓冲/流中取一帧(分隔符或定长切分;超时不断线,内部方法)。

        :raises DeviceError: 接收超时(链路完好,不断线)
        :raises ProtocolFrameError: 超过 max_frame 未见完整帧(失步断线)
        :raises OSError: 连接错误(标记断开惰性重连)
        """
        previous_timeout = transport.receive_timeout
        transport.receive_timeout = timeout
        try:
            while True:
                frame = self._cut_frame()
                if frame is not None:
                    return frame
                if len(self._buffer) > self._max_frame:
                    raise ProtocolFrameError(
                        "接收超过 {} 字节未成帧,判定流内失步".format(
                            self._max_frame
                        )
                    )
                chunk = transport.recv_some(OPEN_TCP_RECV_CHUNK)
                if not chunk:
                    raise OSError("连接被对端关闭")
                self._buffer.extend(chunk)
        except socket.timeout:
            raise DeviceError(
                "接收超时({}s),已收 {} 字节未成帧".format(timeout, len(self._buffer)),
                0,
            )
        finally:
            transport.receive_timeout = previous_timeout

    def _cut_frame(self) -> Optional[bytes]:
        """从缓冲头部切出一帧;不足一帧返回 ``None``(内部方法)。

        定长模式按 ``frame_length`` 硬切;分隔符模式找到分隔符后按
        ``strip_delimiter`` 决定是否连同分隔符一并取走。
        """
        if self._frame_length is not None:
            if len(self._buffer) < self._frame_length:
                return None
            frame = bytes(self._buffer[:self._frame_length])
            del self._buffer[:self._frame_length]
            return frame
        delimiter = self._delimiter
        assert delimiter is not None  # 构造期保证 delimiter/frame_length 二选一
        index = self._buffer.find(delimiter)
        if index < 0:
            return None
        end = index + len(delimiter)
        if self._strip_delimiter:
            frame = bytes(self._buffer[:index])
        else:
            frame = bytes(self._buffer[:end])
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

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """通用 TCP 无点位语义(内部方法)。"""
        raise DeviceError("通用 TCP 客户端不支持点位读取,请使用 receive/transact", 0)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """通用 TCP 无点位语义(内部方法)。"""
        raise DeviceError("通用 TCP 客户端不支持点位写入,请使用 send/transact", 0)
