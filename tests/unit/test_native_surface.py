"""原生异步层守卫:公开面与同步层的对应关系、命名空间卫生、协程性。

把"首批能力面"从口头承诺变成门禁:

- 同步基类的公开面**要么**已镜像,**要么**在下面显式声明的"未到批次"表里
  (新增同步公开面忘了同步 → 红;补齐后忘了从表里删除 → 也红)
- 原生客户端**不进** ``omniplc.__all__``——根包的镜像守卫按 ``A<名字>`` 找
  ``omniplc.aio`` 镜像,原生命名是 ``Async<名字>``,混进根导出会破坏该守卫
- 原生侧的方法必须是协程(防手误漏 ``async``),签名与同步孪生一致
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import omniplc as pkg
import omniplc.native as native

_SYNC_BASE_PENDING: set = {"create_monitor"}
"""同步基类里**尚未**进入原生层的公开面(create_monitor 监视器为同步线程形态,
原生 asyncio task 版后置单独立项——设计闭版 2026-10-02,落地后从表里删除)。"""

_MODBUS_PENDING: set = {"create_monitor"}
"""同步 Modbus TCP 里尚未进入原生层的公开面(监视器经基类继承,原生版后置)。"""

_MELSEC_PENDING: set = {"create_monitor"}
"""同步 MC 客户端里尚未进入原生层的公开面(监视器经基类继承,原生版后置)。"""

_FINS_PENDING: set = {"create_monitor"}
"""同步 FINS 客户端里尚未进入原生层的公开面(监视器经基类继承,原生版后置)。"""

_INOVANCE_PENDING: set = {"create_monitor"}
"""同步汇川客户端里尚未进入原生层的公开面(监视器经基类继承,原生版后置)。"""

_S7_PENDING: set = {"create_monitor"}
"""同步 S7 客户端里尚未进入原生层的公开面(监视器经基类继承,原生版后置)。"""

_TWIN_PAIRS = [
    ("ModbusTcpClient", "AsyncModbusTcpClient"),
    ("MelsecMcTcpClient", "AsyncMelsecMcTcpClient"),
    ("MelsecMcUdpClient", "AsyncMelsecMcUdpClient"),
    ("OmronFinsTcpClient", "AsyncOmronFinsTcpClient"),
    ("OmronFinsUdpClient", "AsyncOmronFinsUdpClient"),
    ("InovanceTcpClient", "AsyncInovanceTcpClient"),
    ("InovanceMcTcpClient", "AsyncInovanceMcTcpClient"),
    ("SiemensS7Client", "AsyncSiemensS7Client"),
]
"""八对同步/原生孪生客户端(汇川两对与全网签名守卫共用)。"""


def _public(cls: type) -> set:
    """类上的公开名(方法/属性,排除下划线内部)。"""
    return {name for name in dir(cls) if not name.startswith("_")}


def test_native_all_names_importable() -> None:
    """``omniplc.native.__all__`` 每个名字都真实存在。"""
    for name in native.__all__:
        assert getattr(native, name, None) is not None, "原生包导出缺失:{}".format(name)


def test_native_clients_stay_out_of_root_namespace() -> None:
    """原生客户端不进根包 ``__all__``(否则破坏 aio 镜像守卫)。"""
    stray = [name for name in pkg.__all__ if name.startswith("Async")]
    assert not stray, "根包 __all__ 不应含原生客户端:{}".format(stray)
    assert "native" not in pkg.__all__, "根包不导出 native 子包(避免 __all__ 守卫误判)"


def test_sync_base_surface_mirrored_or_pending() -> None:
    """同步基类公开面 = 原生已镜像面 ∪ 声明的未到批次表(不多不少)。"""
    gap = _public(pkg.BaseClient) - _public(native.AsyncBaseClient)
    assert gap == _SYNC_BASE_PENDING, (
        "原生基类与同步基类的公开面差异变了:实际 {},声明 {}".format(
            sorted(gap), sorted(_SYNC_BASE_PENDING)
        )
    )


def test_modbus_client_surface_mirrored_or_pending() -> None:
    """Modbus TCP 客户端公开面 = 原生已镜像面 ∪ 声明的未到批次表。"""
    gap = _public(pkg.ModbusTcpClient) - _public(native.AsyncModbusTcpClient)
    assert gap == _MODBUS_PENDING, (
        "原生 Modbus 客户端与同步版的公开面差异变了:实际 {},声明 {}".format(
            sorted(gap), sorted(_MODBUS_PENDING)
        )
    )


def test_melsec_clients_surface_mirrored_or_pending() -> None:
    """MC 客户端(TCP/UDP)公开面 = 原生已镜像面 ∪ 声明的未到批次表。"""
    for sync_cls, async_cls in (
        (pkg.MelsecMcTcpClient, native.AsyncMelsecMcTcpClient),
        (pkg.MelsecMcUdpClient, native.AsyncMelsecMcUdpClient),
    ):
        gap = _public(sync_cls) - _public(async_cls)
        assert gap == _MELSEC_PENDING, "{},实际 {}".format(
            sync_cls.__name__, sorted(gap)
        )


def test_fins_clients_surface_mirrored_or_pending() -> None:
    """FINS 客户端(TCP/UDP)公开面 = 原生已镜像面 ∪ 声明的未到批次表。"""
    for sync_cls, async_cls in (
        (pkg.OmronFinsTcpClient, native.AsyncOmronFinsTcpClient),
        (pkg.OmronFinsUdpClient, native.AsyncOmronFinsUdpClient),
    ):
        gap = _public(sync_cls) - _public(async_cls)
        assert gap == _FINS_PENDING, "{},实际 {}".format(sync_cls.__name__, sorted(gap))


def test_s7_client_surface_mirrored_or_pending() -> None:
    """S7 客户端公开面 = 原生已镜像面 ∪ 声明的未到批次表(会话型首个入原生层)。"""
    gap = _public(pkg.SiemensS7Client) - _public(native.AsyncSiemensS7Client)
    assert gap == _S7_PENDING, "S7,实际 {}".format(sorted(gap))


def test_inovance_clients_surface_mirrored_or_pending() -> None:
    """汇川客户端(Modbus TCP + MC 兼容)公开面 = 原生已镜像面 ∪ 未到批次表。

    汇川两对历史上不在守卫清单里(2026-10-05 异步对齐核查补上)——同步端
    后续加方法时原生侧不再静默漂移。
    """
    for sync_cls, async_cls in (
        (pkg.InovanceTcpClient, native.AsyncInovanceTcpClient),
        (pkg.InovanceMcTcpClient, native.AsyncInovanceMcTcpClient),
    ):
        gap = _public(sync_cls) - _public(async_cls)
        assert gap == _INOVANCE_PENDING, "{},实际 {}".format(
            sync_cls.__name__, sorted(gap)
        )


def test_melsec_native_supports_ethernet_frames_only() -> None:
    """原生 MC 覆盖以太网三种帧(1E/3E/4E);串口帧构造期显式拒绝(不留半成品)。"""
    for frame in ("1E", "3E", "4E"):
        assert (
            native.AsyncMelsecMcTcpClient("127.0.0.1", 2000, frame).frame.value == frame
        )
    for frame in ("3C", "4C", "1C"):
        with pytest.raises(ValueError):
            native.AsyncMelsecMcTcpClient("127.0.0.1", 2000, frame)


def test_native_async_methods_are_coroutines() -> None:
    """原生客户端公开异步方法必须都是协程函数(防手误漏 async)。

    传输层不在检查范围:``close`` / ``mark_synced`` 是**有意同步**的
    (取消路径不能再 ``await``),见 :mod:`omniplc.native.transport` 模块 docstring。
    """
    sync_passthrough = {"bind_tags"}  # 纯配置透传,与 aio 层同款豁免
    for name in native.__all__:
        cls = getattr(native, name)
        if not inspect.isclass(cls) or name.endswith("Transport"):
            continue
        for attr in dir(cls):
            if attr.startswith("_") or attr in sync_passthrough:
                continue
            member = inspect.getattr_static(cls, attr)
            if isinstance(member, (staticmethod, property)):
                continue
            func = getattr(member, "func", member)
            if inspect.isfunction(func):
                assert inspect.iscoroutinefunction(func), "{}.{} 不是协程".format(
                    name, attr
                )


def test_after_connect_failure_hook_mirrored() -> None:
    """失败清理钩子两层同名同义:同步侧普通函数、原生侧协程(漏一层 = 泄漏面重现)。"""
    assert not inspect.iscoroutinefunction(pkg.BaseClient._after_connect_failure)
    assert inspect.iscoroutinefunction(native.AsyncBaseClient._after_connect_failure)


def test_constructors_match_sync_twin() -> None:
    """8 对孪生客户端的构造签名(参数名 + 默认值)与同步侧逐一对齐。

    建成同型构造是"切换两层只改类名"的前提;``__init__`` 不在
    :func:`test_sync_base_surface_mirrored_or_pending` 的公开面扫描里
    (那是方法/属性集),故单独锁一道。
    """
    for sync_name, async_name in _TWIN_PAIRS:
        sync_sig = inspect.signature(getattr(pkg, sync_name).__init__)
        async_sig = inspect.signature(getattr(native, async_name).__init__)
        assert list(sync_sig.parameters) == list(async_sig.parameters), sync_name
        assert [p.default for p in sync_sig.parameters.values()] == [
            p.default for p in async_sig.parameters.values()
        ], sync_name


def test_method_signatures_match_sync_twin() -> None:
    """八对孪生客户端公开交集内**全部**同名方法的签名(参数名+默认值)逐一对齐。

    方法名集合守卫(``test_*_surface_mirrored_or_pending``)只锁"有这个名字",
    不锁形状——同步端给已有方法加参/改默认值而原生薄层没跟上时,切层即
    行为分裂(2026-10-05 异步对齐核查固化;此前只锁基类 15 个类型化方法,
    见 :func:`test_typed_signatures_match_sync_twin`)。property/静态方法
    不涉签名比对;基类同名方法(超类解析后的公共面)同样在本守卫射程内。
    """
    problems: list = []
    base_public = _public(pkg.BaseClient) & _public(native.AsyncBaseClient)
    for sync_name, async_name in _TWIN_PAIRS:
        s_cls = getattr(pkg, sync_name)
        a_cls = getattr(native, async_name)
        for name in sorted((_public(s_cls) & _public(a_cls)) - base_public):
            s_m = inspect.getattr_static(s_cls, name)
            a_m = inspect.getattr_static(a_cls, name)
            if isinstance(s_m, (staticmethod, classmethod, property)) or isinstance(
                a_m, (staticmethod, classmethod, property)
            ):
                continue
            if not (inspect.isfunction(s_m) and inspect.isfunction(a_m)):
                continue
            s_sig = inspect.signature(s_m)
            a_sig = inspect.signature(a_m)
            if list(s_sig.parameters) != list(a_sig.parameters):
                problems.append(
                    "{}.{} 参数名不符:同步 {} vs 原生 {}".format(
                        sync_name, name, list(s_sig.parameters), list(a_sig.parameters)
                    )
                )
            elif [p.default for p in s_sig.parameters.values()] != [
                p.default for p in a_sig.parameters.values()
            ]:
                problems.append("{}.{} 默认值不符".format(sync_name, name))
    assert not problems, "原生方法签名漂移:{}".format("; ".join(problems))


def test_cancelled_error_covers_asyncio_class() -> None:
    """取消异常口径必须覆盖 asyncio 的取消异常(3.8 起与 futures 版**不同类**)。

    3.7 里 ``asyncio.CancelledError is concurrent.futures.CancelledError`` 为真
    (别名,均为 ``Exception`` 子类);3.8 起 asyncio 的改为**内建**
    ``BaseException`` 子类、与 futures 版不再是同一个类——只捕后者会让任务取消与
    "超时取消内层任务"落地时的 ``CancelledError`` 全部漏网(native 超时/取消路径
    静默失效,CI 的 3.12 腿实测 7 例 FAILED)。本机 .venv 是 3.7.9,两条类天然
    同一,故这里改为断言"口径里含 asyncio 的那一条"。
    """
    from omniplc import aio as aio_module
    from omniplc.core import errors as errors_module
    from omniplc.native import base as base_module
    from omniplc.native import transport as transport_module

    assert asyncio.CancelledError in errors_module._CANCELLED_ERRORS
    # 三层用同一份口径(不是各自捕各自的类)
    assert base_module._CANCELLED_ERRORS is errors_module._CANCELLED_ERRORS
    assert transport_module._CANCELLED_ERRORS is errors_module._CANCELLED_ERRORS
    assert aio_module._CANCELLED_ERRORS is errors_module._CANCELLED_ERRORS


def test_typed_signatures_match_sync_twin() -> None:
    """类型化读写的方法签名(参数名与默认值)与同步基类逐一对齐。"""
    checked = [
        "read",
        "write",
        "read_short",
        "read_ushort",
        "read_int",
        "read_float",
        "read_double",
        "read_bool",
        "read_string",
        "write_bool",
        "write_short",
        "write_float",
        "write_string",
        "read_tag",
        "write_tag",
    ]
    for name in checked:
        sync_sig = inspect.signature(getattr(pkg.BaseClient, name))
        async_sig = inspect.signature(getattr(native.AsyncBaseClient, name))
        assert list(sync_sig.parameters) == list(async_sig.parameters), name
        assert [p.default for p in sync_sig.parameters.values()] == [
            p.default for p in async_sig.parameters.values()
        ], name


def test_write_bool_value_guard_matches_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_bool`` 的值守卫两侧同口径(手抄的守卫最容易单边漂移)。

    原生层的守卫是从同步基类抄过来的,review §7.7 的 P3-1 正是"只拒 str、
    放过了 5"这类单边收口不彻底 —— 这里把"同一批入参在两层得到同一结论"
    固化成守门用例。

    打桩在基类 ``write`` 入口、地址用 ``c0``(不带位号的 BOOL 写仅线圈区,
    b0cf2ab 依 V1.1b3 写区域收口):本用例只验值守卫——原实现放行后走真实
    链路,本机 502 有从站监听时未连接的惰性重连会真连真写,首次调用即绑定
    事件循环、第二次 ``asyncio.run`` 触发跨循环守卫(该用例因此环境敏感
    恒红,review-1019 实施批复核发现,随批改为纯守卫口径)。
    """
    allowed = [True, False, 1, 0]
    rejected = ["0", "false", 1.0, None, 2, -1, 5, 255]
    sync_client = pkg.ModbusTcpClient("127.0.0.1", 502, 1)
    async_client = native.AsyncModbusTcpClient("127.0.0.1", 502, 1)

    sync_calls: list = []
    async_calls: list = []

    def _sync_capture(address: str, data_type: object, value: object) -> bool:
        sync_calls.append((address, value))
        return True

    async def _async_capture(address: str, data_type: object, value: object) -> bool:
        async_calls.append((address, value))
        return True

    monkeypatch.setattr(sync_client, "write", _sync_capture)
    monkeypatch.setattr(async_client, "write", _async_capture)

    for value in allowed:
        # 值守卫放行 = 基类 write 恰被调用一次(链路已打桩,零网络)
        assert sync_client.write_bool("c0", value) is True
        assert asyncio.run(async_client.write_bool("c0", value)) is True
    assert sync_calls == [("c0", bool(value)) for value in allowed]
    assert async_calls == sync_calls  # bool(value) 归一两侧同型
    for value in rejected:
        with pytest.raises(ValueError):
            sync_client.write_bool("c0", value)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            asyncio.run(async_client.write_bool("c0", value))  # type: ignore[arg-type]
    # 拒绝路径不落 write:调用次数不增
    assert len(sync_calls) == len(allowed) and len(async_calls) == len(allowed)

    asyncio.run(async_client.close())


