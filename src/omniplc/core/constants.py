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
MODBUS_COMMAND_MASK_WRITE: int = 0x16
"""掩码写保持寄存器命令(FC22,设备侧原子 AND/OR 位修改)。"""
MODBUS_MASK_WRITE_PDU_SIZE: int = 7
"""FC22 请求/响应 PDU 长度:功能码(1) + 地址(2) + AND 掩码(2) + OR 掩码(2)。"""
MODBUS_MAX_ADU_SIZE: int = 260
"""MBAP 最大帧长 = 帧头 7 + 最大 PDU 253(UDP 整包接收缓冲)。"""
MODBUS_MBAP_LENGTH_MAX: int = MODBUS_MAX_ADU_SIZE - MBAP_HEADER_SIZE + 1
"""MBAP 长度域合法上限(= 站号 1 + PDU 253);超出按坏帧处理,防止按长收包挂死。"""
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

# ---------------------------------------------------------------- 三菱 MC 串口帧(3C/4C)
MC_SERIAL_FRAME_ID_3C: int = 0xF9
"""3C 帧(QnA 兼容 ASCII)帧识别码,报文中两字符 ``"F9"``(SH-080008 4.3 节)。"""
MC_SERIAL_FRAME_ID_4C: int = 0xF8
"""4C 帧(QnA 扩展二进制)帧识别码,报文中单字节 F8H(同上;格式 5 专属帧型)。"""
MC_SERIAL_STATION_MAX: int = 0x1F
"""站号上限:0~31(00H~1FH,多点连接访问目标站;连接站固定 0)。"""
MC_SERIAL_DEFAULT_STATION: int = 0
"""默认站号(0 = 连接站/主机站)。"""
MC_SERIAL_DEFAULT_NETWORK_NUMBER: int = 0
"""默认网络编号(0 = 本网络)。"""
MC_SERIAL_DEFAULT_PC_NUMBER: int = 0xFF
"""默认 PC 编号(FFH = 连接站 CPU;串口帧可选 00~03/FF)。"""
MC_SERIAL_DEFAULT_SELF_STATION: int = 0
"""默认本站号(m:n 多点连接时外部设备自身站号;直连为 0)。"""
MC_SERIAL_DEFAULT_MODULE_IO: int = 0x03FF
"""4C 帧请求目标模块 I/O 编号默认值(CPU 直连 03FF,报文小端 FF 03)。"""
MC_SERIAL_DEFAULT_MODULE_STATION: int = 0
"""4C 帧请求目标模块局号默认值(CPU 直连恒为 0)。"""

# ---------------------------------------------------------------- 基恩士 MC 协议兼容
KEYENCE_MC_DEFAULT_PORT: int = 5000
"""基恩士 KV 系列 MC 协议兼容(SLMP)默认 TCP 端口(以太网单元设置默认值)。"""
KEYENCE_MC_DEVICE_CODES: Dict[str, Tuple[int, int, int]] = {
    "R": (0x90, 1, 10),
    "B": (0xA0, 1, 16),
    "W": (0xB4, 0, 16),
    "DM": (0xA8, 0, 10),
    "ZR": (0xB0, 0, 10),
}
"""基恩士 KV SLMP 兼容(MC 协议 3E 帧)软元件码表:软元件 → (二进制码, 位软元件?, 地址进制)。

KV-7500/8000/X 系列以太网口的 SLMP 兼容模式帧格式与三菱 3E 完全一致,
仅软元件记号/码不同:R(继电器,位)用三菱 M 的 90h、DM(数据存储,字)用 D 的
A8h、ZR(文件寄存器,字)同 B0h;B/W 与三菱同码同进制。
T/C 类软元件与三菱表一并不在本库 MC 范围内。
"""

