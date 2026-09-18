"""欧姆龙 FINS 帧编解码(纯函数)。

FINS 帧:ICF/RSV/GCT + 目标网络/节点/单元 + 源网络/节点/单元 + SID
+ 命令(MRC/SRC)+ 数据。FINS/TCP 在帧外还有 "FINS" 魔数 + 长度 +
命令字的 TCP 头,以及连接建立时的节点分配握手。

**当前为骨架**:签名与语义已定,实现将在下一阶段(FINS 驱动)完成。
"""
from __future__ import annotations

from typing import List, Sequence

from .address import FinsAddress


def build_node_allocation_request(local_node: int) -> bytes:
    """构造 FINS/TCP 节点分配请求(命令字 00000001)。

    :param local_node: 期望的本地节点号,0 = 由 PLC 自动分配
    """
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def parse_node_allocation_response(response: bytes) -> int:
    """解析节点分配响应,返回 PLC 分配的本地节点号。"""
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def build_area_read(
    address: FinsAddress, word_count: int, node_addresses: Sequence[int]
) -> bytes:
    """构造 Area Read(命令 0101)FINS 帧。

    :param address: 起始地址(存储区码由 area 查表得到)
    :param word_count: 读取字数
    :param node_addresses: (目标网络, 目标节点, 目标单元, 源网络, 源节点, 源单元)
    """
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def build_area_write(address: FinsAddress, data: List[int], node_addresses: Sequence[int]) -> bytes:
    """构造 Area Write(命令 0102)FINS 帧。"""
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def parse_area_read_response(response: bytes, word_count: int) -> List[int]:
    """解析 Area Read 响应,返回逐字数据(大端)。

    :raises omniplc.core.errors.DeviceError: 结束码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构不符
    """
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def wrap_fins_tcp(fins_frame: bytes) -> bytes:
    """为 FINS 帧添加 FINS/TCP 头("FINS" 魔数 + 长度 + 命令 00000002)。"""
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")


def unwrap_fins_tcp(data: bytes) -> bytes:
    """剥离 FINS/TCP 头,返回内层 FINS 帧。"""
    raise NotImplementedError("FINS 帧编解码将在下一阶段实现")
