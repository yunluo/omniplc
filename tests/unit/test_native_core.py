"""原生异步核心语义测试:重试、写重试保护、退避门控、超时口径、取消、关闭。

这些语义在 ``AsyncBaseClient._execute`` / ``_connect_locked`` 里与同步基类逐条
对应,但**不是帧级对拍能覆盖的**(对拍只跑默认 ``retries=0`` 的成功/失败路径),
故单独立文件用可编程假传输固化:

- 读失败 → 拆连重连 + 重发(`retries`),成功后清空 ``last_error``
- **写保护**:``write_retries=0``(默认)时写失败**绝不重发**(危险动作)
- 退避门控:窗口内不再尝试连接,且门控期调用不污染 ``error_count``
- ``TransportTimeoutError``(0 字节已读)→ **不拆连**仍重试
- ``socket.timeout``(TCP 口径)→ 拆连
- 连接期取消 → 干净清场(不计失败);``close()`` 等锁不锯断在途事务
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, List, Optional, Sequence

import pytest

from omniplc.core.errors import TransportTimeoutError
from omniplc.native import AsyncModbusTcpClient
from omniplc.native.transport import AsyncBaseTransport
from omniplc.types import DataType

# FC03 读 1 寄存器 = 20 的完整 MBAP 响应(tid=1)
_RESP_TID1 = bytes([0x00, 0x01, 0x00, 0x00, 0x00, 0x05, 0x01, 0x03, 0x02, 0x00, 0x14])
_RESP_TID2 = bytes([0x00, 0x02, 0x00, 0x00, 0x00, 0x05, 0x01, 0x03, 0x02, 0x00, 0x14])
_ECHO_TID2 = bytes([0x00, 0x02, 0x00, 0x00, 0x00, 0x06, 0x01, 0x06, 0x00, 0x00, 0x00, 0x14])


def _chunks(frame: bytes) -> List[bytes]:
    """按 Modbus TCP 收包阶段切分响应:7 字节 MBAP 头 + 其余。"""
    return [frame[:7], frame[7:]]


class FakeTransport(AsyncBaseTransport):
    """可编程假传输:按队列决定**每次 ``recv``** 的行为,记录发送帧。

    行为项(注意粒度是"一次 recv 调用",不是"一次事务"——Modbus 会先收 7 字节
    头再按长度域收其余):``bytes``(作为本次收到的分片返回)、``"reset"``
    (抛 ``ConnectionResetError``)、``"timeout"``(抛 ``TransportTimeoutError``,
    UDP 口径)、``"sockettimeout"``(抛 ``socket.timeout``,TCP 口径)、
    ``"hang"``(永不返回)。
    """

    def __init__(
        self,
        behaviors: Sequence[Any],
        datagram: bool = False,
        connect_error: Optional[BaseException] = None,
        hang_connect: bool = False,
    ) -> None:
        super().__init__()
        self.datagram = datagram
        self.behaviors: List[Any] = list(behaviors)
        self.connect_error = connect_error
        self.hang_connect = hang_connect
        self.sent: List[bytes] = []
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.hang_connect:
            await asyncio.sleep(30)
        if self.connect_error is not None:
            raise self.connect_error

    def close(self) -> None:
        pass

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        self._pending = True

    async def recv(self, size: int) -> bytes:
        if not self.behaviors:
            raise ConnectionError("假传输行为队列已空")
        behavior = self.behaviors.pop(0)
        if behavior == "reset":
            raise ConnectionResetError("模拟链路抖动")
        if behavior == "timeout":
            raise TransportTimeoutError("UDP 接收超时(0.1s)", 0)
        if behavior == "sockettimeout":
            raise socket.timeout("TCP 接收超时(0.1s)")
        if behavior == "hang":
            await asyncio.sleep(30)
        return behavior  # type: ignore[no-any-return]


def _client(transport: AsyncBaseTransport, **kwargs: Any) -> AsyncModbusTcpClient:
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    client._create_transport = lambda: transport  # type: ignore[method-assign]
    for key, value in kwargs.items():
        setattr(client, key, value)
    return client


class _RecordingTransport(FakeTransport):
    """记录是否已关闭的假传输(守"清理钩子在关传输之前")。"""

    def __init__(self) -> None:
        super().__init__([])
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _HandshakeFailClient(AsyncModbusTcpClient):
    """握手必失败的原生客户端替身:记录清理钩子的调用时机。"""

    def __init__(self, transport: _RecordingTransport) -> None:
        super().__init__("127.0.0.1", 502, 1)
        self._create_transport = lambda: transport  # type: ignore[method-assign]
        self.hook_calls = 0
        self.closed_at_hook: Optional[bool] = None

    async def _after_connect(self) -> None:
        raise ConnectionResetError("模拟握手失败")

    async def _after_connect_failure(self) -> None:
        self.hook_calls += 1
        self.closed_at_hook = bool(getattr(self._transport, "closed", False))


def test_after_connect_failure_hook_runs_before_transport_close() -> None:
    """握手失败:清理钩子在**关传输之前**跑(注销帧才发得出去),随后照常关闭。"""
    transport = _RecordingTransport()
    client = _HandshakeFailClient(transport)

    assert asyncio.run(client.connect()) is False

    assert client.hook_calls == 1
    assert client.closed_at_hook is False  # 钩子执行时传输仍可用
    assert transport.closed is True  # 钩子之后照常关闭
    assert client.last_error is not None
    assert "连接初始化失败" in client.last_error


def test_read_retry_reconnects_and_resends() -> None:
    """读失败 → 重连 + 重发,成功后 ``last_error`` 清空、``transactions`` 只计 1 次。"""
    transport = FakeTransport(["reset"] + _chunks(_RESP_TID2))
    client = _client(transport, retries=1)

    result = asyncio.run(client.read_ushort("hr0"))

    assert result == (True, 20)
    assert transport.connect_calls == 2  # 首次建连 + 失败后惰性重连
    assert len(transport.sent) == 2  # 重发了一次(帧内事务号已递增)
    assert transport.sent[1][:2] == b"\x00\x02"  # 重发用的是新事务号
    assert client.stats["transactions"] == 1  # 一次公开调用 = 一次事务
    assert client.stats["error_count"] == 1  # 失败尝试记一次
    assert client.last_error is None  # 成功即清空
    assert client.connected is True
    asyncio.run(client.close())


def test_write_retries_default_protects_against_duplicate_write() -> None:
    """``write_retries=0``(默认):写失败**只发 1 帧**,绝不重复写危险动作。"""
    transport = FakeTransport(["reset"])
    client = _client(transport, retries=3, write_retries=0)

    ok = asyncio.run(client.write_ushort("hr0", 20))

    assert ok is False
    assert len(transport.sent) == 1, "写失败不得重发"
    assert client.connected is False  # 传输失败仍拆连,下次事务惰性重连
    asyncio.run(client.close())


def test_write_retries_enabled_resends_once() -> None:
    """显式 ``write_retries=1``:写失败重发一次(第二帧事务号递增)。"""
    transport = FakeTransport(["reset"] + _chunks(_ECHO_TID2))
    client = _client(transport, retries=3, write_retries=1)

    ok = asyncio.run(client.write_ushort("hr0", 20))

    assert ok is True
    assert len(transport.sent) == 2
    assert transport.sent[0][:2] == b"\x00\x01"
    assert transport.sent[1][:2] == b"\x00\x02"
    asyncio.run(client.close())


def test_backoff_gate_blocks_reconnect_without_polluting_error_count() -> None:
    """退避门控:窗口内不再尝试连接;门控期调用不新增 ``error_count``。"""
    transport = FakeTransport([], connect_error=ConnectionRefusedError("模拟拒绝"))
    client = _client(transport)

    assert asyncio.run(client.connect()) is False
    assert client.next_connect_in is not None  # 门控已武装
    assert transport.connect_calls == 1
    root_cause = client.last_error

    result = asyncio.run(client.read_ushort("hr0"))

    assert result == (False, None)
    assert transport.connect_calls == 1, "门控窗口内不得再次尝试连接"
    assert client.stats["error_count"] == 1, "门控拒绝不计失败(与同步层同口径)"
    assert client.last_error == root_cause, "last_error 保留武装门控的真实根因"

    client.reconnect_backoff = False  # 关掉门控后立即可重连
    assert asyncio.run(client.connect()) is False
    assert transport.connect_calls == 2
    asyncio.run(client.close())


def test_transport_timeout_keeps_connection_and_retries() -> None:
    """``TransportTimeoutError``(0 字节已读)→ 不拆连,且与其他传输失败一样重试。"""
    transport = FakeTransport(["timeout"] + _chunks(_RESP_TID2))
    client = _client(transport, retries=1)

    result = asyncio.run(client.read_ushort("hr0"))

    assert result == (True, 20)
    assert client.stats["disconnect_count"] == 0, "超时不得拆连"
    assert client.stats["device_error_count"] == 0, "超时不是设备错误码"
    assert len(transport.sent) == 2, "同连接上重发(未重连)"
    assert transport.connect_calls == 1
    asyncio.run(client.close())


def test_socket_timeout_disconnects_like_sync_tcp() -> None:
    """``socket.timeout``(TCP 口径)→ 按连接死亡拆连(与同步 TCP 同口径)。"""
    transport = FakeTransport(["sockettimeout"])
    client = _client(transport)

    result = asyncio.run(client.read_ushort("hr0"))

    assert result == (False, None)
    assert client.connected is False
    assert client.stats["disconnect_count"] == 1
    asyncio.run(client.close())


def test_cancel_during_connect_cleans_up() -> None:
    """连接期取消:CancelledError 原样传播,状态清干净且不计失败。"""

    async def scenario() -> None:
        transport = FakeTransport([], hang_connect=True)
        client = _client(transport)
        task = asyncio.ensure_future(client.connect())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.connected is False
        assert client._transport is None
        assert client.stats["connect_count"] == 0
        assert client.stats["error_count"] == 0
        assert client.next_connect_in is None, "取消不是建连失败,不应武装门控"
        await client.close()

    asyncio.run(scenario())


def test_close_waits_for_in_flight_transaction() -> None:
    """``close()`` 等锁:不锯断在途事务;在途事务结束后关闭完成且拒绝新调用。"""

    async def scenario() -> None:
        transport = FakeTransport(["hang"])
        client = _client(transport)
        client.receive_timeout = 30.0  # 让在途事务一直挂着(靠取消收尾)
        read_task = asyncio.ensure_future(client.read_ushort("hr0"))
        await asyncio.sleep(0.05)
        close_task = asyncio.ensure_future(client.close())
        await asyncio.sleep(0.05)
        assert not close_task.done(), "close 应等在途事务(不锯断)"
        read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read_task
        await asyncio.wait_for(close_task, 1.0)
        assert client.connected is False
        with pytest.raises(RuntimeError):
            await client.read_ushort("hr0")

    asyncio.run(scenario())


def test_typed_calls_share_one_transaction_template() -> None:
    """类型化调用与 ``read`` 共用同一事务模板:同地址/类型请求帧逐字节相同。"""
    typed_transport = FakeTransport(_chunks(_RESP_TID1))
    typed_client = _client(typed_transport, retries=0)
    generic_transport = FakeTransport(_chunks(_RESP_TID1))
    generic_client = _client(generic_transport, retries=0)

    assert asyncio.run(typed_client.read_ushort("hr0")) == (True, 20)
    assert asyncio.run(generic_client.read("hr0", DataType.USHORT)) == (True, 20)

    assert len(typed_transport.sent) == 1
    assert len(generic_transport.sent) == 1
    assert typed_transport.sent[0] == generic_transport.sent[0], "类型化与泛化请求帧必须同模板"

    asyncio.run(typed_client.close())
    asyncio.run(generic_client.close())
