"""核心层:客户端公共基类、错误类与全局常量。"""
from .base_client import BaseClient, validate_endpoint
from .constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
)
from .errors import (
    DeviceError,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
)

__all__ = [
    "BaseClient",
    "validate_endpoint",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_RECEIVE_TIMEOUT",
    "OmniPLCInternalError",
    "TransportClosedError",
    "ProtocolFrameError",
    "DeviceError",
]
