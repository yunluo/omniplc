"""omniplc —— 多品牌多协议 PLC 统一通信库。

一次编写,通过一致的 API 对接三菱(MC 协议)、欧姆龙(FINS、NJ/NX CIP)、
基恩士(KV Host Link、MC 协议兼容、SR 扫码枪)、汇川(H3U/H5U)、
松下(FP 系列:MC 协议兼容、MEWTOCOL)、丰田(TOYOPUC 计算机链接)、
罗克韦尔(Allen-Bradley,EtherNet/IP)等设备,
支持 Modbus TCP/RTU、MC 3E/4E/1E、MC 串口 3C/4C 帧、MC over MX Component、
FINS over TCP/UDP、NJ/NX CIP(EtherNet/IP 变量读写)、
KV Host Link over TCP/UDP、KV MC 协议兼容(SLMP 3E)、
汇川 Modbus TCP/RTU 与 MC 协议兼容(3E)、
松下 MC 协议兼容(3E)与 MEWTOCOL(TCP/UDP)、
SR 扫码枪、TOYOPUC 计算机链接 over TCP/UDP、EtherNet/IP(Logix 标签读写)、
OPC-UA(封装 asyncua,opc.tcp 会话)、
CNC 机床数采(MTConnect Agent,HTTP/XML 只读;
三菱 CNC EZSocket GIOP 直连,M70/M700 系数采只读)、
西门子 S7(自研 S7comm 协议栈,DB/I/Q/M 绝对寻址,零第三方依赖)。

同步客户端::

    from omniplc import ModbusTcpClient

    with ModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = client.read_float("hr0")

异步客户端(同步类名前加 A)::

    from omniplc.aio import AModbusTcpClient

    client = AModbusTcpClient("192.168.0.10", 502, 1)
    await client.connect()
    ok, value = await client.read_float("hr0")

报文调试(全局开关,输出所有协议的请求/响应)::

    import omniplc

    omniplc.set_debug(True)

报文黑匣子(只存不打印,故障后取最近报文)::

    omniplc.set_frame_recorder(True)            # 常驻留存最近 1000 帧
    for rec in omniplc.recorded_frames():       # 故障后取现场
        print(rec.at, rec.direction, rec.label, rec.data.hex())

报错语言(全局开关,默认中文)::

    import omniplc

    omniplc.set_lang("en")  # 之后报错文案输出英文

监视器(客户端下建,默认不启动;本地快照读,不发报文)::

    import omniplc

    client = omniplc.ModbusTcpClient("192.168.0.10", 502, 1)
    monitor = client.create_monitor({"炉温": ("hr0", "float")}, interval=1.0,
                                    on_change=lambda ev: print(ev))
    with client:
        monitor.start()
        snap = monitor.get("炉温")   # PointSnapshot(质量, 值, 时间戳)
"""

from __future__ import annotations

from .core import convert
from .core.base_client import BaseClient, ClientStats
from .core.debug import (
    FrameRecord,
    clear_recorded_frames,
    recorded_frames,
    set_debug,
    set_frame_recorder,
)
from .core.errors import ErrorCategory
from .core.i18n import set_lang
from .core.monitor import (
    Monitor,
    MonitorEvent,
    MonitorQuality,
    MonitorStats,
    PointSnapshot,
)
from .core.tag import Tag, TagTable
from .core.types import ByteOrder, DataType, McFrame, S7Model, SerialParity, WordOrder
from .cnc import FanucFocasClient, MitsubishiEzSocketClient, MTConnectClient
from .cnc.ezsocket import (
    EzAlarm,
    EzAlarmType,
    EzDeviceStatus,
    EzFeedSpeedType,
    EzPositionType,
    EzProgramBlock,
    EzProgramFileInfo,
    EzRunMode,
    EzRunState,
    EzRunStatus,
    EzSocketMachine,
)
from .cnc.focas import FocasCncId, FocasDynamic, FocasStatus, FocasSysInfo
from .plc.modbus import ModbusArea, ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from .plc.opcua import OpcUaClient, OpcUaSubscription
from .plc.melsec import (
    MelsecMcSerialClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
)
from .plc.siemens import SiemensS7Client
from .plc.omron import (
    FinsClock,
    OmronCipClient,
    OmronFinsTcpClient,
    OmronFinsUdpClient,
)
from .plc.ab import AbTagEntry, AllenBradleyEthIpClient
from .plc.panasonic import (
    PanasonicMcTcpClient,
    PanasonicMewtocolTcpClient,
    PanasonicMewtocolUdpClient,
)
from .plc.keyence import (
    KeyenceHostLinkTcpClient,
    KeyenceHostLinkUdpClient,
    KeyenceMcTcpClient,
    KeyenceMcUdpClient,
)
from .plc.inovance import InovanceMcTcpClient, InovanceRtuClient, InovanceTcpClient
from .plc.toyopuc import ToyopucTcpClient, ToyopucUdpClient
from .reader import (
    HikrobotIdModbusClient,
    HikrobotIdSdkClient,
    HikrobotIdSerialClient,
    HikrobotIdTcpClient,
    HikrobotSdkCode,
    HikrobotSdkFrame,
    HikrobotSdkQuality,
    HikrobotStatus,
    KeyenceSrClient,
)
from .transport import (
    BaseTransport,
    SerialConfig,
    SerialTransport,
    TcpTransport,
    UdpTransport,
)

