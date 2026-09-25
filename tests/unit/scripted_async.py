"""原生异步测试共享脚手架:脚本化异步传输、进程内应答器与双事件循环运行器。

与 :mod:`scripted` 同思想(无网络的确定性脚本),面向 :mod:`omniplc.native`:

- :class:`ScriptedAsyncTransport` 注入客户端,``send`` 记录请求字节、
  ``recv`` 按序弹出预置分片(对齐同步侧 ``ScriptedTransport`` 的用法)
- :class:`TcpResponder` 进程内 asyncio TCP 服务端:回显 MBAP 事务号/单元号,
  请求体交给 ``pdu_handler`` 合成响应(真内核 + 真事件循环路径)
- :class:`UdpResponder` 线程内阻塞 socket 的 UDP 应答器:不依赖事件循环,
  因此 ProactorEventLoop(3.7 无数据报端点)下同样可用
"""
from __future__ import annotations

import asyncio
import socket
import threading
from typing import Callable, List, Optional
from omniplc.core.errors import TransportTimeoutError
from omniplc.native.transport import AsyncBaseTransport

# 3.7 Windows 默认 Selector、3.8+ 默认 Proactor,两者对 socket API 的支持面
# 不同(3.7 的 Proactor 没有数据报端点),原生传输的用例一律双跑验证。
_LOOP_NAMES = ["SelectorEventLoop"]
if hasattr(asyncio, "ProactorEventLoop"):
    _LOOP_NAMES.append("ProactorEventLoop")


def loop_names() -> List[str]:
    """可用的先事件循环类名(Windows 为两种,其他平台只有 Selector)。"""
    return list(_LOOP_NAMES)


def make_loop(name: str) -> asyncio.AbstractEventLoop:
    """按类名构造事件循环。"""
    return getattr(asyncio, name)()


async def close_server(server: asyncio.AbstractServer) -> None:
    """关闭测试服务端,且**不** ``await wait_closed()``。

    3.7 的 ``Server.wait_closed()`` 只等监听套接字关闭就返回;3.12 起它还会等
    **每个连接由应用侧关净**——"接受但不回应"的测试 handler(读超时类用例必需的
    形态,如 ``lambda r, w: None``)从不关 writer,``wait_closed()`` 于是永久挂住
    (纯标准库即可复现,不需要本库参与)。测试收尾一律走本函数。
    """
    server.close()


class ScriptedAsyncTransport(AsyncBaseTransport):
    """按脚本应答的假异步传输:send 记录请求,recv 按序返回预置分片。

    与真传输同契约(含 :attr:`receive_timeout` 语义):``hang=True`` 时
    ``recv`` 等到 ``receive_timeout`` 就按走线口径抛超时——TCP 抛
    :class:`socket.timeout`(0 字节已读但连接可能已死 → 拆连),UDP 抛
    :class:`~omniplc.core.errors.TransportTimeoutError`(数据报无残渣 → 不断线)。

    :param chunks: 预置响应分片(TCP 按 recv 尺寸切片;UDP 单分片即整包)
    :param hang: True 时 recv 永不给数据(用于取消/超时用例)
    """

    def __init__(
        self, chunks: List[bytes], datagram: bool = False, hang: bool = False
    ) -> None:
        super().__init__()
        self.datagram = datagram
        self._chunks = list(chunks)
        self._hang = hang
        self.sent = bytearray()

    async def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def send(self, data: bytes) -> None:
        self.sent.extend(data)
        # 与真传输同语义:发出请求即进入"未配对"状态(取消判据)
        self._pending = True

    async def recv(self, size: int) -> bytes:
        if self._hang:
            await asyncio.sleep(self._receive_timeout * 3)  # 永不主动给数据
            if self.datagram:
                raise TransportTimeoutError(
                    f"UDP 接收超时({self._receive_timeout}s)", 0
                )
            raise socket.timeout(f"TCP 接收超时({self._receive_timeout}s)")
        if not self._chunks:
            raise ConnectionError("脚本分片已耗尽")
        return self._chunks.pop(0)


class RawTcpServer:
    """进程内 asyncio TCP 服务端:连接处理逻辑由用例提供。

    :meth:`stop` 会取消未结束的连接任务——否则事件循环关闭时那些"还在等
    下一个请求"的协程会抛 ``Event loop is closed`` 噪声,掩盖真正的失败。
    """

    def __init__(self, handler: Callable[[object, object], object]) -> None:
        """:param handler: ``async def handler(reader, writer)`` 连接处理协程"""
        self._connection_handler = handler
        self.port = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> int:
        """启动服务端,返回监听端口。"""
        self._server = await asyncio.start_server(self._dispatch, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """关闭服务端并取消在等数据的连接任务(幂等)。

        顺序刻意是"先取消连接任务 → 再关服务端":3.12 起
        :meth:`asyncio.AbstractServer.wait_closed` 还会等**每个连接由应用侧关净**,
        先取消在等数据的任务才可能关净(否则会等到超时)。等待本身也加了上限,
        3.12 下即便仍有连接未关净也不至于挂死测试。
        """
        pending, self._tasks = self._tasks, []
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 1)
            except asyncio.TimeoutError:
                pass  # 3.12:连接未由应用侧关净时 wait_closed 会一直等,不再阻塞测试
            self._server = None

    async def _dispatch(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.append(task)
        try:
            await self._connection_handler(reader, writer)
        finally:
            if task is not None and task in self._tasks:
                self._tasks.remove(task)
            writer.close()


class TcpResponder(RawTcpServer):
    """进程内 asyncio TCP 应答器(Modbus MBAP 语义)。

    回显请求的 MBAP 事务号与单元号,PDU 由 ``pdu_handler`` 按请求 PDU 合成。
    """

    def __init__(
        self,
        pdu_handler: Callable[[bytes], bytes],
        delay: float = 0.0,
    ) -> None:
        """:param pdu_handler: 请求 PDU → 响应 PDU
        :param delay: 收到请求后延迟多久应答(模拟慢 PLC)
        """
        self.sent: List[bytes] = []
        self._pdu_handler = pdu_handler
        self._delay = delay
        super().__init__(self._serve_requests)

    async def _serve_requests(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                length = int.from_bytes(header[4:6], "big")
                body = await reader.readexactly(length - 1)
                self.sent.append(header + body)
                if self._delay:
                    await asyncio.sleep(self._delay)
                pdu = self._pdu_handler(body[1:])
                writer.write(
                    header[:2]
                    + b"\x00\x00"
                    + (len(pdu) + 1).to_bytes(2, "big")
                    + header[6:7]
                    + pdu
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass  # 对端关闭(用例结束)属正常收尾


class UdpResponder:
    """线程内阻塞 socket 的 UDP 应答器(与事件循环解耦,Proactor 下同样可用)。

    :param transform: 收到的数据报 → 回应数据报;返回 ``None`` 表示**只记录
        不回应**(构造"对端存在但静默"的超时场景)
    """

    def __init__(
        self,
        transform: Optional[Callable[[bytes], Optional[bytes]]] = None,
        recv_timeout: float = 5.0,
    ) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = int(self._sock.getsockname()[1])
        self._transform = transform or (lambda data: b"PONG:" + data)
        self._recv_timeout = recv_timeout
        self._stop = threading.Event()
        self.received: List[bytes] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> int:
        """启动线程,返回监听端口。"""
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """停止应答器(幂等)。"""
        self._stop.set()
        self._thread.join(timeout=self._recv_timeout)
        self._sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self.received.append(data)
            reply = self._transform(data)
            if reply is not None:
                try:
                    self._sock.sendto(reply, addr)
                except OSError:
                    pass
