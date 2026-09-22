"""EtherNet/IP(CIP)编解码纯函数——ENIP 封装 + CIP 消息路由服务。

帧格式按 CIP/EtherNet/IP 规范实现,并经参考实现交叉核证
(pylogix 1.1.6 / cm_ethernetip 0.1.0 / aphyt 0.1.30 / pycomm3 1.2.16,
见 architecture.md §8.1):

- ENIP 封装头 24 字节:command(H) + length(H) + session(I) + status(I)
  + sender context(8) + options(I)
- RegisterSession ``0x0065``(载荷 4 字节 = 协议版本 1 + 选项 0)、
  UnregisterSession ``0x0066``、SendRRData ``0x006F``
- SendRRData 公共包格式(CPF)两项:NullAddress(0x0000,长 0)
  + UnconnectedData(0x00B2;0x00B1 是 connected 数据项,不用于 RRData)
- CIP Unconnected Send(服务 ``0x52``,Connection Manager 类 0x06 实例 1)
  包裹实际标签服务,路由段 = 背板端口(0x01)+ 槽号;NJ/NX 等内置以太网口
  设备目标即消息路由器本体,不经此包裹,请求/应答直接承载于 0xB2 项
  (见 :mod:`omniplc.plc.omron.cip`,pycomm3 对 Micro800 同款处理)
- Logix 标签服务:读 ``0x4C`` / 写 ``0x4D`` / 读-改-写 ``0x4E``
- 符号段 ``0x91`` + 名字长度 + 名字(补齐偶对齐);元素段
  ``0x28``/``0x29``/``0x2A``(1/2/4 字节下标)
- 读应答 = 服务回显 + 状态 + 2 字节类型域(结构体为 0xA0 + 模板号)+ 数据

本模块全部为纯函数(bytes ↔ 结构),不接触 socket。
"""
from __future__ import annotations

import struct
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .address import AbTag
from ...core.constants import (
    AB_CIP_EXTENDED_STATUS_TEXT,
    AB_CIP_STATUS_TEXT,
    AB_EIP_STATUS_TEXT,
    AB_EIP_STRING_MAX_CHARS,
    AB_EIP_STRING_STRUCT_ID,
    AB_MAX_BATCH_SERVICES,
)
from ...core.errors import DeviceError, ProtocolFrameError
from ...core.validation import (
    check_int16,
    check_range,
    check_uint16,
    require_float,
    require_int,
)
from ...types import DataType, PrimitiveValue

# ---- ENIP 封装(报文常量) ----
EIP_COMMAND_REGISTER_SESSION: int = 0x0065
EIP_COMMAND_UNREGISTER_SESSION: int = 0x0066
EIP_COMMAND_SEND_RR_DATA: int = 0x006F
EIP_COMMAND_SEND_UNIT_DATA: int = 0x0070
"""ENIP SendUnitData:connected 显式报文(0xA1 地址项 + 0xB1 数据项 + 序列号)。"""
EIP_HEADER_SIZE: int = 24
"""ENIP 封装头长度(command/length/session/status/context/options)。"""
EIP_RRDATA_PREFIX_SIZE: int = 16
"""RRData 头之后、CIP 数据之前的前缀:
interface(4) + timeout(2) + 项数(2) + NullAddress 项(4) + 数据项头(4)。"""
_CPF_ITEM_NULL_ADDRESS: int = 0x0000
_CPF_ITEM_UNCONNECTED_DATA: int = 0x00B2
_CPF_ITEM_CONNECTED_ADDRESS: int = 0x00A1
"""CPF 连接地址项:4 字节连接 ID(connected 报文)。"""
_CPF_ITEM_CONNECTED_DATA: int = 0x00B1
"""CPF 连接数据项:序列号 + CIP 报文(connected 报文)。"""

# ---- CIP 服务码 ----
CIP_SERVICE_GET_ATTRIBUTES_ALL: int = 0x01
"""CIP Get_Attributes_All(对象全部属性读取,Identity Object 等通用)。"""
CIP_SERVICE_GET_ATTRIBUTE_LIST: int = 0x03
"""CIP Get_Attribute_List(指定属性号列表读取)。"""
CIP_SERVICE_UNCONNECTED_SEND: int = 0x52
CIP_SERVICE_READ_TAG: int = 0x4C
CIP_SERVICE_WRITE_TAG: int = 0x4D
CIP_SERVICE_READ_MODIFY_WRITE: int = 0x4E
CIP_SERVICE_FORWARD_OPEN: int = 0x54
"""CIP Forward Open(Connection Manager,普通连接,参数域 16 位)。"""
CIP_SERVICE_LARGE_FORWARD_OPEN: int = 0x5B
"""CIP Large Forward Open(参数域 32 位,连接尺寸 >505 时使用)。"""
CIP_SERVICE_FORWARD_CLOSE: int = 0x4E
"""CIP Forward Close(Connection Manager;与标签 RMW 同码不同类,不冲突)。"""
CIP_SERVICE_MULTIPLE: int = 0x0A
"""CIP Multiple Service Packet(批量内嵌服务,发往消息路由器 0x02/0x01)。"""

# ---- ENIP 封装命令扩展(发现/调试) ----
EIP_COMMAND_LIST_IDENTITY: int = 0x0063
"""ENIP ListIdentity(广播/单播发现,无 CIP 会话)。"""

# Identity Object 类/实例号(CIP Vol 1 §5-1)
CIP_CLASS_IDENTITY: int = 0x01
"""Identity Object 类号(CIP 通用 Class ID 0x01)。"""
CIP_INSTANCE_IDENTITY: int = 0x01
"""Identity Object 实例号(GetAttributesAll/GetAttributeList 实例属性用 1)。"""
CIP_CLASS_MESSAGE_ROUTER: int = 0x02
"""Message Router 类号(Multiple Service Packet 0x0A 的目标对象)。"""
CIP_INSTANCE_MESSAGE_ROUTER: int = 0x01
"""Message Router 实例号(pylogix 多标签请求同口径)。"""

