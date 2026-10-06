"""欧姆龙 FINS 帧编解码(纯函数)。

帧布局(依据:W342-E1-18 §3-3-3 帧内各字段定义 p.33-34 / §5-3-2·§5-3-3
命令帧 p.171-175 / §5-3-5 多存储区读 p.177-179 / §5-1-3 结束码 p.155-162):

- FINS 帧 = ICF(1) + RSV(1) + GCT(1) + DNA/DA1/DA2(3) + SNA/SA1/SA2(3)
  + SID(1) + 命令(2,大端:0101 区域读 / 0102 区域写) + 载荷
- 区域读载荷 = 存储区码(1) + 起始地址(3 = 字地址 2 大端 + 位 1) + 点数(2,大端)
- 区域写载荷 = 同上 + 数据(字:逐字 2 字节大端;位:每点 1 字节 0x00/0x01)
- 响应 = 同帧头(ICF|0xC0)+ 命令 + 结束码(2,大端,0=正常) + 数据
- FINS/TCP 传输帧 = ``"FINS"``(4) + 长度(4,大端,= 后续字节数) + 命令(4)
  + 错误(4) + FINS 帧;数据帧命令 = 2,握手命令 = 0
  (依据待补:W342 不含 FINS/TCP 封装,见 docs/protocol/README.md「待补」)
- 握手:请求 20 字节(本地节点号在末字节);响应 24 字节,错误域之后为
  本地节点(4 字节,末字节)与 PLC 节点(4 字节,末字节)

多字值(32/64 位)为**字内大端、低字在前**(低字存低地址;与 MC 的小端
不同——MC 是字内小端;2026-09-30 修正,原误作"整体大端字序")。
"""

from __future__ import annotations

import datetime
import struct
from typing import Dict, List, NamedTuple, Sequence, Tuple

from .address import FinsAddress
from ...core.constants import (
    FINS_COMMAND_AREA_READ,
    FINS_COMMAND_AREA_WRITE,
    FINS_COMMAND_CLOCK_READ,
    FINS_COMMAND_CLOCK_WRITE,
    FINS_COMMAND_CPU_UNIT_STATUS_READ,
    FINS_COMMAND_MULTIPLE_AREA_READ,
    FINS_EM_BANK_MAX,
    FINS_EM_BIT_CODE_BASE,
    FINS_EM_WORD_CODE_BASE,
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
    FINS_TCP_COMMAND_HANDSHAKE_RESPONSE,
    FINS_TCP_HEADER_SIZE,
    FINS_TCP_MAGIC,
)
from ...core.debug import format_hex
from ...core.errors import DeviceError, ProtocolFrameError
from ...core.i18n import _


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
                _("EM 区 bank 号超出范围 0~{}:{}").format(FINS_EM_BANK_MAX, bank)
            )
        return FINS_EM_BIT_CODE_BASE + bank, FINS_EM_WORD_CODE_BASE + bank
    try:
        return FINS_MEMORY_CODES[area]
    except KeyError:
        raise ValueError(
            _("不支持的 FINS 存储区:{!r},支持:{}").format(
                area, "/".join(sorted(FINS_MEMORY_CODES))
            )
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
        raise ValueError(_("多存储区读至少需要一条 (区码, 字地址)"))
    if len(entries) > FINS_MAX_MULTIPLE_ELEMENTS:
        raise ValueError(
            _("多存储区读条目数超出上限 {}:{}").format(
                FINS_MAX_MULTIPLE_ELEMENTS, len(entries)
            )
        )
    payload = bytearray()
    # 条目定长 4 字节(区码 1 + 字地址 2 大端 + 保留 0x00),校验后一次组包
    # (逐条目三次 to_bytes 追加换出,上限 167 条,review-1019 P3-7)
    flat: List[int] = []
    for code, offset in entries:
        if not 0 <= offset <= 0xFFFF:
            raise ValueError(_("FINS 字地址超出范围 0~65535:{}").format(offset))
        flat.extend((code, offset >> 8, offset & 0xFF, 0))
    payload += struct.pack(f">{len(flat)}B", *flat)
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
            _("FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})").format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if not _is_normal_end_code(end_code):
        text = _(_end_code_text(end_code))
        raise DeviceError(
            _("FINS 结束码 0x{:04X}({})").format(end_code, text), end_code
        )
    expected = len(codes) * 3
    total = prefix + FINS_END_CODE_SIZE + expected
    if len(frame) != total:
        raise ProtocolFrameError(
            _(
                "FINS 多存储区读响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})"
            ).format(total, len(frame), format_hex(frame))
        )
    data = frame[14 : 14 + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            _(
                "FINS 多存储区读响应数据不足:期望 {} 字节,实际 {}(收到的原始帧:{})"
            ).format(expected, len(data), format_hex(frame))
        )
    words: List[int] = []
    # 条目 3 字节定长(区码回显 1 + 字数据 2 大端),unpack_from 免切片分配
    for index, code in enumerate(codes):
        base = index * 3
        echo = data[base]
        if echo != code:
            raise ProtocolFrameError(
                _(
                    "FINS 多存储区读区码回显不符:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})"
                ).format(code, echo, format_hex(frame))
            )
        words.append(struct.unpack_from(">H", data, base + 1)[0])
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
            _("FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})").format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if not _is_normal_end_code(end_code):
        text = _(_end_code_text(end_code))
        raise DeviceError(
            _("FINS 结束码 0x{:04X}({})").format(end_code, text), end_code
        )
    if not is_read:
        total = prefix + FINS_END_CODE_SIZE
        if len(frame) != total:
            raise ProtocolFrameError(
                _("FINS 写响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                    total, len(frame), format_hex(frame)
                )
            )
        return []
    expected = count if is_bit else count * 2
    total = prefix + FINS_END_CODE_SIZE + expected
    if len(frame) != total:
        raise ProtocolFrameError(
            _("FINS 响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                total, len(frame), format_hex(frame)
            )
        )
    data = frame[14 : 14 + expected]
    if len(data) != expected:
        raise ProtocolFrameError(
            _("FINS 响应数据不足:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                expected, len(data), format_hex(frame)
            )
        )
    if is_bit:
        return [int(byte) for byte in data]
    # 一次 struct.unpack(逐字切片 + from_bytes 换出,review-1019 P2-4)
    return list(struct.unpack(f">{expected // 2}H", data))


# ----------------------------------------------------------------------
# CPU Unit Status Read(0601,探活/状态读)
# ----------------------------------------------------------------------


def build_cpu_unit_status_read(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
) -> bytes:
    """构造 CPU Unit Status Read(0601)FINS 帧。

    依据:W342 §5-3-17 印刷页 194——命令格式仅命令码 ``06 01`` 两字节,
    无参数(零副作用,用作探活探测命令)。
    """
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_CPU_UNIT_STATUS_READ,
        b"",
    )


