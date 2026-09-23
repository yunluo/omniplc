"""omniplc —— 多品牌多协议 PLC 统一通信库。

一次编写,通过一致的 API 对接三菱(MC 协议)、欧姆龙(FINS、NJ/NX CIP)、
基恩士(KV Host Link、MC 协议兼容、SR 扫码枪)、汇川(H3U/H5U)、
松下(FP 系列:MC 协议兼容、MEWTOCOL)、丰田(TOYOPUC 计算机链接)、
罗克韦尔(Allen-Bradley,EtherNet/IP)、倍福(TwinCAT,ADS)等设备,
支持 Modbus TCP/RTU、MC 3E/4E/1E、MC 串口 3C/4C 帧、MC over MX Component、
FINS over TCP/UDP、NJ/NX CIP(EtherNet/IP 变量读写)、TwinCAT ADS(封装 pyads)、
KV Host Link over TCP/UDP、KV MC 协议兼容(SLMP 3E)、
汇川 Modbus TCP/RTU 与 MC 协议兼容(3E)、
松下 MC 协议兼容(3E)与 MEWTOCOL(TCP/UDP)、
SR 扫码枪、TOYOPUC 计算机链接 over TCP/UDP、EtherNet/IP(Logix 标签读写)、
通用自定义 TCP(分隔符成帧,收发行为可配)、
OPC-UA(封装 asyncua,opc.tcp 会话)、
CNC 机床数采(MTConnect Agent,HTTP/XML 只读)、
西门子 S7(封装 python-snap7,DB/I/Q/M 绝对寻址)。

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
"""
from __future__ import annotations

from . import convert
from .cnc import MTConnectClient
from .core.base_client import BaseClient
from .core.debug import set_debug
from .modbus import ModbusArea, ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from .opcua import OpcUaClient
from .plc.melsec import (
    MelsecMcSerialClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
)
from .plc.siemens import SiemensS7Client
from .plc.beckhoff import BeckhoffAdsClient
from .plc.omron import OmronCipClient, OmronFinsTcpClient, OmronFinsUdpClient
from .plc.ab import AllenBradleyEthIpClient
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
from .opentcp import OpenTcpClient
from .scanner import KeyenceSrClient
from .tag import Tag, TagTable
from .transport import BaseTransport, SerialConfig, SerialTransport, TcpTransport, UdpTransport
from .types import ByteOrder, DataType, McFrame, SerialParity, WordOrder

__version__ = "0.31.1"
__author__ = "云落"
__description__ = "多品牌多协议 PLC/扫码枪统一通信库(Modbus / 三菱 MC / 欧姆龙 FINS / 基恩士 KV Host Link / MC 兼容 / SR / 丰田 TOYOPUC)"

__all__ = [
    # ---- 客户端基类 ----
    "BaseClient",
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
    # ---- 欧姆龙 FINS / CIP 客户端 ----
    "OmronFinsTcpClient",
    "OmronFinsUdpClient",
    "OmronCipClient",
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
    # ---- 倍福 TwinCAT ADS 客户端 ----
    "BeckhoffAdsClient",
    # ---- 西门子 S7 客户端 ----
    "SiemensS7Client",
    # ---- 通用自定义 TCP 客户端 ----
    "OpenTcpClient",
    # ---- OPC-UA 客户端 ----
    "OpcUaClient",
    # ---- CNC 机床数采客户端 ----
    "MTConnectClient",
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
    "ModbusArea",
    # ---- 纯帮助函数模块(转换/校验和) ----
    "convert",
    # ---- 全局调试 ----
    "set_debug",
    # ---- 元数据 ----
    "__version__",
    "__author__",
    "__description__",
]