# ---- Forward Open / SendUnitData 参数(报文常量,沿用参考库惯例值) ----
FO_PRIORITY_TIME_TICK: int = 0x0A
FO_TIMEOUT_TICKS: int = 0x0E
FO_TIMEOUT_MULTIPLIER: int = 0x03
FO_TRANSPORT_TRIGGER: int = 0xA3
"""传输类/触发器:Class 3(应用触发,显式报文连接)。"""
FO_OT_RPI: int = 0x00201234
"""O->T 请求包间隔 RPI(微秒粒度)。"""
FO_TO_RPI: int = 0x00204001
"""T->O 请求包间隔 RPI(微秒粒度)。"""
FO_PARAM_BASE: int = 0x4200
"""网络连接参数基底:点对点(bit14)+ 固定尺寸(bit9),低 9 位为连接尺寸。"""
CONNECTION_SIZE_LARGE: int = 4002
"""Large Forward Open 连接尺寸(优先尝试)。"""
CONNECTION_SIZE_NORMAL: int = 504
"""普通 Forward Open 连接尺寸(Large 被拒时回落)。"""

# ---- CIP 类型码(Logix) ----
CIP_TYPE_BOOL: int = 0xC1
CIP_TYPE_DWORD: int = 0xD3
"""BOOL 数组的存储类型(Logix 把 BOOL 数组按 32 位字打包)。"""
CIP_TYPE_STRUCT: int = 0xA0

# DataType → CIP 类型码(请求侧)
_DATA_TYPE_CODES: Dict[DataType, int] = {
    DataType.BOOL: 0xC1,
    DataType.SHORT: 0xC3,
    DataType.USHORT: 0xC7,
    DataType.INT: 0xC4,
    DataType.UINT: 0xC8,
    DataType.LONG: 0xC5,
    DataType.ULONG: 0xC9,
    DataType.FLOAT: 0xCA,
    DataType.DOUBLE: 0xCB,
}

# CIP 类型码 → (字节大小, struct 解码格式, 名称)
_CIP_TYPE_LAYOUTS: Dict[int, Tuple[int, str, str]] = {
    0xC1: (1, "<B", "BOOL"),
    0xC2: (1, "<b", "SINT"),
    0xC3: (2, "<h", "INT"),
    0xC4: (4, "<i", "DINT"),
    0xC5: (8, "<q", "LINT"),
    0xC6: (1, "<B", "USINT"),
    0xC7: (2, "<H", "UINT"),
    0xC8: (4, "<I", "UDINT"),
    0xC9: (8, "<Q", "LWORD"),
    0xCA: (4, "<f", "REAL"),
    0xCB: (8, "<d", "LREAL"),
    0xD3: (4, "<I", "DWORD"),
}
# 支持位访问(位号后缀/位写入)的整型类型码
_BIT_ACCESS_CODES = frozenset((0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xD3))
# 按字节大小的无符号解码格式(位提取用)
_UNSIGNED_FORMATS: Dict[int, str] = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}


def data_type_code(data_type: DataType) -> int:
    """omniplc :class:`DataType` → CIP 类型码。

    :raises ValueError: 不支持的类型
    """
    try:
        return _DATA_TYPE_CODES[data_type]
    except KeyError:
        raise ValueError("AB 不支持的数据类型:{}".format(data_type))


def type_name(cip_type: int) -> str:
    """CIP 类型码 → 可读名称(未知码返回十六进制原文)。"""
    layout = _CIP_TYPE_LAYOUTS.get(cip_type)
    if layout is not None:
        return layout[2]
    if cip_type == CIP_TYPE_STRUCT:
        return "STRUCT"
    return "0x{:02X}".format(cip_type)


def type_size(cip_type: int) -> int:
    """CIP 类型码 → 元素字节大小(结构体按 STRING 结构 88 字节)。"""
    layout = _CIP_TYPE_LAYOUTS.get(cip_type)
    if layout is not None:
        return layout[0]
    if cip_type == CIP_TYPE_STRUCT:
        return 4 + AB_EIP_STRING_MAX_CHARS + 2
    raise ValueError("不支持的 CIP 类型:0x{:02X}".format(cip_type))


def is_bit_access_type(cip_type: int) -> bool:
    """该类型码是否支持位号访问(整型族 + BOOL 数组存储字)。"""
    return cip_type in _BIT_ACCESS_CODES


# ----------------------------------------------------------------------
# ENIP 封装
# ----------------------------------------------------------------------

def build_register_session() -> bytes:
    """构造 RegisterSession 请求(会话注册)。"""
    return struct.pack(
        "<HHIIQIHH",
        EIP_COMMAND_REGISTER_SESSION,
        4,
        0,
        0,
        0,
        0,
        1,  # 协议版本
        0,  # 选项
    )


def parse_register_session(reply: bytes) -> int:
    """解析 RegisterSession 应答,返回会话句柄。

    :raises ProtocolFrameError: 命令不符 / 封装状态非 0 / 长度不符
    """
    _check_enip_reply(reply, EIP_COMMAND_REGISTER_SESSION)
    if len(reply) < EIP_HEADER_SIZE + 4:
        raise ProtocolFrameError(
            "RegisterSession 应答长度不足:{} 字节".format(len(reply))
        )
    return struct.unpack_from("<I", reply, 4)[0]


def build_unregister_session(session_handle: int) -> bytes:
    """构造 UnregisterSession 请求(会话注销)。"""
    return struct.pack(
        "<HHIIQI",
        EIP_COMMAND_UNREGISTER_SESSION,
        0,
        session_handle,
        0,
        0,
        0,
    )


def build_rr_data(session_handle: int, cip_request: bytes) -> bytes:
    """把 CIP 请求封装为 SendRRData 帧(CPF:NullAddress + UnconnectedData)。

    CPF 超时域填 1 秒(HSL/IoTClient/pycomm3 惯例;个别服务端对 0 拒收)。
    """
    header = struct.pack(
        "<HHIIQI",
        EIP_COMMAND_SEND_RR_DATA,
        EIP_RRDATA_PREFIX_SIZE + len(cip_request),
        session_handle,
        0,
        0,
        0,
    )
    prefix = struct.pack(
        "<IHHHHHH",
        0,  # interface handle
        1,  # timeout(秒)
        2,  # CPF 项数
        _CPF_ITEM_NULL_ADDRESS,
        0,
        _CPF_ITEM_UNCONNECTED_DATA,
        len(cip_request),
    )
    return header + prefix + cip_request


