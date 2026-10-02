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
import threading
from typing import Any, List, Optional, Sequence

import pytest

from omniplc.core.constants import RECONNECT_BACKOFF_MAX
from omniplc.core.errors import TransportTimeoutError
from omniplc.native import AsyncModbusTcpClient
from omniplc.native.transport import AsyncBaseTransport
from omniplc.core.types import DataType

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


def test_close_wins_gate_race_and_queued_transaction_cannot_revive() -> None:
    """回归:close() 先拿锁的时序下,排队中的事务拿到锁后不得复活建连。

    事务在锁外过完 ``_ensure_open`` 后挂起等锁,``close()`` 插队先拿锁置
    ``_closed`` 并断开;事务随后进锁,若循环体内复查关闸缺失,会看到
    ``_connected=False`` 而重新 ``_connect_locked`` ——已关闸客户端向
    PLC 发出新请求且新传输无人回收。修复后事务应在锁内复查处抛
    ``RuntimeError``。
    """

    async def scenario() -> None:
        transport = FakeTransport([_RESP_TID1])
        client = _client(transport)
        gate = asyncio.Event()
        armed = [False]
        real_lock_holder = [None]

        original_guard = client._guard

        class _InterposedLock:
            """代理锁:__aenter__ 时先跑完 close(),再放行事务拿真锁。"""

            async def __aenter__(self):
                armed[0] = False
                gate.set()
                await client.close()
                real_lock = original_guard()
                real_lock_holder[0] = real_lock
                return await real_lock.__aenter__()

            async def __aexit__(self, *exc):
                if real_lock_holder[0] is None:
                    # close() 已把锁标为换循环重建:拿当前真锁做退出
                    real_lock_holder[0] = original_guard()
                return await real_lock_holder[0].__aexit__(*exc)

        class _InterposedGuard:
            """模拟 _guard 返回锁:等锁那次(事务插队点)返回插队代理。"""

            def __call__(self):
                if armed[0]:
                    armed[0] = False
                    return _InterposedLock()
                return original_guard()

        client._guard = _InterposedGuard()  # type: ignore[method-assign]

        # 先武装再启动事务:事务先在未连接下走 connect()(真锁),随后
        # 事务本体进锁——armed 标志恰好落在那次 _guard() 调用上触发插队
        armed[0] = True
        read_task = asyncio.ensure_future(client.read_ushort("hr0"))
        # connect 的锁 acquisition 与事务等锁之间隔若干事件循环拍
        for _ in range(8):
            await asyncio.sleep(0)
        assert gate.is_set(), "close 未插队到事务拿锁之前"
        with pytest.raises(RuntimeError):
            await read_task
        assert client.connected is False
        assert client._transport is None, "close 已断开,事务不得重建连接"

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


def test_backoff_exponent_is_capped() -> None:
    """退避指数封顶(与同步基类同口径):失败次数很大时不得抛 ``OverflowError``。

    长跑轮询下 ``_connect_fail_count`` 可持续增长,``2.0 ** 大指数`` 会抛
    ``OverflowError`` 逃出连接路径——退避门控本意是节流,不该把调用方打断。
    """
    transport = FakeTransport([], connect_error=ConnectionRefusedError("模拟拒绝"))
    client = _client(transport)
    client._connect_fail_count = 5000  # 远超 2**1024 的理论累积值

    assert asyncio.run(client.connect()) is False  # 不抛 OverflowError

    assert client.next_connect_in is not None
    assert 0.0 <= client.next_connect_in <= RECONNECT_BACKOFF_MAX
    asyncio.run(client.close())


def test_write_tag_rejects_nonfinite_scale_offset() -> None:
    """native write_tag 拒绝 inf/NaN scale/offset(直传 Tag 绕过 TagTable 校验)。

    与同步层同守卫:scale=inf 时逆缩放 ``(值-offset)/inf = 0.0 →
    is_integer() → int 0`` 会静默写 0(触发设备动作),NaN 写 nan。
    """
    from omniplc.core.tag import Tag

    transport = FakeTransport([b"\x00\x00\x00\x00\x00\x06\x01\x06\x00\x00\x00\x05"])
    client = _client(transport)
    asyncio.run(client.connect())
    try:
        tag = Tag(tag_id="t", address="hr0", data_type="short", scale=float("inf"))
        with pytest.raises(ValueError, match="必须为有限数"):
            asyncio.run(client.write_tag(tag, 100))
        nan_tag = Tag(tag_id="t", address="hr0", data_type="short", offset=float("nan"))
        with pytest.raises(ValueError, match="必须为有限数"):
            asyncio.run(client.write_tag(nan_tag, 100))
        assert transport.sent == []  # 校验失败零字节发送
    finally:
        asyncio.run(client.close())


