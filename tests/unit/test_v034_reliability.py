"""v0.34.0 可靠性测试:失败结构化(ErrorCategory)与连接退避门控。"""
from __future__ import annotations

import socket
from typing import List

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

    def test_socket_timeout_category(self) -> None:
        client = _ScriptedClient()
        client.connect()
        client.script_failure(socket.timeout())
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT
        assert "通信超时" in (client.last_error or "")

    def test_transport_timeout_error_wins_over_device(self) -> None:
        """顺序敏感:TransportTimeoutError(DeviceError 子类)必须归 TIMEOUT。"""
        client = _ScriptedClient()
        client.connect()
        client.script_failure(TransportTimeoutError("接收超时", 0))
        ok, _ = client.read("hr0", "short")
        assert ok is False
        assert client.last_error_category is ErrorCategory.TIMEOUT

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
