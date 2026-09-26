"""欧姆龙 FINS 帧编解码(纯函数)。

帧布局(按欧姆龙 FINS 手册 W340 口径):

- FINS 帧 = ICF(1) + RSV(1) + GCT(1) + DNA/DA1/DA2(3) + SNA/SA1/SA2(3)
  + SID(1) + 命令(2,大端:0101 区域读 / 0102 区域写) + 载荷
- 区域读载荷 = 存储区码(1) + 起始地址(3 = 字地址 2 大端 + 位 1) + 点数(2,大端)
- 区域写载荷 = 同上 + 数据(字:逐字 2 字节大端;位:每点 1 字节 0x00/0x01)
- 响应 = 同帧头(ICF|0xC0)+ 命令 + 结束码(2,大端,0=正常) + 数据
- FINS/TCP 传输帧 = ``"FINS"``(4) + 长度(4,大端,= 后续字节数) + 命令(4)
  + 错误(4) + FINS 帧;数据帧命令 = 2,握手命令 = 0
- 握手:请求 20 字节(本地节点号在末字节);响应 24 字节,错误域之后为
  本地节点(4 字节,末字节)与 PLC 节点(4 字节,末字节)

多字数据为**大端字序**(与 MC 的小端相反)。
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from .address import FinsAddress
from ...core.constants import (
    FINS_COMMAND_AREA_READ,
    FINS_COMMAND_AREA_WRITE,
    FINS_COMMAND_MULTIPLE_AREA_READ,
    FINS_EM_BANK_MAX,
    FINS_EM_BIT_CODE_BASE,
    FINS_EM_WORD_CODE_BASE,
    FINS_END_CODE_OK,
    FINS_END_CODE_CPU_ERROR_FLAGS,
    FINS_END_CODE_HINT,
    FINS_END_CODE_RELAY_ERROR_FLAG,
    FINS_END_CODE_SIZE,
    FINS_END_CODE_TEXT,
    FINS_GCT,
    FINS_HANDSHAKE_LENGTH,
    FINS_HANDSHAKE_RESPONSE_SIZE,
    FINS_HEADER_SIZE,
    FINS_ICF,
    FINS_ICF_RESPONSE,
    FINS_MAX_MULTIPLE_ELEMENTS,
    FINS_MAX_TCP_FRAME,
    FINS_MEMORY_CODES,
    FINS_RSV,
    FINS_TCP_COMMAND_DATA,
    FINS_TCP_COMMAND_HANDSHAKE,
    FINS_TCP_HEADER_SIZE,
    FINS_TCP_MAGIC,
)
from ...core.debug import format_hex
from ...core.errors import DeviceError, ProtocolFrameError


def memory_codes(area: str, bank: int = 0) -> Tuple[int, int]:
    """查存储区码表。

    :param area: 存储区记号(CIO/W/H/A/D/E)
    :param bank: EM 区 bank 号(仅 area 为 ``"E"`` 时使用)
    :return: ``(位操作码, 字操作码)``
    :raises ValueError: 不支持的存储区或 bank 越界
    """
    if area == "E":
        if not 0 <= bank <= FINS_EM_BANK_MAX:
            raise ValueError(
                f"EM 区 bank 号超出范围 0~{FINS_EM_BANK_MAX}:{bank}"
            )
        return FINS_EM_BIT_CODE_BASE + bank, FINS_EM_WORD_CODE_BASE + bank
    try:
        return FINS_MEMORY_CODES[area]
    except KeyError:
        raise ValueError(
            "不支持的 FINS 存储区:{!r},支持:{}".format(area, "/".join(sorted(FINS_MEMORY_CODES)))
        )


def build_area_read(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
    address: FinsAddress,
    count: int,
    is_bit: bool,
) -> bytes:
    """构造 Area Read(0101)FINS 帧。

    :param count: 读取点数(位单位为位数,字单位为字数)
    :raises ValueError: 参数非法
    """
    _check_count(count)
    payload = (
        _area_code(address, is_bit).to_bytes(1, "big")
        + _address_bytes(address)
        + count.to_bytes(2, "big")
    )
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_AREA_READ,
        payload,
    )


def build_area_write(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
    address: FinsAddress,
    data: List[int],
    is_bit: bool,
) -> bytes:
    """构造 Area Write(0102)FINS 帧。

    :param data: 写数据(字单位逐字 0~65535;位单位 0/1 序列)
    :raises ValueError: 参数非法
    """
    _check_count(len(data))
    payload = (
        _area_code(address, is_bit).to_bytes(1, "big")
        + _address_bytes(address)
        + len(data).to_bytes(2, "big")
        + _write_data(data, is_bit)
    )
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_AREA_WRITE,
        payload,
    )


def build_multiple_area_read(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
    entries: Sequence[Tuple[int, int]],
) -> bytes:
    """构造 Multiple Memory Area Read(0104)FINS 帧。

    每条 ``(存储区字码, 字地址)`` 读 **1 个字**(W342 §5-3-5:非连续字
    成批读,仅字码,条目位号恒 0);32/64 位类型由调用方拆成相邻多条。
    Ethernet/Controller Link 单命令上限 :data:`~omniplc.core.constants.
    FINS_MAX_MULTIPLE_ELEMENTS` 条(SYSMAC LINK/DeviceNet 为 89)。

    :raises ValueError: 无条目/条数超限/地址越界
    """
    if not entries:
        raise ValueError("多存储区读至少需要一条 (区码, 字地址)")
    if len(entries) > FINS_MAX_MULTIPLE_ELEMENTS:
        raise ValueError(
            "多存储区读条目数超出上限 {}:{}".format(
                FINS_MAX_MULTIPLE_ELEMENTS, len(entries)
            )
        )
    payload = bytearray()
    for code, offset in entries:
        if not 0 <= offset <= 0xFFFF:
            raise ValueError(f"FINS 字地址超出范围 0~65535:{offset}")
        payload += code.to_bytes(1, "big")
        payload += offset.to_bytes(2, "big")
        payload += b"\x00"
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_MULTIPLE_AREA_READ,
        bytes(payload),
    )


def parse_multiple_area_read(
    frame: bytes, request_frame: bytes, codes: Sequence[int]
) -> List[int]:
    """解析多存储区读响应,返回逐条字数据(大端)。

    响应每条为 ``区码回显(1 字节) + 字数据(2 字节,大端)``,区码
    与请求顺序逐一比对,不符按坏帧处理(流内可能已失步)。

    :param request_frame: 对应的请求 FINS 帧;用于 ICF / SID / 命令码
        回显校验——三者是本次事务的身份标识,不符按串话/迟到处理。

    :raises omniplc.core.errors.DeviceError: 结束码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/ICF·SID·
        命令码·区码回显不符(消息带**收到的原始帧**十六进制转储,
        便于现场与抓包比对)
    """
    _check_identity(frame, request_frame)
    prefix = FINS_HEADER_SIZE + 2
    if len(frame) < prefix + FINS_END_CODE_SIZE:
        raise ProtocolFrameError(
            "FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})".format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if end_code != FINS_END_CODE_OK:
        text = _end_code_text(end_code)
        raise DeviceError(f"FINS 结束码 0x{end_code:04X}({text})", end_code)
    expected = len(codes) * 3
    data = frame[14:14 + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            "FINS 多存储区读响应数据不足:期望 {} 字节,实际 {}(收到的原始帧:{})".format(
                expected, len(data), format_hex(frame)
            )
        )
    words: List[int] = []
    for index, code in enumerate(codes):
        base = index * 3
        echo = data[base]
        if echo != code:
            raise ProtocolFrameError(
                "FINS 多存储区读区码回显不符:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})".format(
                    code, echo, format_hex(frame)
                )
            )
        words.append(int.from_bytes(data[base + 1:base + 3], "big"))
    return words


def parse_response(
    frame: bytes,
    request_frame: bytes,
    count: int,
    is_bit: bool,
    is_read: bool,
) -> List[int]:
    """解析 FINS 帧响应(TCP 载荷或 UDP 整包均可)。

    :param frame: FINS 帧响应
    :param request_frame: 对应的请求 FINS 帧;用于 ICF / SID / 命令码
        回显校验——三者是本次事务的身份标识,不符按串话/迟到处理。
    :param count: 请求点数(读时校验数据长度)
    :param is_bit: 是否位单位访问
    :param is_read: 是否读操作(写响应无数据)
    :return: 读为逐点数据(位 0/1,字 0~65535);写恒为空列表
    :raises omniplc.core.errors.DeviceError: 结束码非 0
    :raises omniplc.core.errors.ProtocolFrameError: 帧结构/ICF·SID·
        命令码不符(消息带**收到的原始帧**十六进制转储,便于现场与抓包比对)
    """
    _check_identity(frame, request_frame)
    prefix = FINS_HEADER_SIZE + 2
    if len(frame) < prefix + FINS_END_CODE_SIZE:
        raise ProtocolFrameError(
            "FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})".format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if end_code != FINS_END_CODE_OK:
        text = _end_code_text(end_code)
        raise DeviceError(f"FINS 结束码 0x{end_code:04X}({text})", end_code)
    if not is_read:
        return []
    expected = count if is_bit else count * 2
    data = frame[14:14 + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            "FINS 响应数据不足:期望 {} 字节,实际 {}(收到的原始帧:{})".format(
                expected, len(data), format_hex(frame)
            )
        )
    if is_bit:
        return [int(byte) for byte in data]
    return [int.from_bytes(data[i:i + 2], "big") for i in range(0, expected, 2)]


# ----------------------------------------------------------------------
# FINS/TCP 封装
# ----------------------------------------------------------------------

def build_handshake(local_node: int) -> bytes:
    """构造 20 字节节点分配握手请求(本地节点号取帧内末字节)。

    :param local_node: 本地节点号(0 = 由 PLC 自动分配)
    :raises ValueError: 节点号非法
    """
    if not 0 <= local_node <= 0xFF:
        raise ValueError(f"本地节点号超出范围 0~255:{local_node}")
    return (
        FINS_TCP_MAGIC
        + FINS_HANDSHAKE_LENGTH.to_bytes(4, "big")
        + FINS_TCP_COMMAND_HANDSHAKE.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00"
        + bytes([local_node])
    )


def parse_tcp_head(head: bytes) -> int:
    """校验 FINS/TCP 帧头(8 字节)并返回长度域(后续字节数)。

    :raises ProtocolFrameError: 魔数不符、帧头过短或长度域超限
        (消息带**收到的原始帧头**十六进制转储,便于现场与抓包比对)
    """
    if len(head) < FINS_TCP_HEADER_SIZE or head[:4] != FINS_TCP_MAGIC:
        raise ProtocolFrameError(
            "FINS/TCP 帧头非法:{}(收到的原始数据:{})".format(
                head.hex(), format_hex(head)
            )
        )
    length = int.from_bytes(head[4:8], "big")
    # FINS 帧最小 = 头(10) + 命令(2) + 结束码(2) = 14 字节;长度域
    # 小于此即坏帧,直接报错定位前移(避免下游 ``recv(0)`` 阻塞到超时)。
    min_fins_frame = FINS_HEADER_SIZE + 2 + FINS_END_CODE_SIZE
    if length < min_fins_frame:
        raise ProtocolFrameError(
            "FINS/TCP 长度域过短:{} < 最小 FINS 帧 {} 字节(收到的原始帧头:{})".format(
                length, min_fins_frame, format_hex(head)
            )
        )
    if length > FINS_MAX_TCP_FRAME:
        raise ProtocolFrameError(
            "FINS/TCP 长度域超限:{} > {}(收到的原始帧头:{})".format(
                length, FINS_MAX_TCP_FRAME, format_hex(head)
            )
        )
    return length


def parse_handshake_response(frame: bytes) -> Tuple[int, int]:
    """解析握手响应,返回 ``(本地节点号, PLC 节点号)``。

    :raises ProtocolFrameError: 帧不完整或握手错误码非 0
        (消息带**收到的原始帧**十六进制转储,便于现场与抓包比对)
    """
    if len(frame) < FINS_HANDSHAKE_RESPONSE_SIZE:
        raise ProtocolFrameError(
            "FINS/TCP 握手响应不完整:期望 {} 字节,实际 {}(收到的原始帧:{})".format(
                FINS_HANDSHAKE_RESPONSE_SIZE, len(frame), format_hex(frame)
            )
        )
    error = int.from_bytes(frame[12:16], "big")
    if error != 0:
        raise ProtocolFrameError(
            "FINS/TCP 握手失败,错误码 0x{:08X}(收到的原始帧:{})".format(
                error, format_hex(frame)
            )
        )
    return frame[19], frame[23]


def build_tcp_frame(fins_frame: bytes) -> bytes:
    """把 FINS 帧封装为 FINS/TCP 数据帧(命令 2,长度 = 后续字节数)。"""
    body = (
        FINS_TCP_COMMAND_DATA.to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + fins_frame
    )
    return FINS_TCP_MAGIC + len(body).to_bytes(4, "big") + body


def extract_tcp_error(content: bytes) -> None:
    """校验 FINS/TCP 数据帧的错误域(帧内偏移 4~8,大端 4 字节)。

    :raises ProtocolFrameError: 错误码非 0(消息带收到的原始数据转储)
    """
    if len(content) < 8:
        raise ProtocolFrameError(
            "FINS/TCP 数据帧过短:{} 字节(收到的原始数据:{})".format(
                len(content), format_hex(content)
            )
        )
    error = int.from_bytes(content[4:8], "big")
    if error != 0:
        raise ProtocolFrameError(
            "FINS/TCP 错误码 0x{:08X}(收到的原始数据:{})".format(
                error, format_hex(content)
            )
        )


def extract_tcp_payload(content: bytes) -> bytes:
    """取 FINS/TCP 数据帧中的 FINS 帧(偏移 8 起)。

    :raises ProtocolFrameError: 内容过短(消息带收到的原始数据转储)
    """
    if len(content) < 8:
        raise ProtocolFrameError(
            "FINS/TCP 数据帧过短:{} 字节(收到的原始数据:{})".format(
                len(content), format_hex(content)
            )
        )
    return content[8:]


# ----------------------------------------------------------------------
# 内部函数
# ----------------------------------------------------------------------

def _end_code_text(end_code: int) -> str:
    """结束码 → 可读文本,先屏蔽标志位再查表(内部函数)。

    W342-E1-18 §5-1-3:结束码 = 主码(高字节)+ 子码(低字节);主码 bit15
    表示**网络中继错误**,子码 bit7/6 表示**目标 CPU 单元出错**。这些位与
    主/子码值叠加(如 ``0x9005`` = 网络中继错误标志 + 主码 0x10/子码 0x05
    「头错误」),故查表前须屏蔽,否则会落入"未知码"。标志同时附在文本末尾。
    """
    flags = []
    if end_code & FINS_END_CODE_RELAY_ERROR_FLAG:
        flags.append("网络中继错误")
    if end_code & FINS_END_CODE_CPU_ERROR_FLAGS:
        flags.append("目标 CPU 单元出错")
    base = end_code & ~(FINS_END_CODE_RELAY_ERROR_FLAG | FINS_END_CODE_CPU_ERROR_FLAGS)
    text = FINS_END_CODE_TEXT.get(base, "详见 Omron FINS 手册")
    if flags:
        text = "{} [{}]".format(text, "、".join(flags))
    hint = FINS_END_CODE_HINT.get(base)
    if hint:
        text = "{};现场排查:{}".format(text, hint)
    return text


def _build_frame(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
    command: int,
    payload: bytes,
) -> bytes:
    """组装 10 字节 FINS 帧头 + 命令 + 载荷(内部函数)。"""
    head = bytes(
        [
            FINS_ICF,
            FINS_RSV,
            FINS_GCT,
            destination_network & 0xFF,
            destination_node & 0xFF,
            destination_unit & 0xFF,
            source_network & 0xFF,
            source_node & 0xFF,
            source_unit & 0xFF,
            sid & 0xFF,
        ]
    )
    return head + command.to_bytes(2, "big") + payload


def _area_code(address: FinsAddress, is_bit: bool) -> int:
    """按访问单位取存储区码(内部函数)。"""
    bit_code, word_code = memory_codes(address.area, address.bank)
    return bit_code if is_bit else word_code


def _address_bytes(address: FinsAddress) -> bytes:
    """起始地址 3 字节:字地址 2 字节大端 + 位号 1 字节(内部函数)。"""
    if not 0 <= address.offset <= 0xFFFF:
        raise ValueError(f"FINS 字地址超出范围 0~65535:{address.offset}")
    bit = address.bit if address.bit is not None else 0
    return address.offset.to_bytes(2, "big") + bytes([bit])


def _write_data(data: List[int], is_bit: bool) -> bytes:
    """写数据编码:位每点 1 字节 0x00/0x01;字逐字大端(内部函数)。"""
    if is_bit:
        return bytes([1 if flag else 0 for flag in data])
    for word in data:
        if not 0 <= word <= 0xFFFF:
            raise ValueError(f"字写数据超出范围 0~65535:{word}")
    return b"".join(word.to_bytes(2, "big") for word in data)


def _check_count(count: int) -> None:
    """点数范围校验(内部函数)。"""
    if not 1 <= count <= 0xFFFF:
        raise ValueError(f"FINS 访问点数超出范围 1~65535:{count}")


def _check_identity(frame: bytes, request_frame: bytes) -> None:
    """校验应答帧与请求帧的 ICF / SID / 命令码三者回显(内部函数)。

    串话(局域网其他 FINS 设备)/ 迟到响应(超时后到达的过期数据)
    在单锁模型下风险较低,但 UDP 无源地址过滤时仍有混入可能——
    三层事务标识回显校验作为最后一道防线,任何一项不符即按坏帧处理。
    """
    expected_sid = request_frame[9]
    expected_command = int.from_bytes(request_frame[10:12], "big")
    # 先做长度快速判断再回显比对,避免 IndexError 掩盖真正问题
    if len(frame) < FINS_HEADER_SIZE + 2:
        raise ProtocolFrameError(
            "FINS 响应不完整(无法核对 ICF/SID/命令码):至少 {} 字节,实际 {}(收到的原始帧:{})".format(
                FINS_HEADER_SIZE + 2, len(frame), format_hex(frame)
            )
        )
    if frame[0] != FINS_ICF_RESPONSE:
        raise ProtocolFrameError(
            "FINS 响应 ICF 非法:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})".format(
                FINS_ICF_RESPONSE, frame[0], format_hex(frame)
            )
        )
    if frame[9] != expected_sid:
        raise ProtocolFrameError(
            "FINS 响应 SID 回显不符:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})".format(
                expected_sid, frame[9], format_hex(frame)
            )
        )
    if frame[10:12] != expected_command.to_bytes(2, "big"):
        actual_command = int.from_bytes(frame[10:12], "big")
        raise ProtocolFrameError(
            "FINS 响应命令码回显不符:期望 0x{:04X},收到 0x{:04X}(收到的原始帧:{})".format(
                expected_command, actual_command, format_hex(frame)
            )
        )
