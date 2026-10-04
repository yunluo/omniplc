"""包导出面测试:根包/aio 包 __all__ 卫生 + 版本号与 pyproject 同步。

守卫三条约定:

- 根包 ``__all__`` 里每个名字都可导入(防改名后残留死导出)
- ``omniplc.__version__`` 与 pyproject 版本一致(防两处漂移)
- ``omniplc.aio`` 星号导入面 == 其 ``__all__``(只有 A* 异步类;
  同步客户端/常量/内部助手只是实现依赖,不得进使用者命名空间)
"""

from __future__ import annotations

import re
from pathlib import Path

import omniplc
import omniplc.aio as aio_pkg

_ROOT = Path(__file__).resolve().parents[2]


def test_root_all_names_importable() -> None:
    """根包 __all__ 每个名字都真实存在(改名/删类后同步 __all__)。"""
    for name in omniplc.__all__:
        assert getattr(omniplc, name, None) is not None, "根包导出缺失:{}".format(name)


def test_version_matches_pyproject() -> None:
    """__version__ 与 pyproject version 一致(防版本号两处漂移)。"""
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "(.+?)"', text, re.MULTILINE)
    assert match is not None, "pyproject 缺 version 字段"
    assert omniplc.__version__ == match.group(1)


def test_description_covers_supported_protocols() -> None:
    """__description__ 与 pyproject 描述同步(不残留旧协议清单)。"""
    for token in ("Modbus", "三菱 MC", "FINS", "OPC-UA", "MTConnect", "S7"):
        assert token in omniplc.__description__, "__description__ 缺:{}".format(token)


def test_all_async_clients_have_mirror() -> None:
    """根包每个 *Client 客户端都有 A 前缀异步镜像,反之亦然。"""
    sync_clients = {name for name in omniplc.__all__ if name.endswith("Client")}
    mirror_names = {name[1:] for name in aio_pkg.__all__ if name[1:].endswith("Client")}
    assert sync_clients == mirror_names


def test_public_result_types_exported_at_root() -> None:
    """出现在公共 API 返回签名里的类型必须统一在根包可导入。

    口径(review-1005 §7.4「顶层导出统一」):客户端返回的句柄/记录类型
    与 Monitor 事件家族、读码器结果类型同级——AB 的 ``AbTagEntry``、
    欧姆龙的 ``FinsClock`` 已在根包,OPC-UA 的 ``OpcUaSubscription`` 原
    只能从 ``omniplc.plc.opcua`` 导入(标注返回类型要深路径),本用例
    把"返回类型 ⊆ 根包"锁成门禁。地址解析助手(parse_*/*Address)有意
    留子包,不入本清单。
    """
    required = (
        "OpcUaSubscription",
        "AbTagEntry",
        "FinsClock",
        "Monitor",
        "MonitorEvent",
        "MonitorQuality",
        "MonitorStats",
        "PointSnapshot",
        "HikrobotSdkFrame",
        "HikrobotSdkCode",
        "HikrobotSdkQuality",
        "HikrobotStatus",
        "FrameRecord",
    )
    missing = [name for name in required if name not in omniplc.__all__]
    assert not missing, "根包缺少公共返回类型导出:{}".format(missing)


def test_aio_star_import_surface() -> None:
    """aio 星号导入面 == __all__:同步客户端/常量/内部名不得公开。"""
    namespace: dict = {}
    exec("from omniplc.aio import *", namespace)  # noqa: S102 - 导出面测试本体
    exported = {key for key in namespace if not key.startswith("__")}
    assert exported == set(aio_pkg.__all__)
    # 卫生抽查:同步客户端与 stdlib 名不得借星号导入泄漏
    assert "ModbusTcpClient" not in exported
    assert "asyncio" not in exported
    assert "ThreadPoolExecutor" not in exported
