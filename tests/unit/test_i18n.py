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
    assert _("不支持的语言:{!r},支持:{}") == "Unsupported language: {0!r}, supported: {1}"
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
    assert template.format("ja", "zh / en") == "Unsupported language: 'ja', supported: zh / en"


# ----------------------------------------------------------------------
# en 端到端:真实 codec 解析路径的报错语言切换
# ----------------------------------------------------------------------

def test_en_end_to_end_codec_errors() -> None:
    """en 下真实 codec 抛错:外层模板与码表值均为英文;zh 恢复中文。"""
    from omniplc.core.errors import DeviceError
    from omniplc.modbus import codec as mb
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
            0, 1, 0, 0, 1, 0, 0x01, 0x0101,
            b"\x00\x00\x00\x00\x01\x82\x00\x00\x00\x01",
        )
        head = bytes([fins.FINS_ICF_RESPONSE, fins.FINS_RSV, fins.FINS_GCT]) + req[3:12]
        resp = head + (0x2101).to_bytes(2, "big") + req[12:]
        try:
            fins.parse_response(resp, req, 1, False, True)
        except DeviceError as exc:
            return str(exc)
        raise AssertionError("should raise")

    # MEWTOCOL:错误响应(BCC = 帧体各字节异或)
    body = b"%01!2102"
    bcc = 0
    for byte in body:
        bcc ^= byte
    resp = body + "{:02X}".format(bcc).encode() + b"\r"

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
        "MEWTOCOL error code 21: NACK error (remote unit not correctly recognized"
        " or data error)"
    )

    set_lang("zh")
    assert mb_error() == "Modbus 异常码 0x02(ILLEGAL DATA ADDRESS(地址越界))(请求功能码 0x03)"
    assert fins_error() == "FINS 结束码 0x2101(指定区域只读)"
    assert mew_error() == "MEWTOCOL 错误码 21:NACK 错误(远程单元未正确识别或数据错误)"


# ----------------------------------------------------------------------
# 导出面
# ----------------------------------------------------------------------

def test_set_lang_exported_from_root() -> None:
    """根包导出面:omniplc.set_lang 可用(导出守卫测试会自动覆盖 __all__)。"""
    assert "set_lang" in omniplc.__all__
    assert omniplc.set_lang is set_lang
