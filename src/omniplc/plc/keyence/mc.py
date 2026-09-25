"""基恩士 KV 系列 MC 协议兼容(SLMP)客户端——继承三菱 MC 实现。

KV-7500/8000/X 系列以太网口提供 MC 协议兼容(SLMP,3E 帧二进制)模式,
支持 TCP 与 UDP 两种走线,帧格式与三菱 QnA 兼容 3E 完全一致
(副头部 ``50 00``、成批读 0104/写 0114、结束代码语义),仅软元件记号与
软元件码不同(R/DM/ZR 用基恩士记号),因此本模块只覆写软元件码表并把
帧型固定为 3E,收发、按长收包(TCP)/一问一答一数据报(UDP)与解析
全部复用 :mod:`omniplc.plc.melsec`。

地址语法::

    R5        继电器(位,十进制)
    B1F       工作位(位,十六进制)
    W10       工作区(字,十六进制)
    DM100     数据存储(字,十进制)
    ZR100     文件寄存器(字,十进制)
    DM100.3   字软元件位访问(读-改-写)

位组记号 R 的编号口径(2026-09-26,勿按 review K1 改):线上编号 = **记号数字
原样**(组号十进制 + 位号两位,R515 → 515),**不**换算为线性位号(R515 → 95)。
依据:三菱 SLMP 手册 SH081257ENG 对"通信对象为 KEYENCE(KV 系列)"的设备格式
约定(除 B 设备外,上位数字为字号、低 2 位为位号)与 KV 公布的位组点数范围
(R00000~R199915,同样按该记号书写);本库 Host Link 路径同一字符串语义一致
(``R515`` = 组 5 位 15)。MC 路径不复核"位号低两位 00~15"(``R16`` 会被发成
16),是否收口待真机终核后决定。**真机核证判据**见
``docs/real-machine-checklist.md`` 的 KV MC 条目。

软元件码表见 :data:`omniplc.core.constants.KEYENCE_MC_DEVICE_CODES`;
不使用三菱记号(D/M/X/Y),连三菱机型请直接用 ``MelsecMcTcpClient``。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..melsec import codec_qna
from ..melsec.address import McAddress
from ..melsec.melsec import MelsecMcTcpClient, MelsecMcUdpClient, _MelsecMcBase
from ...core.constants import (
    KEYENCE_MC_DEFAULT_PORT,
    KEYENCE_MC_DEVICE_CODES,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
)
from ...types import McFrame


class _KeyenceMcCodeMixin(_MelsecMcBase):
    """基恩士码表覆写(TCP/UDP 走线共用,内部基类)。

    仅把软元件码查找与 3E 请求组帧换到基恩士表
    (:data:`KEYENCE_MC_DEVICE_CODES`),收发与解析仍走三菱实现;
    未实现 ``_create_transport``,本类不可实例化。
    """

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """查基恩士 SLMP 兼容码表(内部方法)。"""
        return codec_qna.device_info(device, KEYENCE_MC_DEVICE_CODES)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """构造 3E 请求帧,软元件码走基恩士表(内部方法)。"""
        return codec_qna.build_request(
            self._frame.value,
            self._next_serial(),
            self._network_number,
            self._pc_number,
            MC_DEFAULT_MONITOR_TIMER,
            parsed,
            points,
            is_bit,
            is_write,
            data,
            KEYENCE_MC_DEVICE_CODES,
        )


class KeyenceMcTcpClient(_KeyenceMcCodeMixin, MelsecMcTcpClient):
    """基恩士 KV MC 协议兼容(SLMP)客户端(TCP,3E 帧二进制)。

    :example: ``client = KeyenceMcTcpClient("192.168.1.22", 5000)``

    仅支持 3E 帧(基恩士 SLMP 兼容模式不提供 4E/1E),
    ``frame`` 属性恒为 :attr:`McFrame.FRAME_3E`。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.22",
        port: int = KEYENCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 KV MC 兼容客户端。

        :param ip_address: PLC 的 IP 或主机名(KV 以太网单元设置中配置)
        :param port: 端口(KV SLMP 兼容默认 5000,以单元设置为准)
        :param network_number: 网络编号(KV 通常按默认 0 应答)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, McFrame.FRAME_3E, network_number, pc_number)


class KeyenceMcUdpClient(_KeyenceMcCodeMixin, MelsecMcUdpClient):
    """基恩士 KV MC 协议兼容(SLMP)客户端(UDP 走线,3E 帧二进制)。

    KV 以太网单元 SLMP 兼容通信的 UDP 走线:一问一答一数据报,
    帧格式与 TCP 完全一致,默认端口同为 5000(以单元设置为准)。

    :example: ``client = KeyenceMcUdpClient("192.168.1.22", 5000)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.22",
        port: int = KEYENCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 KV MC 兼容 UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名(KV 以太网单元设置中配置)
        :param port: 端口(KV SLMP 兼容默认 5000,以单元设置为准)
        :param network_number: 网络编号(KV 通常按默认 0 应答)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, McFrame.FRAME_3E, network_number, pc_number)
