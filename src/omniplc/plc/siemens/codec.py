"""S7comm 三层编解码(TPKT / COTP / S7 PDU,纯函数,自研客户端专用)。

依据源:S7comm 无官方公开手册,帧面按「参考实现逐字节比对」铁律退档,
全部事实与出处(文件 + 函数)归档于
`docs/protocol/siemens/s7comm/README.md`(python-snap7 3.2.0 为主源,
Sally7/S7netplus 交叉;snap7 C++ 源码 LGPL 只比对行为不抄码)。

- TPKT(RFC 1006):4 字节头,版本 3 + 总长 u16 大端
- COTP(ISO 8073):CR/CC/DT 三形态
- S7:10/12 字节报头(Job/ACK/ACK_DATA/USERDATA)+ 协商 + 读/写变量 +
  multi + SZL(USERDATA)

真机 pcap 是黄金帧的唯一权威,落地前黄金帧以参考实现字节锁定。
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple

from ...core.debug import format_hex
from ...core.errors import DeviceError, OmniPLCInternalError
from ...core.i18n import _

# ---------------------------------------------------------------- 常量

PROTOCOL_ID: int = 0x32
"""S7 协议 ID(报文头首字节恒 0x32)。"""

PDU_REQUEST: int = 0x01
PDU_ACK: int = 0x02
PDU_ACK_DATA: int = 0x03
PDU_USERDATA: int = 0x07
"""S7 PDU 类型:Job 请求 / ACK 写应答 / ACK_DATA 读·协商应答 / USERDATA。

依据:python-snap7 3.2.0 s7protocol.py `S7PDUType`;ACK/ACK_DATA 应答头
12 字节(尾接 error_class+error_code),USERDATA 头 10 字节(无错误两字,
`parse_response` L1536-1554)。"""

FUNC_READ: int = 0x04
FUNC_WRITE: int = 0x05
FUNC_SETUP_COMM: int = 0xF0
"""S7 功能码:读变量 / 写变量 / 通信协商;multi 读 = 0x04 多项同帧。"""

COTP_CR: int = 0xE0
COTP_CC: int = 0xD0
COTP_DT: int = 0xF0
COTP_PARAM_PDU_SIZE: int = 0xC0
COTP_PARAM_CALLING_TSAP: int = 0xC1
COTP_PARAM_CALLED_TSAP: int = 0xC2
"""COTP PDU 类型与参数码(ISO 8073;connection.py L49-63)。"""

LOCAL_TSAP: int = 0x0100
"""本端 TSAP(python-snap7 默认,client.py L334)。"""

CONNECTION_TYPE_PG: int = 1
"""S7 连接资源类型:PG(编程设备);OP=2、S7 基本=3(python-snap7 同款缺省)。"""

TPDU_SIZE_CODE: int = 0x0A
"""COTP 协商请求的 TPDU 尺寸指数(2^0x0A = 1024;connection.py 缺省)。"""

SRC_REFERENCE: int = 0x0001
"""COTP CR 源引用(python-snap7 缺省 0x0001)。"""

AREA_PE: int = 0x81
AREA_PA: int = 0x82
AREA_MK: int = 0x83
AREA_DB: int = 0x84
AREA_CT: int = 0x1C
AREA_TM: int = 0x1D
"""S7 区域码(datatypes.py `S7Area`)。"""

WORD_LEN_BIT: int = 0x01
WORD_LEN_BYTE: int = 0x02
WORD_LEN_CHAR: int = 0x03
WORD_LEN_WORD: int = 0x04
WORD_LEN_INT: int = 0x05
WORD_LEN_DWORD: int = 0x06
WORD_LEN_DINT: int = 0x07
WORD_LEN_REAL: int = 0x08
WORD_LEN_COUNTER: int = 0x1C
WORD_LEN_TIMER: int = 0x1D
"""地址规范传输尺寸码(datatypes.py `S7WordLen`)。"""

SZL_GROUP: int = 0x04
"""USERDATA 功能组:SZL(group 4);时钟 7、安全 5(s7protocol.py L45-53)。"""

SZL_READ_SUBFUNCTION: int = 0x01
"""USERDATA SZL 读子功能(s7protocol.py L64-66)。"""

SZL_CPU_STATUS_ID: int = 0x0424
"""CPU 状态 SZL ID(snap7 C 家族 `Cli_GetCpuStatus` 读 0x0424/0)。