def test_write_tag_inverse_scale_rounds_float_noise() -> None:
    """native write_tag 整数点位逆缩放取整(与同步层同口径,第八轮 P1-5)。

    回归:非恒等逆缩放 ``(0.3-0)/0.1 = 2.9999…`` 原样透传 →
    ``require_int`` 拒收 float,ValueError 逃出公共 API、语义污染成
    "参数非法"(同步层有 ``int(round())`` 分支,native 曾漏镜像)。
    """
    from omniplc.core.tag import Tag, TagTable

    transport = FakeTransport(
        _chunks(b"\x00\x01\x00\x00\x00\x06\x01\x06\x00\x00\x00\x03")
    )

    async def scenario() -> None:
        client = _client(transport)
        await client.connect()
        try:
            client.bind_tags(TagTable([Tag("设定", "hr0", "short", scale=0.1)]))
            assert await client.write_tag("设定", 0.3) is True
            # FC06 写单寄存器请求:ADU 末 2 字节 = 寄存器值 3(而非 2.9999… 被拒)
            assert transport.sent[0][-2:] == (3).to_bytes(2, "big")
        finally:
            await client.close()

    asyncio.run(scenario())


def test_async_with_exit_gates_the_client_like_close() -> None:
    """``async with`` 退出 = ``close()``(关闸),不是 ``disconnect()``。

    块外继续用同一实例必须抛 ``RuntimeError``(与 ``omniplc.aio`` 同口径):
    退出即断开、再调用不静默惰性重连——块外误用立即暴露,而不是"悄悄又连上"。
    """
    transport = FakeTransport(_chunks(_RESP_TID1))
    client = _client(transport)

    async def scenario() -> None:
        async with client:
            assert client.connected is True
            assert await client.read_ushort("hr0") == (True, 20)
        assert client.connected is False, "退出 async with 必须断开"
        with pytest.raises(RuntimeError):
            await client.read_ushort("hr0")

    asyncio.run(scenario())


def test_sequential_cross_loop_calls_without_connection_stay_usable() -> None:
    """未建立连接时,逐个调用各起一次 ``asyncio.run`` 必须继续可用。

    换循环本身无害(会新建传输);若一律报错,这种写法会被误伤——本文件多例
    即"一次调用一次 ``asyncio.run``",且部分用例从未成功连上。
    """

    async def read_once() -> Any:
        return await client.read_ushort("hr0")

    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    client._create_transport = lambda: FakeTransport([], connect_error=OSError("拒绝"))  # type: ignore[method-assign]

    assert asyncio.run(read_once()) == (False, None)  # 循环 A:连不上
    assert asyncio.run(read_once()) == (False, None)  # 循环 B:仍可调用,不抛
    asyncio.run(client.close())


def test_reuse_of_connected_client_across_loops_raises() -> None:
    """连接已建立后换事件循环发起事务 → 显式 ``RuntimeError``。

    传输对象(``StreamReader`` / 已连接 UDP 套接字)绑在旧循环上,在别的循环里
    读写要么静默失败要么串帧——原实现静默换锁,读写"看着能用"但结果是脏的。
    """
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

    async def connect_in_first_loop() -> None:
        client._create_transport = lambda: FakeTransport(["hang"])  # type: ignore[method-assign]
        assert await client.connect() is True

    asyncio.run(connect_in_first_loop())

    with pytest.raises(RuntimeError):
        asyncio.run(client.read_ushort("hr0"))


def test_connect_on_another_loop_of_connected_client_raises() -> None:
    """已连接后换循环调 ``connect()`` → 显式 ``RuntimeError``。

    回归:connect 原无亲和检查,而 _guard 换锁分支会静默改写 ``_lock_loop``
    ——已连接(旧循环锁空闲)时 connect() 把锁换到新循环并把 ``_connected``
    短路返回,事务入口亲和检查的比对基准被洗掉,后续跨循环收发永远放行
    (静默失败/串帧)。
    """
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

    async def connect_in_first_loop() -> None:
        client._create_transport = lambda: FakeTransport(["hang"])  # type: ignore[method-assign]
        assert await client.connect() is True

    asyncio.run(connect_in_first_loop())

    with pytest.raises(RuntimeError):
        asyncio.run(client.connect())