# ---------------------------------------------------------------- 汇川 H3U/H5U
INOVANCE_BIT_DEVICES: Dict[str, Tuple[int, int]] = {
    "M": (0x0000, 8512),
    "SM": (0x2400, 1024),
    "S": (0xE000, 4096),
    "T": (0xF000, 512),
    "C": (0xF400, 256),
    "X": (0xF800, 256),
    "Y": (0xFC00, 256),
    "B": (0x3000, 32768),
}
"""汇川线圈区软元件表:软元件 → (Modbus 基址, 编号上限,不含)。

来源:H3U 手册 9.4.3 Modbus 通信地址(19010394)与 H5U&Easy 手册
9.5.1"被 ModBus 访问的线圈地址"(19011157)。M 编号即偏移:
H5U 为 M0~M7999,H3U 的 M8000~M8511 从 0x1F40(8000)连续;
X/Y 编号为八进制,见 :data:`INOVANCE_OCTAL_DEVICES`。
"""
INOVANCE_WORD_DEVICES: Dict[str, Tuple[int, int]] = {
    "D": (0x0000, 8512),
    "SD": (0x2400, 1024),
    "R": (0x3000, 32768),
    "T": (0xF000, 256),
    "C": (0xF400, 200),
}
"""汇川保持寄存器区软元件表:软元件 → (Modbus 基址, 编号上限,不含)。

C 仅 C0~C199(16 位当前值);C200~C255 为 32 位计数器,Modbus 侧按
双寄存器展开(0xF700 起),本库 v1 不提供其字访问。
"""
INOVANCE_OCTAL_DEVICES: Tuple[str, ...] = ("X", "Y")
"""编号为八进制的软元件(X0~X377、Y0~Y377)。"""

# ---------------------------------------------------------------- 汇川 MC 协议兼容
INOVANCE_MC_DEFAULT_PORT: int = 2000
"""汇川 MC 服务器端口默认值(占位)。

手册未规定出厂默认端口,由 AutoShop"MC配置"设置(范围 1025~4999、
5010~49151,且不可用 502/9600/44818/2222/34980/12939/12940);
此默认沿用三菱 MC 以太网惯例端口,实际以现场配置为准。
"""
INOVANCE_MC_DEVICE_CODES: Dict[str, Tuple[int, int, int]] = {
    "X": (0x9C, 1, 16),
    "Y": (0x9D, 1, 16),
    "M": (0x90, 1, 10),
    "S": (0x92, 1, 10),
    "B": (0xA0, 1, 16),
    "D": (0xA8, 0, 10),
    "W": (0xB4, 0, 16),
}
"""汇川 MC 兼容(3E 帧)软元件码表:软元件 → (三菱二进制码, 位软元件?, 帧内进制)。

来源:H5U&Easy 手册第 16 章"MC通信"16.4 支持规格(19011157,Easy 系列
固件 V6.4.0.0+;16.5 故障码表含 3E/4E 副帧头校验)。请求帧完全按三菱
Q/L 系列口径编码,映射关系:S 按三菱 L(92h)访问;R 与 D 统一编址
(R n ≡ D(8000+n),见 :data:`INOVANCE_MC_R_BASE`,码表不单列,由客户端
层换算);X/Y 用户命名沿用汇川八进制(X0~X1777),客户端层换算为三菱
3E 帧的十六进制编号。
"""
INOVANCE_MC_R_BASE: int = 8000
"""汇川 R 与 D 统一编址偏移:经 MC 协议访问 R n 即访问 D(8000+n),如 R0 = D8000。"""

# ---------------------------------------------------------------- 松下 FP 系列 MC 协议兼容
PANASONIC_MC_DEFAULT_PORT: int = 2000
"""松下 MC 协议兼容端口默认值(占位)。

FP0H/FP7 以太网口的 MC 协议(QnA 兼容 3E 帧,仅二进制、成批读/写)
端口在模块配置中设置,手册未规定出厂默认;此默认沿用三菱 MC 惯例
端口(HSL ``PanasonicMcNet`` 亦继承 2000),实际以现场配置为准。
"""
PANASONIC_MC_DEVICE_CODES: Dict[str, Tuple[int, int, int]] = {
    "X": (0x9C, 1, 10),
    "Y": (0x9D, 1, 10),
    "L": (0xA0, 1, 10),
    "R": (0x90, 1, 10),
    "SM": (0x91, 1, 10),
    "TS": (0xC1, 1, 10),
    "CS": (0xC4, 1, 10),
    "D": (0xA8, 0, 10),
    "LD": (0xB4, 0, 10),
    "SD": (0xA9, 0, 10),
    "TN": (0xC2, 0, 10),
    "CN": (0xC5, 0, 10),
}
"""松下 MC 兼容(3E 帧)软元件码表:软元件 → (二进制码, 位软元件?, 帧内进制)。

来源:HSL ``PanasonicMcNet``/``MelsecMcDataType.Panasonic_*``(与三菱
Q/L 系列同码):X/Y/L/R 为"字号 + 位号"组织的位软元件(D/T/C 类似三菱
TS/TC 记号),帧内编号 = 字号×16+位号,由客户端层换算;D/LD/TN/CN 为
字软元件(纯十进制)。R 字号 ≥900 映射系统继电器 SM(见
:data:`PANASONIC_MC_SM_LINEAR_BASE`),D 编号 ≥90000 映射系统寄存器 SD
(偏移 -90000)。
"""
PANASONIC_MC_SM_LINEAR_BASE: int = 14400
"""松下 SM 线性编号基点:位软元件 X/Y/L/R 的线性编号(字号×16+位号)≥ 14400
(即字号 ≥900,记法 R9000 起)时映射到 SM,SM 编号 = 线性编号 - 14400。"""
PANASONIC_MC_SD_BASE: int = 90000
"""松下 SD 系统寄存器分界:D 编号 ≥ 90000 时映射 SD,SD 编号 = D 编号 - 90000。"""

