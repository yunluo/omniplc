"""三菱 MC 协议 QnA 兼容帧(3E/4E)编解码(纯函数)。

帧布局(对照 HslCommunication ``MelsecMcNet.PackMcCommand`` 与
SLMP 参考库 ``core.encode_3e_request``/``encode_4e_request``,SH-080956):

- 3E 请求 = 副头部 ``50 00`` + 网络号(1) + PC号(1) + 目标模块I/O(2,小端)
  + 目标模块局号(1) + 请求数据长(2,小端) + 监视定时器(2,小端) + 核心命令
- 4E 请求 = 副头部 ``54 00`` + 序列号(2,小端) + 保留(2,恒 0) + 同 3E 其余字段
- 核心读 = ``01 04`` + 子命令(2,小端:0=字单位/1=位单位)
  + 起始软元件编号(3,小端) + 软元件码(1) + 点数(2,小端)
- 核心写 = ``01 14`` + 同上 + 数据
- 数据:字单位逐字小端;位单位每字节 2 位,**高位在前**(点 0 在高半字节)
- 3E 响应 = 副头部 ``D0 00`` + 网络(1) + PC(1) + 模块I/O(2) + 局号(1)
  + 应答数据长(2,小端) + 结束代码(2,小端,0=成功) + 数据
- 4E 响应 = 副头部 ``D4 00`` + 序列号(2) + 保留(2) + 网络(1) + PC(1)
  + 模块I/O(2) + 局号(1) + 应答数据长(2) + 结束代码(2) + 数据
- 请求数据长 = 监视定时器(2) + 命令(2) + 子命令(2) + 载荷

软元件地址进制见 :data:`omniplc.core.constants.MC_DEVICE_CODES`
(Q/L/R 口径:X/Y/W/B 十六进制,其余十进制)。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .address import McAddress
from ...core.constants import (
    MC_4E_RESPONSE_HEAD_SIZE,
    MC_COMMAND_BATCH_READ,
    MC_COMMAND_BATCH_WRITE,
    MC_DEVICE_CODES,
    MC_DEST_MODULE_IO,
    MC_DEST_MODULE_STATION,
    MC_MAX_TRANSFER_POINTS,
    MC_RESPONSE_HEAD_SIZE,
    MC_RESPONSE_SUBHEADER_3E,
    MC_RESPONSE_SUBHEADER_4E,
    MC_SUBCOMMAND_BIT_UNITS,
    MC_SUBCOMMAND_WORD_UNITS,
    MC_SUBHEADER_3E,
    MC_SUBHEADER_4E,
)
from ...core.errors import DeviceError, ProtocolFrameError

_FRAME_NAMES = ("3E", "4E")


def device_info(
    device: str,
    codes: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> Tuple[int, bool, int]:
    """查 3E/4E 软元件码表。

    :param device: 软元件名(如 ``D``、``X``、``R``)
    :param codes: 码表(缺省为三菱 ``MC_DEVICE_CODES``;基恩士 SLMP 兼容传 ``KEYENCE_MC_DEVICE_CODES``)
    :return: ``(软元件码, 是否位软元件, 地址进制)``
    :raises ValueError: 不支持的软元件
    """
    table = MC_DEVICE_CODES if codes is None else codes
    try:
        code, is_bit, base = table[device]
    except KeyError:
        raise ValueError(
            "不支持的 MC 软元件:{!r},支持:{}".format(device, "/".join(sorted(table)))
        )
    return code, bool(is_bit), base


def device_number(device: str, number: str, base: int) -> int:
    """把用户编号数字原文换算为报文中的软元件编号(按码表进制)。

    :raises ValueError: 编号按目标进制解析失败
    """
    try:
        return int(number, base)
    except ValueError:
        raise ValueError(
            "软元件 {} 编号按 {} 进制解析失败:{!r}".format(device, base, number)
        )


def build_core(
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
    codes: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> bytes:
    """构造 QnA 兼容核心命令(命令+子命令+软元件+点数+写数据)。

    3E/4E(以太网)与 4C 格式 5(串口二进制)的请求核心完全一致
    (SH-080008 手册 8.2 节:软元件码/编号/点数三帧同格式),供两处复用。

    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位
    :param is_write: 是否写操作
    :param data: 写数据(字单位逐字 0~65535;位单位 0/1 序列,长度 = points)
    :param codes: 软元件码表(缺省三菱)
    :return: 核心命令字节(命令 2 + 子命令 2 + 编号 3 + 码 1 + 点数 2 + 写数据)
    :raises ValueError: 软元件/点数/数据非法
    """
    code, is_bit_device, base = device_info(address.device, codes)
    if is_bit and not is_bit_device:
        raise ValueError(
            "字软元件 {} 不支持位单位成批访问,请按字访问后提取位".format(address.device)
        )
    _check_points(points, MC_MAX_TRANSFER_POINTS)
    number = device_number(address.device, address.number, base)
    if number > 0xFFFFFF:
        raise ValueError("MC 软元件编号超出 3 字节范围:{}".format(number))

    command = MC_COMMAND_BATCH_WRITE if is_write else MC_COMMAND_BATCH_READ
    subcommand = MC_SUBCOMMAND_BIT_UNITS if is_bit else MC_SUBCOMMAND_WORD_UNITS
    core = bytearray(command.to_bytes(2, "big"))
    core += subcommand.to_bytes(2, "little")
    core += number.to_bytes(3, "little")
    core.append(code)
    core += points.to_bytes(2, "little")
    if is_write:
        core += _write_payload(points, is_bit, data or [])
    return bytes(core)


def parse_data(data: bytes, points: int, is_bit: bool) -> List[int]:
    """解析读响应数据段:位按半字节(高半字节在前),字逐字小端。

    :param data: 响应数据段(长度已由调用方校验)
    :param points: 请求点数
    :param is_bit: 是否位单位
    :return: 逐点数据(位 0/1,字 0~65535)
    """
    if is_bit:
        return [
            1 if data[index // 2] & (0x10 if index % 2 == 0 else 0x01) else 0
            for index in range(points)
        ]
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, points * 2, 2)]


def build_request(
    frame: str,
    serial: int,
    network_number: int,
    pc_number: int,
    monitoring_timer: int,
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
    codes: Optional[Dict[str, Tuple[int, int, int]]] = None,
) -> bytes:
    """构造 3E/4E 帧请求(成批读 0104 / 成批写 0114)。

    :param frame: ``"3E"`` 或 ``"4E"``
    :param serial: 序列号(仅 4E 使用,0~65535 回绕;3E 忽略)
    :param network_number: 网络编号
    :param pc_number: PC 编号
    :param monitoring_timer: 监视定时器(0~65535)
    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位访问
    :param is_write: 是否写操作
    :param data: 写数据(字单位为逐字 0~65535;位单位为 0/1 序列,长度 = points)
    :param codes: 软元件码表(缺省三菱;基恩士 SLMP 兼容传自有码表)
    :raises ValueError: 帧型/软元件/点数/数据非法
    """
    frame_name = _check_frame(frame)
    core = build_core(address, points, is_bit, is_write, data, codes)

    routing = bytearray()
    routing.append(network_number & 0xFF)
    routing.append(pc_number & 0xFF)
    routing += MC_DEST_MODULE_IO.to_bytes(2, "little")
    routing.append(MC_DEST_MODULE_STATION & 0xFF)
    _check_timer(monitoring_timer)
    body = (
        bytes(routing)
        + (2 + len(core)).to_bytes(2, "little")
        + monitoring_timer.to_bytes(2, "little")
        + bytes(core)
    )

    if frame_name == "4E":
        return MC_SUBHEADER_4E + serial.to_bytes(2, "little") + b"\x00\x00" + body
    return MC_SUBHEADER_3E + body


def parse_response_head(head: bytes, frame: str) -> int:
    """解析响应头,返回应答数据长(TCP 分段收包用)。

    :param head: 已读取的响应头(3E 为 9 字节,4E 为 13 字节)
    :param frame: ``"3E"`` 或 ``"4E"``
    :raises ProtocolFrameError: 帧头过短/副头部非法/长度域非法
    """
    frame_name = _check_frame(frame)
    expected_size = MC_4E_RESPONSE_HEAD_SIZE if frame_name == "4E" else MC_RESPONSE_HEAD_SIZE
    if len(head) < expected_size:
        raise ProtocolFrameError(
            "MC 响应头不足 {} 字节:{}".format(expected_size, len(head))
        )
    wanted = MC_RESPONSE_SUBHEADER_4E if frame_name == "4E" else MC_RESPONSE_SUBHEADER_3E
    if head[0] != wanted or head[1] != 0x00:
        raise ProtocolFrameError(
            "MC 响应副头部非法:0x{:02X} 0x{:02X}".format(head[0], head[1])
        )
    offset = 11 if frame_name == "4E" else 7
    length = int.from_bytes(head[offset:offset + 2], "little")
    if length < 2:
        raise ProtocolFrameError(
            "MC 应答数据长非法(至少含结束码 2 字节):{}".format(length)
        )
    return length


def parse_response(
    frame: bytes,
    frame_type: str,
    points: int,
    is_bit: bool,
    is_read: bool,
    expected_serial: Optional[int] = None,
) -> List[int]:
    """解析 3E/4E 完整响应帧(TCP 拼接帧或 UDP 整包均可)。

    :param frame: 完整响应帧
    :param frame_type: ``"3E"`` 或 ``"4E"``
    :param points: 请求点数(读时校验数据长度)
    :param is_bit: 是否位单位访问
    :param is_read: 是否读操作(写响应无数据)
    :param expected_serial: 期望序列号(仅 4E 校验)
    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 结束代码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/序列号不符
    """
    frame_name = _check_frame(frame_type)
    content_length = parse_response_head(frame, frame_name)
    is_4e = frame_name == "4E"
    head_size = MC_4E_RESPONSE_HEAD_SIZE if is_4e else MC_RESPONSE_HEAD_SIZE
    if len(frame) < head_size + content_length:
        raise ProtocolFrameError(
            "MC 响应帧不完整:期望 {} 字节,实际 {}".format(
                head_size + content_length, len(frame)
            )
        )
    if is_4e:
        if expected_serial is not None:
            serial = int.from_bytes(frame[2:4], "little")
            if serial != expected_serial:
                raise ProtocolFrameError(
                    "MC 序列号不匹配:期望 {},收到 {}".format(expected_serial, serial)
                )
        end_offset = 13
        data_offset = 15
    else:
        end_offset = 9
        data_offset = 11
    end_code = int.from_bytes(frame[end_offset:end_offset + 2], "little")
    if end_code != 0:
        raise DeviceError("MC 结束代码 0x{:04X},详见 MELSEC 手册".format(end_code), end_code)
    if not is_read:
        return []
    expected = (points + 1) // 2 if is_bit else points * 2
    data = frame[data_offset:data_offset + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            "MC 响应数据不足:期望 {} 字节,实际 {}".format(expected, len(data))
        )
    return parse_data(data, points, is_bit)


def _check_frame(frame: str) -> str:
    """帧型归一化与校验(内部函数)。"""
    frame_name = str(frame).strip().upper()
    if frame_name not in _FRAME_NAMES:
        raise ValueError("QnA 兼容帧型必须是 3E/4E,收到:{!r}".format(frame))
    return frame_name


def _check_points(points: int, limit: int) -> None:
    """点数范围校验(内部函数)。"""
    if not 1 <= points <= limit:
        raise ValueError("MC 访问点数超出范围 1~{}:{}".format(limit, points))


def _check_timer(monitoring_timer: int) -> None:
    """监视定时器范围校验(内部函数)。"""
    if not 0 <= monitoring_timer <= 0xFFFF:
        raise ValueError("监视定时器超出范围 0~65535:{}".format(monitoring_timer))


def _write_payload(points: int, is_bit: bool, data: List[int]) -> bytes:
    """写数据编码:位按"每字节 2 位、高位在前"打包;字逐字小端(内部函数)。"""
    if len(data) != points:
        raise ValueError("写数据个数 {} 与点数 {} 不符".format(len(data), points))
    if is_bit:
        packed = bytearray((points + 1) // 2)
        for index, flag in enumerate(data):
            if flag:
                packed[index // 2] |= 0x10 if index % 2 == 0 else 0x01
        return bytes(packed)
    for word in data:
        if not 0 <= word <= 0xFFFF:
            raise ValueError("字写数据超出范围 0~65535:{}".format(word))
    return b"".join(word.to_bytes(2, "little") for word in data)
