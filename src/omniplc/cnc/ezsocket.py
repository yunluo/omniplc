"""三菱 CNC EZSocket 客户端(GIOP 直连,M70/M700 系数采只读)。

依据链(两级,详见 ``docs/protocol/mitsubishi/m70-ezsocket/README.md``):

- **语义层**:官方手册 FCSB1224W000 リファレンス IB-1501208(262 页,
  OLE/COM 接口;PDF 本地留存 ``docs/protocol/mitsubishi/``,印刷页
  2-7 起 I/F 详解、3-1 起错误码表)——方法含义/数据范围/错误码文本。
- **帧面层**:GIOP 线上格式**无官方公开文档**,逐字节参照同源参考
  实现 ``开源参考实现快照(内部留存,M70 真机验证)``(MIT,M70 真机
  验证;下称「C 库」,行号指 ``m70_giop.c``/``m70_ezsocket_private.h``
  /``m70_ezsocket.c``)——**单源,真机核证必做**;section/sub_section
  编号值同(官方仅证实「大区分/小区分番号」概念,见错误码
  ``EZNC_DATA_READ_SECT/SUBSECT``)。

- 走线:纯 TCP,默认端口 683(COM 样例与 C 库双源);**连接 = TCP
  三次握手,无握手帧**,断开即 TCP close(C 库 ``giop_connect``)。
- ``request_id``:建连时随机 0~0xFFFE,**会话内恒定**(C 库
  ``rand() % 0xFFFF`` 且从不自增);omniplc **加严**:响应回显校验
  (C 库不校验,按 2026-09-25 FINS 加固确立的应答身份回显统一模式)。
- 范围(数采只读,与 MTConnect/FOCAS 同域):状态/轴位置/主轴/进给/
  程序/报警/时间统计/版本轴数等 ``read_*`` 面。写(mochaSetData)、
  文件操作(mochaFS*)、主轴/进给倍率拼合留后续版本(档案 §7)。
"""

from __future__ import annotations

import random
import struct
from enum import IntEnum
from typing import Any, List, NamedTuple, Optional, Tuple

from ..core.base_client import BaseClient, validate_endpoint
from ..core.debug import format_hex
from ..core.errors import DeviceError, ProtocolFrameError
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue
from ..transport import BaseTransport, TcpTransport

__all__ = [
    "EzAlarm",
    "EzAlarmType",
    "EzDeviceStatus",
    "EzFeedSpeedType",
    "EzPositionType",
    "EzProgramBlock",
    "EzProgramFileInfo",
    "EzRunMode",
    "EzRunState",
    "EzRunStatus",
    "EzSocketMachine",
    "EZSOCKET_DEFAULT_PORT",
    "MitsubishiEzSocketClient",
]

# --------------------------------------------------------------------------
# 常量(模块内不入 core/constants,FOCAS/海康 SDK 同款口径)
# --------------------------------------------------------------------------

EZSOCKET_DEFAULT_PORT: int = 683
"""EZSocket 以太网默认端口(COM 样例 ``SetTCPIPProtocol(ip, 683)`` 与
C 库示例 ``m70_cnc_connect(..., 683, ...)`` 双源一致)。"""

_GIOP_MAGIC: bytes = b"GIOP"
"""GIOP 魔数(C 库 m70_ezsocket_private.h L81;build_giop_header L981)。"""

_GIOP_HEADER_SIZE: int = 12
"""GIOP 头定长(magic 4 + version 2 + byte_order 1 + msg_type 1 +
data_length 4;m70_ezsocket_private.h L80~86)。"""

_RESPONSE_HEADER_SIZE: int = 12
"""响应体头(sc_list 4 + request_id 4 + is_error 4;
m70_ezsocket_private.h L98~103)。"""

_GET_DATA_HEADER_SIZE: int = 12
"""mochaGetData 成功响应的数据头(u32 0 + data_type 4 + data_length 4;
m70_ezsocket_private.h L107~110)。C 库另有 T_FLOATBIN 多轴 20 字节
头变体(L112~119)——omniplc 首批恒单轴,不触发,不实现(档案 §7)。"""

_GIOP_MAX_PAYLOAD: int = 16384
"""响应 data_length 上界(防御钳制;首批最大合法面 = 报警 10 条
× 264 字节 = 2640,余量给程序块/未来文件面)。"""

_MSG_TYPE_REQUEST: int = 0
_MSG_TYPE_REPLY: int = 1
"""GIOP msg_type(C 库 tag_giop_msg_types;omniplc 只发 Request)。"""

_OP_GET_DATA: str = "mochaGetData"
"""读数据操作名(C 库 m70_giop.c L63;op 定长数组 16、
operation_length = 13 含 NUL,L166~181 get_data_pack)。"""

_OP_GET_ALARM: str = "mochaGetCurrentAlarmMsgFirst"
"""报警读取操作名(C 库 L65;op 数组 32、length 29,L163~168)。"""

_OP_GET_PROG_BLOCK: str = "mochaGetCurrentPrgBlockFirst"
"""当前程序块读取操作名(C 库 L66;op 数组 32、length 29,L153~157)。"""


class EzSocketMachine(IntEnum):
    """NC 机型(COM Open2 lSystemType;手册 2.3.1 印刷页 2-7 只给符号,
    数值为 C 库 typedef.h 与官方 COM 样例 m700.py 注释双源一致)。

    M70/M700 系首批支持 5/6;7~9(C70/M800)枚举在列但帧面未真机核。
    """

    MELDAS700L = 5
    """M700 系车床系。"""
    MELDAS700M = 6
    """M700/M70 系加工中心系(缺省)。"""
    MELDASC70 = 7
    """C70(Open2 前须 SetMelsecProtocol,GIOP 直连未核,不承诺)。"""
    MELDAS800M = 8
    """M800 系加工中心系。"""
    MELDAS800L = 9
    """M800 系车床系。"""


