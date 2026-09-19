"""UDP 传输实现。"""
from __future__ import annotations

import socket
from types import TracebackType
from typing import Optional

from .base import BaseTransport
from ..core.errors import TransportClosedError


class UdpTransport(BaseTransport):
    """UDP 传输:面向 Modbus UDP、MC over UDP、FINS/UDP。

    - 采用"已连接 UDP"语义:``connect()`` 固定对端,之后直接 send/recv
    - :meth:`recv` 返回**一条数据报**(最长 ``size`` 字节,超出截断),
      即一次收发对应一个协议帧
    - UDP 无连接概念,``connect()`` 只做本地套接字初始化,不会失败于对端
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

    def connect(self) -> None:
        """初始化 UDP 套接字并固定对端。

        :raises OSError: 地址解析失败或端口绑定错误
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self._connect_timeout)
        sock.connect((self._ip_address, self._port))
        sock.settimeout(self._receive_timeout)
        self._socket = sock

    def close(self) -> None:
        """关闭 UDP 套接字,幂等。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def send(self, data: bytes) -> None:
        """发送一条数据报到固定对端。

        :raises TransportClosedError: 未初始化
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        sock.send(data)

    def recv(self, size: int) -> bytes:
        """接收一条数据报。

        :param size: 缓冲上限(超出部分截断)
        :raises TransportClosedError: 未初始化
        :raises OSError: 接收超时
        """
        sock = self._require_socket()
        return sock.recv(size)

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未初始化则抛出。"""
        if self._socket is None:
            raise TransportClosedError("UDP 未初始化,请先调用 connect()")
        return self._socket

    def __enter__(self) -> "UdpTransport":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        self.close()
