"""报错文案语言开关测试:set_lang 校验、取词口径、导出面与默认语言契约。"""

from __future__ import annotations

from typing import Iterator

import pytest

import omniplc
from omniplc.core import i18n
from omniplc.core.i18n import SUPPORTED_LANGUAGES, _, current_language, set_lang


@pytest.fixture(autouse=True)
def _restore_language_state() -> Iterator[None]:
    """每个用例后切回默认中文,避免污染其他测试。"""
    yield
    set_lang("zh")


# ----------------------------------------------------------------------
# 开关与取词
# ----------------------------------------------------------------------


def test_default_language_is_zh() -> None:
    """契约:默认语言恒为中文——已依赖中文文案的应用零影响。"""
    assert current_language() == "zh"
    assert SUPPORTED_LANGUAGES == ("zh", "en")


def test_set_lang_toggle() -> None:
    """set_lang 开/关:全局标志同步切换。"""
    set_lang("en")
    assert current_language() == "en"
    set_lang("zh")
    assert current_language() == "zh"


def test_set_lang_rejects_unknown() -> None:
    """非法语言代码抛 ValueError,且语言保持切换前的值。"""
    with pytest.raises(ValueError, match="不支持的语言"):
        set_lang("ja")
    assert current_language() == "zh"


def test_gettext_zh_returns_verbatim() -> None:
    """中文(默认)原样返回:模板逐字节一致,零改写。"""
    assert _("不支持的 MC 软元件:{!r},支持:{}") == "不支持的 MC 软元件:{!r},支持:{}"


def test_gettext_en_hit() -> None:
    """英文:字典命中返回英文,未命中兜底中文原文(永不 KeyError)。"""
    set_lang("en")
    assert (
        _("不支持的语言:{!r},支持:{}") == "Unsupported language: {0!r}, supported: {1}"
    )
    assert _("字典未收录的模板 xyz") == "字典未收录的模板 xyz"


def test_gettext_zh_ignores_dictionary() -> None:
    """中文取词不查表:即使字典已收录也原样返回(键即中文原文)。"""
    set_lang("en")
    en = _("不支持的语言:{!r},支持:{}")
    set_lang("zh")
    assert _("不支持的语言:{!r},支持:{}") == "不支持的语言:{!r},支持:{}"
    assert en == "Unsupported language: {0!r}, supported: {1}"


def test_translation_placeholder_reorder() -> None:
    """英文模板可用显式编号重排语序:同一组 format 参数两种写法通用。"""
    # 中文源码侧:自动编号占位
    zh_text = _("不支持的语言:{!r},支持:{}").format("ja", "zh / en")
    # 英文翻译侧:{0}/{1} 显式编号(本库模板内字段顺序重排时不改调用点)
    set_lang("en")
    en_text = _("不支持的语言:{!r},支持:{}").format("ja", "zh / en")
    assert zh_text == "不支持的语言:'ja',支持:zh / en"
    assert en_text == "Unsupported language: 'ja', supported: zh / en"


def test_format_zh_autonumber_matches_en_explicit() -> None:
    """自动编号 {} 与显式编号 {0} 混用于同一调用点时 format 结果一致。"""
    template = i18n._TRANSLATIONS["不支持的语言:{!r},支持:{}"]
    assert (
        template.format("ja", "zh / en")
        == "Unsupported language: 'ja', supported: zh / en"
    )


def test_no_mixed_auto_manual_numbering_in_any_translation() -> None:
    """全表扫描:单条模板禁止自动编号({}/{!r})与手动编号({0}/{1!r})混用。

    ``str.format`` 同串混用两套编号必抛
    ``ValueError: cannot switch from automatic field numbering to manual
    field specification``——en 模式下该错误路径被格式化异常吞掉(真实报错
    反而出不来)。zh 键以 ``{}`` 自动编号书写,en 值如需重排语序必须**整条**
    改手动编号({0}/{1}/…)。
    """
    import re

    auto_spec = re.compile(r"\{\D[^}]*\}|\{\}")  # {} / {!r} / {:02X}(无编号)
    manual_spec = re.compile(r"\{\d")  # {0} / {1!r} / {2:02X}
    mixed = [
        key
        for key, value in i18n._TRANSLATIONS.items()
        if auto_spec.search(value) and manual_spec.search(value)
    ]
    assert mixed == [], f"自动/手动编号混用的模板:{mixed}"