__version__ = "0.55.7"
__author__ = "云落"
__description__ = "多品牌多协议 PLC 统一通信库(Modbus / 三菱 MC 3E/4E/1E 与串口 1C/3C/4C / MX Component / 欧姆龙 FINS / NJ/NX CIP / 基恩士 KV Host Link / KV MC 兼容 / 汇川 H3U/H5U / 松下 MC 兼容/MEWTOCOL / 丰田 TOYOPUC / AB EtherNet/IP / 西门子 S7 / OPC-UA / CNC MTConnect / 三菱 CNC EZSocket)"

__all__ = [
    # ---- 客户端基类 ----
    "BaseClient",
    # ---- 连接健康统计快照类型 ----
    "ClientStats",
    # ---- Modbus 客户端 ----
    "ModbusBaseClient",
    "ModbusTcpClient",
    "ModbusRtuClient",
    # ---- 三菱 MC 客户端 ----
    "MelsecMcTcpClient",
    "MelsecMcUdpClient",
    "MelsecMcSerialClient",
    "MelsecMxClient",
    # ---- 基恩士 KV Host Link / MC 兼容客户端 ----
    "KeyenceHostLinkTcpClient",
    "KeyenceHostLinkUdpClient",
    "KeyenceMcTcpClient",
    "KeyenceMcUdpClient",
    # ---- 基恩士 SR 扫码枪 ----
    "KeyenceSrClient",
    # ---- 海康机器人 ID 系列智能读码器(Modbus) ----
    "HikrobotIdModbusClient",
    "HikrobotStatus",
    # ---- 海康机器人 ID 系列读码器(TCP 命令) ----
    "HikrobotIdTcpClient",
    # ---- 海康机器人 ID 系列智能读码器(SDK) ----
    "HikrobotIdSdkClient",
    "HikrobotSdkCode",
    "HikrobotSdkFrame",
    "HikrobotSdkQuality",
    # ---- 海康机器人 ID 系列读码器(串口) ----
    "HikrobotIdSerialClient",
    # ---- 欧姆龙 FINS / CIP 客户端 ----
    "OmronFinsTcpClient",
    "OmronFinsUdpClient",
    "OmronCipClient",
    "FinsClock",
    # ---- 汇川 H3U/H5U 客户端 ----
    "InovanceTcpClient",
    "InovanceRtuClient",
    "InovanceMcTcpClient",
    # ---- 松下 FP 系列客户端 ----
    "PanasonicMcTcpClient",
    "PanasonicMewtocolTcpClient",
    "PanasonicMewtocolUdpClient",
    # ---- 丰田 TOYOPUC 客户端 ----
    "ToyopucTcpClient",
    "ToyopucUdpClient",
    # ---- 罗克韦尔 AB EtherNet/IP 客户端 ----
    "AllenBradleyEthIpClient",
    "AbTagEntry",
    # ---- 西门子 S7 客户端 ----
    "SiemensS7Client",
    # ---- OPC-UA 客户端 ----
    "OpcUaClient",
    "OpcUaSubscription",
    # ---- CNC 机床数采客户端 ----
    "MTConnectClient",
    "FanucFocasClient",
    "FocasSysInfo",
    "FocasDynamic",
    "FocasStatus",
    "FocasCncId",
    "MitsubishiEzSocketClient",
    "EzSocketMachine",
    "EzRunState",
    "EzRunMode",
    "EzRunStatus",
    "EzDeviceStatus",
    "EzPositionType",
    "EzFeedSpeedType",
    "EzAlarmType",
    "EzProgramFileInfo",
    "EzAlarm",
    "EzProgramBlock",
    # ---- 传输层 ----
    "BaseTransport",
    "TcpTransport",
    "UdpTransport",
    "SerialTransport",
    "SerialConfig",
    # ---- 点位表 ----
    "Tag",
    "TagTable",
    # ---- 类型与字序 ----
    "DataType",
    "WordOrder",
    "ByteOrder",
    "SerialParity",
    "McFrame",
    "S7Model",
    "ModbusArea",
    # ---- 错误分类 ----
    "ErrorCategory",
    # ---- 纯帮助函数模块(转换/校验和) ----
    "convert",
    # ---- 全局调试 ----
    "set_debug",
    # ---- 报文黑匣子(环形缓冲留存最近报文) ----
    "set_frame_recorder",
    "FrameRecord",
    "recorded_frames",
    "clear_recorded_frames",
    # ---- 报错文案语言 ----
    "set_lang",
    # ---- 监视器(周期轮询采集) ----
    "Monitor",
    "MonitorQuality",
    "MonitorEvent",
    "MonitorStats",
    "PointSnapshot",
    # ---- 元数据 ----
    "__version__",
    "__author__",
    "__description__",
]
