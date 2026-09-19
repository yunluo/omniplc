"""基恩士 KV Host Link ASCII 帧编解码。

帧格式(参考本地 KEYENCE KV Host Link 参考库,与官方手册一致):

- 命令帧:``"<命令> <参数...>\\r"``,纯 ASCII 可打印字符
- 响应帧:一行 ASCII 文本,以 CR/LF 结束
- 写命令成功应答为 ``OK``;失败应答为 ``E0``~``E9`` 单个出错代码
- 数据格式后缀跟在软元件名后:``.U``/``.S``(16 位)、``.D``/``.L``(32 位)、
  ``.H``(十六进制);位软元件无后缀
- 命令:``RD``(单点读)、``RDS``(连续读)、``WR``(单点写)、``WRS``(连续写)
"""
from __future__ import annotations

import re
from typing import List, Sequence

from ...core.constants import KV_ERROR_TEXT
from ...core.errors import DeviceError, ProtocolFrameError

_CR = b"\r"
_ERROR_RE = re.compile(r"^E[0-9]$")
_SPLIT_RE = re.compile(r"[ ,]+")

# 数据格式 → (令牌模式, 范围下限, 范围上限)
_TOKEN_RULES = {
    ".U": (r"\d+", 0, 0xFFFF),
    ".S": (r"[+-]?\d+", -0x8000, 0x7FFF),
    ".D": (r"\d+", 0, 0xFFFFFFFF),
    ".L": (r"[+-]?\d+", -0x80000000, 0x7FFFFFFF),
}
_VALUE_RULES = {
    ".U": (0, 0xFFFF),
    ".S": (-0x8000, 0x7FFF),
    ".D": (0, 0xFFFFFFFF),
    ".L": (-0x80000000, 0x7FFFFFFF),
}


def build_frame(body: str) -> bytes:
    """把命令体编码为命令帧(ASCII + CR 结束)。

    :raises ProtocolFrameError: 命令体为空、含控制字符或非 ASCII
    """
    if not body or not body.strip():
        raise ProtocolFrameError("KV Host Link 命令体不能为空")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in body):
        raise ProtocolFrameError("KV Host Link 命令体不能包含控制字符:{!r}".format(body))
    try:
        payload = body.strip().encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolFrameError("KV Host Link 命令体必须为 ASCII:{!r}".format(body)) from exc
    return payload + _CR


def build_read(device_text: str, count: int = 1) -> bytes:
    """构造单点(RD)或连续(RDS)读命令帧。"""
    if count == 1:
        return build_frame("RD {}".format(device_text))
    if count < 1:
        raise ValueError("连续读取点数必须大于 0,收到:{}".format(count))
    return build_frame("RDS {} {}".format(device_text, count))


def build_write(device_text: str, value_text: str) -> bytes:
    """构造单点写命令帧(WR)。"""
    return build_frame("WR {} {}".format(device_text, value_text))


def build_write_consecutive(device_text: str, values: Sequence[str]) -> bytes:
    """构造连续写命令帧(WRS)。"""
    if not values:
        raise ValueError("连续写入至少需要 1 个值")
    return build_frame(
        "WRS {} {} {}".format(device_text, len(values), " ".join(values))
    )


def parse_response(raw: bytes) -> str:
    """解码一行 ASCII 响应(去除结尾 CR/LF)。

    :raises ProtocolFrameError: 空响应或非 ASCII
    """
    if not raw:
        raise ProtocolFrameError("KV Host Link 响应为空")
    body = raw.rstrip(b"\r\n")
    if not body:
        raise ProtocolFrameError("KV Host Link 响应行无效:{!r}".format(raw))
    try:
        return body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolFrameError("KV Host Link 响应不是 ASCII:{!r}".format(raw)) from exc


def check_error_code(text: str) -> None:
    """检查响应是否为出错代码(E0~E9)。

    :raises DeviceError: 响应为出错代码,``code`` 携带原文
    """
    if _ERROR_RE.match(text):
        raise DeviceError(
            "KV Host Link 出错 {}:{}".format(
                text, KV_ERROR_TEXT.get(text, "未知错误,请查阅 KEYENCE 手册")
            ),
            int(text[1]),
        )


def split_tokens(text: str) -> List[str]:
    """把数据响应拆分为非空令牌(空格/逗号分隔)。"""
    return [token for token in _SPLIT_RE.split(text) if token]


def parse_bit_token(token: str) -> bool:
    """解析位令牌(``0``/``1``/``OFF``/``ON``)。"""
    normalized = token.strip().upper()
    if normalized in ("1", "ON"):
        return True
    if normalized in ("0", "OFF"):
        return False
    raise ProtocolFrameError("无效的位响应令牌:{!r}(应为 0/1 或 OFF/ON)".format(token))


def parse_word_token(token: str, data_format: str) -> int:
    """按数据格式解析数值令牌并做范围校验。"""
    normalized = token.strip()
    if data_format == ".H":
        if not re.fullmatch(r"[0-9A-Fa-f]{1,4}", normalized):
            raise ProtocolFrameError("无效的十六进制响应令牌:{!r}".format(token))
        return int(normalized, 16)
    rules = _TOKEN_RULES.get(data_format)
    if rules is None:
        raise ProtocolFrameError("不支持的数据格式:{!r}".format(data_format))
    pattern, low, high = rules
    if not re.fullmatch(pattern, normalized):
        raise ProtocolFrameError(
            "无效的数值响应令牌:{!r}(格式 {})".format(token, data_format)
        )
    value = int(normalized, 10)
    if not low <= value <= high:
        raise ProtocolFrameError(
            "数值响应令牌超出 {} 范围:{!r}".format(data_format, token)
        )
    return value


def format_value(value: int, data_format: str) -> str:
    """按数据格式把整数格式化为命令值文本。"""
    if data_format == ".H":
        return format(value, "X")
    return str(value)
