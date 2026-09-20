"""全局常量集中定义。

本模块是 omniplc **唯一的常量来源**:协议默认值、边界值、报文常量
全部在这里定义,业务代码禁止内联魔法数字。

约定:全部使用大写下划线命名(常量),运行期不允许修改;
Python 3.7 无 ``typing.Final``,以命名约定与 Code Review 约束只读性。
"""
from __future__ import annotations

from typing import Dict, Tuple

from ..types import SerialParity

# ---------------------------------------------------------------- 默认超时(秒)
DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_RECEIVE_TIMEOUT: float = 3.0

# ---------------------------------------------------------------- 默认端口
MODBUS_DEFAULT_PORT: int = 502
MC_DEFAULT_PORT: int = 2000
FINS_DEFAULT_PORT: int = 9600

# ---------------------------------------------------------------- 串口默认值
SERIAL_DEFAULT_BAUD_RATE: int = 9600
SERIAL_DEFAULT_DATA_BITS: int = 8
SERIAL_DEFAULT_STOP_BITS: float = 1.0
SERIAL_DEFAULT_PARITY: SerialParity = SerialParity.NONE

# ---------------------------------------------------------------- Modbus
MODBUS_DEFAULT_STATION: int = 1
MODBUS_STATION_MIN: int = 0
MODBUS_STATION_MAX: int = 247
MODBUS_REGISTER_BIT_MAX: int = 15
"""寄存器位访问的位号上限(hr0.15)。"""
MBAP_HEADER_SIZE: int = 7
"""MBAP 帧头长度:事务号(2) + 协议号(2) + 长度(2) + 站号(1)。"""
CRC16_INIT: int = 0xFFFF
CRC16_POLY: int = 0xA001
"""Modbus RTU CRC-16 的初始值与反射多项式。"""
MODBUS_PROTOCOL_ID: int = 0
"""MBAP 协议标识符,恒为 0。"""
MODBUS_EXCEPTION_FLAG: int = 0x80
"""异常响应功能码标志:请求功能码 | 0x80。"""
MODBUS_COIL_ON: int = 0xFF00
"""FC05 写线圈"ON"的线状态值。"""
MODBUS_COIL_OFF: int = 0x0000
"""FC05 写线圈"OFF"的线状态值。"""
MODBUS_MAX_READ_BITS: int = 2000
"""单次读位的数量上限(协议规定)。"""
MODBUS_MAX_READ_REGISTERS: int = 125
"""单次读寄存器的数量上限(协议规定)。"""
MODBUS_MAX_WRITE_BITS: int = 1968
"""单次写线圈的数量上限(协议规定)。"""
MODBUS_MAX_WRITE_REGISTERS: int = 123
"""单次写寄存器的数量上限(协议规定)。"""
MODBUS_MAX_ADU_SIZE: int = 260
"""MBAP 最大帧长 = 帧头 7 + 最大 PDU 253(UDP 整包接收缓冲)。"""
MODBUS_EXCEPTION_TEXT: Dict[int, str] = {
    0x01: "ILLEGAL FUNCTION(不支持的功能码)",
    0x02: "ILLEGAL DATA ADDRESS(地址越界)",
    0x03: "ILLEGAL DATA VALUE(数值非法)",
    0x04: "SERVER DEVICE FAILURE(设备故障)",
    0x05: "ACKNOWLEDGE(已受理,处理中)",
    0x06: "SERVER DEVICE BUSY(设备忙)",
    0x08: "MEMORY PARITY ERROR(存储区奇偶校验错)",
    0x0A: "GATEWAY PATH UNAVAILABLE(网关路径不可用)",
    0x0B: "GATEWAY TARGET DEVICE FAILED TO RESPOND(网关目标设备无响应)",
}
"""Modbus 标准异常码 → 可读描述(last_error 用)。"""

