"""传输层单元测试:TCP/UDP 走本机 echo 服务。"""
from __future__ import annotations

import logging
import sys
from typing import List

import pytest

from omniplc.core.debug import LOGGER_NAME
from omniplc.core.errors import TransportClosedError
from omniplc.transport import SerialConfig, TcpTransport, UdpTransport
from omniplc.types import SerialParity

# Windows 上 UDP ``recv`` 对超长报文直接抛 ``WSAEMSGSIZE``(OS 显式报错),
# 不存在"静默截断"路径——截断日志机制只对 POSIX(Linux/macOS)有意义。
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="UDP 静默截断 + MSG_TRUNC 仅 POSIX 平台适用",
)


class TestTcpTransport:
    """TCP 传输。"""

    def test_send_recv_exact(self, tcp_echo_port: int) -> None:
        transport = TcpTransport("127.0.0.1", tcp_echo_port)
        transport.connect()
        try:
            data = b"\x01\x02\x03\x04\x05\x06\x07\x08"
            transport.send(data)
            assert transport.recv(8) == data
        finally:
            transport.close()

    def test_recv_accumulates_fragments(self, tcp_echo_port: int) -> None:
        # TCP 是流式协议:两次 send 的 10 字节,recv(10) 必须精确凑齐
        transport = TcpTransport("127.0.0.1", tcp_echo_port)
        transport.connect()
        try:
            transport.send(b"12345")
            transport.send(b"67890")
            assert transport.recv(10) == b"1234567890"
        finally:
            transport.close()

    def test_recv_without_connect(self) -> None:
        transport = TcpTransport("127.0.0.1", 1)
        with pytest.raises(TransportClosedError):
            transport.recv(1)

    def test_connect_refused(self) -> None:
        transport = TcpTransport("127.0.0.1", 1)
        with pytest.raises(OSError):
            transport.connect()

    def test_timeout_validation(self) -> None:
        transport = TcpTransport("127.0.0.1", 502)
        with pytest.raises(ValueError):
            transport.receive_timeout = 0
        with pytest.raises(ValueError):
            transport.connect_timeout = -1

    def test_context_manager(self, tcp_echo_port: int) -> None:
        with TcpTransport("127.0.0.1", tcp_echo_port) as transport:
            transport.send(b"ping")
            assert transport.recv(4) == b"ping"
        assert transport._socket is None  # 退出后已关闭


class TestUdpTransport:
    """UDP 传输。"""

    def test_datagram_roundtrip(self, udp_echo_port: int) -> None:
        transport = UdpTransport("127.0.0.1", udp_echo_port)
        transport.connect()
        try:
            transport.send(b"\xaa\x55")
            assert transport.recv(4096) == b"\xaa\x55"
        finally:
            transport.close()

    def test_send_without_connect(self) -> None:
        transport = UdpTransport("127.0.0.1", 9600)
        with pytest.raises(TransportClosedError):
            transport.send(b"x")

    @_POSIX_ONLY
    def test_recv_truncation_logs_warning(self, udp_echo_port: int, caplog: pytest.LogCaptureFixture) -> None:
        """UDP 数据报超过缓冲:返回截断后字节,并通过 WARNING 日志提示截断量。

        UDP 是原子报文协议——超出 ``size`` 的字节会被**静默丢弃**,且**不会**
        留到下一次 recv。库必须以 WARNING 级(不受 ``set_debug`` 门控)输出一条
        故障信号,提示调用方协议层 size 估错或对端回了超长报文。
        """
        transport = UdpTransport("127.0.0.1", udp_echo_port)
        transport.connect()
        try:
            # 发 2000 字节,recv 仅 1024——必截断
            big = b"\xAA" * 2000
            transport.send(big)
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                frame = transport.recv(1024)
            assert len(frame) == 1024
            # 截断日志必出现,带"实收 2000B,缓冲 1024B,超出 976B"
            truncate_records = [
                record for record in caplog.records
                if record.levelno == logging.WARNING and "UDP 数据报截断" in record.getMessage()
            ]
            assert len(truncate_records) == 1
            assert "2000" in truncate_records[0].getMessage()
            assert "1024" in truncate_records[0].getMessage()
        finally:
            transport.close()

    @_POSIX_ONLY
    def test_recv_no_truncation_no_warning(self, udp_echo_port: int, caplog: pytest.LogCaptureFixture) -> None:
        """UDP 数据报未超 size:不输出截断 WARNING。"""
        transport = UdpTransport("127.0.0.1", udp_echo_port)
        transport.connect()
        try:
            transport.send(b"\xAA" * 100)
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                frame = transport.recv(1024)
            assert frame == b"\xAA" * 100
            truncate_records = [
                record for record in caplog.records
                if record.levelno == logging.WARNING and "UDP 数据报截断" in record.getMessage()
            ]
            assert len(truncate_records) == 0
        finally:
            transport.close()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows 上 WSAEMSGSIZE(WinError 10040)才直接抛 OSError,POSIX 走 MSG_TRUNC 静默截断分支",
    )
    def test_recv_windows_oversize_raises_device_error(self, udp_echo_port: int) -> None:
        """Windows 上 recv 对超长 UDP 报文抛 WSAEMSGSIZE:库捕获后转 :class:`DeviceError`,
        让基类按 DEVICE 分类记入 last_error_category(链路完好,区分于真断线 TRANSPORT),
        且不触发重连(协议层 size 估错或对端报文超长,与链路健康无关)。
        """
        from omniplc.core.errors import DeviceError

        transport = UdpTransport("127.0.0.1", udp_echo_port)
        transport.connect()
        try:
            transport.send(b"\xAA" * 2000)  # 对端原样回 2000B
            with pytest.raises(DeviceError) as excinfo:
                transport.recv(1024)
            # message 直说"UDP 报文超过缓冲"
            assert "UDP 报文超过缓冲" in str(excinfo.value)
            assert "1024" in str(excinfo.value)
            # code 字段承载原 WSAEMSGSIZE errno(10040)便于应用层判定
            assert excinfo.value.code == 10040
        finally:
            transport.close()


