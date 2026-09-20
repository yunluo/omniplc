"""西门子 S7 客户端测试:假 snap7 Client 驱动真实解析/编解码/错误契约。

连接工厂 ``_new_client`` 以模块级函数隔离(同 MX Component 惯例),
测试替换为内存版假 Client:按区域维护字节数组,可注入读写错误与断连态。
"""
from __future__ import annotations

import struct
from typing import Optional

import pytest

from omniplc import SiemensS7Client
from omniplc.aio import ASiemensS7Client
from omniplc.plc.siemens import parse_s7_address
from omniplc.plc.siemens import client as s7_module

_AREA_I, _AREA_Q, _AREA_M, _AREA_DB = 0x81, 0x82, 0x83, 0x84


class FakeS7Client:
    """内存版 snap7 Client:区域字节数组存取,可注入错误与断连态。"""

    def __init__(self, lib_location: Optional[str] = None) -> None:
        self.lib_location = lib_location
        self.connected_flag = True
        self.connect_args: Optional[tuple] = None
        self.connect_error: Optional[RuntimeError] = None
        self.read_error: Optional[RuntimeError] = None
        self.mem: dict = {}
        self.destroyed = False

    def connect(self, address: str, rack: int, slot: int, tcpport: int = 102) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connect_args = (address, rack, slot, tcpport)

    def get_connected(self) -> bool:
        return self.connected_flag

    def read_area(self, area: int, db: int, start: int, size: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        buf = self.mem.setdefault((area, db), bytearray(4096))
        return bytes(buf[start:start + size])

    def write_area(self, area: int, db: int, start: int, data: bytearray) -> None:
        buf = self.mem.setdefault((area, db), bytearray(4096))
        buf[start:start + len(data)] = data

    def disconnect(self) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True

    def seed(self, area: int, db: int, offset: int, data: bytes) -> None:
        buf = self.mem.setdefault((area, db), bytearray(4096))
        buf[offset:offset + len(data)] = data

    def dump(self, area: int, db: int, offset: int, size: int) -> bytes:
        buf = self.mem.setdefault((area, db), bytearray(4096))
        return bytes(buf[offset:offset + size])


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """挂上假 Client 工厂,返回 (已连接客户端, 假实例)。"""
    fake = FakeS7Client()
    monkeypatch.setattr(s7_module, "_new_client", lambda dll_path: fake)
    client = SiemensS7Client("127.0.0.1", rack=0, slot=1)
    assert client.connect() is True
    assert fake.connect_args == ("127.0.0.1", 0, 1, 102)
    return client, fake


# ----------------------------------------------------------------------
# 地址解析
# ----------------------------------------------------------------------

class TestParseS7Address:
    """S7 地址语法。"""

    def test_db_forms(self) -> None:
        assert parse_s7_address("DB1.DBX0.3") == ("DB", 1, 0, 3)
        assert parse_s7_address("db2.dbb4") == ("DB", 2, 4, None)
        assert parse_s7_address("DB3.DBW6") == ("DB", 3, 6, None)
        assert parse_s7_address("DB4.DBD8") == ("DB", 4, 8, None)
        assert parse_s7_address("DB5.DBS20") == ("DB", 5, 20, None)

    def test_area_forms(self) -> None:
        assert parse_s7_address("M10.2") == ("M", 0, 10, 2)
        assert parse_s7_address("mw10") == ("M", 0, 10, None)
        assert parse_s7_address("I0.0") == ("I", 0, 0, 0)
        assert parse_s7_address("IW64") == ("I", 0, 64, None)
        assert parse_s7_address("Q0.1") == ("Q", 0, 0, 1)
        assert parse_s7_address("QW10") == ("Q", 0, 10, None)
        assert parse_s7_address("MD100") == ("M", 0, 100, None)

    @pytest.mark.parametrize(
        "bad",
        ["", "D100", "DB1.DBX0", "DB1.DBB4.2", "M10.8", "M", "DB1.DBS20.3", "XYZ"],
    )
    def test_invalid(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_s7_address(bad)


# ----------------------------------------------------------------------
# 构造与会话
# ----------------------------------------------------------------------

def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        SiemensS7Client("192.168.0.1", rack=8)
    with pytest.raises(ValueError):
        SiemensS7Client("192.168.0.1", slot=32)
    with pytest.raises(ValueError):
        SiemensS7Client("")
    client = SiemensS7Client("192.168.0.1", rack=0, slot=1)
    assert client.rack == 0 and client.slot == 1


def test_dll_load_failure_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """snap7 原生库加载失败 → 翻译为带 dll_path 出路的 OSError(走真实翻译路径)。"""
    import sys
    import types

    class _BoomClient:
        def __init__(self, lib_location: Optional[str] = None) -> None:
            raise OSError("[WinError 193] 不是有效的 Win32 应用程序")

    pkg = types.ModuleType("snap7")
    client_mod = types.ModuleType("snap7.client")
    setattr(client_mod, "Client", _BoomClient)
    setattr(pkg, "client", client_mod)
    monkeypatch.setitem(sys.modules, "snap7", pkg)
    monkeypatch.setitem(sys.modules, "snap7.client", client_mod)

    with pytest.raises(OSError) as exc_info:
        s7_module._new_client("")
    assert "dll_path" in str(exc_info.value)

    monkeypatch.setattr(
        s7_module, "_new_client", lambda dll_path: (_ for _ in ()).throw(OSError("x: dll_path 指定"))
    )
    client = SiemensS7Client("127.0.0.1")
    assert client.connect() is False
    assert client.last_error is not None and "dll_path" in client.last_error


def test_connect_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """CPU 拒绝连接 → connect() False + last_error。"""
    fake = FakeS7Client()
    fake.connect_error = RuntimeError("TCP : Connection refused")
    monkeypatch.setattr(s7_module, "_new_client", lambda dll_path: fake)
    client = SiemensS7Client("127.0.0.1")
    assert client.connect() is False
    assert client.last_error is not None and "连接失败" in client.last_error


# ----------------------------------------------------------------------
# 数值读写(大端)
# ----------------------------------------------------------------------

def test_read_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """读:按 DataType 尺寸大端解码;BOOL 提位。"""
    client, fake = _client(monkeypatch)
    fake.seed(_AREA_DB, 1, 6, struct.pack(">f", 12.5))       # DB1.DBD6
    fake.seed(_AREA_M, 0, 10, struct.pack(">H", 0x1234))     # MW10
    fake.seed(_AREA_M, 0, 0, bytes([0b00001000]))            # M0.3
    fake.seed(_AREA_DB, 2, 0, struct.pack(">q", -3))         # DB2.DBD0 8 字节
    assert client.read_float("DB1.DBD6") == (True, 12.5)
    assert client.read_ushort("MW10") == (True, 0x1234)
    assert client.read_short("MW10") == (True, 0x1234)
    assert client.read_bool("M0.3") == (True, True)
    assert client.read_long("DB2.DBD0") == (True, -3)
    fake.seed(_AREA_DB, 1, 16, struct.pack(">d", 12.5))      # DB1.DBD16(8 字节)
    ok, value = client.read_double("DB1.DBD16")
    assert ok is True and value is not None and abs(value - 12.5) < 1e-3


def test_read_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数错误:位地址读数值/数值地址读 BOOL/STRING 泛型读 → ValueError。"""
    client, _ = _client(monkeypatch)
    with pytest.raises(ValueError):
        client.read_ushort("M10.2")     # 位地址只能按 BOOL
    with pytest.raises(ValueError):
        client.read_bool("MW10")        # 字节地址需要位号
    with pytest.raises(ValueError):
        client.read("DB1.DBS20", "STRING")  # 泛型读不支持字符串(用 read_string)


def test_write_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """写:按 DataType 大端编码落内存;越界转 ValueError。"""
    client, fake = _client(monkeypatch)
    assert client.write_int("DB1.DBD20", -5) is True
    assert fake.dump(_AREA_DB, 1, 20, 4) == b"\xFF\xFF\xFF\xFB"
    assert client.write_float("DB1.DBD24", 3.5) is True
    assert fake.dump(_AREA_DB, 1, 24, 4) == struct.pack(">f", 3.5)
    with pytest.raises(ValueError):
        client.write_ushort("MW10", 70000)
    with pytest.raises(ValueError):
        client.write_float("DB1.DBD24", 1.0e300)


def test_bit_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """位写为锁内读-改-写:只动目标位,其余位保持。"""
    client, fake = _client(monkeypatch)
    fake.seed(_AREA_M, 0, 0, bytes([0b1010_0010]))
    assert client.write_bool("M0.3", True) is True
    assert fake.dump(_AREA_M, 0, 0, 1) == bytes([0b1010_1010])
    assert client.write_bool("M0.7", False) is True
    assert fake.dump(_AREA_M, 0, 0, 1) == bytes([0b0010_1010])
    assert client.read_bool("M0.3") == (True, True)
    assert client.read_bool("M0.7") == (True, False)


# ----------------------------------------------------------------------
# 字符串
# ----------------------------------------------------------------------

def test_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """S7 String:读按头部长度截取;写头部=实际长度。"""
    client, fake = _client(monkeypatch)
    fake.seed(_AREA_DB, 1, 20, bytes([80, 5]) + b"HELLO")
    assert client.read_string("DB1.DBS20", length=80) == (True, "HELLO")
    assert client.write_string("DB1.DBS20", "HI") is True
    assert fake.dump(_AREA_DB, 1, 20, 4) == bytes([2, 2, 72, 73])
    fake.seed(_AREA_DB, 1, 40, bytes([80, 0]))  # 空串(actual=0)
    assert client.read_string("DB1.DBS40") == (True, "")


# ----------------------------------------------------------------------
# 错误契约与惰性重连
# ----------------------------------------------------------------------

def test_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """在线态 RuntimeError → DeviceError 不断线。"""
    client, fake = _client(monkeypatch)
    fake.read_error = RuntimeError("CPU : object does not exist")
    ok, value = client.read_float("DB1.DBD6")
    assert ok is False and value is None
    assert client.last_error is not None and "S7 错误" in client.last_error
    assert client.connected is True


def test_link_down_lazy_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """断连态 RuntimeError → OSError 标记断线;下次读自动重建会话。"""
    first = FakeS7Client()
    second = FakeS7Client()
    created = [first, second]

    monkeypatch.setattr(
        s7_module, "_new_client", lambda dll_path: created.pop(0)
    )
    client = SiemensS7Client("127.0.0.1")
    assert client.connect() is True
    first.read_error = RuntimeError("TCP : connection reset")
    first.connected_flag = False
    assert client.read_float("DB1.DBD6") == (False, None)
    assert client.connected is False
    second.seed(_AREA_DB, 1, 6, struct.pack(">f", 12.5))
    assert client.read_float("DB1.DBD6") == (True, 12.5)
    assert client.connected is True
    assert first.destroyed is True


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------

def test_async_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:类型化读写经单工作线程驱动同步实例。"""
    import asyncio

    fake = FakeS7Client()
    monkeypatch.setattr(s7_module, "_new_client", lambda dll_path: fake)

    async def scenario() -> None:
        client = ASiemensS7Client("127.0.0.1", rack=0, slot=1)
        assert client.rack == 0 and client.slot == 1
        assert await client.connect() is True
        assert await client.write_float("DB1.DBD0", 3.5) is True
        assert await client.read_float("DB1.DBD0") == (True, 3.5)
        assert await client.write_bool("M0.1", True) is True
        assert await client.read_bool("M0.1") == (True, True)
        await client.disconnect()

    asyncio.run(scenario())
