"""OPC-UA 客户端测试:假会话注入验证全链路(无需安装 asyncua)。

覆盖:

- NodeId 解析(四类标识符、ns 省略、大小写规范化、非法样本)
- 端点 URL 校验(opc.tcp 前缀、端口范围、路径)
- 读写类型映射与值收窄(BOOL/整数/浮点/字符串;返回类型不符 → last_error)
- Bad 状态码(DeviceError)不断线;连接故障标记断开 + 惰性重连
- asyncua 异常翻译(UaError → DeviceError;其余 → 内部异常)
- 异步镜像往返
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from omniplc import OpcUaClient
from omniplc.aio import AOpcUaClient
from omniplc.core.errors import DeviceError, OmniPLCInternalError
from omniplc.opcua.address import parse_opcua_nodeid
from omniplc.opcua.client import (
    _OpcUaSession,
    _translate_ua_error,
    _validate_endpoint_url,
)


class FakeSession(_OpcUaSession):
    """内存版会话:按 NodeId 文本存值,可注入指定节点的读取异常。"""

    def __init__(self) -> None:
        super().__init__("opc.tcp://fake:4840")
        self.values: dict = {}
        self.written: list = []
        self.read_errors: dict = {}
        self.connect_count = 0

    def connect(self) -> None:
        self.connect_count += 1

    def read_value(self, node_text: str) -> Any:
        if node_text in self.read_errors:
            raise self.read_errors[node_text]
        return self.values[node_text]

    def read_values(self, node_texts: list) -> list:
        out = []
        for text in node_texts:
            if text in self.read_errors:
                raise self.read_errors[text]
            out.append(self.values[text])
        return out

    def write_value(self, node_text: str, value: Any, variant_name: str) -> None:
        self.written.append((node_text, value, variant_name))
        self.values[node_text] = value


# ----------------------------------------------------------------------
# NodeId 解析
# ----------------------------------------------------------------------

def test_parse_nodeid() -> None:
    """NodeId 解析:四类标识符、ns 省略、前缀小写规范化。"""
    parsed = parse_opcua_nodeid("ns=2;s=Device.Tag")
    assert parsed.namespace == 2
    assert parsed.text == "ns=2;s=Device.Tag"
    assert parse_opcua_nodeid("i=2258").namespace == 0
    assert parse_opcua_nodeid("i=2258").text == "i=2258"
    assert parse_opcua_nodeid("ns=4;i=100").text == "ns=4;i=100"
    assert parse_opcua_nodeid("b=AAECAw==").text == "b=AAECAw=="
    assert parse_opcua_nodeid("g=0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0").namespace == 0
    # 前缀大小写规范化,标识符值保留原文
    assert parse_opcua_nodeid("NS=3;S=My.Tag").text == "ns=3;s=My.Tag"
    assert parse_opcua_nodeid("ns=2;S=My.Tag").text == "ns=2;s=My.Tag"


def test_parse_nodeid_errors() -> None:
    """NodeId 解析:非法样本。"""
    for bad in (
        "",
        "D100",          # 不是 NodeId 语法
        "ns=2;",         # 缺标识符
        "ns=x;i=1",      # 命名空间非数字
        "ns=2;t=abc",    # 未知标识符类型
        "s=",            # 空字符串标识符
        "i=abc",         # 数字标识符非数字
        "ns=2;s=a;b=c",  # 标识符内含分号
    ):
        with pytest.raises(ValueError):
            parse_opcua_nodeid(bad)


def test_validate_endpoint_url() -> None:
    """端点 URL 校验:主机/端口/路径/IPv6 与非法样本。"""
    _validate_endpoint_url("opc.tcp://192.168.0.10")
    _validate_endpoint_url("opc.tcp://192.168.0.10:4840")
    _validate_endpoint_url("opc.tcp://host.local:4840/FreeOpcUa")
    _validate_endpoint_url("OPC.TCP://[::1]:4840")
    for bad in (
        "",
        "http://192.168.0.10:4840",
        "opc.tcp://",
        "opc.tcp:// host:4840",
        "opc.tcp://host:0",
        "opc.tcp://host:99999",
    ):
        with pytest.raises(ValueError):
            _validate_endpoint_url(bad)
    with pytest.raises(ValueError):
        OpcUaClient("127.0.0.1", 4840, endpoint="opc.tcp://host:0")  # 覆盖 URL 端口非法
    with pytest.raises(ValueError):
        OpcUaClient("127.0.0.1", 0)  # 端口越界(与全库入口校验一致)


def test_constructor_endpoint_styles() -> None:
    """构造入口:IP+端口(+路径)组装端点 URL,或 endpoint 显式覆盖。"""
    assert OpcUaClient().endpoint == "opc.tcp://192.168.0.10:4840"
    assert OpcUaClient("127.0.0.1").endpoint == "opc.tcp://127.0.0.1:4840"
    assert OpcUaClient("127.0.0.1", 4840, "UA/Server").endpoint == \
        "opc.tcp://127.0.0.1:4840/UA/Server"
    assert OpcUaClient("127.0.0.1", 4840, "/UA/Server/").endpoint == \
        "opc.tcp://127.0.0.1:4840/UA/Server"  # 路径斜杠规范化
    url = "opc.tcp://10.0.0.1:4840/Discovered/Endpoint"
    assert OpcUaClient("127.0.0.1", 4840, endpoint=url).endpoint == url  # 显式覆盖
    # 入口与其他客户端一致:错误消息显示 host:port
    client = OpcUaClient("192.168.0.7", 4840)
    assert client._ip_address == "192.168.0.7" and client._port == 4840


# ----------------------------------------------------------------------
# 读写全链路(假会话)
# ----------------------------------------------------------------------

def test_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读:BOOL/SHORT/FLOAT/STRING 按类型收窄。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    fake.values["ns=2;s=Run"] = True
    fake.values["ns=2;s=Temp"] = -5
    fake.values["ns=2;s=Speed"] = 3.14
    fake.values["ns=2;s=Batch"] = "A2024"
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.read_bool("ns=2;s=Run") == (True, True)
    assert client.read_short("ns=2;s=Temp") == (True, -5)
    ok, value = client.read_float("ns=2;s=Speed")
    assert ok is True and value is not None and abs(value - 3.14) < 1e-6
    assert client.read_string("ns=2;s=Batch") == (True, "A2024")


def test_read_type_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """读:返回类型不符 → ValueError(参数错误,不断线);空值 → (False, None)。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    fake.values["ns=2;s=Text"] = "abc"
    fake.values["ns=2;s=Empty"] = None
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    with pytest.raises(ValueError):
        client.read_float("ns=2;s=Text")  # 字符串节点按数值读:参数错误直接抛出
    assert client.connected is True  # 参数错误不触碰连接
    assert client.read_ushort("ns=2;s=Empty") == (False, None)  # 空值:设备侧条件
    assert client.last_error is not None and "节点值为空" in client.last_error
    assert client.connected is True  # 空值不断线、不重连


