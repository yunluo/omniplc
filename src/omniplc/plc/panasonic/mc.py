"""松下 FP 系列 MC 协议兼容客户端——继承三菱 MC 实现。

FP0H/FP7 系列以太网口提供三菱 MC 协议兼容模式(QnA 兼容 3E 帧,
仅二进制、成批读/写,松下 FP0H《以太网通信手册》),帧格式与三菱
QnA 兼容 3E 完全一致,因此本模块继承
:class:`~omniplc.plc.melsec.MelsecMcTcpClient`,只做两件事:

1. 换软元件码表(:data:`~omniplc.core.constants.PANASONIC_MC_DEVICE_CODES`,
   与三菱 Q/L 系列同码:X=9Ch/Y=9Dh/L=A0h/R=90h/D=A8h…);
2. 换算"字号 + 位号"组织的位软元件地址:X/Y/L/R 的帧内编号 =
   字号×16+位号;字号 ≥900(记法 R9000 起)映射系统继电器 SM,
   D 编号 ≥90000 映射系统寄存器 SD。

地址语法::

    R000F      内部继电器(位):字号 000(十进制)+ 位号 F(十六进制一位)
    R1.15      同上,点号形式(字号.位号,位号十进制)
    X0000 / Y000F / L001F   输入/输出/链接继电器(位)
    SM10       系统继电器(位,十进制)
    D100       数据寄存器 DT(字,十进制)
    D100.3     字软元件位访问(读-改-写)
    LD10 / SD10             链接/系统寄存器(字)
    TN0 / TS0 / CN0 / CS0   定时器计数器当前值/接点

软元件范围由 PLC 校验(越界按结束码报设备故障)。默认端口沿用三菱
惯例 2000(占位),实际以 PLC 模块配置为准。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..melsec import codec_qna
from ..melsec.address import McAddress
from ..melsec.melsec import MelsecMcTcpClient
from ...core.constants import (
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    PANASONIC_MC_DEFAULT_PORT,
    PANASONIC_MC_DEVICE_CODES,
    PANASONIC_MC_SD_BASE,
    PANASONIC_MC_SM_LINEAR_BASE,
)
from ...types import McFrame

# "字号 + 位号"组织的位软元件(帧内编号 = 字号×16 + 位号)
_WORD_BIT_DEVICES = ("X", "Y", "L", "R")


def _to_melsec_address(parsed: McAddress) -> McAddress:
    """松下记号换算为帧内三菱编号(内部函数)。

    位软元件 X/Y/L/R:字号(十进制)×16 + 位号(末位十六进制一位,
    或点号形式 .十进制位号)→ 线性编号;R 线性编号 ≥14400(字号 ≥900,
    记法 R9000 起)映射 SM(编号减 14400)。D 编号 ≥90000 映射 SD。
    :raises ValueError: 字号/位号解析失败
    """
    device = parsed.device
    if device in _WORD_BIT_DEVICES:
        linear, bit = _linearize(device, parsed.number, parsed.bit)
        if device == "R" and linear >= PANASONIC_MC_SM_LINEAR_BASE:
            return McAddress("SM", str(linear - PANASONIC_MC_SM_LINEAR_BASE), None)
        return McAddress(device, str(linear), None)
    if device == "D" and int(parsed.number, 10) >= PANASONIC_MC_SD_BASE:
        return McAddress("SD", str(int(parsed.number, 10) - PANASONIC_MC_SD_BASE), parsed.bit)
    return parsed


def _linearize(device: str, number_text: str, bit: Optional[int]) -> Tuple[int, int]:
    """把"字号 + 位号"地址换算为线性编号与位号(内部函数)。

    末位字符恒为位号(十六进制 0~F),其余为十进制字号;点号形式
    (字号.十进制位号)按 :class:`McAddress` 的 bit 字段携带。
    """
    if bit is not None:
        word_text, bit_value = number_text, bit
    else:
        if not number_text:
            raise ValueError(
                "松下位软元件 {} 需要字号+位号(如 {}000F)或点号形式(如 {}.15)".format(
                    device, device, device
                )
            )
        word_text, bit_value = number_text[:-1], int(number_text[-1], 16)
    if not word_text:
        raise ValueError(
            "松下位软元件 {} 地址缺少字号:{!r}"
            "(字号+位号形式如 {}000F,点号形式如 {}.15)".format(device, number_text, device, device)
        )
    try:
        word = int(word_text, 10)
    except ValueError:
        raise ValueError(
            "松下位软元件 {} 字号为十进制,解析失败:{!r}".format(device, word_text)
        )
    return word * 16 + bit_value, bit_value


class PanasonicMcTcpClient(MelsecMcTcpClient):
    """松下 MC 协议兼容客户端(TCP,3E 帧二进制)。

    :example: ``client = PanasonicMcTcpClient("192.168.0.10", 2000)``

    仅支持 3E 帧(松下 MC 兼容模式仅提供 QnA 兼容 3E 二进制),
    ``frame`` 属性恒为 :attr:`McFrame.FRAME_3E`。
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = PANASONIC_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化松下 MC 兼容客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,与 PLC 以太网模块 MC 协议配置一致
            (手册未规定出厂默认;默认 2000 为三菱惯例占位)
        :param network_number: 网络编号(按三菱 MC 默认 0)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, McFrame.FRAME_3E, network_number, pc_number)

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """查松下 MC 码表(R 视按位软元件,内部方法)。"""
        return codec_qna.device_info(device, PANASONIC_MC_DEVICE_CODES)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """构造 3E 请求帧,松下记号先换算为帧内编号(内部方法)。"""
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
            PANASONIC_MC_DEVICE_CODES,
        )