def _check_enip_reply(
    reply: bytes, expected_command: Union[int, Tuple[int, ...]]
) -> None:
    """校验 ENIP 封装头(命令/长度/封装状态),内部函数。

    ``expected_command`` 传元组即多命令宽容:非连接请求规范上以 SendRRData
    (0x6F)应答,但个别模拟器以 0x66(UnregisterSession 的命令号,疑为把
    SendUnitData 记成 0x66)回帧——pylogix 客户端对响应命令号不做校验,
    此处对齐该宽容,应答体由调用方按 CPF 布局分流解析。
    """
    if len(reply) < EIP_HEADER_SIZE:
        raise ProtocolFrameError("ENIP 应答头不完整:{} 字节".format(len(reply)))
    command, length, _, status = struct.unpack_from("<HHII", reply, 0)
    allowed = (expected_command,) if isinstance(expected_command, int) \
        else tuple(expected_command)
    if command not in allowed:
        canonical = allowed[0]
        raise ProtocolFrameError(
            "ENIP 命令不符:期望 0x{:04X},实际 0x{:04X}".format(canonical, command)
        )
    if len(reply) - EIP_HEADER_SIZE != length:
        raise ProtocolFrameError(
            "ENIP 长度域不符:声明 {},实际 {}".format(length, len(reply) - EIP_HEADER_SIZE)
        )
    if status != 0:
        text = AB_EIP_STATUS_TEXT.get(status, "未知状态")
        raise ProtocolFrameError(
            "ENIP 封装状态 0x{:08X}({})".format(status, text)
        )


# ----------------------------------------------------------------------
# CIP Unconnected Send 包裹与标签服务
# ----------------------------------------------------------------------

def build_uc_send(request: bytes, slot: int) -> bytes:
    """把标签服务请求包裹进 CIP Unconnected Send(路由:背板端口 + 槽号)。

    布局:服务 0x52 + CM 路径(20 06 24 01)+ 优先级/超时 + 内嵌请求
    字节数 + 内嵌请求(补齐偶对齐)+ 路由段(path_size + 保留 + 端口 +
    槽号)。path_size 单位 **16 位字**:路由 ``01 slot`` 为 1 字 → 恒 1
    (HSL 实帧 / IoTClient / pycomm3 三方一致;曾误填字节数 2,真机 Logix
    按规范解析会报路由错误)。
    """
    if len(request) > 0xFFFF:
        raise ValueError("CIP 请求超过 65535 字节:{}".format(len(request)))
    frame = bytearray(
        struct.pack(
            "<BBBBBBBBH",
            CIP_SERVICE_UNCONNECTED_SEND,
            2,  # CM 路径字数(class 1 字 + instance 1 字)
            0x20,
            0x06,
            0x24,
            0x01,
            0x0A,  # 优先级
            0xF0,  # 超时 ticks(HSL/IoTClient 惯例值)
            len(request),
        )
    )
    frame += request
    if len(request) % 2:
        frame += b"\x00"
    frame += bytes((1, 0x00, 0x01, slot))
    return bytes(frame)


def build_symbol_path(
    members: Tuple[str, ...], indices: Tuple[Tuple[int, ...], ...], zero_last_index: bool = False
) -> bytes:
    """构造标签请求路径(IOI):逐级符号段 + 元素段。

    :param members: 逐级成员名(结构体成员以 ``.`` 分级后拆开)
    :param indices: 与 members 对齐的每级下标元组(无数组下标为空元组)
    :param zero_last_index: 末级成员下标置 0(类型发现读首元素用)
    """
    path = bytearray()
    last = len(members) - 1
    for position, member in enumerate(members):
        member_indices = indices[position]
        if zero_last_index and position == last and member_indices:
            member_indices = (0,) * len(member_indices)
        encoded = member.encode("utf-8")
        if not 1 <= len(encoded) <= 0xFF:
            raise ValueError("AB 标签段长度非法:{}".format(member))
        path += struct.pack("<BB", 0x91, len(encoded))
        path += encoded
        if len(encoded) % 2:
            path += b"\x00"
        for number in member_indices:
            if number < 0x100:
                path += struct.pack("<BB", 0x28, number)
            elif number < 0x10000:
                path += struct.pack("<BH", 0x29, number)
            else:
                path += struct.pack("<BI", 0x2A, number)
    return bytes(path)


