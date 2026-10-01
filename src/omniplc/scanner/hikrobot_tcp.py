"""海康机器人 ID 系列读码器客户端(TCP 命令协议)。

依据:
- 《工业读码器通信指令操作手册》V1.0.3(随 IDMVS 客户端分发:
  ``Applications/common/doc/CommunicationCommand_Chinese.pdf``)——命令面:
  §1.3 指令格式(印刷页 1-2)、§2.1 TCP 通信方式(印刷页 3,读码器作 TCP
  服务端、端口在「通信命令控制」模块配置)、§3 通信指令列表(印刷页 7-12,
  含 ``<Exec,TriSoft>`` 软件触发印刷页 9)、错误码表(印刷页 12-13)。
- 《极小型智能读码器用户手册》V1.7.0 / 《超小型智能读码器用户手册》V1.1.2
  ——结果面:TCP 触发端口默认 2001(超小型印刷页 45-47)、TCP Server 结果
  输出(极小型 §8.6.5 印刷页 137)、**输出格式化标志符表**(极小型印刷页 150:
  ``<code_content>``/``<code_type>``/``<code_quality>``/``<code_cen_pos>``/
  ``<trigger_num>`` 等 16 项)、「输出无读」默认 ``NoRead``(印刷页 150)。

**双通道架构**(手册明确两端口不得相同——极小型手册印刷页 122 与通信指令
手册 §2.1 印刷页 3):

- **命令通道**(构造参数 ``command_port``):读码器作 TCP 服务端,本库发送
  ``<Get/Set/Exec,cmdStr[,param]>``、接收 ``<...,...,OK/errno>`` 角括号应答。
  软触发走 ``<Exec,TriSoft>``,采集控制走 ``<Set,Acq,0/1>``,全量参数命令经
  :meth:`command` 低阶入口透传(指令列表见手册 §3)。
- **结果通道**(构造参数 ``result_port``):读码器作 TCP 服务端(IDMVS
  「通信配置 > TCP 服务器」)主动推送结果报文,本库接收。报文无固定帧界
  (内容由读码器侧「输出格式化」模板决定),按**静默间隔**成帧
  (:attr:`settle_interval`),元数据随模板输出透传。

前置配置(IDMVS):运行模式 normal/工作模式;触发模式开启、触发源选软触发;
「通信命令控制」开启 TCP 命令并设端口;「通信配置」选 TCP 服务器并设端口;
**开始采集**(IDMVS 工具栏或 ``<Set,Acq,1>``)——未开始采集时触发无结果。

公共 API 沿用扫码枪家族约定::meth:`scan` 返回 ``(是否读到条码, 条码文本)``,
未读到(NoRead 文本)/超时/断线/命令出错都返回 ``(False, None)``,原因记入
:attr:`last_error`——与 :class:`~omniplc.KeyenceSrClient` 同口径;结果文本为
读码器格式化模板的原文(含元数据字段时由调用方按模板解析)。
"""
from __future__ import annotations

import socket
import time
from typing import Optional, Tuple

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    HIKROBOT_CMD_ERRNO_TEXT,
    HIKROBOT_CMD_REPLY_MAX,
    HIKROBOT_NOREAD_TEXT,
    HIKROBOT_RESULT_MAX_FRAME,
    HIKROBOT_RESULT_SETTLE_INTERVAL,
)
from ..core.debug import format_hex
from ..core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
    TransportTimeoutError,
)
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue
from ..transport import BaseTransport

__all__ = ["HikrobotIdTcpClient"]


def _open_connection(host: str, port: int, timeout: float) -> socket.socket:
    """建立一条到读码器的 TCP 连接(模块级工厂,测试替换点)。"""
    return socket.create_connection((host, port), timeout=timeout)


