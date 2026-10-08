"""FANUC FOCAS 客户端测试:假函数表全流程(不依赖真 DLL)+ sizeof 守卫。

结构体布局以 fwlib32.h 为权威(docs/protocol/fanuc/,行号引用见
focas.py 各定义):sizeof 期望值按 `#pragma pack(4)` + MAX_AXIS=32
手工推导写死,防 ctypes 字段漂移。假 DLL 在 Python 侧模拟 FOCAS
流程语义(allclibhndl3 → rdcncid → 读三件 → freelibhndl),操作
模块内**真实 ctypes 结构体**(海康 SDK 测试同款手法)。
"""

from __future__ import annotations

import ctypes
from typing import Any, Callable, Dict, List, Optional

import pytest

from omniplc import FanucFocasClient
from omniplc.cnc import focas
from omniplc.core.errors import OmniPLCInternalError

# ----------------------------------------------------------------------
# 假 DLL / 函数表(操作真实 ctypes 结构体)
# ----------------------------------------------------------------------

_HELD: List[Any] = []
"""保活柄:防 ctypes 指针目标被 GC(海康测试同款)。"""


class _FakeFn:
    """可挂 restype/argtypes 的假函数(调用转 Python 侧回调)。"""

    def __init__(self, name: str, impl: Callable[..., int]) -> None:
        self.name = name
        self.impl = impl
        self.restype: Any = None
        self.argtypes: Any = None
        self.calls: List[Any] = []

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return self.impl(*args)


