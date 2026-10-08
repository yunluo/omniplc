"""OPC-UA NodeId 地址解析。

OPC-UA 以 **NodeId** 寻址,本文语法与标准字符串形式一致
(OPC 10000-3,不区分命名空间前缀大小写):

============  ============================================
语法           含义
============  ============================================
``ns=2;s=Tag``  命名空间 2 的字符串标识符(最常用)
``ns=4;i=100``  命名空间 4 的数字标识符
``i=2258``      省略 ns 时默认命名空间 0(如 Server 状态)
``b=AAECAw==``  base64 不透明标识符
``g=...``       GUID 标识符
============  ============================================

本模块只做语法校验与拆分,**不依赖 asyncua**——解析在参数校验
阶段完成,组帧(会话调用)时还原为标准字符串交给 asyncua。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, NamedTuple
from ...core.constants import ADDRESS_CACHE_MAXSIZE
from ...core.i18n import _

_BASE64_RE = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}(?:==)|[A-Za-z0-9+/]{3}(?:=)|[A-Za-z0-9+/]{4})?$"
)
"""base64 严格校验(RFC 4648 §4):4 字符组 + 可选尾部 2/3 字符 + 对应
数量的 padding。宽松的 ``[A-Za-z0-9+/=]*`` 会放行 ``AA==BB==`` 之类
``=`` 夹在中间的串——Python ``b64decode`` 在首个 padding 后丢弃余料,
该串静默解码为单字节节点,**寻址到与书写意图无关的节点**。"""
_GUID_RE = re.compile(
    r"^(?:\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{12}\}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$"
)
"""GUID 严格校验:8-4-4-4-12 共 32 位十六进制 + 4 连字符,花括号**成对**
可选(review-1018 P2-14:原 `\{?`/`\}?` 各自可选,`{8-4-…` 单开括号
也放行,运行时 asyncua UaError 才暴露);依据 OPC 10000-3 / RFC 4122
文本格式。"""


class OpcUaNodeId(NamedTuple):
    """解析后的 OPC-UA NodeId。

    :ivar namespace: 命名空间索引(0 = OPC-UA 标准命名空间)
    :ivar text: 标准字符串形式(还原给 asyncua,如 ``"ns=2;s=Tag"``)
    """

    namespace: int
    text: str


def _bad_nodeid(address: str) -> str:
    """构造非法 NodeId 的统一错误文本(内部函数)。"""
    return (
        "无法解析 OPC-UA NodeId:{!r}"
        "(示例:ns=2;s=Device.Tag / ns=4;i=100 / i=2258)".format(address)
    )


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)
def parse_opcua_nodeid(address: str) -> OpcUaNodeId:
    """解析 OPC-UA NodeId 字符串。

    与 asyncua ``NodeId.from_string`` 对齐:``;`` 分隔组件;``s=`` 之后的
    内容(可含 ``;`` 与 ``=``)整体作为字符串标识符;支持 ``ns=``/``i=``/
    ``s=``/``b=``/``g=`` 以及扩展的 ``nsu=``/``srv=``。

    :param address: NodeId,如 ``"ns=2;s=Device.Tag"``、``"i=2258"``
    :return: :class:`OpcUaNodeId`
    :raises ValueError: 语法非法
    """
    if not address or not address.strip():
        raise ValueError(_("OPC-UA NodeId 不能为空"))
    normalized = address.strip()
    elements = normalized.split(";")
    namespace = 0
    kind = None
    body = None
    extras: Dict[str, str] = {}
    index = 0
    while index < len(elements):
        element = elements[index]
        index += 1
        if not element:
            continue
        key, sep, value = element.partition("=")
        if not sep:
            raise ValueError(_bad_nodeid(address))
        key = key.strip().lower()
        if key == "ns":
            try:
                namespace = int(value.strip())
            except ValueError as exc:
                raise ValueError(_bad_nodeid(address)) from exc
            if not 0 <= namespace <= 0xFFFF:
                # NamespaceIndex 是 UInt16(OPC 10000-3):负数/超界先在
                # 参数校验期拒绝,不延迟到 asyncua 编码层以深层异常爆出
                raise ValueError(
                    _("OPC-UA 命名空间索引超出 UInt16 范围 0~65535:{!r}").format(
                        address
                    )
                )
        elif key in ("i", "b", "g"):
            if kind is not None:
                raise ValueError(_bad_nodeid(address))
            kind, body = key, value.strip()
        elif key == "s":
            if kind is not None:
                raise ValueError(_bad_nodeid(address))
            # s= 吞掉其余全部组件(字符串标识符可含 ; 与 =)
            body = ";".join([value] + elements[index:])
            index = len(elements)
            kind = "s"
        elif key == "srv":
            if not value.strip().isdigit():
                raise ValueError(_bad_nodeid(address))
            extras[key] = value.strip()
        elif key == "nsu":
            if not value.strip():
                raise ValueError(_bad_nodeid(address))
            extras[key] = value.strip()
        else:
            raise ValueError(_bad_nodeid(address))
    if kind is None or body is None:
        raise ValueError(_bad_nodeid(address))
    if kind == "i":
        try:
            number = int(body)
        except ValueError as exc:
            raise ValueError(_bad_nodeid(address)) from exc
        if number < 0 or number > 0xFFFFFFFF:
            raise ValueError(_("OPC-UA 数字标识符超出 32 位范围:{!r}").format(address))
        body = str(number)
    elif kind == "s":
        if body == "":
            raise ValueError(_bad_nodeid(address))
    elif kind == "b":
        if not _BASE64_RE.match(body):
            raise ValueError(_bad_nodeid(address))
    elif kind == "g":
        if not _GUID_RE.match(body):
            raise ValueError(_bad_nodeid(address))
    parts = []
    if namespace != 0:
        parts.append("ns={}".format(namespace))
    for extra_key in ("nsu", "srv"):
        if extra_key in extras:
            parts.append("{}={}".format(extra_key, extras[extra_key]))
    parts.append("{}={}".format(kind, body))
    return OpcUaNodeId(namespace=namespace, text=";".join(parts))