python-snap7 3.2.0 的 `build_cpu_state_request` 是纯 Python 服务端桩
(恒返 Run),不可作依据;本表按 snap7 C 行为对照,**待真机核证**。"""

RETURN_CODE_OK: int = 0xFF
"""S7 数据段条目返回码:0xFF = 成功(s7protocol.py `S7_RETURN_CODES`)。"""

RETURN_CODE_TEXT: Dict[int, str] = {
    0xFF: "成功",
    0x00: "保留",
    0x01: "硬件故障",
    0x03: "不允许访问该对象",
    0x05: "地址非法",
    0x06: "数据类型不支持",
    0x07: "数据类型不一致",
    0x0A: "对象不存在",
    0x10: "块类型号非法",
    0x11: "存储介质中未找到该块",
    0x20: "参数非法",
    0x21: "PG 资源耗尽(连接数已达上限)",
}
"""S7 数据段返回码 → 文本(常用子集;依据 s7protocol.py `S7_RETURN_CODES`
L78-100,条目文本按中文口径意译)。"""

MAX_PDU_REQUEST: int = 480
"""协商请求的 PDU 长度(python-snap7 缺省 480;实际以对端确认为准)。"""

MAX_VARS: int = 20
"""multi read 单 PDU 项数上限(snap7 MAX_VARS 口径,与旧封装一致)。"""


class S7ProtocolError(OmniPLCInternalError):
    """S7 协议帧错误(报头/参数/数据段不符;由会话层归类翻译)。"""


def return_code_text(code: int) -> str:
    """S7 数据段返回码 → 文本(未知码给十六进制;内部函数)。"""
    if code in RETURN_CODE_TEXT:
        return RETURN_CODE_TEXT[code]
    return "未知错误 0x{:02X}".format(code)


# ---------------------------------------------------------------- TPKT / COTP


def build_tpkt(payload: bytes) -> bytes:
    """TPKT 封帧:版本 3 + 保留 0 + 总长 u16 大端(含头 4 字节)。

    依据:python-snap7 3.2.0 connection.py `_build_tpkt`(L279-291)。
    :raises S7ProtocolError: 总长越出 7~65535
    """
    length = len(payload) + 4
    if not 7 <= length <= 0xFFFF:
        raise S7ProtocolError(_("TPKT 帧长越界:{}(须 7~65535)").format(length))
    return struct.pack(">BBH", 3, 0, length) + payload


def parse_tpkt_header(header: bytes) -> int:
    """解析 TPKT 4 字节头,返回总长(含头);调用方续读 总长-4 字节。

    :raises S7ProtocolError: 版本非 3 或长度不足
    """
    if len(header) < 4:
        raise S7ProtocolError(_("TPKT 头不足 4 字节:实收 {}").format(len(header)))
    version, _reserved, length = struct.unpack(">BBH", header)
    if version != 3:
        raise S7ProtocolError(_("TPKT 版本非法:{}(应为 3)").format(version))
    if length < 7:
        raise S7ProtocolError(_("TPKT 帧长非法:{}(至少 7)").format(length))
    return length


def build_cotp_cr(remote_tsap: int) -> bytes:
    """构造 COTP 连接请求(CR)。

    参数 = Calling TSAP(0xC1,本地 0x0100)+ Called TSAP(0xC2,远端)+
    PDU Size(0xC0,指数)。远端 TSAP = ``(连接类型 << 8) | (rack << 5) | slot``
    由调用方算好传入。

    依据:python-snap7 3.2.0 connection.py `_build_cotp_cr`(L293-339);
    TSAP 编码 `client.py::Client.connect`(L621)。
    """
    body = struct.pack(
        ">BBHHB",
        0,  # PDU 长度占位(下方回填)
        COTP_CR,
        0x0000,  # 目的引用(CR 恒 0)
        SRC_REFERENCE,
        0x00,  # 类别 0
    )
    parameters = struct.pack(">BBH", COTP_PARAM_CALLING_TSAP, 2, LOCAL_TSAP)
    parameters += struct.pack(">BBH", COTP_PARAM_CALLED_TSAP, 2, remote_tsap)
    parameters += struct.pack(">BBB", COTP_PARAM_PDU_SIZE, 1, TPDU_SIZE_CODE)
    total = 6 + len(parameters)
    return struct.pack(">B", total) + body[1:] + parameters


def parse_cotp_cc(payload: bytes) -> None:
    """校验 COTP 连接确认(CC)。

    :raises S7ProtocolError: 类型非 0xD0 或帧过短
    """
    if len(payload) < 7:
        raise S7ProtocolError(_("COTP CC 过短:实收 {} 字节").format(len(payload)))
    pdu_len, pdu_type = struct.unpack(">BB", payload[:2])
    if pdu_type != COTP_CC:
        raise S7ProtocolError(
            _("COTP 应答类型非法:0x{:02X}(应为 CC 0xD0)").format(pdu_type)
        )
    if pdu_len < 6:
        raise S7ProtocolError(_("COTP CC 头长度非法:{}").format(pdu_len))


def build_cotp_dt(data: bytes) -> bytes:
    """COTP 数据帧:头 3 字节 `02 F0 80`(EOT 位 + 序号 0)+ S7 PDU。

    依据:connection.py `_build_cotp_dt`(L399-410)。
    """
    return struct.pack(">BBB", 2, COTP_DT, 0x80) + data


def parse_cotp_dt(cotp_pdu: bytes) -> bytes:
    """解析 COTP 数据帧,剥头返回 S7 PDU。

    :raises S7ProtocolError: 头长/类型/EOT 位不符
    """
    if len(cotp_pdu) < 3:
        raise S7ProtocolError(_("COTP DT 过短:实收 {} 字节").format(len(cotp_pdu)))
    pdu_len, pdu_type, eot_num = struct.unpack(">BBB", cotp_pdu[:3])
    if pdu_len != 2:
        raise S7ProtocolError(_("COTP DT 头长度非法:{}(应为 2)").format(pdu_len))
    if eot_num & 0x7F:
        raise S7ProtocolError(_("COTP DT 序号位非 0:0x{:02X}").format(eot_num))
    if pdu_type != COTP_DT:
        raise S7ProtocolError(
            _("COTP 应答类型非法:0x{:02X}(应为 DT 0xF0)").format(pdu_type)
        )
    return cotp_pdu[3:]


# ---------------------------------------------------------------- S7 报头


def build_s7_request(
    pdu_type: int, sequence: int, parameters: bytes, data: bytes = b""
) -> bytes:
    """构造 S7 请求(Job/USERDATA)报文:10 字节头 + 参数 + 数据。

    头 `>BBHHHH`:协议 ID 0x32、PDU 类型、冗余 0x0000、PDU 引用 u16、
    参数长 u16、数据长 u16。依据:s7protocol.py `build_read_request`
    (L165-188)/`build_setup_communication_request`(L379-398)。
    """
    header = struct.pack(
        ">BBHHHH",
        PROTOCOL_ID,
        pdu_type,
        0x0000,
        sequence & 0xFFFF,
        len(parameters),
        len(data),
    )
    return header + parameters + data


def parse_s7_response(
    pdu: bytes, expected_sequence: int
) -> Tuple[int, int, int, bytes, bytes]:
    """解析 S7 应答报文,返回 ``(功能码, error_class, error_code, 参数, 数据)``。

    ACK/ACK_DATA 头 12 字节(尾接 error_class/error_code),USERDATA 头
    10 字节;PDU 引用须与请求一致。依据:python-snap7 3.2.0
    `parse_response`(L1523-1606)。

    :raises S7ProtocolError: 协议 ID/类型/序列号/分段长度不符
    """
    if len(pdu) < 10:
        raise S7ProtocolError(
            _("S7 应答过短:实收 {} 字节(至少 10)").format(len(pdu))
        )
    pdu_type = pdu[1]
    if pdu_type == PDU_USERDATA:
        header = struct.unpack(">BBHHHH", pdu[:10])
        _protocol_id, _pdu_type, _reserved, sequence, param_len, data_len = header
        error_class = 0
        error_code = 0
        offset = 10
    elif pdu_type in (PDU_ACK, PDU_ACK_DATA):
        if len(pdu) < 12:
            raise S7ProtocolError(
                _("S7 应答过短:实收 {} 字节(ACK 型至少 12)").format(len(pdu))
            )
        header = struct.unpack(">BBHHHHBB", pdu[:12])
        (_protocol_id, _pdu_type, _reserved, sequence,
         param_len, data_len, error_class, error_code) = header
        offset = 12
    else:
        raise S7ProtocolError(
            _("S7 应答类型非法:0x{:02X}(应为 ACK/ACK_DATA/USERDATA)").format(pdu_type)
        )
    if pdu[:1] != bytes((PROTOCOL_ID,)):
        raise S7ProtocolError(
            _("S7 协议 ID 非法:0x{:02X}(应为 0x32)").format(pdu[0])
        )
    if sequence != (expected_sequence & 0xFFFF):
        raise S7ProtocolError(
            _("S7 序列号回显不符:期望 {},收到 {}").format(
                expected_sequence & 0xFFFF, sequence
            )
        )
    if error_class != 0:
        raise DeviceError(
            _("S7 协议错误:class=0x{:02X} code=0x{:02X}").format(error_class, error_code),
            (error_class << 8) | error_code,
        )
    if offset + param_len + data_len > len(pdu):
        raise S7ProtocolError(
            _("S7 应答分段越界:参数 {} + 数据 {} 超出实收 {}").format(
                param_len, data_len, len(pdu) - offset
            )
        )
    parameters = pdu[offset:offset + param_len]
    data = pdu[offset + param_len:offset + param_len + data_len]
    function = parameters[0] if parameters else 0
    return function, error_class, error_code, parameters, data


# ---------------------------------------------------------------- 协商


def build_setup_comm(pdu_length: int, sequence: int) -> bytes:
    """构造通信协商请求(功能 0xF0):参数 8 字节 + 请求数据长 0。

    依据:python-snap7 3.2.0 `build_setup_communication_request`(L373-398)。
    """
    parameters = struct.pack(
        ">BBHHH", FUNC_SETUP_COMM, 0x00, 1, 1, pdu_length
    )
    return build_s7_request(PDU_REQUEST, sequence, parameters)


def parse_setup_comm(pdu: bytes, sequence: int) -> int:
    """解析协商应答,返回对端确认的 PDU 长度。

    :raises S7ProtocolError: 功能码/参数长不符
    """
    function, _eclass, _ecode, parameters, _data = parse_s7_response(pdu, sequence)
    if function != FUNC_SETUP_COMM or len(parameters) < 8:
        raise S7ProtocolError(
            _("S7 协商应答非法:功能码 0x{:02X},参数 {} 字节").format(
                function, len(parameters)
            )
        )
    return struct.unpack(">H", parameters[6:8])[0]


# ---------------------------------------------------------------- 地址规范


def build_address_spec(
    area: int, db_number: int, byte_index: int, word_len: int, count: int
) -> bytes:
    """构造 12 字节 S7-Any 地址规范。

    地址 = BIT 形态(字节地址 ×8 + 位号);字类访问由调用方传入已 ×8 的
    位地址。依据:python-snap7 3.2.0 datatypes.py `encode_address`
    (L55-96):`>BBBBHHB3s` = 0x12 / 0x0A / 0x10(S7-Any)/ 传输尺寸 /
    count u16 / DB 号 u16(DB 区外 0)/ 区域码 / 地址 3 字节大端。

    :param byte_index: BIT 形态位地址(字类 = 字节地址 ×8;TM/CT = 元素号)
    """
    address_bytes = struct.pack(">I", byte_index)[1:]
    return struct.pack(
        ">BBBBHHB3s",
        0x12,
        0x0A,
        0x10,
        word_len,
        count,
        db_number if area == AREA_DB else 0,
        area,
        address_bytes,
    )


# ---------------------------------------------------------------- 读


def build_read(
    area: int,
    db_number: int,
    byte_index: int,
    word_len: int,
    count: int,
    sequence: int,
) -> bytes:
    """构造单变量读请求(功能 0x04,单 Item)。

    依据:python-snap7 3.2.0 `build_read_request`(L151-188)——参数 =
    功能 + 项数 + 地址规范,头数据长 0。
    """
    parameters = struct.pack(">BB", FUNC_READ, 1) + build_address_spec(
        area, db_number, byte_index, word_len, count
    )
    return build_s7_request(PDU_REQUEST, sequence, parameters)


def build_multi_read(
    items: Sequence[Tuple[int, int, int, int]], sequence: int
) -> bytes:
    """构造多变量读请求(功能 0x04,N Item 单 PDU)。

    各项按字节跨度编(WORD_LEN_BYTE,count = 字节数)——与旧封装
    snap7 read_multi_vars 的 WORDLen=BYTE 口径一致。

    :param items: ``(区域码, DB 号, 字节起点, 字节数)`` 列表(≤ :data:`MAX_VARS`)
    依据:python-snap7 3.2.0 `build_multi_read_request`(L190-226)。
    """
    if not 1 <= len(items) <= MAX_VARS:
        raise ValueError(
            _("S7 多变量读条数须在 1~{} 之间,收到:{}").format(MAX_VARS, len(items))
        )
    parts = []
    for area, db_number, start, size in items:
        parts.append(
            build_address_spec(area, db_number, start * 8, WORD_LEN_BYTE, size)
        )
    parameters = struct.pack(">BB", FUNC_READ, len(items)) + b"".join(parts)
    return build_s7_request(PDU_REQUEST, sequence, parameters)


def parse_read_response(
    pdu: bytes, sequence: int, item_count: int, byte_lengths: Optional[List[int]] = None
) -> List[bytes]:
    """解析读应答,返回逐项数据(条目级失败抛 :class:`DeviceError`)。

    数据段逐项 = 返回码 1B + 传输尺寸 1B + 位长 u16 + 数据;传输尺寸
    0x04(BIT)按位长/8 计字节数,其余按字节;奇数长非末项后随 1 字节
    填充。依据:python-snap7 3.2.0 `extract_multi_read_data`(L228-285)。

    :param byte_lengths: 单项读时给 None(按位长/8 推);multi 时给逐项
        期望字节数(与 build_multi_read 的 count 一致)
    :raises S7ProtocolError: 帧结构/截断
    :raises DeviceError: 条目返回码非 0xFF(地址非法等)
    """
    _function, _eclass, _ecode, _parameters, data = parse_s7_response(pdu, sequence)
    results: List[bytes] = []
    offset = 0
    total = len(data)
    for index in range(item_count):
        if offset + 4 > total:
            raise S7ProtocolError(
                _("S7 读应答第 {} 项被截断(收到的原始数据:{})").format(index, format_hex(data))
            )
        return_code = data[offset]
        transport_size = data[offset + 1]
        bit_length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        offset += 4
        if return_code != RETURN_CODE_OK:
            raise DeviceError(
                _("S7 读条目 {} 失败:{}(返回码 0x{:02X})").format(
                    index, return_code_text(return_code), return_code
                ),
                0,
            )
        if transport_size == 0x04:
            byte_length = bit_length // 8
        else:
            byte_length = bit_length
        if byte_lengths is not None and index < len(byte_lengths):
            byte_length = byte_lengths[index]
        if offset + byte_length > total:
            raise S7ProtocolError(
                _("S7 读应答第 {} 项数据被截断(收到的原始数据:{})").format(index, format_hex(data))
            )
        results.append(data[offset:offset + byte_length])
        offset += byte_length
        if index < item_count - 1 and byte_length % 2 != 0:
            offset += 1  # 奇数长非末项的偶对齐填充
    return results


# ---------------------------------------------------------------- 写


_WRITE_TRANSPORT_SIZES: Dict[int, int] = {
    WORD_LEN_BIT: 0x03,
    WORD_LEN_BYTE: 0x04,
    WORD_LEN_WORD: 0x04,
    WORD_LEN_DWORD: 0x04,
    WORD_LEN_INT: 0x05,
    WORD_LEN_DINT: 0x05,
    WORD_LEN_REAL: 0x07,
    WORD_LEN_CHAR: 0x09,
    WORD_LEN_COUNTER: 0x09,
    WORD_LEN_TIMER: 0x09,
}
"""写数据段传输尺寸映射(与地址规范 WordLen 不同码)。

