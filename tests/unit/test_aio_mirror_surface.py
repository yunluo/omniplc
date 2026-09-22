"""异步镜像方法面守卫:同步客户端的每个公开扩展必须有异步镜像。

背景:异步镜像为逐驱动手写转发,历史上曾漏 AOpcUaClient.endpoint、
AModbusBaseClient.write_mask_register、AB/NJ 通用服务、MC 家族
read_batch 等(2026-09-22 全量内省审计补齐)。本测试把审计固化为
门禁:任一同步扩展方法/属性未镜像即失败。

约定:对比每对同步/异步客户端"类自身公开面 - 基类公开面",
基类面(connect/读写/重试等)由 ABaseClient 统一镜像,单独校验。
"""
from __future__ import annotations

import inspect

import omniplc as pkg
import omniplc.aio as aio


def _public(cls: type) -> set:
    """类上的公开名(方法/属性,排除下划线内部,内部函数)。"""
    return {name for name in dir(cls) if not name.startswith("_")}


def test_base_client_surface_mirrored() -> None:
    """基类面:BaseClient 公开名在 ABaseClient 全部可见(含 close)。"""
    missing = sorted(_public(pkg.BaseClient) - _public(aio.ABaseClient))
    assert not missing, "ABaseClient 缺少基类镜像:{}".format(missing)


def test_async_methods_are_coroutines() -> None:
    """A* 客户端全部 async 方法均为协程函数(防手误漏 async)。

    ``bind_tags`` 是标签表绑定的同步透传(纯配置,不涉 I/O),豁免。
    """
    sync_passthrough = {"bind_tags", "configure_serial"}
    for name in aio.__all__:
        cls = getattr(aio, name)
        for attr in dir(cls):
            if attr.startswith("_") or attr in sync_passthrough:
                continue
            member = inspect.getattr_static(cls, attr)
            if isinstance(member, staticmethod):
                continue
            func = getattr(member, "func", member)
            if inspect.isfunction(func):
                assert inspect.iscoroutinefunction(func), "{}.{} 不是协程".format(
                    name, attr
                )


def test_concrete_clients_mirror_sync_extensions() -> None:
    """每个同步客户端的公开扩展(方法/属性)必须有其异步镜像。"""
    base_s = _public(pkg.BaseClient)
    problems: list = []
    for name in pkg.__all__:
        if not name.endswith("Client"):
            continue
        s_cls = getattr(pkg, name)
        a_cls = getattr(aio, "A" + name)
        missing = sorted((_public(s_cls) - base_s) - _public(a_cls))
        if missing:
            problems.append("{} 缺:{}".format(name, missing))
    assert not problems, "异步镜像缺口:{}".format("; ".join(problems))