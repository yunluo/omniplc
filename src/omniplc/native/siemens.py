"""原生 asyncio 西门子 S7 客户端(自研 S7comm 栈,会话型适配)。

与同步 :mod:`omniplc.plc.siemens.client` 的关系(原生层惯例):

- **帧面零复刻**:TPKT/COTP/S7 三层编解码全部复用同步侧
  :mod:`omniplc.plc.siemens.codec` 纯函数——两侧请求帧逐字节相同,
  由 ``tests/unit/test_native_siemens.py`` 对拍守卫锁定;
- **会话逻辑镜像**:`AsyncS7Session` 逐方法镜像同步 ``_S7Session``
  (三步握手 / TPKT 会话事务 / 读写自动分片 / SZL 状态),差异只在
  传输调用换 ``await``(:class:`AsyncTcpTransport`);
- 错误边界与同步一致:TCP 层 OSError 透传(基类惰性重连)、坏帧
  (:class:`.codec.S7ProtocolError`,属 ``ProtocolFrameError``)拆连
  重同步、PLC 侧拒绝(DeviceError)不断线,DB 区附优化块提示。

S7 为**会话型协议**(一连接一 S7 会话,非字节流):``AsyncS7Session``
是 :class:`AsyncBaseTransport` 的会话适配(同同步 ``_S7Session`` 形态),
``send``/``recv`` 字节流接口显式拒绝;事务经客户端基类 ``_execute``
的 asyncio 事务锁串行,取消语义(已发请求 → 保守拆连)由基类统一处理。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple, Union

from ..core import convert
from ..core.base_client import DEFAULT_STRING_ENCODING, validate_endpoint
from ..core.constants import (
    S7_DEFAULT_PORT,
    S7_MAX_MULTI_VARS,
    S7_WSTRING_DEFAULT_LENGTH,
)
from ..core.debug import log_op
from ..core.errors import DeviceError, TransportClosedError
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue, S7Model
from ..core.validation import require_bool, require_float, require_int
from ..plc.siemens import codec
from ..plc.siemens.address import (
    S7Address,
    area_code,
    parse_s7_address,
    translate_v_address,
)
from ..plc.siemens.client import (
    _CPU_STATUS_NAMES,
    _INT_FORMATS,
    _SIZES,
    _V_MODELS,
    _WSTRING_MODELS,
    resolve_s7_connection,
)
from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport


class AsyncS7Session(AsyncBaseTransport):
    """S7comm 会话(原生 asyncio,适配为传输对象外形;私有)。

    供 :class:`AsyncBaseClient` 的连接状态机直接管理——``connect`` 完成
    TCP → COTP(CR/CC,TSAP 编码 rack/slot)→ S7 通信协商(PDU 长度)
    三步,``close`` 尽力发 COTP DR 后关传输;区域读写经
    :meth:`read_area` / :meth:`write_area`(统一 BYTE 传输尺寸,自动
    按 PDU 分片),错误边界与同步 ``_S7Session`` 逐条一致。

    ``receive_timeout`` 已连接时立即下发到底层 TCP 传输(与同步同口径)。
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
        """初始化 S7 会话(与同步 ``_S7Session`` 同签名同语义)。

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
        self._tcp: Optional[AsyncTcpTransport] = None
        self._sequence = 0
        self.pdu_size = codec.MAX_PDU_REQUEST
        self._dst_ref = 0
        """CC 应答回显的对端引用,close 发 COTP DR 时回填(connection.py
        L431-450 用 CC 回给的 dst_ref;默认 0,DR 为尽力而为语义)。"""
        self._debug_label = "s7://{}:{}(机架{}槽位{})".format(
            ip_address, port, rack, slot
        )

    @AsyncBaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒);已连接时立即下发到底层 TCP 传输。"""
        if seconds <= 0:
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
        AsyncBaseTransport.receive_timeout.fset(self, float(seconds))  # type: ignore[attr-defined]
        tcp = self._tcp
        if tcp is not None:
            tcp.receive_timeout = float(seconds)

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """三步建连:TCP → COTP(CR/CC)→ S7 协商。

        连接超时取本会话 ``connect_timeout``(基类创建会话时已同步到
        会话属性,此处下发到底层 TCP 传输;与同步侧 review-1008 修复同口径)。

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
        tcp = AsyncTcpTransport(self._ip_address, self._port)
        try:
            tcp.connect_timeout = self._connect_timeout
            await tcp.connect()
            tcp.receive_timeout = self._receive_timeout
            self._tcp = tcp  # 先挂会话(协商事务经 _require_tcp 取传输)
            await tcp.send(
                codec.build_tpkt(
                    codec.build_cotp_cr(
                        self._remote_tsap, self._local_tsap, self._tpdu_size_code
                    )
                )
            )
            length = codec.parse_tpkt_header(await tcp.recv(4))
            self._dst_ref = codec.parse_cotp_cc(
                await tcp.recv(length - 4), self._remote_tsap
            )
            self._sequence = 0
            sequence = self._next_sequence()
            response = await self._transact(
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
        """关闭传输,幂等(同步接口;COTP DR 由 client 断开钩子经
        :meth:`send_dr` 异步尽力发送——原生传输层发送是协程,同步
        ``close`` 里发不了)。

        传输失败拆连路径(基类 ``_mark_disconnected`` 直调本方法)**不发
        DR**:半开链路上 DR 本就不可达(同步侧发了也会被静默吞掉,等效
        空操作);显式 :meth:`AsyncSiemensS7Client.disconnect` 经
        ``_disconnect_locked`` 钩子在链路存活时发送(告知 PLC 释放 PG
        连接资源,S7 连接数上限场景有意义)。"""
        tcp, self._tcp = self._tcp, None
        if tcp is None:
            return
        try:
            tcp.close()
        except OSError:
            pass
        log_op(self._debug_label, "会话已断开")

    async def send_dr(self) -> None:
        """尽力发 COTP DR 断连请求(内部;client 断开钩子调用)。

        DR 语义尽力而为:对端半开/发送失败由调用方吞掉,不影响断开流程;
        dst_ref = CC 应答回显值(python-snap7 connection.py L431-450 同款)。
        """
        tcp = self._tcp
        if tcp is None:
            return
        dr = struct.pack(
            ">BBHHBB", 6, 0x80, self._dst_ref, codec.SRC_REFERENCE, 0x00, 0x00
        )
        await tcp.send(codec.build_tpkt(dr))

    def _next_sequence(self) -> int:
        """PDU 引用 u16 循环递增(内部方法,会话事务内调用)。"""
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    async def _transact(self, request: bytes) -> bytes:
        """完整事务:COTP DT 包裹发送 → 收 TPKT 剥 COTP 返回 S7 PDU(内部)。"""
        tcp = self._require_tcp()
        await tcp.send(codec.build_tpkt(codec.build_cotp_dt(request)))
        length = codec.parse_tpkt_header(await tcp.recv(4))
        return codec.parse_cotp_dt(await tcp.recv(length - 4))

    def _require_tcp(self) -> AsyncTcpTransport:
        """取底层 TCP 传输,未建立则抛出(内部方法)。"""
        if self._tcp is None:
            raise TransportClosedError(_("S7 会话未建立"))
        return self._tcp

    # ------------------------------------------------------------------
    # 会话调用(区域读写 / 状态 / 批量)
    # ------------------------------------------------------------------

    async def send(self, data: bytes) -> None:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    async def recv(self, size: int) -> bytes:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    async def read_area(
        self, area: int, db_number: int, start: int, size: int
    ) -> bytes:
        """读一块区域字节(会话调用;统一 BYTE 传输尺寸,与同步层口径一致)。

        跨度超过单请求 PDU 容量(协商值 - 18 字节读侧开销,至少 1)时
        **自动分片**循环读回拼接(python-snap7 3.2.0 client.py L997-1063
        `read_area` 同款行为,client_base.py L199-208 容量公式);任一片
        失败即整块失败(异常上抛,调用方按各自容错口径处理)。
        """
        chunks = []
        offset = 0
        while offset < size:
            chunk = min(size - offset, max(1, self.pdu_size - 18))
            chunks.append(
                await self._read_area_once(area, db_number, start + offset, chunk)
            )
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

    async def _read_area_once(
        self, area: int, db_number: int, start: int, size: int
    ) -> bytes:
        """单事务读一块区域字节(分片原子操作,内部方法)。"""
        sequence = self._next_sequence()
        request = codec.build_read(
            area, db_number, start * 8, codec.WORD_LEN_BYTE, size, sequence
        )
        response = await self._transact(request)
        try:
            data = codec.parse_read_response(response, sequence, 1, [size])[0]
        except DeviceError as exc:
            raise self._with_db_hint(area, db_number, exc) from exc
        return bytes(data)

    async def write_area(
        self, area: int, db_number: int, start: int, data: bytes
    ) -> None:
        """写一块区域字节(会话调用)。

        跨度超过单请求 PDU 容量(协商值 - 35 字节写侧开销,至少 1)时
        **自动分片**顺序写(python-snap7 3.2.0 `write_area` 同款行为,
        client_base.py L210-219 容量公式);分片中途失败时前片已落盘
        (部分写,协议无跨片原子性),调用方按整块异常感知。
        """
        data = bytes(data)
        offset = 0
        while offset < len(data):
            chunk = min(len(data) - offset, max(1, self.pdu_size - 35))
            await self._write_area_once(
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

    async def _write_area_once(
        self, area: int, db_number: int, start: int, data: bytes
    ) -> None:
        """单事务写一块区域字节(分片原子操作,内部方法)。"""
        sequence = self._next_sequence()
        request = codec.build_write(
            area, db_number, start * 8, codec.WORD_LEN_BYTE, data, sequence
        )
        response = await self._transact(request)
        try:
            codec.parse_write_response(response, sequence, 1)
        except DeviceError as exc:
            raise self._with_db_hint(area, db_number, exc) from exc

    @staticmethod
    def _with_db_hint(area: int, db_number: int, exc: DeviceError) -> DeviceError:
        """DB 区绝对访问被 PLC 拒绝时附优化块访问提示(与同步侧同口径)。

        S7-1200/1500 默认优化块无绝对地址,报错常被误读为通信故障——
        提示指向 TIA 设置(review-1008 P2:同步侧自研重写时曾丢失)。
        """
        if area == codec.AREA_DB and db_number:
            return DeviceError(
                str(exc)
                + _("(按绝对地址访问 DB 失败:若为 S7-1200/1500,请确认该 DB ")
                + _("已在 TIA 中取消 Optimized block access)"),
                exc.code,
            )
        return exc

    async def get_cpu_state(self) -> str:
        """读 CPU 运行状态(SZL 0x0424,零副作用;同时是探活探测命令)。

        返回状态枚举名;状态取记录区第 4 字节 bzu_id(与同步侧同口径,
        review-1008 P1 修正),偏移待真机核证。
        """
        sequence = self._next_sequence()
        response = await self._transact(
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

    async def read_multi_vars(
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
        response = await self._transact(request)
        byte_lengths = [size for _area, _db, _start, size in specs]
        blobs = codec.parse_read_response(response, sequence, len(specs), byte_lengths)
        log_op(self._debug_label, "multi read %d 项 → %d 项", len(specs), len(blobs))
        return [bytes(blob) for blob in blobs]


class AsyncSiemensS7Client(AsyncBaseClient):
    """西门子 S7 客户端(原生 asyncio;自研 S7comm 协议栈,核心零依赖)。

    公开面与同步 :class:`~omniplc.plc.siemens.client.SiemensS7Client`
    一致(方法皆为协程);帧面/校验/错误边界由共享 codec 纯函数与
    会话镜像保证两侧逐字节一致。

    :example::

        client = AsyncSiemensS7Client("192.168.0.1", model=S7Model.S7_1200)
        await client.connect()
        ok, value = await client.read_float("DB1.DBD6")
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
        """初始化 S7 客户端(签名与同步侧一致;型号驱动连接预设)。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param model: CPU 型号(:class:`~omniplc.core.types.S7Model`,缺省
            S7-1200;语义与同步侧 :class:`~omniplc.plc.siemens.SiemensS7Client`
            一致)
        :param rack: 机架号,缺省用型号预设(0);显式给出则覆写
        :param slot: 槽位号,缺省用型号预设;显式给出则覆写
        :raises ValueError: 参数非法

        .. note:: 构造签名第 3 参起为 ``model``(原 rack/slot 位置实参须
            改键字传递)。
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

    def _session(self) -> AsyncS7Session:
        """取当前 S7 会话(仅事务锁内调用,内部方法)。"""
        transport = self._require_transport()
        if not isinstance(transport, AsyncS7Session):
            raise TransportClosedError(_("S7 会话未建立"))
        return transport

    def _parse_address(self, address: str) -> S7Address:
        """解析地址,V 区记号按型号放行(内部方法;与同步侧同口径)。"""
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

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncS7Session(
            self._ip_address,
            self._port,
            self._local_tsap,
            self._remote_tsap,
            self._tpdu_size_code,
            self._rack,
            self._slot,
        )

    async def _disconnect_locked(self) -> bool:
        """断开实现:先尽力发 COTP DR(会话关闭语义),再走基类清理(内部)。"""
        transport = self._transport
        if isinstance(transport, AsyncS7Session):
            try:
                await transport.send_dr()
            except Exception:
                pass  # DR 尽力而为:对端已断/发送失败都不阻断断开流程
        return await super()._disconnect_locked()

    async def _ping_probe(self) -> str:
        """探活探测命令:CPU 状态 SZL 读(内部方法)。"""
        return await self._session().get_cpu_state()

    # ------------------------------------------------------------------
    # 单点读写(类型化方法经基类路由到 _read/_write 钩子)
    # ------------------------------------------------------------------

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读数据项;数值大端解码,位读 1 字节后提位(与同步侧同口径)。"""
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
        else:
            if parsed.bit is not None:
                raise ValueError(
                    _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(
                        address
                    )
                )
        data = await self._session().read_area(
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

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
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
                await self._write_string(address, value, DEFAULT_STRING_ENCODING)
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
            raw = await session.read_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
            )
            byte = (
                (raw[0] | (1 << parsed.bit)) if flag else (raw[0] & ~(1 << parsed.bit))
            )
            await session.write_area(
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
        await session.write_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, data
        )

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """读 S7 String(头 2 字节 = 声明长/实际长,正文按声明长;与同步同口径)。

        实际长超出请求 ``length`` 时按 ``length`` 截断返回(不报错不丢帧)。
        """
        parsed = self._parse_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        size = length + 2
        data = await self._session().read_area(
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

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
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
        head = await self._session().read_area(
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
        await self._session().write_area(
            area_code(parsed.area),
            parsed.db_number,
            parsed.byte_index,
            header + encoded,
        )
        return value

    # ------------------------------------------------------------------
    # WString(UTF-16BE,4 字节头)
    # ------------------------------------------------------------------

    async def read_wstring(
        self, address: str, length: int = S7_WSTRING_DEFAULT_LENGTH
    ) -> Tuple[bool, Optional[str]]:
        """读 S7 WString(UTF-16BE,支持中文/日文等非 ASCII 文本)。

        WString 布局:声明长(2 字节,字符数)+ 实际长(2 字节)+ 字符
        (每字符 2 字节,UTF-16BE 大端)。实际长超出请求 ``length`` 时按
        ``length`` 截断返回。
        """
        ok, value = await self._execute(
            lambda: self._read_wstring_impl(address, length)
        )
        if not ok or value is None:
            return False, None
        return True, str(value)

    async def _read_wstring_impl(self, address: str, length: int) -> PrimitiveValue:
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
        data = await self._session().read_area(
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

    async def write_wstring(self, address: str, value: str) -> bool:
        """写 S7 WString(UTF-16BE,保留 PLC 侧声明长,超声明长拒绝)。

        :param address: 字符串起点地址
        :param value: 待写入文本(不能为空;须为 BMP 字符,避免代理对歧义)
        :return: 是否成功
        :raises ValueError: 地址/值非法或超出 PLC 侧声明长
        """
        if not value:
            raise ValueError(_("value 不能为空字符串"))
        ok, _unused = await self._execute(
            lambda: self._write_wstring_impl(address, str(value)), is_write=True
        )
        return ok

    async def _write_wstring_impl(self, address: str, value: str) -> PrimitiveValue:
        """写 S7 WString 实现(内部方法;声明长回退口径与 STRING 同款)。"""
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
        head = await self._session().read_area(
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
        await self._session().write_area(
            area_code(parsed.area),
            parsed.db_number,
            parsed.byte_index,
            header + encoded,
        )
        return value

    # ------------------------------------------------------------------
    # 批量与连续读
    # ------------------------------------------------------------------

    async def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 Read Var 多 Item 单事务(一次 PDU 组包)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        条目上限 20(:data:`.codec.MAX_VARS`;S7 ReadMultiVars 每请求 20 项)。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = await self.read_batch(
            [(address, data_type_enum) for address in addresses]
        )
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    async def read_batch(
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

        async def operation() -> List[PrimitiveValue]:
            blobs = await self._session().read_multi_vars(specs)
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

        ok, values = await self._execute(operation)
        if not ok or values is None:
            return False, None
        return True, values

    async def read_range(
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
        会话层自动分片循环读回拼接(与同步侧 review-1008 修复同口径),
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

        async def operation() -> List[PrimitiveValue]:
            data = await self._session().read_area(
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

        ok, values = await self._execute(operation)
        if not ok or values is None:
            return False, None
        return True, values

    # ------------------------------------------------------------------
    # 状态读与探活(SZL 0x0424)
    # ------------------------------------------------------------------

    async def get_cpu_state(self) -> Tuple[bool, Optional[str]]:
        """读 CPU 运行状态(SZL 0x0424;零副作用)。

        返回 ``(是否成功, 状态枚举名)``,如 ``"S7CpuStatusRun"``/
        ``"S7CpuStatusStop"``/``"S7CpuStatusUnknown"``;语义与同步侧一致。
        """
        ok, name = await self._execute(lambda: self._session().get_cpu_state())
        if not ok or name is None:
            return False, None
        return True, str(name)

    @staticmethod
    def _pack(fmt: str, value: PrimitiveValue) -> bytes:
        """大端打包,越界 struct 报错统一转 ValueError(内部方法)。"""
        try:
            return struct.pack(fmt, value)
        except (struct.error, OverflowError) as exc:
            raise ValueError(_("S7 写入值超出类型范围:{}").format(value)) from exc
