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
import threading as _threading
import time as _time
import types
from typing import Any

import pytest

from omniplc import OpcUaClient
from omniplc.aio import AOpcUaClient
from omniplc.core.errors import DeviceError, OmniPLCInternalError
from omniplc.opcua.address import parse_opcua_nodeid
from omniplc.opcua.client import (
    OpcUaSubscription,
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


# ----------------------------------------------------------------------
# v0.35 真实 OPC-UA 服务端测试(临时启停 asyncua.sync.Server)
# ----------------------------------------------------------------------

try:
    import asyncua.sync as _asyncua_sync  # type: ignore
    _HAVE_ASYNCUA = True
except ImportError:  # pragma: no cover
    _HAVE_ASYNCUA = False

if _HAVE_ASYNCUA:
    import socket as _socket
    _tmp_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _tmp_sock.bind(("127.0.0.1", 0))
    _OPCUA_TEST_PORT = _tmp_sock.getsockname()[1]
    _tmp_sock.close()
    _TEST_ENDPOINT = "opc.tcp://127.0.0.1:{}/test/".format(_OPCUA_TEST_PORT)

    @pytest.fixture(scope="module")
    def _opcua_server_module():
        """module 级 OPC-UA 服务端;只起一次,所有 v0.35 测试复用。"""
        server = _asyncua_sync.Server()
        server.set_endpoint(_TEST_ENDPOINT)
        server.start()
        idx = server.register_namespace("http://omniplc.test")
        try:
            yield server, idx
        finally:
            _stop_sync_server(server)

    def _stop_sync_server(server, timeout=15.0):
        """停 asyncua sync Server;stop 挂起时强停其事件循环线程(内部助手)。

        asyncua 1.1.5 的 ``sync.Server.stop()`` 偶发挂起(post 的 future
        120s 不完成,实测命中过一次),且其 ``tloop.stop()`` 不在 finally
        里——超时抛异常后非守护 ThreadLoop 线程泄漏,解释器退出被卡死。
        兜底:后台线程跑 stop,超时未归则 ``call_soon_threadsafe(loop.stop)``
        强停事件循环(run_forever 返回 → 线程自然终结,不拦进程退出)。
        """
        done = _threading.Event()

        def _run():
            try:
                server.stop()
            except Exception:
                pass
            done.set()

        t = _threading.Thread(target=_run, daemon=True)
        t.start()
        if not done.wait(timeout):
            print("!! asyncua server.stop() 挂起,已强停事件循环", file=sys.stderr)
            try:
                loop = server.tloop.loop
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        t.join(timeout=5.0)

    def _make_variable(server, idx, name, value=42):
        """在服务端 Objects 下加 Variable 节点,返回 (SyncNode, node_id 文本)。

        注意:服务端 add_variable 自动分配**数值 NodeId**(``ns=<idx>;i=<n>``),
        测试地址必须用返回节点的实际 NodeId,不能按 ``s=`` 字符串名猜。
        """
        node = server.nodes.objects.add_variable(idx, name, value)
        return node, str(node)

    @pytest.fixture
    def opcua_client(_opcua_server_module):
        server, idx = _opcua_server_module
        client = OpcUaClient(
            "127.0.0.1",
            _OPCUA_TEST_PORT,
            endpoint=_TEST_ENDPOINT,
        )
        client.connect()
        yield client
        client.disconnect()
else:  # pragma: no cover

    @pytest.fixture
    def opcua_client():
        pytest.skip("asyncua 未安装,跳过 v0.35 真实服务端测试")


# ----------------------------------------------------------------------
# v0.35 Browse
# ----------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_browse_root_top_level(opcua_client: OpcUaClient) -> None:
    """browse(Root, recursive=False):顶层枚举(Objects 节点应在其中)。"""
    ok, tree = opcua_client.browse("Root", recursive=False)
    assert ok is True
    assert isinstance(tree, dict)
    # Objects 节点(服务端必有)在树中,以 browse_name 标识
    names = [v["browse_name"] for v in tree.values()]
    assert "Objects" in names, (names, list(tree.keys()))
    for k, v in tree.items():
        assert "browse_name" in v
        assert "node_class" in v
        assert "children" in v
        # 顶层时 children 必为 None(非递归)
        assert v["children"] is None


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_browse_recursive_finds_variable(_opcua_server_module) -> None:
    """browse 递归能走到我添加的 Variable 节点。"""
    server, idx = _opcua_server_module
    name = "BrowseRecursive_{}".format(_time.time_ns())
    node, node_id = _make_variable(server, idx, name, 100)

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok, tree = client.browse("Root", recursive=True)
        assert ok is True
        # 在 Objects 节点递归子树中找到刚加的变量
        # tree[Root] -> children[Objects ns idx] -> ... -> 我们添加的变量
        # 简化路径:用扁平搜索
        flat = []
        def _flatten(d):
            for k, v in d.items():
                flat.append(k)
                if isinstance(v.get("children"), dict):
                    _flatten(v["children"])
        _flatten(tree)
        assert node_id in flat, "变量 {} 未在递归树中找到, 树:{}".format(node_id, flat[:10])
    finally:
        client.disconnect()


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_browse_max_depth_limits_recursion(_opcua_server_module) -> None:
    """max_depth 边界:depth=0 仅顶层。"""
    server, idx = _opcua_server_module
    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok, tree = client.browse("Root", recursive=True, max_depth=0)
        assert ok is True
        for k, v in tree.items():
            assert v["children"] is None  # max_depth=0 → 无下钻
    finally:
        client.disconnect()


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_browse_rejects_negative_max_depth(opcua_client: OpcUaClient) -> None:
    """max_depth 负数 → ValueError(参数校验)。"""
    with pytest.raises(ValueError):
        opcua_client.browse("Root", max_depth=-1)


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_browse_nonexistent_node_returns_empty_tree(opcua_client: OpcUaClient) -> None:
    """OPC-UA Browse 语义:不存在的节点不报错,返回空引用列表(与 Read 不同)。"""
    ok, tree = opcua_client.browse("ns=99;s=NoSuchNode")
    assert ok is True
    assert tree == {}


# ----------------------------------------------------------------------
# v0.35 Subscribe DataChange
# ----------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_subscribe_data_change_receives_update(_opcua_server_module) -> None:
    """订阅成功 + 服务端写入触发回调。"""
    server, idx = _opcua_server_module
    name = "SubDC_{}".format(_time.time_ns())
    node, node_id = _make_variable(server, idx, name, 0)

    received: list = []
    event = _threading.Event()

    def on_change(value, nid, ts):
        received.append((value, nid, ts))
        if value == 99:
            event.set()

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok, sub = client.subscribe_data_change(node_id, on_change, sampling_interval_ms=50)
        assert ok is True
        assert isinstance(sub, OpcUaSubscription)
        assert sub.node_id == node_id
        # 给订阅一点时间建立
        _time.sleep(0.2)
        # 服务端写入(直接用建节点时拿到的节点对象;裸名 get_child 匹配不上带 ns 前缀的 browse name)
        node.write_value(99)
        # 等待回调触发(<=2s)
        assert event.wait(timeout=3.0), "DataChange 回调未触发, received={}".format(received)
        # 检查收到的回调
        assert any(v == 99 for v, _, _ in received)
    finally:
        client.disconnect()


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_subscribe_data_change_unsubscribe_idempotent(_opcua_server_module) -> None:
    """unsubscribe 幂等:二次返回 False。"""
    server, idx = _opcua_server_module
    name = "SubUnsub_{}".format(_time.time_ns())
    _, node_id = _make_variable(server, idx, name, 0)

    received: list = []
    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok, sub = client.subscribe_data_change(node_id, lambda *a: received.append(a))
        assert ok is True
        assert sub.unsubscribe() is True
        assert sub.unsubscribe() is False  # 第二次幂等
    finally:
        client.disconnect()


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_subscribe_data_change_callback_exception_logged(_opcua_server_module) -> None:
    """用户回调抛异常 → last_error 记录,订阅继续(不杀订阅)。"""
    server, idx = _opcua_server_module
    name = "SubBoom_{}".format(_time.time_ns())
    node, node_id = _make_variable(server, idx, name, 0)

    boom_count = [0]

    def on_change(value, nid, ts):
        boom_count[0] += 1
        raise RuntimeError("user callback boom")

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok, sub = client.subscribe_data_change(node_id, on_change, sampling_interval_ms=50)
        assert ok is True
        _time.sleep(0.2)
        # 触发若干次值变化(直接用建节点时拿到的节点对象)
        node.write_value(1)
        _time.sleep(0.2)
        node.write_value(2)
        _time.sleep(0.3)
        # 回调被调用了至少 1 次(异常吞掉不挂订阅)
        assert boom_count[0] >= 1
        # last_error 应记了回调异常(UNKNOWN,不杀订阅)
        assert client.last_error is not None
        assert "回调" in (client.last_error or "")
        # 订阅句柄未消亡
        assert sub.unsubscribe() is True
    finally:
        client.disconnect()


# ----------------------------------------------------------------------
# v0.35 Lifecycle
# ----------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_disconnect_clears_active_subscriptions(_opcua_server_module) -> None:
    """disconnect() 清空活跃订阅(服务端资源释放)。"""
    server, idx = _opcua_server_module
    name = "SubDisconnect_{}".format(_time.time_ns())
    _, node_id = _make_variable(server, idx, name, 0)

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    ok, _ = client.subscribe_data_change(node_id, lambda *a: None)
    assert ok is True
    assert len(client.active_subscriptions) == 1
    client.disconnect()
    # disconnect 后 active_subscriptions 快照应为空
    assert client.active_subscriptions == {}


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_multiple_subscribes_same_node_get_separate_handles(_opcua_server_module) -> None:
    """同一节点多次订阅 → 多个独立 handle。"""
    server, idx = _opcua_server_module
    name = "SubMulti_{}".format(_time.time_ns())
    _, node_id = _make_variable(server, idx, name, 0)

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    try:
        ok1, sub1 = client.subscribe_data_change(node_id, lambda *a: None)
        ok2, sub2 = client.subscribe_data_change(node_id, lambda *a: None)
        assert ok1 and ok2
        assert sub1 is not sub2
        assert len(client.active_subscriptions) == 2
        # 各 unsubscribe 独立
        assert sub1.unsubscribe() is True
        assert sub2.unsubscribe() is True
    finally:
        client.disconnect()


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_reconnect_does_not_auto_resubscribe(_opcua_server_module) -> None:
    """断开重连后订阅句柄不再 active(不自动重订)。"""
    server, idx = _opcua_server_module
    name = "SubReconnect_{}".format(_time.time_ns())
    _, node_id = _make_variable(server, idx, name, 0)

    client = OpcUaClient(
        "127.0.0.1",
        _OPCUA_TEST_PORT,
        endpoint=_TEST_ENDPOINT,
    )
    client.connect()
    ok, sub = client.subscribe_data_change(node_id, lambda *a: None)
    assert ok is True
    sub_id_before = sub.subscription_id
    client.disconnect()
    # 重连
    client.connect()
    try:
        # 订阅表空,不自动重订
        assert client.active_subscriptions == {}
        # 旧句柄 unsubscribe 静默返回 False
        assert sub.unsubscribe() is False
        assert sub.subscription_id == sub_id_before  # ID 不变,只是服务端已无
    finally:
        # 收尾必须断开:残留连接的 asyncua ThreadLoop 是非守护线程,
        # 会卡死解释器退出(pytest 跑完进程不结束)
        client.disconnect()


# ----------------------------------------------------------------------
# v0.35 aio 镜像
# ----------------------------------------------------------------------


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_aio_subscribe_data_change_callback_on_loop(_opcua_server_module) -> None:
    """aio subscribe_data_change:回调被 call_soon_threadsafe 桥接到 aio loop 线程。"""
    import asyncio as _asyncio
    server, idx = _opcua_server_module
    name = "AioDC_{}".format(_time.time_ns())
    node, node_id = _make_variable(server, idx, name, 0)

    async def scenario() -> None:
        aclient = AOpcUaClient(
            "127.0.0.1",
            _OPCUA_TEST_PORT,
            endpoint=_TEST_ENDPOINT,
        )
        await aclient.connect()
        loop = _asyncio.get_running_loop()
        loop_tid = _threading.get_ident()
        aev = _asyncio.Event()
        cb_tids: list = []

        def on_change(value, nid, ts):
            # 经 call_soon_threadsafe 桥接 → 必在 aio loop 线程执行
            cb_tids.append(_threading.get_ident())
            aev.set()

        ok, sub = await aclient.subscribe_data_change(
            node_id, on_change, sampling_interval_ms=50
        )
        assert ok is True
        # 等 loop(不用 time.sleep 堵 loop 线程,否则桥接回调无法执行)
        await _asyncio.sleep(0.2)
        # 服务端写入放 executor,同样不阻塞 loop
        await loop.run_in_executor(None, node.write_value, 7)
        await _asyncio.wait_for(aev.wait(), timeout=5.0)
        assert cb_tids, "回调未被调用"
        assert cb_tids[0] == loop_tid, "回调未在 aio loop 线程执行"
        assert sub.unsubscribe() is True
        # close 收尾(disconnect 只断同步侧;close 才释放工作线程,
        # py3.9+ executor 线程非守护,不关会卡死解释器退出)
        await aclient.close()

    _asyncio.run(scenario())


@pytest.mark.skipif(not _HAVE_ASYNCUA, reason="需 asyncua")
def test_aio_browse_returns_dict() -> None:
    """aio browse:返回嵌套 dict。"""
    import asyncio as _asyncio

    async def scenario() -> None:
        aclient = AOpcUaClient(
            "127.0.0.1",
            _OPCUA_TEST_PORT,
            endpoint=_TEST_ENDPOINT,
        )
        await aclient.connect()
        ok, tree = await aclient.browse("Root", recursive=False)
        assert ok is True
        assert isinstance(tree, dict)
        assert "Objects" in [v["browse_name"] for v in tree.values()]
        # close 收尾(理由同上:释放 aio 工作线程)
        await aclient.close()

    _asyncio.run(scenario())
