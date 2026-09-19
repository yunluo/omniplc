"""TCP 传输实现。"""
from __future__ import annotations

import socket
from types import TracebackType
from typing import Optional

from .base import BaseTransport
from ..core.errors import TransportClosedError


class TcpTransport(BaseTransport):
    """TCP 传输:面向 Modbus TCP、MC 3E/4E/1E over TCP、FINS/TCP。

    - 连接后启用 ``TCP_NODELAY``,保证小报文立即发出
    - :meth:`recv` 阻塞读取恰好 ``size`` 字节,应对流式粘包
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
        self._socket = sock

    def close(self) -> None:
        """关闭 TCP 连接,幂等。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def send(self, data: bytes) -> None:
        """发送字节,阻塞直到全部发出。

        :raises TransportClosedError: 未连接
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        sock.sendall(data)

    def recv(self, size: int) -> bytes:
        """读取恰好 ``size`` 字节(循环读取,应对粘包/分段)。

        :param size: 期望读取的字节数
        :raises TransportClosedError: 未连接或对端关闭连接
        :raises OSError: 接收超时
        """
        sock = self._require_socket()
        chunks = []
        received = 0
        while received < size:
            chunk = sock.recv(size - received)
            if not chunk:
                raise TransportClosedError("TCP 连接已被对端关闭")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未连接则抛出。"""
        if self._socket is None:
            raise TransportClosedError("TCP 未连接,请先调用 connect()")
        return self._socket

    def __enter__(self) -> "TcpTransport":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        self.close()