class _FakeSerialPort:
    """可编程假串口:read 按脚本逐项吐字节,脚本耗尽回空字节(模拟超时)。"""

    def __init__(self, script: List[bytes]) -> None:
        self.script = list(script)
        self.timeout = 0.0
        self.write_timeout = 0.0
        self.closed = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes) -> int:
        return len(data)

    def read(self, size: int) -> bytes:
        if not self.script:
            return b""
        chunk = self.script.pop(0)
        return chunk[:size]


class TestSerialTransportRecv:
    """串口 recv 超时三分支(0 字节不断线 / 帧截断断线重同步 / 正常读满)。

    用假 pyserial 模块替换 ``sys.modules["serial"]``——SerialTransport
    在 connect() 里才延迟导入,无需真实串口。
    """

    @staticmethod
    def _make_transport(monkeypatch: pytest.MonkeyPatch, script: List[bytes]):
        import sys
        import types

        port = _FakeSerialPort(script)
        fake = types.ModuleType("serial")
        fake.Serial = lambda: port  # type: ignore[attr-defined]
        fake.PARITY_NONE = "N"  # type: ignore[attr-defined]
        fake.PARITY_EVEN = "E"  # type: ignore[attr-defined]
        fake.PARITY_ODD = "O"  # type: ignore[attr-defined]
        fake.FIVEBITS = 5  # type: ignore[attr-defined]
        fake.SIXBITS = 6  # type: ignore[attr-defined]
        fake.SEVENBITS = 7  # type: ignore[attr-defined]
        fake.EIGHTBITS = 8  # type: ignore[attr-defined]
        fake.STOPBITS_ONE = 1  # type: ignore[attr-defined]
        fake.STOPBITS_ONE_POINT_FIVE = 1.5  # type: ignore[attr-defined]
        fake.STOPBITS_TWO = 2  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "serial", fake)

        from omniplc.transport.serial import SerialTransport

        transport = SerialTransport(SerialConfig(port_name="COM3"))
        transport.receive_timeout = 0.1
        transport.connect()
        return transport, port

    def test_recv_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport, port = self._make_transport(monkeypatch, [b"AB", b"CD"])
        try:
            assert transport.recv(4) == b"ABCD"
            assert port.closed is False
        finally:
            transport.close()

    def test_recv_timeout_zero_bytes_keeps_link(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 字节已读超时:TransportTimeoutError(不断线),串口保持打开。"""
        from omniplc.core.errors import TransportTimeoutError

        transport, port = self._make_transport(monkeypatch, [])
        try:
            with pytest.raises(TransportTimeoutError):
                transport.recv(4)
            assert port.closed is False  # 线上安静,无残渣,不断线
        finally:
            transport.close()

    def test_recv_partial_truncation_closes_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """部分字节已读后超时(帧截断):TransportClosedError + 串口已主动关闭
        ——残渣已被消费、无法回退,下一帧开头必然错位,断线重开是唯一
        重新同步手段(与 RTU CRC 校验失败后的断线恢复同一逻辑)。
        """
        from omniplc.core.errors import TransportClosedError

        transport, port = self._make_transport(monkeypatch, [b"AB"])
        try:
            with pytest.raises(TransportClosedError) as excinfo:
                transport.recv(4)
            assert port.closed is True
            assert "2/4" in str(excinfo.value)  # 消息带已收/期望字节数
        finally:
            transport.close()


class TestSerialConfig:
    """串口参数校验(不打开真实串口)。"""

    def test_valid(self) -> None:
        config = SerialConfig(port_name="COM3")
        config.validate()

    def test_parity_enum_accepted(self) -> None:
        config = SerialConfig(port_name="COM3", parity=SerialParity.EVEN)
        config.validate()

    def test_parity_string_accepted(self) -> None:
        config = SerialConfig(port_name="COM3", parity="E")
        config.validate()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"port_name": ""},
            {"port_name": "COM3", "baud_rate": 0},
            {"port_name": "COM3", "data_bits": 4},
            {"port_name": "COM3", "stop_bits": 3},
            {"port_name": "COM3", "parity": "X"},
        ],
    )
    def test_invalid(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            SerialConfig(**kwargs).validate()
