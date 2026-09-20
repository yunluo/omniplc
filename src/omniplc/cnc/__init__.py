"""CNC 机床数采驱动(MTConnect 等开放标准;FOCAS/EZSocket DLL 封装留后续)。"""
from .mtconnect import MTConnectClient

__all__ = ["MTConnectClient"]