class _HikrobotIdSession(BaseTransport):
    """读码器双通道会话(命令通道收发 + 结果通道接收,内部传输适配)。

    命令与结果走两条独立 TCP 连接(读码器两端口),:meth:`send`/:meth:`recv`
    基类契约映射为命令发送/结果接收;命令应答经 :meth:`recv_command_reply`
    在命令连接上收取。
    """

    def __init__(
        self,
        host: str,
        command_port: int,
        result_port: Optional[int],
    ) -> None:
        super().__init__()
        self._host = host
        self._command_port = int(command_port)
        self._result_port = None if result_port is None else int(result_port)
        self._command_sock: Optional[socket.socket] = None
        self._result_sock: Optional[socket.socket] = None

    # ------------------------------------------------------------------
    # BaseTransport 契约
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """建立命令与结果两条 TCP 连接(结果通道未配置时只连命令通道)。

        :raises OSError: 任一连接失败
        """
        self.close()
        self._command_sock = _open_connection(
            self._host, self._command_port, self._connect_timeout
        )
        try:
            if self._result_port is not None:
                self._result_sock = _open_connection(
                    self._host, self._result_port, self._connect_timeout
                )
        except OSError:
            self._command_sock.close()
            self._command_sock = None
            raise

    def close(self) -> None:
        """关闭两条连接,重复调用幂等。"""
        for name in ("_command_sock", "_result_sock"):
            sock = getattr(self, name)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, name, None)

    def send(self, data: bytes) -> None:
        """发送命令字节(命令通道)。"""
        sock = self._command_sock
        if sock is None:
            raise TransportClosedError(_("命令通道未连接,请先调用 connect()"))
        sock.sendall(data)

    def recv(self, size: int) -> bytes:
        """接收结果报文片段(结果通道)。"""
        sock = self._result_sock
        if sock is None:
            raise TransportClosedError(
                _("结果通道未配置(result_port=None),无法接收读码结果")
            )
        sock.settimeout(self._receive_timeout)
        data = sock.recv(size)
        if not data:
            # 对端关闭:连接失效,按断线处理走惰性重连
            raise ConnectionError(_("结果通道被对端关闭"))
        return data

    # ------------------------------------------------------------------
    # 通道专用读写(命令应答 / 结果成帧)
    # ------------------------------------------------------------------

    def recv_command_reply(self, timeout: float) -> bytes:
        """在命令通道上读取一条 ``<...>`` 角括号应答(内部方法)。

        逐字节读到 ``>`` 为止(应答自括号定界),整体受 ``timeout`` 预算约束;
        超时抛 :class:`socket.timeout`(链路仍完好,由调用方转译)。

        :raises ProtocolFrameError: 超过 :data:`HIKROBOT_CMD_REPLY_MAX` 上限
        """
        sock = self._command_sock
        if sock is None:
            raise TransportClosedError(_("命令通道未连接,请先调用 connect()"))
        chunks: list = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(_("命令应答读取超时({}s)").format(timeout))
            sock.settimeout(remaining)
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError(_("命令通道被对端关闭"))
            chunks.append(byte)
            if byte == b">":
                break
            if len(chunks) > HIKROBOT_CMD_REPLY_MAX:
                raise ProtocolFrameError(
                    _("命令应答超过 {} 字节上限:{}").format(
                        HIKROBOT_CMD_REPLY_MAX, format_hex(b"".join(chunks))
                    )
                )
        return b"".join(chunks)

    def recv_result(self, timeout: float, settle: float, max_frame: int) -> bytes:
        """在结果通道上按静默间隔收取一帧推送报文(内部方法)。

        首字节在 ``timeout`` 预算内等待;收到数据后,静默 ``settle`` 秒无新
        数据即判一帧结束(多码/前后缀/元数据模板均透传,由调用方解析)。

        :raises socket.timeout: ``timeout`` 内无任何数据
        :raises OmniPLCInternalError: 超过 ``max_frame`` 上限
        """
        sock = self._result_sock
        if sock is None:
            raise TransportClosedError(
                _("结果通道未配置(result_port=None),无法接收读码结果")
            )
        chunks: list = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(_("读码结果等待超时({}s)").format(timeout))
            # 首字节前等满剩余预算;已有数据后只等一个静默间隔
            sock.settimeout(remaining if not chunks else min(settle, remaining))
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if chunks:
                    return b"".join(chunks)
                # 尚无数据:假传输会立即超时,微睡眠防空转后继续等
                time.sleep(0.001)
                continue
            if not data:
                raise ConnectionError(_("结果通道被对端关闭"))
            chunks.append(data)
            if sum(len(chunk) for chunk in chunks) > max_frame:
                raise OmniPLCInternalError(
                    _("读码结果超过 {} 字节上限:{}").format(
                        max_frame, format_hex(b"".join(chunks))
                    )
                )


