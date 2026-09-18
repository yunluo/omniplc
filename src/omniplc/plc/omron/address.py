"""欧姆龙 FINS 地址解析。

支持的地址语法(不区分大小写):

===========  ===========================================
语法         含义
===========  ===========================================
``CIO0``     CIO 区(字访问);``CIO0.5`` 位访问
``W10``      工作区(W);``W10.3`` 位访问
``H20``      保持继电器区(H)
``A0``       辅助区(A)
``D100``     数据存储器(D,最常用)
``E0_100``   EM 区 bank 0 字 100(下划线分隔 bank 号)
===========  ===========================================

FINS 存储区码(memory code)查表在编码阶段使用,见 :mod:`.codec`。
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

_FINS_ADDRESS_RE = re.compile(r"^([A-Za-z]{1,4})(\d+)(?:\.(\d+))?$")


class FinsAddress(NamedTuple):
    """解析后的 FINS 地址。

    :ivar area: 存储区记号(大写),如 ``"D"``、``"CIO"``
    :ivar offset: 字地址
    :ivar bit: 位号(0~15,仅位访问时有值)
    """

    area: str
    offset: int
    bit: Optional[int]


def parse_fins_address(address: str) -> FinsAddress:
    """解析 FINS 地址字符串。

    :param address: 地址,如 ``"D100"``、``"CIO0.5"``
    :return: :class:`FinsAddress`
    :raises ValueError: 语法错误或位号越界
    """
    if not address or not address.strip():
        raise ValueError("FINS 地址不能为空")
    match = _FINS_ADDRESS_RE.match(address.strip())
    if match is None:
        raise ValueError(
            "无法解析 FINS 地址:{!r}(示例:D100 / CIO0.5 / E0_100)".format(address)
        )
    area = match.group(1).upper()
    offset = int(match.group(2))
    bit: Optional[int] = None
    if match.group(3) is not None:
        bit = int(match.group(3))
        if not 0 <= bit <= 15:
            raise ValueError("位号必须在 0~15 之间,收到:{}".format(bit))
    return FinsAddress(area=area, offset=offset, bit=bit)