# ---------------------------------------------------------------- 松下 MEWTOCOL
MEWTOCOL_DEFAULT_PORT: int = 1024
"""MEWTOCOL 以太网默认端口(TCP/UDP,PLC 为服务器)。

来源:Pro-face《MEWTOCOL-COM Ethernet Driver》目标端口号 1024;
MewtocolNet 等实现可配,实际以 PLC 以太网模块设置为准。
"""
MEWTOCOL_DEFAULT_STATION: int = 1
"""MEWTOCOL 默认站号(01~99;编程口直连场景用 :data:`MEWTOCOL_STATION_DIRECT`)。"""
MEWTOCOL_STATION_DIRECT: int = 0xEE
"""MEWTOCOL 直连站号(EE):经编程口/无需站号寻址的场景使用(HSL 与
MewtocolNet 默认)。"""
MEWTOCOL_CONTACT_AREAS: Tuple[str, ...] = ("X", "Y", "R", "T", "C", "L")
"""MEWTOCOL 接点(位)区代码:X/Y 外部输入输出、R 内部继电器、
T/C 定时器计数器接点、L 链接继电器。"""
MEWTOCOL_MAX_DATAGRAM: int = 2048
"""UDP 整包接收缓冲上限。"""

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
FINS_END_CODE_TEXT: Dict[int, str] = {
    0x0000: "正常完成",
    0x0001: "服务被中断",
    0x0101: "本地节点不在网络中",
    0x0102: "令牌超时或节点号过大",
    0x0103: "发送重试次数超限",
    0x0104: "超过最大帧数",
    0x0105: "节点号设置错误(范围)",
    0x0106: "节点号重复",
    0x0201: "目标节点不在网络中",
    0x0202: "无指定节点号的节点",
    0x0203: "第三节点不在网络中(指定了广播)",
    0x0204: "目标节点忙",
    0x0205: "响应超时",
    0x0301: "发生错误(ERC 指示灯亮)",
    0x0302: "目标节点 CPU 出错",
    0x0303: "控制器错误导致无法正常响应",
    0x0304: "节点号设置错误",
    0x0401: "使用了未定义命令",
    0x0402: "单元型号/版本不支持该命令",
    0x0501: "目标节点号未登记到路由表",
    0x0502: "未登记路由表",
    0x0503: "路由表错误",
    0x0504: "超过最大中继节点数(2)",
    0x1001: "命令超过最大允许长度",
    0x1002: "命令小于最小允许长度",
    0x1003: "指定数据项数与实际不符",
    0x1004: "命令格式错误",
    0x1005: "命令头错误",
    0x1101: "存储区码非法或 DM 不可用",
    0x1102: "访问尺寸错误",
    0x1103: "起始地址在不可访问区",
    0x1104: "指定字范围越界",
    0x1106: "程序号不存在",
    0x1109: "命令块内数据项尺寸错误",
    0x110A: "无法执行 IOM 中断",
    0x110B: "响应块超过最大长度",
    0x110C: "参数码错误",
    0x2002: "数据被保护",
    0x2003: "登记表不存在",
    0x2004: "搜索数据不存在",
    0x2005: "程序号不存在",
    0x2006: "文件不存在",
    0x2007: "校验错误",
    0x2101: "指定区域只读",
    0x2102: "数据被保护",
    0x2103: "打开文件数过多",
    0x2105: "程序号不存在",
    0x2106: "文件不存在",
    0x2107: "文件已存在",
    0x2108: "数据不可更改",
    0x2201: "模式错误(运行中)",
    0x2202: "模式错误(停止)",
    0x2203: "PLC 处于 PROGRAM 模式",
    0x2204: "PLC 处于 DEBUG 模式",
    0x2205: "PLC 处于 MONITOR 模式",
    0x2206: "PLC 处于 RUN 模式",
    0x2207: "指定节点不是控制节点",
    0x2208: "模式错误,步骤无法执行",
    0x2301: "指定文件设备不存在",
    0x2302: "指定存储器不存在",
    0x2303: "无时钟",
    0x2401: "数据链接表不正确",
    0x2502: "奇偶/校验和错误",
    0x2503: "I/O 设置错误",
    0x2504: "I/O 点数过多",
    0x2505: "CPU 总线错误",
    0x2506: "I/O 重复错误",
    0x2507: "I/O 总线错误",
    0x2509: "SYSMAC BUS/2 错误",
    0x250A: "特殊 I/O 单元错误",
    0x250D: "SYSMAC BUS 字分配重复",
    0x250F: "发生存储器错误",
    0x2510: "SYSMAC BUS 系统终端未连接",
    0x2601: "指定区域未保护",
    0x2602: "密码错误",
    0x2604: "指定区域被保护",
    0x2605: "服务正在执行",
    0x2606: "服务未在执行",
    0x2607: "服务无法从本地节点执行",
    0x2608: "设置不正确,服务无法执行",
    0x2609: "命令数据设置不正确,服务无法执行",
    0x260A: "指定动作已登记",
    0x260B: "无法清除错误,错误仍存在",
    0x3001: "访问权被其他设备持有",
    0x4001: "命令被 ABORT 命令中止",
}
"""FINS 结束码 → 可读描述(全表,与手册 W340 5-4-2 及 fins-driver
参考实现一致;2026-09 对照补全);未收录的提示查阅手册。"""
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
FINS_HANDSHAKE_RESPONSE_SIZE: int = 24
"""握手响应总长 = 帧头 8 + 长度域 16;本地节点在 [19],PLC 节点在 [23]。"""
FINS_EM_BANK_MAX: int = 15
"""EM 区 bank 号上限(E0~EF)。"""
FINS_EM_WORD_CODE_BASE: int = 0xA0
"""EM 区字操作码基址(bank 0 → 0xA0;手册 W340 5-2-2:字 A0~AF、位 20~2F。
2026-09 对照 fins-driver 修正:原 0xE0 为 bank≥16 扩展区的位码,系笔误)。"""
FINS_EM_BIT_CODE_BASE: int = 0x20
"""EM 区位操作码基址(bank 0 → 0x20)。"""
FINS_MEMORY_CODES: Dict[str, Tuple[int, int]] = {
    "CIO": (0x30, 0xB0),
    "W": (0x31, 0xB1),
    "H": (0x32, 0xB2),
    "A": (0x33, 0xB3),
    "D": (0x02, 0x82),
    "T": (0x09, 0x89),
    "C": (0x09, 0x89),
}
"""FINS 存储区码:区名 → (位操作码, 字操作码)。来源 ``OmronFinsDataType``
与手册 W340 5-2-2;EM 区按 bank 换算(位 0x20/字 0xA0 + bank,bank 0~15)。
T/C(定时器/计数器)共享码表(按地址区分):位 = 完成标志(09,只读),
字 = 当前值 PV(89,可读写)。"""
FINS_TIMER_COUNTER_AREAS: Tuple[str, ...] = ("T", "C")
"""定时器/计数器区记号:位访问为完成标志(只读,地址不带位号),
字访问为当前值 PV(可读写)。"""
FINS_BIT_WRITABLE_AREAS: Tuple[str, ...] = ("CIO", "W", "H", "A")
"""支持位区域写(0102 位单位)的存储区;D/EM 区按位写走读-改-写,
T/C 完成标志只读。"""

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

