"""西门子 S7 客户端(自研 S7comm 协议栈,ISO-on-TCP 102,核心零第三方依赖)。

依据:S7comm 无官方公开手册,帧面按「参考实现逐字节比对」铁律退档——
同类开源参考实现(3.2.0 快照)(纯 Python 重写版,MIT)为主源,交叉参考实现 交叉,
同类开源库 C++(LGPL)只比对行为不抄码;全部帧面事实与出处(文件 + 函数)
归档 `docs/protocol/siemens/s7comm/README.md`(2026-10-03 抽取)。S7-1500
Communication Function Manual §3.5 p.22(ISO-on-TCP 端口 102)、§7 p.50
(PUT/GET 指令:仅绝对寻址数据块、需在 CPU 保护组态开启该服务)。

v0.53 起由 同类开源封装 封装**整体替换为自研 S7comm 栈**(用户裁决:
名字与 API 不变、依赖退役):`dll_path` 参数移除(同类开源库 DLL 按解释器
分版本、32 位自备 DLL 的痛点正是替换动机);3.7~3.9 用户从此免装
同类开源封装(1.3) + setuptools。公开面冻结:构造参数 / rack·slot /
通用读写全族 / `get_cpu_state` / `read_range` / `read_many` /
`read_batch` / `read_wstring` / `write_wstring` 与地址语法全部不变。

类继承::

    BaseClient
    └── SiemensS7Client   S7 会话(默认 rack 0 / slot 1 / 端口 102;会话适配见 _S7Session)

地址语法见 :mod:`omniplc.plc.siemens.address`(DB/I/Q/M,尺寸由显式
DataType 决定,大端序)。S7-1200/1500 侧需勾选"允许来自远程对象的
PUT/GET 通信访问",且 DB 须为**非优化块**(绝对寻址;优化块访问报
DeviceError 并附提示)。

错误边界:传输层故障(TCP 断/帧收发失败)抛 OSError(惰性重连);
帧结构错(坏帧/序列号回显不符)抛内部协议错误(拆连重同步);
PLC 侧拒绝(数据段返回码非 0xFF、协议 error_class)→ DeviceError
(不断线,DB 访问附优化块提示)。

范围:单点读写(位读改写)+ S7 String/WString + 多变量批量读
(``read_batch``/``read_many``,S7 Read Var 多 Item 单 PDU,≤20 条)+
``read_range`` + CPU 状态(SZL 0x0424,**待真机核证**);块操作、
SZL 全家、时钟留后续版本。
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ...core import convert
from ...core.base_client import BaseClient, DEFAULT_STRING_ENCODING, validate_endpoint
from ...core.constants import (
    S7_MODEL_PRESETS,
    S7_DEFAULT_PORT,
    S7_MAX_MULTI_VARS,
    S7_RACK_MAX,
    S7_SLOT_MAX,
    S7_WSTRING_DEFAULT_LENGTH,
)
from ...core.debug import log_op
from ...core.errors import DeviceError, TransportClosedError
from ...core.validation import require_bool, require_float, require_int
from ...core.types import DataType, PrimitiveValue, S7Model
from ...transport.base import BaseTransport
from ...transport.tcp import TcpTransport
from .address import S7Address, area_code, parse_s7_address, translate_v_address
from . import codec
from ...core.i18n import _

_V_MODELS: Tuple[S7Model, ...] = (S7Model.S7_200, S7Model.S7_200_SMART)
"""V 区记号放行的型号(经典 S7-200 与 200 SMART;V 存储器 = DB1)。"""

_WSTRING_MODELS: Tuple[S7Model, ...] = (S7Model.S7_1200, S7Model.S7_1500)
"""WString 放行的型号(仅 TIA 时代的 1200/1500;WString 是 TIA 类型,
经典 300/400 与 200 系无该类型——读会按 UTF-16BE 解出乱码,写会污染
数据,显式拒防静默错值)。"""

_SIZES = {
    DataType.BOOL: 1,
    DataType.SHORT: 2,
    DataType.USHORT: 2,
    DataType.INT: 4,
    DataType.UINT: 4,
    DataType.LONG: 8,
    DataType.ULONG: 8,
    DataType.FLOAT: 4,
    DataType.DOUBLE: 8,
}
"""数值 DataType → 字节数(S7 大端序)。"""

_INT_FORMATS = {
    DataType.SHORT: ">h",
    DataType.USHORT: ">H",
    DataType.INT: ">i",
    DataType.UINT: ">I",
    DataType.LONG: ">q",
    DataType.ULONG: ">Q",
}
"""整数 DataType → struct 大端格式(含符号语义)。"""

_CPU_STATUS_NAMES: Dict[int, str] = {
    0x08: "S7CpuStatusRun",
    0x04: "S7CpuStatusStop",
    0x03: "S7CpuStatusStop",
}
"""SZL 0x0424 状态值(bzu_id)→ 枚举名:0x08 Run / 0x04 Stop / 0x03 Stop
(老 CPU 兼容,同类开源库 C `opGetPlcStatus` L2044-2048 注「STOP 有时编为 0x03」;
其余 Unknown 兜底——**有意不照搬 C 的「未知一律按 STOP」**:把坏帧/新值
误报成停机比 Unknown 更危险,分歧点已记帧面档案;**偏移待真机核证**)。"""


def resolve_s7_connection(
    model: S7Model, rack: Optional[int], slot: Optional[int]
) -> Tuple[int, int, int, int, int]:
    """CPU 型号 → 连接参数解析(同步/原生/异步三面共用,模块函数)。

    预设表 :data:`~omniplc.core.constants.S7_MODEL_PRESETS` 给出型号惯例的
    本端 TSAP / 连接类型 / rack / slot / TPDU 尺寸;``rack``/``slot`` 显式
    给出时覆写预设(0~上限校验),缺省 ``None`` 用预设。

    :return: ``(local_tsap, remote_tsap, tpdu_size_code, rack, slot)``
    :raises ValueError: ``model`` 非 :class:`~omniplc.core.types.S7Model`
        成员,或覆写 rack/slot 越界
    """
    if not isinstance(model, S7Model):
        raise ValueError(_("model 必须是 S7Model 枚举成员,收到:{!r}").format(model))
    preset = S7_MODEL_PRESETS[model]
    resolved_rack = preset.rack if rack is None else int(rack)
    resolved_slot = preset.slot if slot is None else int(slot)
    if not 0 <= resolved_rack <= S7_RACK_MAX:
        raise ValueError(
            _("机架号必须在 0~{} 之间,收到:{}").format(S7_RACK_MAX, resolved_rack)
        )
    if not 0 <= resolved_slot <= S7_SLOT_MAX:
        raise ValueError(
            _("槽位号必须在 0~{} 之间,收到:{}").format(S7_SLOT_MAX, resolved_slot)
        )
    if preset.fixed_remote_tsap is not None:
        # S7-200/CP243:远端 TSAP = "MW" 记号,与 rack/slot 无关
        remote_tsap = preset.fixed_remote_tsap
    else:
        remote_tsap = (
            (preset.connection_type << 8) | (resolved_rack << 5) | resolved_slot
        )
    return (
        preset.local_tsap,
        remote_tsap,
        preset.tpdu_size_code,
        resolved_rack,
        resolved_slot,
    )


class _S7Session(BaseTransport):
    """S7comm 会话(自研栈,适配为传输对象外形;私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 完成
    TCP → COTP(CR/CC,TSAP 编码 rack/slot)→ S7 通信协商(PDU 长度)
    三步,``close`` 尽力发 COTP DR 后关传输;区域读写经
    :meth:`read_area` / :meth:`write_area`(统一 BYTE 传输尺寸,与
    同类开源库 read_area/write_area 的 WORDLen=BYTE 口径一致),错误边界:

    - TCP 层 OSError 透传(惰性重连)
    - 帧结构错(codec :class:`.codec.S7ProtocolError`)→ 内部协议错误,
      基类按 OmniPLCInternalError 拆连重同步
    - PLC 侧拒绝(条目返回码非 0xFF)→ DeviceError 不断线,DB 访问附
      优化块访问提示

    ``receive_timeout`` 直接下发到底层 TCP 传输(已连接时立即生效)。
    """

    def __init__(
        self,
        ip_address: str,
        port: int,
        local_tsap: int,
        remote_tsap: int,
        tpdu_size_code: int,
        rack: int,
        slot: int,
    ) -> None:
        """初始化 S7 会话。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param local_tsap: 本端(Calling)TSAP(型号预设,见 S7_MODEL_PRESETS)
        :param remote_tsap: 远端(Called)TSAP(由 :func:`resolve_s7_connection`
            按型号预设 + rack/slot 解析)
        :param tpdu_size_code: CR 的 TPDU 尺寸指数(0x0A=1024,CP243 口径 0x09)
        :param rack: 机架号(仅用于调试标签)
        :param slot: 槽位号(仅用于调试标签)
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._local_tsap = local_tsap
        self._remote_tsap = remote_tsap
        self._tpdu_size_code = tpdu_size_code
        self._rack = rack
        self._slot = slot
        self._tcp: Optional[TcpTransport] = None
        self._sequence = 0
        self.pdu_size = codec.MAX_PDU_REQUEST
        self._dst_ref = 0
        """CC 应答回显的对端引用,close 发 COTP DR 时回填(connection.py
        L431-450 用 CC 回给的 dst_ref;默认 0,DR 为尽力而为语义)。"""
        self._debug_label = "s7://{}:{}(机架{}槽位{})".format(
            ip_address, port, rack, slot
        )

    @BaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒);已连接时立即下发到底层 TCP 传输。"""
        if seconds <= 0:
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
        BaseTransport.receive_timeout.fset(self, float(seconds))  # type: ignore[attr-defined]
        tcp = self._tcp
        if tcp is not None:
            tcp.receive_timeout = float(seconds)

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """三步建连:TCP → COTP(CR/CC)→ S7 协商。

        连接超时取 :attr:`connect_timeout`(基类在创建会话时已同步到本
        会话属性,此处下发到底层 TCP 传输;review-1008 P1:曾漏下发,
        恒用传输层默认值)。

        :raises OSError: TCP 连接失败或握手帧异常(连接期任何失败都按
            断连语义抛 OSError,由基类惰性重连)
        """
        stale = self._tcp
        if stale is not None:  # 防御:重复 connect 先清旧传输防泄漏
            self._tcp = None
            try:
                stale.close()
            except OSError:
                pass
        tcp = TcpTransport(self._ip_address, self._port)
        try:
            tcp.connect_timeout = self._connect_timeout
            tcp.connect()
            tcp.receive_timeout = self._receive_timeout
            self._tcp = tcp  # 先挂会话(协商事务经 _require_tcp 取传输)
            tcp.send(
                codec.build_tpkt(
                    codec.build_cotp_cr(
                        self._remote_tsap, self._local_tsap, self._tpdu_size_code
                    )
                )
            )
            length = codec.parse_tpkt_header(tcp.recv(4))
            self._dst_ref = codec.parse_cotp_cc(tcp.recv(length - 4), self._remote_tsap)
            self._sequence = 0
            sequence = self._next_sequence()
            response = self._transact(
                codec.build_setup_comm(codec.MAX_PDU_REQUEST, sequence)
            )
            self.pdu_size = codec.parse_setup_comm(response, sequence)
        except Exception as exc:
            self._tcp = None
            try:
                tcp.close()
            except OSError:
                pass
            if isinstance(exc, OSError):
                raise
            # 连接期坏帧按断连语义(基类重试即重连)
            raise OSError(_("S7 握手失败:{}").format(exc)) from exc
        log_op(self._debug_label, "会话已建立(PDU {}B)".format(self.pdu_size))

    def close(self) -> None:
        """尽力发 COTP DR 后关闭传输,幂等。"""
        tcp, self._tcp = self._tcp, None
        if tcp is None:
            return
        try:
            # COTP DR(断连请求,同类开源封装 connection.py L431-450 同款;
            # dst_ref = CC 应答回显的对端引用,src_ref 恒本端缺省)
            dr = struct.pack(
                ">BBHHBB", 6, 0x80, self._dst_ref, codec.SRC_REFERENCE, 0x00, 0x00
            )
            tcp.send(codec.build_tpkt(dr))
        except OSError:
            pass
        try:
            tcp.close()
        except OSError:
            pass
        log_op(self._debug_label, "会话已断开")

    def _next_sequence(self) -> int:
        """PDU 引用 u16 循环递增(内部方法,会话线程内调用)。"""
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    def _transact(self, request: bytes) -> bytes:
        """完整事务:COTP DT 包裹发送 → 收 TPKT 剥 COTP 返回 S7 PDU(内部)。"""
        tcp = self._require_tcp()
        tcp.send(codec.build_tpkt(codec.build_cotp_dt(request)))
        length = codec.parse_tpkt_header(tcp.recv(4))
        return codec.parse_cotp_dt(tcp.recv(length - 4))

    def _require_tcp(self) -> TcpTransport:
        """取底层 TCP 传输,未建立则抛出(内部方法)。"""
        if self._tcp is None:
            raise TransportClosedError(_("S7 会话未建立"))
        return self._tcp

    # ------------------------------------------------------------------
    # 会话调用(区域读写 / 状态 / 批量)
    # ------------------------------------------------------------------

    def send(self, data: bytes) -> None:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    def recv(self, size: int) -> bytes:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    def read_area(self, area: int, db_number: int, start: int, size: int) -> bytes:
        """读一块区域字节(会话调用;统一 BYTE 传输尺寸,与旧封装口径一致)。

        跨度超过单请求 PDU 容量(协商值 - 18 字节读侧开销,至少 1)时
        **自动分片**循环读回拼接(同类开源参考实现(3.2.0 快照) client.py L997-1063
        `read_area` 同款行为,client_base.py L199-208 容量公式);任一片
        失败即整块失败(异常上抛,调用方按各自容错口径处理)。
        """
        chunks = []
        offset = 0
        while offset < size:
            chunk = min(size - offset, max(1, self.pdu_size - 18))
            chunks.append(self._read_area_once(area, db_number, start + offset, chunk))
            offset += chunk
        data = b"".join(chunks)
        log_op(
            self._debug_label,
            "read area=0x%02X db=%d start=%d size=%d → %dB",
            area,
            db_number,
            start,
            size,
            len(data),
        )
        return data

    def _read_area_once(
        self, area: int, db_number: int, start: int, size: int
    ) -> bytes:
        """单事务读一块区域字节(分片原子操作,内部方法)。"""
        sequence = self._next_sequence()
        request = codec.build_read(
            area, db_number, start * 8, codec.WORD_LEN_BYTE, size, sequence
        )
        response = self._transact(request)
        try:
            data = codec.parse_read_response(response, sequence, 1, [size])[0]
        except DeviceError as exc:
            raise self._with_db_hint(area, db_number, exc) from exc
        return bytes(data)

    def write_area(self, area: int, db_number: int, start: int, data: bytes) -> None:
        """写一块区域字节(会话调用)。

        跨度超过单请求 PDU 容量(协商值 - 35 字节写侧开销,至少 1)时
        **自动分片**顺序写(同类开源参考实现(3.2.0 快照) `write_area` 同款行为,
        client_base.py L210-219 容量公式);分片中途失败时前片已落盘
        (部分写,协议无跨片原子性),调用方按整块异常感知。
        """
        data = bytes(data)
        offset = 0
        while offset < len(data):
            chunk = min(len(data) - offset, max(1, self.pdu_size - 35))
            self._write_area_once(
                area, db_number, start + offset, data[offset : offset + chunk]
            )
            offset += chunk
        log_op(
            self._debug_label,
            "write area=0x%02X db=%d start=%d %dB",
            area,
            db_number,
            start,
            len(data),
        )

    def _write_area_once(
        self, area: int, db_number: int, start: int, data: bytes
    ) -> None:
        """单事务写一块区域字节(分片原子操作,内部方法)。"""
        sequence = self._next_sequence()
        request = codec.build_write(
            area, db_number, start * 8, codec.WORD_LEN_BYTE, data, sequence
        )
        response = self._transact(request)
        try:
            codec.parse_write_response(response, sequence, 1)
        except DeviceError as exc:
            raise self._with_db_hint(area, db_number, exc) from exc

    @staticmethod
    def _with_db_hint(area: int, db_number: int, exc: DeviceError) -> DeviceError:
        """DB 区绝对访问被 PLC 拒绝时附优化块访问提示(旧封装同口径)。

        S7-1200/1500 默认优化块无绝对地址,报错常被误读为通信故障——
        提示指向 TIA 设置(review-1008 P2:自研重写时曾丢失该提示)。
        """
        if area == codec.AREA_DB and db_number:
            return DeviceError(
                str(exc)
                + _("(按绝对地址访问 DB 失败:若为 S7-1200/1500,请确认该 DB ")
                + _("已在 TIA 中取消 Optimized block access)"),
                exc.code,
            )
        return exc

    def get_cpu_state(self) -> str:
        """读 CPU 运行状态(SZL 0x0424,零副作用;同时是探活探测命令)。

        返回状态枚举名(``"S7CpuStatusRun"``/``"S7CpuStatusStop"``/
        ``"S7CpuStatusUnknown"``)。状态取记录区第 4 字节 bzu_id——
        同类开源库 C `opGetPlcStatus` L2038 读 ``opData[7]``,opData =
        AddLen/AddCount + 记录区,即记录区 [3](ereig 2B + ae 1B 之后);
        review-1008 P1:曾取记录区 [0](ereig 首字节,与状态字段错位,
        真机近乎必错)。**偏移仍待真机核证**(C 源 + 参考桩双源,pcap
        落地后回填帧面档案)。
        """
        sequence = self._next_sequence()
        response = self._transact(
            codec.build_read_szl(codec.SZL_CPU_STATUS_ID, 0x0000, sequence)
        )
        entries = codec.parse_szl_response(
            response, sequence, codec.SZL_CPU_STATUS_ID, 0x0000
        )
        if len(entries) < 4:
            # 0x0424 记录 20 字节,不足 4 字节视为应答异常(防 IndexError 逃逸)
            raise codec.S7ProtocolError(_("S7 SZL 应答无记录(0x0424 状态读)"))
        # 记录区布局:ereig(2) + ae(1) + bzu_id(1) + res(4) + anlinfo(4) + time(8)
        name = _CPU_STATUS_NAMES.get(entries[3], "S7CpuStatusUnknown")
        log_op(self._debug_label, "cpu state → %r", name)
        return name

    def read_multi_vars(
        self, specs: "Sequence[Tuple[int, int, int, int]]"
    ) -> "List[bytes]":
        """多变量一次读(功能 0x04 多 Item 单 PDU,会话调用)。

        :param specs: ``(区码, DB 号, 字节起点, 字节数)`` 列表
            (≤ :data:`.codec.MAX_VARS`,S7 MAX_VARS 口径)
        :return: 与 specs 顺序一致的逐条字节
        :raises DeviceError: 在线但任一条目读取失败(条目级返回码非 0xFF)
        """
        sequence = self._next_sequence()
        request = codec.build_multi_read(specs, sequence)
        response = self._transact(request)
        byte_lengths = [size for _area, _db, _start, size in specs]
        blobs = codec.parse_read_response(response, sequence, len(specs), byte_lengths)
        log_op(self._debug_label, "multi read %d 项 → %d 项", len(specs), len(blobs))
        return [bytes(blob) for blob in blobs]


class SiemensS7Client(BaseClient):
    """西门子 S7 客户端(自研 S7comm 协议栈,型号参数化,核心零依赖)。

    型号进构造参数(对标三菱 MC 的 ``McFrame`` 形态):``model`` 驱动
    连接预设(本端 TSAP / 连接类型 / 缺省 rack·slot),``rack``/``slot``
    可显式覆写。S7-200/200 SMART 支持 ``V`` 区记号(= DB1)。

    :example::

        client = SiemensS7Client("192.168.0.1", model=S7Model.S7_1200)
        client.connect()
        ok, value = client.read_float("DB1.DBD6")
        ok = client.write_bool("DB1.DBX0.3", True)
        ok, text = client.read_string("DB1.DBS20", length=32)

        smart = SiemensS7Client("192.168.2.1", model=S7Model.S7_200_SMART)
        smart.connect()
        ok, value = smart.read_ushort("VW100")  # V 区 = DB1
    """

    _has_ping = True
    """支持探活(:meth:`get_cpu_state` SZL 读,零副作用)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.1",
        port: int = S7_DEFAULT_PORT,
        model: S7Model = S7Model.S7_1200,
        rack: Optional[int] = None,
        slot: Optional[int] = None,
    ) -> None:
        """初始化 S7 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param model: CPU 型号(:class:`~omniplc.core.types.S7Model`,缺省
            S7-1200)。型号驱动连接预设——300/400 槽位 2、1200/1500 槽位 1
            (PG 资源类型);200 SMART 本端 TSAP 0x1000 + S7 基本资源类型
            (远端 0x0300);经典 S7-200 仅限 CP243-1 以太网模块接入。
            1200/1500 与 200 SMART 须在 CPU 侧开启 PUT/GET 授权、DB 为
            非优化块
        :param rack: 机架号,缺省用型号预设(0);显式给出则覆写
        :param slot: 槽位号,缺省用型号预设(1200/1500=1、300/400=2、
            200 SMART=0);显式给出则覆写
        :raises ValueError: 参数非法

        .. note:: v0.52.x 的 ``dll_path`` 参数已随 同类开源封装 依赖退役
            移除(同类开源库 DLL 分发痛点正是自研动机);传递该参数会得到
            TypeError,请删除该实参。v0.53 构造签名第 3 参起为
            ``model``(原 rack/slot 位置实参须改键字传递)。
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        (
            self._local_tsap,
            self._remote_tsap,
            self._tpdu_size_code,
            resolved_rack,
            resolved_slot,
        ) = resolve_s7_connection(model, rack, slot)
        self._model = model
        self._rack = resolved_rack
        self._slot = resolved_slot

    @property
    def model(self) -> S7Model:
        """CPU 型号(构造参数,驱动连接预设)。"""
        return self._model

    @property
    def rack(self) -> int:
        """机架号。"""
        return self._rack

    @property
    def slot(self) -> int:
        """槽位号。"""
        return self._slot

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _S7Session:
        """取当前 S7 会话(仅事务锁内调用,内部方法)。"""
        transport = self._require_transport()
        if not isinstance(transport, _S7Session):
            raise TransportClosedError(_("S7 会话未建立"))
        return transport

    def _parse_address(self, address: str) -> S7Address:
        """解析地址,V 区记号按型号放行(内部方法)。

        S7-200/200 SMART 的 V 记号先翻译为 DB1 再走通用解析
        (:func:`~omniplc.plc.siemens.address.translate_v_address`);
        其余型号遇到 V 前缀直接拒(V 不是 300/400/1200/1500 的地址概念)。
        """
        text = address.strip() if isinstance(address, str) else ""
        if text[:1].upper() == "V":
            if self._model not in _V_MODELS:
                raise ValueError(
                    _(
                        "V 区地址仅 S7-200/200 SMART 支持:{!r}(其余型号请用 DB 记号)"
                    ).format(address)
                )
            return parse_s7_address(translate_v_address(text))
        return parse_s7_address(address)

    def _create_transport(self) -> BaseTransport:
        return _S7Session(
            self._ip_address,
            self._port,
            self._local_tsap,
            self._remote_tsap,
            self._tpdu_size_code,
            self._rack,
            self._slot,
        )

    # ------------------------------------------------------------------
    # 状态读与探活(SZL 0x0424)
    # ------------------------------------------------------------------

    def get_cpu_state(self) -> Tuple[bool, Optional[str]]:
        """读 CPU 运行状态(SZL 0x0424;零副作用)。

        返回 ``(是否成功, 状态枚举名)``,如 ``"S7CpuStatusRun"``/
        ``"S7CpuStatusStop"``(状态字节偏移待真机核证,见模块 docstring)。

        同时是 :meth:`ping` 的探测命令:能应答即 CPU 会话存活。
        """
        return self._execute(lambda: self._session().get_cpu_state())

    def _ping_probe(self) -> str:
        """探活探测命令:CPU 状态 SZL 读(内部方法)。"""
        return self._session().get_cpu_state()

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读数据项并按 DataType 尺寸收窄(大端序)。"""
        if data_type not in _SIZES:
            raise ValueError(_("S7 不支持的数据类型:{}").format(data_type))
        parsed = self._parse_address(address)
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    _("S7 按位读取需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(
                        address
                    )
                )
        elif parsed.bit is not None:
            raise ValueError(
                _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(
                    address
                )
            )
        data = self._session().read_area(
            area_code(parsed.area),
            parsed.db_number,
            parsed.byte_index,
            _SIZES[data_type],
        )
        if data_type is DataType.BOOL:
            # 前置校验已保证 bit 非空,or 0 仅供类型收窄
            return bool((data[0] >> (parsed.bit or 0)) & 1)
        if data_type is DataType.FLOAT:
            return struct.unpack(">f", data)[0]
        if data_type is DataType.DOUBLE:
            return struct.unpack(">d", data)[0]
        return int.from_bytes(
            data,
            "big",
            signed=data_type in (DataType.SHORT, DataType.INT, DataType.LONG),
        )

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """写数据项;位为锁内读-改-写,数值按大端编码;STRING 路由
        :meth:`_write_string`(含 PLC 侧声明长预检)。

        .. warning:: BOOL 写是**非原子读-改-写**(S7 协议按字节写):若 HMI
            或 PLC 程序同时修改同一字节的其它位,存在互踩风险。多写者场景
            请让同一字节只由一个写者负责(协议无单字节置位/复位原语)。
        """
        if data_type not in _SIZES:
            if data_type is DataType.STRING:
                if not isinstance(value, str):
                    raise ValueError(
                        _("字符串必须是 str,收到:{}").format(type(value).__name__)
                    )
                self._write_string(address, value, DEFAULT_STRING_ENCODING)
                return
            raise ValueError(_("S7 不支持的数据类型:{}").format(data_type))
        parsed = self._parse_address(address)
        session = self._session()
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    _("S7 按位写入需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(
                        address
                    )
                )
            flag = require_bool(value)
            raw = session.read_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
            )
            byte = (
                (raw[0] | (1 << parsed.bit)) if flag else (raw[0] & ~(1 << parsed.bit))
            )
            session.write_area(
                area_code(parsed.area),
                parsed.db_number,
                parsed.byte_index,
                bytes([byte & 0xFF]),
            )
            return
        if data_type is DataType.FLOAT:
            number = require_float(value)
            data = self._pack(">f", number)
        elif data_type is DataType.DOUBLE:
            number = require_float(value)
            data = self._pack(">d", number)
        else:
            number = require_int(value)
            data = self._pack(_INT_FORMATS[data_type], number)
        if parsed.bit is not None:
            raise ValueError(
                _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(
                    address
                )
            )
        session.write_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, data
        )

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读 S7 String(头 2 字节 = 声明长/实际长,正文按声明长)。

        实际长超出请求 ``length`` 时按 ``length`` 截断返回(不报错不丢帧)。
        """
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        size = length + 2
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, size
        )
        if len(data) < 2:
            raise DeviceError(_("S7 String 响应过短:{}").format(len(data)), 0)
        actual = data[1]
        if actual <= 0:
            return ""
        if actual > length:
            # PLC 侧实际长 > 请求 length:截断返回,避免静默丢成空串
            actual = length
        return convert.decode_string(data[2 : 2 + actual], encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写 S7 String(声明长字节保留 PLC 侧现值,仅覆盖实际长字节)。

        先读 1 字节取 PLC 侧声明长(STRING[x] 的 x),写入值超声明长时
        拒绝(防溢出污染相邻变量);声明长字节读得 0(未初始化区)时
        按本次编码长度落盘,且**回写的声明长字节 = 本次实际长**——等价
        把 PLC 侧声明长改写为实际值(有意保留的旧版兼容口径,正常
        STRING[x] 声明长非 0 不会走到;review-1005 §4.2 登记)。
        """
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        encoded = convert.encode_string(value, len(value.encode(encoding)), encoding)
        head = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
        )
        declared_max = head[0] if head else 0
        if declared_max == 0:
            # 未初始化区(声明长为 0 非法):退回旧口径,声明长=实际长
            # (review-1005 §4.2 登记);超 1 字节声明长字段显式拒绝,
            # 防裸 ValueError(review-1008 P3)
            if len(encoded) > 255:
                raise ValueError(
                    _("S7 String 写入值超出声明长字段上限 255,收到:{} 字符").format(
                        len(encoded)
                    )
                )
            declared_max = len(encoded)
        if len(encoded) > declared_max:
            raise ValueError(
                _("S7 String 写入值超出 PLC 侧声明长:{} > {} 字符({!r})").format(
                    len(encoded), declared_max, address
                )
            )
        header = bytes([declared_max, len(encoded)])
        self._session().write_area(
            area_code(parsed.area),
            parsed.db_number,
            parsed.byte_index,
            header + encoded,
        )
        return value

    def read_wstring(
        self, address: str, length: int = S7_WSTRING_DEFAULT_LENGTH
    ) -> Tuple[bool, Optional[str]]:
        """读 S7 WString(UTF-16BE,支持中文/日文等非 ASCII 文本)。

        WString 布局:声明长(2 字节,字符数)+ 实际长(2 字节)+ 字符
        (每字符 2 字节,UTF-16BE 大端)。实际长超出请求 ``length`` 时按
        ``length`` 截断返回。

        :param address: 字符串起点地址,如 ``"DB1.DBW20"``/``"DB1.DBS20"``
        :param length: 最多读取的字符数,默认 64
        :return: ``(是否成功, 文本)``;失败为 ``(False, None)``
        :raises ValueError: 地址/长度非法
        """
        if length <= 0:
            raise ValueError(_("length 必须大于 0,收到:{}").format(length))
        ok, value = self._execute(lambda: self._read_wstring_impl(address, int(length)))
        if not ok or value is None:
            return False, None
        return True, str(value)

    def write_wstring(self, address: str, value: str) -> bool:
        """写 S7 WString(UTF-16BE,保留 PLC 侧声明长,超声明长拒绝)。

        :param address: 字符串起点地址
        :param value: 待写入文本(不能为空;须为 BMP 字符,避免代理对歧义)
        :return: 是否成功
        :raises ValueError: 地址/值非法或超出 PLC 侧声明长
        """
        if not value:
            raise ValueError(_("value 不能为空字符串"))
        ok, _unused = self._execute(
            lambda: self._write_wstring_impl(address, str(value)), is_write=True
        )
        return ok

    def _read_wstring_impl(self, address: str, length: int) -> PrimitiveValue:
        if self._model not in _WSTRING_MODELS:
            raise ValueError(
                _(
                    "S7 WString 仅 S7-1200/1500 支持:{!r}(其余型号无该类型,读取会解出乱码)"
                ).format(address)
            )
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        size = 4 + length * 2
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, size
        )
        if len(data) < 4:
            raise DeviceError(_("S7 WString 响应过短:{}").format(len(data)), 0)
        actual = int.from_bytes(data[2:4], "big")
        if actual <= 0:
            return ""
        if actual > length:
            actual = length
        return convert.decode_string(data[4 : 4 + actual * 2], "utf-16-be")

    def _write_wstring_impl(self, address: str, value: str) -> PrimitiveValue:
        """写 S7 WString 实现(内部方法,异常经 :meth:`write_wstring` 翻译)。

        声明长读得 0(未初始化区)时回写声明长 = 本次实际长——与
        STRING 同款旧版兼容口径(有意保留,review-1005 §4.2 登记)。
        """
        if self._model not in _WSTRING_MODELS:
            raise ValueError(
                _(
                    "S7 WString 仅 S7-1200/1500 支持:{!r}(其余型号无该类型,读取会解出乱码)"
                ).format(address)
            )
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        encoded = value.encode("utf-16-be")
        if len(encoded) != len(value) * 2:
            raise ValueError(
                _("S7 WString 仅支持 BMP 字符(不含代理对):{!r}").format(address)
            )
        head = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, 2
        )
        declared_max = int.from_bytes(head[:2], "big") if len(head) >= 2 else 0
        if declared_max == 0:
            # 回退口径与 STRING 同款;超 2 字节声明长字段显式拒绝(review-1008 P3)
            if len(value) > 65535:
                raise ValueError(
                    _("S7 WString 写入值超出声明长字段上限 65535,收到:{} 字符").format(
                        len(value)
                    )
                )
            declared_max = len(value)
        if len(value) > declared_max:
            raise ValueError(
                _("S7 WString 写入值超出 PLC 侧声明长:{} > {} 字符({!r})").format(
                    len(value), declared_max, address
                )
            )
        header = declared_max.to_bytes(2, "big") + len(value).to_bytes(2, "big")
        self._session().write_area(
            area_code(parsed.area),
            parsed.db_number,
            parsed.byte_index,
            header + encoded,
        )
        return value

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:同区域字节起点起连续 ``count`` 个元素,Read Var 单事务。

        地址只定位**区域 + 字节起点**,总字节数 = ``count × 类型字节数``
        (SHORT/USHORT 2、INT/UINT/FLOAT 4、LONG/ULONG/DOUBLE 8,大端),
        按类型尺寸切片解码。
        总字节数不设入参上限(review-1002 P3):超出单请求 PDU 容量时经
        会话层自动分片循环读回拼接(review-1008:与旧封装行为对齐),
        任一片失败即整批 ``(False, None)``(非入参期 ``ValueError``)。
        BOOL 连续读无位语义(单个字节内的位不构成连续序列),不支持;
        STRING 变长不支持(请用 :meth:`read_string`)。

        :param address: 起始字节地址(如 ``"DB1.DBB0"``、``"MW20"``;
            ``DBX``/位号记号不支持)
        :param count: 元素个数(按 ``data_type`` 计,INT×10 = 40 字节)
        :param data_type: 数据类型(数值类型)
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 位地址
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        data_type_enum = DataType.coerce(data_type)
        if data_type_enum is DataType.STRING:
            raise ValueError(_("read_range 不支持 STRING,请用 read_string"))
        if data_type_enum not in _SIZES or data_type_enum is DataType.BOOL:
            raise ValueError(
                _("S7 read_range 不支持的数据类型:{}").format(data_type_enum)
            )
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(
                _("S7 read_range 不支持位地址:{!r}(位访问请逐点读)").format(address)
            )
        size = _SIZES[data_type_enum]

        def operation() -> List[PrimitiveValue]:
            data = self._session().read_area(
                area_code(parsed.area),
                parsed.db_number,
                parsed.byte_index,
                size * count,
            )
            values: List[PrimitiveValue] = []
            for index in range(count):
                blob = data[index * size : (index + 1) * size]
                if data_type_enum is DataType.FLOAT:
                    values.append(struct.unpack(">f", blob)[0])
                elif data_type_enum is DataType.DOUBLE:
                    values.append(struct.unpack(">d", blob)[0])
                else:
                    values.append(
                        int.from_bytes(
                            blob,
                            "big",
                            signed=data_type_enum
                            in (DataType.SHORT, DataType.INT, DataType.LONG),
                        )
                    )
            return values

        ok, values = self._execute(operation)
        if not ok or values is None:
            return False, None
        return True, values

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 Read Var 多 Item 单事务(一次 PDU 组包)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        条目上限 20(:data:`.codec.MAX_VARS`;S7 ReadMultiVars 每请求 20 项)。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = self.read_batch(
            [(address, data_type_enum) for address in addresses]
        )
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多变量批量读取:Read Var 多 Item 单 PDU 混读(DB/I/Q/M)。

        每个条目独立寻址(区域/DB/字节起点可不同);BOOL 读 1 字节后本地
        提位;STRING 为变长不支持批量(请用 :meth:`read_string`)。条目上限
        **20**(:data:`.codec.MAX_VARS`;S7 ReadMultiVars 每请求 20 项)。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空/地址或类型非法/条目数超限
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        if len(items) > S7_MAX_MULTI_VARS:
            raise ValueError(
                _("S7 多变量读条目数超出上限 {}:{}(MAX_VARS)").format(
                    S7_MAX_MULTI_VARS, len(items)
                )
            )
        specs: List[Tuple[int, int, int, int]] = []
        plan: List[Tuple[str, int, Optional[int], DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            if data_type_enum not in _SIZES:
                raise ValueError(
                    _("S7 批量读取不支持的数据类型:{}").format(data_type_enum)
                )
            parsed = self._parse_address(address)
            if data_type_enum is DataType.BOOL:
                if parsed.bit is None:
                    raise ValueError(
                        _("S7 按位读取需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(
                            address
                        )
                    )
                plan.append(("bit", len(specs), parsed.bit, data_type_enum))
            else:
                if parsed.bit is not None:
                    raise ValueError(
                        _(
                            "S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)"
                        ).format(address)
                    )
                plan.append(("word", len(specs), None, data_type_enum))
            specs.append(
                (
                    area_code(parsed.area),
                    parsed.db_number,
                    parsed.byte_index,
                    _SIZES[data_type_enum],
                )
            )

        def operation() -> List[PrimitiveValue]:
            blobs = self._session().read_multi_vars(specs)
            values: List[PrimitiveValue] = []
            for kind, index, bit, data_type_enum in plan:
                blob = blobs[index]
                if kind == "bit":
                    values.append(bool((blob[0] >> (bit or 0)) & 1))
                elif data_type_enum is DataType.FLOAT:
                    values.append(struct.unpack(">f", blob)[0])
                elif data_type_enum is DataType.DOUBLE:
                    values.append(struct.unpack(">d", blob)[0])
                else:
                    values.append(
                        int.from_bytes(
                            blob,
                            "big",
                            signed=data_type_enum
                            in (DataType.SHORT, DataType.INT, DataType.LONG),
                        )
                    )
            return values

        return self._execute(operation)

    @staticmethod
    def _pack(fmt: str, value: PrimitiveValue) -> bytes:
        """大端打包,越界 struct 报错统一转 ValueError(内部方法)。"""
        try:
            return struct.pack(fmt, value)
        except (struct.error, OverflowError) as exc:
            raise ValueError(_("S7 写入值超出类型范围:{}").format(value)) from exc
