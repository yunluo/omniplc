"""海康机器人 ID 系列智能读码器客户端(Modbus TCP 服务端模式)。

依据:《海康机器人智能读码器工业协议操作手册》V1.0.4(随官方
「智能读码器工业协议20250922.zip」发布)§3 ModBus 章节。手册随包附带的
《读码器固件版本要求.txt》给出各系列最低固件:ID800/ID2000 ≥ V3.0.0、
ID3000/ID5000 ≥ V2.4.0、ID5000X ≥ V3.2.2、IDH 系列 ≥ V1.0.4。

**协议角色**(本类的立足点):工业协议包中五种协议的读码器侧角色不同——

=========  ====================  ==================  ==================
协议        读码器角色            对接方角色           本库可否对接
=========  ====================  ==================  ==================
Modbus     服务端(从站)         主站                 **可(本类)**
MELSEC     主站(轮询 PLC D 区)  被动 MC 服务器       不可(读码器↔PLC 点对点)
FINS       主站(轮询 PLC DM 区) FINS 服务器          不可(同上)
EtherNet/IP Class 1 隐式 I/O    Scanner 扫描器       不可(EDS 仅声明 Class 1 周期连接)
PROFINET   设备(GSDML)          RT 控制器            不可(实时以太网,库无 PROFINET)
=========  ====================  ==================  ==================

即读码器的 SLMP/FINS/EIP/PROFINET 模式为**读码器↔PLC 点对点**集成,
上位库无角色;Modbus TCP 服务端模式是唯一"读码器=从站、主站=本库"的
工业协议路径(§3.2 印刷页 31:工作模式选择服务端)。

**继承形态**:本类继承 :class:`omniplc.ModbusTcpClient`——读码器就是一个
Modbus TCP 从站,设备特有知识(寄存器模型 + 触发握手 + 结果解码)叠加在
主站能力之上,与 :class:`omniplc.InovanceTcpClient`(H3U/H5U Modbus 兼容
PLC)同型。因此本实例**本身就是 Modbus 主站**:既可 :meth:`scan` 驱动
读码握手,也可用基类 ``read``/``write`` 访问同一网络的其他从站设备
(共享一条 TCP 连接时按需各自建实例)。

寄存器模型(§3.5 印刷页 39-41,服务端模式偏移固定):

- 控制区 REG0(本库写,FC06):bit0 Trigger Enable、bit1 Trigger、
  bit2 Results Ack、bit15 Clear Error
- 状态区 REG1(本库读,FC03):bit0 Trigger Ready、bit1 Trigger Ack、
  bit2 Acquiring、bit3 Decoding、bit8 Results OK、bit9 Results NG、
  bit15 General Fault
- 结果区 REG2 起(本库读,FC03):REG2 = Result Length(结果字符数),
  REG3 起连续存放 ASCII 码格式的读码结果,不足填 0、超出截断

触发握手时序(§3.6 印刷页 41-42):

1. 写控制字 = Trigger Enable,等待状态区 Trigger Ready
2. 写控制字 = Enable | Trigger(上升沿触发),等 Trigger Ack 后回落
   Trigger 位(下一轮触发依赖 0→1 上升沿)
3. 轮询状态区直至 Results OK / Results NG(**与结果区同笔 FC03 读出**,
   状态与数据同快照,规避新旧结果错配)
4. Results Ack 置位应答,并等待设备清掉 OK/NG(未消费即发起下一轮会
   读到陈旧结果,故按握手闭环处理)
5. General Fault = 设备内部异常,经 :meth:`clear_error` 清除后可继续

公共 API 沿用扫码枪家族约定::meth:`scan` 返回 ``(是否读到条码, 条码文本)``。
与基恩士 :class:`~omniplc.KeyenceSrClient` 的差异:读码器以 OK/NG/超时
三态显式区分"未读到码"与"链路/设备异常",故 NG 归 ``scan`` 正常返回
``(False, None)``,超时与故障**抛异常**(细分自动化处置),不并入
``(False, None)``。

结果内容为读码器 IDMVS「数据处理」配置的输出字符串(§3.2 印刷页 31):
除条码内容外,质量/码制等元数据可经该配置并入输出串,随结果区原文透传;
结构化元数据(多码列表/质量分/位置)需读码器原生 TCP 命令协议,另行驱动。
"""
from __future__ import annotations

import time
from typing import List, NamedTuple, Optional, Tuple

