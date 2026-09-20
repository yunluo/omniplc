"""通用自定义 TCP/IP 驱动包(分隔符成帧,收发行为可配)。"""
from .client import OpenTcpClient

__all__ = ["OpenTcpClient"]
