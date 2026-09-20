"""倍福 TwinCAT ADS 客户端测试:假会话注入验证全链路(无需安装 pyads)。

覆盖:

- NetId 组装(IP 拼 .1.1 后缀/显式覆盖/非法样本)
- 读写类型映射(DataType → PLCTYPE 成员名)与值收窄
- 写入范围校验(越界 ValueError);返回类型不符 ValueError 不断线
- ADSError(DeviceError)不断线;连接故障标记断开 + 惰性重连
- pyads 异常翻译(桩模块:ADSError → DeviceError;其余 → 内部异常)
- 空变量名拒绝;异步镜像往返
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from omniplc import BeckhoffAdsClient
from omniplc.aio import ABeckhoffAdsClient
from omniplc.core.constants import ADS_DEFAULT_ADS_PORT
from omniplc.core.errors import DeviceError, OmniPLCInternalError
from omniplc.plc.beckhoff.ads import (
    _AdsSession,
    _build_net_id,
    _translate_ads_error,
)


class FakeAdsSession(_AdsSession):
    """内存版会话:按变量名存值,可注入指定变量的读取异常。"""

    def __init__(self) -> None:
        super().__init__("192.168.0.10.1.1", 851)
        self.values: dict = {}
        self.written: list = []
        self.read_errors: dict = {}
        self.connect_count = 0

    def connect(self) -> None:
        self.connect_count += 1

    def read_by_name(self, address: str, plctype_name: str) -> Any:
        if address in self.read_errors:
            raise self.read_errors[address]
        return self.values[address]

    def write_by_name(self, address: str, value: Any, plctype_name: str) -> None:
        self.written.append((address, value, plctype_name))
        self.values[address] = value


# ----------------------------------------------------------------------
# 构造与 NetId
# ----------------------------------------------------------------------

def test_constructor_and_net_id() -> None:
    """默认 AMS 端口 851;NetId 默认 IP 拼 .1.1,可显式覆盖。"""
    client = BeckhoffAdsClient()
    assert client._ip_address == "192.168.0.10"
    assert client.ads_port == ADS_DEFAULT_ADS_PORT == 851
    assert client.net_id == "192.168.0.10.1.1"
    assert BeckhoffAdsClient("10.1.100.5").net_id == "10.1.100.5.1.1"
    assert BeckhoffAdsClient("127.0.0.1", net_id="10.1.100.5.7.42").net_id == \
        "10.1.100.5.7.42"


def test_net_id_validation() -> None:
    """NetId 校验:6 段 0~255 数字,非法样本拒绝。"""
    assert _build_net_id("127.0.0.1", " 1.2.3.4.5.6 ") == "1.2.3.4.5.6"
    for bad in ("1.2.3.4.5", "1.2.3.4.5.6.7", "1.2.3.4.5.a", "1.2.3.4.5.256", "x"):
        with pytest.raises(ValueError):
            _build_net_id("127.0.0.1", bad)
    with pytest.raises(ValueError):
        BeckhoffAdsClient(net_id="1.2.3.4.5")
    with pytest.raises(ValueError):
        BeckhoffAdsClient(ads_port=0)


# ----------------------------------------------------------------------
# 读写全链路(假会话)
# ----------------------------------------------------------------------

def test_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读:BOOL/SHORT/FLOAT/STRING 按类型收窄。"""
    client = BeckhoffAdsClient("127.0.0.1")
    fake = FakeAdsSession()
    fake.values["MAIN.bRun"] = True
    fake.values["MAIN.nTemp"] = -5
    fake.values["MAIN.fSpeed"] = 3.14
    fake.values["MAIN.sBatch"] = "A2024"
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.read_bool("MAIN.bRun") == (True, True)
    assert client.read_short("MAIN.nTemp") == (True, -5)
    ok, value = client.read_float("MAIN.fSpeed")
    assert ok is True and value is not None and abs(value - 3.14) < 1e-6
    assert client.read_string("MAIN.sBatch") == (True, "A2024")


def test_write_type_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """写:值与 PLCTYPE 成员名逐项断言(IEC INT = 16 位)。"""
    client = BeckhoffAdsClient("127.0.0.1")
    fake = FakeAdsSession()
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.write_bool("MAIN.bRun", True) is True
    assert client.write_short("MAIN.a", -1) is True
    assert client.write_ushort("MAIN.b", 1) is True
    assert client.write_int("MAIN.c", -1) is True
    assert client.write_uint("MAIN.d", 1) is True
    assert client.write_long("MAIN.e", -1) is True
    assert client.write_ulong("MAIN.f", 1) is True
    assert client.write_float("MAIN.g", 1.5) is True
    assert client.write_double("MAIN.h", 2.5) is True
    assert client.write_string("MAIN.s", "A2024") is True
    assert fake.written == [
        ("MAIN.bRun", True, "PLCTYPE_BOOL"),
        ("MAIN.a", -1, "PLCTYPE_INT"),
        ("MAIN.b", 1, "PLCTYPE_UINT"),
        ("MAIN.c", -1, "PLCTYPE_DINT"),
        ("MAIN.d", 1, "PLCTYPE_UDINT"),
        ("MAIN.e", -1, "PLCTYPE_LINT"),
        ("MAIN.f", 1, "PLCTYPE_ULINT"),
        ("MAIN.g", 1.5, "PLCTYPE_REAL"),
        ("MAIN.h", 2.5, "PLCTYPE_LREAL"),
        ("MAIN.s", "A2024", "PLCTYPE_STRING"),
    ]


