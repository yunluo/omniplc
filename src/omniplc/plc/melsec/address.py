"""三菱 MC 协议地址解析(QnA 兼容 3E/4E 与 A 兼容 1E)。

支持的地址语法(不区分大小写):

=========  ===========================================
语法        含义
=========  ===========================================
``D100``    数据寄存器(字/双字访问)
``M10``     内部继电器(位访问)
``X20``     输入继电器(位访问,**八进制**编号,1E 帧下同样)
``Y40``     输出继电器(位访问,八进制)
``W100``    文件寄存器(字访问)
``R100``    文件寄存器(A 兼容)
``Z0``      变址寄存器
``D100.3``  字软元件位访问(3E/4E 支持)
=========  ===========================================

**当前为骨架**:进制规则(位软元件八进制/十进制因软元件而异)、
软元件码查表与范围校验将在下一阶段实现;本模块先提供语法拆分。
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

_MC_ADDRESS_RE = re.compile(r"^([A-Za-z]{1,4})(\d+)(?:\.(\d+))?$")


class McAddress(NamedTuple):
    """解析后的 MC 软元件地址。

    :ivar device: 软元件记号(大写),如 ``"D"``、``"M"``
    :ivar offset: 十进制偏移(八进制软元件的换算在编码层处理)
    :ivar bit: 位号(0~15,仅字软元件位访问时有值)
    """

    device: str
    offset: int
    bit: Optional[int]


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
            "无法解析 MC 地址:{!r}(示例:D100 / M10 / D100.3)".format(address)
        )
    device = match.group(1).upper()
    offset = int(match.group(2))
    bit: Optional[int] = None
    if match.group(3) is not None:
        bit = int(match.group(3))
        if not 0 <= bit <= 15:
            raise ValueError("字软元件位号必须在 0~15 之间,收到:{}".format(bit))
    return McAddress(device=device, offset=offset, bit=bit)
