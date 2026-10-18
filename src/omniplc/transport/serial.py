"""串口传输实现(RTU/Host Link 等走线)。

pyserial 为可选依赖:仅在使用 :class:`SerialTransport` 时才需要安装
(``pip install omniplc[serial]`` 或 ``uv add 'omniplc[serial]'``)。
导入本模块**不会**导入 pyserial,实例化并连接时才检查。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional, Union

from .base import BaseTransport
from ..core.constants import (
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
)
from ..core.debug import RECV_MARK, SEND_MARK, log_frame, log_op
from ..core.errors import TransportClosedError, TransportTimeoutError
from ..core.types import PrimitiveValue, SerialParity
from ..core.i18n import _
from ..core.validation import require_int


@dataclass
class SerialConfig:
    """串口参数配置。

    构造即校验(:meth:`validate` 在 ``__post_init__`` 里调用):参数非法在
    ``configure_serial()`` 调用点就抛 :class:`ValueError`,不必等第一次
    收发才发现(客户端把 ``SerialConfig`` 构造在事务路径上,延迟校验会把
    参数错误伪装成事务失败)。

    :param port_name: 串口名,如 ``"COM3"``(Windows)或 ``"/dev/ttyS0"``(Linux)
    :param baud_rate: 波特率,默认 9600(必须是 >0 的整数)
    :param data_bits: 数据位 5~8,默认 8(整数)
    :param stop_bits: 停止位 1/1.5/2,默认 1
    :param parity: 校验位,推荐 :class:`omniplc.types.SerialParity` 枚举,
        也兼容 ``"N"``/``"E"``/``"O"`` 字符串

    :raises ValueError: 参数非法(review-1038 §7.2:原校验用裸比较,
        ``baud_rate=nan/inf/True`` 通过后被 pyserial 静默收窄,
        ``data_bits=6.5`` 通过后炸成 ``KeyError: 6.5``,字符串参数抛
        ``TypeError`` 直穿公开 API——现统一走 :func:`require_int`)
    """

    port_name: str
    baud_rate: int = SERIAL_DEFAULT_BAUD_RATE
    data_bits: int = SERIAL_DEFAULT_DATA_BITS
    stop_bits: float = SERIAL_DEFAULT_STOP_BITS
    parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY

    def __post_init__(self) -> None:
        """构造即校验(见类 docstring)。"""
        self.validate()

    def validate(self) -> None:
        """校验参数合法性。

        :raises ValueError: 参数非法
        """
        if not self.port_name or not self.port_name.strip():
            raise ValueError(_("串口名 port_name 不能为空"))
        baud_rate = _require_int_field(_("波特率 baud_rate"), self.baud_rate)
        if baud_rate <= 0:
            raise ValueError(_("波特率必须大于 0,收到:{}").format(self.baud_rate))
        # 数据位先按整数收口(排除 bool/float/str),再判 5~8:
        # 原实现 ``5 <= 6.5 <= 8`` 判真,越到 connect 才 KeyError
        data_bits = _require_int_field(_("数据位 data_bits"), self.data_bits)
        if not 5 <= data_bits <= 8:
            raise ValueError(_("数据位必须在 5~8 之间,收到:{}").format(self.data_bits))
        if self.stop_bits not in (1, 1.5, 2):
            raise ValueError(_("停止位必须是 1/1.5/2,收到:{}").format(self.stop_bits))
        _coerce_parity(self.parity)


class SerialTransport(BaseTransport):
    """串口传输,基于 pyserial。

    recv 语义:阻塞读取恰好 ``size`` 字节。0 字节已读超时抛
    :class:`omniplc.core.errors.TransportTimeoutError`(DeviceError 子类,
    不断线);部分字节已读后超时(帧截断)主动关闭串口并抛
    :class:`omniplc.core.errors.TransportClosedError`,借惰性重连重新
    同步(见 :meth:`recv`)。

    :attr:`receive_timeout` 修改后**立即作用于已打开串口**。
    """

    _PARITY_MAP = {
        SerialParity.NONE: "PARITY_NONE",
        SerialParity.EVEN: "PARITY_EVEN",
        SerialParity.ODD: "PARITY_ODD",
    }
    _DATA_BITS_MAP = {5: "FIVEBITS", 6: "SIXBITS", 7: "SEVENBITS", 8: "EIGHTBITS"}
    _STOP_BITS_MAP = {
        1: "STOPBITS_ONE",
        1.5: "STOPBITS_ONE_POINT_FIVE",
        2: "STOPBITS_TWO",
    }

    def __init__(self, config: SerialConfig) -> None:
        """初始化串口传输。

        :param config: 串口参数
        """
        super().__init__()
        config.validate()
        self._config = config
        self._serial: Optional[Any] = None
        self._debug_label = f"serial://{config.port_name}({config.baud_rate})"

    @property
    def baud_rate(self) -> int:
        """当前波特率(构造期定,只读)。

        供协议层计算线速相关时序——如 Modbus RTU 帧间 3.5 字符静默期
        (T3.5 = 3.5 × 11 位 / 波特率,每字符按 1 起始 + 8 数据 + 1 校验
        + 1 停止 = 11 位计)。
        """
        return self._config.baud_rate

    @BaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """串口已打开时立即下发。"""
        BaseTransport.receive_timeout.fset(self, seconds)  # type: ignore[attr-defined]
        port = self._serial
        if port is not None:
            port.timeout = self._receive_timeout
            port.write_timeout = self._receive_timeout

    def connect(self) -> None:
        """打开串口。

        :raises RuntimeError: 未安装 pyserial
        :raises OSError: 串口打开失败(占用/不存在等)
        """
        try:
            import serial  # 延迟导入:仅在真正使用串口时要求 pyserial
        except ImportError as exc:
            raise RuntimeError(
                _("串口传输需要 pyserial 支持,请安装:pip install omniplc[serial]")
            ) from exc

        port = serial.Serial()
        port.port = self._config.port_name
        port.baudrate = self._config.baud_rate
        port.bytesize = getattr(serial, self._DATA_BITS_MAP[self._config.data_bits])
        port.stopbits = getattr(serial, self._STOP_BITS_MAP[self._config.stop_bits])
        port.parity = getattr(
            serial, self._PARITY_MAP[_coerce_parity(self._config.parity)]
        )
        port.timeout = self._receive_timeout
        port.write_timeout = self._receive_timeout
        port.open()
        self._serial = port
        log_op(self._debug_label, "已连接")

    def close(self) -> None:
        """关闭串口,幂等。"""
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
            log_op(self._debug_label, "已断开")

    def send(self, data: bytes) -> None:
        """发送字节。

        :raises TransportClosedError: 串口未打开
        :raises OSError: 发送失败或超时
        """
        port = self._require_serial()
        log_frame(self._debug_label, SEND_MARK, data)
        port.write(data)

    def recv(self, size: int) -> bytes:
        """读取恰好 ``size`` 字节。

        超时分两种情况(以是否已收到部分字节划分):

        - **0 字节已读超时**:线上安静、无帧残渣,抛
          :class:`omniplc.core.errors.TransportTimeoutError`(DeviceError
          子类,按链路完好不断线处理)
        - **部分字节已读后超时**:帧被截断,已读字节无法回退到 OS 缓冲,
          下一帧开头必然错位——关闭串口并抛
          :class:`omniplc.core.errors.TransportClosedError`,借基类惰性
          重连重新打开串口(OS 清缓冲)完成重新同步。与 Modbus RTU CRC
          校验失败后的断线重连是同一恢复逻辑。

        :raises TransportClosedError: 串口未打开,或帧截断后已主动关闭串口
        :raises TransportTimeoutError: 接收超时(0 字节已读)
        """
        port = self._require_serial()
        deadline = time.monotonic() + self._receive_timeout
        chunks = []
        received = 0
        while received < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._timeout_exit(received, size)
            port.timeout = remaining
            chunk = port.read(size - received)
            if not chunk:
                self._timeout_exit(received, size)
            chunks.append(chunk)
            received += len(chunk)
        frame = b"".join(chunks)
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def recv_some(self, max_bytes: int) -> bytes:
        """读取一批当前到达的数据(1~``max_bytes`` 字节,有数据即返回)。

        :meth:`recv` 是"读满恰好 size;部分字节超时即关串口"的**定长**口径,
        不能当流式成帧的"有数据尽快返回"用——基类默认退化会把
        ``recv_some(n)`` 变成"要么凑满 n 字节,要么关串口断线"(review-1038
        §7.1:KV Host Link 的流式收行在串口走线上必然失败,每事务
        "超时→关串口→重连→再超时")。本覆写按 pyserial 的 ``in_waiting``
        探量:先等**首字节**(超时按 0 字节口径抛
        :class:`omniplc.core.errors.TransportTimeoutError`,不断线),到达后
        只读当前已可读的量,不足 ``max_bytes`` 不再等待。

        :param max_bytes: 单批字节数上限(必须 ≥1)
        :return: 收到的字节(非空)
        :raises ValueError: ``max_bytes`` ≤ 0
        :raises TransportClosedError: 串口未打开
        :raises TransportTimeoutError: 超时内无任何数据到达(0 字节,不断线)
        """
        if max_bytes <= 0:
            raise ValueError(_("max_bytes 必须大于 0,收到:{}").format(max_bytes))
        port = self._require_serial()
        port.timeout = self._receive_timeout
        chunk = bytearray(port.read(1))
        if not chunk:
            # 0 字节已读:线上安静、无帧残渣,与 recv 同口径不断线
            raise TransportTimeoutError(
                _("串口读取超时(receive_timeout={})").format(self._receive_timeout),
                0,
            )
        waiting = getattr(port, "in_waiting", 0) or 0
        if waiting > 0 and len(chunk) < max_bytes:
            chunk.extend(port.read(min(int(waiting), max_bytes - len(chunk))))
        frame = bytes(chunk)
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def _timeout_exit(self, received: int, size: int) -> None:
        """超时收尾:按已读字节数选择不断线超时或断线重同步(内部方法)。"""
        if received == 0:
            raise TransportTimeoutError(
                _("串口读取超时(receive_timeout={})").format(self._receive_timeout),
                0,
            )
        self.close()
        raise TransportClosedError(
            _(
                "串口读取超时且已收 {}/{} 字节(帧截断,残渣必致后续帧错位),已关闭串口,下次事务将重新打开以重新同步"
            ).format(received, size)
        )

    def _require_serial(self) -> Any:
        """取当前串口对象,未打开则抛出。"""
        if self._serial is None:
            raise TransportClosedError(_("串口未打开,请先调用 connect()"))
        return self._serial


def _coerce_parity(value: Union[SerialParity, str]) -> SerialParity:
    """把枚举成员或字符串统一解析为 SerialParity(内部函数)。"""
    if isinstance(value, SerialParity):
        return value
    try:
        return SerialParity(str(value).strip().upper())
    except ValueError:
        raise ValueError(
            _("校验位必须是 SerialParity 枚举或 N/E/O,收到:{!r}").format(value)
        ) from None


def _require_int_field(name: str, value: PrimitiveValue) -> int:
    """校验串口整数字段(内部函数,复用 :func:`require_int` 口径并带字段名)。

    :raises ValueError: 非整数(bool/浮点/字符串均拒)
    """
    try:
        return require_int(value)
    except ValueError as exc:
        raise ValueError(
            _("{} 必须是整数(不接受 bool/浮点/字符串),收到:{!r}").format(name, value)
        ) from exc
