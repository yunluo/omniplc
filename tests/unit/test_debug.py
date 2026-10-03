"""全局报文调试开关测试:开关状态、报文/操作日志、走线与会话挂钩。

- 走线型:本机 echo 服务验证 TCP/UDP 收发报文输出
- 会话型:假 OPC-UA 客户端/假 MX COM 控件验证操作级输出
"""
from __future__ import annotations

import logging
import sys
import time
import types
from typing import Iterator

import pytest

from omniplc import MelsecMxClient
from omniplc.core import debug
from omniplc.plc.opcua.client import _OpcUaSession
from omniplc.plc.melsec import mx as mx_module
from omniplc.transport import TcpTransport, UdpTransport


@pytest.fixture(autouse=True)
def _restore_debug_state() -> Iterator[None]:
    """每个用例后关闭调试与黑匣子,避免污染其他测试。"""
    yield
    debug.set_debug(False)
    debug.set_frame_recorder(False)


# ----------------------------------------------------------------------
# 开关与日志助手
# ----------------------------------------------------------------------

def test_set_debug_toggle() -> None:
    """set_debug 开/关:全局标志与记录器级别同步切换。"""
    assert debug.debug_enabled() is False
    debug.set_debug(True)
    assert debug.debug_enabled() is True
    assert debug._logger.level == logging.DEBUG
    debug.set_debug(False)
    assert debug.debug_enabled() is False
    assert debug._logger.level == logging.NOTSET