# ---------------------------------------------------------------- 三菱 MC
MC_DEFAULT_NETWORK_NUMBER: int = 0
"""默认网络编号(本网络)。"""
MC_DEFAULT_PC_NUMBER: int = 0xFF
"""默认 PC 编号;0xFF = 本局 CPU 直连约定值(与 HslCommunication 一致)。"""
MC_DEFAULT_MONITOR_TIMER: int = 10
"""CPU 监视定时器默认值,单位 250ms(10 = 等待 PLC 约 2.5 秒)。"""
MC_DEST_MODULE_IO: int = 0x03FF
"""目标模块 I/O 编号(CPU 直连,报文中小端两字节 FF 03)。"""
MC_DEST_MODULE_STATION: int = 0
"""目标模块局号(CPU 直连恒为 0)。"""
MC_MAX_TRANSFER_POINTS: int = 900
"""3E/4E 单事务读/写点数上限(与 HslCommunication 分块大小一致)。"""
MC_REQUEST_HEAD_SIZE: int = 11
"""3E/4E 请求头长度:副头部2+网络1+PC1+IO2+局1+请求数据长2+监视定时器2。"""
MC_RESPONSE_HEAD_SIZE: int = 9
"""3E/4E 响应头长度:副头部2+网络1+PC1+IO2+局1+应答数据长2。"""
MC_RESPONSE_SUBHEADER_3E: int = 0xD0
"""3E 响应(读/写)与 4E 写响应的副头部首字节。"""
MC_RESPONSE_SUBHEADER_4E: int = 0xD4
"""4E 读响应的副头部首字节。"""
MC_COMMAND_BATCH_READ: int = 0x0104
"""成批读核心命令(报文中大端两字节 01 04)。"""
MC_COMMAND_BATCH_WRITE: int = 0x0114
"""成批写核心命令(报文中大端两字节 01 14)。"""
MC_SUBCOMMAND_WORD_UNITS: int = 0x0000
"""子命令:以字为单位(小端两字节 00 00)。"""
MC_SUBCOMMAND_BIT_UNITS: int = 0x0001
"""子命令:以位为单位(小端两字节 01 00)。"""
MC_MAX_DATAGRAM: int = 2048
"""UDP 整包接收缓冲上限(足以容纳最大点数响应)。"""
MC_SUBHEADER_3E: bytes = b"\x50\x00"
"""3E 请求副头部(读/写相同,由命令字段区分;响应为 D0 00)。"""
MC_SUBHEADER_4E: bytes = b"\x54\x00"
"""4E 请求副头部(读/写相同,由命令字段区分;响应为 D4 00)。"""
MC_4E_RESPONSE_HEAD_SIZE: int = 13
"""4E 响应头长度:副头部2+序列号2+保留2+网络1+PC1+IO2+局1+应答数据长2。"""
MC_1E_RESPONSE_HEAD_SIZE: int = 2
"""1E 响应头:副头部(1) + 结束代码(1)。"""
MC_1E_MAX_POINTS: int = 255
"""1E 单事务点数上限(点数域高字节恒 0)。"""
MC_1E_ERROR_EXTRA: int = 0x5B
"""1E 该结束码的响应附带 2 字节扩展信息(TCP 需多读,防止字节流错位)。"""
MC_1E_ERROR_EXTRA_SIZE: int = 2
MC_1E_READ_BIT: int = 0x00
"""1E 副头部:位单位成批读。"""
MC_1E_READ_WORD: int = 0x01
"""1E 副头部:字单位成批读。"""
MC_1E_WRITE_BIT: int = 0x02
"""1E 副头部:位单位成批写。"""
MC_1E_WRITE_WORD: int = 0x03
"""1E 副头部:字单位成批写。"""
MC_DEVICE_CODES: Dict[str, Tuple[int, int, int]] = {
    "X": (0x9C, 1, 16),
    "Y": (0x9D, 1, 16),
    "M": (0x90, 1, 10),
    "S": (0x98, 1, 10),
    "B": (0xA0, 1, 16),
    "D": (0xA8, 0, 10),
    "W": (0xB4, 0, 16),
    "R": (0xAF, 0, 10),
    "Z": (0xCC, 0, 10),
    "ZR": (0xB0, 0, 10),
}
"""3E/4E 软元件码表:软元件 → (二进制码, 位软元件?, 地址进制)。

来源:HslCommunication ``MelsecMcDataType`` 与 SLMP 库 ``constants.DEVICE_CODES``
(SH-080956 手册二进制码列)一致:X/Y/W/B 十六进制,ZR 十进制;
仅 iQ-F(FX5U)的 X/Y 为八进制,v1 按 Q/L/R 口径处理。
"""
MC_1E_DEVICE_CODES: Dict[str, Tuple[int, int, int]] = {
    "X": (0x5820, 1, 8),
    "Y": (0x5920, 1, 8),
    "M": (0x4D20, 1, 10),
    "S": (0x5320, 1, 10),
    "D": (0x4420, 0, 10),
    "R": (0x5220, 0, 10),
}
"""1E 软元件码表:软元件 → (两字节码, 位软元件?, 地址进制)。来源 ``MelsecA1EDataType``。"""

