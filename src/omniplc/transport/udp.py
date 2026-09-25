"""UDP 传输实现。"""
from __future__ import annotations

import socket
import sys
from typing import Optional

from .base import BaseTransport
from ..core.debug import RECV_MARK, SEND_MARK, log_frame, log_op, log_warning
from ..core.errors import DeviceError, TransportClosedError, TransportTimeoutError

# Windows ``recv`` 对超长 UDP 报文抛 :data:`WSAEMSGSIZE`(errno 10040)。
# POSIX 静默截断,Windows 显式 OSError;两者都指示"对端报文超过缓冲"。
_WSAEMSGSIZE_ERRNO = 10040

# UDP 静默截断仅在 POSIX(Linux/macOS)出现——Windows ``recv``/``recv_into``
# 对超长报文直接抛 ``WSAEMSGSIZE``(WinError 10040),``recv_into`` + ``MSG_TRUNC``
# 还会在 Windows 上抛 ``WSAESYSNOTREADY``(WinError 10045)。POSIX 下
# :meth:`recv_into` + ``MSG_TRUNC`` 暴露真实报文字节数,用于探测截断。
_SUPPORTS_MSG_TRUNC = sys.platform != "win32" and hasattr(socket, "MSG_TRUNC")


class UdpTransport(BaseTransport):
    """UDP 传输:面向 Modbus UDP、MC over UDP、FINS/UDP。

    - 采用"已连接 UDP"语义:``connect()`` 固定对端,之后直接 send/recv
    - :meth:`recv` 返回**一条数据报**(最长 ``size`` 字节,超出截断),
      即一次收发对应一个协议帧
    - UDP 无连接概念,``connect()`` 只做本地套接字初始化,不会失败于对端
    - 接收超时抛 :class:`omniplc.core.errors.TransportTimeoutError`
      (DeviceError 子类):数据报整收无残留字节,按"链路完好不断线"
    - :attr:`receive_timeout` 修改后**立即作用于已连接 socket**
    """

    datagram: bool = True
    """一问一答一数据报:recv 整包,协议层按帧内长度字段校验。"""

    def __init__(self, ip_address: str, port: int) -> None:
        """初始化 UDP 传输。

        :param ip_address: 目标 IP 或主机名
        :param port: 目标端口
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._debug_label = f"udp://{ip_address}:{port}"

    @BaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒);已连接时立即下发到 socket。"""
        BaseTransport.receive_timeout.fset(self, seconds)  # type: ignore[attr-defined]
        sock = self._socket
        if sock is not None:
            sock.settimeout(self._receive_timeout)

    def connect(self) -> None:
        """初始化 UDP 套接字并固定对端。

        :raises OSError: 地址解析失败或端口绑定错误
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self._connect_timeout)
        sock.connect((self._ip_address, self._port))
        sock.settimeout(self._receive_timeout)
        self._socket = sock
        log_op(self._debug_label, "已连接")

    def close(self) -> None:
        """关闭 UDP 套接字,幂等。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
            log_op(self._debug_label, "已断开")

    def send(self, data: bytes) -> None:
        """发送一条数据报到固定对端。

        :raises TransportClosedError: 未初始化
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        log_frame(self._debug_label, SEND_MARK, data)
        sock.send(data)

    def recv(self, size: int) -> bytes:
        """接收一条数据报。

        UDP 报文截断处理分平台:

        - **POSIX**(Linux/macOS):``recv(size)`` 对超长报文**静默截断**——
          超出字节不会保留到下次 recv。库用 ``recv_into`` + ``MSG_TRUNC``
          拿到真实报文字节数,截断时通过 :func:`log_warning` 输出一条
          WARNING(不受调试开关门控)以提示调用方增大缓冲——多半是协议层
          size 估错或对端回了超长报文。
        - **Windows**::meth:`recv` 对超长报文抛 ``WSAEMSGSIZE``(errno 10040)。
          库捕获后:1) WARNING 日志输出一行带"缓冲 size"的诊断;2) 转抛
          :class:`DeviceError`,基类按 ``DEVICE`` 分类(与真断线 ``TRANSPORT``
          区分)且"不重试、不断线"——UDP 报文超长是协议帧问题,链路完好。

        :param size: 缓冲上限(超出部分截断)
        :raises TransportClosedError: 未初始化
        :raises TransportTimeoutError: 接收超时(不断线语义)
        :raises DeviceError: Windows 上报文超过缓冲时抛(code=10040)
        :raises OSError: 其他 OS 层错误
        """
        sock = self._require_socket()
        if _SUPPORTS_MSG_TRUNC:
            buffer = bytearray(size)
            try:
                datagram_size = sock.recv_into(buffer, size, socket.MSG_TRUNC)
            except socket.timeout as exc:
                raise TransportTimeoutError(
                    f"UDP 接收超时({self._receive_timeout}s)", 0
                ) from exc
            if datagram_size > size:
                log_warning(
                    self._debug_label,
                    "UDP 数据报截断:实收 %dB,缓冲 %dB(超出 %dB 已丢,检查协议层 size 或对端报文)",
                    datagram_size,
                    size,
                    datagram_size - size,
                )
                frame = bytes(buffer)
            else:
                frame = bytes(buffer[:datagram_size])
            log_frame(self._debug_label, RECV_MARK, frame)
            return frame
        # Windows:recv 对超长报文抛 WSAEMSGSIZE(无静默截断);捕获并
        # 转协议帧错,这样基类 last_error_category = PROTOCOL,与真断线区分
        try:
            frame = sock.recv(size)
        except socket.timeout as exc:
            raise TransportTimeoutError(
                f"UDP 接收超时({self._receive_timeout}s)", 0
            ) from exc
        except OSError as exc:
            if getattr(exc, "errno", None) == _WSAEMSGSIZE_ERRNO:
                log_warning(
                    self._debug_label,
                    "UDP 数据报超长(WinError 10040 WSAEMSGSIZE):缓冲 %dB,检查协议层 size 或对端报文",
                    size,
                )
                # 转 :class:`DeviceError`:基类按 DEVICE 分类(与真断线 TRANSPORT
                # 区分),且"不重试、不断线"——UDP 报文超长是协议帧问题,
                # 链路是好的,不应当触发连接重置。
                raise DeviceError(
                    f"UDP 报文超过缓冲({size}B),链路正常(对端报文超长)",
                    code=_WSAEMSGSIZE_ERRNO,
                ) from exc
            raise
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未初始化则抛出。"""
        if self._socket is None:
            raise TransportClosedError("UDP 未初始化,请先调用 connect()")
        return self._socket