def test_log_frame_enabled(caplog: pytest.LogCaptureFixture) -> None:
    """开启后:报文按 标识 + 方向 + 长度 + 十六进制 输出。"""
    debug.set_debug(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        debug.log_frame("tcp://1.2.3.4:502", debug.SEND_MARK, b"\x01\x02\x03")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "tcp://1.2.3.4:502 → 发送 3B: 01 02 03" in text


def test_log_frame_disabled_silent(caplog: pytest.LogCaptureFixture) -> None:
    """默认关闭:任何报文/操作都不产生日志。"""
    debug.log_frame("tcp://1.2.3.4:502", debug.SEND_MARK, b"\x01\x02\x03")
    debug.log_op("opcua://x", "读 ns=2;s=T → 1")
    assert caplog.records == []


def test_log_frame_truncates_large_dump(caplog: pytest.LogCaptureFixture) -> None:
    """超长报文只转储前 4096B,并注明总长。"""
    debug.set_debug(True)
    big = bytes(range(256)) * 20  # 5120 字节
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        debug.log_frame("tcp://x:1", debug.RECV_MARK, big)
    text = caplog.records[0].getMessage()
    assert "5120B:" in text
    dumped = text.split(": ", 1)[1].split(" …", 1)[0]
    assert len(dumped.split(" ")) == 4096
    assert "仅转储前 4096B,共 5120B" in text


def test_attach_default_handler_only_when_unconfigured() -> None:
    """无任何日志配置时挂 stderr 处理器;已配置时不接管、重复调用不重复挂。"""
    logger = logging.Logger("omniplc.debug-test")
    root = logging.Logger("root-test")
    debug._attach_default_handler(logger, root)
    assert len(logger.handlers) == 1
    assert logger.propagate is False
    debug._attach_default_handler(logger, root)
    assert len(logger.handlers) == 1

    logger2 = logging.Logger("omniplc.debug-test2")
    root2 = logging.Logger("root-test2")
    root2.addHandler(logging.NullHandler())
    debug._attach_default_handler(logger2, root2)
    assert logger2.handlers == []
    assert logger2.propagate is True


# ----------------------------------------------------------------------
# 走线型:TCP / UDP(本机 echo)
# ----------------------------------------------------------------------

def test_tcp_frames_logged(tcp_echo_port: int, caplog: pytest.LogCaptureFixture) -> None:
    """TCP:连接事件 + 收发报文十六进制全部输出。"""
    debug.set_debug(True)
    transport = TcpTransport("127.0.0.1", tcp_echo_port)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        transport.connect()
        transport.send(b"\x01\x02")
        assert transport.recv(2) == b"\x01\x02"
        transport.close()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "tcp://127.0.0.1:{} 已连接".format(tcp_echo_port) in text
    assert "tcp://127.0.0.1:{} → 发送 2B: 01 02".format(tcp_echo_port) in text
    assert "tcp://127.0.0.1:{} ← 接收 2B: 01 02".format(tcp_echo_port) in text
    assert "tcp://127.0.0.1:{} 已断开".format(tcp_echo_port) in text


def test_udp_frames_logged(udp_echo_port: int, caplog: pytest.LogCaptureFixture) -> None:
    """UDP:数据报收发输出。"""
    debug.set_debug(True)
    transport = UdpTransport("127.0.0.1", udp_echo_port)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        transport.connect()
        transport.send(b"\xaa\x55")
        assert transport.recv(4096) == b"\xaa\x55"
        transport.close()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "udp://127.0.0.1:{} → 发送 2B: AA 55".format(udp_echo_port) in text
    assert "udp://127.0.0.1:{} ← 接收 2B: AA 55".format(udp_echo_port) in text


# ----------------------------------------------------------------------
# 会话型:OPC-UA / MX Component
# ----------------------------------------------------------------------

class _FakeUaNode:
    """假 asyncua 节点:读返回预置值。"""

    def __init__(self, values: dict, text: str) -> None:
        self._values = values
        self.text = text

    def read_value(self) -> object:
        return self._values[self.text]

    def write_value(self, value: object, variant_type: object) -> None:
        pass


class _FakeUaClient:
    """假 asyncua.sync.Client:按 NodeId 返回假节点。"""

    def __init__(self) -> None:
        self.values: dict = {}
        self.nodes: dict = {}

    def get_node(self, text: str) -> _FakeUaNode:
        if text not in self.nodes:
            node = _FakeUaNode(self.values, text)
            self.nodes[text] = node
        return self.nodes[text]


def test_opcua_ops_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """OPC-UA:读写操作输出节点、类型与值(写以桩 asyncua.ua 解析类型)。"""
    ua_mod = types.ModuleType("asyncua.ua")

    class _VariantType:
        UInt16 = object()

    setattr(ua_mod, "VariantType", _VariantType)
    pkg = types.ModuleType("asyncua")
    setattr(pkg, "ua", ua_mod)
    monkeypatch.setitem(sys.modules, "asyncua", pkg)
    monkeypatch.setitem(sys.modules, "asyncua.ua", ua_mod)

    fake = _FakeUaClient()
    fake.values["ns=2;s=Run"] = True
    session = _OpcUaSession("opc.tcp://127.0.0.1:4840")
    session._client = fake
    debug.set_debug(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        assert session.read_value("ns=2;s=Run") is True
        session.write_value("ns=2;s=Speed", 1200, "UInt16")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "opcua://opc.tcp://127.0.0.1:4840 读 ns=2;s=Run → True" in text
    assert "opcua://opc.tcp://127.0.0.1:4840 写 ns=2;s=Speed ← 1200(UInt16)" in text


class _FakeCom:
    """假 ActUtlType:Open/Close 返回正常码。"""

    def Open(self) -> int:
        return 0

    def Close(self) -> int:
        return 0


def test_mx_ops_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """MX Component:连接事件 + Get/Set/块读写操作全部输出。"""
    com = _FakeCom()
    monkeypatch.setattr(mx_module, "_com_initialize", lambda: None)
    monkeypatch.setattr(mx_module, "_new_com_object", lambda station: com)
    monkeypatch.setattr(mx_module, "_com_get_device", lambda com_, text: 1234)
    monkeypatch.setattr(mx_module, "_com_set_device", lambda com_, text, value: None)
    monkeypatch.setattr(mx_module, "_com_read_words", lambda com_, text, count: [1, 2])
    monkeypatch.setattr(mx_module, "_com_write_words", lambda com_, text, words: None)

    client = MelsecMxClient(1)
    debug.set_debug(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        client.connect()
        assert client.read_ushort("D100") == (True, 1234)
        assert client.write_ushort("D100", 7) is True
        ok, value = client.read_float("D200")
        assert ok is True and value is not None
        assert client.write_float("D200", 1.5) is True
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "mx://站号1 会话已建立" in text
    assert "mx://站号1 GetDevice D100 → 1234" in text
    assert "mx://站号1 SetDevice D100 ← 7" in text
    assert "mx://站号1 ReadDeviceBlock D200×2 → [1, 2]" in text
    assert "mx://站号1 WriteDeviceBlock D200×2 ← [" in text


def test_log_op_lazy_args_when_disabled() -> None:
    """关闭时 log_op 不求值参数(%r 不执行——与走线型 log_frame 同口径零成本)。"""

    class _Boom:
        def __repr__(self) -> str:
            raise AssertionError("调试关闭时不应格式化参数")

    debug.log_op("tcp://x", "value %r", _Boom())  # 不抛异常即证明惰性


def test_log_op_formats_args_when_enabled(caplog: pytest.LogCaptureFixture) -> None:
    """开启后 log_op 按 %-模板格式化,输出与 .format 时代一致。"""
    debug.set_debug(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        debug.log_op("opcua://x", "读 %s → %r", "ns=2;s=T", True)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "opcua://x 读 ns=2;s=T → True" in text


# ----------------------------------------------------------------------
# 报文黑匣子(set_frame_recorder:环形缓冲只存不打印)
# ----------------------------------------------------------------------

def test_recorder_default_off() -> None:
    """默认关闭:不记录、快照为空。"""
    assert debug.frame_recorder_enabled() is False
    debug.log_frame("tcp://1.2.3.4:502", debug.SEND_MARK, b"\x01")
    assert debug.recorded_frames() == []


def test_recorder_records_frames() -> None:
    """开启后:方向/标识/原始字节/墙钟时间戳逐条留存,先旧后新。"""
    debug.set_frame_recorder(True, capacity=10)
    assert debug.frame_recorder_enabled() is True
    before = time.time()
    debug.log_frame("tcp://1.2.3.4:502", debug.SEND_MARK, b"\x01\x02")
    debug.log_frame("tcp://1.2.3.4:502", debug.RECV_MARK, b"\x01\x02\x03\x04")
    records = debug.recorded_frames()
    assert len(records) == 2
    assert [rec.direction for rec in records] == [debug.SEND_MARK, debug.RECV_MARK]
    assert records[0].label == "tcp://1.2.3.4:502"
    assert records[0].data == b"\x01\x02"
    assert records[1].data == b"\x01\x02\x03\x04"
    assert all(before - 1 <= rec.at <= time.time() + 1 for rec in records)


def test_recorder_capacity_truncates() -> None:
    """容量截断:只留最近 N 帧。"""
    debug.set_frame_recorder(True, capacity=3)
    for seq in range(4):
        debug.log_frame("tcp://x", debug.SEND_MARK, bytes([seq]))
    records = debug.recorded_frames()
    assert [rec.data[0] for rec in records] == [1, 2, 3]


def test_recorder_snapshot_is_copy() -> None:
    """快照拷贝:取快照后新报文进缓冲,不影响已取列表。"""
    debug.set_frame_recorder(True, capacity=10)
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x01")
    snapshot = debug.recorded_frames()
    assert len(snapshot) == 1
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x02")
    assert len(snapshot) == 1
    assert len(debug.recorded_frames()) == 2


def test_recorder_independent_of_debug(caplog: pytest.LogCaptureFixture) -> None:
    """黑匣子与实时日志相互独立:只开黑匣子 → 有留存无输出;只开日志 → 有输出无留存。"""
    debug.set_frame_recorder(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        debug.log_frame("tcp://x", debug.SEND_MARK, b"\x01")
    assert caplog.records == []
    assert len(debug.recorded_frames()) == 1

    caplog.clear()
    debug.set_frame_recorder(False)
    debug.set_debug(True)
    with caplog.at_level(logging.DEBUG, logger="omniplc.debug"):
        debug.log_frame("tcp://x", debug.SEND_MARK, b"\x01")
    assert "→ 发送 1B: 01" in caplog.records[0].getMessage()
    assert debug.recorded_frames() == []


def test_recorder_disable_clears_reenable_keeps() -> None:
    """关闭丢弃全部留存;重开从头记;同容量重复开启不清空历史(幂等)。"""
    debug.set_frame_recorder(True, capacity=5)
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x01")
    debug.set_frame_recorder(False)
    assert debug.recorded_frames() == []

    debug.set_frame_recorder(True, capacity=5)
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x02")
    debug.set_frame_recorder(True, capacity=5)
    records = debug.recorded_frames()
    assert [rec.data for rec in records] == [b"\x02"]


def test_recorder_capacity_validation() -> None:
    """容量校验:开启时非整数/越界拒绝,缓冲状态不变;关闭不校验容量(P3)。"""
    debug.set_frame_recorder(True, capacity=5)
    for bad in (0, -1, debug.FRAME_RECORDER_MAX_CAPACITY + 1, True, 2.5, "10"):
        with pytest.raises(ValueError):
            debug.set_frame_recorder(True, capacity=bad)  # type: ignore[arg-type]
    assert debug.frame_recorder_enabled() is True
    # 关闭语义与容量无关:enabled=False 时 capacity 任意(含非法值)都直接关闭
    debug.set_frame_recorder(False, capacity=0)
    assert debug.frame_recorder_enabled() is False
    assert debug.recorded_frames() == []


def test_clear_recorded_frames() -> None:
    """clear 只清内容不关开关。"""
    debug.set_frame_recorder(True)
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x01")
    debug.clear_recorded_frames()
    assert debug.recorded_frames() == []
    assert debug.frame_recorder_enabled() is True
    debug.log_frame("tcp://x", debug.SEND_MARK, b"\x02")
    assert len(debug.recorded_frames()) == 1
