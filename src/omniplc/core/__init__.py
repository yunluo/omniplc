"""核心层:客户端公共基类、共享类型与点位表、纯转换函数、错误类与全局常量。

子模块分工:``base_client``(客户端模板方法)/ ``types``(公共枚举)/
``tag``(Tag / TagTable 点位表)/ ``convert``(纯转换与校验和函数)/
``errors`` / ``constants`` / ``debug`` / ``validation``。协议层与传输层
一律从本层取共享定义,不反向依赖具体驱动。
"""
from .base_client import BaseClient, ClientStats, validate_endpoint
from .constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
)
from .errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
)

__all__ = [
    "BaseClient",
    "ClientStats",
    "validate_endpoint",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_RECEIVE_TIMEOUT",
    "OmniPLCInternalError",
    "TransportClosedError",
    "ProtocolFrameError",
    "DeviceError",
    "ErrorCategory",
]
