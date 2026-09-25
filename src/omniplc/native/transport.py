"""原生 asyncio 传输层:TCP(流式)与 UDP(数据报)。

与同步传输层(:mod:`omniplc.transport`)**同契约、同错误口径**:协议层只依赖
``connect/close/send/recv/recv_some`` 与 :attr:`datagram` 标志,因此同步/异步
两套协议逻辑共用同一份编解码与地址解析。

实现选型(约束:Python 3.7 起、Windows 为主、零第三方依赖):

- **TCP** 走 :func:`asyncio.open_connection` 流式读写。``recv`` 用**绝对
  deadline**(对端涓流挤字节不能无限拖住读,与同步 TCP 同口径),超时抛
  :class:`socket.timeout`(OSError 语义 → 事务层按"连接可能已死 + 迟到响应
  残留在缓冲"拆连重连),对端关闭抛 ``TransportClosedError``。
- **UDP 不用** :meth:`loop.create_datagram_endpoint`:3.7 的
  ``ProactorEventLoop`` 没有数据报端点实现(``_make_datagram_transport``
  仅 selector 版,Proactor 上抛 ``NotImplementedError``),而 3.7 也没有
  ``loop.sock_recvfrom/sock_sendto``(3.11 才加)。改用**已连接 UDP socket +
  ``loop.sock_recv_into`` / ``loop.sock_sendall``**:这两个 API 在 3.7 的
  Selector 与 Proactor 上都有实现(后者走 IOCP),3.7~3.13 通用,形态与同步
  UDP 传输一致(一次收发一条数据报)。

与同步层的**能力差异**(asyncio 数据报路径所致,与解释器版本无关):拿不到
``MSG_TRUNC`` 真长,POSIX 下超长数据报的"静默截断"无法在传输层探测(同步层
会打一条 WARNING);Windows 的 ``WSAEMSGSIZE``(10040)照旧映射为
``DeviceError``。超长/长度不符由协议层自身的长度域校验兜底。

:meth:`AsyncBaseTransport.close` 是**同步方法**(不等底层完成):取消路径无法
``await``(任务已处于取消态,再 await 立即抛 ``CancelledError``),而拆连必须
在取消路径里也能完成。需要确保已发数据落地的调用方应先 ``await`` 到响应。

超时统一走模块内的 :func:`_await_with_timeout`,不用 :func:`asyncio.wait_for`:
后者的超时异常类跨版本变过(3.7 是 ``concurrent.futures.TimeoutError``,
3.11+ 是内建 ``TimeoutError``),而本库要求 TCP 超时抛 :class:`socket.timeout`
(OSError 语义 → 拆连)、UDP 超时抛 :class:`TransportTimeoutError`(不拆连),
自建等待器既省掉对具体类的依赖,也能在**外层被取消时显式取消内层任务**
(``wait_for`` 之外用 ``asyncio.wait`` 时,内层任务会继续跑,那正是本层要消灭
的"取消之后还在跑"行为)。
"""
from __future__ import annotations

import asyncio
import socket
from abc import ABC, abstractmethod
from concurrent.futures import CancelledError
from typing import Awaitable, List, Optional, Tuple, TypeVar

from ..core.constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_RECEIVE_TIMEOUT
from ..core.debug import RECV_MARK, SEND_MARK, log_frame, log_op, log_warning
from ..core.errors import DeviceError, TransportClosedError, TransportTimeoutError
from ..transport.tcp import _enable_keepalive

# Windows ``recv`` 对超长 UDP 报文抛 ``WSAEMSGSIZE``(errno 10040),与同步
# 传输层同一常量含义:协议帧问题而非链路问题。
_WSAEMSGSIZE_ERRNO = 10040

_T = TypeVar("_T")


