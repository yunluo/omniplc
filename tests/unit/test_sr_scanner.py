"""基恩士 SR 扫码枪客户端测试:脚本化传输验证 LON → LOFF → 读应答全流程。

覆盖:

- 命令帧逐字节断言(LON / LON,{bank} / LOFF / BCLR / RESET,CR 结束)
- 应答解析:条码文本 / ERROR / OK / 读超时
- 断线重连:send 失败标记断开,下次扫码惰性重连
- bank 越界参数校验、数据读写原语拒绝
"""
from __future__ import annotations

import socket
import time

import pytest

from omniplc import ErrorCategory, KeyenceSrClient
from omniplc.aio import AKeyenceSrClient
from scripted import ScriptedTransport


@pytest.fixture
def client() -> KeyenceSrClient:
    """扫码枪客户端(扫码窗口缩到最短,避免拖慢测试)。"""
    return KeyenceSrClient("192.168.0.10", 9004, scan_dwell=0.01)


def _mount(
    monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient, scripted: ScriptedTransport
) -> None:
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_scan_success(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """扫码成功:LON → dwell → LOFF → 读到条码文本。"""
    line = b"ABC123\r"
    scripted = ScriptedTransport(_chunks_of(line))
    _mount(monkeypatch, client, scripted)
    assert client.connect() is True
    ok, code = client.scan()
    assert (ok, code) == (True, "ABC123")
    assert bytes(scripted.sent) == b"LON\rLOFF\r"
    assert client.last_error is None


def test_scan_with_bank(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """指定 bank:LON,{bank:02d} 命令。"""
    line = b"XYZ\r"
    scripted = ScriptedTransport(_chunks_of(line))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.scan(bank=1) == (True, "XYZ")
    assert bytes(scripted.sent).startswith(b"LON,01\r")


def test_scan_bank_validation(client: KeyenceSrClient) -> None:
    """bank 越界抛 ValueError。"""
    with pytest.raises(ValueError):
        client.scan(bank=16)
    with pytest.raises(ValueError):
        client.scan(bank=-1)


def test_scan_error_response(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """ERROR 应答 = 未读到条码,链路完好不断线。"""
    line = b"ERROR\r"
    scripted = ScriptedTransport(_chunks_of(line))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.scan() == (False, None)
    assert client.last_error is not None and "ERROR" in client.last_error
    assert client.connected is True
    # 无读出是链路成功的正常结果,不计入设备错误码
    assert client.stats["device_error_count"] == 0
    assert client.stats["transactions"] == 1


def test_scan_ok_no_read(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """OK 应答 = 无读出。"""
    line = b"OK\r"
    scripted = ScriptedTransport(_chunks_of(line))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.scan() == (False, None)
    assert client.last_error is not None and "无读出" in client.last_error


def test_scan_read_timeout(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """LOFF 后无应答(读超时)= 链路完好,不断线,且不计设备错误码。"""

    class TimeoutTransport(ScriptedTransport):
        def recv(self, size: int) -> bytes:
            raise socket.timeout("接收超时")

    scripted = TimeoutTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.scan(timeout=0.5) == (False, None)
    assert client.last_error is not None and "超时" in client.last_error
    assert client.connected is True
    # 超时是 TIMEOUT 分类、无错误码,不计入 device_error_count
    assert client.last_error_category is ErrorCategory.TIMEOUT
    assert client.last_error_code is None
    assert client.stats["device_error_count"] == 0
    assert client.stats["error_count"] == 1
    assert client.stats["transactions"] == 1
    # 默认 write_retries=0:不重试,不重复触发激光
    assert bytes(scripted.sent) == b"LON\rLOFF\r"


def test_scan_retry_uses_write_retries(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """扫码按动作型操作走事务模板:重试次数取 write_retries,不是 retries。"""

    class TimeoutTransport(ScriptedTransport):
        def recv(self, size: int) -> bytes:
            raise socket.timeout("接收超时")

    scripted = TimeoutTransport([])
    _mount(monkeypatch, client, scripted)
    client.retries = 3  # 读重试不参与扫码
    client.write_retries = 1  # 写重试 → 超时后重复触发一次
    client.connect()
    assert client.scan(timeout=0.5) == (False, None)
    # 1 次原始 + 1 次重试,各触发一轮 LON/LOFF(retries=3 未被采用)
    assert bytes(scripted.sent) == b"LON\rLOFF\rLON\rLOFF\r"
    assert client.stats["device_error_count"] == 0
    assert client.connected is True  # 每次都在原连接上重发


def test_scan_send_failure_lazy_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """LON 发送失败 → 标记断开;恢复后下一次扫码惰性重连成功。"""
    client = KeyenceSrClient("192.168.0.10", 9004, scan_dwell=0.01)

    class BrokenThenGood(ScriptedTransport):
        def __init__(self) -> None:
            super().__init__([])
            self.broken = True

        def send(self, data: bytes) -> None:
            if self.broken:
                raise OSError("连接已断开")

    scripted = BrokenThenGood()
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.scan() == (False, None)
    assert client.connected is False
    scripted.broken = False
    scripted._chunks.extend(_chunks_of(b"OK\r"))
    assert client.scan() == (False, None)  # OK = 无读出,但链路已重连
    assert client.connected is True


def test_reset_roundtrip(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """reset:BCLR + RESET 双 OK。"""
    lines = _chunks_of(b"OK\rOK\r")
    scripted = ScriptedTransport(lines)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.reset() is True
    assert bytes(scripted.sent) == b"BCLR\rRESET\r"


def test_reset_failure(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """reset:应答非 OK → False(DeviceError,不断线)。"""
    line = b"ER\r"
    scripted = ScriptedTransport(_chunks_of(line))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.reset() is False
    assert client.connected is True
    assert client.last_error is not None and "OK" in client.last_error


def test_data_read_write_rejected(monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient) -> None:
    """扫码枪不支持 PLC 数据读写:能力缺失 = DeviceError,不断线。"""
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_short("DM100") == (False, None)
    assert client.last_error is not None and "scan()" in client.last_error
    assert client.connected is True  # 能力缺失不拆线(旧实现误走断线分支)
    assert client.last_error_code is None  # code=0 → 无码
    assert client.write_ushort("DM100", 1) is False
    assert client.connected is True


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        KeyenceSrClient("", 9004)
    with pytest.raises(ValueError):
        KeyenceSrClient("192.168.0.10", 0)
    with pytest.raises(ValueError):
        KeyenceSrClient("192.168.0.10", 9004, scan_dwell=0)


def test_scan_dwell_property(client: KeyenceSrClient) -> None:
    """扫码窗口构造期定:构造传参生效,属性只读(双入口取消)。"""
    assert client.scan_dwell == 0.01
    with pytest.raises(AttributeError):
        client.scan_dwell = 2.5


def test_async_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:单工作线程扫码往返。"""
    import asyncio

    async def scenario() -> None:
        client = AKeyenceSrClient("192.168.0.10", 9004, scan_dwell=0.01)
        sync = client._sync
        line = b"BAR-9\r"
        scripted = ScriptedTransport(_chunks_of(line))
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.scan() == (True, "BAR-9")
        await client.close()

    asyncio.run(scenario())


def _chunks_of(line: bytes) -> list:
    return [line[i:i + 1] for i in range(len(line))]


class _HalfLineThenClean(ScriptedTransport):
    """阶段化假传输:半行超时 → drain 吐出残留 → 第二次扫码干净应答。"""

    def __init__(self) -> None:
        super().__init__([])
        self._phase = 0
        self._lines = [[b"B", b"C", b"\r"], _chunks_of(b"XYZ\r")]

    def recv(self, size: int) -> bytes:
        if self._phase == 0:
            self._phase = 1
            return b"A"
        if self._phase == 1:  # 半行后超时
            self._phase = 2
            raise socket.timeout("接收超时")
        if self._phase == 2:  # drain 期:吐出残留后半行
            if len(self._lines[0]) == 1:
                self._phase = 3
            return self._lines[0].pop(0)
        if self._phase == 3:  # 第二次扫码:干净应答
            if self._lines[1]:
                return self._lines[1].pop(0)
            raise socket.timeout("接收超时")
        raise socket.timeout("接收超时")


def test_scan_timeout_drains_half_line(
    monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient
) -> None:
    """超时后残留半行被尽力清掉,下一次扫码不读到旧残留拼接。"""
    transport = _HalfLineThenClean()
    _mount(monkeypatch, client, transport)
    client.connect()
    ok, code = client.scan(timeout=0.2)
    assert ok is False and code is None
    assert "超时" in (client.last_error or "")
    assert client.connected is True
    assert client.scan(timeout=0.2) == (True, "XYZ")


class _TrickleTransport(ScriptedTransport):
    """每 20ms 滴 1 字节、永不换行(验证收行受整事务 deadline 约束)。"""

    def __init__(self) -> None:
        super().__init__([])

    def recv(self, size: int) -> bytes:
        time.sleep(0.02)
        return b"x"


def test_scan_line_deadline_bounds_dribble(
    monkeypatch: pytest.MonkeyPatch, client: KeyenceSrClient
) -> None:
    """滴流对端不能逐字节重置超时:scan 在 read_timeout 预算内超时返回。"""
    transport = _TrickleTransport()
    _mount(monkeypatch, client, transport)
    client.connect()
    started = time.monotonic()
    ok, code = client.scan(timeout=0.05)
    assert ok is False and code is None
    assert time.monotonic() - started < 1.5
    assert client.connected is True  # 读超时不断线语义保留
