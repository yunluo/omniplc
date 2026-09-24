"""v0.34.0 可靠性测试:失败结构化(ErrorCategory)与连接退避门控。"""
from __future__ import annotations

import socket
import time
from typing import List

import pytest

import omniplc.core.base_client as base_client_mod
from omniplc.core.base_client import BaseClient
from omniplc.core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
    TransportTimeoutError,
)
from omniplc.transport import BaseTransport
from omniplc.types import DataType, PrimitiveValue


class _ScriptedTransport(BaseTransport):
    """脚本化传输:可指定前 N 次 connect 失败。"""

    def __init__(self, fail_connect_times: int = 0) -> None:
        super().__init__()
        self.fail_connect_times = fail_connect_times
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connect_times:
            raise OSError("[WinError 10061] 连接被拒绝")

    def close(self) -> None:
        self.close_calls += 1

    def send(self, data: bytes) -> None:
        pass

    def recv(self, size: int) -> bytes:
        return b"\x00" * size


class _ScriptedClient(BaseClient):
    """脚本化驱动:_read/_write 行为由测试注入。"""

    def __init__(self, fail_connect_times: int = 0) -> None:
        super().__init__("127.0.0.1", 502)
        self._fail_connect_times = fail_connect_times
        self._fail_next_op: List[BaseException] = []
        self._read_calls = 0
        self._write_calls = 0
        self.transports: List[_ScriptedTransport] = []

    def script_failure(self, exc: BaseException) -> None:
        """注入一次读写失败。"""
        self._fail_next_op.append(exc)

    def _create_transport(self) -> BaseTransport:
        transport = _ScriptedTransport(self._fail_connect_times)
        self._fail_connect_times = 0  # 只有第一个传输失败
        self.transports.append(transport)
        return transport

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        self._read_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        return 3.14

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        self._write_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)