from ..core.constants import (
    HIKROBOT_ACK_SETTLE,
    HIKROBOT_CONTROL_OFFSET,
    HIKROBOT_CTRL_CLEAR_ERROR,
    HIKROBOT_CTRL_RESULTS_ACK,
    HIKROBOT_CTRL_TRIGGER,
    HIKROBOT_CTRL_TRIGGER_ENABLE,
    HIKROBOT_MODBUS_STATION_DEFAULT,
    HIKROBOT_RESULT_WORDS_DEFAULT,
    HIKROBOT_RESULT_WORDS_MAX,
    HIKROBOT_RESULT_WORDS_MIN,
    HIKROBOT_STATUS_ACQUIRING,
    HIKROBOT_STATUS_DECODING,
    HIKROBOT_STATUS_GENERAL_FAULT,
    HIKROBOT_STATUS_OFFSET,
    HIKROBOT_STATUS_RESULTS_NG,
    HIKROBOT_STATUS_RESULTS_OK,
    HIKROBOT_STATUS_TRIGGER_ACK,
    HIKROBOT_STATUS_TRIGGER_READY,
    MODBUS_DEFAULT_PORT,
)
from ..core.errors import DeviceError, ErrorCategory, TransportTimeoutError
from ..core.i18n import _
from ..core.types import DataType
from ..modbus import ModbusTcpClient

__all__ = ["HikrobotIdModbusClient", "HikrobotStatus"]


class HikrobotStatus(NamedTuple):
    """读码器状态区快照(§3.5 状态区定义,REG1 各位)。"""

    trigger_ready: bool
    """Trigger Ready(bit0):已使能且准备接收下一个触发信号。"""
    trigger_ack: bool
    """Trigger Ack(bit1):设备已成功接收触发信号。"""
    acquiring: bool
    """Acquiring(bit2):设备正在获取图像。"""
    decoding: bool
    """Decoding(bit3):设备正在对图像识别译码。"""
    results_ok: bool
    """Results OK(bit8):设备成功输出新的结果。"""
    results_ng: bool
    """Results NG(bit9):设备未读到码或未获取到输出结果。"""
    general_fault: bool
    """General Fault(bit15):设备内部产生错误。"""
    raw: int
    """状态字原始值(0~65535)。"""


