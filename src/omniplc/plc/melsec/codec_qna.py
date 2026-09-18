"""三菱 MC 协议 QnA 兼容帧(3E/4E)编解码(纯函数)。

3E/4E 帧为 QnA 兼容报文,用于 Q/Q06H、L、R 系列及 iQ-R/iQ-F 的
以太网模块,帧头含网络号/PC 号等路由信息。

**当前为骨架**:签名与语义已定,实现将在下一阶段(MC 驱动)完成。
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from ...core.constants import MC_SUBHEADER_3E_READ, MC_SUBHEADER_3E_WRITE  # noqa: F401  (实现阶段使用)
from .address import McAddress


def build_batch_read_request(
    frame: str,
    network_number: int,
    pc_number: int,
    monitoring_timer: int,
    addresses: Sequence[McAddress],
    word_counts: Sequence[int],
) -> bytes:
    """构造成批读请求(3E/4E 帧的二进制报文)。

    :param frame: ``"3E"`` 或 ``"4E"``
    :param network_number: 网络编号
    :param pc_number: PC 编号
    :param monitoring_timer: 监视定时器(单位 250ms,0 = 等待)
    :param addresses: 软元件地址序列(成批读按点数访问)
    :param word_counts: 与地址对应的读取点数
    :return: 完整请求报文(含帧头与长度)
    """
    raise NotImplementedError("MC 3E/4E 帧编解码将在下一阶段实现")


def build_batch_write_request(
    frame: str,
    network_number: int,
    pc_number: int,
    monitoring_timer: int,
    address: McAddress,
    data: List[int],
) -> bytes:
    """构造成批写请求。

    :param frame: ``"3E"`` 或 ``"4E"``
    :param data: 待写数据(字/位单位,位以 0/1 表示)
    """
    raise NotImplementedError("MC 3E/4E 帧编解码将在下一阶段实现")


def parse_batch_read_response(
    frame: str, response: bytes, word_counts: Sequence[int]
) -> List[int]:
    """解析成批读响应,返回逐点数据(字/位)。

    :raises omniplc.core.errors.DeviceError: 结束代码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构不符
    """
    raise NotImplementedError("MC 3E/4E 帧编解码将在下一阶段实现")


def encode_device(device: str, offset: int, bit: int, frame: str) -> Tuple[bytes, int]:
    """软元件 → 报文中的软元件码 + 点数(查表,3E/4E 与 1E 码表不同)。

    :param device: 软元件记号(如 ``"D"``)
    :param offset: 十进制偏移
    :param bit: 位号(位访问时)
    :param frame: 帧型(影响码表)
    :return: ``(软元件码字节, 访问点数)``
    """
    raise NotImplementedError("MC 软元件码表将在下一阶段实现")
