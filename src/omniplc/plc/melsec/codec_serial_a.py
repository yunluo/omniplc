"""三菱 MC 协议 A 兼容串口帧(1C)编解码(纯函数)。

帧布局(SH-080008《MELSEC Communication Protocol Reference Manual》
第 17 章「COMMUNICATING USING 1C FRAMES」,请求包裹与和校验按 4.3 节
算例逐字节核证;1C 帧无帧识别码——4.3 节帧识别码表:F8=4C/F9=3C/
FB=2C/1C 不需要):

- 1C 帧(ASCII,格式 4,与 3C 同款包裹)请求 = ENQ(05H)
  + 站号(2,十六进制 ASCII) + PC号(2,``"FF"`` = 连接站 CPU)
  + 命令(2)+ 消息等待(1,10ms 单位 0~F)+ 软元件区 + 和校验(2) + CR LF
  软元件区:位读 BR/位写 BW = 软元件码 + 编号 + 点数(2,十六进制,
  256 点传 ``"00"``)+ 写数据;字读 WR/字写 WW 同构,写数据每字 4 位十六进制
  命令为 ACPU 共通命令:BR(位成批读)/WR(字成批读)/BW(位成批写)/
  WW(字成批写);JR/QR/JW/QW(AnA/AnU 共通)、BT/WT(随机测试)、
  监视登录/监视、扩展文件寄存器、特殊功能模块缓冲、环回测试等命令
  与本库"点位读写"契约不符,不做
- 1C 响应:读正常 = STX(02H) + 站号/PC号回显(4) + 数据 + ETX(03H)
  + 和校验(2) + CR LF,和校验范围 = 站号起至 ETX **含 ETX**
  (与 3C 格式 4 同规则);写正常 = ACK(06H) + 回显(4) + CR LF;
  异常 = NAK(15H) + 回显(4) + 错误代码(2,十六进制;1C 专属 2 位规格,
  与 3C/4C 的 4 位不同) + CR LF
- 和校验 = 校验范围字节之和的低 8 位,两位 ASCII 十六进制(4.3 节算例:
  ``"00FFBR3M0000"`` 之和 2C0H → ``"C0"``)

软元件规格(17.4 节):X/Y/B/W 十六进制、M/L/S/F/D/R 十进制,编号
4 位 ASCII;T/C 以双字符码访问且编号 3 位——字单位 TN/CN(当前值)、
位读 TS/CS(接点)、位写 TC/CC(线圈)。位软元件按字单位访问时起始
编号必须是 16 的倍数(16 点/字)。点数上限:BR 256 点、BW 160 点、
WR/WW 64 字(位软元件按字:WR 32 字、WW 10 字)。
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Tuple

from . import codec_qna
from . import codec_serial
from .address import McAddress
from ...core.constants import (
    MC_1C_MAX_BITDEV_WORD_READ,
    MC_1C_MAX_BITDEV_WORD_WRITE,
    MC_1C_MAX_BIT_READ_POINTS,
    MC_1C_MAX_BIT_WRITE_POINTS,
    MC_1C_MAX_MESSAGE_WAIT,
    MC_1C_MAX_WORD_POINTS,
)
from ...core.debug import format_hex
from ...core.errors import DeviceError, ProtocolFrameError

_COMMAND_READ_BIT = "BR"
"""位单位成批读命令原文(ACPU 共通命令,SH-080008 17.2 节)。"""
_COMMAND_READ_WORD = "WR"
"""字单位成批读命令原文;位软元件按 16 点/字访问。"""
_COMMAND_WRITE_BIT = "BW"
"""位单位成批写命令原文;写正常无响应数据(裸 ACK)。"""
_COMMAND_WRITE_WORD = "WW"
"""字单位成批写命令原文;位软元件按 16 点/字访问。"""

_MAX_DEC4 = 9999
"""4 位十进制编号上限。"""
_MAX_HEX4 = 0xFFFF
"""4 位十六进制编号上限。"""
_MAX_TC_NUMBER = 999
"""T/C 编号上限(3 位十进制)。"""


class _Spec(NamedTuple):
    """1C 软元件规格:码文本 / 位软元件? / 编号进制 / 编号位数 / 是否 T/C 族。"""

    code: str
    is_bit_device: bool
    base: int
    width: int
    is_timer_counter: bool


_DEVICES: Dict[str, _Spec] = {
    "X": _Spec("X", True, 16, 4, False),
    "Y": _Spec("Y", True, 16, 4, False),
    "M": _Spec("M", True, 10, 4, False),
    "L": _Spec("L", True, 10, 4, False),
    "S": _Spec("S", True, 10, 4, False),
    "F": _Spec("F", True, 10, 4, False),
    "B": _Spec("B", True, 16, 4, False),
    "D": _Spec("D", False, 10, 4, False),
    "W": _Spec("W", False, 16, 4, False),
    "R": _Spec("R", False, 10, 4, False),
    "T": _Spec("T", True, 10, 3, True),
    "C": _Spec("C", True, 10, 3, True),
}
"""1C 软元件码表(SH-080008 17.4 节):编号 4 位,T/C 族 3 位。

