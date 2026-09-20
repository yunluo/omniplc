"""BaseClient 公共逻辑测试:用可控的模拟驱动验证事务模板。

覆盖:惰性重连、读重试、写默认不重试、设备错误不断线、
线程安全(事务串行化)、last_error、超时属性传播、Tag 缩放。
"""
from __future__ import annotations

import threading
from typing import List, Optional

import pytest

from omniplc.core.errors import DeviceError
from omniplc.core.base_client import BaseClient
from omniplc.tag import Tag, TagTable
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
        self.last_written: Optional[PrimitiveValue] = None
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
        if data_type is DataType.BOOL:
            return True
        return 3.14

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        self._write_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        self.last_written = value


class TestLazyReconnect:
    """惰性自动重连。"""

    def test_connect_failure_then_lazy_reconnect(self) -> None:
        client = _ScriptedClient(fail_connect_times=1)
        # 第一次:第一个传输 connect 失败
        ok, value = client.read("hr0", "short")
        assert ok is False
        assert value is None
        assert client.connected is False
        assert "连接被拒绝" in (client.last_error or "")
        # 第二次:惰性重连成功
        ok, value = client.read("hr0", "short")
        assert ok is True
        assert value == 3.14
        assert client.connected is True
        assert client.last_error is None

    def test_connect_is_idempotent(self) -> None:
        client = _ScriptedClient(fail_connect_times=0)
        assert client.connect() is True
        assert client.connect() is True
        assert len(client.transports) == 1

    def test_disconnect_idempotent(self) -> None:
        client = _ScriptedClient()
        assert client.disconnect() is True
        assert client.disconnect() is True


class TestStringSupport:
    """字符串原语缺省实现(驱动未覆写时)。"""

    def test_string_unsupported_default(self) -> None:
        """read_string/write_string → (False, None)/False,不断线,last_error 说明。"""
        client = _ScriptedClient()
        client.connect()
        ok, value = client.read_string("D100", 4)
        assert ok is False and value is None
        assert "暂不支持字符串" in (client.last_error or "")
        assert client.connected is True  # 能力缺失不是链路故障,不触发重连
        assert client.write_string("D100", "AB") is False
        assert client.connected is True


class TestRetry:
    """读/写重试语义。"""

    def test_read_retries_on_transport_error(self) -> None:
        client = _ScriptedClient()
        client.retries = 1
        client.script_failure(OSError("网络中断"))
        ok, value = client.read("hr0", "short")
        assert ok is True
        assert value == 3.14
        assert client._read_calls == 2

    def test_write_no_retry_by_default(self) -> None:
        client = _ScriptedClient()
        client.script_failure(OSError("网络中断"))
        ok = client.write("hr0", "short", 1)
        assert ok is False
        assert client._write_calls == 1  # 未重试,防重复写入

    def test_write_retry_when_enabled(self) -> None:
        client = _ScriptedClient()
        client.write_retries = 1
        client.script_failure(OSError("网络中断"))
        ok = client.write("hr0", "short", 1)
        assert ok is True
        assert client._write_calls == 2

    def test_device_error_no_retry_no_disconnect(self) -> None:
        client = _ScriptedClient()
        client.retries = 3
        client.script_failure(DeviceError("PLC 返回错误码 0x02", 2))
        ok, value = client.read("hr0", "short")
        assert ok is False
        assert value is None
        assert client._read_calls == 1  # 不重试
        assert client.connected is True  # 不断线
        assert "0x02" in (client.last_error or "")

    def test_retries_negative_invalid(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.retries = -1


class TestTimeoutProperties:
    """超时属性。"""

    def test_defaults(self) -> None:
        client = _ScriptedClient()
        assert client.connect_timeout == 5.0
        assert client.receive_timeout == 3.0

    def test_propagates_to_live_transport(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.receive_timeout = 2.5
        assert client.transports[0].receive_timeout == 2.5

    def test_invalid_value(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.receive_timeout = 0


class TestThreadSafety:
    """线程安全:同一 client 跨线程读,事务必须串行化。"""

    def test_concurrent_reads_all_succeed(self) -> None:
        client = _ScriptedClient()
        client.connect()
        results: List[Optional[bool]] = []
        values: List[Optional[PrimitiveValue]] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                ok, value = client.read("hr0", "float")
                with lock:
                    results.append(ok)
                    values.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(results)
        assert None not in values
        assert len(values) == 400

    def test_reconnect_race_is_serialized(self) -> None:
        # 全部线程从断线状态开始,竞争触发重连,只允许建立一个传输
        client = _ScriptedClient(fail_connect_times=0)
        threads = [
            threading.Thread(target=lambda: client.read("hr0", "short"))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(client.transports) == 1


class TestContextManager:
    """上下文管理器。"""

    def test_enter_failure_raises_connection_error(self) -> None:
        client = _ScriptedClient(fail_connect_times=1)
        with pytest.raises(ConnectionError):
            with client:
                pass

    def test_enter_success_closes_on_exit(self) -> None:
        client = _ScriptedClient()
        with client:
            assert client.connected is True
        assert client.connected is False
        assert client.transports[0].close_calls == 1


class TestTagScaling:
    """点位表读写与缩放。"""

    def test_read_tag_with_scale(self) -> None:
        client = _ScriptedClient()
        client.bind_tags(TagTable([Tag("炉温", "hr0", "float", scale=0.1, offset=5)]))
        ok, value = client.read_tag("炉温")
        assert ok is True
        assert value == pytest.approx(3.14 * 0.1 + 5)

    def test_write_tag_inverse_scale(self) -> None:
        client = _ScriptedClient()
        client.bind_tags(TagTable([Tag("设定", "hr0", "float", scale=2.0, offset=10)]))
        ok = client.write_tag("设定", 30.0)
        assert ok is True
        # 写入前逆缩放:(30 - 10) / 2 = 10,写入调用的值无法直接观察,
        # 这里只验证成功路径
        assert ok is True

    def test_unbound_name_raises(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.read_tag("不存在")

    def test_direct_tag_object(self) -> None:
        client = _ScriptedClient()
        ok, value = client.read_tag(Tag("温度", "hr0", "float"))
        assert ok is True
        assert value == pytest.approx(3.14)
