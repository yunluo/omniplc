"""丰田 TOYOPUC 计算机链接二进制帧编解码。

帧格式(TOYOPUC 计算机链接,二进制):

- 命令帧:``00 00 LL LH CMD [数据...]``,帧长(LL/LH,小端)= CMD(1) + 数据字节数
- 响应帧:``80 RC LL LH CMD [数据...]``;``RC=00`` 正常,``RC=10`` 命令出错,
  详细出错代码在 CMD 字节(无数据时)或数据末字节
- 地址与点数为 16 位小端;多字数据小端、低字在前(64 位类型同理)
- 命令:1C/1D 连续字读/写、1E/1F 连续字节读/写、20/21 单位读/写
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ...core.constants import (
    TOYOPUC_CMD_BIT_READ,
    TOYOPUC_CMD_BIT_WRITE,
    TOYOPUC_CMD_BYTE_READ,
    TOYOPUC_CMD_BYTE_WRITE,
    TOYOPUC_CMD_WORD_READ,
    TOYOPUC_CMD_WORD_WRITE,
    TOYOPUC_ERROR_TEXT,
    TOYOPUC_FT_COMMAND,
    TOYOPUC_FT_RESPONSE,
    TOYOPUC_MAX_BYTE_COUNT,
    TOYOPUC_MAX_WORD_COUNT,
    TOYOPUC_RC_ERROR,
    TOYOPUC_RC_OK,
)
from ...core.debug import format_hex
from ...core.errors import DeviceError, ProtocolFrameError


def build_command(cmd: int, data: bytes = b"") -> bytes:
    """把命令与数据编码为命令帧(内部函数)。

    :raises ProtocolFrameError: 命令码非法或数据超出 16 位帧长
    """
    if not 0 <= cmd <= 0xFF:
        raise ProtocolFrameError(f"TOYOPUC 命令码非法:0x{cmd:X}")
    payload = bytes(data)
    length = 1 + len(payload)
    if length > 0xFFFF:
        raise ProtocolFrameError("TOYOPUC 命令数据超出 16 位帧长:{}".format(len(payload)))
    return bytes((TOYOPUC_FT_COMMAND, 0x00, length & 0xFF, length >> 8, cmd)) + payload


def pack_u16(value: int) -> bytes:
    """16 位无符号整数 → 小端两字节(越界抛错,内部函数)。"""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"16 位无符号数值越界:{value}")
    return bytes((value & 0xFF, value >> 8))


def unpack_u16(data: bytes) -> List[int]:
    """小端字节串 → 16 位无符号整数列表(长度必须为偶数,内部函数)。"""
    if len(data) % 2 != 0:
        raise ProtocolFrameError(
            "TOYOPUC 字数据长度必须为偶数,收到 {}(收到的原始数据:{})".format(
                len(data), format_hex(data)
            )
        )
    return [data[index] | (data[index + 1] << 8) for index in range(0, len(data), 2)]


def build_word_read(address: int, count: int) -> bytes:
    """构造连续字读命令帧(CMD=1C)。"""
    if not 1 <= count <= TOYOPUC_MAX_WORD_COUNT:
        raise ValueError(f"连续字读取点数必须在 1~{TOYOPUC_MAX_WORD_COUNT},收到:{count}")
    return build_command(TOYOPUC_CMD_WORD_READ, pack_u16(address) + pack_u16(count))


def build_word_write(address: int, values: Sequence[int]) -> bytes:
    """构造连续字写命令帧(CMD=1D)。"""
    if not 1 <= len(values) <= TOYOPUC_MAX_WORD_COUNT:
        raise ValueError("连续字写入点数必须在 1~{},收到:{}".format(TOYOPUC_MAX_WORD_COUNT, len(values)))
    payload = pack_u16(address) + b"".join(pack_u16(value) for value in values)
    return build_command(TOYOPUC_CMD_WORD_WRITE, payload)


def build_byte_read(address: int, count: int) -> bytes:
    """构造连续字节读命令帧(CMD=1E)。"""
    if not 1 <= count <= TOYOPUC_MAX_BYTE_COUNT:
        raise ValueError(f"连续字节读取点数必须在 1~{TOYOPUC_MAX_BYTE_COUNT},收到:{count}")
    return build_command(TOYOPUC_CMD_BYTE_READ, pack_u16(address) + pack_u16(count))


def build_byte_write(address: int, values: bytes) -> bytes:
    """构造连续字节写命令帧(CMD=1F)。"""
    payload = bytes(values)
    if not 1 <= len(payload) <= TOYOPUC_MAX_BYTE_COUNT:
        raise ValueError("连续字节写入点数必须在 1~{},收到:{}".format(TOYOPUC_MAX_BYTE_COUNT, len(payload)))
    return build_command(TOYOPUC_CMD_BYTE_WRITE, pack_u16(address) + payload)


def build_bit_read(address: int) -> bytes:
    """构造单位读命令帧(CMD=20)。"""
    return build_command(TOYOPUC_CMD_BIT_READ, pack_u16(address))


def build_bit_write(address: int, value: bool) -> bytes:
    """构造单位写命令帧(CMD=21)。"""
    return build_command(TOYOPUC_CMD_BIT_WRITE, pack_u16(address) + bytes((1 if value else 0,)))


def parse_response(frame: bytes) -> Tuple[int, int, bytes]:
    """解码响应帧,返回 ``(CMD, RC, 数据)``。

    :raises ProtocolFrameError: 帧过短、FT 非法或帧长不一致
        (消息带**收到的原始帧**十六进制转储,便于现场与抓包比对)
    """
    if len(frame) < 5:
        raise ProtocolFrameError(
            "TOYOPUC 响应过短:{} 字节(收到的原始帧:{})".format(
                len(frame), format_hex(frame)
            )
        )
    frame_type = frame[0]
    rc = frame[1]
    length = frame[2] | (frame[3] << 8)
    if len(frame) != 4 + length:
        raise ProtocolFrameError(
            "TOYOPUC 响应帧长不符:收到 {},应为 {}(收到的原始帧:{})".format(
                len(frame), 4 + length, format_hex(frame)
            )
        )
    if frame_type != TOYOPUC_FT_RESPONSE:
        raise ProtocolFrameError(
            "TOYOPUC 响应 FT 非法:0x{:02X}(收到的原始帧:{})".format(
                frame_type, format_hex(frame)
            )
        )
    return frame[4], rc, frame[5:]


def check_response(
    cmd: int,
    rc: int,
    data: bytes,
    request_cmd: int,
    expected_size: Optional[int],
    whole: bytes = b"",
) -> bytes:
    """校验响应并返回数据(内部函数)。

    - ``RC != 00`` → :class:`DeviceError`(``RC=10`` 提取详细出错代码)
    - 命令字与请求不符 / 读数据长度不符 → :class:`ProtocolFrameError`
      (``whole`` 非空时随消息转储**收到的原始帧**,便于现场比对)

    :param whole: 完整响应帧(可选;调用方传原始响应以便坏帧诊断)
    """
    detail_suffix = "(收到的原始帧:{})".format(format_hex(whole)) if whole else ""
    if rc != TOYOPUC_RC_OK:
        if rc == TOYOPUC_RC_ERROR:
            detail = data[-1] if data else cmd
            raise DeviceError(
                "TOYOPUC 出错 0x{:02X}:{}".format(
                    detail, TOYOPUC_ERROR_TEXT.get(detail, "未知错误,请查阅 TOYOPUC 手册")
                ),
                detail,
            )
        raise DeviceError(
            f"TOYOPUC 响应出错 RC=0x{rc:02X},请查阅 TOYOPUC 手册", rc
        )
    if cmd != request_cmd:
        raise ProtocolFrameError(
            "TOYOPUC 响应命令字不符:期望 0x{:02X},收到 0x{:02X}{}".format(
                request_cmd, cmd, detail_suffix
            )
        )
    if expected_size is not None and len(data) != expected_size:
        raise ProtocolFrameError(
            "TOYOPUC 响应数据长度不符:期望 {},收到 {}{}".format(
                expected_size, len(data), detail_suffix
            )
        )
    return data
