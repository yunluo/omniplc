"""MTConnect 客户端测试:假 HTTP 连接驱动真实解析/翻译代码。

连接工厂 ``_new_connection`` 以模块级函数隔离(同 MX Component 惯例),
测试替换为内存版假连接:按路径返回罐头 XML,可注入 socket/协议异常。
"""
from __future__ import annotations

from typing import Optional

import pytest

from omniplc import MTConnectClient
from omniplc.aio import AMTConnectClient
from omniplc.cnc import mtconnect as mtc_module
from omniplc.cnc.mtconnect import _MtConnectSession

_CURRENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3">
  <Header instanceId="123" bufferSize="131072" nextSequence="45"/>
  <Streams>
    <DeviceStream name="VMC-850" uuid="dev.001">
      <ComponentStream component="Controller" id="c1">
        <Events>
          <Availability>AVAILABLE</Availability>
          <ControllerMode dataItemId="mode" name="cmode">AUTOMATIC</ControllerMode>
          <Execution dataItemId="exec">ACTIVE</Execution>
          <Program dataItemId="program" name="program">O0022.nc</Program>
          <PartCount dataItemId="pcount" name="pcount">12</PartCount>
          <DoorState dataItemId="door">CLOSED</DoorState>
          <ClampState dataItemId="clamp" name="clamp">true</ClampState>
          <AuxPower dataItemId="aux">FALSE</AuxPower>
          <PowerState dataItemId="power">UNAVAILABLE</PowerState>
        </Events>
      </ComponentStream>
      <ComponentStream component="Rotary" id="s1">
        <Samples>
          <SpindleSpeed dataItemId="Sspeed" name="Sspeed" subType="ACTUAL">8400.0</SpindleSpeed>
          <Position dataItemId="Xact" name="Xact" subType="ACTUAL">123.456</Position>
          <Position dataItemId="Yact">-0.5</Position>
          <Load dataItemId="Sload">35.2</Load>
        </Samples>
        <Condition>
          <Fault dataItemId="Sload_c" type="LOAD" nativeCode="201" severity="WARNING">主轴负载高</Fault>
          <Normal dataItemId="cool_c" type="PRESSURE"/>
        </Condition>
      </ComponentStream>
    </DeviceStream>
  </Streams>
</MTConnectStreams>"""

_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MTConnectError xmlns="urn:mtconnect.org:MTConnectError:1.3">
  <Errors>
    <Error errorCode="OUT_OF_RANGE">from sequence must be positive</Error>
  </Errors>
</MTConnectError>"""

_PROBE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MTConnectDevices xmlns="urn:mtconnect.org:MTConnectDevices:1.3">
  <Header instanceId="123" bufferSize="131072"/>
  <Devices>
    <Device name="VMC-850" uuid="dev.001" sampleInterval="100">
      <Description manufacturer="ACME">三轴立加</Description>
    </Device>
  </Devices>
