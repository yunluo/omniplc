"""三菱 MC 协议地址解析(QnA 兼容 3E/4E 与 A 兼容 1E)。

支持的地址语法(不区分大小写):

=========  ===========================================
语法        含义
=========  ===========================================
``D100``    数据寄存器(字/双字访问)
``M10``     内部继电器(位访问)
``X1F``     输入继电器(位访问,3E/4E 十六进制、1E 八进制)
``Y40``     输出继电器(位访问,进制同 X)
``W100``    链接寄存器(字访问,十六进制)
``R100``    文件寄存器(A 兼容,十进制)
``Z0``      变址寄存器
``D100.3``  字软元件位访问(3E/4E 支持)
=========  ===========================================

编号按软元件码表的进制在编码层换算(:func:`.codec_qna.device_number`),
本模块只做语法拆分,保留数字原文。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple, Optional

_MC_ADDRESS_RE = re.compile(r"^([A-Za-z]{1,4})([0-9A-Fa-f]+)(?:\.(\d+))?$")


class McAddress(NamedTuple):
    """解析后的 MC 软元件地址。

    :ivar device: 软元件记号(大写),如 ``"D"``、``"M"``
    :ivar number: 编号数字原文(进制由软元件码表决定,如 ``"100"``、``"1F"``)
    :ivar bit: 位号(0~15,仅字软元件位访问时有值)
    """

    device: str
    number: str
    bit: Optional[int]


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=4096)
def parse_mc_address(address: str) -> McAddress:
    """解析 MC 软元件地址字符串。

    :param address: 地址,如 ``"D100"``、``"M10"``、``"D100.3"``
    :return: :class:`McAddress`
    :raises ValueError: 语法错误或位号越界
    """
    if not address or not address.strip():
        raise ValueError("MC 地址不能为空")
    match = _MC_ADDRESS_RE.match(address.strip())
    if match is None:
        raise ValueError(
            f"无法解析 MC 地址:{address!r}(示例:D100 / M10 / X1F / D100.3)"
        )
    bit = _parse_bit(match.group(3))
    return McAddress(device=match.group(1).upper(), number=match.group(2), bit=bit)


def _parse_bit(text: Optional[str]) -> Optional[int]:
    """位号解析与范围校验(内部函数)。"""
    if text is None:
        return None
    bit = int(text)
    if not 0 <= bit <= 15:
        raise ValueError(f"字软元件位号必须在 0~15 之间,收到:{bit}")
    return bit