class HikrobotIdModbusClient(ModbusTcpClient):
    """海康机器人 ID 系列智能读码器客户端(Modbus TCP 服务端模式)。

    :example::

        client = HikrobotIdModbusClient("192.168.1.100", station=0)
        client.connect()
        ok, code = client.scan()            # (True, "ABC123") / (False, None)=NG
        status = client.read_status()       # 状态区快照
        client.close()
    """

    # 探活:读状态区 REG1(FC03 零副作用单读,读码器自身状态寄存器)
    _has_ping = True

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MODBUS_DEFAULT_PORT,
        station: int = HIKROBOT_MODBUS_STATION_DEFAULT,
        result_words: int = HIKROBOT_RESULT_WORDS_DEFAULT,
        byte_swap: bool = False,
        encoding: str = "utf-8",
        encoding_errors: str = "strict",
    ) -> None:
        """初始化读码器客户端。

        :param ip_address: 读码器 IP 或主机名(示例默认 192.168.0.10,
            实际以 IDMVS 通信配置为准)
        :param port: Modbus TCP 端口,默认 502
        :param station: 从机地址,默认 0(§3.2 印刷页 31:读码器默认
            255 或 0,V3.2.2.R 及以上固件可配;本库站号按 Modbus 规范钉
            0~247,255 超出上限故取双默认之一的 0,现场若配了 255 需改
            到 0~247;地址不一致将无法连通)
        :param result_words: 结果区大小(寄存器数,4~500 默认 100,§3.5
            印刷页 41)——**须与读码器 IDMVS「结果模块大小」配置一致**,
            偏小截断、偏大多读尾部填 0 字
        :param byte_swap: 结果数据寄存器内字节交换。与读码器通信配置中
            「ModBus 结果字节交换」开关对应(§3.6 印刷页 44:结果字节序
            与实际不一致时可开启);默认 False
        :param encoding: 条码内容解码编码,默认 utf-8(ASCII 条码自然兼容;
            QR/DataMatrix 可携带 GB2312/任意二进制内容,按现场码制选择)
        :param encoding_errors: 解码失败策略,默认 ``strict``——非法序列
            抛错(不静默以 U+FFFD 乱码当条码成功返回);确需容错可显式传
            ``replace``
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, int(port), station)
        if not HIKROBOT_RESULT_WORDS_MIN <= int(result_words) <= HIKROBOT_RESULT_WORDS_MAX:
            raise ValueError(
                _("result_words 必须在 {}~{} 之间,收到:{}").format(
                    HIKROBOT_RESULT_WORDS_MIN, HIKROBOT_RESULT_WORDS_MAX, result_words
                )
            )
        self._result_words = int(result_words)
        self._byte_swap = bool(byte_swap)
        try:
            "".encode(encoding)
        except LookupError as exc:
            raise ValueError(_("encoding 非法:{!r}").format(encoding)) from exc
        self._encoding = encoding
        self._encoding_errors = encoding_errors
        # 预生成结果区地址表(REG1 状态字 + REG2..REG2+N-1 结果区,USHORT):
        # 偏移连续,轮询时经 read_batch 合并为一笔 FC03(状态与结果同快照,
        # 防新旧结果错配)
        self._block_addresses = [
            ("hr{}".format(HIKROBOT_STATUS_OFFSET + index), DataType.USHORT)
            for index in range(self._result_words + 1)
        ]

    # ------------------------------------------------------------------
    # 读码 API
    # ------------------------------------------------------------------

    def scan(
        self, timeout: float = 10.0, poll_interval: float = 0.05
    ) -> Tuple[bool, Optional[str]]:
        """触发一次读码并等待结果(完整握手,§3.6 印刷页 41-42 时序)。

        时序:使能 → 等 Trigger Ready → Trigger 上升沿 → 等 Trigger Ack
        后回落 Trigger 位 → 轮询(状态字 + 结果区**同笔 FC03**)至
        Results OK/NG → Results Ack 应答 → 等设备清掉 OK/NG。

        :param timeout: 整个握手(含等待结果)的总超时(秒)
        :param poll_interval: 状态轮询间隔(秒),过小增加 Modbus 事务负载
        :return: ``(是否读到条码, 条码文本)``。Results OK → ``(True, 文本)``
            (读码器 NoRead 使能时未读到码反馈 OK + ``"NoRead"`` 字符,原样
            返回);Results NG(未读到码,需读码器侧关闭 NoRead)→
            ``(False, None)``
        :raises TransportTimeoutError: 超时未出结果(读码器/链路停滞)
        :raises DeviceError: General Fault(设备内部异常,先
            :meth:`clear_error` 再重试)、Results Ack 未被设备消费、
            Modbus 事务失败(细节见 ``last_error``)
        :raises ValueError: 参数非法

        握手全程持**事务锁**(与家族其余 scan 同口径,审查 1001 P2-5:
        防并发线程交插图双触发),握手级失败写 ``last_error`` 三件套。
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        if poll_interval <= 0:
            raise ValueError(_("poll_interval 必须大于 0,收到:{}").format(poll_interval))
        with self._lock:
            return self._scan_locked(timeout, poll_interval)

    def _scan_locked(
        self, timeout: float, poll_interval: float
    ) -> Tuple[bool, Optional[str]]:
        """scan 握手实现(内部方法,须持事务锁)。"""
        deadline = time.monotonic() + float(timeout)

        # 1. 使能触发(§3.6 步骤 1)
        self._write_control(HIKROBOT_CTRL_TRIGGER_ENABLE, _("使能触发"))
        # 2. 等 Trigger Ready(§3.6 步骤 1:设备准备就绪后置位)
        self._wait_status_bit(
            HIKROBOT_STATUS_TRIGGER_READY,
            deadline,
            poll_interval,
            _("等待 Trigger Ready"),
        )
        # 3. Trigger 上升沿(§3.6 步骤 2;下一轮触发依赖 0→1)
        self._write_control(
            HIKROBOT_CTRL_TRIGGER_ENABLE | HIKROBOT_CTRL_TRIGGER, _("发送触发")
        )
        # 4. 轮询状态字 + 结果区(同笔 FC03,同快照);Trigger Ack 出现后
        #    回落 Trigger 位(§3.6 步骤 6:下一轮需先复位)
        trigger_armed = True
        words: Optional[List[int]] = None
        ng = False
        while True:
            words = self._read_status_and_result()
            status = words[0]
            if status & HIKROBOT_STATUS_GENERAL_FAULT:
                # 设备内部异常(§3.6:确认错误原因后 Clear Error 可继续)
                message = _("读码器内部故障(General Fault),请排查后调用 clear_error() 清除")
                self._set_error(message, ErrorCategory.DEVICE, 0)
                raise DeviceError(message, 0)
            if trigger_armed and status & HIKROBOT_STATUS_TRIGGER_ACK:
                self._write_control(HIKROBOT_CTRL_TRIGGER_ENABLE, _("回落触发位"))
                trigger_armed = False
            if status & HIKROBOT_STATUS_RESULTS_OK:
                ng = False
                break
            if status & HIKROBOT_STATUS_RESULTS_NG:
                ng = True
                break
            if time.monotonic() >= deadline:
                message = _("读码超时({}s),设备未输出 Results OK/NG").format(timeout)
                self._set_error(message, ErrorCategory.TIMEOUT, None)
                raise TransportTimeoutError(message, 0)
            time.sleep(poll_interval)
        # 5. Results Ack 应答(§3.6 步骤 5:读取完成后置位,设备清 OK/NG)
        self._write_control(
            HIKROBOT_CTRL_TRIGGER_ENABLE | HIKROBOT_CTRL_RESULTS_ACK, _("应答结果")
        )
        # 6. 等设备消费 Ack(OK/NG 清零):未清即发起下一轮会读到陈旧结果,
        #    握手必须闭环(§3.6 步骤 6)。用**独立收尾预算**(审查 1001
        #    P2-6:不继承结果等待的过期时刻,防边界拍误报并丢结果)
        ack_deadline = time.monotonic() + HIKROBOT_ACK_SETTLE
        while True:
            status = self._read_status_word()
            if not status & (HIKROBOT_STATUS_RESULTS_OK | HIKROBOT_STATUS_RESULTS_NG):
                break
            if time.monotonic() >= ack_deadline:
                message = _(
                    "Results Ack 未被设备消费(Results OK/NG 未清零),握手未闭环"
                )
                self._set_error(message, ErrorCategory.DEVICE, 0)
                raise DeviceError(message, 0)
            time.sleep(poll_interval)
        if ng:
            # Results NG:设备已把结果区清零(§3.6 步骤 3),正常未读到码
            return False, None
        assert words is not None  # 循环必经 break,words 已就绪
        return True, self._decode_result(words)

    def read_status(self) -> HikrobotStatus:
        """读取状态区快照(FC03 读 REG1,一次事务)。

        供 PLC 式轮询/诊断使用;读码请走 :meth:`scan`。

        :raises DeviceError: Modbus 事务失败(细节见 ``last_error``)
        """
        return self._to_status(self._read_status_word())

    def _ping_probe(self) -> int:
        """探活探测命令:FC03 读状态字 REG1(内部方法)。

        依据:工业协议手册 V1.0.4 §3.5 印刷页 40——状态寄存器只读,
        零副作用;不触发扫描握手,读码器应答即链路存活。
        """
        return self._read_status_word()

    def clear_error(self, timeout: float = 2.0, poll_interval: float = 0.05) -> bool:
        """清除设备错误状态(控制字 bit15 置位,§3.5 印刷页 40)。

        置位 Clear Error 后轮询 General Fault 清零即复位控制字,避免
        Clear Error 长期挂在高电平。

        :param timeout: 等待故障清零的超时(秒)
        :param poll_interval: 状态轮询间隔(秒)
        :return: 故障是否已清零
        :raises ValueError: 参数非法
        :raises DeviceError: Modbus 事务失败
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        if poll_interval <= 0:
            raise ValueError(_("poll_interval 必须大于 0,收到:{}").format(poll_interval))
        self._write_control(
            HIKROBOT_CTRL_TRIGGER_ENABLE | HIKROBOT_CTRL_CLEAR_ERROR, _("清除错误")
        )
        deadline = time.monotonic() + float(timeout)
        while True:
            if not self._read_status_word() & HIKROBOT_STATUS_GENERAL_FAULT:
                self._write_control(HIKROBOT_CTRL_TRIGGER_ENABLE, _("复位控制字"))
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # 内部实现(寄存器读写与结果解码)
    # ------------------------------------------------------------------

    def _write_control(self, value: int, action: str) -> None:
        """写控制字(FC06 单寄存器写,内部方法)。

        :raises DeviceError: 事务失败(附 last_error 细节)
        """
        if not self.write(
            "hr{}".format(HIKROBOT_CONTROL_OFFSET), DataType.USHORT, value
        ):
            raise DeviceError(
                _("控制字写入失败({}):{}").format(action, self.last_error), 0
            )

    def _read_status_word(self) -> int:
        """读状态字 REG1 原始值(内部方法)。

        :raises DeviceError: 事务失败
        """
        ok, value = self.read("hr{}".format(HIKROBOT_STATUS_OFFSET), DataType.USHORT)
        if not ok or value is None:
            raise DeviceError(_("状态字读取失败:{}").format(self.last_error), 0)
        return int(value)

    def _read_status_and_result(self) -> List[int]:
        """状态字 + 结果区一次读出(FC03 连续寄存器,内部方法)。

        REG1(状态)与 REG2 起(结果区)偏移连续,:meth:`scan` 轮询经
        :meth:`ModbusTcpClient.read_batch` 合并为一笔 FC03——状态与结果
        同快照,规避"状态已 OK 而结果区仍是旧值"的新旧错配。

        **上限注意**(审查 1001 P3-⑱):``result_words > 124`` 时单笔超出
        Modbus 字读上限 125(含状态字),``read_batch`` 自动拆多笔 FC03——
        此时状态与结果**不再同快照**(设备先写数据后置 OK 位,错配窗口小);
        需要严格同快照请把 ``result_words`` 控制在 124 以内。

        :raises DeviceError: 事务失败
        """
        ok, values = self.read_batch(self._block_addresses)
        if not ok or values is None:
            raise DeviceError(_("状态与结果区读取失败:{}").format(self.last_error), 0)
        return [int(value) for value in values]

    def _wait_status_bit(
        self, bit: int, deadline: float, poll_interval: float, action: str
    ) -> None:
        """轮询等待状态字某位置位(内部方法)。

        :raises TransportTimeoutError: 超时未置位
        :raises DeviceError: General Fault 或事务失败
        """
        while True:
            status = self._read_status_word()
            if status & HIKROBOT_STATUS_GENERAL_FAULT:
                raise DeviceError(
                    _("读码器内部故障(General Fault),请排查后调用 clear_error() 清除"), 0
                )
            if status & bit:
                return
            if time.monotonic() >= deadline:
                raise TransportTimeoutError(
                    _("等待状态位超时({}),未在期限内置位").format(action), 0
                )
            time.sleep(poll_interval)

    @staticmethod
    def _to_status(raw: int) -> HikrobotStatus:
        """状态字原始值转快照(内部方法)。"""
        return HikrobotStatus(
            trigger_ready=bool(raw & HIKROBOT_STATUS_TRIGGER_READY),
            trigger_ack=bool(raw & HIKROBOT_STATUS_TRIGGER_ACK),
            acquiring=bool(raw & HIKROBOT_STATUS_ACQUIRING),
            decoding=bool(raw & HIKROBOT_STATUS_DECODING),
            results_ok=bool(raw & HIKROBOT_STATUS_RESULTS_OK),
            results_ng=bool(raw & HIKROBOT_STATUS_RESULTS_NG),
            general_fault=bool(raw & HIKROBOT_STATUS_GENERAL_FAULT),
            raw=raw,
        )

    def _decode_result(self, words: List[int]) -> str:
        """结果区寄存器解码为条码文本(内部方法)。

        布局(§3.5 结果区定义,印刷页 41):首字 = Result Length(结果
        字符数),其后连续存放 ASCII 码格式的读码结果,不足填 0、超出
        截断。每寄存器两字节,Modbus 寄存器标准大端;读码器侧开启
        「结果字节交换」时寄存器内高低字节对调,按 :attr:`byte_swap`
        解码。

        **待核假设**(审查 1001 P3-⑰):手册(印刷页 44)只写「交换结果
        数据字节序」,长度字 REG2 是否同样交换未明——本库按"仅数据寄存器"
        解读(长度字恒标准大端);若真机整区交换,length=6 解成 0x0600 被
        min() 截到容量会解出乱码,核证要点已列真机清单。
        """
        length = words[1] if len(words) > 1 else 0
        # words[0] 为状态字、words[1] 为长度寄存器,数据自 words[2](REG3)起
        capacity = (len(words) - 2) * 2
        length = min(length, capacity)
        chunks = []
        for word in words[2:]:
            raw = int(word)
            if self._byte_swap:
                raw = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
            chunks.append(raw.to_bytes(2, "big"))
        data = b"".join(chunks)[:length]
        return data.decode(self._encoding, errors=self._encoding_errors)