# ---------------------------------------------------------------- 欧姆龙 FINS
FINS_DEFAULT_DESTINATION_NETWORK: int = 0
"""默认目标网络号(0 = 本网络)。"""
FINS_DEFAULT_DESTINATION_NODE: int = 0
"""默认目标节点号(0 = 握手自动获取;手工配置常用 PLC IP 地址末位)。"""
FINS_DEFAULT_DESTINATION_UNIT: int = 0
"""默认目标单元号(0 = CPU 单元)。"""
FINS_ICF: int = 0x80
"""请求 ICF:要求响应 + 非网关(响应帧为 0xC0)。"""
FINS_RSV: int = 0x00
"""RSV 恒为 0。"""
FINS_GCT: int = 0x02
"""网关允许穿越数(固定值)。"""
FINS_HEADER_SIZE: int = 10
"""FINS 帧头长度:ICF..SID。"""
FINS_COMMAND_AREA_READ: int = 0x0101
"""Area Read 命令(MRC=01, SRC=01)。"""
FINS_COMMAND_AREA_WRITE: int = 0x0102
"""Area Write 命令(MRC=01, SRC=02)。"""
FINS_END_CODE_SIZE: int = 2
"""结束码长度(大端两字节,紧跟 MRC/SRC)。"""
FINS_END_CODE_OK: int = 0
"""结束码:正常完成。"""
FINS_END_CODE_TEXT: Dict[int, str] = {0: "正常完成"}
"""常见结束码文本;未收录的提示查阅 Omron FINS 手册。"""
FINS_MAX_DATAGRAM: int = 2048
"""UDP 整包接收缓冲上限(足以容纳最大区域读响应)。"""
FINS_TCP_MAGIC: bytes = b"FINS"
"""FINS/TCP 帧头魔数。"""
FINS_TCP_HEADER_SIZE: int = 8
"""FINS/TCP 帧头长度:"FINS"(4) + 长度(4,大端,= 后续字节数)。"""
FINS_TCP_COMMAND_HANDSHAKE: int = 0
"""FINS/TCP 命令:节点分配握手。"""
FINS_TCP_COMMAND_DATA: int = 2
"""FINS/TCP 命令:发送 FINS 帧。"""
FINS_HANDSHAKE_LENGTH: int = 12
"""握手长度域(命令 4 + 错误 4 + 节点 4)。"""
FINS_HANDSHAKE_SIZE: int = 20
"""握手请求总长 = 帧头 8 + 长度域 12;本地节点号在帧内末字节。"""
FINS_HANDSHAKE_RESPONSE_SIZE: int = 24
"""握手响应总长 = 帧头 8 + 长度域 16;本地节点在 [19],PLC 节点在 [23]。"""
FINS_EM_BANK_MAX: int = 15
"""EM 区 bank 号上限(E0~EF)。"""
FINS_EM_WORD_CODE_BASE: int = 0xE0
"""EM 区字操作码基址(bank 0 → 0xE0)。"""
FINS_EM_BIT_CODE_BASE: int = 0x20
"""EM 区位操作码基址(bank 0 → 0x20)。"""
FINS_MEMORY_CODES: Dict[str, Tuple[int, int]] = {
    "CIO": (0x30, 0xB0),
    "W": (0x31, 0xB1),
    "H": (0x32, 0xB2),
    "A": (0x33, 0xB3),
    "D": (0x02, 0x82),
}
"""FINS 存储区码:区名 → (位操作码, 字操作码)。来源 ``OmronFinsDataType``;EM 区按 bank 换算。"""
FINS_BIT_WRITABLE_AREAS: Tuple[str, ...] = ("CIO", "W", "H", "A")
"""支持位区域写(0102 位单位)的存储区;D/EM 区按位写走读-改-写。"""

