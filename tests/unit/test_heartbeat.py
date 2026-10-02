"""BaseClient 心跳保活测试:ping 探活契约 + 守护线程自动心跳生命周期。

覆盖:未实现探活的兜底、探测失败的三类口径(设备错误不断线/传输错误
拆连/超时保留连接)、心跳线程随 connect 启动与 disconnect 停止、
间隔 setter 校验、失败计数、传输失败拆连后的自动重连自愈。
"""
from __future__ import annotations

import threading
import time
from typing import Any, List

import pytest

from omniplc.core.base_client import BaseClient
from omniplc.core.constants import HEARTBEAT_INTERVAL_DEFAULT
from omniplc.core.errors import DeviceError, ErrorCategory, TransportTimeoutError
from omniplc.core.types import DataType, PrimitiveValue
from omniplc.transport import BaseTransport


class _HeartbeatTransport(BaseTransport):
    """脚本化传输:可指定前 N 次 connect 失败(同 test_base_client 夹具)。"""

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


class _ProbeClient(BaseClient):
    """可控探活驱动:是否支持 ping 与探测结果均由测试注入。"""

    def __init__(self, fail_connect_times: int = 0, with_ping: bool = True) -> None:
        super().__init__("127.0.0.1", 502)
        self._fail_connect_times = fail_connect_times
        self.probe_calls = 0
        self._probe_script: List[BaseException] = []
        self.transports: List[_HeartbeatTransport] = []
        if with_ping:
            self._has_ping = True

    def script_probe_failure(self, exc: BaseException) -> None:
        """注入一次探测失败(队列首;耗尽后探测恒成功)。"""
        self._probe_script.append(exc)

    def _create_transport(self) -> BaseTransport:
        transport = _HeartbeatTransport(self._fail_connect_times)
        self._fail_connect_times = 0  # 只有第一个传输失败
        self.transports.append(transport)
        return transport

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        return 1

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        return None

    def _ping_probe(self) -> int:
        self.probe_calls += 1
        if self._probe_script:
            raise self._probe_script.pop(0)
        return 0


