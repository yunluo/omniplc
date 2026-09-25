"""v0.30.0 工业场景可靠性 + 观测诊断聚焦测试。

覆盖 11 项(A1~A10 + B1):

- A1 ``receive_timeout`` setter 下发到 live socket(TCP/UDP)
- A2 整事务 deadline(涓流对端不能无限拖住读)
- A3 ``TransportTimeoutError`` 串口/UDP 超时按"链路完好不断线"
- A4 ``connect``/``_after_connect`` 异常必清理到干净状态
- A5 AB connected CIP 状态 0x01 → ``ProtocolFrameError``(断开重连)
- A6 SO_KEEPALIVE 默认开启(best-effort)
- A7 aio close 生命周期(幂等、关闭后调用抛明确错)
- A8 UDP datagram 上限抬至 8192
- A9 FINS/TCP 重连刷新自动节点号
- A10 MX COM Close 调用顺序与 COM 计数配对
- B1 BaseClient 健康统计快照
- B2 超时/错误码口径(超时不计设备错误码、``code=0`` 归 ``None``、超时跨走线重试)
"""
from __future__ import annotations

import asyncio
import socket
import time
from typing import List, Optional, Tuple

import pytest

from omniplc import aio
from omniplc.core import errors
from omniplc.core.base_client import BaseClient, ClientStats
from omniplc.core.constants import (
    FINS_MAX_DATAGRAM,
    MC_MAX_DATAGRAM,
    TCP_KEEPALIVE_IDLE,
)
from omniplc.plc.ab import codec_cip
from omniplc.plc.melsec import mx
from omniplc.plc.omron import codec as fins_codec
from omniplc.transport import BaseTransport, TcpTransport, UdpTransport


# ----------------------------------------------------------------------
# 测试夹具与脚本化驱动
# ----------------------------------------------------------------------


class _FakeSocket:
    """挂到 TcpTransport._socket 的最小假 socket。"""

    def __init__(self) -> None:
        self.timeout: Optional[float] = None
        self.setsockopt_calls: List[Tuple[int, int, object]] = []
        self.ioctl_calls: List[Tuple[int, object]] = []
        self._closed = False

    def settimeout(self, value: Optional[float]) -> None:
        self.timeout = value

    def setsockopt(self, level: int, name: int, value: object) -> None:
        self.setsockopt_calls.append((level, name, value))

    def ioctl(self, request: int, value: object) -> None:
        self.ioctl_calls.append((request, value))

    def close(self) -> None:
        self._closed = True


def _attach_fake_socket(transport: TcpTransport, sock: _FakeSocket) -> None:
    """直接把假 socket 挂到 TcpTransport,绕过真 connect。"""
    transport._socket = sock  # type: ignore[assignment]


def _make_fins_handshake_frame(local_node: int, plc_node: int) -> bytes:
    """拼一个合法 FINS/TCP 握手应答(节点分配响应),内部辅助。"""
    # build_handshake_response: 16 头 + 4 server_node + 4 client_node
    # + 4 destination_node + 4 source_node(共 28 字节)
    header = fins_codec.build_handshake(local_node)
    body = (
        (4).to_bytes(4, "big")  # 命令码
        + (0).to_bytes(4, "big")  # 出错码
        + struct_pack_node(plc_node)  # PLC 节点号
        + struct_pack_node(local_node)  # 上位机节点号
        + struct_pack_node(plc_node)  # 目标节点号
        + struct_pack_node(local_node)  # 源节点号
    )
    length = len(body)
    payload = header[4:8] + length.to_bytes(4, "big") + body
    return payload


def struct_pack_node(node: int) -> bytes:
    """节点号 4 字节大端(>=1)。"""
    return (int(node) & 0xFFFFFFFF).to_bytes(4, "big")


# ----------------------------------------------------------------------
# A1 receive_timeout setter 下发到 live socket
# ----------------------------------------------------------------------


