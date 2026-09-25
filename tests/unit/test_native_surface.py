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

_SYNC_BASE_PENDING = {"read_many", "write_many"}
"""同步基类里**尚未**进入原生首批的公开面(批量读写,后续批次对齐)。"""

_MODBUS_PENDING = {
    "read_batch",
    "read_device_id",
    "read_device_object",
    "read_many",
    "read_write_registers",
    "write_batch",
    "write_many",
    "write_mask_register",
}
"""同步 Modbus TCP 里尚未进入原生首批的公开面(批量/扩展功能码)。"""

_MELSEC_PENDING = {"read_batch", "read_many", "write_many"}
"""同步 MC 客户端里尚未进入原生首批的公开面(0406 多块批量读)。"""

_FINS_PENDING = {"read_batch", "read_many", "write_many"}
"""同步 FINS 客户端里尚未进入原生首批的公开面(0104 多存储区读)。"""


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
        assert gap == _FINS_PENDING, "{},实际 {}".format(
            sync_cls.__name__, sorted(gap)
        )


def test_melsec_native_supports_first_batch_frames_only() -> None:
    """原生 MC 首批 1E/3E:4E 与串口帧构造期显式拒绝(不留半成品)。"""
    for frame in ("1E", "3E"):
        assert native.AsyncMelsecMcTcpClient("127.0.0.1", 2000, frame).frame.value == frame
    for frame in ("4E", "3C", "4C", "1C"):
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
    """5 对孪生客户端的构造签名(参数名 + 默认值)与同步侧逐一对齐。

    建成同型构造是"切换两层只改类名"的前提;``__init__`` 不在
    :func:`test_sync_base_surface_mirrored_or_pending` 的公开面扫描里
    (那是方法/属性集),故单独锁一道。
    """
    pairs = [
        ("MelsecMcTcpClient", "AsyncMelsecMcTcpClient"),
        ("MelsecMcUdpClient", "AsyncMelsecMcUdpClient"),
        ("ModbusTcpClient", "AsyncModbusTcpClient"),
        ("OmronFinsTcpClient", "AsyncOmronFinsTcpClient"),
        ("OmronFinsUdpClient", "AsyncOmronFinsUdpClient"),
    ]
    for sync_name, async_name in pairs:
        sync_sig = inspect.signature(getattr(pkg, sync_name).__init__)
        async_sig = inspect.signature(getattr(native, async_name).__init__)
        assert list(sync_sig.parameters) == list(async_sig.parameters), sync_name
        assert [p.default for p in sync_sig.parameters.values()] == [
            p.default for p in async_sig.parameters.values()
        ], sync_name


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


def test_write_bool_value_guard_matches_sync() -> None:
    """``write_bool`` 的值守卫两侧同口径(手抄的守卫最容易单边漂移)。

    原生层的守卫是从同步基类抄过来的,review §7.7 的 P3-1 正是"只拒 str、
    放过了 5"这类单边收口不彻底 —— 这里把"同一批入参在两层得到同一结论"
    固化成守门用例。
    """
    allowed = [True, False, 1, 0]
    rejected = ["0", "false", 1.0, None, 2, -1, 5, 255]
    sync_client = pkg.ModbusTcpClient("127.0.0.1", 502, 1)
    async_client = native.AsyncModbusTcpClient("127.0.0.1", 502, 1)

    for value in allowed:
        # 只验"未被守卫拒绝"(不真发报文:未连接时 write 返回 False)
        assert sync_client.write_bool("hr0", value) is False
        assert asyncio.run(async_client.write_bool("hr0", value)) is False
    for value in rejected:
        with pytest.raises(ValueError):
            sync_client.write_bool("hr0", value)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            asyncio.run(async_client.write_bool("hr0", value))  # type: ignore[arg-type]

    asyncio.run(async_client.close())


def test_stats_uses_shared_client_stats_type() -> None:
    """原生 ``stats`` 与同步层共用同一份 ``ClientStats`` 契约(键集一致)。"""
    from omniplc.core.base_client import ClientStats

    client = native.AsyncModbusTcpClient("127.0.0.1", 1, 1)
    assert set(client.stats) == set(ClientStats.__annotations__)