class EzRunMode(IntEnum):
    """运转模式码(section 35 / sub_section 11,T_SHORT)。

    码表 = C 库 typedef.h ``m70_run_mode_e`` 注释(M700 PLC 信号名,
    单源;语义上游 = 手册 §2.10 状态族)。
    """

    MEM = 0
    """存储器运转(自动)。"""
    DNC = 1
    """DNC 运转。"""
    LNK = 2
    """在线运转。"""
    MDI = 3
    """MDI。"""
    PC = 4
    """PC(留值)。"""
    MNL = 5
    """手动。"""
    JOG = 6
    """JOG。"""
    JOG_HANDLE = 7
    """JOG + 手轮。"""
    RAPID_HANDLE = 8
    """快速 + 手轮。"""
    HANDLE = 9
    """手轮。"""
    STEP = 10
    """步进。"""
    STEP1 = 11
    """步进 1。"""
    ZERO_RETURN = 12
    """回零。"""
    DRT = 13
    """手动进给。"""
    INIT = 14
    """初始化。"""
    NONE = 15
    """无。"""
    LINE = 16
    """留值。"""


class EzRunStatus(IntEnum):
    """运转状态码(section 35 / sub_section 10,T_SHORT;C 库单源)。"""

    RESET = 0
    """复位。"""
    EMERGENCY = 1
    """急停。"""
    READY = 2
    """就绪。"""
    AUTO = 3
    """自动运转中。"""
    SYNC = 4
    """同步。"""
    CRS = 5
    """留值。"""
    BUFFER_STOP = 6
    """缓冲停止。"""
    HOLD = 7
    """保持。"""


class EzDeviceStatus(IntEnum):
    """综合设备状态(C 库 ``m70_device_status_e``,read_run_state 拼合)。"""

    UNKNOWN = 0
    STOP = 1
    """停止(急停)。"""
    RUN = 2
    """运转中。"""
    IDLE = 3
    """空闲。"""
    OFFLINE = 4
    """离线(模式读取失败)。"""
    DEBUG = 5
    """调试(LNK~LIN 模式段)。"""


class EzPositionType(IntEnum):
    """位置种类(section 37 的 sub_section;C 库 position_type_e 映射)。"""

    WORKPIECE = 1
    """工件坐标位置。"""
    MACHINE = 2
    """机械坐标位置(缺省)。"""
    CURRENT = 3
    """当前位置计数器。"""
    RELATIVE = 4
    """相对位置。"""
    PROGRAM = 5
    """程序位置计数器。"""
    DISTANCE = 6
    """残指令(剩余移动量)。"""


class EzFeedSpeedType(IntEnum):
    """进给速度种类(section 42/33;C 库 feed_speed_type_e)。"""

    FA = 0
    """F 指令进给速度(section 42/1)。"""
    FM = 1
    """手动有效进给速度(42/2)。"""
    FS = 2
    """同步进给速度(42/3)。"""
    FC = 3
    """自动有效进给速度(33/1,缺省)。"""
    FE = 4
    """螺纹切削进给速度(42/4)。"""


class EzAlarmType(IntEnum):
    """报警种类(mochaGetCurrentAlarmMsgFirst 的 msg_type;C 库单源)。"""

    ALL = 0x000
    """全部消息。"""
    NC_ALARM = 0x100
    """NC 报警。"""
    STOP_CODE = 0x200
    """停止因子。"""
    PLC_ALARM = 0x300
    """PLC 报警。"""
    OPE_MSG = 0x400
    """操作消息。"""
    ALL_NON_STOPCD = 0x1000
    """全部(不含停止因子)。"""
    NC_SYSTEM = 0x101
    """NC 系统报警。"""
    NC_SERVO = 0x102
    """NC 伺服报警。"""
    NC_MCP = 0x103
    """NC MCP 报警。"""
    NC_BASIC_PLC = 0x104
    """NC 基本 PLC 报警。"""
    NC_USER_PLC = 0x105
    """NC 用户 PLC 报警。"""
    NC_PROGRAM = 0x106
    """NC 程序报警。"""
    NC_SERVO_WARNING = 0x107
    """NC 伺服警告。"""
    NC_MCP_WARNING = 0x108
    """NC MCP 警告。"""
    NC_SYSTEM_WARNING = 0x109
    """NC 系统警告。"""
    NC_OPERATION = 0x10A
    """NC 操作警告。"""
    OPE_ALARM = 0x10B
    """操作报警。"""


class EzProgramFileInfo(IntEnum):
    """程序文件信息种类(section 25;C 库 m70_file_info_type_e)。"""

    REGISTERED = 1
    """已登录加工程序数。"""
    REMAINING = 2
    """剩余可登录加工程序数。"""
    CAPACITY = 3
    """加工程序容量(字符数)。"""
    FREE = 4
    """加工程序剩余容量(字符数)。"""
    TRANSFER_SIZE = 10
    """传送数据尺寸。"""


# 手册 §3 表 3-1 官方错误码 → 文本(首批高频子集;全表见手册 PDF 3-1~3-11 页)
EZ_ERROR_TEXT = {
    0x80A00101: "通信回线未打开(EZ_ERR_NOT_OPEN)",
    0x80A00104: "重复打开(EZ_ERR_DOUBLE_OPEN)",
    0x80A00105: "参数数据类型非法(EZ_ERR_DATA_TYPE)",
    0x80A00106: "参数数据范围非法(EZ_ERR_DATA_RANGE)",
    0x80A00107: "不支持(EZ_ERR_NOT_SUPPORT)",
    0x80A00109: "通信回线无法打开(EZ_ERR_CANNOT_OPEN)",
    0x80A0010A: "参数为 NULL 指针(EZ_ERR_NULLPTR)",
    0x80A0010B: "参数数据非法(EZ_ERR_DATA_LENGTH)",
    0x80A0010C: "COMM 端口句柄错误(EZ_ERR_OPEN_COMM)",
    0x80B00101: "内存分配失败(EZ_ERR_MEMORY_ALLOC)",
    0x80B00302: "TCP/IP 通信未设定(EZNC_COMM_NOTSETUP_PROTOCOL)",
    0x80040190: "系统/轴指定非法(EZNC_DATA_READ_ADDR)",
    0x80040191: "大区分番号非法(EZNC_DATA_READ_SECT)",
    0x80040192: "小区分番号非法(EZNC_DATA_READ_SUBSECT)",
    0x80040196: "数据超出应用缓冲(EZNC_DATA_READ_DATASIZE)",
    0x80040197: "数据类型非法(EZNC_DATA_READ_DATATYPE)",
    0x8004019A: "读出数据范围非法(EZNC_DATA_READ_VALUE)",
    0x8004019D: "数据处于不可读状态(EZNC_DATA_READ_READ)",
    0x8004019F: "只写数据(EZNC_DATA_READ_WRITEONLY)",
    0x800401A0: "轴指定非法(EZNC_DATA_READ_AXIS)",
    0x800401A1: "数据番号非法(EZNC_DATA_READ_DATANUM)",
    0x800401A3: "无读出数据(EZNC_DATA_READ_NODATA)",
    0x80050D90: "系统/轴指定非法(EZNC_OPE_CURRALM_ADDR)",
    0x80050D02: "报警种类非法(EZNC_OPE_CURRALM_ALMTYPE)",
    0x80050D03: "NC 与 PC 间通信数据错误(EZNC_OPE_CURRALM_DATAERR)",
    0x8202000A: "未连接(EZ_NC_NOT_CONNECT)",
    0x82020014: "超时(0x82020014)",
    0x82020015: "数据非法(0x82020015)",
    0x82020032: "命令非法(0x82020032)",
}
"""三菱侧错误码文本(手册 §3 官方表意译;键 = mel_error_code 原码)。"""