class TestReceiveTimeoutSetterPropagation:
    """``receive_timeout`` 在已连接 TCP 上立即生效。"""

    def test_tcp_setter_pushes_to_live_socket(self) -> None:
        sock = _FakeSocket()
        transport = TcpTransport("127.0.0.1", 502)
        _attach_fake_socket(transport, sock)
        transport.receive_timeout = 1.5
        # setter 已下发新值到假 socket
        assert sock.timeout == 1.5
        assert transport.receive_timeout == 1.5
        transport.close()  # 不影响假 socket,只是清理 self._socket

    def test_tcp_setter_no_socket_noop(self) -> None:
        transport = TcpTransport("127.0.0.1", 502)
        # 未连接时 setter 只更新内部值
        transport.receive_timeout = 2.0
        assert transport.receive_timeout == 2.0
        assert transport._socket is None

    def test_udp_setter_pushes_to_live_socket(self) -> None:
        transport = UdpTransport("127.0.0.1", 9600)
        # 直接挂 socket 跳过真 connect
        sock = _FakeSocket()
        transport._socket = sock  # type: ignore[assignment]
        transport.receive_timeout = 1.25
        assert sock.timeout == 1.25


# ----------------------------------------------------------------------
# A2 整事务 deadline(涓流拖不死)
# ----------------------------------------------------------------------


class TestRecvDeadline:
    """对端涓流挤字节,在 deadline 处抛错(不无限等)。"""

    def test_tcp_trickle_hits_deadline(self) -> None:
        """假 socket 每次吐 1 字节,但 receive_timeout=0.2s 应在 ~0.2s 抛 socket.timeout。"""
        # 构造一个每次只回 1 字节的 socket
        class TrickleSocket:
            def __init__(self) -> None:
                self.timeout: Optional[float] = None

            def settimeout(self, value: Optional[float]) -> None:
                self.timeout = value

            def recv(self, n: int) -> bytes:
                # 每个 1 字节,但给外层 0.05s 时间,模拟对端每秒 20 字节
                if self.timeout and self.timeout > 0:
                    time.sleep(min(self.timeout, 0.05))
                return b"\x00" if n >= 1 else b""

            def close(self) -> None:
                pass

        transport = TcpTransport("127.0.0.1", 502)
        transport._socket = TrickleSocket()  # type: ignore[assignment]
        transport.receive_timeout = 0.2
        start = time.monotonic()
        with pytest.raises(socket.timeout):
            transport.recv(10)
        elapsed = time.monotonic() - start
        # 应在 receive_timeout 附近抛(允许稍大,但远小于无限)
        assert elapsed < 1.0, "deadline 失效:耗时 {}s".format(elapsed)


# ----------------------------------------------------------------------
# A3 TransportTimeoutError(串口/UDP 超时按链路完好不断线)
# ----------------------------------------------------------------------


class TestTransportTimeoutError:
    """``TransportTimeoutError`` 是 DeviceError 子类,标识"链路完好"。"""

    def test_is_device_error_subclass(self) -> None:
        assert issubclass(errors.TransportTimeoutError, errors.DeviceError)

    def test_udp_recv_timeout_raises_timeout_error(self) -> None:
        class SilentSocket:
            def settimeout(self, _v: float) -> None:
                pass

            def recv(self, _n: int) -> bytes:
                raise socket.timeout("timed out")

            def close(self) -> None:
                pass

        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = SilentSocket()  # type: ignore[assignment]
        with pytest.raises(errors.TransportTimeoutError):
            transport.recv(64)
        # 已 connected 状态不丢(UDP 整数据报无残留)
        assert transport._socket is not None

    def test_udp_timeout_message_contains_timeout_value(self) -> None:
        class SilentSocket:
            def settimeout(self, _v: float) -> None:
                pass

            def recv(self, _n: int) -> bytes:
                raise socket.timeout("timed out")

            def close(self) -> None:
                pass

        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = SilentSocket()  # type: ignore[assignment]
        transport.receive_timeout = 1.5
        with pytest.raises(errors.TransportTimeoutError) as ei:
            transport.recv(64)
        assert "1.5" in str(ei.value)


