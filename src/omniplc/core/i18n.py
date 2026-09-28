"""全局报错文案语言开关:中文(默认)与英文双语文案,一行切换。

用法::

    import omniplc

    omniplc.set_language("en")  # 之后所有报错文案输出英文
    ...
    omniplc.set_language("zh")  # 切回中文(默认)

口径:

- 进程级开关,对所有客户端实例生效(与 :func:`omniplc.set_debug` 同款
  机制,同步与异步客户端共用;异步镜像在单工作线程内驱动同步实例);
- 默认中文,报错文本与历史版本逐字节一致——已依赖中文文案的应用零影响;
- 仅作用于**运行期报错文本**(异常消息、错误码→文案表);注释、docstring
  与 :mod:`omniplc.core.debug` 调试日志不参与翻译;
- 只影响**之后**产生的文案,已生成的 ``last_error`` / 异常对象不回溯改写;
- 取词以中文原文为键:当前语言为中文时原样返回(零查表开销);为英文时
  查双语字典,未收录的模板兜底返回中文原文(永不抛 KeyError)。

占位符与翻译语序:中文键保持源码文案原样(``{}`` 自动编号 / ``{name}`` /
``{!r}``),英文值可改用 ``{0}``/``{1}`` 显式编号重排语序——同一组
``format`` 参数,两种写法通用(单一模板内不要混用两种编号)。
"""
from __future__ import annotations

from typing import Dict, Tuple

SUPPORTED_LANGUAGES: Tuple[str, ...] = ("zh", "en")
"""支持的语言代码(传给 :func:`set_language` 的合法取值)。"""

_language = "zh"


def set_language(language: str) -> None:
    """设置全局报错文案语言(进程级开关,对所有客户端实例生效)。

    :param language: 语言代码,见 :data:`SUPPORTED_LANGUAGES`
        (``"zh"`` 中文 / ``"en"`` 英文)
    :raises ValueError: 传入不支持的语言代码(报错文案按切换前的语言输出)
    """
    global _language
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            _("不支持的语言:{!r},支持:{}").format(
                language, " / ".join(SUPPORTED_LANGUAGES)
            )
        )
    _language = language


def current_language() -> str:
    """当前报错文案语言(内部与测试使用)。"""
    return _language


# ----------------------------------------------------------------------
# 双语文案表:键 = 中文原文(与源码报错文本逐字节一致),值 = 英文文案。
# 按模块分块追加;英文模板可改用 {0}/{1} 显式编号重排语序,含字面
# 花括号时写 {{}}。
# ----------------------------------------------------------------------

_TRANSLATIONS: Dict[str, str] = {
    # ---- core/i18n ----
    "不支持的语言:{!r},支持:{}": "Unsupported language: {0!r}, supported: {1}",
}


def _(message: str) -> str:
    """取词:按当前语言返回报错文案模板(库内部使用)。

    中文(默认)原样返回——调用点零查表开销;英文查双语字典,未收录的
    模板兜底返回中文原文。调用点统一写法::

        raise ValueError(_("不支持的软元件:{!r}").format(device))

    即:模板字面量包进 :func:`_`,占位符由调用方 ``format`` 填充
    (报错路径低频,不做惰性格式化)。

    :param message: 中文文案模板(与源码报错文本逐字节一致)
    :return: 当前语言下的文案模板
    """
    if _language == "zh":
        return message
    return _TRANSLATIONS.get(message, message)