_LINK_ERROR_CODES = frozenset({0x80A00101, 0x8202000A})
"""链路类错误码:通信回线未开/未连接 → 拆连(OSError → 惰性重连)。"""

_ALARM_STRUCT_SIZE = 264
"""单条报警结构(alarm_no i32 + alarm_length i32 + text[256];
C 库 typedef.h alarm_string)。"""

_PROG_BLOCK_STRUCT_SIZE = 528
"""单条程序块结构(current_block/row/u1/block_length i32×4 +
text[512];C 库 typedef.h prog_block)。"""

# 数据类型码(C 库 m70_data_type_e;尺寸按 get_data_type_length)
_T_CHAR = 0x01
_T_SHORT = 0x02
_T_LONG = 0x03
_T_DLONG = 0x04
_T_DOUBLE = 0x05
_T_FLOATBIN = 0x06
_T_STR = 0x10
_T_UCHAR = 0x21
_T_USHORT = 0x22
_T_UINT32 = 0x23


# --------------------------------------------------------------------------
# codec 纯函数(编解码与传输解耦,黄金帧测试直接打点)
# --------------------------------------------------------------------------


def _op_field(op: str, size: int) -> bytes:
    """操作名 → 定长 NUL 填充字段(内部函数;C 库 strncpy 到定长数组)。"""
    raw = op.encode("ascii")
    if len(raw) + 1 > size:
        raise ValueError(_("操作名超长:{}").format(op))
    return raw + b"\x00" * (size - len(raw))


def build_giop_request(request_id: int, op: str, op_size: int, params: bytes) -> bytes:
    """构造 GIOP 请求帧(内部函数)。

    帧布局:C 库 build_giop_header(L973~985)+ build_request_pack_header
    (L987~1003)+ 各 pack 结构(m70_ezsocket_private.h)。

    :param request_id: 会话恒定请求号(0~0xFFFE)
    :param op: 操作名(mochaGetData 等)
    :param op_size: op 定长数组字节数(get_data=16/alarm·block=32)
    :param params: principal 之后的参数区(定长小端字节)
    """
    operation_length = len(op) + 1  # 含 NUL(C 库传 0x0D/0x1D 等)
    body = (
        struct.pack("<I", 0)  # service_context = 0
        + struct.pack("<I", request_id)
        + b"\x01"  # response_expected
        + b"\x00\x00\x00"  # reserved
        + struct.pack("<I", 4)  # object_key_length
        + struct.pack("<I", 1)  # object_key
        + struct.pack("<I", operation_length)
        + _op_field(op, op_size)
        + params
    )
    header = (
        _GIOP_MAGIC
        + struct.pack("<H", 1)  # version 1.0
        + b"\x01"  # byte_order = little
        + bytes([_MSG_TYPE_REQUEST])
        + struct.pack("<I", len(body))
    )
    return header + body


def build_get_data_request(
    request_id: int,
    section: int,
    sub_section: int,
    system_no: int,
    axis_no: int,
    data_type: int,
) -> bytes:
    """构造 mochaGetData 请求(内部函数)。

    参数区布局 = C 库 get_data_pack(m70_ezsocket_private.h L166~181):
    principal + section + sub_section + system_no + axis_no(位标志)+
    u2(0)+ data_type,全 u32 小端。
    """
    params = struct.pack(
        "<7I", 0, section, sub_section, system_no, axis_no, 0, data_type
    )
    return build_giop_request(request_id, _OP_GET_DATA, 16, params)


def build_alarm_request(
    request_id: int, system_no: int, msg_count: int, msg_type: int
) -> bytes:
    """构造 mochaGetCurrentAlarmMsgFirst 请求(内部函数)。

    参数区 = C 库 alarm_info_pack(L163~168):principal + system_no +
    msg_count + msg_type。
    """
    params = struct.pack("<4I", 0, system_no, msg_count, msg_type)
    return build_giop_request(request_id, _OP_GET_ALARM, 32, params)


def build_prog_block_request(request_id: int, system_no: int, row_count: int) -> bytes:
    """构造 mochaGetCurrentPrgBlockFirst 请求(内部函数)。

    参数区 = C 库 prog_block_pack(L153~157):principal + system_no +
    row_count。
    """
    params = struct.pack("<3I", 0, system_no, row_count)
    return build_giop_request(request_id, _OP_GET_PROG_BLOCK, 32, params)


