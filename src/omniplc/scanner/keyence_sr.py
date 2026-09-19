"""基恩士 SR 系列扫码枪客户端(TCP,用户模式)。

协议为请求/应答式(TCP 默认端口 9004,命令以 CR 结束):

1. 发送 ``LON\\r`` 打开扫码窗口(激光开,可带 bank:``LON,01\\r``)
2. 等待扫码窗口时长(:attr:`scan_dwell`,读码在此窗口内发生)
3. 发送 ``LOFF\\r`` 关闭窗口——**应答在 LOFF 之后才发送**
4. 读取一行应答:条码文本 / ``ERROR``(未读到)/ ``OK``(无读出)

关键时序:应答在 LOFF 之后才发,LOFF 之前读会超时(参考本地
vention_barcode_scanner 库的 SR 驱动口径)。

公共 API 沿用库约定:读码 ``scan()`` 返回 ``(是否读到条码, 条码文本)``,
失败原因记入 :attr:`last_error`;``reset()`` 返回 ``bool``。
"""
from __future__ import annotations

import socket
import time
from typing import Optional, Tuple

from ..core.base_client import BaseClient, _describe, validate_endpoint
from ..core.constants import (
    SR_BANK_MAX,
    SR_CMD_BUFFER_CLEAR,
    SR_CMD_LOFF,
    SR_CMD_LON,
    SR_CMD_RESET,
    SR_DEFAULT_PORT,
    SR_DEFAULT_SCAN_DWELL,
    SR_RECV_MAX,
    SR_RESP_ERROR,
    SR_RESP_OK,
)
from ..core.errors import DeviceError, OmniPLCInternalError
from ..transport import BaseTransport, TcpTransport
from ..types import DataType, PrimitiveValue


class KeyenceSrClient(BaseClient):
    """基恩士 SR 系列扫码枪客户端(Ethernet 用户模式)。

    :example::

        client = KeyenceSrClient("192.168.0.10", 9004)
        client.scan_dwell = 1.0
        client.connect()
        ok, code = client.scan()          # (True, "ABC123") 或 (False, None)
        ok, code = client.scan(bank=1)    # 使用预设 bank 1
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = SR_DEFAULT_PORT,
        scan_dwell: float = SR_DEFAULT_SCAN_DWELL,
    ) -> None:
        """初始化 SR 扫码枪客户端。

        :param ip_address: 扫码枪 IP 或主机名
        :param port: TCP 端口,默认 9004
        :param scan_dwell: 扫码窗口时长(秒),LON 到 LOFF 的等待时间
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__()
        self._ip_address = ip_address
        self._port = int(port)
        self.scan_dwell = scan_dwell

    @property
    def scan_dwell(self) -> float:
        """扫码窗口时长(秒),LON 开窗到 LOFF 关窗的等待时间。"""
        return self._scan_dwell

    @scan_dwell.setter
    def scan_dwell(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("scan_dwell 必须大于 0,收到:{}".format(seconds))
        self._scan_dwell = float(seconds)

    # ------------------------------------------------------------------
    # 扫码 API
    # ------------------------------------------------------------------

    def scan(self, bank: Optional[int] = None, timeout: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """触发一次扫码(锁内完成 LON → 窗口 → LOFF → 读应答)。

        :param bank: 预设 bank 号(0~15),不同 bank 存储不同的解码/曝光/对焦配置;
            ``None`` 使用扫码枪当前 bank
        :param timeout: LOFF 后等待应答的超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否读到条码, 条码文本)``;未读到/ERROR/超时/断线返回
            ``(False, None)``,原因记入 :attr:`last_error`
        :raises ValueError: bank 越界
        """
        if bank is not None and not 0 <= int(bank) <= SR_BANK_MAX:
            raise ValueError("bank 必须在 0~{} 之间,收到:{}".format(SR_BANK_MAX, bank))
        read_timeout = self._receive_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError("timeout 必须大于 0,收到:{}".format(read_timeout))
        with self._lock:
            if not self._connected and not self.connect():
                return False, None  # connect() 已记录 last_error
            transport = self._require_transport()
            try:
                lon = "LON,{:02d}\r".format(bank).encode("ascii") if bank is not None else SR_CMD_LON
                transport.send(lon)
                time.sleep(self._scan_dwell)
                transport.send(SR_CMD_LOFF)
                # 应答在 LOFF 之后才发送;临时收紧收包超时
                previous_timeout = transport.receive_timeout
                transport.receive_timeout = read_timeout
                try:
                    text = self._read_line(transport)
                finally:
                    transport.receive_timeout = previous_timeout
            except socket.timeout:
                # 读码窗口内无应答:链路仍然完好,不断线
                self._last_error = "扫码读超时({}s),未收到应答".format(read_timeout)
                return False, None
            except (OSError, OmniPLCInternalError) as exc:
                self._last_error = _describe(exc)
                self._mark_disconnected()
                return False, None
            text = text.strip()
            if text == SR_RESP_ERROR:
                self._last_error = "扫码枪返回 ERROR(未读到条码或距离过远)"
                return False, None
            if text == SR_RESP_OK or not text:
                self._last_error = "扫码枪无读出(OK)"
                return False, None
            self._last_error = None
            return True, text

    def reset(self) -> bool:
        """清缓冲(BCLR)并复位扫码枪(RESET),两步都应答 OK 才算成功。"""
        def operation() -> bool:
            transport = self._require_transport()
            self._command_expect_ok(transport, SR_CMD_BUFFER_CLEAR)
            self._command_expect_ok(transport, SR_CMD_RESET)
            return True

        ok, _ = self._execute(operation)
        return ok

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _read_line(self, transport: BaseTransport) -> str:
        """读取一行以 CR 结束的应答(内部方法)。"""
        chunks = []
        started = False
        while True:
            byte = transport.recv(1)
            if byte == b"\r" or byte == b"\n":
                if not started:
                    continue
                break
            started = True
            chunks.append(byte)
            if len(chunks) > SR_RECV_MAX:
                raise OmniPLCInternalError("SR 应答超过 {} 字节上限".format(SR_RECV_MAX))
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _command_expect_ok(self, transport: BaseTransport, command: bytes) -> None:
        """发送命令并校验 OK 应答(内部方法)。"""
        transport.send(command)
        text = self._read_line(transport).strip()
        if text != SR_RESP_OK:
            raise DeviceError("SR 命令 {} 应答异常:期望 OK,收到 {!r}".format(
                command.decode("ascii").rstrip("\r"), text
            ), 0)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    # ------------------------------------------------------------------
    # 扫码枪不支持 PLC 数据读写(基类抽象原语必须实现)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """SR 为触发式设备,不支持 PLC 数据读写(内部方法)。"""
        raise OmniPLCInternalError("SR 扫码枪不支持数据读取,请使用 scan()")

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """SR 为触发式设备,不支持 PLC 数据写入(内部方法)。"""
        raise OmniPLCInternalError("SR 扫码枪不支持数据写入")