def test_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """写:值与 VariantType 成员名逐项断言。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.write_bool("ns=2;s=Run", True) is True
    assert client.write_ushort("ns=2;s=Speed", 1200) is True
    assert client.write_int("ns=2;s=Offset", -1) is True
    assert client.write_string("ns=2;s=Batch", "A2024") is True
    assert fake.written == [
        ("ns=2;s=Run", True, "Boolean"),
        ("ns=2;s=Speed", 1200, "UInt16"),
        ("ns=2;s=Offset", -1, "Int32"),
        ("ns=2;s=Batch", "A2024", "String"),
    ]


def test_write_range_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """写:整数越界/浮点超 float32 → ValueError(参数校验约定)。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    with pytest.raises(ValueError):
        client.write_short("ns=2;s=T", 32768)
    with pytest.raises(ValueError):
        client.write_uint("ns=2;s=T", -1)
    with pytest.raises(ValueError):
        client.write_float("ns=2;s=T", 1.0e300)
    assert fake.written == []


def test_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad 状态码(DeviceError)→ 失败但不断线。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    fake.read_errors["ns=2;s=Missing"] = DeviceError(
        "OPC-UA 出错 0x80350000:BadNodeIdUnknown", 0x80350000
    )
    fake.values["ns=2;s=Run"] = True
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.read_bool("ns=2;s=Missing") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "0x80350000" in client.last_error
    assert client.read_bool("ns=2;s=Run") == (True, True)