class _FakeFocas:
    """Python 侧模拟 FOCAS 库语义:句柄表 + 逐函数行为。

    ``behaviors`` 可按函数名覆写默认行为(返回值或回调),供错误
    路径注入。
    """

    def __init__(self, behaviors: Optional[Dict[str, Any]] = None) -> None:
        self.behaviors = behaviors or {}
        self.calls: Dict[str, List[Any]] = {}
        self.handles: Dict[int, Dict[str, Any]] = {}
        self._next_handle = 1
        self.freed: List[int] = []
        # 预置数据(测试可改)
        self.sysinfo = {
            "addinfo": 2,
            "max_axis": 3,
            "cnc_type": b"16",
            "mt_type": b"M",
            "series": b"D34",
            "version": b"A02",
            "axes": b"3",
        }
        self.dynamic = {
            "alarm": 0,
            "prgnum": 1024,
            "prgmnum": 1024,
            "seqnum": 88,
            "actf": 1500,
            "acts": 3000,
            "absolute": [100, 200, 300],
            "machine": [0, 0, 0],
            "relative": [10, 20, 30],
            "distance": [-5, -6, -7],
        }
        self.status = {
            "hdck": 0,
            "tmmode": 0,
            "aut": 1,
            "run": 1,
            "motion": 1,
            "mstb": 2,
            "emergency": 0,
            "alarm": 0,
            "edit": 0,
            "warning": 0,
            "o3dchk": 0,
            "ext_opt": 0,
            "restart": 0,
        }
        self.cnc_id = (0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00)

    def _resolve(self, name: str, default: Callable[..., int]) -> _FakeFn:
        behavior = self.behaviors.get(name, default)
        impl = behavior if callable(behavior) else (lambda *a, _b=behavior: _b)

        def wrapper(*args: Any) -> int:
            self.calls.setdefault(name, []).append(args)
            return impl(*args)

        return _FakeFn(name, wrapper)

    def _record(self, name: str, args: Any) -> None:
        self.calls.setdefault(name, []).append(args)

    # ---- 连接生命周期(模拟 fwlib32 语义)----

    def _allclibhndl3(self, ip: bytes, port: int, timeout: int, out: Any) -> int:
        if ip == b"0.0.0.1":  # 测试注入的不可达地址
            return focas.EW_SOCKET
        handle = self._next_handle
        self._next_handle += 1
        out[0] = (
            handle  # POINTER(c_ushort) setitem 写真实内存(简单类型 [0] 返回 int,不能链 .value)
        )
        self.handles[handle] = {"ip": ip, "port": port, "timeout": timeout}
        return focas.EW_OK

    def _freelibhndl(self, handle: int) -> int:
        self.freed.append(handle)
        self.handles.pop(handle, None)
        return focas.EW_OK

    def _settimeout(self, handle: int, ms: int) -> int:
        if handle in self.handles:
            self.handles[handle]["timeout"] = ms
        return focas.EW_OK

    def _rdcncid(self, handle: int, out: Any) -> int:
        array = out[0]  # POINTER(c_ulong×4):out[0] 即数组本体
        for i, value in enumerate(self.cnc_id):
            array[i] = value
        return focas.EW_OK

    # ---- 只读三件(把预置数据填进真实 ctypes 结构体)----

    def _sysinfo(self, handle: int, out: Any) -> int:
        target = out[0]  # POINTER → 结构体实例解引用(POINTER 无属性转发)
        info = self.sysinfo
        target.addinfo = info["addinfo"]
        target.max_axis = info["max_axis"]
        target.cnc_type = info["cnc_type"]
        target.mt_type = info["mt_type"]
        target.series = info["series"]
        target.version = info["version"]
        target.axes = info["axes"]
        return self.behaviors.get("cnc_sysinfo", focas.EW_OK)

    def _rddynamic2(self, handle: int, axis: int, length: int, out: Any) -> int:
        target = out[0]
        axis = int(axis)  # 会话侧传 c_short 时归一(避免 list[c_short] 索引)
        dyn = self.dynamic
        target.axis = 3 if axis == focas.ALL_AXES else axis
        target.alarm = dyn["alarm"]
        target.prgnum = dyn["prgnum"]
        target.prgmnum = dyn["prgmnum"]
        target.seqnum = dyn["seqnum"]
        target.actf = dyn["actf"]
        target.acts = dyn["acts"]
        if axis == focas.ALL_AXES:
            for i, value in enumerate(dyn["absolute"]):
                target.pos.faxis.absolute[i] = value
            for i, value in enumerate(dyn["machine"]):
                target.pos.faxis.machine[i] = value
            for i, value in enumerate(dyn["relative"]):
                target.pos.faxis.relative[i] = value
            for i, value in enumerate(dyn["distance"]):
                target.pos.faxis.distance[i] = value
        else:
            target.pos.oaxis.absolute = dyn["absolute"][axis]
            target.pos.oaxis.machine = dyn["machine"][axis]
            target.pos.oaxis.relative = dyn["relative"][axis]
            target.pos.oaxis.distance = dyn["distance"][axis]
        return self.behaviors.get("cnc_rddynamic2", focas.EW_OK)

    def _statinfo2(self, handle: int, out: Any) -> int:
        target = out[0]
        for key, value in self.status.items():
            setattr(target, key, value)
        return self.behaviors.get("cnc_statinfo2", focas.EW_OK)

    def build_dll(self) -> Any:
        """组装假 DLL 对象(循环 setattr,海康 _FakeDll 同款)。"""

        class _Dll:
            pass

        dll = _Dll()
        mapping = {
            "cnc_allclibhndl3": self._allclibhndl3,
            "cnc_freelibhndl": self._freelibhndl,
            "cnc_settimeout": self._settimeout,
            "cnc_rdcncid": self._rdcncid,
            "cnc_sysinfo": self._sysinfo,
            "cnc_rddynamic2": self._rddynamic2,
            "cnc_statinfo2": self._statinfo2,
        }
        for name, impl in mapping.items():
            setattr(dll, name, self._resolve(name, impl))
        return dll


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeFocas:
    """注入假 DLL 工厂,返回假 FOCAS 实例(测试替换点 = _load_fwlib)。"""
    fake_focas = _FakeFocas()
    monkeypatch.setattr(
        focas, "_load_fwlib", lambda sdk_dir, dll_path: fake_focas.build_dll()
    )
    return fake_focas


def _make_client(**kwargs: Any) -> FanucFocasClient:
    return FanucFocasClient("192.168.1.10", **kwargs)


# ----------------------------------------------------------------------
# sizeof 守卫(fwlib32.h 布局手工推导,pack(4) + MAX_AXIS=32)
# ----------------------------------------------------------------------