# ---------------------------------------------------------------- AB(罗克韦尔)
AB_EIP_DEFAULT_PORT: int = 44818
"""EtherNet/IP TCP 默认端口(Logix CPU 内置以太网口)。"""
AB_EIP_DEFAULT_SLOT: int = 0
"""默认 CPU 槽号(Unconnected Send 背板路由;0 = CPU 与以太网口同模块)。"""
AB_EIP_SLOT_MAX: int = 31
"""槽号上限(CIP 端口段 link 为 1 字节)。"""
AB_EIP_ORIGINATOR_VENDOR_ID: int = 0x1337
"""Forward Open 的发起方厂商号(目标侧不校验,任意非冲突值即可;沿用参考库惯例)。"""
AB_EIP_STRING_STRUCT_ID: int = 0x0FCE
"""Logix 标准 STRING 结构体模板实例号(写入类型域携带)。"""
AB_EIP_STRING_MAX_CHARS: int = 82
"""Logix STRING 结构体最大字符数(len u32 + 82 字符 + 2 对齐 = 88 字节)。"""
AB_EIP_STATUS_TEXT: dict = {
    0x01: "非法命令或未提供协议版本",
    0x02: "内存不足",
    0x03: "数据不正确或格式错误",
    0x64: "会话句柄非法(目标侧会话已失效)",
    0x65: "长度域非法",
    0x69: "不支持的封装协议版本",
}
"""ENIP 封装层状态 → 可读描述(last_error 用;非 0 按坏帧处理触发惰性重连)。"""
AB_CIP_STATUS_TEXT: dict = {
    0x00: "成功",
    0x01: "连接失败",
    0x02: "资源不可用",
    0x03: "参数值非法",
    0x04: "路径段错误",
    0x05: "路径目标不存在",
    0x06: "部分传输(分片)",
    0x07: "连接已丢失",
    0x08: "服务不支持",
    0x09: "属性非法",
    0x0A: "属性列表错误",
    0x0B: "已处于请求的模式/状态",
    0x0C: "对象状态冲突",
    0x0D: "对象已存在",
    0x0E: "属性不可写",
    0x0F: "权限不足",
    0x10: "设备状态冲突",
    0x11: "应答数据过大",
    0x12: "原始值分片",
    0x13: "数据不足",
    0x14: "属性不支持",
    0x15: "数据过多",
    0x16: "对象不存在",
    0x17: "分片序列未在进行中",
    0x18: "无存储的属性数据",
    0x19: "存储操作失败",
    0x1A: "路由失败,请求包过大",
    0x1B: "路由失败,应答包过大",
    0x1C: "属性列表缺项",
    0x1D: "属性值列表非法",
    0x1E: "内嵌服务错误",
    0x1F: "厂商自定义",
    0x20: "参数非法",
    0x21: "一次性值已写入",
    0x22: "收到非法应答",
    0x23: "缓冲区溢出",
    0x24: "报文格式非法",
    0x25: "路径关键字校验失败",
    0x26: "路径字数非法",
    0x27: "列表中出现意外属性",
    0x28: "成员 ID 非法",
    0x29: "成员不可写",
    0x2A: "仅组 2 服务器通用失败",
    0x2B: "未知 Modbus 错误",
    0x2C: "属性不可读",
}
"""CIP 通用状态码 → 可读描述(last_error 用;未收录的提示十六进制原文)。"""

