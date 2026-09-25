"""错误类集中定义(core 层)。

公共 API(读写方法)**不抛自定义异常**——所有失败都以
``(False, None)``/``False`` 返回,并把原因写入 :attr:`BaseClient.last_error`。
本模块的异常只在库内部用于控制流,不会逃逸到调用方,驱动实现用它区分错误类别:

- :class:`OmniPLCInternalError`:内部异常基类
- :class:`TransportClosedError`:连接未建立/已被对端关闭
- :class:`ProtocolFrameError`:坏帧(长度不符、校验错、非预期功能码)
- :class:`DeviceError`:PLC 返回了错误码,``code`` 属性携带原始错误码
  (Modbus 异常码 / MC 结束码 / FINS 结束码)
- :class:`TransportTimeoutError`:串口/UDP 传输超时(DeviceError 子类,
  不断线;TCP 超时保持 OSError 语义拆连防串帧)
"""
from __future__ import annotations

import asyncio
import enum
from concurrent.futures import CancelledError as _FuturesCancelledError
from typing import Tuple, Type

# 取消异常口径(3.7 与 3.8+ **不是同一个类**,必须两条一起捕):
#
# - **3.7**:``asyncio.CancelledError`` 就是 ``concurrent.futures.CancelledError``
#   (同一个类,``Exception`` 子类);
# - **3.8 起**:asyncio 的取消异常改为自己的 ``BaseException`` 子类
#   (``asyncio.exceptions.CancelledError``),与 futures 版不再是同一个类。
#
# 只捕其中一个,任务取消、以及"超时后取消内层任务"落地时的 ``CancelledError``
# 都会漏网(native 的超时与取消路径会因此静默失效:超时抛 CancelledError 而非
# ``socket.timeout``/``TransportTimeoutError``,取消也不再保守拆连)——本地 .venv
# 是 3.7.9,天然看不出来,由 CI 的 3.12 腿抓到(实测 7 例 FAILED)。故这里给出
# 跨代元组供 ``except`` 直接用;需要连普通异常一起吞时用
# ``_CANCELLED_ERRORS + (Exception,)``。
#
# 注意取 asyncio 那一条要走 ``getattr``:它的类名**不在 builtins 里**,
# 且 3.11+ 的类型库把它建模成内建名,直接 ``from asyncio import CancelledError``
# 会被 ty 判为"模块无此成员"。
_CANCELLED_ERRORS: Tuple[Type[BaseException], ...] = tuple(
    cls
    for cls in (
        _FuturesCancelledError,
        getattr(asyncio, "CancelledError", None),
    )
    if cls is not None
)


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


class TransportTimeoutError(DeviceError):
    """传输层接收超时(链路完好,按"不断线"语义处理)。

    与连接死亡(OSError)区分:**0 字节已读**的超时无残留字节错位风险
    (UDP 整数据报、串口 0 字节已读),不拆连可避免慢链路上的重连+握手
    抖动;串口**部分字节已读后超时**(帧截断)由 SerialTransport 主动
    关闭串口抛 TransportClosedError,借重连重新同步;TCP 超时保持
    OSError 语义拆连——迟到响应残留在 socket 缓冲,拆连正是防串帧的机制。

    计数与重试口径(``BaseClient._execute``):**不计入**
    ``device_error_count``("设备返回错误码的次数"不含传输超时),但与其他
    传输失败一样按 ``retries``/``write_retries`` 重试(0 字节已读,原连接
    上重发安全)。
    """


class ErrorCategory(enum.Enum):
    """失败分类(供上位系统告警分级,见 ``BaseClient.last_error_category``)。

    规则表在 ``base_client._categorize``(顺序敏感)。**新增异常类型时
    必须同步维护该规则表**,否则落入 UNKNOWN 兜底。
    """

    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    DEVICE = "device"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