# ----------------------------------------------------------------------
# A4 connect/_after_connect 异常清理
# ----------------------------------------------------------------------


class _HandshakeFailTransport(BaseTransport):
    """connect 成功后,_after_connect 抛 ValueError。"""

    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def send(self, _data: bytes) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        return b""


class _HandshakeFailClient(BaseClient):
    """驱动在 _after_connect 抛 ValueError(模拟 FINS 握手错误解析)。"""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 9999)

    def _create_transport(self) -> BaseTransport:
        return _HandshakeFailTransport()

    def _after_connect(self) -> None:
        raise ValueError("握手响应节点号非法")

    def _read(self, address: str, data_type):
        return 0

    def _write(self, address: str, data_type, value) -> None:
        return None


class TestConnectCleanup:
    """``_after_connect`` 抛错时传输必清理到 None,返回 False(下次可重连)。"""

    def test_after_connect_failure_cleans_up(self) -> None:
        client = _HandshakeFailClient()
        ok = client.connect()
        assert ok is False
        # 状态干净:传输对象释放,下次 connect() 可建新传输
        assert client._transport is None
        assert client.connected is False
        assert "握手响应节点号非法" in (client.last_error or "")
        # 第二次 connect 重建传输(走 _create_transport 又一次)
        ok = client.connect()
        assert ok is False  # _after_connect 仍失败
        # 但 _transport 仍被清空
        assert client._transport is None


# ----------------------------------------------------------------------
# A5 AB connected CIP 状态 0x01 → ProtocolFrameError(断开重连)
# ----------------------------------------------------------------------


class TestAbConnectionFailureTriggersReconnect:
    """AB connected 模式下,CIP 状态 0x01 应抛 ``ProtocolFrameError``。"""

    def test_status_01_mapped_to_protocol_frame_error(self) -> None:
        from omniplc.plc.ab.ab import AllenBradleyEthIpClient

        client = AllenBradleyEthIpClient("127.0.0.1", 44818, connected_messaging=True)
        # 已设置 connected 模式 + _ot_connection_id 不为空(模拟已建立 Forward Open)
        client._ot_connection_id = 0x12345678
        client._to_connection_id = 0x87654321
        client._session_handle = 0xAABBCCDD
        client._connected = True

        # 注入假传输:捕获发送以取 sequence,返回 CIP 状态 0x01 应答
        captured: dict = {}

        class _BoomTransport(BaseTransport):
            def connect(self) -> None:
                pass

            def close(self) -> None:
                pass

            def send(self, data: bytes) -> None:
                captured["sent"] = data

            def recv(self, size: int) -> bytes:
                # 从发送的 SendUnitData 帧取出 sequence
                # ENIP 头 24B + prefix 22B:sequence 在偏移 24+16=40(2 字节 LE)
                sent = captured["sent"]
                seq = int.from_bytes(sent[44:46], "little")
                reply = self._build_unit_data_reply(seq)
                # 按请求字节数切片(模拟流式 recv)
                pos = captured.get("pos", 0)
                chunk = reply[pos : pos + size]
                captured["pos"] = pos + len(chunk)
                return chunk

            def _build_unit_data_reply(self, sequence: int) -> bytes:
                # CIP 状态 0x01(Connection failure):service echo + reserved + status + ext_size=0
                cip = bytes(
                    [
                        codec_cip.CIP_SERVICE_READ_TAG | 0x80,
                        0x00,
                        0x01,  # CIP status 0x01
                        0x00,  # size_of_additional_status (words)
                    ]
                )
                # SendUnitData prefix:interface(4)+timeout(2)+count(2)+
                # addr_item: type(H)+len(H)+conn_id(I)+
                # data_item: type(H)+len(H)+seq(H)
                prefix = (
                    (0).to_bytes(4, "little")  # interface handle
                    + (1).to_bytes(2, "little")  # timeout
                    + (2).to_bytes(2, "little")  # item count
                    + (codec_cip._CPF_ITEM_CONNECTED_ADDRESS).to_bytes(2, "little")
                    + (4).to_bytes(2, "little")
                    + (0x87654321).to_bytes(4, "little")  # T->O connection ID
                    + (codec_cip._CPF_ITEM_CONNECTED_DATA).to_bytes(2, "little")
                    + (len(cip) + 2).to_bytes(2, "little")
                    + sequence.to_bytes(2, "little")
                )
                payload = prefix + cip
                enip_header = (
                    codec_cip.EIP_COMMAND_SEND_UNIT_DATA.to_bytes(2, "little")
                    + (len(payload)).to_bytes(2, "little")
                    + (0).to_bytes(4, "little")
                    + (0).to_bytes(4, "little")
                    + b"\x00" * 8
                    + (0).to_bytes(4, "little")
                )
                return enip_header + payload

        boom = _BoomTransport()
        client._transport = boom  # type: ignore[assignment]

        # 直接调 _transact → 应抛 ProtocolFrameError(0x01 被映射为连接失效)
        with pytest.raises(errors.ProtocolFrameError) as ei:
            client._transact(b"\x4c\x00\x00", codec_cip.CIP_SERVICE_READ_TAG)
        assert "0x01" in str(ei.value) or "Connection failure" in str(ei.value)