@pytest.mark.parametrize(
    "key",
    [
        "随机写值超出 {} 字节无符号范围:{}={}",
        "C{}(32 位计数器)不支持位号后缀:{!r}(接点位访问请用 BOOL 读)",
    ],
)
def test_regressed_mixed_numbering_entries_format_in_en(key: str) -> None:
    """回归:曾混用编号的两条 en 模板在 en 模式下 format 正常(不再抛 ValueError)。"""
    set_lang("en")
    text = _(key).format("2", 5, "X10")
    assert isinstance(text, str)
    set_lang("zh")


def test_error_code_tables_fully_translated_in_en() -> None:  # noqa: C901
    """经 ``_()`` 取词的**全部码表**逐条断言 en 可译(review-1022 P1-3)。

    取词 fallback 查不到键时静默返回中文原文(v0.47.2 刚修过 FINS/CIP 两处
    漏包,用户无感知)。历史门禁硬编码 9 张表,而实际经 ``_()`` 取词的码表
    为 12 张(EZSocket/FOCAS/S7 三张从未进门禁,68 条漏收录)——现改为
    **AST 自动发现**:

    1. 全库扫描「Dict 字面量赋给**大写或下划线开头**名字」= 候选码表;
    2. 逐文件对 ``_(非字面量)`` 取词点解析实参基名(``表.get(...)`` /
       ``表[...]`` / 局部变量转发链),命中候选表的即判定"该表经取词";
    3. import 该表并递归取全部文本值,逐条断言 ``_(text) != text``(en 命中)。
    新增码表忘翻译、或新增取词点引用未收录表,本门禁立即失败。
    """
    import ast
    import importlib
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "omniplc"

    def _base_of(node: ast.AST) -> str | None:
        """取表达式基名:``表.get(...)`` / ``表[...]`` / 裸名字。"""
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            return node.value.id
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
        ):
            return node.func.value.id
        if isinstance(node, ast.Name):
            return node.id
        return None

    def _targets(node: ast.AST) -> list:
        if isinstance(node, ast.Assign):
            return [t for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target]
        return []

    src_files = [p for p in sorted(src_root.rglob("*.py")) if p.name != "i18n.py"]
    trees = {}
    for path in src_files:
        trees[path] = ast.parse(path.read_text(encoding="utf-8"))

    # 候选码表名:Dict 字面量赋给大写/下划线开头名字(全库口径)
    candidate_tables = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                node.value, ast.Dict
            ):
                for target in _targets(node):
                    if target.id.isupper() or target.id.startswith("_"):
                        candidate_tables.add(target.id)

    # 逐文件解析取词点 → 命中的表名 -> 所属模块文件
    tables_dir = {}
    for path, tree in trees.items():
        var_map = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                base = _base_of(node.value)
                if base:
                    for target in _targets(node):
                        if target.id != base:
                            var_map.setdefault(target.id, set()).add(base)

        def resolve(name: str, chain: tuple = (), _var_map: dict = var_map) -> set:
            out = set()
            if name in chain:
                return out
            if name in candidate_tables:
                out.add(name)
            for base in _var_map.get(name, ()):
                out |= resolve(base, chain + (name,))
            return out

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
            ):
                continue
            arg = node.args[0]
            if isinstance(arg, (ast.Constant, ast.Str)):
                continue  # 字面量模板由另一门禁覆盖
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                continue  # 助手函数形态(助手体内取词点独立入扫)
            base = _base_of(arg)
            if base is not None:
                for name in resolve(base):
                    tables_dir[name] = path

    assert len(tables_dir) >= 12, f"码表发现数异常:{sorted(tables_dir)}"

    def texts_of(value: object) -> list:
        if isinstance(value, dict):
            out: list = []
            for item in value.values():
                out.extend(texts_of(item))
            return out
        return [value]

    set_lang("en")
    try:
        missing = []
        for name, path in sorted(tables_dir.items()):
            rel = path.relative_to(src_root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            module = importlib.import_module("omniplc." + ".".join(parts))
            table = getattr(module, name)
            for text in texts_of(table):
                if _(text) == text:
                    missing.append(f"{name}:{str(text)[:40]}")
    finally:
        set_lang("zh")
    assert missing == [], f"码表 en 漏翻(键不在 _TRANSLATIONS):{missing}"


def test_no_chinese_fstring_raises_in_src() -> None:
    """扫描 src:含中文的裸 f-string raise 必须为 0(f-string 无法过 _() 取词)。

    f-string 在字符串拼好后才进入 raise,取词函数无从介入 → en 模式该报错
    恒为中文(v0.47.0 i18n 改造时漏网两处即此形态,见 review-0929 P2-9)。
    调用点须写"中文模板字面量包 `_()` + .format 填参"。近似口径:逐行匹配
    ``raise X(f"…"`` 且行内含中文字符(多行 f-string 由本门禁历史版本逐批
    补漏,新增代码以 CI 本门禁拦截)。
    """
    import re
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "omniplc"
    fstring_raise = re.compile(r"raise \w+\(f('|\")")
    chinese = re.compile(r"[\u4e00-\u9fff]")
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        with open(path, encoding="utf-8") as fp:
            for number, line in enumerate(fp, 1):
                if fstring_raise.search(line) and chinese.search(line):
                    offenders.append(f"{path.name}:{number}")
    assert offenders == [], f"含中文的 f-string raise(无法过 _()):{offenders}"


def test_all_raise_templates_in_translations() -> None:  # noqa: C901
    """扫描 src:所有 ``_("…")`` 字面量模板必须在 ``_TRANSLATIONS`` 有键
    (review-1002 P3:原 en 守卫只扫码表不扫 raise 文案——read_range/心跳
    批的 73 条新文案 en 模式全部兜底回中文即此盲区;AST 提取含 py3.7 的
    ``ast.Str`` 与 3.8+ 的 ``ast.Constant`` 双形态)。"""
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[2] / "src" / "omniplc"

    def _key_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Str) and isinstance(node.s, str):
            return node.s
        return None

    i18n_tree = ast.parse((src_root / "core" / "i18n.py").read_text(encoding="utf-8"))
    table = set()
    for node in ast.walk(i18n_tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                key = _key_of(key)
                if key is not None:
                    table.add(key)

    missing = []
    total = 0
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "i18n.py":
            # 有意排除取词模块自身(review-1004 P3-3 登记):i18n.py 是双语
            # 表的载体——本守卫扫的就是这张表的键集,扫自身会把表键当文案
            # 自比较,无信息量;若日后 i18n.py 内部出现真正的 raise 文案,
            # 需人工确认键已在表(盲区登记于此,勿当遗漏)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
            ):
                continue
            key = _key_of(node.args[0]) if node.args else None
            if key is None:
                continue  # 非字面量模板(拼接/变量)不经本门禁
            total += 1
            if key not in table:
                missing.append(f"{path.name}:{key[:40]}")
    assert total > 1000, f"AST 扫描零命中:提取逻辑失效(_() 调用共 {total})"
    assert missing == [], f"raise 文案漏入 _TRANSLATIONS(en 模式兜底中文):{missing}"