def parse_cpu_unit_status_read(frame: bytes, request_frame: bytes) -> Dict[str, object]:
    """解析 CPU Unit Status Read(0601)响应。

    依据:W342 §5-3-17 印刷页 194-196——响应 = 结束码(2B)+ Status(1B,
    bit0 运行/停止、bit1 内置 Flash 写入、bit2 电池、bit7 CPU 待机,
    bit3~6 未定义)+ Mode(1B,00=PROGRAM/02=MONITOR/04=RUN)+ 致命错(2B)
    + 非致命错(2B)+ MSG 显示标志(2B)+ 错误码(2B)+ 错误消息(16B
    ASCII,FAL/FALS 文本,无则 16 空格)。

    :raises ProtocolFrameError: 帧结构/ICF·SID·命令码不符或数据长度不符
        (消息带原始帧十六进制转储)
    :raises DeviceError: 结束码非正常完成
    """
    _check_identity(frame, request_frame)
    prefix = FINS_HEADER_SIZE + 2
    # 结束码先于数据长度校验(与 parse_response 同口径):错误应答只带
    # 结束码、不带数据,按结束码抛 DeviceError 而非误判坏帧
    if len(frame) < prefix + FINS_END_CODE_SIZE:
        raise ProtocolFrameError(
            _("FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})").format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if not _is_normal_end_code(end_code):
        text = _(_end_code_text(end_code))
        raise DeviceError(
            _("FINS 结束码 0x{:04X}({})").format(end_code, text), end_code
        )
    data_total = 1 + 1 + 2 + 2 + 2 + 2 + 16
    total = prefix + FINS_END_CODE_SIZE + data_total
    if len(frame) != total:
        raise ProtocolFrameError(
            _("FINS 0601 响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                total, len(frame), format_hex(frame)
            )
        )
    data = frame[prefix + FINS_END_CODE_SIZE :]
    return {
        "status": int(data[0]),
        "run": bool(data[0] & 0x01),
        "mode": int(data[1]),
        "fatal_error": int.from_bytes(data[2:4], "big"),
        "nonfatal_error": int.from_bytes(data[4:6], "big"),
        "message_flags": int.from_bytes(data[6:8], "big"),
        "error_code": int.from_bytes(data[8:10], "big"),
        "error_message": data[10:26].decode("ascii", errors="replace").rstrip(),
    }


# ----------------------------------------------------------------------
# 时钟读/写(0701 / 0702)
# ----------------------------------------------------------------------


class FinsClock(NamedTuple):
    """FINS PLC 时钟值(0701 读 / 0702 写,共 7 字段)。

    :ivar year: 年,**右两位原文**(0~99;W342 §5-3-19 印刷页 198:
        1998/1999/2000 → 98/99/00,2096/2097 → 96/97)——世纪映射是
        调用方责任,本库不猜世纪
    :ivar month: 月 1~12
    :ivar day: 日 1~31
    :ivar hour: 时 0~23
    :ivar minute: 分 0~59
    :ivar second: 秒 0~59
    :ivar day_of_week: 星期 0~6(0 = 星期日;PLC 不校验星期与日期一致)
    """

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    day_of_week: int

    @classmethod
    def from_datetime(cls, value: datetime.datetime) -> "FinsClock":
        """从 :class:`datetime.datetime` 构造(年取右两位,星期按周日 = 0 换算)。"""
        return cls(
            year=value.year % 100,
            month=value.month,
            day=value.day,
            hour=value.hour,
            minute=value.minute,
            second=value.second,
            # datetime.weekday():周一 = 0;FINS:周日 = 0
            day_of_week=(value.weekday() + 1) % 7,
        )

    def to_datetime(self, century: int = 2000) -> datetime.datetime:
        """转 :class:`datetime.datetime`(年份 = ``century + 两位年``,默认 2000+)。

        :param century: 世纪基值;跨世纪时钟(如 99 → 1999)请显式传 1900
        """
        return datetime.datetime(
            century + self.year,
            self.month,
            self.day,
            self.hour,
            self.minute,
            self.second,
        )


def _bcd_decode(value: int, frame: bytes) -> int:
    """单字节 BCD 解码(0x25 → 25;内部函数)。

    :raises ProtocolFrameError: 字节非 BCD(高/低半字节 > 9,坏数据)
    """
    if (value >> 4) > 9 or (value & 0x0F) > 9:
        raise ProtocolFrameError(
            _("FINS 0701 时钟字段非 BCD 编码:0x{:02X}(收到的原始帧:{})").format(
                value, format_hex(frame)
            )
        )
    return (value >> 4) * 10 + (value & 0x0F)


def _bcd_encode(value: int) -> bytes:
    """单字节 BCD 编码(25 → 0x25;内部函数,调用方已做范围校验)。"""
    return bytes([(value // 10) << 4 | (value % 10)])


def validate_clock(clock: FinsClock) -> None:
    """校验时钟字段范围(入参期收口,同步/native 写入路径共用)。

    :raises ValueError: 任一字段越界(月 1~12 / 日 1~31 / 时 0~23 /
        分·秒 0~59 / 星期 0~6 / 年 0~99)
    """
    if not 0 <= clock.year <= 99:
        raise ValueError(_("时钟年份须为右两位 0~99,收到:{}").format(clock.year))
    if not 1 <= clock.month <= 12:
        raise ValueError(_("时钟月份须为 1~12,收到:{}").format(clock.month))
    if not 1 <= clock.day <= 31:
        raise ValueError(_("时钟日期须为 1~31,收到:{}").format(clock.day))
    if not 0 <= clock.hour <= 23:
        raise ValueError(_("时钟小时须为 0~23,收到:{}").format(clock.hour))
    if not 0 <= clock.minute <= 59:
        raise ValueError(_("时钟分钟须为 0~59,收到:{}").format(clock.minute))
    if not 0 <= clock.second <= 59:
        raise ValueError(_("时钟秒须为 0~59,收到:{}").format(clock.second))
    if not 0 <= clock.day_of_week <= 6:
        raise ValueError(
            _("时钟星期须为 0~6(0 = 星期日),收到:{}").format(clock.day_of_week)
        )


def build_clock_read(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
) -> bytes:
    """构造 CLOCK READ(0701)FINS 帧。

    依据:W342 §5-3-19 印刷页 197——命令格式仅命令码 ``07 01`` 两字节,
    无参数(RUN/MONITOR/PROGRAM 三模式均可执行,零副作用数据读)。
    """
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_CLOCK_READ,
        b"",
    )


def parse_clock_read(frame: bytes, request_frame: bytes) -> FinsClock:
    """解析 CLOCK READ(0701)响应。

    依据:W342 §5-3-19 印刷页 198——响应 = 命令码回显 + 结束码(2B)+
    年/月/日/时/分/秒/星期各 1 字节 BCD;星期值 00~06 对应星期日~星期六。
    年份为右两位原文(世纪映射见 :class:`FinsClock`)。

    :raises ProtocolFrameError: 帧结构/ICF·SID·命令码不符、长度不符或
        字段非 BCD(消息带原始帧十六进制转储)
    :raises DeviceError: 结束码非正常完成
    """
    _check_identity(frame, request_frame)
    prefix = FINS_HEADER_SIZE + 2
    # 结束码先于数据长度校验(与 parse_response 同口径):错误应答只带
    # 结束码、不带数据,按结束码抛 DeviceError 而非误判坏帧
    if len(frame) < prefix + FINS_END_CODE_SIZE:
        raise ProtocolFrameError(
            _("FINS 响应不完整:至少 {} 字节,实际 {}(收到的原始帧:{})").format(
                prefix + FINS_END_CODE_SIZE, len(frame), format_hex(frame)
            )
        )
    end_code = int.from_bytes(frame[12:14], "big")
    if not _is_normal_end_code(end_code):
        text = _(_end_code_text(end_code))
        raise DeviceError(
            _("FINS 结束码 0x{:04X}({})").format(end_code, text), end_code
        )
    data_total = 7
    total = prefix + FINS_END_CODE_SIZE + data_total
    if len(frame) != total:
        raise ProtocolFrameError(
            _("FINS 0701 响应长度不符:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                total, len(frame), format_hex(frame)
            )
        )
    data = frame[prefix + FINS_END_CODE_SIZE :]
    return FinsClock(
        year=_bcd_decode(data[0], frame),
        month=_bcd_decode(data[1], frame),
        day=_bcd_decode(data[2], frame),
        hour=_bcd_decode(data[3], frame),
        minute=_bcd_decode(data[4], frame),
        second=_bcd_decode(data[5], frame),
        day_of_week=_bcd_decode(data[6], frame),
    )


def build_clock_write(
    destination_network: int,
    destination_node: int,
    destination_unit: int,
    source_network: int,
    source_node: int,
    source_unit: int,
    sid: int,
    clock: FinsClock,
) -> bytes:
    """构造 CLOCK WRITE(0702)FINS 帧。

    依据:W342 §5-3-20 印刷页 198——命令 = 命令码 + 年/月/日/时/分/秒/
    星期各 1 字节 BCD;PLC 侧自动校验范围,任一字段非法则时钟不被设置
    (执行条件:本方须持 CPU 访问权、未开网络写保护)。

    :raises ValueError: 时钟字段越界(见 :func:`validate_clock`)
    """
    validate_clock(clock)
    payload = (
        _bcd_encode(clock.year)
        + _bcd_encode(clock.month)
        + _bcd_encode(clock.day)
        + _bcd_encode(clock.hour)
        + _bcd_encode(clock.minute)
        + _bcd_encode(clock.second)
        + _bcd_encode(clock.day_of_week)
    )
    return _build_frame(
        destination_network,
        destination_node,
        destination_unit,
        source_network,
        source_node,
        source_unit,
        sid,
        FINS_COMMAND_CLOCK_WRITE,
        payload,
    )


def parse_clock_write(frame: bytes, request_frame: bytes) -> None:
    """解析 CLOCK WRITE(0702)响应(命令码回显 + 结束码,无数据段)。

    依据:W342 §5-3-20 印刷页 198——响应 = 命令码回显 + 结束码(2B)。

    :raises ProtocolFrameError: 帧结构/ICF·SID·命令码不符或长度不符
    :raises DeviceError: 结束码非正常完成
    """
    parse_response(frame, request_frame, 0, False, False)


# ----------------------------------------------------------------------
# FINS/TCP 封装
# ----------------------------------------------------------------------


def build_handshake(local_node: int) -> bytes:
    """构造 20 字节节点分配握手请求(本地节点号取帧内末字节)。

    :param local_node: 本地节点号(0 = 由 PLC 自动分配)
    :raises ValueError: 节点号非法
    """
    if not 0 <= local_node <= 0xFF:
        raise ValueError(_("本地节点号超出范围 0~255:{}").format(local_node))
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
            _("FINS/TCP 帧头非法:{}(收到的原始数据:{})").format(
                head.hex(), format_hex(head)
            )
        )
    length = int.from_bytes(head[4:8], "big")
    # FINS 帧最小 = 头(10) + 命令(2) + 结束码(2) = 14 字节;长度域
    # 小于此即坏帧,直接报错定位前移(避免下游 ``recv(0)`` 阻塞到超时)。
    min_fins_frame = FINS_HEADER_SIZE + 2 + FINS_END_CODE_SIZE
    if length < min_fins_frame:
        raise ProtocolFrameError(
            _(
                "FINS/TCP 长度域过短:{} < 最小 FINS 帧 {} 字节(收到的原始帧头:{})"
            ).format(length, min_fins_frame, format_hex(head))
        )
    if length > FINS_MAX_TCP_FRAME:
        raise ProtocolFrameError(
            _("FINS/TCP 长度域超限:{} > {}(收到的原始帧头:{})").format(
                length, FINS_MAX_TCP_FRAME, format_hex(head)
            )
        )
    return length


def parse_handshake_response(frame: bytes) -> Tuple[int, int]:
    """解析握手响应,返回 ``(本地节点号, PLC 节点号)``。

    :raises ProtocolFrameError: 帧不完整、命令码回显不符或握手错误码非 0
        (消息带**收到的原始帧**十六进制转储,便于现场与抓包比对)
    """
    if len(frame) < FINS_HANDSHAKE_RESPONSE_SIZE:
        raise ProtocolFrameError(
            _("FINS/TCP 握手响应不完整:期望 {} 字节,实际 {}(收到的原始帧:{})").format(
                FINS_HANDSHAKE_RESPONSE_SIZE, len(frame), format_hex(frame)
            )
        )
    command = int.from_bytes(frame[8:12], "big")
    if command != FINS_TCP_COMMAND_HANDSHAKE_RESPONSE:
        # 命令码回显校验(review-1010 P3-1):握手**响应**命令码 = 1
        # (黄金样本命令域 00000001 + C 语言参考实现 fins_io.c L311 校验 == 1;
        # 请求 0 / 响应 1 是同一次握手的两侧,勿按请求常量比对)——TCP 虽有
        # 连接语义,响应头错位/非握手帧落入时早失败优于按错位字段取节点号
        raise ProtocolFrameError(
            _(
                "FINS/TCP 握手响应命令码回显不符:期望 0x00000001,收到 0x{:08X}(收到的原始帧:{})"
            ).format(command, format_hex(frame))
        )
    error = int.from_bytes(frame[12:16], "big")
    if error != 0:
        raise ProtocolFrameError(
            _("FINS/TCP 握手失败,错误码 0x{:08X}(收到的原始帧:{})").format(
                error, format_hex(frame)
            )
        )
    return frame[19], frame[23]


def build_tcp_frame(fins_frame: bytes) -> bytes:
    """把 FINS 帧封装为 FINS/TCP 数据帧(命令 2,长度 = 后续字节数)。"""
    body = (
        FINS_TCP_COMMAND_DATA.to_bytes(4, "big") + (0).to_bytes(4, "big") + fins_frame
    )
    return FINS_TCP_MAGIC + len(body).to_bytes(4, "big") + body


def extract_tcp_error(content: bytes) -> None:
    """校验 FINS/TCP 数据帧的错误域(帧内偏移 4~8,大端 4 字节)。

    :raises ProtocolFrameError: 错误码非 0(消息带收到的原始数据转储)
    """
    if len(content) < 8:
        raise ProtocolFrameError(
            _("FINS/TCP 数据帧过短:{} 字节(收到的原始数据:{})").format(
                len(content), format_hex(content)
            )
        )
    error = int.from_bytes(content[4:8], "big")
    if error != 0:
        raise ProtocolFrameError(
            _("FINS/TCP 错误码 0x{:08X}(收到的原始数据:{})").format(
                error, format_hex(content)
            )
        )


def extract_tcp_payload(content: bytes) -> bytes:
    """取 FINS/TCP 数据帧中的 FINS 帧(偏移 8 起)。

    :raises ProtocolFrameError: 内容过短(消息带收到的原始数据转储)
    """
    if len(content) < 8:
        raise ProtocolFrameError(
            _("FINS/TCP 数据帧过短:{} 字节(收到的原始数据:{})").format(
                len(content), format_hex(content)
            )
        )
    return content[8:]


# ----------------------------------------------------------------------
# 内部函数
# ----------------------------------------------------------------------


def _is_normal_end_code(end_code: int) -> bool:
    """结束码是否正常完成(内部函数)。

    W342 §5-1-3 p.161:主/子码 00 为正常完成;bit6/7(0x00C0)是「目标 CPU
    单元出错」标志——非致命(bit6,如电池电压低)/致命(bit7)错误存在时,
    标志位会随正常完成的命令一起返回(手册原文:「Basically, the end code
    of a sent command that is completed normally is 0040」),此时命令本身
    完成、数据有效,应按成功处理;bit15(0x8000)是网络中继错误标志,命中
    即按错误处理(且响应会附带中继错误码双字节、数据布局改变)。
    """
    return end_code & ~FINS_END_CODE_CPU_ERROR_FLAGS == 0


def _end_code_text(end_code: int) -> str:
    """结束码 → 可读文本,先屏蔽标志位再查表(内部函数)。

    W342-E1-18 §5-1-3:结束码 = 主码(高字节)+ 子码(低字节);主码 bit15
    表示**网络中继错误**,子码 bit7/6 表示**目标 CPU 单元出错**。这些位与
    主/子码值叠加(如 ``0x9005`` = 网络中继错误标志 + 主码 0x10/子码 0x05
    「头错误」),故查表前须屏蔽,否则会落入"未知码"。标志同时附在文本末尾。
    """
    flags = []
    if end_code & FINS_END_CODE_RELAY_ERROR_FLAG:
        flags.append(_("网络中继错误"))
    if end_code & FINS_END_CODE_CPU_ERROR_FLAGS:
        flags.append(_("目标 CPU 单元出错"))
    base = end_code & ~(FINS_END_CODE_RELAY_ERROR_FLAG | FINS_END_CODE_CPU_ERROR_FLAGS)
    # 表值查表结果过 _(表值全为无占位符的纯文案,en 模式全表可译;fallback 一并过 _)
    text = _(FINS_END_CODE_TEXT.get(base, _("详见 Omron FINS 手册")))
    if flags:
        text = _("{} [{}]").format(text, _("、").join(flags))
    hint = FINS_END_CODE_HINT.get(base)
    if hint:
        text = _("{};现场排查:{}").format(text, _(hint))
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
        raise ValueError(_("FINS 字地址超出范围 0~65535:{}").format(address.offset))
    bit = address.bit if address.bit is not None else 0
    return address.offset.to_bytes(2, "big") + bytes([bit])


def _write_data(data: List[int], is_bit: bool) -> bytes:
    """写数据编码:位每点 1 字节 0x00/0x01;字逐字大端(内部函数)。"""
    if is_bit:
        return bytes([1 if flag else 0 for flag in data])
    for word in data:
        if not 0 <= word <= 0xFFFF:
            raise ValueError(_("字写数据超出范围 0~65535:{}").format(word))
    # 校验遍保留,打包一次 struct.pack(逐字 to_bytes + join 换出,review-1019 P2-5)
    return struct.pack(f">{len(data)}H", *data)


def _check_count(count: int) -> None:
    """点数范围校验(内部函数)。"""
    if not 1 <= count <= 0xFFFF:
        raise ValueError(_("FINS 访问点数超出范围 1~65535:{}").format(count))


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
            _(
                "FINS 响应不完整(无法核对 ICF/SID/命令码):至少 {} 字节,实际 {}(收到的原始帧:{})"
            ).format(FINS_HEADER_SIZE + 2, len(frame), format_hex(frame))
        )
    if frame[0] != FINS_ICF_RESPONSE:
        raise ProtocolFrameError(
            _("FINS 响应 ICF 非法:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})").format(
                FINS_ICF_RESPONSE, frame[0], format_hex(frame)
            )
        )
    if frame[9] != expected_sid:
        raise ProtocolFrameError(
            _(
                "FINS 响应 SID 回显不符:期望 0x{:02X},收到 0x{:02X}(收到的原始帧:{})"
            ).format(expected_sid, frame[9], format_hex(frame))
        )
    if frame[10:12] != expected_command.to_bytes(2, "big"):
        actual_command = int.from_bytes(frame[10:12], "big")
        raise ProtocolFrameError(
            _(
                "FINS 响应命令码回显不符:期望 0x{:04X},收到 0x{:04X}(收到的原始帧:{})"
            ).format(expected_command, actual_command, format_hex(frame))
        )