依据:python-snap7 3.2.0 `build_write_request`(L336-355):BIT→0x03、
BYTE/WORD/DWORD→0x04、INT/DINT→0x05、REAL→0x07、CHAR/CT/TM→0x09。"""


def build_write(
    area: int,
    db_number: int,
    byte_index: int,
    word_len: int,
    data: bytes,
    sequence: int,
) -> bytes:
    """构造单变量写请求(功能 0x05,参数 + 数据段)。

    数据段 = `>BBH`(保留 0x00 + 数据传输尺寸 + 数据长)+ 数据;BIT/REAL/
    STRING 类数据长 = 字节数,BYTE/WORD/DWORD/INT/DINT 数据长 = **位数**。
    依据:python-snap7 3.2.0 `build_write_request`(L287-371)。
    """
    transport_size = _WRITE_TRANSPORT_SIZES.get(word_len, 0x04)
    if transport_size in (0x03, 0x07, 0x09):
        data_length = len(data)
    else:
        data_length = len(data) * 8
    parameters = struct.pack(">BB", FUNC_WRITE, 1) + build_address_spec(
        area, db_number, byte_index, word_len, 1
    )
    data_section = struct.pack(">BBH", 0x00, transport_size, data_length) + data
    return build_s7_request(PDU_REQUEST, sequence, parameters, data_section)


def parse_write_response(pdu: bytes, sequence: int, item_count: int) -> None:
    """解析写应答(ACK,数据段 = 逐项返回码)。

    :raises S7ProtocolError: 帧结构/截断
    :raises DeviceError: 条目返回码非 0xFF
    """
    _function, _eclass, _ecode, _parameters, data = parse_s7_response(pdu, sequence)
    if len(data) < item_count:
        raise S7ProtocolError(
            _("S7 写应答条目不足:期望 {},实收 {} 字节").format(item_count, len(data))
        )
    for index in range(item_count):
        return_code = data[index]
        if return_code != RETURN_CODE_OK:
            raise DeviceError(
                _("S7 写条目 {} 失败:{}(返回码 0x{:02X})").format(
                    index, return_code_text(return_code), return_code
                ),
                0,
            )


# ---------------------------------------------------------------- SZL(USERDATA)


def build_read_szl(szl_id: int, szl_index: int, sequence: int) -> bytes:
    """构造 USERDATA SZL 读请求(group 0x04,子功能 0x01)。

    参数 8 字节 `>BBBBBBBB`(0x00/0x01/0x12/0x04/0x11/type|group/子功能/
    DataRef)+ 数据段 `>BBHHH`(0x0A/0x00/4/ID/Index)。
    依据:python-snap7 3.2.0 `build_read_szl_request`(L1075-1120)。
    """
    parameters = struct.pack(
        ">BBBBBBBB",
        0x00,
        0x01,
        0x12,
        0x04,
        0x11,
        0x40 | SZL_GROUP,
        SZL_READ_SUBFUNCTION,
        0x00,
    )
    data_section = struct.pack(">BBHHH", 0x0A, 0x00, 4, szl_id, szl_index)
    return build_s7_request(PDU_USERDATA, sequence, parameters, data_section)


def parse_szl_response(pdu: bytes, sequence: int) -> bytes:
    """解析 SZL 应答,返回 SZL 数据(头 ID+Index 之后的载荷)。

    USERDATA 应答参数区同构;数据段 = 返回码 1B + 传输尺寸 1B + 长 u16 +
    载荷(SZL ID u16 + Index u16 + 载荷)。依据:python-snap7 3.2.0
    `parse_read_szl_response`(L1168-1201)。

    :raises S7ProtocolError: 非.USERDATA/截断
    :raises DeviceError: 数据段返回码非 0xFF
    """
    _function, _eclass, _ecode, parameters, data = parse_s7_response(pdu, sequence)
    if pdu[1] != PDU_USERDATA:
        raise S7ProtocolError(
            _("S7 SZL 应答类型非法:0x{:02X}(应为 USERDATA)").format(pdu[1])
        )
    if len(parameters) < 8 or parameters[4] != 0x12:
        raise S7ProtocolError(_("S7 SZL 应答参数区非法"))
    if len(data) < 4:
        raise S7ProtocolError(
            _("S7 SZL 应答数据段过短:实收 {} 字节").format(len(data))
        )
    return_code = data[0]
    if return_code != RETURN_CODE_OK:
        raise DeviceError(
            _("S7 SZL 读取失败:{}(返回码 0x{:02X})").format(
                return_code_text(return_code), return_code
            ),
            0,
        )
    payload = data[4:]
    if len(payload) < 8:
        raise S7ProtocolError(
            _("S7 SZL 载荷过短:实收 {} 字节(ID+Index+AddLen+AddCount 至少 8)").format(
                len(payload)
            )
        )
    return payload[8:]