def test_write_range_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """写:整数越界/浮点超 float32 → ValueError(参数校验约定)。"""
    client = BeckhoffAdsClient("127.0.0.1")
    fake = FakeAdsSession()
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    with pytest.raises(ValueError):
        client.write_short("MAIN.t", 32768)
    with pytest.raises(ValueError):
        client.write_uint("MAIN.t", -1)
    with pytest.raises(ValueError):
        client.write_float("MAIN.t", 1.0e300)
    assert fake.written == []


def test_read_type_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """读:返回类型不符 → ValueError(参数错误,不断线)。"""
    client = BeckhoffAdsClient("127.0.0.1")
    fake = FakeAdsSession()
    fake.values["MAIN.sText"] = "abc"
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    with pytest.raises(ValueError):
        client.read_float("MAIN.sText")
    assert client.connected is True


def test_empty_address_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """空变量名 → ValueError(参数错误,直接抛出)。"""
    client = BeckhoffAdsClient("127.0.0.1")
    monkeypatch.setattr(client, "_create_transport", lambda: FakeAdsSession())
    client.connect()
    with pytest.raises(ValueError):
        client.read_int("  ")
    with pytest.raises(ValueError):
        client.write_bool("", True)
    with pytest.raises(ValueError):
        client.read("MAIN.x", "not-a-type")


def test_ads_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADS 状态码错误(DeviceError)→ 失败但不断线。"""
    client = BeckhoffAdsClient("127.0.0.1")
    fake = FakeAdsSession()
    fake.read_errors["MAIN.missing"] = DeviceError(
        "ADS 出错 0x00000712:符号不存在", 0x712
    )
    fake.values["MAIN.bRun"] = True
    monkeypatch.setattr(client, "_create_transport", lambda: fake)
    client.connect()
    assert client.read_bool("MAIN.missing") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "0x00000712" in client.last_error
    assert client.read_bool("MAIN.bRun") == (True, True)


def test_connection_error_lazy_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接故障 → 标记断开;下一次读写自动重建会话(惰性重连)。"""
    client = BeckhoffAdsClient("127.0.0.1")
    sessions: list = []
    shared: dict = {"MAIN.bRun": True}

    def create() -> FakeAdsSession:
        fake = FakeAdsSession()
        fake.values.update(shared)
        if len(sessions) == 0:
            fake.read_errors["MAIN.bRun"] = ConnectionError("会话中断")
        sessions.append(fake)
        return fake

    monkeypatch.setattr(client, "_create_transport", create)
    client.connect()
    assert client.read_bool("MAIN.bRun") == (False, None)
    assert client.connected is False
    assert client.read_bool("MAIN.bRun") == (True, True)
    assert client.connected is True
    assert len(sessions) == 2 and sessions[1].connect_count == 1


# ----------------------------------------------------------------------
# pyads 异常翻译(桩模块,不安装 pyads/无 TcAdsDll 也可测)
# ----------------------------------------------------------------------

def _install_pyads_stub(monkeypatch: pytest.MonkeyPatch) -> type:
    """注入 pyads 桩模块,返回桩内 ADSError 类(测试脚手架)。"""
    pkg = types.ModuleType("pyads")

    class ADSError(Exception):
        def __init__(self, err_code: int, text: str) -> None:
            super().__init__(text)
            self.err_code = err_code

    setattr(pkg, "ADSError", ADSError)
    monkeypatch.setitem(sys.modules, "pyads", pkg)
    return ADSError


def test_translate_ads_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADSError → DeviceError(code = ADS 错误码);其余 → 内部异常。"""
    ads_error_class = _install_pyads_stub(monkeypatch)
    translated = _translate_ads_error(ads_error_class(0x712, "symbol not found"))
    assert isinstance(translated, DeviceError)
    assert translated.code == 0x712
    assert "0x00000712" in str(translated)
    other = _translate_ads_error(ValueError("boom"))
    assert isinstance(other, OmniPLCInternalError)
    assert not isinstance(other, DeviceError)


def test_session_connect_failure_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话连接失败:pyads Connection.open 异常 → OSError(断线重连口径)。"""
    pkg = types.ModuleType("pyads")

    class Connection:
        def __init__(self, net_id: str, port: int) -> None:
            assert net_id == "192.168.0.10.1.1" and port == 851

        def open(self) -> None:
            raise RuntimeError("no AMS router")

        def close(self) -> None:
            pass

    setattr(pkg, "Connection", Connection)
    monkeypatch.setitem(sys.modules, "pyads", pkg)
    session = _AdsSession("192.168.0.10.1.1", 851)
    with pytest.raises(OSError):
        session.connect()
    assert session._connection is None


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------

def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:单工作线程往返读写;net_id/ads_port 属性与同步实例一致。"""

    async def scenario() -> None:
        client = ABeckhoffAdsClient("127.0.0.1")
        assert client.net_id == "127.0.0.1.1.1"
        assert client.ads_port == 851
        fake = FakeAdsSession()
        fake.values["MAIN.nCounter"] = 42
        sync = client._sync
        monkeypatch.setattr(sync, "_create_transport", lambda: fake)
        assert await client.connect() is True
        assert await client.read_int("MAIN.nCounter") == (True, 42)
        assert await client.write_bool("MAIN.bStart", True) is True
        assert fake.written == [("MAIN.bStart", True, "PLCTYPE_BOOL")]
        await client.close()

    asyncio.run(scenario())
