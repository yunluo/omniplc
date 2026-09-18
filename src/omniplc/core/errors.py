"""错误类集中定义(core 层)。

公共 API(读写方法)**不抛自定义异常**——所有失败都以
``(False, None)``/``False`` 返回,并把原因写入 :attr:`BaseClient.last_error`。
本模块的异常只在库内部用于控制流,不会逃逸到调用方,驱动实现用它区分错误类别:

- :class:`OmniPLCInternalError`:内部异常基类
- :class:`TransportClosedError`:连接未建立/已被对端关闭
- :class:`ProtocolFrameError`:坏帧(长度不符、校验错、非预期功能码)
- :class:`DeviceError`:PLC 返回了错误码,``code`` 属性携带原始错误码
  (Modbus 异常码 / MC 结束码 / FINS 结束码)
"""
from __future__ import annotations


class OmniPLCInternalError(Exception):
    """omniplc 内部异常基类。"""


class TransportClosedError(OmniPLCInternalError):
    """传输通道未连接或已被对端关闭。"""


class ProtocolFrameError(OmniPLCInternalError):
    """报文编解码失败:长度不符、校验错、非预期功能码等。"""


class DeviceError(OmniPLCInternalError):
    """PLC 返回了错误码(如 Modbus 异常码、MC 结束码、FINS 结束码)。

    :param message: 错误描述
    :param code: PLC 返回的原始错误码
    """

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code
