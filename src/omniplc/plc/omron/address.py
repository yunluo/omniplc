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
from functools import lru_cache
from typing import NamedTuple, Optional

from ...core.constants import ADDRESS_CACHE_MAXSIZE, FINS_EM_BANK_MAX, MODBUS_REGISTER_BIT_MAX

_FINS_ADDRESS_RE = re.compile(r"^([A-Za-z]{1,4})(\d+)(?:\.(\d+))?$")
_FINS_EM_ADDRESS_RE = re.compile(r"^E(\d{1,2})_(\d+)(?:\.(\d+))?$")


class FinsAddress(NamedTuple):
    """解析后的 FINS 地址。

    :ivar area: 存储区记号(大写),如 ``"D"``、``"CIO"``、``"E"``
    :ivar offset: 字地址
    :ivar bit: 位号(0~15,仅位访问时有值)
    :ivar bank: EM 区 bank 号(仅 area 为 ``"E"`` 时非 0)
    """

    area: str
    offset: int
    bit: Optional[int]
    bank: int = 0


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)
def parse_fins_address(address: str) -> FinsAddress:
    """解析 FINS 地址字符串。

    :param address: 地址,如 ``"D100"``、``"CIO0.5"``、``"E0_100"``
    :return: :class:`FinsAddress`
    :raises ValueError: 语法错误或位号越界
    """
    if not address or not address.strip():
        raise ValueError("FINS 地址不能为空")
    text = address.strip()
    match = _FINS_EM_ADDRESS_RE.match(text)
    if match is not None:
        bank = int(match.group(1))
        if not 0 <= bank <= FINS_EM_BANK_MAX:
            raise ValueError(f"EM 区 bank 号必须在 0~{FINS_EM_BANK_MAX} 之间,收到:{bank}")
        return FinsAddress(area="E", offset=int(match.group(2)), bit=_parse_bit(match.group(3)), bank=bank)
    match = _FINS_ADDRESS_RE.match(text)
    if match is None:
        raise ValueError(
            f"无法解析 FINS 地址:{address!r}(示例:D100 / CIO0.5 / E0_100)"
        )
    area = match.group(1).upper()
    if area == "E":
        raise ValueError("EM 区请使用 E<bank>_<字地址> 语法,如 E0_100")
    return FinsAddress(area=area, offset=int(match.group(2)), bit=_parse_bit(match.group(3)))


def _parse_bit(text: Optional[str]) -> Optional[int]:
    """位号解析与范围校验(内部函数)。"""
    if text is None:
        return None
    bit = int(text)
    if not 0 <= bit <= MODBUS_REGISTER_BIT_MAX:
        raise ValueError(f"位号必须在 0~{MODBUS_REGISTER_BIT_MAX} 之间,收到:{bit}")
    return bit