def test_struct_size_matches_header() -> None:
    """sizeof 对拍:ODBSYS=18 / ODBDY2 前部 28+union 512 / ODBST2=26。

    逐结构体推导(均 pack(4)):
    - ODBSYS: short×2 + char[2]×3 + char[4]×2 = 4+6+8 = 18(fwlib32.h 2850~2858)
    - ODBDY2 前部: short×2 + long×6 = 4+24 = 28;union faxis =
      4×MAX_AXIS(32)×4B = 512,oaxis = 16 → union 512(457~481)
    - ODBST2: short×13 = 26(2928~2942)
    """
    assert ctypes.sizeof(focas._ODBSYS) == 18
    assert ctypes.sizeof(focas._ODBDY2) == 28 + 512
    assert ctypes.sizeof(focas._ODBDY2Axes) == 512
    assert ctypes.sizeof(focas._ODBDY2OneAxis) == 16
    assert ctypes.sizeof(focas._ODBST2) == 26
    # 关键字段名在位(布局漂移早发现)
    field_names = {name for name, _ in focas._ODBDY2._fields_}
    assert {"alarm", "prgnum", "prgmnum", "seqnum", "actf", "acts"} <= field_names
    assert {name for name, _ in focas._ODBST2._fields_} == {
        "hdck",
        "tmmode",
        "aut",
        "run",
        "motion",
        "mstb",
        "emergency",
        "alarm",
        "edit",
        "warning",
        "o3dchk",
        "ext_opt",
        "restart",
    }


def test_max_axis_matches_generic_dll() -> None:
    """尺寸宏取 Ethernet 通用构建值(系列宏全不定义 → #else 分支)。"""
    assert focas.MAX_AXIS == 32
    assert focas.MAX_SPINDLE == 8
    assert focas.ALL_AXES == -1


# ----------------------------------------------------------------------
# 生命周期
# ----------------------------------------------------------------------


def test_connect_reads_cnc_id(fake: _FakeFocas) -> None:
    """connect:加载 → allclibhndl3(8193)→ rdcncid 身份确认。"""
    client = _make_client(sdk_dir="C:/fwlib")
    assert client.connect() is True
    assert client.cnc_id == "11223344-55667788-99aabbcc-ddeeff00"
    handle = client._transport._handle  # type: ignore[attr-defined]
    assert fake.handles[handle]["port"] == 8193
    client.close()
    assert handle in fake.freed  # 句柄已释放
    assert client.cnc_id is None


def test_connect_rejects_unreachable(fake: _FakeFocas) -> None:
    """allclibhndl3 返回链路错误(EW_SOCKET)→ connect False,记 last_error。"""
    client = FanucFocasClient("0.0.0.1", sdk_dir="C:/fwlib")
    assert client.connect() is False
    assert client.connected is False
    assert "分配库句柄失败" in client.last_error


def test_connect_with_debug_enabled(fake: _FakeFocas) -> None:
    """set_debug(True) 时 connect 不因日志格式化失败。

    log_op 为 %-风格模板,旧实现 connect 末条用 {} 占位 → TypeError 被
    connect 捕获记「连接初始化失败」(review-1020 P1-1,绿着错同型:
    调试关闭时全部用例照绿)。
    """
    from omniplc.core.debug import set_debug

    set_debug(True)
    try:
        client = _make_client(sdk_dir="C:/fwlib")
        assert client.connect() is True
        assert client.connected is True
        assert client.last_error is None
        # 读三件在调试开启下同样走通(log_op 其余调用均为 %-风格)
        ok, _info = client.read_sysinfo()
        assert ok is True
        client.close()
    finally:
        set_debug(False)


def test_close_idempotent(fake: _FakeFocas) -> None:
    """close 幂等:句柄只释放一次,重复调用不报错。"""
    client = _make_client(sdk_dir="C:/fwlib")
    assert client.connect() is True
    handle = client._transport._handle  # type: ignore[attr-defined]
    client.close()
    client.close()
    assert fake.freed.count(handle) == 1


def test_constructor_requires_sdk() -> None:
    """构造期校验:sdk_dir 与 dll_path 二选一。"""
    with pytest.raises(ValueError, match="sdk_dir"):
        FanucFocasClient("192.168.1.10")


