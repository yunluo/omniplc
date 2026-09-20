"""三菱 MC 协议 QnA 兼容串口帧(3C/4C)编解码(纯函数)。

帧布局(对照 SH-080008《MELSEC Communication Protocol Reference Manual》
4.2/4.3 节与 Appendix 7 完整报文示例,逐字节核证):

- 3C 帧(ASCII,格式 4)请求 = ENQ(05H) + 帧识别码 ``"F9"``
  + 站号(2) + 网络号(2) + PC号(2) + 本站号(2,各两位十六进制 ASCII)
  + 核心命令 + 和校验(2) + CR LF
  其中核心命令 = 命令(4) + 子命令(4) + 软元件码(2,``*`` 补位)
  + 起始编号(6,按码表进制) + 点数(4) + 写数据(位每点 1 字符/字每字 4 字符)
- 3C 响应:读正常 = STX(02H) + ``"F9"`` + 路由回显(8) + 数据 + ETX(03H)
  + 和校验(2) + CR LF;写正常 = ACK(06H) + ``"F9"`` + 路由(8) + CR LF;
  异常 = NAK(15H) + ``"F9"`` + 路由(8) + 错误代码(4) + CR LF
- 4C 帧(二进制,格式 5)请求 = DLE STX(10 02) + 数据长(2,小端,可附加码)
  + 帧识别码 F8H + 站号(1) + 网络号(1) + PC号(1) + 目标模块 I/O(2,小端)
  + 目标模块局号(1) + 本站号(1) + 核心命令 + DLE ETX(10 03) + 和校验(2,ASCII)
  核心命令与 3E 帧完全一致(复用 :func:`codec_qna.build_core`);
  数据长 = 帧识别码(1) + 路由(7) + 核心命令
- 4C 响应 = DLE STX + 数据长 + F8H + 路由(7) + 应答识别码 FFFFH
  + 结束代码(2,小端,0=成功) + 数据 + DLE ETX + 和校验(2,ASCII)
- 附加码(DLE 填充):数据长与数据区内出现 10H 时其前再补一个 10H
  (10H → 10H 10H);数据长与和校验按未填充字节计算
- 和校验 = 校验范围字节之和的低 8 位,恒以两位 ASCII 十六进制发送
  (3C 读响应范围:帧识别码起至 ETX 含;请求范围:帧识别码起至数据;
  4C 范围:数据长起至数据,均不含 CR LF 与控制码起始字符)

与以太网帧的差异:串口帧没有监视定时器与请求数据长(3C)字段;
ASCII 核心命令字段宽度为串口规格(码 2 字符/编号 6 字符/点数 4 字符)。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .address import McAddress
from . import codec_qna
from ...core.constants import (
    MC_MAX_TRANSFER_POINTS,
    MC_SERIAL_FRAME_ID_3C,
    MC_SERIAL_FRAME_ID_4C,
    MC_SERIAL_STATION_MAX,
    MC_SUBCOMMAND_BIT_UNITS,
    MC_SUBCOMMAND_WORD_UNITS,
)
from ...core.errors import DeviceError, ProtocolFrameError

ENQ: int = 0x05
"""控制码:ENQ(询问,请求帧起始)。"""
STX: int = 0x02
"""控制码:STX(正文开始,读响应起始)。"""
ETX: int = 0x03
"""控制码:ETX(正文结束)。"""
ACK: int = 0x06
"""控制码:ACK(写正常响应起始)。"""
NAK: int = 0x15
"""控制码:NAK(异常响应起始)。"""
DLE: int = 0x10
"""控制码:DLE(4C 格式 5 帧定界与附加码)。"""

_CRLF = b"\r\n"
_ID_3C_TEXT = "{:02X}".format(MC_SERIAL_FRAME_ID_3C).encode("ascii")
_COMMAND_READ_TEXT = "0401"
"""成批读命令的 ASCII 标识符原文(二进制帧按 0x0104 大端两字节发送)。"""
_COMMAND_WRITE_TEXT = "1401"
"""成批写命令的 ASCII 标识符原文(二进制帧按 0x0114 大端两字节发送)。"""
_MAX_DEC_NUMBER = 999999
"""6 位十进制编号上限(ASCII 核心命令编号域宽度)。"""


def checksum(payload: bytes) -> int:
    """和校验:校验范围字节之和的低 8 位(SH-080008 4.3 节)。"""
    return sum(payload) & 0xFF


def check_station_number(station: int) -> int:
    """校验站号(0~31),非法抛 :class:`ValueError`。"""
    if not 0 <= int(station) <= MC_SERIAL_STATION_MAX:
        raise ValueError(
            "站号必须在 0~{} 之间,收到:{}".format(MC_SERIAL_STATION_MAX, station)
        )
    return int(station)


def check_pc_number(pc_number: int) -> int:
    """校验 PC 编号(0~3 或 0xFF),非法抛 :class:`ValueError`。"""
    value = int(pc_number)
    if value != 0xFF and not 0 <= value <= 3:
        raise ValueError("PC 编号必须是 0~3 或 0xFF,收到:{}".format(pc_number))
    return value


def check_byte_field(name: str, value: int, maximum: int = 0xFF) -> int:
    """校验单字节路由字段(0~maximum),非法抛 :class:`ValueError`。"""
    if not 0 <= int(value) <= maximum:
        raise ValueError("{} 必须在 0~{} 之间,收到:{}".format(name, maximum, value))
    return int(value)


def build_3c_request(
    station_number: int,
    network_number: int,
    pc_number: int,
    self_station_number: int,
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
    codes: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> bytes:
    """构造 3C 帧请求(格式 4:ENQ 起始,和校验后接 CR LF)。

    :param station_number: 站号(0~31)
    :param network_number: 网络编号
    :param pc_number: PC 编号(0~3 或 0xFF)
    :param self_station_number: 本站号(m:n 多点连接时外部设备自身站号)
    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位
    :param is_write: 是否写操作
    :param data: 写数据(位单位 0/1 序列;字单位 0~65535 序列)
    :param codes: 软元件码表(缺省三菱)
    :return: 完整请求帧
    :raises ValueError: 路由/软元件/点数/数据非法
    """
    core = _ascii_core(address, points, is_bit, is_write, data, codes).encode("ascii")
    route = "{:02X}{:02X}{:02X}{:02X}".format(
        check_station_number(station_number),
        check_byte_field("网络编号", network_number),
        check_pc_number(pc_number),
        check_byte_field("本站号", self_station_number),
    ).encode("ascii")
    body = _ID_3C_TEXT + route + core
    checksum_text = "{:02X}".format(checksum(body)).encode("ascii")
    return bytes([ENQ]) + body + checksum_text + _CRLF


def parse_3c_response(
    response: bytes, points: int, is_bit: bool, is_read: bool
) -> List[int]:
    """解析 3C 帧完整响应(格式 4:报文以 CR LF 结尾)。

    :param response: 完整响应帧(由客户端按控制码分流后拼齐)
    :param points: 请求点数(读时校验数据长度)
    :param is_bit: 是否位单位
    :param is_read: 是否读操作(写正常响应无数据)
    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 错误代码非 0(保持连接)
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/帧识别码/和校验不符
    """
    if not response:
        raise ProtocolFrameError("3C 响应为空")
    head = response[0]
    if head == NAK:
        if len(response) < 17:
            raise ProtocolFrameError(
                "3C 异常响应不完整:期望 17 字节,实际 {}".format(len(response))
            )
        _check_3c_frame_id(response[1:3])
        _check_crlf(response[15:17], "3C 异常响应")
        status = _hex_int(response[11:15])
        raise DeviceError("MC 错误代码 0x{:04X},详见 MELSEC 手册".format(status), status)
    if head == ACK:
        if len(response) != 13:
            raise ProtocolFrameError(
                "3C 写响应长度不符:期望 13 字节,实际 {}".format(len(response))
            )
        _check_3c_frame_id(response[1:3])
        _check_crlf(response[11:13], "3C 写响应")
        return []
    if head != STX:
        raise ProtocolFrameError("3C 响应控制码非法:0x{:02X}".format(head))
    expected = _data_chars(points, is_bit) if is_read else 0
    total = 1 + 2 + 8 + expected + 1 + 2 + 2
    if len(response) != total:
        raise ProtocolFrameError(
            "3C 读响应长度不符:期望 {} 字节,实际 {}".format(total, len(response))
        )
    _check_3c_frame_id(response[1:3])
    if response[11 + expected] != ETX:
        raise ProtocolFrameError("3C 响应数据后必须是 ETX")
    _check_crlf(response[14 + expected:16 + expected], "3C 读响应")
    # 和校验范围 = 帧识别码 + 路由 + 数据 + ETX(手册 Appendix 7 小计:22BH + 18FH)
    wanted = "{:02X}".format(checksum(response[1:12 + expected])).encode("ascii")
    if response[12 + expected:14 + expected] != wanted:
        raise ProtocolFrameError(
            "3C 和校验码不符:期望 {},收到 {!r}".format(
                wanted.decode("ascii"),
                response[12 + expected:14 + expected].decode("ascii", "replace"),
            )
        )
    if not is_read:
        return []
    data_text = response[11:11 + expected].decode("ascii")
    if is_bit:
        if not set(data_text) <= {"0", "1"}:
            raise ProtocolFrameError("3C 位读数据含非 0/1 字符:{!r}".format(data_text))
        return [1 if char == "1" else 0 for char in data_text]
    return [_hex_int(data_text[i:i + 4].encode("ascii")) for i in range(0, expected, 4)]


def build_4c_request(
    station_number: int,
    network_number: int,
    pc_number: int,
    module_io: int,
    module_station: int,
    self_station_number: int,
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
    codes: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> bytes:
    """构造 4C 帧请求(格式 5:DLE STX 定界的二进制帧)。

    :param station_number: 站号(0~31)
    :param network_number: 网络编号
    :param pc_number: PC 编号(0~3 或 0xFF)
    :param module_io: 请求目标模块 I/O 编号(CPU 直连 0x03FF)
    :param module_station: 请求目标模块局号(CPU 直连 0)
    :param self_station_number: 本站号
    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位
    :param is_write: 是否写操作
    :param data: 写数据(位单位 0/1 序列;字单位 0~65535 序列)
    :param codes: 软元件码表(缺省三菱)
    :return: 完整请求帧
    :raises ValueError: 路由/软元件/点数/数据非法
    """
    core = codec_qna.build_core(address, points, is_bit, is_write, data, codes)
    route = (
        bytes(
            [
                check_station_number(station_number),
                check_byte_field("网络编号", network_number),
                check_pc_number(pc_number),
            ]
        )
        + check_byte_field("目标模块 I/O 编号", module_io, 0xFFFF).to_bytes(2, "little")
        + bytes(
            [
                check_byte_field("目标模块局号", module_station),
                check_byte_field("本站号", self_station_number),
            ]
        )
    )
    body = bytes([MC_SERIAL_FRAME_ID_4C]) + route + core
    length = len(body)
    payload = _stuff(length.to_bytes(2, "little")) + _stuff(body)
    total = checksum(length.to_bytes(2, "little") + body)
    return (
        bytes([DLE, STX])
        + payload
        + bytes([DLE, ETX])
        + "{:02X}".format(total).encode("ascii")
    )


def parse_4c_response(
    frame: bytes, points: int, is_bit: bool, is_read: bool
) -> List[int]:
    """解析 4C 帧完整响应(逻辑帧:附加码已由收包层还原)。

    :param frame: 逻辑响应帧 = 数据长(2,小端) + 帧识别码 F8H + 路由(7)
        + 应答识别码 FFFFH + 结束代码(2) + 数据 + DLE ETX + 和校验(2)
    :param points: 请求点数(读时校验数据长度)
    :param is_bit: 是否位单位
    :param is_read: 是否读操作(写正常响应无数据)
    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 结束代码非 0(保持连接)
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/识别码/和校验不符
    """
    if len(frame) < 18:
        raise ProtocolFrameError(
            "4C 响应不完整:至少 18 字节(写响应),实际 {}".format(len(frame))
        )
    length = int.from_bytes(frame[0:2], "little")
    if length < 12:
        raise ProtocolFrameError(
            "4C 应答数据长非法(至少含帧识别码+路由+应答识别码+结束代码):{}".format(length)
        )
    body_size = length - 1
    if len(frame) != 2 + length + 4:
        raise ProtocolFrameError(
            "4C 响应长度不符:期望 {} 字节,实际 {}".format(2 + length + 4, len(frame))
        )
    if frame[2] != MC_SERIAL_FRAME_ID_4C:
        raise ProtocolFrameError(
            "4C 帧识别码不符:期望 F8H,收到 0x{:02X}".format(frame[2])
        )
    body = frame[3:3 + body_size]
    trailer = frame[3 + body_size:]
    if trailer[0] != DLE or trailer[1] != ETX:
        raise ProtocolFrameError(
            "4C 响应和校验前必须是 DLE ETX,收到 0x{:02X} 0x{:02X}".format(
                trailer[0], trailer[1]
            )
        )
    wanted = "{:02X}".format(checksum(frame[:3 + body_size])).encode("ascii")
    if trailer[2:4] != wanted:
        raise ProtocolFrameError(
            "4C 和校验码不符:期望 {},收到 {!r}".format(
                wanted.decode("ascii"), trailer[2:4].decode("ascii", "replace")
            )
        )
    if body[7:9] != b"\xff\xff":
        raise ProtocolFrameError(
            "4C 应答识别码非 FFFFH:0x{:02X} 0x{:02X}".format(body[7], body[8])
        )
    status = int.from_bytes(body[9:11], "little")
    if status != 0:
        raise DeviceError("MC 结束代码 0x{:04X},详见 MELSEC 手册".format(status), status)
    if not is_read:
        return []
    data = body[11:]
    expected = (points + 1) // 2 if is_bit else points * 2
    if len(data) != expected:
        raise ProtocolFrameError(
            "4C 响应数据不足:期望 {} 字节,实际 {}".format(expected, len(data))
        )
    return codec_qna.parse_data(data, points, is_bit)


def _stuff(payload: bytes) -> bytes:
    """DLE 附加码:数据区中的 10H 前再补一个 10H(内部函数)。"""
    out = bytearray()
    for value in payload:
        if value == DLE:
            out.append(DLE)
        out.append(value)
    return bytes(out)


def _data_chars(points: int, is_bit: bool) -> int:
    """3C 读响应数据字符数:位每点 1 字符,字每字 4 字符(内部函数)。"""
    return points if is_bit else points * 4


def _check_3c_frame_id(raw: bytes) -> None:
    """校验 3C 帧识别码回显为 "F9"(内部函数)。"""
    if raw != _ID_3C_TEXT:
        raise ProtocolFrameError(
            "3C 帧识别码不符:期望 {!r},收到 {!r}".format(
                _ID_3C_TEXT.decode("ascii"), raw.decode("ascii", "replace")
            )
        )


def _check_crlf(raw: bytes, label: str) -> None:
    """校验帧尾 CR LF(内部函数)。"""
    if raw != _CRLF:
        raise ProtocolFrameError("{}必须以 CR LF 结尾".format(label))


def _hex_int(raw: bytes) -> int:
    """把 ASCII 十六进制域转为整数,非法抛 ProtocolFrameError(内部函数)。"""
    try:
        return int(raw.decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError):
        raise ProtocolFrameError(
            "十六进制域非法:{!r}".format(raw.decode("ascii", "replace"))
        )


def _ascii_core(
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]],
    codes: Optional[Dict[str, Tuple[int, int, int]]],
) -> str:
    """构造 3C ASCII 核心命令文本(内部函数)。

    宽度为串口规格:命令 4 + 子命令 4 + 软元件码 2(``*`` 补位)
    + 起始编号 6(按码表进制)+ 点数 4 + 写数据。
    """
    code, is_bit_device, base = codec_qna.device_info(address.device, codes)
    if is_bit and not is_bit_device:
        raise ValueError(
            "字软元件 {} 不支持位单位成批访问,请按字访问后提取位".format(address.device)
        )
    if not 1 <= points <= MC_MAX_TRANSFER_POINTS:
        raise ValueError(
            "MC 访问点数超出范围 1~{}:{}".format(MC_MAX_TRANSFER_POINTS, points)
        )
    number = codec_qna.device_number(address.device, address.number, base)
    if base == 16 and number > 0xFFFFFF or base == 10 and number > _MAX_DEC_NUMBER:
        raise ValueError(
            "MC 软元件 {} 编号超出 6 位表示范围:{}".format(address.device, number)
        )
    command = _COMMAND_WRITE_TEXT if is_write else _COMMAND_READ_TEXT
    subcommand = "{:04X}".format(
        MC_SUBCOMMAND_BIT_UNITS if is_bit else MC_SUBCOMMAND_WORD_UNITS
    )
    device_code = "{:*<2}".format(address.device)
    number_text = "{:06X}".format(number) if base == 16 else "{:06d}".format(number)
    core = command + subcommand + device_code + number_text + "{:04d}".format(points)
    if not is_write:
        return core
    values = data or []
    if len(values) != points:
        raise ValueError("写数据个数 {} 与点数 {} 不符".format(len(values), points))
    if is_bit:
        for flag in values:
            if flag not in (0, 1):
                raise ValueError("位写数据只能是 0/1,收到:{}".format(flag))
        return core + "".join(str(flag) for flag in values)
    for word in values:
        if not 0 <= word <= 0xFFFF:
            raise ValueError("字写数据超出范围 0~65535:{}".format(word))
    return core + "".join("{:04X}".format(word) for word in values)