# ---------------------------------------------------------------- 通用自定义 TCP
OPEN_TCP_DEFAULT_PORT: int = 9000
"""OpenTcpClient 默认端口(自定义设备无统一标准,仅占位,按现场配置)。"""
OPEN_TCP_DEFAULT_DELIMITER: str = "\r\n"
"""OpenTcpClient 默认帧分隔符(CR LF,行式协议最常见的应答结尾)。"""
OPEN_TCP_MAX_FRAME: int = 4096
"""OpenTcpClient 帧内容字节上限(不含分隔符;超限未见到分隔符按坏帧断线惰性重连)。"""
OPEN_TCP_RECV_CHUNK: int = 256
"""OpenTcpClient 接收缓冲单次读取字节数(内部实现参数)。"""

# ---------------------------------------------------------------- 倍福 TwinCAT(ADS)
ADS_DEFAULT_ADS_PORT: int = 851
"""TwinCAT 3 PLC 运行时 1 的默认 AMS 端口(852 起为后续运行时;TC2 为 801)。"""
ADS_NET_ID_SUFFIX: str = ".1.1"
"""AMS NetId 默认后缀:NetId 共 6 字节,前 4 字节通常为 IP,后两段惯例 1.1。"""

# ---------------------------------------------------------------- 通用
BIT_INDEX_MAX: int = 63
"""位操作工具允许的最大位号。"""
READ_STRING_DEFAULT_LENGTH: int = 32
DEFAULT_STRING_ENCODING: str = "ascii"