T/C 为双性质软元件:字单位 = 当前值 TN/CN,位读 = 接点 TS/CS,
位写 = 线圈 TC/CC——按操作类型映射,与 FINS T/C、汇川 T/C 同思路。
"""


def device_info(device: str) -> Tuple[int, bool, int]:
    """查 1C 软元件信息:``(0, 位软元件?, 进制)``。

    首位二进制码恒 0(1C 用 ASCII 码文本,无二进制码;基类仅在
    read_batch 路径使用码值,而 1C 不支持批量读)。

    :raises ValueError: 未知软元件
    """
    spec = _spec_of(device)
    return 0, spec.is_bit_device, spec.base


def check_message_wait(value: int) -> int:
    """校验消息等待时间(0~15,10ms 单位),非法抛 :class:`ValueError`。"""
    if not 0 <= int(value) <= MC_1C_MAX_MESSAGE_WAIT:
        raise ValueError(
            f"消息等待时间必须在 0~{MC_1C_MAX_MESSAGE_WAIT}(×10ms)之间,收到:{value}"
        )
    return int(value)


def build_1c_request(
    station_number: int,
    pc_number: int,
    message_wait: int,
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
) -> bytes:
    """构造 1C 帧请求(格式 4:ENQ 起始,和校验后接 CR LF)。

    :param station_number: 站号(0~31;0 = 连接站)
    :param pc_number: PC 编号(0~3 或 0xFF;0xFF = 连接站 CPU)
    :param message_wait: 消息等待(0~15,单位 10ms;响应发送最短延迟)
    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位(位读 BR/位写 BW;T/C 位读接点/位写线圈)
    :param is_write: 是否写操作
    :param data: 写数据(位单位 0/1 序列;字单位 0~65535 序列)
    :return: 完整请求帧
    :raises ValueError: 路由/软元件/点数/数据非法
    """
    spec = _spec_of(address.device)
    number = codec_qna.device_number(address.device, address.number, spec.base)
    command = _command_of(is_bit, is_write)
    if is_bit and not spec.is_bit_device:
        raise ValueError(
            f"字软元件 {address.device} 不支持位单位成批访问,请按字访问后提取位"
        )
    if is_bit and spec.is_bit_device:
        codec_qna.reject_bit_suffix_on_bit_device(address)
    if spec.is_timer_counter:
        code_text = spec.code + ("N" if not is_bit else ("C" if is_write else "S"))
    else:
        code_text = spec.code
    limit = _point_limit(spec, is_bit, is_write)
    if not 1 <= points <= limit:
        raise ValueError(f"1C 帧 {command} 点数超出范围 1~{limit}:{points}")
    if not is_bit and spec.is_bit_device and not spec.is_timer_counter:
        if number % 16 != 0:
            raise ValueError(
                f"位软元件 {address.device} 按字单位访问时起始编号必须是 16 的倍数:{number}"
            )
    head = code_text + _number_text(spec, number)
    points_text = "00" if points == MC_1C_MAX_BIT_READ_POINTS else "{:02X}".format(points)
    body_text = (
        _route_text(station_number, pc_number)
        + command
        + "{:X}".format(check_message_wait(message_wait))
        + head
        + points_text
        + _data_text(address, points, is_bit, is_write, data)
    )
    body = body_text.encode("ascii")
    checksum_text = "{:02X}".format(codec_serial.checksum(body)).encode("ascii")
    return bytes([codec_serial.ENQ]) + body + checksum_text + codec_serial._CRLF


def parse_1c_response(
    response: bytes,
    points: int,
    is_bit: bool,
    is_read: bool,
    station_number: int,
    pc_number: int,
) -> List[int]:
    """解析 1C 帧完整响应(格式 4:报文以 CR LF 结尾)。

    :param response: 完整响应帧(由客户端按控制码分流后拼齐)
    :param points: 请求点数(读时校验数据长度)
    :param is_bit: 是否位单位
    :param is_read: 是否读操作(写正常响应无数据)
    :param station_number: 站号(校验响应回显)
    :param pc_number: PC 编号(校验响应回显)
    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 错误代码非 0(保持连接)
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/回显/和校验不符
        (消息带**收到的原始帧**十六进制转储,便于现场与抓包比对)
    """
    if not response:
        raise ProtocolFrameError("1C 响应为空(未收到任何字节)")
    head = response[0]
    route = _route_text(station_number, pc_number).encode("ascii")
    if head == codec_serial.NAK:
        if len(response) != 9:
            raise ProtocolFrameError(
                "1C 异常响应长度不符:期望 9 字节,实际 {}(收到的原始帧:{})".format(
                    len(response), format_hex(response)
                )
            )
        _check_route(response[1:5], route, response)
        codec_serial._check_crlf(response[7:9], "1C 异常响应", response)
        status = codec_serial._hex_int(response[5:7], response)
        raise DeviceError(f"MC 错误代码 0x{status:02X},详见 MELSEC 手册", status)
    if head == codec_serial.ACK:
        if len(response) != 7:
            raise ProtocolFrameError(
                "1C 写响应长度不符:期望 7 字节,实际 {}(收到的原始帧:{})".format(
                    len(response), format_hex(response)
                )
            )
        _check_route(response[1:5], route, response)
        codec_serial._check_crlf(response[5:7], "1C 写响应", response)
        return []
    if head != codec_serial.STX:
        raise ProtocolFrameError(
            "1C 响应控制码非法:0x{:02X}(收到的原始帧:{})".format(
                head, format_hex(response)
            )
        )
    expected = _data_chars(points, is_bit) if is_read else 0
    total = 10 + expected
    if len(response) != total:
        raise ProtocolFrameError(
            "1C 读响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})".format(
                total, len(response), format_hex(response)
            )
        )
    _check_route(response[1:5], route, response)
    if response[5 + expected] != codec_serial.ETX:
        raise ProtocolFrameError(
            "1C 响应数据后必须是 ETX(收到的原始帧:{})".format(format_hex(response))
        )
    codec_serial._check_crlf(response[8 + expected:10 + expected], "1C 读响应", response)
    # 和校验范围 = 站号/PC 号回显 + 数据 + ETX(含 ETX,与 3C 格式 4 同规则)
    wanted = "{:02X}".format(
        codec_serial.checksum(response[1:6 + expected])
    ).encode("ascii")
    if response[6 + expected:8 + expected] != wanted:
        raise ProtocolFrameError(
            "1C 和校验码不符:期望 {},收到 {!r}(收到的原始帧:{})".format(
                wanted.decode("ascii"),
                response[6 + expected:8 + expected].decode("ascii", "replace"),
                format_hex(response),
            )
        )
    if not is_read:
        return []
    data_text = response[5:5 + expected].decode("ascii")
    if is_bit:
        if not set(data_text) <= {"0", "1"}:
            raise ProtocolFrameError(
                "1C 位读数据含非 0/1 字符:{!r}(收到的原始帧:{})".format(
                    data_text, format_hex(response)
                )
            )
        return [1 if char == "1" else 0 for char in data_text]
    return [codec_serial._hex_int(data_text[i:i + 4].encode("ascii")) for i in range(0, expected, 4)]


def _command_of(is_bit: bool, is_write: bool) -> str:
    """按操作方向取 1C 命令原文(内部函数)。"""
    if is_write:
        return _COMMAND_WRITE_BIT if is_bit else _COMMAND_WRITE_WORD
    return _COMMAND_READ_BIT if is_bit else _COMMAND_READ_WORD


def _spec_of(device: str) -> _Spec:
    """查软元件规格,未知软元件抛 :class:`ValueError`(内部函数)。"""
    spec = _DEVICES.get(device)
    if spec is None:
        raise ValueError(
            f"1C 帧不支持的软元件:{device!r},支持:{'/'.join(_DEVICES)}"
        )
    return spec


def _point_limit(spec: _Spec, is_bit: bool, is_write: bool) -> int:
    """按命令与软元件类别取点数上限(SH-080008 17.4 节,内部函数)。

    BR 256 点(256 传 ``"00"``)、BW 160 点、WR/WW 字软元件 64 字;
    位软元件按字单位(16 点/字):WR 32 字、WW 10 字。
    """
    if is_bit:
        return MC_1C_MAX_BIT_WRITE_POINTS if is_write else MC_1C_MAX_BIT_READ_POINTS
    if spec.is_bit_device and not spec.is_timer_counter:
        return MC_1C_MAX_BITDEV_WORD_WRITE if is_write else MC_1C_MAX_BITDEV_WORD_READ
    return MC_1C_MAX_WORD_POINTS


def _number_text(spec: _Spec, number: int) -> str:
    """按码表进制与位数格式化编号,超宽抛 :class:`ValueError`(内部函数)。"""
    limit = _MAX_HEX4 if spec.base == 16 else _MAX_DEC4
    if spec.is_timer_counter:
        limit = _MAX_TC_NUMBER
    if number > limit:
        raise ValueError(
            f"软元件 {spec.code} 编号超出 {spec.width} 位表示范围:{number}"
        )
    return "{:0{}X}".format(number, spec.width) if spec.base == 16 else "{:0{}d}".format(
        number, spec.width
    )


def _route_text(station_number: int, pc_number: int) -> str:
    """格式化并校验站号/PC 号回显域(内部函数)。"""
    return "{:02X}{:02X}".format(
        codec_serial.check_station_number(station_number),
        codec_serial.check_pc_number(pc_number),
    )


def _data_text(
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]],
) -> str:
    """格式化写数据,读操作恒为空串(内部函数)。"""
    if not is_write:
        return ""
    values = data or []
    if len(values) != points:
        raise ValueError("写数据个数 {} 与点数 {} 不符".format(len(values), points))
    if is_bit:
        for flag in values:
            if flag not in (0, 1):
                raise ValueError(f"位写数据只能是 0/1,收到:{flag}")
        return "".join(str(flag) for flag in values)
    for word in values:
        if not 0 <= word <= 0xFFFF:
            raise ValueError(f"字写数据超出范围 0~65535:{word}")
    return "".join("{:04X}".format(word) for word in values)


def _data_chars(points: int, is_bit: bool) -> int:
    """1C 读响应数据字符数:位每点 1 字符,字每字 4 字符(内部函数)。"""
    return points if is_bit else points * 4


def _check_route(raw: bytes, wanted: bytes, whole: bytes) -> None:
    """校验响应站号/PC 号回显(内部函数);``whole`` 为完整响应帧,失败时随消息转储。"""
    if raw != wanted:
        raise ProtocolFrameError(
            "1C 响应站号/PC 号回显不符:期望 {!r},收到 {!r}(收到的原始帧:{})".format(
                wanted.decode("ascii"), raw.decode("ascii", "replace"), format_hex(whole)
            )
        )