def test_close_wins_gate_race_and_queued_connect_cannot_revive() -> None:
    """回归:close() 先拿锁的时序下,排队中的 ``connect()`` 进锁后不得复活建连。

    与事务版(_execute 锁内复查,第五轮修)同款窗口:connect 在锁外过完
    ``_ensure_open`` 后挂起等锁,``close()`` 插队先拿锁置 ``_closed`` 并断开;
    connect 随后进锁,若锁内复查缺失会照常 ``_connect_locked`` 真建连——
    已关闸客户端向 PLC 发出新请求且新传输无人回收。修复后 connect 应在
    锁内复查处抛 ``RuntimeError``。
    """

    async def scenario() -> None:
        transport = FakeTransport([_RESP_TID1])
        client = _client(transport)
        gate = asyncio.Event()
        armed = [False]
        original_guard = client._guard

        class _InterposedLock:
            """代理锁:__aenter__ 时先跑完 close(),再放行 connect 拿真锁。"""

            async def __aenter__(self):
                armed[0] = False
                gate.set()
                await client.close()
                return await original_guard().__aenter__()

            async def __aexit__(self, *exc):
                return await original_guard().__aexit__(*exc)

        class _InterposedGuard:
            def __call__(self):
                if armed[0]:
                    armed[0] = False
                    return _InterposedLock()
                return original_guard()

        client._guard = _InterposedGuard()  # type: ignore[method-assign]

        armed[0] = True
        connect_task = asyncio.ensure_future(client.connect())
        for _ in range(8):
            await asyncio.sleep(0)
        assert gate.is_set(), "close 未插队到 connect 拿锁之前"
        with pytest.raises(RuntimeError):
            await connect_task
        assert client.connected is False
        assert client._transport is None, "close 已关闸,connect 不得建连"

    asyncio.run(scenario())


def test_close_from_another_loop_still_closes() -> None:
    """跨循环 ``close()`` 不拦(收尾路径):换锁后照常断开,不把清理机会也堵死。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

    async def connect_in_first_loop() -> None:
        client._create_transport = lambda: FakeTransport([])  # type: ignore[method-assign]
        assert await client.connect() is True

    asyncio.run(connect_in_first_loop())
    asyncio.run(client.close())  # 不抛

    assert client.connected is False
    with pytest.raises(RuntimeError):  # 关闸仍然生效
        asyncio.run(client.read_ushort("hr0"))


def test_concurrent_cross_loop_use_raises_runtime_error() -> None:
    """另一事件循环**正持有**事务锁时,本循环发起调用 → 显式 ``RuntimeError``。

    静默换锁会让两个循环各自"串行"却互不排斥:同一连接上的收发交错,协议帧
    串包(见 :meth:`AsyncBaseClient._guard` / :meth:`_check_loop_affinity`)。
    """
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    release = threading.Event()
    started = threading.Event()

    async def busy_loop() -> None:
        scripted = FakeTransport(["hang"])
        client._create_transport = lambda: scripted  # type: ignore[method-assign]
        client.receive_timeout = 30.0
        task = asyncio.ensure_future(client.read_ushort("hr0"))
        lock = client._lock
        while lock is None or not lock.locked():
            await asyncio.sleep(0.01)  # 等事务拿到锁(在途)
            lock = client._lock
        started.set()
        await asyncio.get_event_loop().run_in_executor(None, release.wait, 5.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await client.close()

    thread = threading.Thread(target=lambda: asyncio.run(busy_loop()), daemon=True)
    thread.start()
    try:
        assert started.wait(5.0), "后台循环未能进入在途事务"
        with pytest.raises(RuntimeError):
            asyncio.run(client.read_ushort("hr0"))
    finally:
        release.set()
        thread.join(5.0)


def test_disconnect_rejects_cross_loop() -> None:
    """跨循环 disconnect 显式拒绝(第八轮 P2-15):亲和检查先于取锁。

    跨循环 disconnect 会在错误循环上触发 ``transport.close()`` 的
    ``call_soon``(非线程安全,debug 模式必炸),不能靠 ``_guard`` 静默
    换锁兜底。close 无亲和门槛(兜底清理通道),收尾用它。
    """
    transport = FakeTransport(_chunks(_RESP_TID1))
    client = _client(transport)

    async def connect_only() -> None:
        await client.connect()

    asyncio.run(connect_only())
    with pytest.raises(RuntimeError, match="另一个事件循环"):
        asyncio.run(client.disconnect())
    asyncio.run(client.close())


# ----------------------------------------------------------------------
# 心跳保活(asyncio 任务)
# ----------------------------------------------------------------------


def test_heartbeat_task_lifecycle() -> None:
    """心跳任务:connect 启动、disconnect 取消;显式断开后不再有探测发出。"""

    async def scenario() -> None:
        # FC08 回显应答(tid 递增),给足心跳 tick 消费
        behaviors: List[Any] = []
        for tid in range(1, 8):
            frame = tid.to_bytes(2, "big") + bytes([0, 0, 0, 6, 1, 8, 0, 0, 0, 0])
            behaviors.extend([frame[:7], frame[7:]])
        client = _client(FakeTransport(behaviors))
        client.heartbeat_interval = 0.05
        assert client.ping_supported is True
        assert await client.connect() is True
        task = client._heartbeat_task
        assert task is not None and not task.done()
        await asyncio.sleep(0.25)
        assert client.stats["heartbeat_ok"] >= 2
        assert client.stats["last_heartbeat_at"] is not None
        await client.disconnect()
        await asyncio.sleep(0.05)
        assert task is not None and task.done()
        ticks = client.stats["heartbeat_ok"] + client.stats["heartbeat_fail"]
        await asyncio.sleep(0.15)  # ≥ 3 个间隔,足够暴露"未取消"
        assert client.stats["heartbeat_ok"] + client.stats["heartbeat_fail"] == ticks

    asyncio.run(scenario())


def test_tcp_unit_id_ff_allowed_native() -> None:
    """TCP Unit ID 0~255(native 补齐 R9-1,与同步层对齐):0xFF 放行,
    256/-1 拒——Unit ID 是路由字段非串口站号。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 0xFF)
    assert client.station == 255
    with pytest.raises(ValueError, match="Unit ID"):
        AsyncModbusTcpClient("127.0.0.1", 502, 256)
    with pytest.raises(ValueError, match="Unit ID"):
        AsyncModbusTcpClient("127.0.0.1", 502, -1)


