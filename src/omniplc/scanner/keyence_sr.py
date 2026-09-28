"""基恩士 SR 系列扫码枪客户端(TCP,用户模式)。

依据:基恩士 SR-2000 用户手册 Rev6.0(CN)「多站通讯网络/多个读取头」命令表
(PDF 页 56):读取开始 `LON`、指定库 `LON,b`(**b:01~16**)、读取结束 `LOFF`、
`BCLR`、`RESET`(另有 `KEYENCE` 版本查询 / `CANCEL` 未实现)。

协议为请求/应答式(TCP 默认端口 9004,命令以 CR 结束):

1. 发送 ``LON\\r`` 打开扫码窗口(激光开,可带 bank:``LON,01\\r``)
2. 等待扫码窗口时长(:attr:`scan_dwell`,读码在此窗口内发生)
3. 发送 ``LOFF\\r`` 关闭窗口——**应答在 LOFF 之后才发送**
4. 读取一行应答:条码文本 / ``ERROR``(未读到)/ ``OK``(无读出)

关键时序:应答在 LOFF 之后才发,LOFF 之前读会超时。

公共 API 沿用库约定:读码 ``scan()`` 返回 ``(是否读到条码, 条码文本)``,
失败原因记入 :attr:`last_error`;``reset()`` 返回 ``bool``。
"""
from __future__ import annotations

import socket
import time
from typing import Optional, Tuple

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    SR_BANK_MAX,
    SR_BANK_MIN,
    SR_CMD_BUFFER_CLEAR,
    SR_CMD_LOFF,
    SR_CMD_LON,
    SR_CMD_RESET,
    SR_DEFAULT_PORT,
    SR_DEFAULT_SCAN_DWELL,
    SR_DRAIN_TIMEOUT,
    SR_RECV_MAX,
    SR_RESP_ERROR,
    SR_RESP_OK,
)
from ..core.errors import DeviceError, ErrorCategory, OmniPLCInternalError, TransportTimeoutError
from ..transport import BaseTransport, TcpTransport
from ..core.types import DataType, PrimitiveValue
from ..core.i18n import _