</MTConnectDevices>"""


class FakeResponse:
    """假 HTTP 响应:状态码 + 预置响应体。"""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeHTTPConnection:
    """内存版 HTTPConnection:按路径回罐头文档,可注入异常。"""

    def __init__(self, host: str, port: int, timeout: Optional[float] = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.requests: list = []
        self.responses: dict = {}
        self.fail_next: Optional[BaseException] = None

    def request(self, method: str, path: str, headers: Optional[dict] = None) -> None:
        self.requests.append(path)

    def getresponse(self) -> FakeResponse:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        path = self.requests[-1]
        if path not in self.responses:
            # 真实 Agent 对未知路径也返回错误文档(4xx)
            return FakeResponse(404, _ERROR_XML.encode("utf-8"))
        status, body = self.responses[path]
        return FakeResponse(status, body)

    def close(self) -> None:
        pass


def _client(monkeypatch: pytest.MonkeyPatch) -> MTConnectClient:
    """挂上假连接工厂,返回已连接客户端(未建真 TCP)。"""
    conn = FakeHTTPConnection("127.0.0.1", 5000)
    conn.responses = {
        "/current": (200, _CURRENT_XML.encode("utf-8")),
        "/probe": (200, _PROBE_XML.encode("utf-8")),
    }
    monkeypatch.setattr(mtc_module, "_new_connection", lambda ip, port, timeout: conn)
    client = MTConnectClient("127.0.0.1", 5000)
    assert client.connect() is True
    return client


# ----------------------------------------------------------------------
# 快照与类型化读
# ----------------------------------------------------------------------

def test_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """/current 全量快照:数据项 id 与 name 均入表,非数据项不入。"""
    client = _client(monkeypatch)
    ok, items = client.snapshot()
    assert ok is True and items is not None
    assert items["Sspeed"] == "8400.0"
    assert items["Xact"] == "123.456"
    assert items["Yact"] == "-0.5"
    assert items["exec"] == "ACTIVE"
    assert items["cmode"] == "AUTOMATIC"  # name 兼容寻址
    assert "Availability" not in items  # 无 dataItemId 的非数据项不进快照


def test_typed_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """类型化读:float/int/string/bool 按显式类型收窄。"""
    client = _client(monkeypatch)
    assert client.read_float("Sspeed") == (True, 8400.0)
    assert client.read_int("pcount") == (True, 12)
    assert client.read_string("program") == (True, "O0022.nc")
    assert client.read_string("program", 8, "gbk") == (True, "O0022.nc")  # length/encoding 不适用
    assert client.read_float("Xact") == (True, 123.456)


def test_bool_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """布尔量:true/false 与大小写兼容;非布尔文本抛 ValueError。"""
    client = _client(monkeypatch)
    assert client.read_bool("clamp") == (True, True)
    assert client.read_bool("aux") == (True, False)
    with pytest.raises(ValueError):
        client.read_bool("exec")  # ACTIVE 不是布尔文本


def test_read_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """地址兼容数据项的 name 属性。"""
    client = _client(monkeypatch)
    assert client.read_string("cmode") == (True, "AUTOMATIC")


def test_unavailable_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNAVAILABLE → (False, None) + last_error 标注不可用,不断线不重连。"""
    client = _client(monkeypatch)
    ok, value = client.read_float("power")
    assert ok is False and value is None
    assert client.last_error is not None and "UNAVAILABLE" in client.last_error
    assert client.connected is True
    assert client.read_float("Sspeed") == (True, 8400.0)


def test_missing_item_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据项不存在 → (False, None) + last_error,设备侧条件不断线。"""
    client = _client(monkeypatch)
    ok, value = client.read_float("NoSuchItem")
    assert ok is False and value is None
    assert client.last_error is not None and "NoSuchItem" in client.last_error
    assert client.connected is True
    assert client.read_float("Sspeed") == (True, 8400.0)  # 同会话继续可用


def test_type_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """类型不符 → ValueError(参数错误,直接抛出)。"""
    client = _client(monkeypatch)
    with pytest.raises(ValueError):
        client.read_int("Xact")  # 123.456 不是整数文本
    with pytest.raises(ValueError):
        client.read_float("exec")  # ACTIVE 不是数值
    with pytest.raises(ValueError):
        client.read("Xact", "not-a-type")  # 非法类型名
    with pytest.raises(ValueError):
        client.read_string("   ")  # 空地址


def test_empty_address_rejected() -> None:
    """空地址直接 ValueError(不发请求)。"""
    client = MTConnectClient("127.0.0.1")
    with pytest.raises(ValueError):
        client.read_string("")


def test_write_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """只读协议:写返回 False + last_error,不断线。"""
    client = _client(monkeypatch)
    assert client.write_float("Sspeed", 6000.0) is False
    assert client.last_error is not None and "只读" in client.last_error
    assert client.connected is True


# ----------------------------------------------------------------------
# HTTP 状态 / 错误文档 / 断线重连
# ----------------------------------------------------------------------

def test_http_error_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 404 + MTConnectError 文档 → DeviceError(不断线)。"""
    conn = FakeHTTPConnection("127.0.0.1", 5000)
    conn.responses = {
        "/current": (404, _ERROR_XML.encode("utf-8")),
        "/probe": (200, _PROBE_XML.encode("utf-8")),
    }
    monkeypatch.setattr(mtc_module, "_new_connection", lambda ip, port, timeout: conn)
    client = MTConnectClient("127.0.0.1", 5000)
    assert client.connect() is True
    ok, value = client.read_float("Sspeed")
    assert ok is False and value is None
    assert client.last_error is not None and "OUT_OF_RANGE" in client.last_error
    assert client.connected is True