def parse_giop_response(
    raw: bytes, expected_request_id: int
) -> Tuple[bytes, Optional[int]]:
    """解析 GIOP 响应帧(内部函数)。

    :param raw: 完整响应(GIOP 头 + data_length 区)
    :param expected_request_id: 请求的 request_id(回显校验,omniplc 加严)
    :return: ``(payload, error_code)`` 元组——成功时 ``(数据段 bytes, None)``
        (数据段 = 响应体头之后的原始字节,按操作自行解码);失败时
        ``(b"", 三菱错误码)``
    :raises ProtocolFrameError: 魔数/类型/回显/长度非法
    """
    if len(raw) < _GIOP_HEADER_SIZE:
        raise ProtocolFrameError(
            _("GIOP 响应帧长非法:{}(收到的原始帧:{})").format(len(raw), format_hex(raw))
        )
    magic = raw[0:4]
    if magic != _GIOP_MAGIC:
        raise ProtocolFrameError(
            _("GIOP 响应帧魔数非法,收到原始帧:{}").format(format_hex(raw))
        )
    msg_type = raw[7]
    if msg_type != _MSG_TYPE_REPLY:
        raise ProtocolFrameError(
            _("GIOP 响应帧类型非法:{}(应为 Reply),收到原始帧:{}").format(
                msg_type, format_hex(raw)
            )
        )
    (data_length,) = struct.unpack_from("<I", raw, 8)
    if data_length < _RESPONSE_HEADER_SIZE:
        raise ProtocolFrameError(
            _("GIOP 响应帧长非法:{}(收到的原始帧:{})").format(
                data_length, format_hex(raw)
            )
        )
    if data_length > _GIOP_MAX_PAYLOAD:
        raise ProtocolFrameError(
            _("GIOP 响应帧长超限:{} > {}(收到的原始帧:{})").format(
                data_length, _GIOP_MAX_PAYLOAD, format_hex(raw)
            )
        )
    expected_total = _GIOP_HEADER_SIZE + data_length
    if len(raw) < expected_total:
        raise ProtocolFrameError(
            _("GIOP 响应帧不完整:需要 {} 字节,收到 {} 字节").format(
                expected_total, len(raw)
            )
        )
    body = raw[_GIOP_HEADER_SIZE:expected_total]
    sc_list, request_id, is_error = struct.unpack_from("<3I", body, 0)
    if request_id != expected_request_id:
        raise ProtocolFrameError(
            _("GIOP 响应 request_id 回显不符:期望 {},收到 {}(收到的原始帧:{})").format(
                expected_request_id, request_id, format_hex(raw)
            )
        )
    if is_error == 0:
        return body[_RESPONSE_HEADER_SIZE:], None
    return b"", _parse_error_body(body[_RESPONSE_HEADER_SIZE:])


def _parse_error_body(body: bytes) -> int:
    """解析错误体,返回三菱侧错误码(内部函数)。

    布局照录 C 库 receive_error_data_response(L97~110):u32(语义
    未定)+ 等长字节 + mel_error_code(char[3] + u32 error_code +
    u32,共 11 字节;L142~147)——**布局存疑,真机核证项**。

    :param body: 响应体头(is_error 所在 12 字节)之后的原始字节
    """
    # 4(长度域)+ 11(mel_error_code)= 15 为无描述数据的最小错误体
    if len(body) < 15:
        raise ProtocolFrameError(
            _("GIOP 响应错误体长度非法:{}(原始数据:{})").format(
                len(body), format_hex(body)
            )
        )
    (desc_len,) = struct.unpack_from("<I", body, 0)
    code_offset = 4 + desc_len
    if code_offset + 11 > len(body):
        raise ProtocolFrameError(
            _("GIOP 响应错误体长度非法:{}(原始数据:{})").format(
                len(body), format_hex(body)
            )
        )
    (code,) = struct.unpack_from("<I", body, code_offset + 3)
    return code


def decode_get_data(payload: bytes, request_type: int) -> Any:
    """解码 mochaGetData 数据段(内部函数)。

    数据头 = C 库 get_data_response_header(L107~110):u32(0)+
    data_type + data_length;值按**响应头回传的 data_type** 解码
    (C 库 ``*data_type = rsp.data_type`` 同)。T_DLONG 按 i64 全宽
    解码(C 库按低 32 位读且存在 8 字节收进 4 字节缓冲的越界写,
    omniplc 精确解码并登记差异)。

    :param payload: 响应体头之后的数据段
    :param request_type: 请求的 data_type(仅日志对照用;解码以响应头为准)
    :raises ProtocolFrameError: 数据头/长度非法
    """
    if len(payload) < _GET_DATA_HEADER_SIZE:
        raise ProtocolFrameError(
            _("GIOP 响应数据段长度非法:{}(原始数据:{})").format(
                len(payload), format_hex(payload)
            )
        )
    _u1, data_type, data_length = struct.unpack_from("<3I", payload, 0)
    data = payload[_GET_DATA_HEADER_SIZE : _GET_DATA_HEADER_SIZE + data_length]
    if len(data) < data_length:
        raise ProtocolFrameError(
            _("GIOP 响应数据段长度非法:声明 {} 字节,实际 {} 字节").format(
                data_length, len(data)
            )
        )
    if data_type == _T_CHAR or data_type == _T_UCHAR:
        return data[0]
    if data_type == _T_SHORT:
        return struct.unpack("<h", data[:2])[0]
    if data_type == _T_USHORT:
        return struct.unpack("<H", data[:2])[0]
    if data_type == _T_LONG:
        return struct.unpack("<i", data[:4])[0]
    if data_type == _T_UINT32:
        return struct.unpack("<I", data[:4])[0]
    if data_type == _T_DLONG:
        return struct.unpack("<q", data[:8])[0]
    if data_type == _T_DOUBLE:
        return struct.unpack("<d", data[:8])[0]
    if data_type == _T_FLOATBIN:
        int_nos, dec_nos, option, value = struct.unpack("<hhId", data[:16])
        return value
    if data_type == _T_STR:
        (text_len,) = struct.unpack_from("<I", data, 0)
        text = data[4 : 4 + text_len]
        return text.decode("ascii", "replace")
    raise ProtocolFrameError(
        _("EZSocket 未支持的数据类型:0x{:02X}(请求 0x{:02X})").format(
            data_type, request_type
        )
    )


