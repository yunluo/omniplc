"""测试共享:按脚本应答的假传输(无网络)。"""
from __future__ import annotations

from typing import List

from omniplc.transport.base import BaseTransport


class ScriptedTransport(BaseTransport):
    """按脚本应答的假传输:send 记录请求,recv 按序返回预置分片。

    TCP 用法:把完整应答帧按 recv 尺寸切片(如 ``frame[:9]``);
    UDP 用法:单个分片即整个数据报。
    """

    def __init__(self, chunks: List[bytes], datagram: bool = False) -> None:
        self.datagram = datagram
        self._chunks = list(chunks)
        self.sent = bytearray()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            raise ConnectionError("脚本分片已耗尽")
        return self._chunks.pop(0)
