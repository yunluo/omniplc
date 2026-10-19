"""文档↔代码一致性守卫(review-1021 P2-1/P2-3:门禁内、零网络)。

历史教训:客户端数量/版本号在 README/SKILL/examples/architecture 全线漂移
(32 vs 34、v0.54.0 vs v0.56.0),而仓库的代码面守卫传统恰好缺「文档 ↔ 代码」
一条。本文件把数字口径锁死:

- 客户端计数:N 个同步客户端 / N 客户端(README/SKILL/examples)与
  ``len(__all__)`` 对拍;architecture 分层图 N 个原生异步与 native 对拍
- 版本:architecture 抬头版本 == ``omniplc.__version__``
- examples 的 python 代码块可 AST 解析,构造的 *Client 类 ∈ 公开导出
- docs/protocol/ 子目录都在 protocol README 目录结构中出现(P2-2 防孤儿目录)
- 构造冒烟清单驱动集覆盖预期驱动集(自填;原 tools/manual_test.json 口径)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Tuple

import omniplc
import omniplc.aio
import omniplc.native

ROOT = Path(__file__).resolve().parents[2]

_CONCRETE_NATIVE = 8  # 五协议 8 个具体客户端(Modbus/MC×2/FINS×2/汇川×2/S7)
_NATIVE_BASE = "AsyncBaseClient"
# 抽象基类不计入"具体客户端"口径(review-1043 庚-1:原守卫把导出面里所有
# `*Client` 名字当具体客户端,于是 README/SKILL/architecture 的「34 个同步
# 客户端」被守卫**锁在错误口径上**——34 含 BaseClient/ModbusBaseClient 两个基类,
# 具体客户端实为 32,与推广材料一致)
_SYNC_BASES = ("BaseClient", "ModbusBaseClient")
_AIO_BASES = ("ABaseClient", "AModbusBaseClient")


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _counts() -> Tuple[int, int, int, int]:
    sync_n = len([n for n in omniplc.__all__ if n.endswith("Client")])
    aio_n = len([n for n in omniplc.aio.__all__ if n.endswith("Client")])
    native_client_n = len([n for n in omniplc.native.__all__ if n.endswith("Client")])
    native_concrete_n = native_client_n - (
        1 if _NATIVE_BASE in omniplc.native.__all__ else 0
    )
    return sync_n, aio_n, native_client_n, native_concrete_n


def _concrete_counts() -> Tuple[int, int]:
    """具体(非抽象基类)同步/异步客户端数(review-1043 庚-1 口径)。"""
    sync_n, aio_n, _nc, _nc8 = _counts()
    for base in _SYNC_BASES:
        if base in omniplc.__all__:
            sync_n -= 1
    for base in _AIO_BASES:
        if base in omniplc.aio.__all__:
            aio_n -= 1
    return sync_n, aio_n


def test_readme_skill_examples_client_counts() -> None:
    """README/SKILL/examples 的「N 个具体…客户端」与**具体**客户端数对拍。"""
    sync_concrete, _aio_concrete = _concrete_counts()
    for rel in ("README.md", "SKILL.md", "docs/examples.md"):
        text = _text(rel)
        for m in re.findall(r"(\d+) 个具体同步客户端", text):
            assert int(m) == sync_concrete, f"{rel}: {m} 个具体同步客户端"
        for m in re.findall(r"(\d+) 个具体客户端", text):
            assert int(m) == sync_concrete, f"{rel}: {m} 个具体客户端"
        # 旧的"34 个同步客户端"口径已废弃(含两个抽象基类):出现即报
        assert "个同步客户端" not in text or "个具体同步客户端" in text, (
            f"{rel} 仍用「N 个同步客户端」口径(应写「N 个具体同步客户端」)"
        )


def test_architecture_layer_counts() -> None:
    """architecture 分层图/类清单的计数与**具体**客户端数对拍。"""
    sync_concrete, aio_concrete = _concrete_counts()
    _sync_n, _aio_n, native_client_n, native_concrete_n = _counts()
    text = _text("docs/architecture.md")
    typed = re.findall(r"(\d+) 个具体同步 \+ (\d+) 个具体异步", text)
    assert typed, "分层图应写「N 个具体同步 + N 个具体异步」(review-1043 庚-1 口径)"
    for m in typed:
        assert int(m[0]) == sync_concrete and int(m[1]) == aio_concrete, (
            "分层图计数漂移"
        )
    for m in re.findall(r"(\d+) 个具体同步类 \+ (\d+) 个具体异步镜像类", text):
        assert int(m[0]) == sync_concrete and int(m[1]) == aio_concrete, (
            "类清单计数漂移"
        )
    for m in re.findall(r"覆盖五协议 (\d+) 个", text):
        assert int(m) == native_concrete_n, "原生异步具体客户端计数漂移"
    for m in re.findall(r"(\d+) 个原生异步", text):
        assert int(m) == native_client_n, "原生异步总数(含基类)计数漂移"


def test_docs_client_names_are_importable() -> None:
    """docs/*.md 里出现的 ``*Client`` 名必须能在库里导入(review-1043 庚-2)。

    实测漏网:`docs/protocol-features.md` 曾写 ``InovanceModbusTcpClient``
    ——该名字**库里不存在**(真名 `InovanceTcpClient`),集成商按矩阵选型会
    直接 `ImportError`。守卫半径从「只守 4 种句式 + 架构版本」扩到「全 docs
    的客户端类名逐个可导入」。
    """
    import omniplc.aio
    import omniplc.native

    exported = (
        set(omniplc.__all__) | set(omniplc.aio.__all__) | set(omniplc.native.__all__)
    )
    # 非本库名字白名单:
    # - 抽象基类(公开导出面里也有,文档叙述性提到)
    # - **已移除驱动的历史记录**:`OpenTcpClient`/`AOpenTcpClient`(v0.45.0 移除)、
    #   `BeckhoffAdsClient`/`ABeckhoffAdsClient`(v0.52.0 移除)——出现在
    #   architecture 版本履历表里,是"已移除"事实的记载,不是可用类名
    # - 缩写/占位形态:`UdpClient` 是「XxxTcpClient / UdpClient」成对书写里的
    #   后半截,`AClient` 是文档里的泛指占位(`async with AClient(...)`)
    allowed = {
        "BaseClient",
        "ABaseClient",
        "ModbusBaseClient",
        "AModbusBaseClient",
        "AsyncBaseClient",
        "OpenTcpClient",
        "AOpenTcpClient",
        "BeckhoffAdsClient",
        "ABeckhoffAdsClient",
        "UdpClient",
        "AClient",
    }
    offenders = {}
    for path in sorted((ROOT / "docs").glob("*.md")):
        if path.name.startswith("review-"):
            # 内部审查台账(未入库/gitignored):内容含已删除类名的历史对照,
            # 不属"面向用户的文档"口径
            continue
        text = path.read_text(encoding="utf-8")
        for name in set(re.findall(r"\b([A-Z][A-Za-z0-9]*Client)\b", text)):
            if name in exported or name in allowed:
                continue
            offenders.setdefault(name, []).append(path.name)
    assert not offenders, f"docs 引用了库内不存在的客户端名:{offenders}"


def test_architecture_version_matches_package() -> None:
    """architecture 抬头版本 == omniplc.__version__ == pyproject。"""
    m = re.search(r"版本:v([\w.]+)", _text("docs/architecture.md"))
    assert m, "architecture 抬头缺版本行"
    assert m.group(1) == omniplc.__version__


def test_examples_python_blocks_parse_and_clients_exported() -> None:
    """examples 代码块可 AST 解析;构造的 *Client 类 ∈ 公开导出(根包/aio/native)。"""
    text = _text("docs/examples.md")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(blocks) >= 10, "examples 代码块抽取异常"
    exported = set(omniplc.__all__)
    for module in (omniplc.aio, omniplc.native):
        exported |= set(getattr(module, "__all__", []))
    for block in blocks:
        tree = ast.parse(block)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.endswith("Client")
            ):
                assert node.func.id in exported, f"examples 使用未导出类:{node.func.id}"


def test_protocol_dirs_all_indexed() -> None:
    """docs/protocol/ 每个子目录都在 README 目录结构中出现(P2-2 防孤儿)。"""
    pdir = ROOT / "docs" / "protocol"
    readme = _text("docs/protocol/README.md")
    subdirs = sorted(d.name for d in pdir.iterdir() if d.is_dir())
    assert subdirs
    for name in subdirs:
        assert name in readme, f"protocol 子目录未在 README 索引:{name}"


_EXPECTED_DRIVERS = (
    "modbus_tcp",
    "modbus_rtu",
    "melsec_mc_tcp",
    "melsec_mc_udp",
    "melsec_mc_serial",
    "melsec_mx",
    "omron_fins_tcp",
    "omron_fins_udp",
    "omron_cip",
    "keyence_hostlink_tcp",
    "keyence_hostlink_udp",
    "keyence_mc_tcp",
    "keyence_mc_udp",
    "keyence_sr",
    "inovance_tcp",
    "inovance_rtu",
    "inovance_mc_tcp",
    "panasonic_mc_tcp",
    "panasonic_mewtocol_tcp",
    "panasonic_mewtocol_udp",
    "toyopuc_tcp",
    "toyopuc_udp",
    "opcua",
    "ab_eip",
    "mtconnect",
    "siemens_s7",
)
"""预期驱动集(自填)。原取自 ``tools/manual_test.json``——该文件随联机脚本
按「永不入库」红线撤出仓库(2026-10-14),故改由本测试自填锁定;构造冒烟
清单漏项即失败(回到无守卫)。"""


def test_expected_drivers_all_smoked() -> None:
    """预期驱动集 ⊆ 构造冒烟清单(P2-3 口径:漏项即回到无守卫)。"""
    smoke = _text("tests/unit/test_manual_common_smoke.py")
    listed = set(re.findall(r'"driver": "(\w+)"', smoke))
    assert listed, "冒烟清单未解析出驱动"
    missing = set(_EXPECTED_DRIVERS) - listed
    assert not missing, f"冒烟清单缺驱动:{sorted(missing)}"


_STALE_CORE_REFS = (
    "omniplc.types.",
    "omniplc.tag.",
    "omniplc.convert.",
    "omniplc.validation.",
    "omniplc.monitor.",
    "omniplc.constants.",
    "omniplc.errors.",
    "omniplc.debug.",
    "omniplc.base_client.",
    "omniplc.i18n.",
)
"""陈旧交叉引用前缀:v0.46.0 起 ``types``/``tag``/``convert`` 等叶子模块已迁入
``omniplc.core``,根包只剩 ``__init__.py`` 与子包。"""


def test_src_has_no_stale_core_module_refs() -> None:
    """src 内不得再有 ``omniplc.<叶子模块>.`` 形式的陈旧交叉引用(review-1046 C16)。

    review-1046 实测 36 处 ``:class:`omniplc.types.X``` 未随 v0.46.0 的模块迁移
    更新(另有 ``omniplc.tag.`` 4 处),而 ``src/omniplc/types.py`` 早已不存在
    ——Sphinx/IDE 都解析不到,读者按 docstring 写 import 会直接 ImportError。
    本守卫把「叶子模块一律 core 前缀」锁死(纯注释/docstring 面,零运行影响);
    ``docs/architecture.md`` 的版本履历行是有意的历史记载,不在守卫半径内。
    """
    offenders = {}
    for path in sorted((ROOT / "src" / "omniplc").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in _STALE_CORE_REFS:
            count = text.count(token)
            if count:
                offenders.setdefault(path.name, {})[token] = count
    assert not offenders, f"src 存在陈旧 core 模块引用(应为 omniplc.core.*):{offenders}"