def decode_alarms(payload: bytes) -> List["EzAlarm"]:
    """解码报警数组(内部函数;定长 264 字节逐条,C 库 alarm_string)。"""
    count = len(payload) // _ALARM_STRUCT_SIZE
    alarms = []
    for i in range(count):
        base = i * _ALARM_STRUCT_SIZE
        alarm_no, text_len = struct.unpack_from("<2i", payload, base)
        text = payload[base + 8 : base + 8 + 256][: max(0, text_len)]
        alarms.append(
            EzAlarm(no=alarm_no, length=text_len, text=text.decode("ascii", "replace"))
        )
    return alarms


def decode_prog_block(payload: bytes) -> "EzProgramBlock":
    """解码程序块首条(内部函数;C 库 prog_block,响应可能含多行,
    首批取首条——多行布局待真机核)。"""
    if len(payload) < _PROG_BLOCK_STRUCT_SIZE:
        raise ProtocolFrameError(
            _("程序块响应长度非法:{}(< {} 字节)").format(
                len(payload), _PROG_BLOCK_STRUCT_SIZE
            )
        )
    current_block, current_row, _u1, text_len = struct.unpack_from("<4i", payload, 0)
    text = payload[16 : 16 + 512][: max(0, text_len)]
    return EzProgramBlock(
        block=current_block,
        row=current_row,
        length=text_len,
        text=text.decode("ascii", "replace"),
    )


# --------------------------------------------------------------------------
# 结果类型
# --------------------------------------------------------------------------


class EzRunState(NamedTuple):
    """综合运行状态(:meth:`MitsubishiEzSocketClient.read_run_state`)。"""

    device_status: EzDeviceStatus
    """综合状态(RUN/IDLE/STOP/DEBUG)。"""
    mode: EzRunMode
    """运转模式(MEM/DNC/JOG…)。"""
    run_status: EzRunStatus
    """运转状态(复位/急停/就绪/自动…)。"""
    auto_run: Optional[bool]
    """自动运转中(MEM/DNC 模式下读得;其他模式为 None)。"""


class EzAlarm(NamedTuple):
    """单条报警(:meth:`MitsubishiEzSocketClient.read_alarms`)。"""

    no: int
    """报警号(0 = 该槽无报警)。"""
    length: int
    """报警文本长度。"""
    text: str
    """报警文本(ASCII)。"""