class KeyenceSrClient(BaseClient):
    """基恩士 SR 系列扫码枪客户端(Ethernet 用户模式)。

    :example::

        client = KeyenceSrClient("192.168.0.10", 9004, scan_dwell=1.0)
        client.connect()
        ok, code = client.scan()          # (True, "ABC123") 或 (False, None)
        ok, code = client.scan(bank=1)    # 使用预设 bank 1
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = SR_DEFAULT_PORT,
        scan_dwell: float = SR_DEFAULT_SCAN_DWELL,
        encoding: str = "utf-8",
        encoding_errors: str = "strict",
    ) -> None:
        """初始化 SR 扫码枪客户端。

        :param ip_address: 扫码枪 IP 或主机名
        :param port: TCP 端口,默认 9004
        :param scan_dwell: 扫码窗口时长(秒),LON 到 LOFF 的等待时间
        :param encoding: 条码内容解码编码,默认 utf-8。QR/DataMatrix 可携带
            Shift-JIS/GB2312/任意二进制内容,按现场码制选择
        :param encoding_errors: 解码失败策略,默认 ``strict``——非法序列抛错
            并记 last_error(不静默以 U+FFFD 乱码当条码成功返回);
            确需容错可显式传 ``replace``
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__()
        self._ip_address = ip_address
        self._port = int(port)
        if scan_dwell <= 0:
            raise ValueError(_("scan_dwell 必须大于 0,收到:{}").format(scan_dwell))
        self._scan_dwell = float(scan_dwell)
        try:
            "".encode(encoding)
        except LookupError as exc:
            raise ValueError(_("encoding 非法:{!r}").format(encoding)) from exc
        self._encoding = encoding
        self._encoding_errors = encoding_errors

    @property
    def scan_dwell(self) -> float:
        """扫码窗口时长(秒),LON 开窗到 LOFF 关窗的等待时间(构造期定,只读)。"""
        return self._scan_dwell

    # ------------------------------------------------------------------
    # 扫码 API
    # ------------------------------------------------------------------

    def scan(self, bank: Optional[int] = None, timeout: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """触发一次扫码(事务模板内完成 LON → 窗口 → LOFF → 读应答)。

        扫码是**动作型**操作(触发一次激光读出),按写语义走事务模板——
        重试次数用 :attr:`write_retries`(默认 0),避免重试造成重复触发;
        事务计数、退避门控与错误分类与全库其他驱动一致。

        :param bank: 预设 bank 号(**1~16**;手册 LON,b 为 b:01~16),不同 bank
            存储不同的解码/曝光/对焦配置;``None`` 使用不带 bank 的 ``LON``
        :param timeout: LOFF 后等待应答的超时(秒);``None`` 用 :attr:`receive_timeout`
        :return: ``(是否读到条码, 条码文本)``;未读到/ERROR/超时/断线返回
            ``(False, None)``,原因记入 :attr:`last_error`
        :raises ValueError: bank 越界
        """
        if bank is not None and not SR_BANK_MIN <= int(bank) <= SR_BANK_MAX:
            raise ValueError(
                _("bank 必须在 {}~{} 之间,收到:{}").format(SR_BANK_MIN, SR_BANK_MAX, bank)
            )
        read_timeout = self._receive_timeout if timeout is None else float(timeout)
        if read_timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(read_timeout))
        ok, text = self._execute(
            lambda: self._scan_once(bank, read_timeout), is_write=True
        )
        if not ok or text is None:
            return False, None
        text = text.strip()
        if text == SR_RESP_ERROR or text == SR_RESP_OK or not text:
            # 链路成功但无读出:记错误分类但不计入 device_error_count
            # (扫码枪正常应答,不算设备返回错误码)
            message = (
                _("扫码枪返回 ERROR(未读到条码或距离过远)")
                if text == SR_RESP_ERROR
                else _("扫码枪无读出(OK)")
            )
            with self._lock:
                self._set_error(message, ErrorCategory.DEVICE, None)
            return False, None
        return True, text

    def _scan_once(self, bank: Optional[int], read_timeout: float) -> str:
        """LON → 扫码窗口 → LOFF → 读一行应答(内部方法,须事务内调用)。

        LOFF 之后才发应答,故读超时(LOFF 后 ``read_timeout`` 内无应答)时
        先尽力清掉半行残留,再抛 :class:`TransportTimeoutError`:0 字节
        可回退、链路无残渣,基类按"不断线"处理(与串口/UDP 超时同口径)。
        """
        transport = self._require_transport()
        lon = f"LON,{bank:02d}\r".encode("ascii") if bank is not None else SR_CMD_LON
        transport.send(lon)
        time.sleep(self._scan_dwell)
        transport.send(SR_CMD_LOFF)
        # 应答在 LOFF 之后才发送;临时收紧收包超时
        previous_timeout = transport.receive_timeout
        transport.receive_timeout = read_timeout
        try:
            line = self._read_line(transport, read_timeout)
            text = line.decode(self._encoding, errors=self._encoding_errors)
            if text.strip().upper().startswith("ER,"):
                # 命令错误应答 ``ER,<命令名称>,<错误代码>``(SR-2000 手册 Rev6.0 §12-1
                # 印刷页 76);不可当条码返回,按设备错误抛出(不断线)
                fields = text.strip().split(",")
                code_text = fields[2].strip() if len(fields) >= 3 else ""
                code = int(code_text) if code_text.isdigit() else 0
                raise DeviceError(
                    _("SR 命令错误应答:{}(错误代码 {})").format(
                        text.strip(), code_text or _("未知")
                    ),
                    code,
                )
            return text
        except socket.timeout:
            # 读码窗口内无应答:链路仍然完好,不断线
            self._drain_line(transport)
            raise TransportTimeoutError(
                _("扫码读超时({}s),未收到应答").format(read_timeout), 0
            )
        finally:
            transport.receive_timeout = previous_timeout

    def reset(self) -> bool:
        """清缓冲(BCLR)并复位扫码枪(RESET),两步都应答 OK 才算成功。

        复位是动作型命令,按写语义走事务模板(重试用 :attr:`write_retries`,
        默认 0,避免重复复位)。
        """
        def operation() -> bool:
            transport = self._require_transport()
            self._command_expect_ok(transport, SR_CMD_BUFFER_CLEAR)
            self._command_expect_ok(transport, SR_CMD_RESET)
            return True

        ok, _unused = self._execute(operation, is_write=True)
        return ok

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _read_line(transport: BaseTransport, read_timeout: float) -> bytes:
        """读取一行以 CR 结束的应答,整行受 ``read_timeout`` 总预算约束(内部方法)。"""
        chunks = []
        started = False
        previous_timeout = transport.receive_timeout
        deadline = time.monotonic() + read_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout(_("SR 收行超时({}s)").format(read_timeout))
                transport.receive_timeout = remaining
                byte = transport.recv(1)
                if byte == b"\r" or byte == b"\n":
                    if not started:
                        continue
                    break
                started = True
                chunks.append(byte)
                if len(chunks) > SR_RECV_MAX:
                    raise OmniPLCInternalError(_("SR 应答超过 {} 字节上限").format(SR_RECV_MAX))
        finally:
            transport.receive_timeout = previous_timeout
        return b"".join(chunks)

    def _drain_line(self, transport: BaseTransport) -> bool:
        """尽力读掉已到达的半行残留,防下一事务从流中间续读(内部方法)。

        超时此刻 LOFF 已发且无新应答,链路上只可能是旧残留;读到行尾
        即止,整体受 ``SR_DRAIN_TIMEOUT`` 预算约束。

        :return: 是否已读净——读到行尾或**一个字节都没读到**返回 ``True``
            (无残留);读到了字节却未达行尾返回 ``False``(留半行,调用方
            可据此判定残字节风险)
        """
        previous_timeout = transport.receive_timeout
        deadline = time.monotonic() + SR_DRAIN_TIMEOUT
        received = 0
        try:
            while received <= SR_RECV_MAX:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                transport.receive_timeout = remaining
                byte = transport.recv(1)
                received += 1
                if byte in (b"\r", b"\n"):
                    return True
        except socket.timeout:
            pass
        finally:
            transport.receive_timeout = previous_timeout
        return received == 0

    def _command_expect_ok(self, transport: BaseTransport, command: bytes) -> None:
        """发送命令并校验 OK 应答(内部方法)。"""
        transport.send(command)
        text = self._read_line(transport, self._receive_timeout).decode(
            self._encoding, errors=self._encoding_errors
        ).strip()
        if text != SR_RESP_OK:
            # code 0 = 无具体错误码(设备应答异常但链路正常,不断线)
            raise DeviceError(_("SR 命令 {} 应答异常:期望 OK,收到 {!r}").format(
                command.decode("ascii").rstrip("\r"), text
            ), 0)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    # ------------------------------------------------------------------
    # 扫码枪不支持 PLC 数据读写(基类抽象原语必须实现)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """SR 为触发式设备,不支持 PLC 数据读写(内部方法)。

        能力缺失按基类约定抛 :class:`DeviceError`(``code=0`` 无具体错误码),
        链路正常不断线——与 :meth:`_read_string` 的缺省实现同语义。
        """
        raise DeviceError(_("SR 扫码枪不支持数据读取,请使用 scan()"), 0)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """SR 为触发式设备,不支持 PLC 数据写入(内部方法),语义同 :meth:`_read`。"""
        raise DeviceError(_("SR 扫码枪不支持数据写入"), 0)
