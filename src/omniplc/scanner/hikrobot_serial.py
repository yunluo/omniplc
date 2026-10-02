"""海康机器人 ID 系列读码器客户端(RS-232 串口)。

依据:
- 《超小型智能读码器用户手册》V1.1.2 §4.6.1 触发源=串口触发(印刷页 45:
  串口开始触发文本默认 ``start``)、§4.6.2 串口停止触发(印刷页 47:命令
  字符串默认 ``stop``)、§4.7.3 串口通讯协议(印刷页 52:RS-232 结果输出,
  波特率/数据位/校验位/停止位可配)
- 《极小型智能读码器用户手册》V1.7.0 §8.6.3 Serial 方式(印刷页 134-135:
  串口数据位 7/8、8 位才支持十六进制触发)、「输出条形码换行符使能」
  (印刷页 151)
- 《工业读码器通信指令操作手册》V1.0.3 §2.3 Serial 通信方式(印刷页 5)+
  TriSeriStart/TriSeriStop 触发文本 1~31 字符且**长度不可相等**(印刷页 9)

**单串口角色**(设备仅一路 RS-232,工业协议手册 §3 印刷页 29):串口触发、
串口结果输出共用同一物理口——本类按「串口触发 + 串口输出」一体的现场口径
设计:发送开始触发文本(默认 ``start``)→ 读码器出图解码 → 结果以**行**
(RF/LF 结尾,须读码器侧开启「输出条形码换行符使能」)推回 → 发送停止触发
文本(默认 ``stop``)结束扫描窗。

时序与基恩士 :class:`~omniplc.KeyenceSrClient` 的差异:SR 的应答在关窗
(LOFF)**之后**才发;海康串口结果在扫描窗**内**即时流出——故本类先读结果
行再发停止文本,避免停窗截断解码。

公共 API 沿用扫码枪家族约定::meth:`scan` 返回 ``(是否读到条码, 条码文本)``
(文本为「输出格式化」模板原文,元数据占位符随文透传);NoRead 文本/超时/
断线返回 ``(False, None)``,原因记入 :attr:`last_error`。多码场景用
:meth:`trigger` → ``read_result``×N → :meth:`stop` 手工编排。

串口命令协议(「通信命令控制」选 Serial,``<Exec,TriSoft>`` 等命令帧走
串口)本类不做——文本触发已覆盖,命令帧与文本混流需按现场角色二选一。
"""
from __future__ import annotations

import socket
import time
from typing import Optional, Tuple, Union

from ..core.base_client import BaseClient
from ..core.constants import (
    HIKROBOT_NOREAD_TEXT,
    HIKROBOT_SERIAL_BAUD_DEFAULT,
    HIKROBOT_SERIAL_DRAIN_TIMEOUT,
    HIKROBOT_SERIAL_RECV_MAX,
    HIKROBOT_SERIAL_STOP_TEXT,
    HIKROBOT_SERIAL_TRIGGER_MAX_LEN,
    HIKROBOT_SERIAL_TRIGGER_TEXT,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
)
from ..core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    TransportClosedError,
    TransportTimeoutError,
)
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue, SerialParity
from ..transport import BaseTransport, SerialConfig, SerialTransport