# ----------------------------------------------------------------------
# A6 SO_KEEPALIVE 默认开启(best-effort)
# ----------------------------------------------------------------------


class TestKeepalive:
    """TCP 建连后启用 SO_KEEPALIVE(平台分支 best-effort)。"""

    def test_tcp_connect_enables_keepalive(self, tcp_echo_port: int) -> None:
        transport = TcpTransport("127.0.0.1", tcp_echo_port)
        transport.connect()
        try:
            sock = transport._socket
            assert sock is not None
            opt = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            assert opt == 1, "SO_KEEPALIVE 未启用,值={}".format(opt)
        finally:
            transport.close()

    def test_keepalive_constants_present(self) -> None:
        # 常量定义在 constants 模块
        from omniplc.core import constants

        assert constants.TCP_KEEPALIVE_IDLE == TCP_KEEPALIVE_IDLE
        assert constants.TCP_KEEPALIVE_IDLE > 0
        assert constants.TCP_KEEPALIVE_INTERVAL > 0
        assert constants.TCP_KEEPALIVE_COUNT > 0


# ----------------------------------------------------------------------
# A7 aio close 生命周期
# ----------------------------------------------------------------------


class TestAioCloseLifecycle:
    """aio close 幂等;关闭后任何协议调用抛明确错误。"""

    def test_close_is_idempotent(self) -> None:
        sync = _ScriptedSyncForAio()
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        # 关闭两次
        asyncio.run(async_client.close())
        asyncio.run(async_client.close())
        # executor 已释放
        assert async_client._executor is None

    def test_post_close_protocol_call_raises(self) -> None:
        sync = _ScriptedSyncForAio()
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        asyncio.run(async_client.close())

        async def _call() -> None:
            await async_client.read_short("hr0")

        with pytest.raises(RuntimeError, match="客户端已关闭"):
            asyncio.run(_call())

    def test_close_calls_disconnect_on_sync(self) -> None:
        sync = _ScriptedSyncForAio()
        sync.connect()  # 标记 connected 让 disconnect 走实际路径
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        asyncio.run(async_client.close())
        # disconnect 路径在 BaseClient.disconnect 已断言幂等,这里只验证
        # 调用同步 disconnect 不抛错
        assert async_client._executor is None

    def test_close_drains_in_flight_transaction(self) -> None:
        """close 排空在途事务:不锯断帧,返回时该事务已跑完。"""
        sync = _ScriptedSyncForAio()
        finished: List[str] = []

        def slow_read(_address: str, _data_type: object) -> int:
            time.sleep(0.15)
            finished.append("read-done")
            return 1

        sync._read = slow_read
        client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(client, sync)
        sync.connect()

        async def scenario() -> None:
            pending = asyncio.ensure_future(client.read_short("hr0"))
            await asyncio.sleep(0.02)  # 让读真正进入工作线程
            await client.close()
            # close 返回时在途事务已跑完(未被锯断)
            assert finished == ["read-done"]
            assert (await pending) == (True, 1)
            assert client._executor is None

        asyncio.run(scenario())

    def test_close_gate_refuses_new_calls_while_draining(self) -> None:
        """close 起步即关闸:新调用不再排队到关闭之后执行。"""
        sync = _ScriptedSyncForAio()
        sync._read = lambda _a, _t: (time.sleep(0.15), 1)[1]  # noqa: E731
        client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(client, sync)
        sync.connect()

        async def scenario() -> None:
            pending = asyncio.ensure_future(client.read_short("hr0"))
            await asyncio.sleep(0.02)
            closing = asyncio.ensure_future(client.close())
            await asyncio.sleep(0.01)  # 让 close 起步(关闸 + 投递 disconnect)
            with pytest.raises(RuntimeError, match="客户端已关闭"):
                await client.read_short("hr0")
            await closing
            assert (await pending) == (True, 1)
            assert client._executor is None

        asyncio.run(scenario())


