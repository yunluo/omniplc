"""报错文案语言开关测试:set_language 校验、取词口径、导出面与默认语言契约。"""
from __future__ import annotations

from typing import Iterator

import pytest

import omniplc
from omniplc.core import i18n
from omniplc.core.i18n import SUPPORTED_LANGUAGES, _, current_language, set_language


@pytest.fixture(autouse=True)
def _restore_language_state() -> Iterator[None]:
    """每个用例后切回默认中文,避免污染其他测试。"""
    yield
    set_language("zh")


# ----------------------------------------------------------------------
# 开关与取词
# ----------------------------------------------------------------------

def test_default_language_is_zh() -> None:
    """契约:默认语言恒为中文——已依赖中文文案的应用零影响。"""
    assert current_language() == "zh"
    assert SUPPORTED_LANGUAGES == ("zh", "en")


def test_set_language_toggle() -> None:
    """set_language 开/关:全局标志同步切换。"""
    set_language("en")
    assert current_language() == "en"
    set_language("zh")
    assert current_language() == "zh"


def test_set_language_rejects_unknown() -> None:
    """非法语言代码抛 ValueError,且语言保持切换前的值。"""
    with pytest.raises(ValueError, match="不支持的语言"):
        set_language("ja")
    assert current_language() == "zh"


def test_gettext_zh_returns_verbatim() -> None:
    """中文(默认)原样返回:模板逐字节一致,零改写。"""
    assert _("不支持的 MC 软元件:{!r},支持:{}") == "不支持的 MC 软元件:{!r},支持:{}"


def test_gettext_en_hit() -> None:
    """英文:字典命中返回英文,未命中兜底中文原文(永不 KeyError)。"""
    set_language("en")
    assert _("不支持的语言:{!r},支持:{}") == "Unsupported language: {0!r}, supported: {1}"
    assert _("字典未收录的模板 xyz") == "字典未收录的模板 xyz"


def test_gettext_zh_ignores_dictionary() -> None:
    """中文取词不查表:即使字典已收录也原样返回(键即中文原文)。"""
    set_language("en")
    en = _("不支持的语言:{!r},支持:{}")
    set_language("zh")
    assert _("不支持的语言:{!r},支持:{}") == "不支持的语言:{!r},支持:{}"
    assert en == "Unsupported language: {0!r}, supported: {1}"


def test_translation_placeholder_reorder() -> None:
    """英文模板可用显式编号重排语序:同一组 format 参数两种写法通用。"""
    # 中文源码侧:自动编号占位
    zh_text = _("不支持的语言:{!r},支持:{}").format("ja", "zh / en")
    # 英文翻译侧:{0}/{1} 显式编号(本库模板内字段顺序重排时不改调用点)
    set_language("en")
    en_text = _("不支持的语言:{!r},支持:{}").format("ja", "zh / en")
    assert zh_text == "不支持的语言:'ja',支持:zh / en"
    assert en_text == "Unsupported language: 'ja', supported: zh / en"


def test_format_zh_autonumber_matches_en_explicit() -> None:
    """自动编号 {} 与显式编号 {0} 混用于同一调用点时 format 结果一致。"""
    template = i18n._TRANSLATIONS["不支持的语言:{!r},支持:{}"]
    assert template.format("ja", "zh / en") == "Unsupported language: 'ja', supported: zh / en"


# ----------------------------------------------------------------------
# 导出面
# ----------------------------------------------------------------------

def test_set_language_exported_from_root() -> None:
    """根包导出面:omniplc.set_language 可用(导出守卫测试会自动覆盖 __all__)。"""
    assert "set_language" in omniplc.__all__
    assert omniplc.set_language is set_language