class TestErrorCategory:
    """失败结构化:category + code 与 last_error 同步。"""

    def test_device_error_category_and_code(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(DeviceError("结束代码 0xC059", 0xC059))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.DEVICE
        assert client.last_error_code == 0xC059
        assert "结束代码 0xC059" in (client.last_error or "")
        assert client.connected is True  # 设备错误不断线

    def test_oserror_category_transport(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.connected is False

    def test_oserror_errno_extracted(self) -> None:
        """OSError 的 errno 直通 last_error_code(上位系统告警分类用)。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError(10061, "连接被拒绝"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_code == 10061

    def test_socket_timeout_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(socket.timeout())
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT
        assert "通信超时" in (client.last_error or "")

    def test_transport_timeout_error_wins_over_device(self) -> None:
        """顺序敏感:TransportTimeoutError(DeviceError 子类)必须归 TIMEOUT,code 为 None。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(TransportTimeoutError("接收超时", 0))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT
        assert client.last_error_code is None  # 传输超时码 0 不当作协议码

    def test_protocol_frame_error_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(ProtocolFrameError("校验错"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.PROTOCOL
        assert client.last_error_code is None

    def test_transport_closed_error_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(TransportClosedError("连接未建立"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.last_error_code is None

    def test_unknown_category_fallback(self) -> None:
        """非网络非协议的内部异常落 UNKNOWN 兜底。"""

        class _Weird(OmniPLCInternalError):
            pass

        client = _ScriptedClient()
        client.connect()
        client.script_failure(_Weird("怪异常"))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.UNKNOWN

    def test_success_clears_all_three(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        client.read("hr0", "short")
        client.read("hr0", "short")  # 重连成功
        assert client.last_error is None
        assert client.last_error_category is None
        assert client.last_error_code is None

    def test_connect_failure_category(self) -> None:
        client = _ScriptedClient(fail_connect_times=1)
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert "连接 127.0.0.1:502 失败" in (client.last_error or "")

    def test_last_error_text_unchanged(self) -> None:
        """向后兼容:last_error 文本格式与 v0.33 逐字一致。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(OSError("网络中断"))
        client.read("hr0", "short")
        assert client.last_error == "OSError:网络中断"


class TestReconnectBackoff:
    """连接退避门控:full jitter 时间戳门控,零 sleep。"""

    def test_gate_blocks_immediate_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            base_client_mod.random, "uniform", lambda a, b: 0.25
        )
        client = _ScriptedClient(fail_connect_times=1)
        ok, _ = client.read("hr0", "short")
        assert ok is False  # 真实建连失败
        ok, _ = client.read("hr0", "short")
        assert ok is False  # 立即重试被门控拦下
        # 根因错误保留(门控消息是瞬态提示,不覆盖诊断信息)
        assert "连接被拒绝" in (client.last_error or "")
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert len(client.transports) == 1  # 门控拒绝不再建传输

    def test_gate_preserves_root_cause_with_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """I1 回归:retries>0 时门控窗口内重试直接结束,根因不被覆盖。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.retries = 1
        ok, _ = client.read("hr0", "short")
        assert ok is False
        # 迭代 1 真实失败写入根因;迭代 2 门控预检 break —— 根因保留
        assert "连接被拒绝" in (client.last_error or "")
        assert client.last_error_category is ErrorCategory.TRANSPORT
        assert client.last_error_code is None
        assert len(client.transports) == 1

    def test_gate_does_not_advance_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """门控拒绝不改 _next_connect_at / _connect_fail_count(§2.4)。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")  # 真实失败:推进一次
        assert client._connect_fail_count == 1
        before = client._next_connect_at
        client.read("hr0", "short")  # 门控拒绝
        assert client._connect_fail_count == 1
        assert client._next_connect_at == before

    def test_enter_raises_during_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """退避窗口内 __enter__ 抛 ConnectionError(消息含"退避")。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        assert client.connect() is False  # 真实失败,武装门控
        with pytest.raises(ConnectionError) as ei:
            with client:
                pass
        assert "退避" in str(ei.value)

    def test_gate_does_not_count_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """门控拒绝无网络动作,不计 error_count。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        client.read("hr0", "short")  # 门控拒绝
        assert client.stats["error_count"] == 1  # 只有真实建连失败计入

    def test_gate_expires_allowing_reconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.05)
        client = _ScriptedClient(fail_connect_times=1)
        ok, _ = client.read("hr0", "short")
        assert ok is False
        time.sleep(0.08)  # 门控窗口 0.05s 过期(裕量 30ms)
        ok, value = client.read("hr0", "short")
        assert ok is True
        assert value == 3.14

    def test_zero_jitter_allows_immediate_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """uniform 取下界 0:门控时间戳=now,立即重试放行。"""
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.0)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        ok, _ = client.read("hr0", "short")
        assert ok is True

    def test_backoff_resets_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.05)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        time.sleep(0.08)
        client.read("hr0", "short")  # 重连成功
        assert client._connect_fail_count == 0
        assert client._next_connect_at == 0.0
        assert client.next_connect_in is None

    def test_backoff_resets_on_disconnect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient(fail_connect_times=1)
        client.read("hr0", "short")
        assert client.next_connect_in is not None
        client.disconnect()
        assert client.next_connect_in is None
        assert client._connect_fail_count == 0

    def test_next_connect_in_semantics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(base_client_mod.random, "uniform", lambda a, b: 0.25)
        client = _ScriptedClient()
        assert client.next_connect_in is None  # 从未失败
        client._fail_connect_times = 1
        client.read("hr0", "short")
        remaining = client.next_connect_in
        assert remaining is not None and 0 < remaining <= 0.25

    def test_backoff_capped_at_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omniplc.core.constants import RECONNECT_BACKOFF_MAX

        captured: dict = {}

        def _fake_uniform(a: float, b: float) -> float:
            captured["cap"] = b
            return b

        monkeypatch.setattr(base_client_mod.random, "uniform", _fake_uniform)
        client = _ScriptedClient()
        caps: List[float] = []
        for _ in range(12):
            client._fail_connect_times = 1  # 每轮一个新失败传输
            assert client.connect() is False
            caps.append(captured["cap"])
            client._next_connect_at = 0.0  # 清门控,制造下一次真实失败
        # 真实失败序列下延迟上限单调不降,0.5×2ⁿ 在 n=6 起封顶 30s
        assert caps == sorted(caps)
        assert caps[-1] == RECONNECT_BACKOFF_MAX
        assert client._connect_fail_count == 12

    def test_backoff_disabled_matches_legacy(self) -> None:
        """关闭退避:失败后立即可重连(等同 v0.33 行为)。"""
        client = _ScriptedClient(fail_connect_times=1)
        client.reconnect_backoff = False
        ok, _ = client.read("hr0", "short")
        assert ok is False
        ok, value = client.read("hr0", "short")
        assert ok is True and value == 3.14

    def test_backoff_setter_validation(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.reconnect_backoff = "yes"  # type: ignore[assignment]
        client.reconnect_backoff = False
        assert client.reconnect_backoff is False
        client.reconnect_backoff = True
        assert client.reconnect_backoff is True


class TestAioBackoffAndErrorSurfaces:
    """aio 镜像:退避与错误结构化表面转发(模式同 v030 TestAioStatsForwarding)。"""

    def test_surfaces_forwarded(self) -> None:
        import omniplc.aio as aio

        sync = _ScriptedClient(fail_connect_times=1)
        async_client = aio.AModbusTcpClient.__new__(aio.AModbusTcpClient)
        aio.ABaseClient.__init__(async_client, sync)
        # 属性转发
        assert async_client.reconnect_backoff is True
        async_client.reconnect_backoff = False
        assert sync.reconnect_backoff is False
        sync.reconnect_backoff = True
        # 触发一次真实失败后错误三件套 + 门控剩余时间转发
        sync.read("hr0", "short")
        assert async_client.last_error == sync.last_error
        assert async_client.last_error_category is ErrorCategory.TRANSPORT
        assert async_client.last_error_code is None
        assert async_client.next_connect_in == sync.next_connect_in
        assert async_client.next_connect_in is not None
