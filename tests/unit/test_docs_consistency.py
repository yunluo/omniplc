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


def test_readme_skill_examples_client_counts() -> None:
    """README/SKILL/examples 的「N 个同步客户端 / N 客户端」与导出面对拍。"""
    sync_n, _aio_n, _nc, _nc8 = _counts()
    for rel in ("README.md", "SKILL.md", "docs/examples.md"):
        text = _text(rel)
        for m in re.findall(r"(\d+) 个同步客户端", text):
            assert int(m) == sync_n, "{}: {} 个同步客户端".format(rel, m)
        for m in re.findall(r"(\d+) 个?客户端", text):
            assert int(m) == sync_n, "{}: {} 客户端".format(rel, m)


def test_architecture_layer_counts() -> None:
    """architecture 分层图/类清单的计数与导出面对拍。"""
    sync_n, aio_n, native_client_n, native_concrete_n = _counts()
    text = _text("docs/architecture.md")
    for m in re.findall(r"(\d+) 个同步 \+ (\d+) 个异步", text):
        assert int(m[0]) == sync_n and int(m[1]) == aio_n, "分层图计数漂移"
    for m in re.findall(r"(\d+) 个同步具体类 \+ (\d+) 个异步镜像类", text):
        assert int(m[0]) == sync_n and int(m[1]) == aio_n, "类清单计数漂移"
    for m in re.findall(r"覆盖五协议 (\d+) 个", text):
        assert int(m) == native_concrete_n, "原生异步具体客户端计数漂移"
    for m in re.findall(r"(\d+) 个原生异步", text):
        assert int(m) == native_client_n, "原生异步总数(含基类)计数漂移"


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
                assert node.func.id in exported, "examples 使用未导出类:{}".format(
                    node.func.id
                )


def test_protocol_dirs_all_indexed() -> None:
    """docs/protocol/ 每个子目录都在 README 目录结构中出现(P2-2 防孤儿)。"""
    pdir = ROOT / "docs" / "protocol"
    readme = _text("docs/protocol/README.md")
    subdirs = sorted(d.name for d in pdir.iterdir() if d.is_dir())
    assert subdirs
    for name in subdirs:
        assert name in readme, "protocol 子目录未在 README 索引:{}".format(name)


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
    assert not missing, "冒烟清单缺驱动:{}".format(sorted(missing))