class HikrobotIdSerialClient(BaseClient):
    """海康机器人 ID 系列读码器客户端(RS-232 串口触发 + 串口输出)。

    :example::

        client = HikrobotIdSerialClient()
        client.configure_serial("COM3", 115200)   # 与读码器串口配置一致
        client.connect()
        ok, code = client.scan()                  # start → 结果行 → stop
        client.close()

    前置(IDMVS):触发模式开启、触发源=串口触发(开始/停止文本与本类构造
    参数一致)、「通信配置 > 串口通讯协议」使能且**输出条形码换行符使能**
    开启(结果行以 CR/LF 结尾)、「输出无读」文本与 ``noread_text`` 一致。
    """

    def __init__(
        self,
        trigger_text: str = HIKROBOT_SERIAL_TRIGGER_TEXT,
        stop_text: str = HIKROBOT_SERIAL_STOP_TEXT,
        *,
        encoding: str = "utf-8",
        encoding_errors: str = "strict",
        noread_text: str = HIKROBOT_NOREAD_TEXT,
    ) -> None:
        """初始化串口读码器客户端。

        :param trigger_text: 串口开始触发文本,默认 ``start``(手册默认值;
            须与读码器「串口开始触发文本」配置一致,1~31 字符)
        :param stop_text: 串口停止触发文本,默认 ``stop``(与开始文本**长度
            不可相等**,通信指令手册印刷页 9)
        :param encoding: 条码内容解码编码,默认 utf-8(QR/DataMatrix 可携带
            GB2312/任意二进制内容,按现场码制选择)
        :param encoding_errors: 解码失败策略,默认 ``strict``——非法序列抛错
            并记 last_error(不静默以 U+FFFD 乱码当条码成功返回)
        :param noread_text: 未读到码的输出文本,默认 ``NoRead``(手册默认值;
            现场改过「输出无读」参数时同步修改)
        :raises ValueError: 参数非法
        """
        super().__init__()
        self._trigger_text = self._check_text("trigger_text", trigger_text)
        self._stop_text = self._check_text("stop_text", stop_text)
        if len(self._trigger_text) == len(self._stop_text):
            raise ValueError(
                _("触发文本与停止文本长度不可相等(通信指令手册印刷页 9),收到:"
                  "{!r}/{!r}").format(trigger_text, stop_text)
            )
        try:
            "".encode(encoding)
        except LookupError as exc:
            raise ValueError(_("encoding 非法:{!r}").format(encoding)) from exc
        self._encoding = encoding
        self._encoding_errors = encoding_errors
        if not noread_text:
            raise ValueError(_("noread_text 不能为空"))
        self._noread_text = str(noread_text)
        self._serial_config: Optional[SerialConfig] = None

    @staticmethod
    def _check_text(name: str, value: str) -> str:
        """触发/停止文本校验(内部方法):非空、1~31 字符且 ASCII。

        ASCII 校验前移到构造期:文本触发协议为 ASCII 口径,非 ASCII 文本
        若拖到事务期 ``encode("ascii")`` 才抛 UnicodeEncodeError,会直接
        逃逸事务模板且不记 last_error(审查 1001 P3-⑭)。
        """
        text = str(value)
        if not 1 <= len(text) <= HIKROBOT_SERIAL_TRIGGER_MAX_LEN:
            raise ValueError(
                _("{} 长度必须在 1~{} 之间,收到:{!r}").format(
                    name, HIKROBOT_SERIAL_TRIGGER_MAX_LEN, value
                )
            )
        try:
            text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                _("{} 必须为 ASCII(串口文本触发协议口径),收到:{!r}").format(name, value)
            ) from exc
        return text

    # ------------------------------------------------------------------
    # 串口配置(库惯例:configure_serial,须在 connect 之前)
    # ------------------------------------------------------------------

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = HIKROBOT_SERIAL_BAUD_DEFAULT,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(必须在 connect 之前调用)。

        波特率/数据位/校验位/停止位须与读码器「串口通讯协议」「串口触发」
        配置一致;读码器可选项 4800~115200(通信指令手册 TriSeriBaud,
        印刷页 8)。**出厂默认 9600**(超小型手册 §4.6.2 印刷页 48 原文:
        「串口波特率:设置串口波特率,默认为9600」——审查 1001 P3-⑩ 订正
        原"手册未载"表述);本库缺省取 115200 为常用现场值取舍,不一致时
        以现场 IDMVS 配置为准并在此显式传入。

        :param port_name: 串口名,如 ``"COM3"``
        :param baud_rate: 波特率,默认 115200
        :param data_bits: 数据位 5~8(手册:7/8,8 位才支持十六进制触发)
        :param stop_bits: 停止位 1/1.5/2
        :param parity: 校验位,推荐 :class:`~omniplc.types.SerialParity` 枚举,
            也兼容 ``"N"``/``"E"``/``"O"`` 字符串
        :raises ValueError: 参数非法
        """
        self._serial_config = SerialConfig(
            port_name=port_name,
            baud_rate=baud_rate,
            data_bits=data_bits,
            stop_bits=stop_bits,
            parity=parity,
        )

    @property
    def trigger_text(self) -> str:
        """串口开始触发文本(构造期定,只读)。"""
        return self._trigger_text

    @property
    def stop_text(self) -> str:
        """串口停止触发文本(构造期定,只读)。"""
        return self._stop_text

    # ------------------------------------------------------------------
    # 读码 API
    # ------------------------------------------------------------------

    def scan(self, timeout: float = 10.0) -> Tuple[bool, Optional[str]]:
        """串口触发一次读码并读取结果行(start → 结果行 → stop)。

        触发是**动作型**操作,按写语义走事务模板(重试用 :attr:`write_retries`,
        默认 0,避免重复触发)。结果行须以 CR/LF 结尾(读码器「输出条形码
        换行符使能」);超时同样发送停止文本收窗(尽力),防扫描窗常开。

        :param timeout: 等待结果行的总超时(秒)
        :return: ``(是否读到条码, 条码文本)``——文本为「输出格式化」模板
            原文(配置了元数据占位符时含元数据字段,由调用方按模板解析);
            NoRead 文本/超时/断线返回 ``(False, None)``,原因记入
            :attr:`last_error`
        :raises ValueError: 串口未配置或参数非法
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        self._require_serial_config()
        ok, text = self._execute(lambda: self._scan_once(float(timeout)), is_write=True)
        if not ok or text is None:
            return False, None
        if text == self._noread_text:
            # 链路成功但无读出:读码器正常应答,不算设备错误码
            with self._lock:
                self._set_error(
                    _("读码器无读出({})").format(self._noread_text),
                    ErrorCategory.DEVICE,
                    None,
                )
            return False, None
        return True, text

    def read_result(self, timeout: float = 10.0) -> Tuple[bool, Optional[str]]:
        """被动读取一行结果(不触发;多码编排/连续模式场景)。

        :param timeout: 等待结果行的总超时(秒)
        :return: 语义同 :meth:`scan`
        :raises ValueError: 串口未配置或参数非法
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        self._require_serial_config()
        ok, text = self._execute(
            lambda: self._read_result_once(float(timeout)), is_write=True
        )
        if not ok or text is None:
            return False, None
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
        """发送开始触发文本,开始扫描窗(不读结果;动作型命令)。

        多码编排:``trigger()`` → ``read_result()``×N → :meth:`stop()`。
        """
        ok, _unused = self._execute(self._send_trigger, is_write=True)
        return ok

    def stop(self) -> bool:
        """发送停止触发文本,结束扫描窗(动作型命令)。"""
        ok, _unused = self._execute(self._send_stop, is_write=True)
        return ok

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _require_serial_config(self) -> None:
        """串口已配置校验(内部方法)。"""
        if self._serial_config is None:
            raise ValueError(_("请先调用 configure_serial() 配置串口参数"))

    def _scan_once(self, timeout: float) -> str:
        """start → 读结果行 → stop(内部方法,须事务内调用)。

        结果在扫描窗内即时流出(与 SR 的关窗后应答不同),先读后停;
        超时也尽力停窗,防扫描窗常开。
        """
        transport = self._require_transport()
        stopped = False

        def _stop_best_effort() -> None:
            """发送停止文本收窗(幂等;尽力而为,失败不打断主流程)。"""
            nonlocal stopped
            if stopped:
                return
            stopped = True
            try:
                transport.send(self._stop_text.encode("ascii"))
            except (OSError, TransportClosedError):
                pass  # 停窗失败不产生脏数据

        try:
            transport.send(self._trigger_text.encode("ascii"))
            try:
                line = self._read_line(transport, timeout)
            except socket.timeout:
                # **先**发停止文本关窗(慢解码的真结果不再流入),**再**读
                # 残留——顺序与 _drain_line 的"扫描窗已收"前提一致(审查
                # 1001 P2-4:原顺序 drain 在前,窗口还开着,会把慢结果当
                # 残渣吃掉或漏进下一轮)
                _stop_best_effort()
                if not self._drain_line(transport):
                    self._mark_disconnected()
                raise TransportTimeoutError(
                    _("读码结果等待超时({}s),串口无应答").format(timeout), 0
                )
            try:
                return line.decode(self._encoding, errors=self._encoding_errors)
            except UnicodeDecodeError as exc:
                # 解码失败按"设备应答异常"处理(不断线、记 last_error)
                raise DeviceError(
                    _("结果报文解码失败({}):{}").format(self._encoding, exc), 0
                ) from exc
        finally:
            # 成功路径收窗;超时路径已在 except 中先发过(幂等跳过)
            _stop_best_effort()

    def _read_result_once(self, timeout: float) -> str:
        """读一行结果(内部方法,须事务内调用)。

        超时**不 drain 不断线**:扫描窗的开/关由调用方编排(多码场景
        trigger → read_result → stop),窗口开着时迟到的真结果是合法
        数据,下一次 :meth:`read_result` 即可取回——drain 会把它当残渣
        吃掉(审查 1001 P2-4)。超时转 :class:`TransportTimeoutError`
        (串口 0 字节口径,基类不断线;socket.timeout 是 TCP 走线口径,
        直通会被基类按坏链拆连)。
        """
        transport = self._require_transport()
        try:
            line = self._read_line(transport, timeout)
        except socket.timeout as exc:
            raise TransportTimeoutError(
                _("读码结果等待超时({}s),串口无应答").format(timeout), 0
            ) from exc
        try:
            return line.decode(self._encoding, errors=self._encoding_errors)
        except UnicodeDecodeError as exc:
            raise DeviceError(
                _("结果报文解码失败({}):{}").format(self._encoding, exc), 0
            ) from exc

    def _send_trigger(self) -> None:
        """发送开始触发文本(内部方法)。"""
        self._require_transport().send(self._trigger_text.encode("ascii"))

    def _send_stop(self) -> None:
        """发送停止触发文本(内部方法)。"""
        self._require_transport().send(self._stop_text.encode("ascii"))

    @staticmethod
    def _read_line(transport: BaseTransport, read_timeout: float) -> bytes:
        """读取一行以 CR/LF 结束的结果(内部方法,整行受总预算约束)。"""
        chunks = []
        started = False
        previous_timeout = transport.receive_timeout
        deadline = time.monotonic() + read_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout(_("串口收行超时({}s)").format(read_timeout))
                transport.receive_timeout = remaining
                try:
                    byte = transport.recv(1)
                except TransportTimeoutError as exc:
                    # 串口逐字节超时(0 字节)统一转 socket.timeout:让调用方
                    # "except socket.timeout → 停止文本 → drain → 断线判定"
                    # 的收尾路径对"完全静默"与"半行后静默"都完整生效(审查
                    # 1001 P1-4:原实现只接 socket.timeout,TransportTimeoutError
                    # 直接逃逸,drain/断线判定全成死代码)
                    raise socket.timeout(
                        _("串口收行超时({}s)").format(read_timeout)
                    ) from exc
                if byte == b"\r" or byte == b"\n":
                    if not started:
                        continue
                    break
                started = True
                chunks.append(byte)
                if len(chunks) > HIKROBOT_SERIAL_RECV_MAX:
                    raise OmniPLCInternalError(
                        _("串口应答超过 {} 字节上限").format(HIKROBOT_SERIAL_RECV_MAX)
                    )
        finally:
            transport.receive_timeout = previous_timeout
        return b"".join(chunks)

    def _drain_line(self, transport: BaseTransport) -> bool:
        """尽力读掉已到达的半行残留,防下一事务从流中间续读(内部方法)。

        超时此刻停止文本已发、扫描窗已收,链路上只可能是旧残留;读到行尾
        即止,整体受 :data:`HIKROBOT_SERIAL_DRAIN_TIMEOUT` 预算约束。

        :return: 是否已读净——读到行尾或**一个字节都没读到**返回 ``True``
            (无残留);读到了字节却未达行尾返回 ``False``(留半行,调用方
            可据此判定残字节风险)
        """
        previous_timeout = transport.receive_timeout
        deadline = time.monotonic() + HIKROBOT_SERIAL_DRAIN_TIMEOUT
        received = 0
        try:
            while received <= HIKROBOT_SERIAL_RECV_MAX:
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

    def _create_transport(self) -> BaseTransport:
        if self._serial_config is None:
            raise ValueError(_("请先调用 configure_serial() 配置串口参数"))
        return SerialTransport(self._serial_config)

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