# ---------------------------------------------------------------- 三菱 MX Component
MX_PROG_ID: str = "ActUtlType.ActUtlType"
"""ActUtlType 控件的 COM ProgID(实用程序设置型,按逻辑站号通信)。"""
MX_DEFAULT_LOGICAL_STATION: int = 0
"""默认逻辑站号(与手册默认值一致;须与通信设置实用程序中的配置一致)。"""
MX_LOGICAL_STATION_MAX: int = 1023
"""逻辑站号上限(手册:可设置范围 0~1023)。"""
MX_MAX_BLOCK_WORDS: int = 960
"""单次批量读/写的字数上限(保守值,防止超大块拖死 COM 调用)。"""
MX_BIT_DEVICES: Tuple[str, ...] = (
    "X", "Y", "M", "L", "S", "F", "V", "B", "SB", "DX", "DY",
    "TS", "TC", "ST", "STS", "STC", "CS", "CC", "SM",
)
"""Q/R 系列常见位软元件表(用于区分位/字访问);表外软元件按字软元件处理。"""

# ---------------------------------------------------------------- 基恩士 KV Host Link
KV_DEFAULT_PORT: int = 8000
"""KV Host Link TCP/UDP 默认端口(KEYENCE 惯例值,可在 PLC 侧修改)。"""
KV_MAX_LINE: int = 4096
"""ASCII 响应行长度上限(驱动单次最多读 8 个字,远小于该上限)。"""
KV_MAX_DATAGRAM: int = 2048
"""UDP 整包接收缓冲上限。"""
KV_ERROR_TEXT: Dict[str, str] = {
    "E0": "软元件编号异常",
    "E1": "命令异常",
    "E2": "程序未登记",
    "E4": "禁止写入",
    "E5": "单元异常",
    "E6": "无注释",
}
"""KV Host Link 出错代码文本;未收录的提示查阅 KEYENCE 手册。"""
KV_BIT_DEVICES: Tuple[str, ...] = ("R", "B", "MR", "LR", "CR", "VB", "X", "Y", "M", "L")
"""位软元件(R/MR/CR 为位组十进制,B/VB 十六进制,X/Y 组十进制+位 1 位十六进制,M/L 十进制)。"""
KV_WORD_DEVICES: Tuple[str, ...] = ("DM", "EM", "FM", "ZF", "W", "TM", "Z", "CM", "VM", "D", "E", "F")
"""字软元件(W 十六进制编号,其余十进制)。"""
KV_HEX_NUMBER_DEVICES: Tuple[str, ...] = ("B", "VB", "W")
"""编号为十六进制的软元件。"""
KV_BIT_BANK_DEVICES: Tuple[str, ...] = ("R", "MR", "CR")
"""位组编号软元件:十进制 ``组号+位号两位``,低两位 00~15(如 R515 = 组 5 位 15)。"""

# ---------------------------------------------------------------- 基恩士 SR 扫码枪
SR_DEFAULT_PORT: int = 9004
"""SR 系列 Ethernet 用户模式默认 TCP 端口。"""
SR_DEFAULT_SCAN_DWELL: float = 1.0
"""默认扫码窗口时长(秒):LON 开窗到 LOFF 关窗的等待时间。"""
SR_BANK_MAX: int = 15
"""预设 bank 号上限(LON,{bank:02d},0~15)。"""
SR_RECV_MAX: int = 1024
"""单次响应读取字节上限。"""
SR_CMD_LON: bytes = b"LON\r"
"""打开扫码窗口(默认 bank)。"""
SR_CMD_LOFF: bytes = b"LOFF\r"
"""关闭扫码窗口(触发应答发送)。"""
SR_CMD_RESET: bytes = b"RESET\r"
"""扫码枪复位。"""
SR_CMD_BUFFER_CLEAR: bytes = b"BCLR\r"
"""清除接收缓冲。"""
SR_RESP_OK: str = "OK"
"""命令成功应答。"""
SR_RESP_ERROR: str = "ERROR"
"""读码失败应答(未读到条码或距离过远)。"""

