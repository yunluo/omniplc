"""AB Logix 标签名解析。

Logix 地址即标签名,支持::

    MyDint               基本标签
    MyArray[5]           数组元素(下标 0~0xFFFFFFFF)
    MyMatrix[1,2]        多维数组元素
    MyUdt.Member         UDT 成员路径(可多级、可夹数组下标)
    Program:prog.Tag     程序作用域标签
    MyDint.3             位访问(整型标签位号,读-改-写)

解析结果不涉及软元件码表——AB 标签自描述,实际类型由 PLC 在应答中返回。
解析失败统一抛 ``ValueError``(参数错误约定)。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple, Optional, Tuple

_ADDRESS_PATTERN = re.compile(r"[A-Za-z0-9_:\.\[\], ]+")
_MEMBER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)?")
_BIT_PATTERN = re.compile(r"\.(\d+)$")
_INDEX_PATTERN = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]$")
_INDEX_MAX = 0xFFFFFFFF


class AbTag(NamedTuple):
    """解析后的 Logix 标签。

    - ``name``:地址原文
    - ``members``:逐级成员名(``MyUdt.Member`` → ``("MyUdt", "Member")``)
    - ``indices``:每级成员的数组下标(无下标为 ``()``)
    - ``bit``:末尾位号(``MyDint.3`` → ``3``;无位号 ``None``)
    """

    name: str
    members: Tuple[str, ...]
    indices: Tuple[Tuple[int, ...], ...]
    bit: Optional[int]

    @property
    def base(self) -> str:
        """成员路径基名(不含下标与位号,类型缓存键)。"""
        return ".".join(self.members)


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=4096)
def parse_ab_tag(address: str) -> AbTag:
    """解析 Logix 标签名为 :class:`AbTag`。

    :param address: 标签名原文
    :return: 解析结果
    :raises ValueError: 语法非法
    """
    text = address.strip()
    if not text:
        raise ValueError("AB 标签名为空")
    if not _ADDRESS_PATTERN.fullmatch(text):
        raise ValueError("AB 标签含非法字符:{!r}".format(address))

    bit: Optional[int] = None
    body = text
    bit_match = _BIT_PATTERN.search(body)
    if bit_match:
        bit = int(bit_match.group(1))
        body = body[: bit_match.start()]

    members: list = []
    indices: list = []
    for raw_segment in body.split("."):
        segment = raw_segment.strip()
        index_match = _INDEX_PATTERN.search(segment)
        segment_indexes: Tuple[int, ...] = ()
        if index_match:
            segment_indexes = tuple(
                int(part) for part in index_match.group(1).split(",")
            )
            segment = segment[: index_match.start()]
        if not _MEMBER_PATTERN.fullmatch(segment):
            raise ValueError(
                "AB 标签段非法(应为标识符):{!r}(地址 {!r})".format(raw_segment, address)
            )
        for number in segment_indexes:
            if number > _INDEX_MAX:
                raise ValueError(
                    "AB 数组下标超出 0~{}:{!r}".format(_INDEX_MAX, address)
                )
        members.append(segment)
        indices.append(segment_indexes)

    return AbTag(address, tuple(members), tuple(indices), bit)