class EzProgramBlock(NamedTuple):
    """当前程序块(:meth:`MitsubishiEzSocketClient.read_program_block`)。"""

    block: int
    """当前块号。"""
    row: int
    """当前行号。"""
    length: int
    """块文本长度。"""
    text: str
    """块文本(ASCII)。"""


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class MitsubishiEzSocketClient(BaseClient):
    """三菱 CNC EZSocket 数采客户端(GIOP 直连 TCP 683,只读)。

    :example::

        client = MitsubishiEzSocketClient("192.168.1.10")   # 端口 683
        client.connect()
        ok, state = client.read_run_state()      # 模式/运转状态/自动运转
        ok, pos = client.read_axis_position(axis=1)   # 机械坐标
        ok, speed = client.read_spindle_speed()  # 主轴转速
        ok, alarms = client.read_alarms(count=5)
        client.close()

    面向 M70/M70V/M700/M700V(加工中心系,EZSocketMachine.MELDAS700M);
    机床侧需开启 EZSocket 以太网服务(端口 683)。依赖:无(纯协议,
    零依赖)。**帧面为参考实现单源,真机核证项见
    docs/real-machine-checklist.md**。
    """

    _has_ping = True
    """支持探活(:meth:`read_system_count`,1 字节零副作用读)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = EZSOCKET_DEFAULT_PORT,
        *,
        machine: EzSocketMachine = EzSocketMachine.MELDAS700M,
    ) -> None:
        """初始化 EZSocket 客户端。

        :param ip_address: CNC 的 IP
        :param port: EZSocket 端口,默认 683
        :param machine: NC 机型(缺省 MELDAS700M = M700/M70 加工中心系;
            GIOP 帧面与机型无关,机型仅入构造契约与文档)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if not isinstance(machine, EzSocketMachine):
            raise ValueError(
                _("machine 必须是 EzSocketMachine 枚举,收到:{!r}").format(machine)
            )
        self._machine = machine
        self._request_id = random.randint(0, 0xFFFE)
        """会话恒定请求号(C 库 giop_connect:rand()%0xFFFF 且不自增)。"""

    @property
    def machine(self) -> EzSocketMachine:
        """NC 机型(构造期冻结)。"""
        return self._machine

    # ------------------------------------------------------------------
    # 传输与事务
    # ------------------------------------------------------------------

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    def _transact(self, frame: bytes) -> Tuple[bytes, Optional[int]]:
        """发送 GIOP 请求并收取完整响应(内部方法)。

        收包 = GIOP 头 12 字节 → data_length → 余量;头/回显/长度校验
        在 :func:`parse_giop_response`。

        :return: ``(数据段, 三菱错误码)``;错误码非 None 表示设备层失败
        :raises ProtocolFrameError: 坏帧(拆连由调用链统一处置)
        """
        transport = self._require_transport()
        transport.send(frame)
        header = transport.recv(_GIOP_HEADER_SIZE)
        (data_length,) = struct.unpack_from("<I", header, 8)
        if data_length > _GIOP_MAX_PAYLOAD:
            raise ProtocolFrameError(
                _("GIOP 响应帧长超限:{} > {}(收到的原始帧头:{})").format(
                    data_length, _GIOP_MAX_PAYLOAD, format_hex(header)
                )
            )
        raw = header + transport.recv(data_length)
        payload, error_code = parse_giop_response(raw, self._request_id)
        return payload, error_code

    def _check_device_error(self, error_code: int, action: str) -> None:
        """三菱侧错误码分流(内部方法):链路段拆连,其余 DeviceError。"""
        text = EZ_ERROR_TEXT.get(error_code)
        if text:
            detail = _(text)
        else:
            detail = _("未知错误码 0x{:08X}").format(error_code)
        if error_code in _LINK_ERROR_CODES:
            raise OSError(_("{}失败:{}(链路故障,将重连)").format(action, detail))
        raise DeviceError(
            _("{}失败:三菱 CNC 错误码 0x{:08X}({})").format(action, error_code, detail),
            error_code,
        )

    def _get_data(
        self,
        section: int,
        sub_section: int,
        system_no: int,
        axis_no: int,
        data_type: int,
        action: str,
    ) -> Any:
        """mochaGetData 事务原语(内部方法):构造 → 收发 → 解码。"""
        frame = build_get_data_request(
            self._request_id, section, sub_section, system_no, axis_no, data_type
        )
        payload, error_code = self._transact(frame)
        if error_code is not None:
            self._check_device_error(error_code, action)
        return decode_get_data(payload, data_type)

    def _get_special(self, frame: bytes, action: str, decoder=None) -> Any:
        """mochaGetCurrentAlarmMsgFirst / PrgBlock 事务原语(内部方法)。"""
        payload, error_code = self._transact(frame)
        if error_code is not None:
            self._check_device_error(error_code, action)
        if decoder is not None:
            return decoder(payload)
        return payload

    # ------------------------------------------------------------------
    # 探活与基类抽象方法(结构化数采面,无通用地址读写)
    # ------------------------------------------------------------------

    def _ping_probe(self) -> str:
        """探活探测命令:读系统数(section 2/1,1 字节;内部方法)。"""
        return "system_count={}".format(
            self._get_data(2, 1, 0, 0, _T_CHAR, _("读取系统数"))
        )

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """EZSocket 无通用地址读写(内部方法):引导到 read_* 系列。"""
        raise DeviceError(
            _("EZSocket 为结构化数采面,无通用地址读写;请用 read_* 系列方法"),
            0,
        )

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """EZSocket 首批只读(内部方法)。"""
        raise DeviceError(_("EZSocket 数采客户端为只读,不支持写入"), 0)

    # ------------------------------------------------------------------
    # 系统信息
    # ------------------------------------------------------------------

    def _check_system_no(self, system_no: int) -> int:
        """系统号校验(内部方法;C 库无校验,omniplc 构造期口径补齐)。"""
        if not isinstance(system_no, int) or isinstance(system_no, bool):
            raise ValueError(_("system_no 必须是整数,收到:{!r}").format(system_no))
        if not 1 <= system_no <= 255:
            raise ValueError(_("system_no 必须在 1~255,收到:{}").format(system_no))
        return system_no

    def _check_axis(self, axis: int) -> int:
        """轴号校验并转位标志(内部方法;C 库 get_axis_real_no)。"""
        if not isinstance(axis, int) or isinstance(axis, bool) or not 1 <= axis <= 8:
            raise ValueError(_("axis 必须在 1~8,收到:{!r}").format(axis))
        return 1 << (axis - 1)

    def read_nc_version(self) -> Tuple[bool, Optional[str]]:
        """读 NC 版本(section 67/1,T_STR;COM System_GetVersion 语义)。"""
        return self._execute(
            lambda: self._get_data(67, 1, 0, 0, _T_STR, _("读取 NC 版本"))
        )

    def read_nc_name_version(self) -> Tuple[bool, Optional[str]]:
        """读 NC 名称版本(section 68/1,T_STR)。"""
        return self._execute(
            lambda: self._get_data(68, 1, 0, 0, _T_STR, _("读取 NC 名称版本"))
        )

    def read_plc_version(self) -> Tuple[bool, Optional[str]]:
        """读 PLC 版本(section 67/2,T_STR)。"""
        return self._execute(
            lambda: self._get_data(67, 2, 0, 0, _T_STR, _("读取 PLC 版本"))
        )

    def read_nc_type(self) -> Tuple[bool, Optional[str]]:
        """读机床类型(section 2/100,T_CHAR;1 = 车床,0 = 加工中心)。"""
        return self._execute(
            lambda: (
                "Lathe"
                if self._get_data(2, 100, 0, 0, _T_CHAR, _("读取机床类型")) == 1
                else "MC"
            )
        )

    def read_system_count(self) -> Tuple[bool, Optional[int]]:
        """读系统数(section 2/1,T_CHAR;探活命令)。"""
        return self._execute(
            lambda: self._get_data(2, 1, 0, 0, _T_CHAR, _("读取系统数"))
        )

    def read_nc_axis_count(self) -> Tuple[bool, Optional[int]]:
        """读 NC 控制轴数(section 2/2,T_CHAR)。"""
        return self._execute(
            lambda: self._get_data(2, 2, 0, 0, _T_CHAR, _("读取 NC 轴数"))
        )

    def read_all_axis_count(self) -> Tuple[bool, Optional[int]]:
        """读全轴数(section 2/3,T_CHAR)。"""
        return self._execute(
            lambda: self._get_data(2, 3, 0, 0, _T_CHAR, _("读取全轴数"))
        )

    def read_spindle_axis_count(self) -> Tuple[bool, Optional[int]]:
        """读主轴数(section 2/4,T_CHAR)。"""
        return self._execute(
            lambda: self._get_data(2, 4, 0, 0, _T_CHAR, _("读取主轴数"))
        )

    def read_plc_axis_count(self) -> Tuple[bool, Optional[int]]:
        """读 PLC 轴数(section 2/5,T_CHAR)。"""
        return self._execute(
            lambda: self._get_data(2, 5, 0, 0, _T_CHAR, _("读取 PLC 轴数"))
        )

    # ------------------------------------------------------------------
    # 运行状态
    # ------------------------------------------------------------------

    def read_run_mode(self, system_no: int = 1) -> Tuple[bool, Optional[EzRunMode]]:
        """读运转模式(section 35/11,T_SHORT;C 库 read_status 第一段)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: EzRunMode(
                self._get_data(35, 11, system, 0, _T_SHORT, _("读取运转模式"))
            )
        )

    def read_run_status(self, system_no: int = 1) -> Tuple[bool, Optional[EzRunStatus]]:
        """读运转状态(section 35/10,T_SHORT;复位/急停/就绪/自动…)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: EzRunStatus(
                self._get_data(35, 10, system, 0, _T_SHORT, _("读取运转状态"))
            )
        )

    def read_auto_operation(self, system_no: int = 1) -> Tuple[bool, Optional[bool]]:
        """读自动运转中(section 35/20,T_DLONG;1 = 自动运转中)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: (
                self._get_data(35, 20, system, 0, _T_DLONG, _("读取自动运转状态")) == 1
            )
        )

    def read_run_state(self, system_no: int = 1) -> Tuple[bool, Optional[EzRunState]]:
        """读综合运行状态(三段拼合,C 库 ``m70_cnc_read_status`` 逻辑)。

        模式读取失败 → 整体失败;模式 = MEM/DNC 时读自动运转位
        (RUN/IDLE),模式 = LNK~LIN 时判 DEBUG;运转状态 = EMG 判 STOP。
        """
        system = self._check_system_no(system_no)

        def _read() -> EzRunState:
            mode = EzRunMode(
                self._get_data(35, 11, system, 0, _T_SHORT, _("读取运转模式"))
            )
            status = EzDeviceStatus.IDLE
            auto_run = None
            if mode in (EzRunMode.MEM, EzRunMode.DNC):
                auto_value = self._get_data(
                    35, 20, system, 0, _T_DLONG, _("读取自动运转状态")
                )
                auto_run = auto_value == 1
                if auto_run:
                    status = EzDeviceStatus.RUN
            elif EzRunMode.LNK <= mode <= EzRunMode.LINE:
                status = EzDeviceStatus.DEBUG
            run_status = EzRunStatus(
                self._get_data(35, 10, system, 0, _T_SHORT, _("读取运转状态"))
            )
            if run_status == EzRunStatus.EMERGENCY:
                status = EzDeviceStatus.STOP
            return EzRunState(
                device_status=status,
                mode=mode,
                run_status=run_status,
                auto_run=auto_run,
            )

        return self._execute(_read)

    # ------------------------------------------------------------------
    # 轴与位置
    # ------------------------------------------------------------------

    def read_axis_position(
        self,
        system_no: int = 1,
        axis: int = 1,
        position: EzPositionType = EzPositionType.MACHINE,
    ) -> Tuple[bool, Optional[float]]:
        """读轴位置(section 37/位置种类,T_FLOATBIN;单轴)。

        :param position: 位置种类(:class:`EzPositionType`,缺省机械坐标)
        """
        system = self._check_system_no(system_no)
        axis_flag = self._check_axis(axis)
        return self._execute(
            lambda: float(
                self._get_data(
                    37, int(position), system, axis_flag, _T_FLOATBIN, _("读取轴位置")
                )
            )
        )

    def read_all_axis_positions(
        self,
        system_no: int = 1,
        position: EzPositionType = EzPositionType.MACHINE,
    ) -> Tuple[bool, Optional[List[float]]]:
        """读全轴位置(先读 NC 轴数,再逐轴读取;C 库
        ``m70_cnc_read_all_axis_position`` 同构)。"""
        system = self._check_system_no(system_no)

        def _read() -> List[float]:
            count = int(self._get_data(2, 2, 0, 0, _T_CHAR, _("读取 NC 轴数")))
            positions = []
            for axis in range(1, count + 1):
                value = self._get_data(
                    37,
                    int(position),
                    system,
                    self._check_axis(axis),
                    _T_FLOATBIN,
                    _("读取轴位置"),
                )
                positions.append(float(value))
            return positions

        return self._execute(_read)

    def read_axis_names(self, system_no: int = 1) -> Tuple[bool, Optional[List[str]]]:
        """读轴名列表(section 127/1 按轴,T_STR;C 库
        ``m70_cnc_read_axis_name_ex`` 同构)。"""
        system = self._check_system_no(system_no)

        def _read() -> List[str]:
            count = int(self._get_data(2, 2, 0, 0, _T_CHAR, _("读取 NC 轴数")))
            names = []
            for axis in range(1, count + 1):
                name = self._get_data(
                    127,
                    1,
                    system,
                    self._check_axis(axis),
                    _T_STR,
                    _("读取轴名"),
                )
                names.append(str(name))
            return names

        return self._execute(_read)

    def read_svo_load(
        self, system_no: int = 1, axis: int = 1, absolute: bool = False
    ) -> Tuple[bool, Optional[int]]:
        """读伺服负载(section 59/4,T_SHORT,按轴)。"""
        system = self._check_system_no(system_no)
        axis_flag = self._check_axis(axis)

        def _read() -> int:
            value = self._get_data(
                59, 4, system, axis_flag, _T_SHORT, _("读取伺服负载")
            )
            return abs(int(value)) if absolute else int(value)

        return self._execute(_read)

    # ------------------------------------------------------------------
    # 主轴与进给
    # ------------------------------------------------------------------

    def read_spindle_speed(
        self, system_no: int = 1, axis: int = 1
    ) -> Tuple[bool, Optional[int]]:
        """读主轴转速 rpm(section 34/1,T_DLONG,按轴)。"""
        system = self._check_system_no(system_no)
        axis_flag = self._check_axis(axis)
        return self._execute(
            lambda: int(
                self._get_data(34, 1, system, axis_flag, _T_DLONG, _("读取主轴转速"))
            )
        )

    def read_spindle_load(
        self, system_no: int = 1, axis: int = 1, absolute: bool = False
    ) -> Tuple[bool, Optional[int]]:
        """读主轴负载 %(section 63/4,T_DLONG,按轴)。"""
        system = self._check_system_no(system_no)
        axis_flag = self._check_axis(axis)

        def _read() -> int:
            value = self._get_data(
                63, 4, system, axis_flag, _T_DLONG, _("读取主轴负载")
            )
            return abs(int(value)) if absolute else int(value)

        return self._execute(_read)

    def read_feed_speed(
        self, system_no: int = 1, feed: EzFeedSpeedType = EzFeedSpeedType.FC
    ) -> Tuple[bool, Optional[float]]:
        """读进给速度(T_FLOATBIN;FA/FM/FS/FE 走 section 42,FC 走 33)。"""
        system = self._check_system_no(system_no)
        if feed == EzFeedSpeedType.FC:
            section, sub_section = 33, 1
        else:
            section, sub_section = 42, int(feed) + 1
        return self._execute(
            lambda: float(
                self._get_data(
                    section, sub_section, system, 0, _T_FLOATBIN, _("读取进给速度")
                )
            )
        )

    # ------------------------------------------------------------------
    # 程序与报警
    # ------------------------------------------------------------------

    def read_main_program_name(self, system_no: int = 1) -> Tuple[bool, Optional[str]]:
        """读当前主程序号(section 45/101,T_STR)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: str(self._get_data(45, 101, system, 0, _T_STR, _("读取主程序号")))
        )

    def read_sub_program_name(self, system_no: int = 1) -> Tuple[bool, Optional[str]]:
        """读当前子程序号(section 45/201,T_STR)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: str(self._get_data(45, 201, system, 0, _T_STR, _("读取子程序号")))
        )

    def read_program_file_info(
        self, system_no: int = 1, info: EzProgramFileInfo = EzProgramFileInfo.REGISTERED
    ) -> Tuple[bool, Optional[int]]:
        """读程序文件信息(section 25;登录数/剩余数/容量/剩余/传送尺寸)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: int(
                self._get_data(
                    25, int(info), system, 0, _T_DLONG, _("读取程序文件信息")
                )
            )
        )

    def read_program_block(
        self, system_no: int = 1, rows: int = 10
    ) -> Tuple[bool, Optional[EzProgramBlock]]:
        """读当前程序块(mochaGetCurrentPrgBlockFirst;取响应首条)。

        :param rows: 请求行数(C 库 row_count 直传;响应恒单条结构,
            多行布局待真机核)
        """
        if not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= 120:
            raise ValueError(_("rows 必须在 1~120,收到:{!r}").format(rows))
        system = self._check_system_no(system_no)
        frame = build_prog_block_request(self._request_id, system, rows)
        return self._execute(
            lambda: self._get_special(frame, _("读取程序块"), decode_prog_block)
        )

    def read_alarms(
        self,
        system_no: int = 1,
        count: int = 10,
        alarm_type: EzAlarmType = EzAlarmType.ALL,
    ) -> Tuple[bool, Optional[List[EzAlarm]]]:
        """读当前报警(mochaGetCurrentAlarmMsgFirst;按响应逐条解码)。

        :param count: 请求条数(1~10;响应条数以实际数据段为准)
        :param alarm_type: 报警种类(:class:`EzAlarmType`)
        """
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= 10
        ):
            raise ValueError(_("count 必须在 1~10,收到:{!r}").format(count))
        if not isinstance(alarm_type, EzAlarmType):
            raise ValueError(
                _("alarm_type 必须是 EzAlarmType 枚举,收到:{!r}").format(alarm_type)
            )
        system = self._check_system_no(system_no)
        frame = build_alarm_request(self._request_id, system, count, int(alarm_type))
        return self._execute(
            lambda: self._get_special(frame, _("读取报警"), decode_alarms)
        )

    def read_is_alarm(self, system_no: int = 1) -> Tuple[bool, Optional[bool]]:
        """读是否报警中(请求 1 条全部报警,文本长度 > 0 即报警中)。"""
        system = self._check_system_no(system_no)

        def _read() -> bool:
            frame = build_alarm_request(
                self._request_id, system, 1, int(EzAlarmType.ALL)
            )
            alarms = self._get_special(frame, _("读取报警"), decode_alarms)
            return any(alarm.length > 0 for alarm in alarms)

        return self._execute(_read)

    # ------------------------------------------------------------------
    # 时间统计与杂项
    # ------------------------------------------------------------------

    def _read_time(self, sub_section: int, action: str) -> Any:
        """时间统计原语(section 40,T_UINT32;内部方法)。"""
        return self._get_data(40, sub_section, 0, 0, _T_UINT32, action)

    def read_power_on_time(self) -> Tuple[bool, Optional[int]]:
        """读电源通电时间(section 40/1)。"""
        return self._execute(lambda: int(self._read_time(1, _("读取电源通电时间"))))

    def read_auto_operation_time(self) -> Tuple[bool, Optional[int]]:
        """读自动运转时间(section 40/2)。"""
        return self._execute(lambda: int(self._read_time(2, _("读取自动运转时间"))))

    def read_auto_startup_time(self) -> Tuple[bool, Optional[int]]:
        """读自动启动时间(section 40/3)。"""
        return self._execute(lambda: int(self._read_time(3, _("读取自动启动时间"))))

    def read_cycle_time(self) -> Tuple[bool, Optional[int]]:
        """读周期时间(section 40/8)。"""
        return self._execute(lambda: int(self._read_time(8, _("读取周期时间"))))

    def read_cutting_time(self) -> Tuple[bool, Optional[int]]:
        """读切削时间(section 40/100)。"""
        return self._execute(lambda: int(self._read_time(100, _("读取切削时间"))))

    def read_external_accumulative_time(self) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """读外部累计时间(section 40/4 + 40/5 两段)。"""
        return self._execute(
            lambda: (
                int(self._read_time(4, _("读取外部累计时间"))),
                int(self._read_time(5, _("读取外部累计时间"))),
            )
        )

    def read_system_datetime(self) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """读 NC 日期/时刻原始值(section 40/6 + 40/7)。

        原始 u32(BCD/打包格式待真机核,C 库亦原样透传)。
        """
        return self._execute(
            lambda: (
                int(self._read_time(6, _("读取 NC 日期"))),
                int(self._read_time(7, _("读取 NC 时刻"))),
            )
        )

    def read_counter(self, system_no: int = 1) -> Tuple[bool, Optional[int]]:
        """读计数器(section 126/8002,T_LONG)。"""
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: int(self._get_data(126, 8002, system, 0, _T_LONG, _("读取计数器")))
        )

    def read_current_tool_no(self, system_no: int = 1) -> Tuple[bool, Optional[int]]:
        """读当前主轴刀号(R 寄存器路由 section 55/100536 = R536,T_SHORT)。

        C 库注释另有 (21/1,T_LONG) 取法(「M_SEC_SP_WAIT」)——按
        C 库 ``#if true`` 生效分支取 R536 路由;另一取法待真机核。
        """
        system = self._check_system_no(system_no)
        return self._execute(
            lambda: int(
                self._get_data(55, 100536, system, 0, _T_SHORT, _("读取当前刀号"))
            )
        )
