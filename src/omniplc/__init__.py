"""omniplc —— 多品牌多协议 PLC 统一通信库。

一次编写,通过一致的 API 对接三菱(MC 协议)、欧姆龙(FINS)、
基恩士(KV Host Link、SR 扫码枪)、丰田(TOYOPUC 计算机链接)等设备,
支持 Modbus TCP/RTU、MC 3E/4E/1E、MC over MX Component、FINS over TCP/UDP、
KV Host Link over TCP/UDP、SR 扫码枪、TOYOPUC 计算机链接 over TCP/UDP、
OPC-UA(封装 asyncua,opc.tcp 会话)。

同步客户端::

    from omniplc import ModbusTcpClient

    with ModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = client.read_float("hr0")

异步客户端(同步类名前加 A)::

    from omniplc.aio import AModbusTcpClient

    client = AModbusTcpClient("192.168.0.10", 502, 1)
    await client.connect()
    ok, value = await client.read_float("hr0")
"""
from __future__ import annotations

from . import convert
from .core.base_client import BaseClient
from .modbus import ModbusArea, ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from .opcua import OpcUaClient
from .plc.melsec import MelsecMcTcpClient, MelsecMcUdpClient, MelsecMxClient
from .plc.omron import OmronFinsTcpClient, OmronFinsUdpClient
from .plc.keyence import KeyenceHostLinkTcpClient, KeyenceHostLinkUdpClient
from .plc.toyopuc import ToyopucTcpClient, ToyopucUdpClient
from .scanner import KeyenceSrClient
from .tag import Tag, TagTable
from .transport import BaseTransport, SerialConfig, SerialTransport, TcpTransport, UdpTransport
from .types import ByteOrder, DataType, McFrame, SerialParity, WordOrder

__version__ = "0.1.0"
__author__ = "云落"
__description__ = "多品牌多协议 PLC/扫码枪统一通信库(Modbus / 三菱 MC / 欧姆龙 FINS / 基恩士 KV Host Link / SR / 丰田 TOYOPUC)"

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
    "MelsecMxClient",
    # ---- 基恩士 KV Host Link 客户端 ----
    "KeyenceHostLinkTcpClient",
    "KeyenceHostLinkUdpClient",
    # ---- 基恩士 SR 扫码枪 ----
    "KeyenceSrClient",
    # ---- 欧姆龙 FINS 客户端 ----
    "OmronFinsTcpClient",
    "OmronFinsUdpClient",
    # ---- 丰田 TOYOPUC 客户端 ----
    "ToyopucTcpClient",
    "ToyopucUdpClient",
    # ---- OPC-UA 客户端 ----
    "OpcUaClient",
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
    # ---- 元数据 ----
    "__version__",
    "__author__",
    "__description__",
]
