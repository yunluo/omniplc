"""传输层单元测试:TCP/UDP 走本机 echo 服务。"""

from __future__ import annotations

import logging
import socket
from typing import List, Optional

import pytest

from omniplc.core.debug import LOGGER_NAME
from omniplc.core.errors import DeviceError, TransportClosedError
from omniplc.core.types import SerialParity
from omniplc.transport import SerialConfig, TcpTransport, UdpTransport
from omniplc.transport import udp as udp_module

# UDP 超长报文的两条平台分支(内核行为互斥,单机无法同时复现):
# - POSIX:``recv`` 静默截断,库用 ``MSG_TRUNC`` 探真长并记 WARNING
#   (``udp.py:_SUPPORTS_MSG_TRUNC``,Windows 上该开关恒为假);
# - Windows:``recv`` 抛 ``WSAEMSGSIZE``(errno 10040),库转 ``DeviceError``。
# 两条**库内逻辑**均以假 socket 强制打开对应分支来验证,用例跨平台可跑;
# 内核侧差异记录在 ``transport/udp.py`` 的模块注释里。


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

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_timeout_rejects_nan_inf(self, bad: float) -> None:
        """NaN/inf 能绕过比较判定(review-1023 P1):赋值期必须拒绝,零 FD。"""
        transport = TcpTransport("127.0.0.1", 502)
        with pytest.raises(ValueError):
            transport.receive_timeout = bad
        with pytest.raises(ValueError):
            transport.connect_timeout = bad

    def test_config_failure_after_connect_closes_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_connection 成功后的非 OSError 配置失败也须收 FD(review-1023 P1)。

        ``settimeout(some_value)`` 在本用例里模拟抛 ``ValueError``(真实场景
        即 NaN/inf 致 ``socket.settimeout`` 抛错误)——旧实现只捕 ``OSError``,
        该 FD 收不回且 ``_socket`` 未赋值,客户端兜底也够不到。
        """
        closed: list = []

        class ConfigFailSocket:
            closed_via = None

            def settimeout(self, value: float) -> None:
                raise ValueError("Invalid value NaN (not a number)")

            def setsockopt(self, *_args: object) -> None:  # pragma: no cover
                pass

            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "omniplc.transport.tcp.socket.create_connection",
            lambda *a, **k: ConfigFailSocket(),
        )
        transport = TcpTransport("127.0.0.1", 502)
        with pytest.raises(ValueError):
            transport.connect()
        assert closed == [True]
        assert transport._socket is None

    def test_context_manager(self, tcp_echo_port: int) -> None:
        with TcpTransport("127.0.0.1", tcp_echo_port) as transport:
            transport.send(b"ping")
            assert transport.recv(4) == b"ping"
        assert transport._socket is None  # 退出后已关闭

    def test_drain_is_noop_for_stream(self, tcp_echo_port: int) -> None:
        """流式走线排空是 no-op:TCP 无数据报边界,超时即拆连重同步。"""
        with TcpTransport("127.0.0.1", tcp_echo_port) as transport:
            assert transport.drain() == 0


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

    def test_connect_failure_closes_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UDP connect 失败须关闭刚建的套接字(回归:惰性重连反复泄漏 FD)。"""
        closed: list = []

        class FailingSocket:
            def settimeout(self, _value: float) -> None:
                pass

            def connect(self, _addr: object) -> None:
                raise OSError("unreachable")

            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "omniplc.transport.udp.socket.socket", lambda *a, **k: FailingSocket()
        )
        transport = UdpTransport("127.0.0.1", 9600)
        with pytest.raises(OSError):
            transport.connect()
        assert closed == [True]
        assert transport._socket is None

    def test_receive_timeout_config_failure_closes_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect 成功后的收尾 settimeout 失败同样收 FD(review-1023 P1)。"""
        closed: list = []
        calls: list = []

        class HalfFailSocket:
            def settimeout(self, value: float) -> None:
                calls.append(value)
                if len(calls) == 2:  # 第二次 = 配置 receive_timeout 处
                    raise ValueError("Invalid value NaN (not a number)")

            def connect(self, _addr: object) -> None:
                pass

            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "omniplc.transport.udp.socket.socket", lambda *a, **k: HalfFailSocket()
        )
        transport = UdpTransport("127.0.0.1", 9600)
        with pytest.raises(ValueError):
            transport.connect()
        assert closed == [True]
        assert transport._socket is None

    def test_recv_truncation_logs_warning_and_raises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """UDP 数据报超过缓冲:WARNING 提示截断量,并与 Windows 口径一致抛 DeviceError。

        UDP 是原子报文协议——超出 ``size`` 的字节会被**静默丢弃**
        (POSIX 行为),且**不会**留到下一次 recv。原实现 POSIX 分支返回
        截断字节(仅告警)、Windows 分支抛错,跨平台行为翻转;现统一:
        响亮抛 :class:`DeviceError`(负码 -10040,基类不计 device_error_count)。
        假 socket 强制打开 ``MSG_TRUNC`` 截断分支,故 Windows 上同样可测。
        """

        class TruncatingSocket:
            """真长 2000B > 缓冲 1024B:按 ``MSG_TRUNC`` 语义返回真实长度。"""

            def settimeout(self, _value: float) -> None:
                pass

            def recv_into(self, buffer: bytearray, _size: int, _flags: int = 0) -> int:
                buffer[:] = b"\xaa" * len(buffer)
                return 2000

            def recv(self, _size: int) -> bytes:
                raise AssertionError("截断分支应走 recv_into")

            def close(self) -> None:
                pass

        monkeypatch.setattr(udp_module, "_SUPPORTS_MSG_TRUNC", True)
        monkeypatch.setattr(socket, "MSG_TRUNC", 0x20, raising=False)
        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = TruncatingSocket()  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME), pytest.raises(
                DeviceError
            ) as excinfo:
                transport.recv(1024)
        finally:
            transport.close()
        assert excinfo.value.code == -10040
        assert "2000" in str(excinfo.value)
        # 截断日志必出现,带"实收 2000B,缓冲 1024B,超出 976B"
        truncate_records = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "UDP 数据报截断" in record.getMessage()
        ]
        assert len(truncate_records) == 1
        assert "2000" in truncate_records[0].getMessage()
        assert "1024" in truncate_records[0].getMessage()

    def test_recv_no_truncation_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """UDP 数据报未超 size:不输出截断 WARNING(截断分支同源,跨平台同测)。"""

        class SmallDatagramSocket:
            """真长 100B < 缓冲 1024B:照常写入缓冲并返回真实长度。"""

            def settimeout(self, _value: float) -> None:
                pass

            def recv_into(self, buffer: bytearray, _size: int, _flags: int = 0) -> int:
                buffer[:100] = b"\xaa" * 100
                return 100

            def close(self) -> None:
                pass

        monkeypatch.setattr(udp_module, "_SUPPORTS_MSG_TRUNC", True)
        monkeypatch.setattr(socket, "MSG_TRUNC", 0x20, raising=False)
        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = SmallDatagramSocket()  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                frame = transport.recv(1024)
        finally:
            transport.close()
        assert frame == b"\xaa" * 100
        truncate_records = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "UDP 数据报截断" in record.getMessage()
        ]
        assert len(truncate_records) == 0

    def test_recv_oversize_raises_device_error(self) -> None:
        """``recv`` 报 WSAEMSGSIZE(errno 10040):库转 :class:`DeviceError` 并留码。

        Windows 内核对该情形直接抛错(故 ``udp.py`` 的平台分支为 Windows 保留);
        库捕获后转 :class:`DeviceError`,让基类按 DEVICE 分类记入
        ``last_error_category``(链路完好,区分于真断线 TRANSPORT)且不触发重连
        (协议层 size 估错或对端报文超长,与链路健康无关)。
        假 socket 强制抛出该错误,故非 Windows 平台同样可测这条映射逻辑。
        """

        class OversizeSocket:
            """``recv``/``recv_into`` 恒抛 WSAEMSGSIZE(Windows 内核行为)。"""

            def settimeout(self, _value: float) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                raise OSError(10040, "WSAEMSGSIZE: message too long")

            def recv_into(self, _buffer: bytearray, _size: int, _flags: int = 0) -> int:
                # POSIX 分支(udp.py)经 recv_into 探测截断,同抛
                raise OSError(10040, "WSAEMSGSIZE: message too long")

            def close(self) -> None:
                pass

        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = OversizeSocket()  # type: ignore[assignment]
        try:
            with pytest.raises(DeviceError) as excinfo:
                transport.recv(1024)
        finally:
            transport.close()
        # message 直说"UDP 报文超过缓冲"
        assert "UDP 报文超过缓冲" in str(excinfo.value)
        assert "1024" in str(excinfo.value)
        # code 为诊断性负码(-10040):避开真实协议错误码空间,
        # 基类不把该失败计入 device_error_count(本地缓冲问题非 PLC 报错)
        assert excinfo.value.code == -10040

    def test_drain_stale_datagrams(self) -> None:
        """排空陈旧帧:预置帧逐个吐出,缓冲见底即停(BlockingIOError 语义)。"""

        class StaleSocket:
            """预置陈旧帧的非阻塞 socket:吐尽即抛 BlockingIOError。"""

            def __init__(self) -> None:
                self.pool = [b"\x01\x02", b"\x03"]
                self.blocking: Optional[bool] = None
                self.timeouts: List[Optional[float]] = []

            def setblocking(self, value: bool) -> None:
                self.blocking = value

            def settimeout(self, value: Optional[float]) -> None:
                self.timeouts.append(value)

            def recv(self, _size: int) -> bytes:
                if not self.pool:
                    raise BlockingIOError()
                return self.pool.pop(0)

            def close(self) -> None:
                pass

        fake = StaleSocket()
        transport = UdpTransport("127.0.0.1", 9600)
        transport.receive_timeout = 2.5
        transport._socket = fake  # type: ignore[assignment]
        try:
            assert transport.drain() == 2
            # 第二次排空:缓冲已见底,返回 0(陈旧帧防护的常规空转)
            assert transport.drain() == 0
        finally:
            transport.close()
        assert fake.pool == []
        # 排空期间临时切非阻塞,结束后恢复 receive_timeout
        assert fake.blocking is False
        assert fake.timeouts == [2.5, 2.5]

    def test_drain_drops_oversize_stale(self) -> None:
        """陈旧帧的超长属性(WSAEMSGSIZE)在排空语境下照排不报。"""

        class MixedSocket:
            """先抛一次 10040(超长陈旧帧),再缓冲见底。"""

            def __init__(self) -> None:
                self.calls = 0

            def setblocking(self, _value: bool) -> None:
                pass

            def settimeout(self, _value: Optional[float]) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    raise OSError(10040, "WSAEMSGSIZE: message too long")
                raise BlockingIOError()

            def close(self) -> None:
                pass

        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = MixedSocket()  # type: ignore[assignment]
        try:
            assert transport.drain() == 1
        finally:
            transport.close()

    def test_drain_cap_limits_runaway(self, caplog: pytest.LogCaptureFixture) -> None:
        """排空达上限(64)即停:对端线速灌包的异常形态不长时间占锁。"""

        class FloodingSocket:
            """恒有数据:模拟对端持续灌包(排空永远见不了底)。"""

            def setblocking(self, _value: bool) -> None:
                pass

            def settimeout(self, _value: Optional[float]) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                return b"\xaa"

            def close(self) -> None:
                pass

        transport = UdpTransport("127.0.0.1", 9600)
        transport._socket = FloodingSocket()  # type: ignore[assignment]
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
                assert transport.drain() == 64
        finally:
            transport.close()
        assert any("排空达上限" in record.getMessage() for record in caplog.records)

    def test_drain_without_connect(self) -> None:
        """未初始化即排空:TransportClosedError(与 send/recv 同契约)。"""
        transport = UdpTransport("127.0.0.1", 9600)
        with pytest.raises(TransportClosedError):
            transport.drain()


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


class _BufferedFakeSerialPort(_FakeSerialPort):
    """带接收缓冲的假串口:``in_waiting`` 反映已到达字节数,``read`` 只消费所需。

    ``_FakeSerialPort`` 的 ``read`` 整项弹出(适配"读满恰好 size"的 recv
    用例),不适合 ``recv_some``——后者依赖"读走 1 字节后其余仍可读"的
    真机语义。
    """

    def __init__(self, bursts: List[bytes]) -> None:
        super().__init__(bursts)
        self._buffer = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def read(self, size: int) -> bytes:
        if not self._buffer:
            if not self.script:
                return b""  # 线静默:模拟超时
            self._buffer.extend(self.script.pop(0))
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk


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

    def test_recv_timeout_zero_bytes_keeps_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 字节已读超时:TransportTimeoutError(不断线),串口保持打开。"""
        from omniplc.core.errors import TransportTimeoutError

        transport, port = self._make_transport(monkeypatch, [])
        try:
            with pytest.raises(TransportTimeoutError):
                transport.recv(4)
            assert port.closed is False  # 线上安静,无残渣,不断线
        finally:
            transport.close()

    def test_recv_partial_truncation_closes_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    @pytest.mark.parametrize(
        "kwargs",
        [
            # review-1038 §7.2:原裸比较放过 nan/inf/bool/浮点,越到 connect
            # 才炸成 CPython 文案 / KeyError;字符串参数更抛 TypeError 直穿
            # read() 公开 API。现构造期即 ValueError。
            {"port_name": "COM3", "baud_rate": float("nan")},
            {"port_name": "COM3", "baud_rate": float("inf")},
            {"port_name": "COM3", "baud_rate": True},
            {"port_name": "COM3", "baud_rate": 9600.7},
            {"port_name": "COM3", "baud_rate": "9600"},
            {"port_name": "COM3", "data_bits": 6.5},
            {"port_name": "COM3", "data_bits": True},
            {"port_name": "COM3", "data_bits": "8"},
        ],
    )
    def test_non_int_field_rejected_at_construction(self, kwargs: dict) -> None:
        """非整数字段(含 bool/浮点/字符串)构造期 ValueError,不是 TypeError/KeyError。"""
        with pytest.raises(ValueError, match="必须是整数"):
            SerialConfig(**kwargs)

    def test_construction_validates_eagerly(self) -> None:
        """构造即校验:参数非法在 configure_serial() 调用点就暴露。"""
        with pytest.raises(ValueError, match="波特率"):
            SerialConfig(port_name="COM3", baud_rate=-1)


