"""三菱 MC 协议 A 兼容 1E 帧编解码(纯函数)。

1E 帧用于 A 系列及以太网单元,帧结构比 3E/4E 更短(无网络号/PC 号
路由字段),软元件码表也不同。帧布局(按 MELSEC 手册 1E 帧格式口径):

- 请求 = 副头部(1) + PLC号/站号(1) + 监视定时器(2,小端) + 设备编号(4,小端;
  本库支持 ≤0xFFFF,高 2 字节恒 0) + 软元件码(2,小端) + 点数(2,小端) + [写数据]
- 副头部:位读 0x00 / 字读 0x01 / 位写 0x02 / 字写 0x03
- 响应 = 副头部(请求值 + 0x80)(1) + 结束代码(1,0=成功) + [数据]
  位数据每字节 2 位、高位在前;字数据逐字小端
- 结束代码 0x5B 的响应后跟 1 字节异常细分码(SH-080008 §18.2 印刷页 395
  算例 ``5BH 10H``;TCP 接收需多读,防止字节流错位)
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .address import McAddress
from .codec_qna import reject_bit_suffix_on_bit_device
from ...core.constants import (
    MC_1E_DEVICE_CODES,
    MC_1E_ERROR_EXTRA,
    MC_1E_ERROR_EXTRA_SIZE,
    MC_1E_MAX_POINTS,
    MC_1E_READ_BIT,
    MC_1E_READ_WORD,
    MC_1E_RESPONSE_HEAD_SIZE,
    MC_1E_WRITE_BIT,
    MC_1E_WRITE_WORD,
)
from ...core.errors import DeviceError, ProtocolFrameError
from ...core.i18n import _


def device_info(device: str) -> Tuple[int, bool, int]:
    """查 1E 软元件码表。

    :return: ``(两字节软元件码, 是否位软元件, 地址进制)``
    :raises ValueError: 不支持的软元件
    """
    try:
        code, is_bit, base = MC_1E_DEVICE_CODES[device]
    except KeyError:
        raise ValueError(
            _("1E 帧不支持的软元件:{!r},支持:{}").format(
                device, "/".join(sorted(MC_1E_DEVICE_CODES))
            )
        )
    return code, bool(is_bit), base


def device_number(device: str, number: str, base: int) -> int:
    """把用户编号数字原文换算为报文中的软元件编号(按码表进制)。

    :raises ValueError: 编号按目标进制解析失败或超出范围
    """
    try:
        value = int(number, base)
    except ValueError:
        raise ValueError(
            _("软元件 {} 编号按 {} 进制解析失败:{!r}").format(device, base, number)
        )
    if value > 0xFFFF:
        raise ValueError(_("1E 软元件编号超出 2 字节范围:{}").format(value))
    return value


def build_request(
    pc_number: int,
    monitoring_timer: int,
    address: McAddress,
    points: int,
    is_bit: bool,
    is_write: bool,
    data: Optional[List[int]] = None,
) -> bytes:
    """构造 1E 帧请求。

    :param pc_number: PLC 号/站号(0~255)
    :param monitoring_timer: 监视定时器
    :param address: 软元件地址
    :param points: 访问点数
    :param is_bit: 是否位单位访问
    :param is_write: 是否写操作
    :param data: 写数据(字单位逐字 0~65535;位单位 0/1 序列,长度 = points)
    :raises ValueError: 软元件/点数/数据非法
    """
    code, is_bit_device, base = device_info(address.device)
    if is_bit and not is_bit_device:
        raise ValueError(
            _("字软元件 {} 不支持位单位成批访问,请按字访问后提取位").format(address.device)
        )
    if is_bit and is_bit_device:
        reject_bit_suffix_on_bit_device(address)
    if not 1 <= points <= MC_1E_MAX_POINTS:
        raise ValueError(_("1E 访问点数超出范围 1~{}:{}").format(MC_1E_MAX_POINTS, points))
    number = device_number(address.device, address.number, base)
    if not is_bit and is_bit_device and number % 16 != 0:
        raise ValueError(
            _("1E 字单位访问位软元件要求首编号为 16 的倍数:{}{}").format(
                address.device, address.number
            )
        )

    if is_write:
        subtitle = MC_1E_WRITE_BIT if is_bit else MC_1E_WRITE_WORD
    else:
        subtitle = MC_1E_READ_BIT if is_bit else MC_1E_READ_WORD
    request = bytearray()
    request.append(subtitle)
    request.append(pc_number & 0xFF)
    request += monitoring_timer.to_bytes(2, "little")
    request += number.to_bytes(2, "little")
    request += b"\x00\x00"
    request += code.to_bytes(2, "little")
    request += points.to_bytes(2, "little")
    if is_write:
        request += _write_payload(points, is_bit, data or [])
    return bytes(request)


def parse_response(frame: bytes, points: int, is_bit: bool, is_read: bool) -> List[int]:
    """解析 1E 帧完整响应(TCP 拼接帧或 UDP 整包均可)。

    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 结束代码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构不符
    """
    if len(frame) < MC_1E_RESPONSE_HEAD_SIZE:
        raise ProtocolFrameError(
            _("1E 响应头不足 {} 字节:{}").format(MC_1E_RESPONSE_HEAD_SIZE, len(frame))
        )
    expected_head = (
        (MC_1E_READ_BIT if is_bit else MC_1E_READ_WORD)
        if is_read
        else (MC_1E_WRITE_BIT if is_bit else MC_1E_WRITE_WORD)
    ) + 0x80
    if frame[0] != expected_head:
        raise ProtocolFrameError(
            _("1E 响应副头部不符:期望 0x{:02X},收到 0x{:02X}").format(expected_head, frame[0])
        )
    end_code = frame[1]
    if end_code != 0:
        if end_code == MC_1E_ERROR_EXTRA and len(frame) < MC_1E_RESPONSE_HEAD_SIZE + MC_1E_ERROR_EXTRA_SIZE:
            raise ProtocolFrameError(_("1E 错误响应缺少扩展信息字节"))
        raise DeviceError(_("MC(1E) 结束代码 0x{:02X},详见 A 系列手册").format(end_code), end_code)
    if not is_read:
        if len(frame) != MC_1E_RESPONSE_HEAD_SIZE:
            # 1E 帧无长度域,写响应成功时应答恰为 2 字节头;多余字节滞留
            # TCP 缓冲会被下一事务当响应头消费(串帧),UDP 整包路径下
            # 属数据报边界异常——统一按坏帧拒绝
            raise ProtocolFrameError(
                _("1E 写响应尾部有冗余字节:期望 {} 字节,实际 {}").format(
                    MC_1E_RESPONSE_HEAD_SIZE, len(frame)
                )
            )
        return []
    expected = (points + 1) // 2 if is_bit else points * 2
    data = frame[2:2 + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            _("1E 响应数据不足:期望 {} 字节,实际 {}").format(expected, len(data))
        )
    if len(frame) != MC_1E_RESPONSE_HEAD_SIZE + expected:
        raise ProtocolFrameError(
            _("1E 响应尾部有冗余字节:期望 {} 字节,实际 {}").format(
                MC_1E_RESPONSE_HEAD_SIZE + expected, len(frame)
            )
        )
    if is_bit:
        return [
            1 if data[index // 2] & (0x10 if index % 2 == 0 else 0x01) else 0
            for index in range(points)
        ]
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, expected, 2)]


def _write_payload(points: int, is_bit: bool, data: List[int]) -> bytes:
    """写数据编码:位按"每字节 2 位、高位在前"打包;字逐字小端(内部函数)。"""
    if len(data) != points:
        raise ValueError(_("写数据个数 {} 与点数 {} 不符").format(len(data), points))
    if is_bit:
        packed = bytearray((points + 1) // 2)
        for index, flag in enumerate(data):
            if flag:
                packed[index // 2] |= 0x10 if index % 2 == 0 else 0x01
        return bytes(packed)
    for word in data:
        if not 0 <= word <= 0xFFFF:
            raise ValueError(_("字写数据超出范围 0~65535:{}").format(word))
    return b"".join(word.to_bytes(2, "little") for word in data)