def _wait_until(predicate: Any, timeout: float = 3.0) -> bool:
    """轮询等待谓词成立(心跳是异步线程,断言必须等窗口)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class TestPingManual:
    """ping() 手动探活契约。"""

    def test_default_interval_and_support_flag(self) -> None:
        client = _ProbeClient()
        assert client.heartbeat_interval == HEARTBEAT_INTERVAL_DEFAULT == 30.0
        assert client.ping_supported is True
        unsupported = _ProbeClient(with_ping=False)
        assert unsupported.ping_supported is False

    def test_unsupported_driver_returns_false_without_error_count(self) -> None:
        client = _ProbeClient(with_ping=False)
        before = client.stats["error_count"]
        assert client.ping() is False
        assert "未实现 ping 探活" in (client.last_error or "")
        # 兜底路径不产生网络动作:不计失败统计、不建连
        assert client.stats["error_count"] == before
        assert client.transports == []

    def test_unsupported_driver_ping_stamps_last_error_at(self) -> None:
        """手动 ping 未实现探活驱动:last_error_at 同步刷新(三件套一致,
        review-1002 P3);失败统计仍不计、不建连。"""
        client = _ProbeClient(with_ping=False)
        before = client.stats["error_count"]
        assert client.ping() is False
        assert client.stats["last_error_at"] is not None
        assert client.stats["error_count"] == before
        assert client.transports == []

    def test_ping_success(self) -> None:
        client = _ProbeClient()
        assert client.ping() is True
        assert client.probe_calls == 1
        assert client.last_error is None
        assert client.connected is True

    def test_ping_device_error_keeps_connection(self) -> None:
        client = _ProbeClient()
        client.script_probe_failure(DeviceError("PLC 明确报错", 5))
        assert client.ping() is False
        # 能应答错误码 = 链路活着:不断线,last_error 带协议码
        assert client.connected is True
        assert client.last_error_code == 5
        assert client.last_error_category is not None

    def test_ping_os_error_marks_disconnected(self) -> None:
        client = _ProbeClient()
        assert client.connect() is True
        client.script_probe_failure(OSError("[WinError 10054] 远程主机强迫关闭"))
        assert client.ping() is False
        # 传输类失败拆连,惰性重连发生在下一次事务
        assert client.connected is False
        assert client.stats["disconnect_count"] == 1

    def test_ping_timeout_keeps_connection(self) -> None:
        client = _ProbeClient()
        client.script_probe_failure(TransportTimeoutError("串口读取超时", 0))
        assert client.ping() is False
        # 0 字节超时口径:链路无残渣,不拆连(与读写事务一致)
        assert client.connected is True


class TestHeartbeatInterval:
    """heartbeat_interval 属性与校验。"""

    def test_setter_rejects_invalid(self) -> None:
        client = _ProbeClient()
        for bad in (-1, -0.5, float("nan"), float("inf"), True, "30"):
            with pytest.raises(ValueError):
                client.heartbeat_interval = bad  # type: ignore[assignment]

    def test_setter_accepts_zero_and_positive(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0
        assert client.heartbeat_interval == 0.0
        client.heartbeat_interval = 5
        assert client.heartbeat_interval == 5.0
        client.heartbeat_interval = 2.5
        assert client.heartbeat_interval == 2.5


class TestHeartbeatLifecycle:
    """心跳线程生命周期:随 connect 启动、disconnect 停止、间隔写入生效。"""

    def test_heartbeat_runs_after_connect(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 2)
            assert client.probe_calls >= 2
            assert client.stats["last_heartbeat_at"] is not None
        finally:
            client.disconnect()

    def test_unsupported_driver_never_starts_heartbeat(self) -> None:
        client = _ProbeClient(with_ping=False)
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            time.sleep(0.25)
            assert client.stats["heartbeat_ok"] == 0
            assert client.stats["heartbeat_fail"] == 0
        finally:
            client.disconnect()

    def test_zero_interval_never_starts_heartbeat(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0
        try:
            assert client.connect() is True
            time.sleep(0.25)
            assert client.probe_calls == 0
            assert client._heartbeat_thread is None
        finally:
            client.disconnect()

    def test_disconnect_stops_heartbeat(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        assert client.connect() is True
        assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 1)
        assert client.disconnect() is True
        # _stop_heartbeat 同步清线程引用并置位停止事件
        assert client._heartbeat_thread is None
        assert client._heartbeat_stop is None
        calls = client.probe_calls
        time.sleep(0.25)  # ≥ 5 个间隔,足够暴露"未停止"
        assert client.probe_calls == calls

    def test_disconnected_lazy_reconnect_does_not_revive_heartbeat(self) -> None:
        """disconnect 后的心跳停止优先于 lazy reconnect 的 _start_heartbeat。"""
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 1)
            assert client.disconnect() is True
            calls = client.probe_calls
            time.sleep(0.2)
            assert client.probe_calls == calls
        finally:
            client.disconnect()

    def test_running_interval_change_applies(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 1)
            # 写入新间隔:线程重启,继续按新间隔运行(此处验证不抛、线程仍在)
            client.heartbeat_interval = 0.2
            assert client.heartbeat_interval == 0.2
            before = client.stats["heartbeat_ok"]
            assert _wait_until(lambda: client.stats["heartbeat_ok"] > before)
        finally:
            client.disconnect()

    def test_zero_interval_stops_running_heartbeat(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 1)
            client.heartbeat_interval = 0
            assert client._heartbeat_thread is None
            calls = client.probe_calls
            time.sleep(0.2)
            assert client.probe_calls == calls
        finally:
            client.disconnect()


class TestHeartbeatFailures:
    """心跳失败口径与自愈。"""

    def test_tick_during_backoff_window_is_silent(self) -> None:
        """退避门控激活期 tick 不发包:「未尝试」≠「尝试失败」——不计
        heartbeat_fail 也不刷 last_heartbeat_at(review-1002 P3)。"""
        client = _ProbeClient()
        client._connected = False
        client._next_connect_at = time.monotonic() + 3600.0  # 武装退避窗口
        client._heartbeat_tick(threading.Event())
        assert client.probe_calls == 0
        assert client.stats["heartbeat_fail"] == 0
        assert client.stats["heartbeat_ok"] == 0
        assert client.stats["last_heartbeat_at"] is None

    def test_device_error_fails_counted_connection_kept(self) -> None:
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            client.script_probe_failure(DeviceError("PLC 明确报错", 5))
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_fail"] >= 1)
            # 能应答错误码 = 链路活着:心跳失败但连接保持
            assert client.connected is True
        finally:
            client.disconnect()

    def test_transport_failure_self_heals_by_reconnect(self) -> None:
        """传输失败拆连后,下一 tick 经 _execute 惰性重连自愈。"""
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            # 首个 tick 探测抛 OSError → 拆连;其后 tick 的 _execute 发现已
            # 断开 → 惰性重连成功 → 探测成功(自愈闭环)
            client.script_probe_failure(OSError("[WinError 10054] 远程主机强迫关闭"))
            assert client.connect() is True
            assert _wait_until(
                lambda: client.stats["heartbeat_ok"] >= 1
                and client.stats["heartbeat_fail"] >= 1
            )
            assert client.connected is True
            assert len(client.transports) >= 2
        finally:
            client.disconnect()

    def test_probe_failure_does_not_kill_loop(self) -> None:
        """探测持续失败时循环不终止:失败持续计数,不收敛也不崩溃。"""
        client = _ProbeClient()

        def _always_fail() -> None:
            raise ValueError("参数类错误也不终止心跳")

        client._ping_probe = _always_fail  # type: ignore[method-assign]
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_fail"] >= 2)
        finally:
            client.disconnect()


class TestHeartbeatErrorAccounting:
    """心跳 tick 不劫持业务错误记账(review-1002 P1-2)。"""

    def test_tick_failure_writes_error_without_counting(self) -> None:
        """不支持探活命令的从站形态:tick 失败写 last_error 但零计数。"""
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            client.script_probe_failure(DeviceError("PLC 明确报错", 5))
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_fail"] >= 1)
            assert client.last_error_code == 5  # 失败原因仍可见
            # 不计入 error_count / device_error_count(修复前每 tick 双计数)
            assert client.stats["error_count"] == 0
            assert client.stats["device_error_count"] == 0
        finally:
            client.disconnect()

    def test_tick_success_keeps_business_error_and_stamps(self) -> None:
        """业务失败后心跳成功:last_error 保留,成功戳/RTT 不被心跳刷新。"""
        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        try:
            assert client.connect() is True
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 1)
            client._set_error("业务读失败", ErrorCategory.DEVICE, 5)
            success_at = client.stats["last_success_at"]
            rtt = client.stats["last_rtt"]
            assert _wait_until(lambda: client.stats["heartbeat_ok"] >= 2)
            assert client.last_error == "业务读失败"  # 心跳成功不清业务错误
            assert client.stats["last_success_at"] == success_at
            assert client.stats["last_rtt"] == rtt
        finally:
            client.disconnect()

    def test_manual_ping_success_still_clears_error(self) -> None:
        """手动 ping(非心跳标记)成功照旧清 last_error(契约不变)。"""
        client = _ProbeClient()
        client._set_error("业务读失败", ErrorCategory.DEVICE, 5)
        assert client.ping() is True
        assert client.last_error is None


class TestHeartbeatThreadLifecycle:
    """心跳线程引用形态(review-1002 P2)。"""

    def test_abandoned_client_reclaimed_thread_exits(self) -> None:
        """未 disconnect 丢弃的客户端可回收,心跳线程随之退出。

        回归:循环体曾以绑定方法为 target 强引用客户端——丢弃的
        客户端+传输+连接永不回收,且被丢弃客户端无限期惰性重连 PLC;
        弱引用化后 GC 可回收,线程在下个周期发现失效即退出。
        """
        import gc
        import weakref as _weakref

        client = _ProbeClient()
        client.heartbeat_interval = 0.05
        assert client.connect() is True
        thread = client._heartbeat_thread
        assert thread is not None and thread.is_alive()
        ref = _weakref.ref(client)
        del client
        gc.collect()
        assert ref() is None  # 客户端可回收(修复前被强引用链钉死)
        assert _wait_until(lambda: not thread.is_alive())