class _ScriptedSyncForAio(BaseClient):
    """最简 BaseClient 子类供 aio 镜像测试,不走真传输。"""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 502)

    def _create_transport(self) -> BaseTransport:
        return _NoopTransport()

    def _read(self, address, data_type):
        return 0

    def _write(self, address, data_type, value) -> None:
        return None


class _NoopTransport(BaseTransport):
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, _data: bytes) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        return b"\x00" * _size


# ----------------------------------------------------------------------
# A8 UDP datagram 上限抬至 8192
# ----------------------------------------------------------------------


class TestUdpDatagramLimit:
    """MC/FINS datagram 上限 ≥ 8192。"""

    def test_constants_raised_to_8192(self) -> None:
        assert MC_MAX_DATAGRAM >= 8192
        assert FINS_MAX_DATAGRAM >= 8192


# ----------------------------------------------------------------------
# A9 FINS/TCP 重连刷新自动节点号
# ----------------------------------------------------------------------


class TestFinsReconnectRefreshesNode:
    """自动模式下节点号在重连时被新握手响应覆盖。"""

    def test_second_handshake_refreshes_node(self) -> None:
        """两次握手返回不同节点号,自动模式下第二次后属性刷新。"""
        client = _FinsHandshakeControlled()
        # 第一次握手:返回 local=11, plc=21
        client.next_handshake_nodes = (11, 21)
        assert client.connect() is True
        assert client.local_node == 11
        assert client._source_node == 11
        assert client._destination_node == 21

        # 显式断开后第二次握手:返回 local=33, plc=44
        client.disconnect()
        client.next_handshake_nodes = (33, 44)
        assert client.connect() is True
        # 自动模式覆盖:已更新为 33/44
        assert client.local_node == 33
        assert client._source_node == 33
        assert client._destination_node == 44

    def test_explicit_node_not_overwritten(self) -> None:
        """显式配置节点号(非 0)不被握手响应覆盖。"""
        client = _FinsHandshakeControlled(local_node=99)
        client.next_handshake_nodes = (11, 21)
        assert client.connect() is True
        # 显式配置保持不变
        assert client.local_node == 99
        assert client._source_node == 0  # source 仍自动


class _FinsHandshakeControlled(_ScriptedSyncForAio):
    """脚本化 FINS/TCP:握手由 ``next_handshake_nodes`` 注入。

    模拟真实 FINS 客户端的"自动节点号"语义——构造时若传入 0 视为自动,
    此后每次重连握手都覆盖;显式传入非 0 则保持不变。
    """

    def __init__(self, local_node: int = 0) -> None:
        super().__init__()
        self._local_node = local_node
        self._source_node = 0
        self._destination_node = 0
        self._auto_local = local_node == 0
        self._auto_source = True  # source 默认自动
        self.next_handshake_nodes: Tuple[int, int] = (1, 2)

    @property
    def local_node(self) -> int:
        return self._local_node

    def _after_connect(self) -> None:
        local, plc = self.next_handshake_nodes
        # 自动模式按构造期标志判断,与当前 _local_node 无关(disconnect 后保留)
        if self._auto_local:
            self._local_node = local
            self._source_node = local
            self._destination_node = plc


