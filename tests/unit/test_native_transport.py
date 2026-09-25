"""原生异步传输层测试(真内核 + 真事件循环;Selector 与 Proactor 双跑)。

覆盖:

- TCP 回环收发、``recv`` 的**绝对 deadline** 超时(``socket.timeout`` 口径)
- 对端关闭 → ``TransportClosedError``;未连接调用 → ``TransportClosedError``
- UDP 回环收发(``loop.sock_recv_into`` / ``sock_sendall``,两种事件循环都跑)
- UDP 主机名解析按 IPv4(与同步层同族)与**发送**超时的 OSError 口径
- UDP 静默对端 → ``TransportTimeoutError``(不断线语义)
- UDP 超长报文(WSAEMSGSIZE 10040)→ ``DeviceError(code=10040)``(假 socket)
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from omniplc.core.errors import DeviceError, TransportClosedError, TransportTimeoutError
from omniplc.native.transport import AsyncTcpTransport, AsyncUdpTransport
from omniplc.transport.udp import UdpTransport
from scripted_async import UdpResponder, loop_names, make_loop


@pytest.fixture(params=loop_names())
def loop(request: pytest.FixtureRequest) -> Any:
    """按事件循环类参数化的循环(Windows 上 Selector/Proactor 双跑)。"""
    event_loop = make_loop(request.param)
    yield event_loop
    event_loop.close()


# ----------------------------------------------------------------------
# TCP
# ----------------------------------------------------------------------


def test_tcp_roundtrip(loop: Any) -> None:
    """TCP:发什么收什么,``recv`` 读满恰好 size 字节。"""

    async def scenario() -> None:
        async def handle(reader: Any, writer: Any) -> None:
            data = await reader.readexactly(4)
            writer.write(b"ACK:" + data)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = AsyncTcpTransport("127.0.0.1", port)
        await transport.connect()
        await transport.send(b"ping")
        assert await transport.recv(8) == b"ACK:ping"
        assert transport.pending is True  # 已发出请求,尚未 mark_synced
        transport.mark_synced()
        assert transport.pending is False
        transport.close()
        server.close()
        await server.wait_closed()

    loop.run_until_complete(scenario())


def test_tcp_recv_timeout_is_socket_timeout(loop: Any) -> None:
    """TCP 读超时抛 ``socket.timeout``(OSError 语义 → 事务层拆连,与同步同口径)。"""

    async def scenario() -> None:
        server = await asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        transport = AsyncTcpTransport("127.0.0.1", port)
        transport.receive_timeout = 0.2
        await transport.connect()
        with pytest.raises(socket.timeout):
            await transport.recv(4)
        transport.close()
        server.close()
        await server.wait_closed()

    loop.run_until_complete(scenario())


def test_tcp_peer_close_raises_closed(loop: Any) -> None:
    """对端关闭连接 → ``TransportClosedError``(事务层拆连、下次惰性重连)。"""

    async def scenario() -> None:
        async def handle(reader: Any, writer: Any) -> None:
            await reader.read(16)
            writer.close()  # 直接关,不发任何字节

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = AsyncTcpTransport("127.0.0.1", port)
        await transport.connect()
        await transport.send(b"ping")
        with pytest.raises(TransportClosedError):
            await transport.recv(4)
        transport.close()
        server.close()
        await server.wait_closed()

    loop.run_until_complete(scenario())


def test_tcp_not_connected_raises_closed(loop: Any) -> None:
    """未连接直接 send/recv → ``TransportClosedError``(不裸抛 AttributeError)。"""

    async def scenario() -> None:
        transport = AsyncTcpTransport("127.0.0.1", 1)
        with pytest.raises(TransportClosedError):
            await transport.send(b"x")
        with pytest.raises(TransportClosedError):
            await transport.recv(1)
        transport.close()  # 幂等

    loop.run_until_complete(scenario())


# ----------------------------------------------------------------------
# UDP
# ----------------------------------------------------------------------


def test_udp_roundtrip(loop: Any) -> None:
    """UDP:一问一答一数据报(两种事件循环都走通)。"""
    responder = UdpResponder()
    port = responder.start()
    try:

        async def scenario() -> None:
            transport = AsyncUdpTransport("127.0.0.1", port)
            await transport.connect()
            assert transport.datagram is True
            await transport.send(b"ping")
            assert await transport.recv(256) == b"PONG:ping"
            transport.close()

        loop.run_until_complete(scenario())
        assert responder.received == [b"ping"]
    finally:
        responder.stop()


def test_udp_timeout_is_transport_timeout(loop: Any) -> None:
    """UDP 静默对端 → ``TransportTimeoutError``(0 字节已读,事务层不断线)。"""
    responder = UdpResponder(transform=lambda data: None)  # 只记录不回应
    port = responder.start()
    try:

        async def scenario() -> None:
            transport = AsyncUdpTransport("127.0.0.1", port)
            transport.receive_timeout = 0.2
            await transport.connect()
            await transport.send(b"ping")
            with pytest.raises(TransportTimeoutError):
                await transport.recv(256)
            transport.close()

        loop.run_until_complete(scenario())
    finally:
        responder.stop()


def test_udp_cancel_then_close_keeps_loop_alive() -> None:
    """取消 UDP 接收后关套接字:不得残留 selector 注册(否则``select()``崩循环)。

    3.7 的 ``sock_recv_into`` 被取消时不立即摘 reader 注册,若此时关句柄,
    ``select()`` 会对已关闭句柄抛 ``WSAENOTSOCK``(Windows 10038 / POSIX
    ``EBADF``)——而 3.7 在 Windows 上的**默认**循环正是 Selector,故本用例
    固定走 Selector 循环;修复前实测 ``OSError(10038)`` 从 ``run_until_complete``
    逃逸(即事件循环被带崩),修复后在此通过。
    """
    responder = UdpResponder(transform=lambda data: None)  # 只记录不回应
    port = responder.start()
    try:

        async def scenario() -> None:
            transport = AsyncUdpTransport("127.0.0.1", port)
            transport.receive_timeout = 5.0
            await transport.connect()
            task = asyncio.ensure_future(transport.recv(256))
            await asyncio.sleep(0.05)  # 让 sock_recv_into 注册 reader
            assert not task.done()  # 仍在等待 → reader 已注册(避免空跑)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            transport.close()
            await asyncio.sleep(0.05)  # 让 select() 带着陈旧 fd 再跑一次

        loop = make_loop("SelectorEventLoop")
        try:
            loop.run_until_complete(scenario())
        finally:
            loop.close()
    finally:
        responder.stop()


def test_udp_oversize_datagram_maps_to_device_error() -> None:
    """超长数据报(WSAEMSGSIZE 10040)→ ``DeviceError(code=10040)``,与同步层同口径。

    用假 socket 强制该分支(Windows 内核才抛 10040,POSIX 静默截断);
    假 socket 只在 Selector 循环下有意义(Proactor 走 IOCP 需要真句柄),
    故本用例显式指定 Selector 循环。
    """
    loop = make_loop("SelectorEventLoop")

    class _OversizeDatagramSocket:
        """``recv_into`` 直接抛 WSAEMSGSIZE 的假 UDP socket。"""

        def recv_into(self, buffer: Any) -> int:
            raise OSError(10040, "WSAEMSGSIZE")

        def close(self) -> None:
            pass

    async def scenario() -> None:
        transport = AsyncUdpTransport("127.0.0.1", 1)
        transport._socket = _OversizeDatagramSocket()  # type: ignore[assignment]
        with pytest.raises(DeviceError) as excinfo:
            await transport.recv(64)
        assert excinfo.value.code == 10040
        assert "超过缓冲" in str(excinfo.value)
        transport.close()

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_udp_hostname_resolves_to_ipv4_like_sync(loop: Any) -> None:
    """主机名解析按 AF_INET(与同步层同族):IPv4-only 对端用 ``localhost`` 也要通。

    回归护栏:UDP 的 ``connect`` 不会失败、没有 TCP 那样的候选回退,若不约束
    地址族,``localhost``/双栈主机名的首个 addrinfo 可能是 AF_INET6(本机实测
    即如此)→ 数据报发到 ``::1`` 而同步层发到 127.0.0.1,两层行为分裂。
    """
    responder = UdpResponder()  # 只绑 127.0.0.1,不监听 ::1
    port = responder.start()
    try:
        # 对照基线:同一主机名 + 同一端口,同步层照常通
        sync = UdpTransport("localhost", port)
        sync.receive_timeout = 0.5
        sync.connect()
        sync.send(b"ping")
        assert sync.recv(256) == b"PONG:ping"
        sync.close()

        async def scenario() -> None:
            transport = AsyncUdpTransport("localhost", port)
            transport.receive_timeout = 0.5
            assert transport.peer_ip is None  # 连接前无对端信息
            await transport.connect()
            sock = transport._socket
            assert sock is not None and sock.family == socket.AF_INET
            assert transport.peer_ip == "127.0.0.1"  # 已解析的对端 IP(供节点推导复用)
            await transport.send(b"ping")
            assert await transport.recv(256) == b"PONG:ping"
            transport.close()
            assert transport.peer_ip is None  # 断开后回到 None

        loop.run_until_complete(scenario())
    finally:
        responder.stop()


def test_udp_send_timeout_is_socket_timeout(
    loop: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UDP **发送**超时按 OSError 语义抛(与同步 ``sock.send`` 同口径:拆连重试)。

    真实触发条件是本地发送缓冲打满(罕见),用真 socket 难以稳定复现,也无法用
    假 socket 走通(selector 注册要真句柄、IOCP 要真 handle),故直接注入"等待器
    超时"这一条件,验证异常映射本身:必须是 ``socket.timeout`` 而不是
    ``TransportTimeoutError``(后者语义是"接收 0 字节、不断线")。
    """
    from omniplc.native import transport as transport_module

    async def fake_wait(awaitable: Any, timeout: float) -> Any:
        awaitable.close()  # 丢弃未等待的协程,避免 "never awaited" 告警
        return False, None

    monkeypatch.setattr(transport_module, "_await_with_timeout", fake_wait)

    async def scenario() -> None:
        transport = AsyncUdpTransport("127.0.0.1", 9999)
        transport.receive_timeout = 0.1
        await transport.connect()
        with pytest.raises(socket.timeout):
            await transport.send(b"x")
        transport.close()

    loop.run_until_complete(scenario())


def test_udp_not_connected_raises_closed(loop: Any) -> None:
    """未初始化直接 send/recv → ``TransportClosedError``。"""

    async def scenario() -> None:
        transport = AsyncUdpTransport("127.0.0.1", 1)
        with pytest.raises(TransportClosedError):
            await transport.send(b"x")
        with pytest.raises(TransportClosedError):
            await transport.recv(1)
        transport.close()

    loop.run_until_complete(scenario())