def test_heartbeat_interval_setter_restarts_task() -> None:
    """运行中写间隔:任务立即重启(review-1002 P2,与同步层同语义);置 0 立即停止。"""

    async def scenario() -> None:
        behaviors: List[Any] = []
        for tid in range(1, 10):
            frame = tid.to_bytes(2, "big") + bytes([0, 0, 0, 6, 1, 8, 0, 0, 0, 0])
            behaviors.extend([frame[:7], frame[7:]])
        client = _client(FakeTransport(behaviors))
        client.heartbeat_interval = 0.05
        assert await client.connect() is True
        old_task = client._heartbeat_task
        assert old_task is not None and not old_task.done()
        client.heartbeat_interval = 5.0
        new_task = client._heartbeat_task
        assert new_task is not old_task  # 立即重启,非等旧 sleep 结束
        await asyncio.sleep(0)
        assert old_task.done() and old_task.cancelled()
        assert not new_task.done()
        client.heartbeat_interval = 0
        assert client._heartbeat_task is None  # 置 0 立即停止
        await asyncio.sleep(0)
        assert new_task.done() and new_task.cancelled()
        await client.disconnect()

    asyncio.run(scenario())


def test_heartbeat_failure_writes_error_without_counting() -> None:
    """不支持探活命令的从站形态(native):tick 失败写 last_error 但零计数。

    回归(review-1002 P1-2):修复前 FC08 异常应答每 tick 计入
    error_count + device_error_count;成功 tick 还会清掉业务 last_error。
    """

    async def scenario() -> None:
        # 首 tick:FC08 异常应答(0x88 01,不支持 FC08 的从站);其后回显自愈
        behaviors: List[Any] = []
        exc_frame = (1).to_bytes(2, "big") + bytes([0, 0, 0, 3, 1, 0x88, 0x01])
        behaviors.extend([exc_frame[:7], exc_frame[7:]])
        for tid in range(2, 9):
            frame = tid.to_bytes(2, "big") + bytes([0, 0, 0, 6, 1, 8, 0, 0, 0, 0])
            behaviors.extend([frame[:7], frame[7:]])
        client = _client(FakeTransport(behaviors))
        client.heartbeat_interval = 0.05
        assert await client.connect() is True
        await asyncio.sleep(0.25)
        assert client.stats["heartbeat_fail"] >= 1
        assert client.last_error_code == 1  # 失败原因仍可见
        assert client.stats["error_count"] == 0
        assert client.stats["device_error_count"] == 0
        assert client.stats["heartbeat_ok"] >= 1  # 其后 tick 回显自愈
        await client.disconnect()

    asyncio.run(scenario())


def test_heartbeat_interval_setter_validation() -> None:
    """native 心跳间隔 setter:非数字/负数/非有限数拒绝,0 合法(关闭)。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    for bad in (-1, float("nan"), float("inf"), True, "30"):
        with pytest.raises(ValueError):
            client.heartbeat_interval = bad  # type: ignore[assignment]
    client.heartbeat_interval = 0
    assert client.heartbeat_interval == 0.0
    client.heartbeat_interval = 5.5
    assert client.heartbeat_interval == 5.5
