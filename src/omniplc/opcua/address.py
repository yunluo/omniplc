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
from typing import NamedTuple

_NODEID_RE = re.compile(
    r"^(?:ns=(\d+);)?(i=\d+|s=[^;]+|b=[A-Za-z0-9+/=]*|g=[0-9A-Fa-f-]+)$",
    re.IGNORECASE,
)


class OpcUaNodeId(NamedTuple):
    """解析后的 OPC-UA NodeId。

    :ivar namespace: 命名空间索引(0 = OPC-UA 标准命名空间)
    :ivar text: 标准字符串形式(还原给 asyncua,如 ``"ns=2;s=Tag"``)
    """

    namespace: int
    text: str


def parse_opcua_nodeid(address: str) -> OpcUaNodeId:
    """解析 OPC-UA NodeId 字符串。

    :param address: NodeId,如 ``"ns=2;s=Device.Tag"``、``"i=2258"``
    :return: :class:`OpcUaNodeId`
    :raises ValueError: 语法非法
    """
    if not address or not address.strip():
        raise ValueError("OPC-UA NodeId 不能为空")
    normalized = address.strip()
    match = _NODEID_RE.match(normalized)
    if match is None:
        raise ValueError(
            "无法解析 OPC-UA NodeId:{!r}"
            "(示例:ns=2;s=Device.Tag / ns=4;i=100 / i=2258)".format(address)
        )
    namespace = int(match.group(1)) if match.group(1) is not None else 0
    identifier = match.group(2)
    kind, _, id_body = identifier.partition("=")
    kind = kind.lower()
    if kind == "i" and int(id_body) > 0xFFFFFFFF:
        raise ValueError("OPC-UA 数字标识符超出 32 位范围:{!r}".format(address))
    text = "{}={}".format(kind, id_body) if namespace == 0 \
        else "ns={};{}={}".format(namespace, kind, id_body)
    return OpcUaNodeId(namespace=namespace, text=text)