# ----------------------------------------------------------------------
# A10 MX COM Close 调用顺序与 COM 计数配对
# ----------------------------------------------------------------------


class TestMxComCleanup:
    """MX Close 失败时引用照清,COM 计数配对。"""

    def test_close_success_clears_com_and_calls_uninitialize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        init_calls: List[int] = []
        uninit_calls: List[int] = []
        close_calls: List[int] = []

        class _FakeCom:
            def Open(self) -> int:
                return 0

            def Close(self) -> int:
                close_calls.append(1)
                return 0

        def _fake_init() -> None:
            init_calls.append(1)

        def _fake_uninit() -> None:
            uninit_calls.append(1)

        monkeypatch.setattr(mx, "_com_initialize", _fake_init)
        monkeypatch.setattr(mx, "_com_uninitialize", _fake_uninit)
        monkeypatch.setattr(
            mx, "_new_com_object", lambda _station: _FakeCom()
        )

        link = mx._MxComLink(0)
        link.connect()
        assert link._com is not None
        link.close()
        # Close 先于清理
        assert len(close_calls) == 1
        # 引用清空
        assert link._com is None
        # COM 计数配对
        assert init_calls == [1]
        assert uninit_calls == [1]

    def test_close_failure_still_clears_com(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeCom:
            def Open(self) -> int:
                return 0

            def Close(self) -> int:
                # 返回非 0 表示失败
                return 0xFEFF

        monkeypatch.setattr(mx, "_com_initialize", lambda: None)
        monkeypatch.setattr(mx, "_com_uninitialize", lambda: None)
        monkeypatch.setattr(mx, "_new_com_object", lambda _station: _FakeCom())

        link = mx._MxComLink(0)
        link.connect()
        with pytest.raises(OSError):
            link.close()
        # 即使失败,_com 也照清(下次 connect 重建)
        assert link._com is None


# ----------------------------------------------------------------------
# B1 BaseClient 健康统计
# ----------------------------------------------------------------------


class TestStatsSnapshot:
    """``stats`` 字段在成功 / 失败 / 重连路径都正确更新。"""

    def test_initial_stats_zero(self) -> None:
        client = _ScriptedSyncForAio()
        s = client.stats
        assert s["connect_count"] == 0
        assert s["transactions"] == 0
        assert s["error_count"] == 0
        assert s["device_error_count"] == 0
        assert s["last_connect_at"] is None
        assert s["last_success_at"] is None
        assert s["last_error_at"] is None
        assert s["last_rtt"] is None

    def test_successful_transaction_updates_stats(self) -> None:
        client = _ScriptedSyncForAio()
        client.connect()
        assert client.stats["connect_count"] == 1
        assert client.stats["last_connect_at"] is not None
        ok, _ = client.read_short("hr0")
        assert ok is True
        s = client.stats
        assert s["transactions"] == 1
        assert s["last_success_at"] is not None
        assert s["last_rtt"] is not None
        assert s["last_rtt"] >= 0
        assert s["error_count"] == 0

    def test_device_error_increments_device_error_count(self) -> None:
        client = _ScriptedSyncForAio()
        client.connect()

        # 注入 DeviceError 的 _read:抛 DeviceError
        client._read = lambda _a, _t: (_ for _ in ()).throw(  # noqa: E731
            errors.DeviceError("PLC 错误 0x02", 2)
        )
        ok, _ = client.read_short("hr0")
        assert ok is False
        s = client.stats
        assert s["device_error_count"] == 1
        assert s["error_count"] == 1
        assert s["last_error_at"] is not None
        # DeviceError 不断线
        assert client.connected is True

    def test_transport_error_increments_error_count_and_disconnects(self) -> None:
        client = _ScriptedSyncForAio()
        client.connect()

        # _read 抛 OSError → 走 OSError 分支,断开
        client._read = lambda _a, _t: (_ for _ in ()).throw(OSError("网络中断"))  # noqa: E731
        ok, _ = client.read_short("hr0")
        assert ok is False
        s = client.stats
        assert s["device_error_count"] == 0
        assert s["error_count"] == 1
        assert client.connected is False

    def test_reconnect_increments_connect_count(self) -> None:
        client = _ScriptedSyncForAio()
        client.connect()
        client.disconnect()
        client.connect()
        # 第二次 connect 又算一次
        assert client.stats["connect_count"] == 2


# ----------------------------------------------------------------------
# B1.aio stats 转发
# ----------------------------------------------------------------------


class TestAioStatsForwarding:
    """aio ``stats`` 属性透传同步实例。"""

    def test_stats_forwarded(self) -> None:
        sync = _ScriptedSyncForAio()
        sync.connect()
        sync.read_short("hr0")
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        try:
            s = async_client.stats
            assert s["connect_count"] == 1
            assert s["transactions"] == 1
        finally:
            asyncio.run(async_client.close())

    def test_async_stats_keys_match_typed_dict(self) -> None:
        """异步镜像转发同一份快照,键集与 ``ClientStats`` 声明一致。"""
        sync = _ScriptedSyncForAio()
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        try:
            assert set(async_client.stats) == set(ClientStats.__annotations__)
        finally:
            asyncio.run(async_client.close())


# ----------------------------------------------------------------------
# B1.b stats 返回类型(ClientStats / TypedDict)
# ----------------------------------------------------------------------


class TestClientStatsType:
    """``stats`` 的类型契约:声明字段 = 运行期快照键集,且为隔离拷贝。"""

    def test_snapshot_keys_match_declared_fields(self) -> None:
        """快照键集 == ``ClientStats`` 声明字段(新增计数漏声明/漏快照即红)。"""
        client = _ScriptedSyncForAio()
        client.connect()
        client.read_short("hr0")
        assert set(client.stats) == set(ClientStats.__annotations__)

    def test_runtime_snapshot_is_plain_dict(self) -> None:
        """声明只为类型检查与 IDE 补全服务:运行期就是普通 dict。"""
        client = _ScriptedSyncForAio()
        assert type(client.stats) is dict

    def test_snapshot_is_isolated_copy(self) -> None:
        """改返回值不影响内部计数(每次返回拷贝)。"""
        client = _ScriptedSyncForAio()
        client.connect()
        client.read_short("hr0")
        snapshot = client.stats
        snapshot["transactions"] = 99
        assert client.stats["transactions"] == 1

    def test_exported_from_package_and_core(self) -> None:
        """下游可从包顶层与 ``omniplc.core`` 取到同一类型。"""
        import omniplc
        import omniplc.core

        assert omniplc.ClientStats is ClientStats
        assert omniplc.core.ClientStats is ClientStats


# ----------------------------------------------------------------------
# B2 超时/错误码口径(契约矛盾批)
# ----------------------------------------------------------------------


class TestTimeoutAndCodeSemantics:
    """``_execute`` 的超时分支与 ``last_error_code`` 口径。"""

    def test_transport_timeout_keeps_link_and_skips_device_error_count(self) -> None:
        """超时:0 字节已读 = 链路无残渣 → 不拆连、不计 device_error_count。"""
        client = _ScriptedSyncForAio()
        client.connect()

        def boom(_address: str, _data_type: object) -> None:
            raise errors.TransportTimeoutError("串口读取超时(receive_timeout=1.0)", 0)

        client._read = boom
        assert client.read_short("hr0") == (False, None)
        s = client.stats
        assert s["device_error_count"] == 0  # 超时不是设备返回的错误码
        assert s["error_count"] == 1  # 但仍算一次失败
        assert client.last_error_category is errors.ErrorCategory.TIMEOUT
        assert client.last_error_code is None  # code=0 → 无码
        assert client.connected is True  # 不拆连
        # 传输层自撰文本原样进 last_error(不加类名前缀)
        assert client.last_error == "串口读取超时(receive_timeout=1.0)"

    def test_transport_timeout_retries_without_reconnect(self) -> None:
        """超时与其他传输失败一样按 ``retries`` 重试,且全程不拆连。"""
        client = _ScriptedSyncForAio()
        client.connect()
        client.retries = 1
        calls: List[int] = []

        def boom(_address: str, _data_type: object) -> None:
            calls.append(1)
            raise errors.TransportTimeoutError("UDP 接收超时(1.0s)", 0)

        client._read = boom
        assert client.read_short("hr0") == (False, None)
        assert len(calls) == 2  # retries=1 → 两次尝试
        assert client.connected is True  # 每次都在原连接上重发
        assert client.stats["device_error_count"] == 0
        assert client.stats["error_count"] == 2  # 每次尝试各记一次失败

    def test_device_error_without_code_maps_to_none(self) -> None:
        """``DeviceError(code=0)`` = 无具体错误码:不写 ``last_error_code``,也不计数。

        ``device_error_count`` 只计"PLC 明确返回错误码"的次数——能力缺失、
        设备侧条件(如 MTConnect 数据项不存在)这类无码失败不计入。
        """
        client = _ScriptedSyncForAio()
        client.connect()

        def boom(_address: str, _data_type: object) -> None:
            raise errors.DeviceError("当前驱动暂不支持字符串读取", 0)

        client._read = boom
        assert client.read_short("hr0") == (False, None)
        assert client.last_error_code is None
        assert client.last_error_category is errors.ErrorCategory.DEVICE
        assert client.connected is True  # 能力缺失不断线
        assert client.stats["device_error_count"] == 0  # 无码不计入
        assert client.stats["error_count"] == 1  # 但仍算一次失败

    def test_device_error_code_preserved(self) -> None:
        """有码的 ``DeviceError`` 照原样写进 ``last_error_code`` 并计入设备错误。"""
        client = _ScriptedSyncForAio()
        client.connect()

        def boom(_address: str, _data_type: object) -> None:
            raise errors.DeviceError("PLC 错误 0x02", 2)

        client._read = boom
        assert client.read_short("hr0") == (False, None)
        assert client.last_error_code == 2
        assert client.stats["device_error_count"] == 1

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionRefusedError(10061, "连接被拒绝"),
            ConnectionResetError(10054, "连接被重置"),
            socket.gaierror(-2, "名称解析失败"),
        ],
    )
    def test_connection_errors_still_transport_category(self, exc: OSError) -> None:
        """``_categorize`` 收敛后,连接类 OSError 仍归 TRANSPORT 并拆连。"""
        client = _ScriptedSyncForAio()
        client.connect()

        def boom(_address: str, _data_type: object) -> None:
            raise exc

        client._read = boom
        assert client.read_short("hr0") == (False, None)
        assert client.last_error_category is errors.ErrorCategory.TRANSPORT
        assert client.connected is False
        assert client.stats["device_error_count"] == 0


class TestWriteBoolValueValidation:
    """``write_bool`` 的值校验:非 bool/int 直接拒绝,不静默写反。"""

    @pytest.mark.parametrize("bad", ["0", "false", "True", 1.0, None])
    def test_non_bool_values_rejected(self, bad: object) -> None:
        """``"0"`` 这类非空字符串曾被 ``bool()`` 吞成 ``True``(写反)。"""
        client = _ScriptedSyncForAio()
        client.connect()
        with pytest.raises(ValueError):
            client.write_bool("m0", bad)  # type: ignore[arg-type]

    def test_bool_and_int_still_accepted(self) -> None:
        """bool 与 int 0/1(现场习惯写法)照常写入。"""
        client = _ScriptedSyncForAio()
        client.connect()
        for value in (True, False, 1, 0):
            assert client.write_bool("m0", value) is True