async def _await_with_timeout(
    awaitable: Awaitable[_T], timeout: float
) -> Tuple[bool, Optional[_T]]:
    """等待 ``awaitable`` 至多 ``timeout`` 秒(内部函数)。

    :return: ``(是否在超时前完成, 结果)``;未完成时内层任务已被取消并等待
        其取消落地(不留悬空任务)
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except CancelledError:
        # 外层被取消:内层任务一起取消——否则它会继续跑(TCP 读还在等数据),
        # 正是本层要消灭的"取消不生效"行为
        task.cancel()
        raise
    if done:
        return True, task.result()
    task.cancel()
    try:
        await task
    except (CancelledError, Exception):
        pass  # 我们自己取消的,取消落地即可(inner 异常此处无需关心)
    return False, None


class AsyncBaseTransport(ABC):
    """异步传输基类:同步 :class:`~omniplc.transport.BaseTransport` 的 async 孪生。

    生命周期:``await connect()`` 后可多次 ``await send()``/``await recv()``,
    :meth:`close` 之后不可复用(重新 connect 会创建新的底层通道)。

    recv 语义(与同步层一致):

    - TCP:读取**恰好** ``size`` 字节(流式粘包由协议层按长度拆分)
    - UDP:返回**一条数据报**(最长 ``size`` 字节,超出部分在 POSIX 上静默截断)

    事件循环绑定:实例绑定创建它的那个事件循环(一个客户端一个循环),
    跨循环/跨线程使用不支持。

    :raises omniplc.core.errors.TransportClosedError: 未连接时调用 send/recv
    :raises OSError: 底层 socket 错误(含超时)
    """

    datagram: bool = False
    """True = 一问一答一数据报(recv 整包);False = 流式(协议层按长收包)。"""

    def __init__(self) -> None:
        self._connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
        self._receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT
        # 自上次 mark_synced 以来是否已发出请求:取消时判定链路是否可能
        # 残留未配对的应答(事务层据此决定拆连,见 AsyncBaseClient._execute)
        self._pending: bool = False

    @property
    def connect_timeout(self) -> float:
        """连接超时(秒),必须大于 0。"""
        return self._connect_timeout

    @connect_timeout.setter
    def connect_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"connect_timeout 必须大于 0,收到:{seconds}")
        self._connect_timeout = float(seconds)

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒),必须大于 0。

        异步实现**每次 await 都读当前值**,因此改属性立即生效
        (无需下发给 socket,与同步层的"立即生效"语义一致)。
        """
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"receive_timeout 必须大于 0,收到:{seconds}")
        self._receive_timeout = float(seconds)

    @property
    def pending(self) -> bool:
        """自上次 :meth:`mark_synced` 以来是否已发出过请求(取消判据)。"""
        return self._pending

    def mark_synced(self) -> None:
        """标记链路已回到同步点(一次事务成功完成时由事务层调用)。"""
        self._pending = False

    @abstractmethod
    async def connect(self) -> None:
        """建立底层通道。

        :raises OSError: 连接失败(拒绝/超时/DNS 解析失败等)
        """

    @abstractmethod
    def close(self) -> None:
        """关闭底层通道,幂等;不等待底层完成(见模块 docstring)。"""

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """发送一段字节。

        :raises TransportClosedError: 未连接
        :raises OSError: 发送失败或超时
        """

    @abstractmethod
    async def recv(self, size: int) -> bytes:
        """接收字节(语义见类文档)。

        :param size: 期望读取的字节数
        :return: 收到的字节
        :raises TransportClosedError: 未连接或对端关闭
        :raises OSError: 接收超时或其他错误
        """

    async def recv_some(self, max_bytes: int) -> bytes:
        """接收一批当前到达的数据(有数据即返回),默认退化为 :meth:`recv`。

        :param max_bytes: 单批字节数上限
        :return: 收到的字节(非空)
        """
        return await self.recv(max_bytes)

    async def __aenter__(self) -> "AsyncBaseTransport":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


