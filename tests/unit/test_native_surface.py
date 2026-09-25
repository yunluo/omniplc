"""原生异步层守卫:公开面与同步层的对应关系、命名空间卫生、协程性。

把"首批能力面"从口头承诺变成门禁:

- 同步基类的公开面**要么**已镜像,**要么**在下面显式声明的"未到批次"表里
  (新增同步公开面忘了同步 → 红;补齐后忘了从表里删除 → 也红)
- 原生客户端**不进** ``omniplc.__all__``——根包的镜像守卫按 ``A<名字>`` 找
  ``omniplc.aio`` 镜像,原生命名是 ``Async<名字>``,混进根导出会破坏该守卫
- 原生侧的方法必须是协程(防手误漏 ``async``),签名与同步孪生一致
"""
from __future__ import annotations

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


def test_stats_uses_shared_client_stats_type() -> None:
    """原生 ``stats`` 与同步层共用同一份 ``ClientStats`` 契约(键集一致)。"""
    from omniplc.core.base_client import ClientStats

    client = native.AsyncModbusTcpClient("127.0.0.1", 1, 1)
    assert set(client.stats) == set(ClientStats.__annotations__)
