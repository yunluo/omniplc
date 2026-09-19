"""传输层基类:协议层零感知走线的抽象契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Optional, TypeVar

from ..core.constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_RECEIVE_TIMEOUT

_T = TypeVar("_T", bound="BaseTransport")


class BaseTransport(ABC):
    """所有传输实现的抽象基类。

    生命周期:``connect()`` 后可多次 ``send()``/``recv()``,``close()``
    之后不可复用(重新 :meth:`connect` 会创建新的底层通道)。

    recv 语义:

    - TCP 实现:阻塞读取**恰好** ``size`` 字节(流式粘包由协议层按长度拆分)
    - UDP 实现:返回**一条数据报**(最长 ``size`` 字节,超出部分截断)
    - 串口实现:阻塞读取恰好 ``size`` 字节,超时抛出

    线程安全:实现类自身不保证线程安全,只允许由持有客户端事务锁的
    :class:`omniplc.core.BaseClient` 串行访问。

    :raises omniplc.core.errors.TransportClosedError: 未连接时调用 send/recv
    :raises OSError: 底层 socket/串口错误(含超时)
    """

    datagram: bool = False
    """True = 一问一答一数据报(recv 整包);False = 流式(协议层按长收包)。"""

    def __init__(self) -> None:
        self._connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
        self._receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT

    @property
    def connect_timeout(self) -> float:
        """连接超时(秒),必须大于 0。"""
        return self._connect_timeout

    @connect_timeout.setter
    def connect_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("connect_timeout 必须大于 0,收到:{}".format(seconds))
        self._connect_timeout = float(seconds)

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒),必须大于 0。"""
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("receive_timeout 必须大于 0,收到:{}".format(seconds))
        self._receive_timeout = float(seconds)

    @abstractmethod
    def connect(self) -> None:
        """建立底层通道。

        :raises OSError: 连接失败(拒绝/超时/DNS 解析失败等)
        """

    @abstractmethod
    def close(self) -> None:
        """关闭底层通道,重复调用应保持幂等。"""

    @abstractmethod
    def send(self, data: bytes) -> None:
        """发送一段字节。

        :param data: 待发送字节
        :raises TransportClosedError: 未连接
        :raises OSError: 发送失败或超时
        """

    @abstractmethod
    def recv(self, size: int) -> bytes:
        """接收字节(语义见类文档)。

        :param size: 期望读取的字节数
        :return: 收到的字节
        :raises TransportClosedError: 未连接或对端关闭
        :raises OSError: 接收超时或其他错误
        """
        raise NotImplementedError

    def __enter__(self: _T) -> _T:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[type] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        self.close()
