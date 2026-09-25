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
from ..types import SerialParity


@dataclass
class SerialConfig:
    """串口参数配置。

    :param port_name: 串口名,如 ``"COM3"``(Windows)或 ``"/dev/ttyS0"``(Linux)
    :param baud_rate: 波特率,默认 9600
    :param data_bits: 数据位 5~8,默认 8
    :param stop_bits: 停止位 1/1.5/2,默认 1
    :param parity: 校验位,推荐 :class:`omniplc.types.SerialParity` 枚举,
        也兼容 ``"N"``/``"E"``/``"O"`` 字符串
    """

    port_name: str
    baud_rate: int = SERIAL_DEFAULT_BAUD_RATE
    data_bits: int = SERIAL_DEFAULT_DATA_BITS
    stop_bits: float = SERIAL_DEFAULT_STOP_BITS
    parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY

    def validate(self) -> None:
        """校验参数合法性。

        :raises ValueError: 参数非法
        """
        if not self.port_name or not self.port_name.strip():
            raise ValueError("串口名 port_name 不能为空")
        if self.baud_rate <= 0:
            raise ValueError(f"波特率必须大于 0,收到:{self.baud_rate}")
        if not 5 <= self.data_bits <= 8:
            raise ValueError(f"数据位必须在 5~8 之间,收到:{self.data_bits}")
        if self.stop_bits not in (1, 1.5, 2):
            raise ValueError(f"停止位必须是 1/1.5/2,收到:{self.stop_bits}")
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
    _STOP_BITS_MAP = {1: "STOPBITS_ONE", 1.5: "STOPBITS_ONE_POINT_FIVE", 2: "STOPBITS_TWO"}

    def __init__(self, config: SerialConfig) -> None:
        """初始化串口传输。

        :param config: 串口参数
        """
        super().__init__()
        config.validate()
        self._config = config
        self._serial: Optional[Any] = None
        self._debug_label = f"serial://{config.port_name}({config.baud_rate})"

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
                "串口传输需要 pyserial 支持,请安装:pip install omniplc[serial]"
            ) from exc

        port = serial.Serial()
        port.port = self._config.port_name
        port.baudrate = self._config.baud_rate
        port.bytesize = getattr(serial, self._DATA_BITS_MAP[self._config.data_bits])
        port.stopbits = getattr(serial, self._STOP_BITS_MAP[self._config.stop_bits])
        port.parity = getattr(serial, self._PARITY_MAP[_coerce_parity(self._config.parity)])
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

    def _timeout_exit(self, received: int, size: int) -> None:
        """超时收尾:按已读字节数选择不断线超时或断线重同步(内部方法)。"""
        if received == 0:
            raise TransportTimeoutError(
                f"串口读取超时(receive_timeout={self._receive_timeout})",
                0,
            )
        self.close()
        raise TransportClosedError(
            f"串口读取超时且已收 {received}/{size} 字节(帧截断,残渣必致后续帧错位),"
            "已关闭串口,下次事务将重新打开以重新同步"
        )

    def _require_serial(self) -> Any:
        """取当前串口对象,未打开则抛出。"""
        if self._serial is None:
            raise TransportClosedError("串口未打开,请先调用 connect()")
        return self._serial


def _coerce_parity(value: Union[SerialParity, str]) -> SerialParity:
    """把枚举成员或字符串统一解析为 SerialParity(内部函数)。"""
    if isinstance(value, SerialParity):
        return value
    try:
        return SerialParity(str(value).strip().upper())
    except ValueError:
        raise ValueError(
            f"校验位必须是 SerialParity 枚举或 N/E/O,收到:{value!r}"
        )