def test_en_mode_core_error_paths_output_english() -> None:
    """en 端到端抽查:core 两处曾裸文案的错误路径在 en 模式输出英文。"""
    from omniplc.core.types import DataType

    set_lang("en")
    try:
        with pytest.raises(ValueError) as exc_info:
            DataType.from_name("nope")
        assert exc_info.value.args[0].startswith("Unknown data type")

        # write_tag 的 scale/offset 守卫(与 sync 同一模板键)须在表内且 en 可格式化
        from omniplc.core import i18n as i18n_module

        zh_template = "点位 {!r} 的 scale/offset 必须为有限数:scale={!r}, offset={!r}"
        assert zh_template in i18n_module._TRANSLATIONS
        en_text = i18n_module._TRANSLATIONS[zh_template].format("t", float("inf"), 0.0)
        assert en_text.startswith("scale/offset of tag")
        assert "finite" in en_text
    finally:
        set_lang("zh")


# ----------------------------------------------------------------------
# en 端到端:真实 codec 解析路径的报错语言切换
# ----------------------------------------------------------------------


def test_en_end_to_end_codec_errors() -> None:
    """en 下真实 codec 抛错:外层模板与码表值均为英文;zh 恢复中文。"""
    from omniplc.core.errors import DeviceError
    from omniplc.plc.modbus import codec as mb
    from omniplc.plc.omron import codec as fins
    from omniplc.plc.panasonic import codec_mewtocol as mew

    # Modbus:异常响应(FC03 读 + 异常码 2 = ILLEGAL DATA ADDRESS)
    def mb_error() -> str:
        try:
            mb.parse_read_response(bytes([0x83, 0x02]), 0x03, 10)
        except DeviceError as exc:
            return str(exc)
        raise AssertionError("should raise")

    # FINS:构造读应答帧,结束码 0x2101(指定区域只读)
    def fins_error() -> str:
        req = fins._build_frame(
            0,
            1,
            0,
            0,
            1,
            0,
            0x01,
            0x0101,
            b"\x00\x00\x00\x00\x01\x82\x00\x00\x00\x01",
        )
        head = bytes([fins.FINS_ICF_RESPONSE, fins.FINS_RSV, fins.FINS_GCT]) + req[3:12]
        resp = head + (0x2101).to_bytes(2, "big") + req[12:]
        try:
            fins.parse_response(resp, req, 1, False, True)
        except DeviceError as exc:
            return str(exc)
        raise AssertionError("should raise")

    # MEWTOCOL:错误响应(BCC = 帧体各字节异或)。帧体用 **4 字符错误码**
    # (``2102``,错误码宽度待核:2 字符 vs 4 字符):取 `!` 后到 BCC 前的全长
    # 字符,查表降级到前 2 字符 ``21``(review-1025 §二 P1-D/§2.4)
    body = b"%01!2102"
    bcc = 0
    for byte in body:
        bcc ^= byte
    resp = body + f"{bcc:02X}".encode() + b"\r"

    def mew_error() -> str:
        try:
            mew.parse_response(resp, "01", "RC")
        except DeviceError as exc:
            return str(exc)
        raise AssertionError("should raise")

    set_lang("en")
    assert mb_error() == (
        "Modbus exception 0x02 (ILLEGAL DATA ADDRESS (address out of range))"
        " (request function code 0x03)"
    )
    assert fins_error() == "FINS end code 0x2101 (The specified area is read-only)"
    assert mew_error() == (
        "MEWTOCOL error code 2102: NACK error (remote unit not correctly"
        " recognized or data error)"
    )

    set_lang("zh")
    assert (
        mb_error()
        == "Modbus 异常码 0x02(ILLEGAL DATA ADDRESS(地址越界))(请求功能码 0x03)"
    )
    assert fins_error() == "FINS 结束码 0x2101(指定区域只读)"
    assert mew_error() == "MEWTOCOL 错误码 2102:NACK 错误(远程单元未正确识别或数据错误)"


# ----------------------------------------------------------------------
# 导出面
# ----------------------------------------------------------------------


def test_set_lang_exported_from_root() -> None:
    """根包导出面:omniplc.set_lang 可用(导出守卫测试会自动覆盖 __all__)。"""
    assert "set_lang" in omniplc.__all__
    assert omniplc.set_lang is set_lang