def test_constructor_rejects_bad_endpoint() -> None:
    with pytest.raises(ValueError):
        FanucFocasClient("", sdk_dir="C:/fwlib")
    with pytest.raises(ValueError):
        FanucFocasClient("192.168.1.10", 99999, sdk_dir="C:/fwlib")


def test_load_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台加载显式报不支持(平台判断在工厂内)。"""
    monkeypatch.setattr(focas.platform, "system", lambda: "Linux")
    with pytest.raises(OmniPLCInternalError, match="仅支持 Windows"):
        focas._load_fwlib("C:/fwlib", None)


def test_resolve_dll_missing(tmp_path: Any) -> None:
    """目录里没有 DLL → 明确报「未找到」。"""
    with pytest.raises(OmniPLCInternalError, match="未找到"):
        focas._resolve_dll(str(tmp_path), None)


def test_resolve_dll_explicit_path(tmp_path: Any) -> None:
    """dll_path 显式指定优先,不做位数子目录展开。"""
    target = tmp_path / "Fwlib32.dll"
    target.write_bytes(b"x")
    assert focas._resolve_dll(None, str(target)) == str(target)


def test_resolve_dll_casings(tmp_path: Any) -> None:
    """sdk_dir 根目录直接放 fwlib32.dll(小写)也能命中(物料常见形态)。

    Windows 文件系统不区分大小写,首选候选 `Fwlib32.dll` 即命中同一
    文件——按 normcase 比较指向。
    """
    import os

    target = tmp_path / "fwlib32.dll"
    target.write_bytes(b"x")
    resolved = focas._resolve_dll(str(tmp_path), None)
    assert os.path.normcase(resolved) == os.path.normcase(str(target))


# ----------------------------------------------------------------------
# 只读三件(全流程)
# ----------------------------------------------------------------------


def test_read_sysinfo(fake: _FakeFocas) -> None:
    """read_sysinfo:ODBSYS 各字段解出(定长 ASCII 去填充)。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, info = client.read_sysinfo()
    assert ok is True
    assert info is not None
    assert info.addinfo == 2
    assert info.max_axis == 3
    assert info.cnc_type == "16"
    assert info.mt_type == "M"
    assert info.series == "D34"
    assert info.version == "A02"
    assert info.axes == "3"
    client.close()


def test_read_dynamic_all_axes(fake: _FakeFocas) -> None:
    """read_dynamic(ALL_AXES):坐标四组按轴返回列表(长度 = 响应 axis)。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, dyn = client.read_dynamic()
    assert ok is True
    assert dyn is not None
    assert dyn.alarm == 0
    assert dyn.prgnum == 1024
    assert dyn.seqnum == 88
    assert dyn.actf == 1500
    assert dyn.acts == 3000
    assert dyn.absolute == [100, 200, 300]
    assert dyn.machine == [0, 0, 0]
    assert dyn.relative == [10, 20, 30]
    assert dyn.distance == [-5, -6, -7]
    # 组帧参数:axis=-1(ALL_AXES)、length=0
    call = fake.calls["cnc_rddynamic2"][-1]
    assert call[1] == focas.ALL_AXES
    client.close()


def test_read_dynamic_single_axis(fake: _FakeFocas) -> None:
    """read_dynamic(轴号):坐标为标量(oaxis 分支)。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, dyn = client.read_dynamic(2)
    assert ok is True
    assert dyn is not None
    assert dyn.absolute == 300
    assert dyn.distance == -7
    call = fake.calls["cnc_rddynamic2"][-1]
    assert call[1] == 2
    client.close()


def test_read_dynamic_rejects_bool_axis(fake: _FakeFocas) -> None:
    """axis 传 bool(整数子类)构造期拒(参数错误约定)。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    with pytest.raises(ValueError, match="axis"):
        client.read_dynamic(True)
    client.close()


def test_read_status(fake: _FakeFocas) -> None:
    """read_status:ODBST2 13 位命名展开。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, status = client.read_status()
    assert ok is True
    assert status is not None
    assert status.aut == 1
    assert status.run == 1
    assert status.mstb == 2
    assert status.emergency == 0
    assert status.restart == 0
    client.close()