class TestSerialTransportRecvSome:
    """串口 recv_some:流式"有数据即返回"(review-1038 §7.1)。

    原基类默认退化为 ``recv``(读满恰好 size;不足即关串口),使流式成帧
    在串口走线上必然失败——本节用例挂带接收缓冲的假串口钉住新口径。
    """

    @staticmethod
    def _make_transport(monkeypatch: pytest.MonkeyPatch, bursts: List[bytes]):
        import sys
        import types

        port = _BufferedFakeSerialPort(bursts)
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

    def test_returns_batch_without_filling_max_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有数据即返回:一批 2 字节不因 ``max_bytes=4098`` 而等待/凑满。"""
        transport, port = self._make_transport(monkeypatch, [b"AB", b"CD"])
        try:
            assert transport.recv_some(4098) == b"AB"
            assert transport.recv_some(4098) == b"CD"
            assert port.closed is False
        finally:
            transport.close()

    def test_respects_max_bytes_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``max_bytes`` 是上限:一次突发超出即按上限切分,余量留缓冲。"""
        transport, port = self._make_transport(monkeypatch, [b"ABCDEF"])
        try:
            assert transport.recv_some(3) == b"ABC"
            assert transport.recv_some(3) == b"DEF"
        finally:
            transport.close()

    def test_silence_timeout_keeps_port_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无数据:0 字节口径 TransportTimeoutError,**不关串口**(与 recv 同)。

        这正是原默认退化最恶劣处——``recv_some(KV_MAX_LINE)`` 在安静线上
        会走"部分/零字节 → 关串口"路径,端到端表现为每事务"超时→关串口→
        重连→再超时"永久失败。
        """
        from omniplc.core.errors import TransportTimeoutError

        transport, port = self._make_transport(monkeypatch, [])
        try:
            with pytest.raises(TransportTimeoutError):
                transport.recv_some(4098)
            assert port.closed is False
        finally:
            transport.close()

    def test_non_positive_max_bytes_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``max_bytes`` ≤0 是调用方参数错误,参数期 ValueError。"""
        transport, _port = self._make_transport(monkeypatch, [b"AB"])
        try:
            with pytest.raises(ValueError, match="max_bytes"):
                transport.recv_some(0)
        finally:
            transport.close()

    def test_closed_port_raises_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """串口未打开:与 recv 同,TransportClosedError。"""
        from omniplc.core.errors import TransportClosedError

        transport, _port = self._make_transport(monkeypatch, [b"AB"])
        transport.close()
        with pytest.raises(TransportClosedError):
            transport.recv_some(8)