def test_typed_int_writes_reject_non_int_like_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 六个类型化整数写拒绝非 int(与同步 require_int 同口径,1020 P1-5)。

    旧实现 ``int(value)`` 静默收窄:int(1.9)→1、int(True)→1、int("3")→3,
    双栈同调用两侧行为分叉,native 侧静默错值写。打桩在基类 ``write``
    入口(零网络,与 write_bool 守卫用例同口径)。
    """
    names = [
        "write_short",
        "write_ushort",
        "write_int",
        "write_uint",
        "write_long",
        "write_ulong",
    ]
    sync_client = pkg.ModbusTcpClient("127.0.0.1", 502, 1)
    async_client = native.AsyncModbusTcpClient("127.0.0.1", 502, 1)
    sync_calls: list = []
    async_calls: list = []

    def _sync_capture(address: str, data_type: object, value: object) -> bool:
        sync_calls.append(value)
        return True

    async def _async_capture(address: str, data_type: object, value: object) -> bool:
        async_calls.append(value)
        return True

    monkeypatch.setattr(sync_client, "write", _sync_capture)
    monkeypatch.setattr(async_client, "write", _async_capture)

    for name in names:
        assert getattr(sync_client, name)("hr0", 3) is True
        assert asyncio.run(getattr(async_client, name)("hr0", 3)) is True
        for bad in (1.9, True, "3", None):
            with pytest.raises(ValueError):
                getattr(sync_client, name)("hr0", bad)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                asyncio.run(getattr(async_client, name)("hr0", bad))  # type: ignore[arg-type]
    assert len(sync_calls) == len(names) == len(async_calls)  # 拒绝零下发

    asyncio.run(async_client.close())


def test_retries_setters_reject_non_int_like_sync() -> None:
    """retries/write_retries setter 拒绝非 int(与同步 require_int 同口径)。

    旧实现 ``int(count)`` 静默收窄(1.5→1、True→1);str 因 ``count < 0``
    先抛 TypeError,两侧结构相同故天然一致,不入断言。
    """
    for client in (
        pkg.ModbusTcpClient("127.0.0.1", 502, 1),
        native.AsyncModbusTcpClient("127.0.0.1", 502, 1),
    ):
        client.retries = 2
        client.write_retries = 1
        assert client.retries == 2 and client.write_retries == 1
        for bad in (1.5, True):
            with pytest.raises(ValueError):
                client.retries = bad  # type: ignore[assignment]
            with pytest.raises(ValueError):
                client.write_retries = bad  # type: ignore[assignment]
