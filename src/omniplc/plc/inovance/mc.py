"""汇川 MC 协议兼容客户端——继承三菱 MC 实现。

H5U&Easy 系列固件(V6.4.0.0+)在以太网口提供三菱 MC 协议服务器,
支持 3E/4E 帧、TCP/UDP、二进制/ASCII(手册第 16 章"MC通信";
16.4 软元件表的"对应三菱软元件"列给出映射)。本库取 TCP + 二进制
+ 3E 帧,帧格式与三菱 QnA 兼容 3E 完全一致,因此继承
:class:`~omniplc.plc.melsec.MelsecMcTcpClient`,只做汇川记号 →
三菱帧记号的换算:

- ``S`` 按三菱 L 编码(92h)访问,编号不变
- ``R`` 与 D 统一编址:R n ≡ D(8000+n),如 R0 按三菱 D8000 访问
- ``X``/``Y`` 用户命名沿用汇川八进制(X/Y0~X/Y1777),
  客户端层换算为三菱 3E 帧的十六进制编号(如 X17 → 帧 0x0F)

地址语法::

    M100      内部继电器(位,十进制,M0~M7999)
    S10       状态继电器(位,十进制,S0~S4095)
    B1F       链接继电器(位,十六进制,B0~B32767)
    X17 / Y7  输入/输出继电器(位,八进制命名,0~1777)
    D100      数据寄存器(字,十进制,D0~D7999)
    R100      文件寄存器(字,十进制,R0~R32767 = D8000~D40767)
    W10       链接寄存器(字,十六进制,W0~W32767)
    D100.3    字软元件位访问(读-改-写)

软元件码表见 :data:`omniplc.core.constants.INOVANCE_MC_DEVICE_CODES`;
软元件范围由 PLC 校验(越界返回结束码 4031,按设备故障处理)。
手册 16.1 的支持机型列表为 Easy 系列(Easy523/522/521/320),
同卷 H5U 未单列,接入 H5U 前请先确认固件提供"MC配置"。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..melsec import codec_qna
from ..melsec.address import McAddress
from ..melsec.melsec import MelsecMcTcpClient
from ...core.constants import (
    INOVANCE_MC_DEFAULT_PORT,
    INOVANCE_MC_DEVICE_CODES,
    INOVANCE_MC_R_BASE,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
)
from ...types import McFrame


def _to_melsec_address(parsed: McAddress) -> McAddress:
    """汇川记号换算为三菱帧记号(内部函数)。

    R 统一编址(R n → D(8000+n));X/Y 八进制命名换算为帧内
    十六进制编号;其余软元件原样透传。
    :raises ValueError: R 编号非十进制或 X/Y 编号非八进制
    """
    device = parsed.device
    if device == "R":
        try:
            number = int(parsed.number, 10)
        except ValueError:
            raise ValueError("汇川 R 编号为十进制,解析失败:{!r}".format(parsed.number))
        return McAddress("D", str(number + INOVANCE_MC_R_BASE), parsed.bit)
    if device in ("X", "Y"):
        try:
            number = int(parsed.number, 8)
        except ValueError:
            raise ValueError(
                "汇川 {} 编号为八进制(数字 0~7),解析失败:{!r}(示例:X17)".format(
                    device, parsed.number
                )
            )
        return McAddress(device, format(number, "X"), parsed.bit)
    return parsed


class InovanceMcTcpClient(MelsecMcTcpClient):
    """汇川 MC 协议兼容客户端(TCP,3E 帧二进制)。

    :example: ``client = InovanceMcTcpClient("192.168.1.88", 2000)``

    仅支持 3E 帧(4E/UDP/ASCII 留后续),``frame`` 属性恒为
    :attr:`McFrame.FRAME_3E`。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.88",
        port: int = INOVANCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化汇川 MC 兼容客户端。

        :param ip_address: PLC 的 IP 或主机名(H5U/Easy 出厂默认 192.168.1.88)
        :param port: 端口,与 AutoShop"MC配置"中设置的端口号一致
            (手册未规定出厂默认;范围 1025~4999、5010~49151,
            不可用 502/9600/44818/2222/34980/12939/12940)
        :param network_number: 网络编号(按三菱 MC 默认 0)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, McFrame.FRAME_3E, network_number, pc_number)

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """查汇川 MC 码表(R 视同 D,内部方法)。"""
        return codec_qna.device_info(
            "D" if device == "R" else device, INOVANCE_MC_DEVICE_CODES
        )

    def _translate_address(self, parsed: McAddress) -> McAddress:
        """汇川记号换算为三菱帧记号(批量读路径与 _build_frame 同源,内部方法)。"""
        return _to_melsec_address(parsed)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """构造 3E 请求帧,汇川记号先换算为三菱帧记号(内部方法)。"""
        return codec_qna.build_request(
            self._frame.value,
            self._next_serial(),
            self._network_number,
            self._pc_number,
            MC_DEFAULT_MONITOR_TIMER,
            _to_melsec_address(parsed),
            points,
            is_bit,
            is_write,
            data,
            INOVANCE_MC_DEVICE_CODES,
        )
