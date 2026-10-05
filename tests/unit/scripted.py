"""测试共享:按脚本应答的假传输(无网络)与真传输语义回归脚手架。"""

from __future__ import annotations

import socket
from typing import List, Optional

from omniplc.transport import TcpTransport
from omniplc.transport.base import BaseTransport


class ScriptedTransport(BaseTransport):
    """按脚本应答的假传输:send 记录请求,recv 按序返回预置分片。

    TCP 用法:把完整应答帧按 recv 尺寸切片(如 ``frame[:9]``);
    UDP 用法:单个分片即整个数据报。

    :param chunks: 预置响应分片(本轮事务的应答)
    :param datagram: True = 数据报走线语义
    :param stale: 预置**陈旧帧**队列(:meth:`drain` 排掉的就是它们,
        与 ``chunks`` 分离——排空不得误伤本轮应答)
    """

    def __init__(
        self,
        chunks: List[bytes],
        datagram: bool = False,
        stale: Optional[List[bytes]] = None,
    ) -> None:
        self.datagram = datagram
        self._chunks = list(chunks)
        self._stale = list(stale or [])
        self.sent = bytearray()
        self.drained: List[bytes] = []
        self.drain_count = 0
        self.events: List[str] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.events.append("send")
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            raise ConnectionError("脚本分片已耗尽")
        return self._chunks.pop(0)

    def drain(self) -> int:
        """排空预置陈旧帧队列(流式走线默认 no-op,与真传输同口径)。"""
        if not self.datagram:
            return 0
        self.events.append("drain")
        self.drain_count += 1
        self.drained.extend(self._stale)
        return len(self._stale)


class ChunkSocket:
    """size 感知假 socket:按 size 从剩余字节池切分,弹尽后保持静默超时。

    挂在真 :class:`TcpTransport._socket` 上验证"读满恰好 size"的凑满循环
    语义——``ScriptedTransport.recv`` 忽略 size 整块弹出,会掩盖该语义
    (见 OpenTcp 短帧收不到修复背景,commit bc1ae73)。
    """

    def __init__(self, chunks: List[bytes]) -> None:
        self._pool = bytearray(b"".join(chunks))
        self.timeout: Optional[float] = None

    def settimeout(self, value: Optional[float]) -> None:
        self.timeout = value

    def gettimeout(self) -> Optional[float]:
        """当前超时值(海康 TCP 命令应答的 MSG_PEEK 后缀探测用)。"""
        return self.timeout

    def recv(self, size: int, flags: int = 0) -> bytes:
        if not self._pool:
            raise socket.timeout("timed out")
        chunk = bytes(self._pool[:size])
        if not flags & socket.MSG_PEEK:
            del self._pool[:size]
        return chunk

    def sendall(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass


def mount_real_tcp(
    client: object, chunks: List[bytes], receive_timeout: float = 5.0
) -> TcpTransport:
    """把真 TcpTransport + size 感知假 socket 挂到客户端并置为已连接。

    用于"读满恰好 size 字节"语义的回归测试(绕过真建链;调用方需自行
    预置会话状态,如 AB 的 ``_session_handle``)。
    """
    transport = TcpTransport(client._ip_address, client._port)  # type: ignore[attr-defined]
    transport._socket = ChunkSocket(chunks)  # type: ignore[assignment]
    transport.receive_timeout = receive_timeout
    client._transport = transport  # type: ignore[attr-defined]
    client._connected = True  # type: ignore[attr-defined]
    return transport