def test_connection_error_lazy_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接故障 → 标记断开;下一次读写自动重建会话(惰性重连)。"""
    client = OpcUaClient("127.0.0.1", 4840)
    sessions: list = []
    shared: dict = {"ns=2;s=Run": True}

    def create() -> FakeSession:
        fake = FakeSession()
        fake.values.update(shared)
        if len(sessions) == 0:
            fake.read_errors["ns=2;s=Run"] = ConnectionError("会话中断")
        sessions.append(fake)
        return fake

    monkeypatch.setattr(client, "_create_transport", create)
    client.connect()
    assert client.read_bool("ns=2;s=Run") == (False, None)
    assert client.connected is False
    assert client.read_bool("ns=2;s=Run") == (True, True)
    assert client.connected is True
    assert len(sessions) == 2 and sessions[1].connect_count == 1


def test_unsupported_data_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """地址参数非法直接抛 ValueError(参数校验约定)。"""
    client = OpcUaClient("127.0.0.1", 4840)
    monkeypatch.setattr(client, "_create_transport", lambda: FakeSession())
    client.connect()
    with pytest.raises(ValueError):
        client.read_ushort("D100")  # 不是 NodeId
    with pytest.raises(ValueError):
        client.read("ns=2;s=T", "not-a-type")


# ----------------------------------------------------------------------
# 批量读取(UA Read 单请求)
# ----------------------------------------------------------------------

def test_read_batch_and_read_many(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量读:read_batch/read_many 均为单次 UA Read 请求,值按序对应。"""
    client = OpcUaClient("127.0.0.1", 4840)
    fake = FakeSession()
    fake.values["ns=2;s=Run"] = True
    fake.values["ns=2;s=Temp"] = -5
    fake.values["ns=2;s=Offset"] = 7
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    calls: list = []
    orig = fake.read_values

    def traced(texts: list) -> list:
        calls.append(list(texts))
        return orig(texts)

    fake.read_values = traced
    assert client.read_batch([("ns=2;s=Run", "bool"), ("ns=2;s=Temp", "short")]) == (
        True,
        [True, -5],
    )
    assert calls == [["ns=2;s=Run", "ns=2;s=Temp"]]
    assert client.read_many(["ns=2;s=Temp", "ns=2;s=Offset"], "short") == [
        (True, -5),
        (True, 7),
    ]
    assert len(calls) == 2  # 每次调用各一帧


def test_read_batch_rejects() -> None:
    """read_batch 拒绝路径:空列表与非法数据类型。"""
    client = OpcUaClient("127.0.0.1", 4840)
    with pytest.raises(ValueError):
        client.read_batch([])
    with pytest.raises(ValueError):
        client.read_batch([("ns=2;s=T", "bad-type")])


# ----------------------------------------------------------------------
# asyncua 异常翻译(桩模块,不安装 asyncua 也可测)
# ----------------------------------------------------------------------

def test_translate_ua_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UaError → DeviceError(code = StatusCode);其余 → 内部异常。"""
    pkg_asyncua = types.ModuleType("asyncua")
    pkg_ua = types.ModuleType("asyncua.ua")
    mod_errors = types.ModuleType("asyncua.ua.uaerrors")

    class UaError(Exception):
        def __init__(self, code: int, text: str) -> None:
            super().__init__(text)
            self.code = code

    setattr(mod_errors, "UaError", UaError)
    setattr(pkg_ua, "uaerrors", mod_errors)
    setattr(pkg_asyncua, "ua", pkg_ua)
    monkeypatch.setitem(sys.modules, "asyncua", pkg_asyncua)
    monkeypatch.setitem(sys.modules, "asyncua.ua", pkg_ua)
    monkeypatch.setitem(sys.modules, "asyncua.ua.uaerrors", mod_errors)

    translated = _translate_ua_error(UaError(0x80350000, "BadNodeIdUnknown"))
    assert isinstance(translated, DeviceError)
    assert translated.code == 0x80350000
    assert "0x80350000" in str(translated)
    other = _translate_ua_error(ValueError("boom"))
    assert isinstance(other, OmniPLCInternalError)
    assert not isinstance(other, DeviceError)


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------

def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:单工作线程往返读写;endpoint 属性与同步实例一致。"""
    async def scenario() -> None:
        client = AOpcUaClient("127.0.0.1", 4840)
        sync = client._sync
        assert client.endpoint == "opc.tcp://127.0.0.1:4840"
        fake = FakeSession()
        fake.values["ns=2;s=Run"] = True
        monkeypatch.setattr(sync, "_create_transport", lambda: fake)
        assert await client.connect() is True
        assert await client.read_bool("ns=2;s=Run") == (True, True)
        assert await client.write_ushort("ns=2;s=Speed", 1200) is True
        assert fake.written == [("ns=2;s=Speed", 1200, "UInt16")]
        await client.close()

    asyncio.run(scenario())