class HikrobotIdTcpClient(BaseClient):
    """海康机器人 ID 系列读码器客户端(TCP 命令协议,双通道)。

    :example::

        client = HikrobotIdTcpClient("192.168.0.10", command_port=9989, result_port=9988)
        client.connect()
        client.set_acquisition(True)   # 开始采集(或 IDMVS 工具栏点开始采集)
        ok, code = client.scan()       # <Exec,TriSoft> → 等结果推送 → (True, "ABC123")
        ok, mode = client.get_acquisition()
        client.close()

    现场为 IO 硬触发时可不调用 :meth:`scan`,只经 :meth:`read_result`
    被动接收下一帧推送结果。
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        command_port: int = 9989,
        result_port: Optional[int] = None,
        *,
        encoding: str = "utf-8",
        encoding_errors: str = "strict",
        noread_text: str = HIKROBOT_NOREAD_TEXT,
        settle_interval: float = HIKROBOT_RESULT_SETTLE_INTERVAL,
        max_frame: int = HIKROBOT_RESULT_MAX_FRAME,
    ) -> None:
        """初始化读码器 TCP 命令客户端。

        :param ip_address: 读码器 IP 或主机名
        :param command_port: 命令通道端口(IDMVS「通信命令控制」模块配置;
            手册未记载出厂默认,须与现场配置一致)
        :param result_port: 结果通道端口(IDMVS「通信配置 > TCP 服务器」配置,
            不得与命令端口相同);``None`` = 不开结果通道(仅命令控制,
            :meth:`scan`/:meth:`read_result` 不可用)
        :param encoding: 结果报文解码编码,默认 utf-8(QR/DataMatrix 可携带
            GB2312/任意二进制内容,按现场码制选择)
        :param encoding_errors: 解码失败策略,默认 ``strict``——非法序列抛错
            并记 last_error(不静默以 U+FFFD 乱码当条码成功返回)
        :param noread_text: 未读到码的输出文本,默认 ``NoRead``(手册默认值;
            现场改过「输出无读」参数时同步修改)
        :param settle_interval: 结果成帧静默间隔(秒),过小可能截断多帧连发
        :param max_frame: 结果报文字节上限
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, command_port)
        super().__init__()
        self._ip_address = ip_address
        self._command_port = int(command_port)
        if result_port is not None:
            if int(result_port) == int(command_port):
                raise ValueError(
                    _("结果端口不得与命令端口相同(手册要求两端口不重复),收到:{}").format(
                        result_port
                    )
                )
            validate_endpoint(ip_address, int(result_port))
        self._result_port = None if result_port is None else int(result_port)
        try:
            "".encode(encoding)
        except LookupError as exc:
            raise ValueError(_("encoding 非法:{!r}").format(encoding)) from exc
        self._encoding = encoding
        self._encoding_errors = encoding_errors
        if not noread_text:
            raise ValueError(_("noread_text 不能为空"))
        self._noread_text = str(noread_text)
        if settle_interval <= 0:
            raise ValueError(
                _("settle_interval 必须大于 0,收到:{}").format(settle_interval)
            )
        self._settle_interval = float(settle_interval)
        if max_frame <= 0:
            raise ValueError(_("max_frame 必须大于 0,收到:{}").format(max_frame))
        self._max_frame = int(max_frame)

    # ------------------------------------------------------------------
    # 读码 API
    # ------------------------------------------------------------------

    def scan(self, timeout: float = 10.0) -> Tuple[bool, Optional[str]]:
        """软触发一次读码并等待结果推送(命令应答 + 结果接收)。

        时序:发送 ``<Exec,TriSoft>`` → 等命令应答 OK → 在结果通道等待下一帧
        推送报文(静默成帧)→ 解码。触发是**动作型**操作,按写语义走事务
        模板(重试用 :attr:`write_retries`,默认 0,避免重复触发)。

        前置:读码器已开始采集(IDMVS 工具栏或 :meth:`set_acquisition(True)`),
        且触发模式开启、触发源为软触发。

        :param timeout: 等待命令应答与结果推送的总超时(秒)
        :return: ``(是否读到条码, 结果文本)``。结果文本为读码器「输出格式化」
            模板原文(配置了元数据占位符时含元数据字段,由调用方按模板解析);
            NoRead 文本/超时/断线/命令出错返回 ``(False, None)``,原因记入
            :attr:`last_error`
        :raises ValueError: 结果通道未配置或参数非法
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        if self._result_port is None:
            raise ValueError(
                _("结果通道未配置(result_port=None),无法接收读码结果")
            )
        ok, text = self._execute(lambda: self._scan_once(float(timeout)), is_write=True)
        if not ok or text is None:
            return False, None
        text = text.rstrip("\r\n")
        if text == self._noread_text:
            # 链路成功但未读到码:读码器正常应答,不算设备错误码
            with self._lock:
                self._set_error(
                    _("读码器无读出({})").format(self._noread_text),
                    ErrorCategory.DEVICE,
                    None,
                )
            return False, None
        return True, text

    def read_result(self, timeout: float = 10.0) -> Tuple[bool, Optional[str]]:
        """被动等待下一帧结果推送(不触发,供 IO 硬触发等外部触发场景)。

        :param timeout: 等待推送的总超时(秒)
        :return: 语义同 :meth:`scan`
        :raises ValueError: 结果通道未配置或参数非法
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        if self._result_port is None:
            raise ValueError(
                _("结果通道未配置(result_port=None),无法接收读码结果")
            )
        ok, text = self._execute(
            lambda: self._receive_once(float(timeout)), is_write=True
        )
        if not ok or text is None:
            return False, None
        text = text.rstrip("\r\n")
        if text == self._noread_text:
            with self._lock:
                self._set_error(
                    _("读码器无读出({})").format(self._noread_text),
                    ErrorCategory.DEVICE,
                    None,
                )
            return False, None
        return True, text

    def trigger(self) -> bool:
        """仅发送软触发命令 ``<Exec,TriSoft>``,不等待结果(动作型命令)。

        结果可经 :meth:`read_result` 或现场其他通道接收;命令出错记
        :attr:`last_error` 返回 ``False``。
        """
        ok, _unused = self._execute(
            lambda: self._command_exchange("Exec", "TriSoft"), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 采集控制与命令面
    # ------------------------------------------------------------------

    def set_acquisition(self, enabled: bool) -> bool:
        """设置采集状态 ``<Set,Acq,0/1>``(0=停止采集,1=开始采集)。"""
        ok, _unused = self._execute(
            lambda: self._command_exchange(
                "Set", "Acq", "1" if enabled else "0"
            ),
            is_write=True,
        )
        return ok

    def get_acquisition(self) -> Tuple[bool, Optional[int]]:
        """查询采集状态 ``<Get,Acq>``(0=停止,1=采图中,2=触发等待)。"""
        ok, value = self._execute(lambda: self._command_exchange("Get", "Acq"))
        if not ok or value is None:
            return False, None
        return True, int(value)

    def command(
        self, cmd_type: str, cmd: str, param: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """低阶命令入口:透传手册 §3 指令列表中的任意命令。

        :param cmd_type: 命令类型,``"Get"``/``"Set"``/``"Exec"``
        :param cmd: 命令字符串(如 ``"RunMode"``/``"1DNum"``/``"Reboot"``)
        :param param: 参数(Get 不传;Set 必传;Exec 不传)
        :return: ``(是否成功, 应答参数)``——Set/Exec 成功应答为 OK,应答参数
            为 ``None``;Get 成功返回参数文本;errno 应答记 :attr:`last_error`
            (``last_error_code`` = 负 errno)返回 ``(False, None)``
        :raises ValueError: 命令类型/参数组合非法
        """
        if cmd_type not in ("Get", "Set", "Exec"):
            raise ValueError(
                _("cmd_type 必须是 Get/Set/Exec 之一,收到:{!r}").format(cmd_type)
            )
        if cmd_type == "Set" and param is None:
            raise ValueError(_("Set 命令必须携带参数"))
        if cmd_type in ("Get", "Exec") and param is not None:
            raise ValueError(_("{} 命令不携带参数,收到:{!r}").format(cmd_type, param))
        return self._execute(lambda: self._command_exchange(cmd_type, cmd, param))

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _scan_once(self, timeout: float) -> str:
        """TriSoft → 等结果推送(内部方法,须事务内调用)。"""
        self._command_exchange("Exec", "TriSoft")
        return self._receive_once(timeout)

    def _receive_once(self, timeout: float) -> str:
        """等待一帧结果推送并解码(内部方法,须事务内调用)。"""
        session = self._require_transport()
        if not isinstance(session, _HikrobotIdSession):
            raise OmniPLCInternalError(
                _("传输对象不是读码器会话:{!r}").format(type(session).__name__)
            )
        try:
            raw = session.recv_result(timeout, self._settle_interval, self._max_frame)
        except socket.timeout:
            # 结果通道无推送:链路仍完好,不断线(与 SR 读超时同口径)
            raise TransportTimeoutError(
                _("读码结果等待超时({}s),结果通道无数据").format(timeout), 0
            )
        return raw.decode(self._encoding, errors=self._encoding_errors)

    def _command_exchange(
        self, cmd_type: str, cmd: str, param: Optional[str] = None
    ) -> Optional[str]:
        """发送命令并校验应答回显(内部方法,须事务内调用)。

        应答契约(通信指令手册 §1.3 印刷页 1-2):
        ``<Get,cmdStr,param/errno>`` / ``<Set,cmdStr,OK/errno>`` /
        ``<Exec,cmdStr,OK/errno>``;payload 为 ``OK`` 或 ``0`` 视为成功,
        负整数按错误码表(印刷页 12-13)转译为 :class:`DeviceError`。

        :return: Get 命令返回参数文本;Set/Exec 成功返回 ``None``
        :raises ProtocolFrameError: 应答非 ``<...>`` 形式或回显不符(带原始帧)
        :raises DeviceError: 设备返回 errno / invalid
        :raises TransportTimeoutError: 命令应答超时
        """
        session = self._require_transport()
        if not isinstance(session, _HikrobotIdSession):
            raise OmniPLCInternalError(
                _("传输对象不是读码器会话:{!r}").format(type(session).__name__)
            )
        if param is None:
            payload = "<{},{}>".format(cmd_type, cmd)
        else:
            payload = "<{},{},{}>".format(cmd_type, cmd, param)
        session.send(payload.encode("ascii"))
        try:
            reply = session.recv_command_reply(self._receive_timeout)
        except socket.timeout:
            raise TransportTimeoutError(
                _("命令 {} 应答超时({}s)").format(payload, self._receive_timeout), 0
            )
        text = reply.decode("ascii", errors="replace")
        if not (text.startswith("<") and text.endswith(">")):
            raise ProtocolFrameError(
                _("命令应答帧非法:期望 <...> 形式,收到 {!r}(原始帧:{})").format(
                    text, format_hex(reply)
                )
            )
        fields = text[1:-1].split(",", 2)
        if len(fields) != 3:
            raise ProtocolFrameError(
                _("命令应答字段数不符:期望 3 段,收到 {!r}(原始帧:{})").format(
                    text, format_hex(reply)
                )
            )
        reply_type, reply_cmd, reply_payload = fields
        if reply_type != cmd_type or reply_cmd != cmd:
            raise ProtocolFrameError(
                _(
                    "命令应答回显不符:期望 {}/{} 收到 {}/{}(原始帧:{})"
                ).format(cmd_type, cmd, reply_type, reply_cmd, format_hex(reply))
            )
        if reply_payload == "OK":
            return None
        if "invalid" in reply_payload:
            # 指令不符合通用格式(手册 §1.3 印刷页 2:返回 invalid)
            raise DeviceError(
                _("命令被设备拒绝(invalid):{}").format(text), 0
            )
        try:
            errno = int(reply_payload)
        except ValueError:
            # 非数字:Get 的参数文本(如触发文本)原样返回
            return reply_payload
        if errno < 0:
            # 负数 = 错误码表(印刷页 12-13);Get 参数恰为负数文本的场景手册
            # 未给类型标注,按 errno 解读(极端场景,见 docstring 注意)
            raise DeviceError(
                _("命令 {} 执行失败 errno {}({})").format(
                    text, errno, HIKROBOT_CMD_ERRNO_TEXT.get(errno, _("未知"))
                ),
                errno,
            )
        if errno == 0:
            # errno 0 = 指令执行成功(错误码表 EXT_CMD_ERR_OK=0)
            return None
        # 正整数:Get 的参数值(如 Acq=1、RunMode=2)
        return reply_payload

    def _create_transport(self) -> BaseTransport:
        return _HikrobotIdSession(
            self._ip_address, self._command_port, self._result_port
        )

    # ------------------------------------------------------------------
    # 读码器不支持 PLC 数据读写(基类抽象原语必须实现)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读码器为触发式设备,不支持 PLC 数据读取(内部方法)。

        能力缺失按基类约定抛 :class:`DeviceError`(``code=0`` 无具体错误码),
        链路正常不断线。
        """
        raise DeviceError(_("读码器不支持数据读取,请使用 scan()"), 0)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """读码器为触发式设备,不支持 PLC 数据写入(内部方法),语义同 :meth:`_read`。"""
        raise DeviceError(_("读码器不支持数据写入"), 0)
