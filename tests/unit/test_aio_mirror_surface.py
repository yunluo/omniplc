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

import pytest

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


def test_constructors_match_sync_twin() -> None:
    """全部 A* 客户端的构造签名(参数名 + 默认值)与同步孪生逐一对齐。

    构造参数缺口会让该能力在异步面**不可达**(如 v0.41.0 的 ``xy_octal``
    未镜像,FX5U 八进制口径在 aio 上开不出来,``X10`` 按十六进制静默读错
    软元件);方法/属性面守卫(test_concrete_clients_mirror_sync_extensions)
    不扫 ``__init__``,此处单独锁死——与 native 面
    test_native_surface::test_constructors_match_sync_twin 同口径。
    """
    problems: list = []
    for name in pkg.__all__:
        if not name.endswith("Client") or name == "BaseClient":
            continue
        a_cls = getattr(aio, "A" + name)
        # 包装模式特例:AModbusBaseClient 构造收 sync_client(同步实例注入),
        # 非孪生构造;具体协议客户端(AModbusTcpClient 等)走孪生对表
        if a_cls is aio.AModbusBaseClient:
            continue
        sync_sig = inspect.signature(getattr(pkg, name).__init__)
        async_sig = inspect.signature(getattr(aio, "A" + name).__init__)
        if list(sync_sig.parameters) != list(async_sig.parameters):
            problems.append(
                "{} 构造参数名不符:同步 {} vs 异步 {}".format(
                    name,
                    list(sync_sig.parameters),
                    list(async_sig.parameters),
                )
            )
            continue
        if [p.default for p in sync_sig.parameters.values()] != [
            p.default for p in async_sig.parameters.values()
        ]:
            problems.append("{} 构造默认值不符".format(name))
    assert not problems, "异步构造签名缺口:{}".format("; ".join(problems))


def test_aenter_failure_closes_executor() -> None:
    """``__aenter__`` 失败先 close 再抛(第八轮 P2-9):executor 线程必须释放。

    ``async with`` 语义在 ``__aenter__`` 抛出时不调 ``__aexit__``——
    不主动收尾则线程池永不释放,``async with`` 循环重试线性积累常驻线程。
    """
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502)
        client._sync.connect = lambda: False  # type: ignore[method-assign]
        with pytest.raises(ConnectionError):
            await client.__aenter__()
        assert client._executor is None, "失败分支必须收尾释放 executor"

    asyncio.run(scenario())