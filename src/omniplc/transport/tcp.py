"""TCP 传输实现。"""
from __future__ import annotations

import socket
import time
from typing import Optional

from .base import BaseTransport
from ..core.constants import (
    TCP_KEEPALIVE_COUNT,
    TCP_KEEPALIVE_IDLE,
    TCP_KEEPALIVE_INTERVAL,
)
from ..core.debug import RECV_MARK, SEND_MARK, log_frame, log_op
from ..core.errors import TransportClosedError


class TcpTransport(BaseTransport):
    """TCP 传输:面向 Modbus TCP、MC 3E/4E/1E over TCP、FINS/TCP。

    - 连接后启用 ``TCP_NODELAY``,保证小报文立即发出
    - 尽力启用 ``SO_KEEPALIVE``(平台支持时):空闲半开连接(PLC 断电/
      断网)由系统在 keepalive 空闲秒后探测,不必等下一次收发才暴露
    - :meth:`recv` 阻塞读取恰好 ``size`` 字节(应对流式粘包),并受
      **整事务 deadline** 约束——涓流对端不能无限拖住读
    - :attr:`receive_timeout` 修改后**立即作用于已连接 socket**
    """

    def __init__(self, ip_address: str, port: int) -> None:
        """初始化 TCP 传输。

        :param ip_address: 目标 IP 或主机名
        :param port: 目标端口
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._debug_label = f"tcp://{ip_address}:{port}"

    @BaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒);已连接时立即下发到 socket。"""
        BaseTransport.receive_timeout.fset(self, seconds)  # type: ignore[attr-defined]
        sock = self._socket
        if sock is not None:
            sock.settimeout(self._receive_timeout)

    def connect(self) -> None:
        """建立 TCP 连接。

        :raises OSError: 连接被拒绝、超时或 DNS 解析失败
        """
        sock = socket.create_connection(
            (self._ip_address, self._port),
            timeout=self._connect_timeout,
        )
        sock.settimeout(self._receive_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _enable_keepalive(sock)
        self._socket = sock
        log_op(self._debug_label, "已连接")

    def close(self) -> None:
        """关闭 TCP 连接,幂等。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
            log_op(self._debug_label, "已断开")

    def send(self, data: bytes) -> None:
        """发送字节,阻塞直到全部发出。

        :raises TransportClosedError: 未连接
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        log_frame(self._debug_label, SEND_MARK, data)
        sock.sendall(data)

    def recv(self, size: int) -> bytes:
        """读取恰好 ``size`` 字节(循环读取,应对粘包/分段)。

        整事务受 ``receive_timeout`` 绝对 deadline 约束:对端涓流挤字节
        不能无限拖住读,到点抛 ``socket.timeout``(OSError,由事务层按
        连接死亡拆连——迟到响应残留在缓冲,拆连防串帧)。

        :param size: 期望读取的字节数
        :raises TransportClosedError: 未连接或对端关闭连接
        :raises OSError: 接收超时
        """
        sock = self._require_socket()
        deadline = time.monotonic() + self._receive_timeout
        chunks = []
        received = 0
        while received < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(
                    f"TCP 接收超时({self._receive_timeout}s)"
                )
            sock.settimeout(remaining)
            chunk = sock.recv(size - received)
            if not chunk:
                raise TransportClosedError("TCP 连接已被对端关闭")
            chunks.append(chunk)
            received += len(chunk)
        frame = b"".join(chunks)
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未连接则抛出。"""
        if self._socket is None:
            raise TransportClosedError("TCP 未连接,请先调用 connect()")
        return self._socket


def _enable_keepalive(sock: socket.socket) -> None:
    """尽力启用 TCP keepalive(平台差异,失败静默降级,内部函数)。

    Linux:``TCP_KEEPIDLE/INTVL/CNT``;Windows:``SIO_KEEPALIVE_VALS``。
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPALIVE_IDLE)
            sock.setsockopt(
                socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPALIVE_INTERVAL
            )
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPALIVE_COUNT)
        elif hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, TCP_KEEPALIVE_IDLE * 1000, TCP_KEEPALIVE_INTERVAL * 1000),
            )
    except OSError:
        pass  # 平台/驱动不支持时静默降级,不阻断连接