class AsyncTcpTransport(AsyncBaseTransport):
    """TCP 传输:面向 Modbus TCP、MC 3E/4E/1E over TCP、FINS/TCP。

    - 连接后启用 ``TCP_NODELAY`` 与尽力而为的 ``SO_KEEPALIVE``(与同步 TCP
      传输同一套实现,直接复用 :func:`omniplc.transport.tcp._enable_keepalive`)
    - :meth:`recv` 受**整事务 deadline** 约束,超时抛 :class:`socket.timeout`
      (与同步 TCP 同口径:TIMEOUT 分类 + 拆连重试)
    - :attr:`receive_timeout` 改属性立即生效(每次 await 读当前值)
    """

    def __init__(self, ip_address: str, port: int) -> None:
        """初始化 TCP 传输。

        :param ip_address: 目标 IP 或主机名
        :param port: 目标端口
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._debug_label = f"tcp://{ip_address}:{port}"

    async def connect(self) -> None:
        """建立 TCP 连接(连接超时受 :attr:`connect_timeout` 约束)。

        :raises OSError: 连接被拒绝、超时或 DNS 解析失败
        """
        opened = await _await_with_timeout(
            asyncio.open_connection(self._ip_address, self._port), self._connect_timeout
        )
        if not opened[0] or opened[1] is None:
            raise socket.timeout(f"TCP 连接超时({self._connect_timeout}s)")
        reader, writer = opened[1]
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass  # 平台不支持时静默降级
            _enable_keepalive(sock)
        self._reader = reader
        self._writer = writer
        log_op(self._debug_label, "已连接")

    def close(self) -> None:
        """关闭 TCP 连接,幂等(不等待底层完成,见模块 docstring)。"""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass
            log_op(self._debug_label, "已断开")

    async def send(self, data: bytes) -> None:
        """发送字节,``await drain()`` 保证交给内核。

        :raises TransportClosedError: 未连接
        :raises OSError: 发送失败或超时
        """
        writer = self._require_writer()
        log_frame(self._debug_label, SEND_MARK, data)
        self._pending = True
        writer.write(data)
        drained = await _await_with_timeout(writer.drain(), self._receive_timeout)
        if not drained[0]:
            raise socket.timeout(f"TCP 发送超时({self._receive_timeout}s)")

    async def recv(self, size: int) -> bytes:
        """读取恰好 ``size`` 字节(循环读取,应对粘包/分段)。

        整事务受 ``receive_timeout`` **绝对 deadline** 约束:对端涓流挤字节
        不能无限拖住读,到点抛 ``socket.timeout``(OSError,事务层按连接死亡
        拆连——迟到响应残留在缓冲,拆连防串帧)。

        :param size: 期望读取的字节数
        :raises TransportClosedError: 未连接或对端关闭连接
        :raises OSError: 接收超时
        """
        reader = self._require_reader()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._receive_timeout
        chunks: List[bytes] = []
        received = 0
        while received < size:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise socket.timeout(f"TCP 接收超时({self._receive_timeout}s)")
            done, chunk = await _await_with_timeout(reader.read(size - received), remaining)
            if not done:
                # 已读部分字节留在 StreamReader 缓冲里,但半帧残留在连接上,
                # 按超时拆连处理(与同步 TCP 同口径:迟到响应会串帧)
                raise socket.timeout(f"TCP 接收超时({self._receive_timeout}s)")
            if not chunk:
                raise TransportClosedError("TCP 连接已被对端关闭")
            chunks.append(chunk)
            received += len(chunk)
        frame = b"".join(chunks)
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    async def recv_some(self, max_bytes: int) -> bytes:
        """读一批当前到达的数据(单次 ``read``,1~``max_bytes`` 字节)。

        :raises TransportClosedError: 未连接或对端关闭连接
        :raises OSError: 超时内无任何数据到达
        """
        reader = self._require_reader()
        done, chunk = await _await_with_timeout(
            reader.read(max_bytes), self._receive_timeout
        )
        if not done:
            raise socket.timeout(f"TCP 接收超时({self._receive_timeout}s)")
        if not chunk:
            raise TransportClosedError("TCP 连接已被对端关闭")
        log_frame(self._debug_label, RECV_MARK, chunk)
        return chunk

    def _require_reader(self) -> asyncio.StreamReader:
        """取当前读端,未连接则抛出。"""
        if self._reader is None:
            raise TransportClosedError("TCP 未连接,请先调用 connect()")
        return self._reader

    def _require_writer(self) -> asyncio.StreamWriter:
        """取当前写端,未连接则抛出。"""
        if self._writer is None:
            raise TransportClosedError("TCP 未连接,请先调用 connect()")
        return self._writer


class AsyncUdpTransport(AsyncBaseTransport):
    """UDP 传输:面向 Modbus UDP、MC over UDP、FINS/UDP。

    - **已连接 UDP** 语义:``connect()`` 固定对端,之后直接 send/recv
    - :meth:`recv` 返回**一条数据报**(最长 ``size`` 字节,超出截断),即一次
      收发对应一个协议帧
    - UDP 无连接概念,``connect()`` 只做地址解析与本地套接字初始化
    - 超时抛 :class:`omniplc.core.errors.TransportTimeoutError`(DeviceError
      子类):数据报整收无残留字节,按"链路完好不断线"——与同步 UDP 同口径
    - 平台差异见模块 docstring(超长报文的截断探测在 POSIX 上不可用)
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

    async def connect(self) -> None:
        """解析地址、创建并"连接"UDP 套接字(固定对端)。

        地址解析走 :meth:`loop.getaddrinfo`(异步,不阻塞事件循环);
        随后的 ``connect`` 只是给 UDP 套接字记下对端,不产生网络等待。

        :raises OSError: 地址解析失败或套接字创建失败
        """
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(
            self._ip_address, self._port, type=socket.SOCK_DGRAM
        )
        family, _, _, _, sockaddr = infos[0]
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.connect(sockaddr)
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

    async def send(self, data: bytes) -> None:
        """发送一条数据报到固定对端。

        :raises TransportClosedError: 未初始化
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        log_frame(self._debug_label, SEND_MARK, data)
        self._pending = True
        loop = asyncio.get_event_loop()
        # 已连接 UDP:一次 sendall 即一条数据报(Selector 循环只调一次
        # sock.send;Proactor 的 sock_sendall 为单次 IOCP 发送,不会拆分)
        done, _ = await _await_with_timeout(
            loop.sock_sendall(sock, data), self._receive_timeout
        )
        if not done:
            self._clear_stale_selector(sock)
            raise TransportTimeoutError(f"UDP 发送超时({self._receive_timeout}s)", 0)

    async def recv(self, size: int) -> bytes:
        """接收一条数据报。

        Windows 上 ``recv`` 对超长报文抛 ``WSAEMSGSIZE``(errno 10040),捕获后
        转 :class:`DeviceError`(与同步 UDP 传输同口径:DEVICE 分类、不重试、
        不断线——UDP 报文超长是协议帧问题,链路完好)。POSIX 上内核静默截断,
        传输层无法探测(见模块 docstring),由协议层长度校验兜底。

        :param size: 缓冲上限(超出部分截断)
        :raises TransportClosedError: 未初始化
        :raises TransportTimeoutError: 接收超时(不断线语义)
        :raises DeviceError: Windows 上报文超过缓冲时抛(code=10040)
        :raises OSError: 其他 OS 层错误
        """
        sock = self._require_socket()
        loop = asyncio.get_event_loop()
        buffer = bytearray(size)
        try:
            done, received = await _await_with_timeout(
                loop.sock_recv_into(sock, buffer), self._receive_timeout
            )
        except OSError as exc:
            if getattr(exc, "errno", None) == _WSAEMSGSIZE_ERRNO:
                log_warning(
                    self._debug_label,
                    "UDP 数据报超长(WinError 10040 WSAEMSGSIZE):缓冲 %dB,检查协议层 size 或对端报文",
                    size,
                )
                raise DeviceError(
                    f"UDP 报文超过缓冲({size}B),链路正常(对端报文超长)",
                    code=_WSAEMSGSIZE_ERRNO,
                ) from exc
            raise
        if not done or received is None:
            self._clear_stale_selector(sock)
            raise TransportTimeoutError(f"UDP 接收超时({self._receive_timeout}s)", 0)
        frame = bytes(buffer[:received])
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def _clear_stale_selector(self, sock: socket.socket) -> None:
        """摘掉取消 ``sock_recv_into`` 后残留的 selector 读注册(内部方法)。

        Python 3.7 的实现里,``loop.sock_recv_into`` 被取消时**不会**立即摘掉
        已注册的读事件(要等该 fd 下次可读时才自清理);若期间套接字被关闭,
        Windows 的 ``select`` 会对已关闭句柄抛 ``WSAENOTSOCK``(10038),把事件
        循环带崩。超时路径显式摘一次,之后的 close 就安全了。

        Proactor 循环的 ``sock_recv_into`` 走 IOCP,没有 selector 注册
        (``remove_reader`` 抛 ``NotImplementedError``),直接忽略。
        """
        try:
            asyncio.get_event_loop().remove_reader(sock.fileno())
        except (NotImplementedError, OSError, ValueError):
            pass

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未初始化则抛出。"""
        if self._socket is None:
            raise TransportClosedError("UDP 未初始化,请先调用 connect()")
        return self._socket
