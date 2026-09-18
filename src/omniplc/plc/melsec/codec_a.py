"""三菱 MC 协议 A 兼容 1E 帧编解码(纯函数)。

1E 帧为 A 兼容报文,用于 A 系列及以太网单元(A1SX 等)或以
A 兼容模式运行的 Q 系列;帧结构与 3E/4E 不同(无网络号/PC 号
前缀字段,报文更短),软元件码表也不同。

**当前为骨架**:签名与语义已定,实现将在下一阶段(MC 驱动)完成。
"""
from __future__ import annotations

from typing import List

from .address import McAddress


def build_1e_read_request(
    pc_number: int, monitoring_timer: int, address: McAddress, word_count: int
) -> bytes:
    """构造 1E 帧成批读请求。

    :param pc_number: 站号(00~FF,00 为本站)
    :param monitoring_timer: 监视定时器
    :param address: 软元件地址(位/字软元件)
    :param word_count: 读取点数
    """
    raise NotImplementedError("MC 1E 帧编解码将在下一阶段实现")


def build_1e_write_request(
    pc_number: int, monitoring_timer: int, address: McAddress, data: List[int]
) -> bytes:
    """构造 1E 帧成批写请求。"""
    raise NotImplementedError("MC 1E 帧编解码将在下一阶段实现")


def parse_1e_read_response(response: bytes, word_count: int) -> List[int]:
    """解析 1E 帧成批读响应。

    :raises omniplc.core.errors.DeviceError: 结束代码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构不符
    """
    raise NotImplementedError("MC 1E 帧编解码将在下一阶段实现")
