"""传输层:可插拔的 TCP/UDP/串口实现。

协议层只依赖 :class:`BaseTransport` 抽象,新增走线不动协议层。
"""
from .base import BaseTransport
from .serial import SerialConfig, SerialTransport
from .tcp import TcpTransport
from .udp import UdpTransport

__all__ = [
    "BaseTransport",
    "SerialConfig",
    "SerialTransport",
    "TcpTransport",
    "UdpTransport",
]