def _service_request(service: int, path: bytes, body: bytes) -> bytes:
    """服务请求通用组装:服务码 + 路径字数 + 路径 + 服务数据(内部函数)。"""
    if len(path) % 2:
        raise ValueError("CIP 路径长度必须为偶数:{}".format(len(path)))
    return struct.pack("<BB", service, len(path) // 2) + path + body


def build_class_instance_path(class_id: int, instance: int) -> bytes:
    """构造 class(8 位段)+ instance(8 位段)EPATH(内部函数)。

    CIP Vol 1 §5-2:8 位段头 = ``0x20 | logical_type``,其中
    ``class_id`` logical_type=0x00,``instance_id``=0x04。class 与
    instance 各 1 字节,共 4 字节(偶长度满足 :func:`_service_request`)。
    """
    if not 0 <= class_id <= 0xFF:
        raise ValueError("CIP class_id 超出 0~255:{}".format(class_id))
    if not 0 <= instance <= 0xFF:
        raise ValueError("CIP instance 超出 0~255:{}".format(instance))
    return bytes((0x20, class_id, 0x24, instance))


def build_get_attributes_all(class_id: int, instance: int) -> bytes:
    """构造 Get_Attributes_All 请求(服务 0x01,无 body)。"""
    return _service_request(
        CIP_SERVICE_GET_ATTRIBUTES_ALL,
        build_class_instance_path(class_id, instance),
        b"",
    )


def build_get_attribute_list(
    class_id: int, instance: int, attributes: Tuple[int, ...]
) -> bytes:
    """构造 Get_Attribute_List 请求(服务 0x03,body = 属性数 + 属性号列表)。"""
    for attr in attributes:
        if not 0 <= attr <= 0xFFFF:
            raise ValueError("CIP 属性号超出 0~65535:{}".format(attr))
    body = struct.pack("<H", len(attributes)) + b"".join(
        struct.pack("<H", a) for a in attributes
    )
    return _service_request(
        CIP_SERVICE_GET_ATTRIBUTE_LIST,
        build_class_instance_path(class_id, instance),
        body,
    )


def build_tag_read(path: bytes, elements: int = 1) -> bytes:
    """构造 Tag Read 请求(服务 0x4C)。"""
    return _service_request(CIP_SERVICE_READ_TAG, path, struct.pack("<H", elements))


def build_multiple_service_packet(requests: Sequence[bytes]) -> bytes:
    """构造 Multiple Service Packet(0x0A)服务请求。

    数据域 = 条数(u16 LE)+ 偏移(u16 LE × n,**自条数域首字节起算**,
    pylogix ``_build_multi_service_header`` 与 cm_ethernetip
    ``multi_service.handle_multi_service`` 双参考同口径)+ 内嵌服务请求;
    每条内嵌请求补齐偶数字节(ODVA CIP 字对齐),偏移含补齐字节。

    :raises ValueError: 无请求或条数超出 :data:`AB_MAX_BATCH_SERVICES`
    """
    if not requests:
        raise ValueError("多服务包至少需要一条内嵌服务请求")
    if len(requests) > AB_MAX_BATCH_SERVICES:
        raise ValueError(
            "多服务包内嵌服务数超出上限 {}:{}".format(
                AB_MAX_BATCH_SERVICES, len(requests)
            )
        )
    head = bytearray(len(requests).to_bytes(2, "little"))
    segments = bytearray()
    offset = 2 + 2 * len(requests)
    for request in requests:
        head += offset.to_bytes(2, "little")
        segments += request
        pad = len(request) % 2
        segments += b"\x00" * pad
        offset += len(request) + pad
    data = bytes(head) + bytes(segments)
    return _service_request(
        CIP_SERVICE_MULTIPLE,
        build_class_instance_path(CIP_CLASS_MESSAGE_ROUTER, CIP_INSTANCE_MESSAGE_ROUTER),
        data,
    )


def parse_multiple_service_payload(
    data: bytes, expected_services: Sequence[int]
) -> List[bytes]:
    """解析 Multiple Service Packet(0x0A)应答数据域,返回逐条内嵌服务数据。

    内层布局与请求对称:条数(u16 LE)+ 偏移 × n + 内嵌服务应答
    (标准 CIP 应答帧 = 回显 | 0x80 + 保留 + 通用状态 + 附加长 + 数据),
    逐条经 :func:`_parse_service_payload` 校验回显与状态(含扩展码诊断)。

    :param data: 外层 0x0A 应答的数据域(已由事务层剥掉封装与服务头)
    :raises ValueError: 条数不符
    :raises ProtocolFrameError: 帧结构/内嵌回显不符
    :raises omniplc.core.errors.DeviceError: 任一内嵌服务 CIP 状态非 0
    """
    if len(data) < 2:
        raise ProtocolFrameError("多服务包应答数据不足")
    count = int.from_bytes(data[0:2], "little")
    if count != len(expected_services):
        raise ValueError(
            "多服务包应答条数不符:期望 {},实际 {}".format(len(expected_services), count)
        )
    segments: List[bytes] = []
    for index in range(count):
        base = 2 + index * 2
        offset = int.from_bytes(data[base:base + 2], "little")
        if index + 1 < count:
            end = int.from_bytes(data[base + 2:base + 4], "little")
        else:
            end = len(data)
        segments.append(data[offset:end])
    return [
        _parse_service_payload(segment, expected_services[index])
        for index, segment in enumerate(segments)
    ]


def build_tag_write(path: bytes, cip_type: int, payload: bytes, elements: int = 1) -> bytes:
    """构造 Tag Write 请求(服务 0x4D,原子类型:类型码 + 0x00 + 点数 + 数据)。"""
    body = struct.pack("<BBH", cip_type, 0, elements) + payload
    return _service_request(CIP_SERVICE_WRITE_TAG, path, body)


def build_string_write(path: bytes, payload: bytes, elements: int = 1) -> bytes:
    """构造 STRING 写请求(结构体类型域 = 0xA0 + 0x02 + 模板实例号)。"""
    body = (
        struct.pack("<BBH", CIP_TYPE_STRUCT, 2, AB_EIP_STRING_STRUCT_ID)
        + struct.pack("<H", elements)
        + payload
    )
    return _service_request(CIP_SERVICE_WRITE_TAG, path, body)


def build_read_modify_write(
    path: bytes, cip_type: int, or_mask: int, and_mask: int
) -> bytes:
    """构造 Read-Modify-Write 请求(服务 0x4E,设备侧原子位修改)。

    设备执行 ``(原值 | OR) & AND``;位清零时 OR=0 且 AND 屏蔽该位。
    """
    size = type_size(cip_type)
    body = struct.pack("<H", size)
    body += or_mask.to_bytes(size, "little")
    body += and_mask.to_bytes(size, "little")
    return _service_request(CIP_SERVICE_READ_MODIFY_WRITE, path, body)


def bit_masks(cip_type: int, bit: int, value: bool) -> Tuple[int, int]:
    """位写掩码:返回 ``(or_mask, and_mask)``(内部配合 0x4E 服务)。"""
    ones = (1 << (type_size(cip_type) * 8)) - 1
    or_mask = (1 << bit) if value else 0
    and_mask = ones if value else ones ^ (1 << bit)
    return or_mask, and_mask


# ----------------------------------------------------------------------
# connected 消息(Forward Open/Close + SendUnitData)
# ----------------------------------------------------------------------

def _forward_open_params(is_large: bool, connection_size: int) -> int:
    """网络连接参数域:P2P + 固定尺寸 + 连接尺寸(内部函数)。"""
    base = FO_PARAM_BASE << 16 if is_large else FO_PARAM_BASE
    return base + connection_size


def build_forward_open(
    is_large: bool,
    connection_size: int,
    connection_serial: int,
    to_connection_id: int,
    vendor_id: int,
    originator_serial: int,
    route_path: bytes,
) -> bytes:
    """构造 Forward Open(0x54)/ Large Forward Open(0x5B)请求 CIP 部分。

    O->T 连接 ID 传 0 由目标分配(应答返回);T->O 连接 ID 由发起方指定。
    连接路径 = 路由段 + 消息路由对象(20 02 24 01);AB 传背板路由
    (端口 0x01 + 槽号),NJ/NX 等内置口目标即 CPU 传空字节串。

    :raises ValueError: 路由段长度为奇数
    """
    service = CIP_SERVICE_LARGE_FORWARD_OPEN if is_large else CIP_SERVICE_FORWARD_OPEN
    frame = bytearray(
        struct.pack(
            "<BBBBBBBB",
            service,
            2,  # CM 路径字数
            0x20,
            0x06,
            0x24,
            0x01,
            FO_PRIORITY_TIME_TICK,
            FO_TIMEOUT_TICKS,
        )
    )
    frame += struct.pack("<II", 0, to_connection_id)
    frame += struct.pack("<HH", connection_serial, vendor_id)
    frame += struct.pack("<I", originator_serial)
    frame += struct.pack("<B3x", FO_TIMEOUT_MULTIPLIER)  # 乘数 + 3 保留字节
    frame += struct.pack("<I", FO_OT_RPI)
    params = _forward_open_params(is_large, connection_size)
    if is_large:
        frame += struct.pack("<I", params)
        frame += struct.pack("<I", FO_TO_RPI)
        frame += struct.pack("<I", params)
    else:
        frame += struct.pack("<H", params)
        frame += struct.pack("<I", FO_TO_RPI)
        frame += struct.pack("<H", params)
    frame += struct.pack("<B", FO_TRANSPORT_TRIGGER)
    if len(route_path) % 2:
        raise ValueError("CIP 路由段长度必须为偶数:{}".format(route_path.hex()))
    path = route_path + bytes((0x20, 0x02, 0x24, 0x01))
    frame += struct.pack("<B", len(path) // 2)
    frame += path
    return bytes(frame)


def parse_forward_open_reply(reply: bytes, request_service: int) -> Tuple[int, int]:
    """解析 Forward Open 应答,返回 ``(状态码, O->T 连接 ID)``。

    成功应答数据域以 O->T 连接 ID(4 字节)开头(目标分配);状态非 0
    (如连接尺寸超限)不抛错,由调用方决定 Large→普通 回落。

    :raises ProtocolFrameError: 封装/CPF/服务回显不符
    """
    cip = _parse_rr_data_cip(reply)
    reply_service = cip[0]
    if reply_service != (request_service | 0x80):
        raise ProtocolFrameError(
            "Forward Open 应答服务码不符:期望 0x{:02X},实际 0x{:02X}".format(
                request_service | 0x80, reply_service
            )
        )
    status = cip[2]
    if status != 0:
        return status, 0
    data_offset = 4 + cip[3]
    if len(cip) < data_offset + 4:
        raise ProtocolFrameError("Forward Open 应答数据域不完整")
    return 0, struct.unpack_from("<I", cip, data_offset)[0]


def build_forward_close(
    connection_serial: int, vendor_id: int, originator_serial: int, route_path: bytes
) -> bytes:
    """构造 Forward Close(0x4E)请求 CIP 部分(路径同 Forward Open)。

    :raises ValueError: 路由段长度为奇数
    """
    frame = bytearray(
        struct.pack(
            "<BBBBBBBB",
            CIP_SERVICE_FORWARD_CLOSE,
            2,
            0x20,
            0x06,
            0x24,
            0x01,
            FO_PRIORITY_TIME_TICK,
            FO_TIMEOUT_TICKS,
        )
    )
    frame += struct.pack("<HH", connection_serial, vendor_id)
    frame += struct.pack("<I", originator_serial)
    if len(route_path) % 2:
        raise ValueError("CIP 路由段长度必须为偶数:{}".format(route_path.hex()))
    path = route_path + bytes((0x20, 0x02, 0x24, 0x01))
    frame += struct.pack("<BB", len(path) // 2, 0x00)
    frame += path
    return bytes(frame)


def parse_forward_close_reply(reply: bytes) -> int:
    """解析 Forward Close 应答,返回状态码(非 0 由调用方尽力而为处理)。

    :raises ProtocolFrameError: 封装/CPF/服务回显不符
    """
    cip = _parse_rr_data_cip(reply)
    reply_service = cip[0]
    if reply_service != (CIP_SERVICE_FORWARD_CLOSE | 0x80):
        raise ProtocolFrameError(
            "Forward Close 应答服务码不符:期望 0xCE,实际 0x{:02X}".format(reply_service)
        )
    return cip[2]


def build_send_unit_data(
    session_handle: int, ot_connection_id: int, sequence: int, cip_request: bytes
) -> bytes:
    """把 CIP 请求封装为 SendUnitData 帧(connected 标签读写)。

    CPF:连接地址项(0xA1,O->T 连接 ID)+ 连接数据项(0xB1,序列号 + 请求)。
    """
    header = struct.pack(
        "<HHIIQI",
        EIP_COMMAND_SEND_UNIT_DATA,
        22 + len(cip_request),
        session_handle,
        0,
        0,
        0,
    )
    prefix = struct.pack("<IHH", 0, 1, 2)
    prefix += struct.pack("<HHI", _CPF_ITEM_CONNECTED_ADDRESS, 4, ot_connection_id)
    prefix += struct.pack(
        "<HHH", _CPF_ITEM_CONNECTED_DATA, len(cip_request) + 2, sequence
    )
    return header + prefix + cip_request


def parse_send_unit_data_reply(
    reply: bytes, request_service: int, to_connection_id: int, sequence: int
) -> bytes:
    """解析 SendUnitData 应答,返回标签服务数据域。

    校验连接地址项回显 T->O 连接 ID(发起方在 Forward Open 中指定)、
    序列号回显、服务回显与通用状态。

    :raises ProtocolFrameError: 封装/CPF/连接 ID/序列号/服务回显不符
    :raises DeviceError: CIP 通用状态非 0(不断线)
    """
    _check_enip_reply(reply, EIP_COMMAND_SEND_UNIT_DATA)
    prefix = reply[EIP_HEADER_SIZE:]
    if len(prefix) < 22:
        raise ProtocolFrameError("SendUnitData 前缀不完整:{} 字节".format(len(prefix)))
    item_count, address_type, address_length = struct.unpack_from("<HHH", prefix, 6)
    if item_count != 2 or address_type != _CPF_ITEM_CONNECTED_ADDRESS or address_length != 4:
        raise ProtocolFrameError("SendUnitData CPF 地址项非法")
    connection_id = struct.unpack_from("<I", prefix, 12)[0]
    if connection_id != to_connection_id:
        raise ProtocolFrameError(
            "连接地址项 T->O ID 不符:期望 0x{:08X},实际 0x{:08X}".format(
                to_connection_id, connection_id
            )
        )
    data_type, data_length, reply_sequence = struct.unpack_from("<HHH", prefix, 16)
    if data_type != _CPF_ITEM_CONNECTED_DATA:
        raise ProtocolFrameError("CPF 数据项类型非法:0x{:04X}".format(data_type))
    cip = prefix[22:]
    if len(cip) != data_length - 2:
        raise ProtocolFrameError(
            "CPF 数据项长度不符:声明 {},实际 {}".format(data_length - 2, len(cip))
        )
    if reply_sequence != sequence:
        raise ProtocolFrameError(
            "序列号回显不符:期望 {},实际 {}".format(sequence, reply_sequence)
        )
    return _parse_service_payload(cip, request_service)


# ----------------------------------------------------------------------
# 应答解析
# ----------------------------------------------------------------------

def _parse_rr_data_cip(reply: bytes) -> bytes:
    """校验 SendRRData 封装与 CPF,返回 CIP 数据(内部函数)。

    应答命令规范为 SendRRData(0x6F);个别模拟器以 0x66 回帧非连接请求
    (对齐 pylogix 不校验响应命令号的宽容,见 :func:`_check_enip_reply`)。
    应答体按 CPF 地址项类型分流:NullAddress(0x0000)+ UnconnectedData
    (0xB2)为规范布局;个别对端以连接式项(0xA1 地址 + 0xB1 数据 + 序列号)
    回帧,同样取其数据项载荷(T->O ID 与序列号无连接态可校验,跳过)。
    """
    _check_enip_reply(
        reply, (EIP_COMMAND_SEND_RR_DATA, EIP_COMMAND_UNREGISTER_SESSION)
    )
    prefix = reply[EIP_HEADER_SIZE:]
    if len(prefix) < EIP_RRDATA_PREFIX_SIZE:
        raise ProtocolFrameError("SendRRData 前缀不完整:{} 字节".format(len(prefix)))
    item_count, address_type, address_length, data_type, data_length = struct.unpack_from(
        "<HHHHH", prefix, 6
    )
    if item_count != 2:
        raise ProtocolFrameError("SendRRData CPF 项数非法:{}".format(item_count))
    if address_type == _CPF_ITEM_NULL_ADDRESS:
        if address_length != 0:
            raise ProtocolFrameError("SendRRData CPF 地址项非法")
        if data_type != _CPF_ITEM_UNCONNECTED_DATA:
            raise ProtocolFrameError("CPF 数据项类型非法:0x{:04X}".format(data_type))
        cip = prefix[EIP_RRDATA_PREFIX_SIZE:]
        if len(cip) != data_length:
            raise ProtocolFrameError(
                "CPF 数据项长度不符:声明 {},实际 {}".format(data_length, len(cip))
            )
    elif address_type == _CPF_ITEM_CONNECTED_ADDRESS:
        if address_length != 4:
            raise ProtocolFrameError("SendRRData 连接式 CPF 地址项非法")
        data_type, data_length = struct.unpack_from("<HH", prefix, 16)
        if data_type != _CPF_ITEM_CONNECTED_DATA:
            raise ProtocolFrameError("CPF 数据项类型非法:0x{:04X}".format(data_type))
        cip = prefix[22:]  # 跳过 2 字节序列号(无连接态可校验,丢弃)
        if len(cip) != data_length - 2:
            raise ProtocolFrameError(
                "CPF 数据项长度不符:声明 {},实际 {}".format(data_length - 2, len(cip))
            )
    else:
        raise ProtocolFrameError(
            "SendRRData CPF 地址项类型非法:0x{:04X}".format(address_type)
        )
    if len(cip) < 4:
        raise ProtocolFrameError("CIP 应答不完整")
    return cip


def parse_service_reply(reply: bytes, request_service: int) -> bytes:
    """解析 SendRRData 应答(Unconnected Send 包裹),返回内嵌标签服务的数据域。

    规范应答 = 0xD2(UC-Send 回显)+ 保留 + 路由状态 + 附加状态长 + 内嵌
    服务应答(服务回显 | 0x80 + 保留 + 通用状态 + 附加长 + 数据)。个别
    模拟器(HSL/pylogix 服务端)剥掉 0xD2 路由信封、直接回内嵌服务应答,
    同样接受——应答首字节不是 0xD2 时按裸服务应答解析。

    :raises ProtocolFrameError: 封装/CPF/服务回显不符
    :raises DeviceError: 路由状态或 CIP 通用状态非 0(不断线)
    """
    cip = _parse_rr_data_cip(reply)

    if cip[0] != (CIP_SERVICE_UNCONNECTED_SEND | 0x80):
        # 偏差对端:无 0xD2 路由信封,应答体即内嵌服务应答
        return _parse_service_payload(cip, request_service)
    route_status = cip[2]
    if route_status != 0:
        raise DeviceError(_status_text(route_status), route_status)

    return _parse_service_payload(cip[4 + cip[3]:], request_service)


def parse_direct_service_reply(reply: bytes, request_service: int) -> bytes:
    """解析 SendRRData 直发应答(不经 Unconnected Send 包裹),返回服务数据域。

    NJ/NX 等内置以太网口设备目标即消息路由器本体:RRData 的 Unconnected
    Data 项直接承载服务应答(服务回显 | 0x80 + 保留 + 通用状态 + 附加长 +
    数据),无 :func:`parse_service_reply` 要剥的 0xD2 外层头。

    :raises ProtocolFrameError: 封装/CPF/服务回显不符
    :raises DeviceError: CIP 通用状态非 0(不断线)
    """
    return _parse_service_payload(_parse_rr_data_cip(reply), request_service)


def _parse_service_payload(cip: bytes, request_service: int) -> bytes:
    """校验服务回显与通用状态,返回服务数据域(内部函数)。

    回显宽容:个别服务端(HSL AllenBradleyServer,用户联测实帧核证)写应答
    (0x4D)首字节回 0x00 而非服务回显 | 0x80;pylogix 客户端从不校验服务
    回显,本库事务为同步一问一答,回显对请求-应答关联是冗余,故对 0x00
    (回显省略)放行;非零错回显仍按坏帧拒。

    status 非 0 时把 16 位扩展子状态(命中 :data:`AB_CIP_EXTENDED_STATUS_TEXT`)
    拼到 :class:`DeviceError` 消息末尾;扩展码不进 ``code``(仍仅 8 位通用状态)。
    """
    if len(cip) < 4:
        raise ProtocolFrameError("CIP 服务应答不完整")
    reply_service = cip[0]
    if reply_service != (request_service | 0x80) and reply_service != 0x00:
        raise ProtocolFrameError(
            "标签服务回显不符:期望 0x{:02X},实际 0x{:02X}".format(
                request_service | 0x80, reply_service
            )
        )
    status = cip[2]
    if status != 0:
        ext = _extended_status_text(status, cip)
        msg = _status_text(status) if ext is None else "{} — {}".format(
            _status_text(status), ext
        )
        raise DeviceError(msg, status)
    return cip[4 + cip[3]:]


def status_text(status: int) -> str:
    """CIP 通用状态码 → 可读描述(未知码返回"未知错误")。"""
    return AB_CIP_STATUS_TEXT.get(status, "未知错误")


def _status_text(status: int) -> str:
    """CIP 通用状态 → 可读文本(内部函数)。"""
    return "CIP 状态 0x{:02X}({})".format(status, status_text(status))


def _extended_status_text(status: int, cip: bytes) -> Optional[str]:
    """从 CIP 应答字节里读 16 位扩展子状态码并查表,返回人话(内部函数)。

    布局:CIP 服务应答 = 服务回显 | 0x80(1) + 保留(1) + 通用状态(1) +
    size_of_additional_status(1,单位 16 位字) + N×2 字节扩展码 + 数据。

    :param status: 通用状态字节
    :param cip: 完整服务应答字节(从服务回显字节起算)
    :returns: ``"文本  (status=0xXX, extended=0xYYYY)"`` 或 ``None``
        (size=0 / 未命中表项 / 数据长度不足)
    """
    if len(cip) < 4:
        return None
    word_count = cip[3]
    if word_count == 0:
        return None
    ext_bytes = word_count * 2
    if len(cip) < 4 + ext_bytes:
        return None
    if word_count == 1:
        extended = cip[4]
    elif word_count == 2:
        extended = struct.unpack_from("<H", cip, 4)[0]
    elif word_count == 4:
        extended = struct.unpack_from("<I", cip, 4)[0]
    else:
        return None
    table = AB_CIP_EXTENDED_STATUS_TEXT.get(status)
    if not table:
        return None
    text = table.get(extended)
    if text is None:
        return None
    return "{}  ({:0>2X}, {:0>4X})".format(text, status, extended)


def parse_tag_read_payload(payload: bytes) -> Tuple[int, bytes]:
    """解析 Tag Read 应答数据域,返回 ``(类型码, 值字节)``。

    原子类型域为 2 字节(类型码 + 0x00);结构体为 0xA0 + 模板实例号
    (4 字节前缀,STRING 值从 ``len(u32)`` 起)。
    """
    if len(payload) < 2:
        raise ProtocolFrameError("Tag Read 应答数据域不完整")
    cip_type = payload[0]
    if cip_type == CIP_TYPE_STRUCT:
        if len(payload) < 4:
            raise ProtocolFrameError("结构体应答类型域不完整")
        return cip_type, payload[4:]
    return cip_type, payload[2:]


# ----------------------------------------------------------------------
# 通用 CIP 服务:ListIdentity / GetAttributesAll / GetAttributeList
# ----------------------------------------------------------------------

def build_list_identity() -> bytes:
    """构造 ListIdentity ENIP 请求(命令 0x63,载荷 0 字节,无 CIP 会话)。"""
    return struct.pack(
        "<HHIIQI",
        EIP_COMMAND_LIST_IDENTITY,
        0,  # length:无 CIP 载荷
        0,  # session handle:ListIdentity 不需要会话
        0,  # status
        0,  # sender context (低 8 字节)
        0,  # options
    )


def parse_list_identity_reply(reply: bytes) -> Dict[str, object]:
    """解析 ListIdentity ENIP 应答,返回 Identity Object 字段字典。

    布局(ODVA CIP Vol 2 §2-4.4.2 + pycomm3 1.2.16 cross-check):ENIP 头
    (24 字节)+ 2 字节兼容前缀(部分实现带 interface/version)+ Identity
    Object 字段(vendor 2 + product_type 2 + product_code 2 + revision 2 +
    status 2 + serial 4 + product_name_length 1 + product_name N + state 1)。

    :raises ProtocolFrameError: 长度不足 / 命令不符 / 封装状态非 0
    """
    _check_enip_reply(reply, EIP_COMMAND_LIST_IDENTITY)
    payload = reply[EIP_HEADER_SIZE:]
    # 跳过 2 字节兼容前缀(部分实现带 interface handle / version)
    if len(payload) < 2 + 14:
        raise ProtocolFrameError(
            "ListIdentity 应答载荷不足:{} 字节".format(len(payload))
        )
    body = payload[2:]
    vendor = struct.unpack_from("<H", body, 0)[0]
    product_type = struct.unpack_from("<H", body, 2)[0]
    product_code = struct.unpack_from("<H", body, 4)[0]
    rev_major, rev_minor = body[6], body[7]
    status_word = struct.unpack_from("<H", body, 8)[0]
    serial = struct.unpack_from("<I", body, 10)[0]
    name_len = body[14]
    if len(body) < 15 + name_len + 1:
        raise ProtocolFrameError("ListIdentity product_name 截断")
    product_name = bytes(body[15:15 + name_len]).decode("ascii", errors="replace")
    state = body[15 + name_len]
    return {
        "vendor": vendor,
        "product_type": product_type,
        "product_code": product_code,
        "revision": (rev_major, rev_minor),
        "status": status_word,
        "serial": serial,
        "product_name": product_name,
        "state": state,
    }


def parse_module_identity_payload(payload: bytes) -> Dict[str, object]:
    """解析 GetAttributesAll 应答的数据域(Identity Object 7 字段),返回字典。

    由 :func:`generic_message` 调用方传入从 ``_transact`` 取出的裸数据;
    payload = vendor(2)+ product_type(2)+ product_code(2)+ revision(2)+
    status(2)+ serial(4)+ product_name_length(1)+ product_name(N)。

    :raises ProtocolFrameError: 长度不足
    """
    if len(payload) < 14:
        raise ProtocolFrameError(
            "Identity Object 应答载荷不足:{} 字节".format(len(payload))
        )
    vendor = struct.unpack_from("<H", payload, 0)[0]
    product_type = struct.unpack_from("<H", payload, 2)[0]
    product_code = struct.unpack_from("<H", payload, 4)[0]
    rev_major, rev_minor = payload[6], payload[7]
    status_word = struct.unpack_from("<H", payload, 8)[0]
    serial = struct.unpack_from("<I", payload, 10)[0]
    name_len = payload[14]
    if len(payload) < 15 + name_len:
        raise ProtocolFrameError("Identity Object product_name 截断")
    product_name = bytes(payload[15:15 + name_len]).decode("ascii", errors="replace")
    return {
        "vendor": vendor,
        "product_type": product_type,
        "product_code": product_code,
        "revision": (rev_major, rev_minor),
        "status": status_word,
        "serial": serial,
        "product_name": product_name,
    }


def parse_get_attribute_list_payload(
    payload: bytes, attribute_decoders: Tuple[Tuple[int, Callable[[bytes], object]], ...]
) -> List[Tuple[int, object]]:
    """解析 GetAttributeList 应答数据域,按 ``attribute_decoders`` 顺序消费。

    payload 起点 = 属性数据(已被 :func:`_parse_service_payload` 剥掉 4 字节
    服务回显头);首 2 字节为属性返回个数,随后按 ``attribute_decoders`` 顺序
    每个解一段。解器失败抛 :class:`ValueError`,由调用方收口。

    :param attribute_decoders: ``(属性号, 解码函数)`` 元组列表,顺序需与请求一致
    :raises ProtocolFrameError: 长度不足 / 属性个数与解器不匹配
    """
    if len(payload) < 2:
        raise ProtocolFrameError("GetAttributeList 应答数据域不完整")
    count = struct.unpack_from("<H", payload, 0)[0]
    if count != len(attribute_decoders):
        raise ProtocolFrameError(
            "GetAttributeList 属性数不符:声明 {},期望 {}".format(
                count, len(attribute_decoders)
            )
        )
    offset = 2
    out: List[Tuple[int, object]] = []
    for attr_id, decoder in attribute_decoders:
        # 长度前缀:每个属性前置 2 字节 UINT 长度(ODVA CIP Vol 1 §5-4.4)
        if len(payload) < offset + 2:
            raise ProtocolFrameError("GetAttributeList 属性长度域截断")
        attr_len = struct.unpack_from("<H", payload, offset)[0]
        offset += 2
        if len(payload) < offset + attr_len:
            raise ProtocolFrameError("GetAttributeList 属性数据截断")
        value = decoder(payload[offset:offset + attr_len])
        out.append((attr_id, value))
        offset += attr_len
    return out


def decode_identity_string(data: bytes) -> str:
    """Identity Object SHORT_STRING(1 字节长度 + N 字节 ASCII)解码器(辅助函数)。"""
    if not data:
        return ""
    n = min(data[0], len(data) - 1)
    return bytes(data[1:1 + n]).decode("ascii", errors="replace")


def decode_values(
    data: bytes, cip_type: int, count: int = 1
) -> List[Union[int, float]]:
    """按 CIP 类型码解码定长值序列(BOOL 解码为 0/1,内部配合调用方)。"""
    if cip_type not in _CIP_TYPE_LAYOUTS:
        raise ValueError("不支持的 CIP 类型:0x{:02X}".format(cip_type))
    size, fmt, _ = _CIP_TYPE_LAYOUTS[cip_type]
    if len(data) < size * count:
        raise ProtocolFrameError(
            "应答数据不足:期望 {} 字节,实际 {} 字节".format(size * count, len(data))
        )
    return [struct.unpack_from(fmt, data, offset)[0] for offset in range(0, size * count, size)]


def decode_word(data: bytes, cip_type: int) -> int:
    """按类型大小无符号解码单词(位提取用)。"""
    size = type_size(cip_type)
    fmt = _UNSIGNED_FORMATS.get(size)
    if fmt is None:
        raise ValueError("不支持的位访问类型:0x{:02X}".format(cip_type))
    if len(data) < size:
        raise ProtocolFrameError("应答数据不足:{} 字节".format(size))
    return struct.unpack_from(fmt, data, 0)[0]


def decode_string_payload(data: bytes, encoding: str) -> str:
    """解码 STRING 结构体值字节(len u32 + 字符)。

    :raises ValueError: 非字符串结构
    """
    if len(data) < 4:
        raise ProtocolFrameError("STRING 应答不完整")
    length = struct.unpack_from("<I", data, 0)[0]
    if length > len(data) - 4:
        raise ProtocolFrameError(
            "STRING 长度域超出应答:声明 {},可用 {} 字节".format(length, len(data) - 4)
        )
    return bytes(data[4:4 + length]).decode(encoding)


def encode_string_struct(value: str, encoding: str) -> bytes:
    """编码 STRING 结构体值字节(len u32 + 字符 + 补齐 88 字节)。

    :raises ValueError: 超出 AB STRING 82 字符上限
    """
    raw = value.encode(encoding)
    if len(raw) > AB_EIP_STRING_MAX_CHARS:
        raise ValueError(
            "AB STRING 最多 {} 字符,实际 {} 字符".format(AB_EIP_STRING_MAX_CHARS, len(raw))
        )
    return struct.pack("<I", len(raw)) + raw + b"\x00" * (type_size(CIP_TYPE_STRUCT) - 4 - len(raw))


def encode_value(data_type: DataType, value: PrimitiveValue) -> bytes:
    """按请求类型把单值编码为写数据(范围校验沿用库约定)。

    :raises ValueError: 类型不支持或值越界
    """
    if data_type is DataType.SHORT:
        return struct.pack("<H", check_int16(value))
    if data_type is DataType.USHORT:
        return struct.pack("<H", check_uint16(value))
    if data_type is DataType.FLOAT:
        return struct.pack("<f", require_float(value))
    if data_type is DataType.DOUBLE:
        return struct.pack("<d", require_float(value))
    number = require_int(value)
    if data_type is DataType.INT:
        return struct.pack("<i", check_range(number, -2147483648, 2147483647, "int"))
    if data_type is DataType.UINT:
        return struct.pack("<I", check_range(number, 0, 4294967295, "uint"))
    if data_type is DataType.LONG:
        return struct.pack(
            "<q", check_range(number, -9223372036854775808, 9223372036854775807, "long")
        )
    if data_type is DataType.ULONG:
        return struct.pack("<Q", check_range(number, 0, 18446744073709551615, "ulong"))
    raise ValueError("AB 不支持的数据类型:{}".format(data_type))


def tag_type_path(parsed: AbTag, zero_last_index: bool = False) -> bytes:
    """便捷封装:由 :class:`AbTag` 构造请求路径(不含位号)。"""
    return build_symbol_path(parsed.members, parsed.indices, zero_last_index)