def test_garbage_body_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 但非法 XML → ProtocolFrameError 断线;下次读惰性重连恢复。"""
    conn = FakeHTTPConnection("127.0.0.1", 5000)
    conn.responses = {
        "/current": (200, b"<html>proxy error</html>"),
        "/probe": (200, _PROBE_XML.encode("utf-8")),
    }
    monkeypatch.setattr(mtc_module, "_new_connection", lambda ip, port, timeout: conn)
    client = MTConnectClient("127.0.0.1", 5000)
    assert client.connect() is True
    assert client.read_float("Sspeed") == (False, None)
    assert client.connected is False
    conn.responses["/current"] = (200, _CURRENT_XML.encode("utf-8"))
    assert client.read_float("Sspeed") == (True, 8400.0)
    assert client.connected is True


def test_socket_error_lazy_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """socket 故障 → 断线;下次读自动重建连接(惰性重连)。"""
    conn = FakeHTTPConnection("127.0.0.1", 5000)
    conn.responses = {"/current": (200, _CURRENT_XML.encode("utf-8"))}
    conn.fail_next = ConnectionError("连接被对端关闭")
    created: list = []

    def factory(ip: str, port: int, timeout: float) -> FakeHTTPConnection:
        if not created:
            created.append(conn)
            return conn
        second = FakeHTTPConnection(ip, port, timeout)
        second.responses = dict(conn.responses)
        created.append(second)
        return second

    monkeypatch.setattr(mtc_module, "_new_connection", factory)
    client = MTConnectClient("127.0.0.1", 5000)
    assert client.connect() is True
    assert client.read_float("Sspeed") == (False, None)
    assert client.connected is False
    assert client.read_float("Sspeed") == (True, 8400.0)
    assert client.connected is True
    assert len(created) == 2


# ----------------------------------------------------------------------
# probe / 条件项
# ----------------------------------------------------------------------

def test_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """/probe 返回第一个 Device 的属性。"""
    client = _client(monkeypatch)
    ok, device = client.probe()
    assert ok is True and device is not None
    assert device["name"] == "VMC-850"
    assert device["uuid"] == "dev.001"


def test_read_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """条件项:Fault 带码与文本,无文本的 Normal 也入列表。"""
    client = _client(monkeypatch)
    ok, conditions = client.read_conditions()
    assert ok is True and conditions is not None
    fault = [item for item in conditions if item["level"] == "Fault"]
    assert len(fault) == 1
    assert fault[0]["id"] == "Sload_c"
    assert fault[0]["code"] == "201"
    assert fault[0]["severity"] == "WARNING"
    assert fault[0]["text"] == "主轴负载高"
    normal = [item for item in conditions if item["level"] == "Normal"]
    assert len(normal) == 1 and normal[0]["id"] == "cool_c"


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------

def test_async_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:snapshot/类型化读/条件项/probe 经单工作线程驱动同步实例。"""
    import asyncio

    conn = FakeHTTPConnection("127.0.0.1", 5000)
    conn.responses = {"/current": (200, _CURRENT_XML.encode("utf-8"))}
    monkeypatch.setattr(mtc_module, "_new_connection", lambda ip, port, timeout: conn)

    async def scenario() -> None:
        client = AMTConnectClient("127.0.0.1", 5000)
        assert await client.connect() is True
        ok, items = await client.snapshot()
        assert ok is True and items is not None
        assert items["Sspeed"] == "8400.0"
        assert await client.read_float("Sspeed") == (True, 8400.0)
        ok, alarms = await client.read_conditions()
        assert ok is True and alarms is not None
        assert len(alarms) == 2
        ok, device = await client.probe()
        assert ok is False  # /probe 未配置 → 404 + 错误文档 → DeviceError
        assert client.connected is True
        await client.disconnect()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 会话适配器:未连接即用
# ----------------------------------------------------------------------

def test_session_without_connect() -> None:
    """未建立会话调用 request → TransportClosedError。"""
    session = _MtConnectSession("127.0.0.1", 5000)
    with pytest.raises(mtc_module.TransportClosedError):
        session.request("/current")