def test_ping_uses_statinfo(fake: _FakeFocas) -> None:
    """探活 = cnc_statinfo2:ping() 走读状态且不断线。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    assert client.ping() is True
    assert "cnc_statinfo2" in fake.calls
    assert client.connected is True
    client.close()


# ----------------------------------------------------------------------
# 错误分流(_check_rc 三分:链路拆连 / 环境内部错误 / 设备错误)
# ----------------------------------------------------------------------


def test_device_error_keeps_connection(fake: _FakeFocas) -> None:
    """读侧功能错误(EW_DATA)→ DeviceError 不断线。"""
    fake.behaviors["cnc_rddynamic2"] = focas.EW_DATA
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, dyn = client.read_dynamic()
    assert (ok, dyn) == (False, None)
    assert client.last_error_code == focas.EW_DATA
    assert client.connected is True  # 不断线
    client.close()


def test_link_error_disconnects(fake: _FakeFocas) -> None:
    """读侧链路错误(EW_SOCKET)→ 拆连,事务返回 (False, None)。"""
    fake.behaviors["cnc_statinfo2"] = focas.EW_SOCKET
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, status = client.read_status()
    assert (ok, status) == (False, None)
    assert client.connected is False  # 已拆连(惰性重连接管)
    client.close()


def test_nodll_raises_internal(fake: _FakeFocas) -> None:
    """EW_NODLL → OmniPLCInternalError(环境问题)。"""
    fake.behaviors["cnc_sysinfo"] = focas.EW_NODLL
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, info = client.read_sysinfo()
    assert (ok, info) == (False, None)
    client.close()


def test_unknown_rc_text(fake: _FakeFocas) -> None:
    """未知返回码给未知文本兜底(不 KeyError)。"""
    fake.behaviors["cnc_statinfo2"] = 99
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, _status = client.read_status()
    assert ok is False
    assert "99" in client.last_error
    client.close()


def test_receive_timeout_applied(fake: _FakeFocas) -> None:
    """connect 后 receive_timeout 经 cnc_settimeout 下发,秒 → 毫秒换算。

    cnc_settimeout 形参单位是毫秒(fwlib32.h 15128 行;allclibhndl3 才是
    秒),旧实现直传秒值 7 → 7ms 超时风暴(review-1020 P1-3)。
    """
    client = _make_client(sdk_dir="C:/fwlib")
    client.receive_timeout = 7.0
    assert client.connect() is True
    handle = client._transport._handle  # type: ignore[attr-defined]
    assert fake.handles[handle]["timeout"] == 7000
    client.close()


# ----------------------------------------------------------------------
# 基类通用面边界
# ----------------------------------------------------------------------


def test_read_write_address_rejected(fake: _FakeFocas) -> None:
    """FOCAS 无通用地址读写:read/write 显式 DeviceError(只读数采)。"""
    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    ok, _value = client.read_ushort("anything")
    assert ok is False
    assert "read_sysinfo" in client.last_error
    assert client.write_bool("anything", True) is False
    assert "只读" in client.last_error
    client.close()


def test_session_send_recv_rejected(fake: _FakeFocas) -> None:
    """会话无字节流(同海康/MX 形态)。"""
    from omniplc.core.errors import TransportClosedError

    client = _make_client(sdk_dir="C:/fwlib")
    client.connect()
    session = client._transport  # type: ignore[attr-defined]
    with pytest.raises(TransportClosedError):
        session.send(b"x")
    with pytest.raises(TransportClosedError):
        session.recv(1)
    client.close()


# ----------------------------------------------------------------------
# aio 镜像
# ----------------------------------------------------------------------


def test_aio_mirror(fake: _FakeFocas) -> None:
    """aio 镜像:单工作线程完成建连 + 三件读取。"""
    import asyncio

    from omniplc.aio import AFanucFocasClient

    async def scenario() -> None:
        client = AFanucFocasClient("192.168.1.10", sdk_dir="C:/fwlib")
        assert await client.connect() is True
        ok, info = await client.read_sysinfo()
        assert ok is True and info is not None and info.series == "D34"
        ok, dyn = await client.read_dynamic()
        assert ok is True and dyn is not None and dyn.acts == 3000
        ok, status = await client.read_status()
        assert ok is True and status is not None and status.run == 1
        await client.close()

    asyncio.run(scenario())