# ---------------------------------------------------------------- 丰田 TOYOPUC
TOYOPUC_DEFAULT_PORT: int = 1025
"""TOYOPUC 计算机链接以太网默认端口(参考库示例值,可在 PLC 侧修改)。"""
TOYOPUC_FRAME_HEADER_SIZE: int = 4
"""帧头长度:FT(1) + RC(1) + 帧长(2,小端);帧长 = CMD(1) + 数据字节数。"""
TOYOPUC_MAX_DATAGRAM: int = 2048
"""UDP 整包接收缓冲上限(足以容纳最大点数响应)。"""
TOYOPUC_FT_COMMAND: int = 0x00
"""命令帧 FT 值(RC 恒为 0)。"""
TOYOPUC_FT_RESPONSE: int = 0x80
"""响应帧 FT 值。"""
TOYOPUC_RC_OK: int = 0x00
"""响应 RC:正常结束。"""
TOYOPUC_RC_ERROR: int = 0x10
"""响应 RC:命令出错;详细出错代码在 CMD 字节(无数据时)或数据末字节。"""
TOYOPUC_CMD_WORD_READ: int = 0x1C
"""连续字读命令(CMD=1C)。"""
TOYOPUC_CMD_WORD_WRITE: int = 0x1D
"""连续字写命令(CMD=1D)。"""
TOYOPUC_CMD_BYTE_READ: int = 0x1E
"""连续字节读命令(CMD=1E)。"""
TOYOPUC_CMD_BYTE_WRITE: int = 0x1F
"""连续字节写命令(CMD=1F)。"""
TOYOPUC_CMD_BIT_READ: int = 0x20
"""单位读命令(CMD=20)。"""
TOYOPUC_CMD_BIT_WRITE: int = 0x21
"""单位写命令(CMD=21)。"""
TOYOPUC_MAX_WORD_COUNT: int = 0x0200
"""单事务连续字读/写点数上限。"""
TOYOPUC_MAX_BYTE_COUNT: int = 0x0400
"""单事务连续字节读/写点数上限。"""
TOYOPUC_BIT_DEVICES: Tuple[str, ...] = ("P", "K", "V", "T", "C", "L", "X", "Y", "M")
"""位软元件(编号十六进制;L 为第二段起始 0x1000 的软元件)。"""
TOYOPUC_WORD_DEVICES: Tuple[str, ...] = ("S", "N", "R", "D", "B")
"""字软元件(编号十六进制)。"""
TOYOPUC_ERROR_TEXT: Dict[int, str] = {
    0x11: "CPU 模块硬件故障",
    0x20: "中继命令 ENQ 固定数据非 0x05",
    0x21: "中继命令传送号非法",
    0x23: "命令码非法",
    0x24: "子命令码非法",
    0x25: "命令格式数据字节非法",
    0x26: "功能调用操作数个数非法",
    0x31: "顺序运行中禁止写入",
    0x32: "停止保持中命令不可执行",
    0x33: "非调试模式调用调试功能",
    0x34: "设定禁止访问",
    0x35: "执行优先限制设定禁止执行",
    0x36: "其他设备执行优先限制禁止执行",
    0x39: "写 I/O 参数后需复位才能开始扫描",
    0x3C: "致命故障中命令不可执行",
    0x3D: "处理竞争禁止执行",
    0x3E: "存在复位禁止执行",
    0x3F: "停止期间命令不可执行",
    0x40: "地址或地址+点数越界",
    0x41: "字/字节点数越界",
    0x42: "发送了未指定的数据",
    0x43: "功能调用操作数非法",
    0x52: "定时器/计数器设定·当前值访问命令不符",
    0x66: "中继链接模块无应答",
    0x70: "中继链接模块不可执行",
    0x72: "中继链接模块无应答",
    0x73: "同一链接模块中继命令冲突,需重试",
}
"""TOYOPUC 详细出错代码 → 可读描述(last_error 用);未收录的提示查阅手册。"""

# ---------------------------------------------------------------- OPC-UA
OPCUA_DEFAULT_PORT: int = 4840
"""OPC-UA 标准默认端口(opc.tcp 端点未显式带端口时提示用)。"""
OPCUA_ENDPOINT_SCHEME: str = "opc.tcp://"
"""OPC-UA TCP 传输的端点 URL 前缀(OPC 10000-6 规范)。"""

# ---------------------------------------------------------------- 通用
BIT_INDEX_MAX: int = 63
"""位操作工具允许的最大位号。"""
READ_STRING_DEFAULT_LENGTH: int = 32
DEFAULT_STRING_ENCODING: str = "ascii"
