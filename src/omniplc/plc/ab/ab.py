"""罗克韦尔 Allen-Bradley EtherNet/IP(CIP)客户端——Logix 标签读写。

协议要点(按 CIP/EtherNet/IP 规范核证,见 architecture.md §8.1):

- TCP ``44818``,连接后先注册 CIP 会话(RegisterSession,``_after_connect``
  钩子,与 FINS/TCP 握手同构);断线惰性重连时自动重新注册
- 两种消息通道,``connected_messaging`` 参数选择:

  - **unconnected(默认)**:SendRRData + Unconnected Send(0x52)包裹,
    背板路由到 ``slot`` 槽号——无连接状态,重连零恢复
  - **connected**:Forward Open(优先 Large 4002,被拒回落普通 504)建立
    Class 3 连接,标签读写走 SendUnitData(O->T 连接 ID + 递增序列号);
    disconnect 尽力 Forward Close。单事务开销更小,大批量轮询时吞吐更高

- Logix 标签**自描述**:首次访问先读 1 个元素获取实际类型(按基名缓存),
  请求类型与实际类型不符抛 ``ValueError``;写请求需携带类型码,故写前必查
- ``Tag.3`` 位访问:整型标签读词提位 / ``0x4E`` 读-改-写原子位写;
  BOOL 数组元素(存储类型 DWORD)按 ``下标 // 32`` 定词、``% 32`` 定位
- STRING 走结构体(0xA0,模板 0x0FCE),``len(u32) + 82 字符`` 布局

地址语法见 :mod:`.address`;多级成员/数组下标/程序作用域均原样透传。
批量读取(0x0A 多服务包,``read_batch``/``read_many`` 覆写)已实现;
UDT 整体读取、分片读写在 v1.x 规划。

继承定制点:`_route_path` / `_wrap_unconnected` / `_parse_unconnected_reply`,
欧姆龙 NJ/NX CIP(:mod:`omniplc.plc.omron.cip`)即据此覆写三处走线差异。
"""
from __future__ import annotations

import random
import struct
from typing import List, Optional, Sequence, Tuple, Union

from . import codec_cip
from .codec_cip import CIP_CLASS_IDENTITY, CIP_INSTANCE_IDENTITY
from .address import AbTag, parse_ab_tag
from ...core.base_client import BaseClient, _categorize, _extract_code, validate_endpoint
from ...core.constants import (
    AB_EIP_DEFAULT_PORT,
    AB_EIP_DEFAULT_SLOT,
    AB_EIP_MAX_FRAME,
    AB_EIP_ORIGINATOR_VENDOR_ID,
    AB_EIP_SLOT_MAX,
    AB_MAX_BATCH_PAYLOAD,
    AB_MAX_BATCH_SERVICES,
    INT32_MAX,
    UINT16_MAX,
)
from ...core.debug import format_hex, log_op
from ...core.errors import DeviceError, OmniPLCInternalError, ProtocolFrameError
from ...core.validation import require_bool
from ...transport import BaseTransport, TcpTransport
from ...types import DataType, PrimitiveValue


class AllenBradleyEthIpClient(BaseClient):
    """Allen-Bradley Logix 系列以太网/IP 客户端(ControlLogix/CompactLogix)。

    :example::

        client = AllenBradleyEthIpClient("192.168.1.20", 44818, slot=0)
        client.connect()
        ok, value = client.read_int("MyDint")
        ok = client.write_bool("MyBool", True)
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.20",
        port: int = AB_EIP_DEFAULT_PORT,
        slot: int = AB_EIP_DEFAULT_SLOT,
        connected_messaging: bool = False,
        rpi_us: int = codec_cip.FO_OT_RPI,
    ) -> None:
        """初始化 AB EtherNet/IP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,EtherNet/IP 默认 44818
        :param slot: CPU 槽号(内置以太网口机型为 0;1756 背板按实际槽位)
        :param connected_messaging: True 走 connected 消息(Forward Open +
            SendUnitData);默认 False 走 unconnected 消息
        :param rpi_us: connected 连接的请求包间隔 RPI(微秒),默认 100ms;
            影响连接空闲超时(约 4×RPI),轮询间隔大时应相应调大
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        BaseClient.__init__(self, ip_address, port)
        slot = int(slot)
        if not 0 <= slot <= AB_EIP_SLOT_MAX:
            raise ValueError(
                f"槽号超出范围 0~{AB_EIP_SLOT_MAX}:{slot}"
            )
        if int(rpi_us) <= 0:
            raise ValueError(f"rpi_us 必须大于 0,收到:{rpi_us}")
        self._slot = slot
        self._connected_messaging = bool(connected_messaging)
        self._session_handle = 0
        self._known_types: dict = {}
        self._connection_serial = 0
        self._ot_connection_id: Optional[int] = None
        self._to_connection_id = 0
        self._connection_size: Optional[int] = None
        self._sequence = 0
        self._rpi_us = int(rpi_us)
        self._originator_serial = random.randrange(1, UINT16_MAX)

    @property
    def slot(self) -> int:
        """CPU 槽号(Unconnected Send 背板路由)。"""
        return self._slot

    @property
    def connected_messaging(self) -> bool:
        """是否走 connected 消息(Forward Open + SendUnitData)。"""
        return self._connected_messaging

    @property
    def connection_size(self) -> Optional[int]:
        """生效连接尺寸(connected 模式 Forward Open 后可用,其余为 None)。"""
        return self._connection_size

    # ------------------------------------------------------------------
    # 连接管理:会话注册/注销 + Forward Open/Close
    # ------------------------------------------------------------------

    def _after_connect(self) -> None:
        """CIP 会话注册;connected 模式随后建立 Class 3 连接(内部方法)。"""
        self._session_handle = 0
        self._ot_connection_id = None
        self._connection_size = None
        transport = self._require_transport()
        transport.send(codec_cip.build_register_session())
        self._session_handle = codec_cip.parse_register_session(self._recv_frame())
        if self._connected_messaging:
            self._forward_open()

    def _after_connect_failure(self) -> None:
        """连接初始化失败清理:尽力 Forward Close + 注销 CIP 会话(内部方法)。

        基类清理路径只关传输,注册过的会话要等 PLC 侧超时才回收;反复失败
        重连会耗尽 PLC 会话表(ControlLogix 典型 ≤16)。失败路径下
        ``_ot_connection_id`` 必为 None(Forward Open 未成功),
        :meth:`_forward_close` 自然空转;注销发送失败静默(同
        :meth:`_unregister_session` 口径:发不出去就随 TCP 关闭由 PLC 回收)。
        """
        self._forward_close()
        self._unregister_session()

    def disconnect(self) -> bool:
        """断开连接:尽力 Forward Close(connected)并注销会话后关闭传输(幂等)。"""
        with self._lock:
            self._forward_close()
            self._unregister_session()
        return super().disconnect()

    def _forward_open(self) -> None:
        """Forward Open:优先 Large(4002),被拒回落普通(504)(内部方法)。"""
        transport = self._require_transport()
        self._connection_serial = (self._connection_serial + 1) & 0xFFFF

        for is_large, size in (
            (True, codec_cip.CONNECTION_SIZE_LARGE),
            (False, codec_cip.CONNECTION_SIZE_NORMAL),
        ):
            to_connection_id = random.randrange(1, UINT16_MAX)
            request = codec_cip.build_forward_open(
                is_large,
                size,
                self._connection_serial,
                to_connection_id,
                AB_EIP_ORIGINATOR_VENDOR_ID,
                self._originator_serial,
                self._route_path(),
                self._rpi_us,
            )
            service = (
                codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN
                if is_large
                else codec_cip.CIP_SERVICE_FORWARD_OPEN
            )
            transport.send(codec_cip.build_rr_data(self._session_handle, request))
            status, ot_id = codec_cip.parse_forward_open_reply(
                self._recv_frame(), service
            )
            if status == 0:
                self._ot_connection_id = ot_id
                self._to_connection_id = to_connection_id
                self._connection_size = size
                self._sequence = 0
                return
        raise ProtocolFrameError(
            "Forward Open 失败:CIP 状态 0x{:02X}({})".format(
                status, codec_cip.status_text(status)
            )
        )

    def _forward_close(self) -> None:
        """尽力发送 Forward Close(异常静默,内部方法)。

        应答解析后仅对**非 0 状态**记一条调试日志(关闭是尽力而为,不改变
        流程);解析/收发异常一律吞掉。
        """
        transport = self._transport
        ot_id = self._ot_connection_id
        self._ot_connection_id = None
        self._connection_size = None
        if transport is None or not self._connected_messaging or ot_id is None:
            return
        if not self._session_handle:
            return
        try:
            transport.send(
                codec_cip.build_rr_data(
                    self._session_handle,
                    codec_cip.build_forward_close(
                        self._connection_serial,
                        AB_EIP_ORIGINATOR_VENDOR_ID,
                        self._originator_serial,
                        self._route_path(),
                    ),
                )
            )
            status = codec_cip.parse_forward_close_reply(self._recv_frame())
            if status != 0:
                log_op(
                    "ab://{}:{}".format(self._ip_address, self._port),
                    "forward-close 非 0 状态 0x{:02X}".format(status),
                )
        except (OSError, OmniPLCInternalError):
            pass

    def _unregister_session(self) -> None:
        """尽力发送 UnregisterSession(内部方法)。

        规范上该命令**无应答**——发完即收,不得等应答(等了会把关闭流程
        卡到收包超时;常见实现同此教训,有的干脆
        不发直接断 TCP)。发送失败静默:会话随 TCP 关闭由 PLC 侧超时回收。
        """
        transport = self._transport
        handle = self._session_handle
        self._session_handle = 0
        if transport is None or not handle:
            return
        try:
            transport.send(codec_cip.build_unregister_session(handle))
        except (OSError, OmniPLCInternalError):
            pass

    def _recv_frame(self) -> bytes:
        """按 ENIP 长度域收完整帧:24 字节头 + 声明长度(内部方法)。

        :raises ProtocolFrameError: 长度域超限(坏帧快失败,防按声明长收包)
        """
        transport = self._require_transport()
        head = transport.recv(codec_cip.EIP_HEADER_SIZE)
        length = int.from_bytes(head[2:4], "little")
        if length > AB_EIP_MAX_FRAME:
            raise ProtocolFrameError(
                "ENIP 长度域超限:{} > {}(收到的原始帧头:{})".format(
                    length, AB_EIP_MAX_FRAME, format_hex(head)
                )
            )
        return head + transport.recv(length)

    def _next_sequence(self) -> int:
        """connected 序列号递增(1~65535 回绕,内部方法)。"""
        return self._bump_id("_sequence", 16)

    def _route_path(self) -> bytes:
        """Forward Open/Close 连接路径的路由段(内部方法,继承定制点)。

        AB 目标 CPU 在背板上:背板端口(0x01)+ 槽号。
        """
        return bytes((0x01, self._slot))

    def _wrap_unconnected(self, cip_request: bytes) -> bytes:
        """unconnected 请求封装:UC Send 包裹 + 背板路由(内部方法,继承定制点)。"""
        return codec_cip.build_uc_send(cip_request, self._slot)

    def _parse_unconnected_reply(self, reply: bytes, request_service: int) -> bytes:
        """unconnected 应答解析:剥 UC Send 应答两层头(内部方法,继承定制点)。"""
        return codec_cip.parse_service_reply(reply, request_service)

    def _transact(self, cip_request: bytes, request_service: int) -> bytes:
        """CIP 事务:按消息通道封装发送并解析应答数据域(内部方法)。

        unconnected:子类封装(AB 为 UC Send 包裹)→ RRData;
        connected:SendUnitData。

        :raises DeviceError: CIP 状态非 0(不断线)
        :raises ProtocolFrameError: 坏帧(标记断开惰性重连)
        """
        transport = self._require_transport()
        if self._ot_connection_id is None:
            frame = codec_cip.build_rr_data(
                self._session_handle, self._wrap_unconnected(cip_request)
            )
            transport.send(frame)
            return self._parse_unconnected_reply(self._recv_frame(), request_service)
        sequence = self._next_sequence()
        frame = codec_cip.build_send_unit_data(
            self._session_handle, self._ot_connection_id, sequence, cip_request
        )
        transport.send(frame)
        try:
            return codec_cip.parse_send_unit_data_reply(
                self._recv_frame(), request_service, self._to_connection_id, sequence
            )
        except DeviceError as exc:
            if codec_cip.is_connection_reset_status(exc.code):
                # connected 会话已被 PLC 丢弃(空闲超时/连接丢失等):按坏帧
                # 断开,下次事务惰性重连并重新 Forward Open;其余 CIP 状态
                # (真实标签错误如只读)保持 DeviceError 不断线
                raise ProtocolFrameError(
                    f"connected 连接失效(CIP 状态 0x{exc.code:02X}):{exc}"
                ) from exc
            raise

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

    # ------------------------------------------------------------------
    # 通用 CIP 服务入口:ListIdentity / GetAttributesAll / GetAttributeList
    # ------------------------------------------------------------------

    def generic_message(
        self,
        service: int,
        class_id: int,
        instance: int,
        body: bytes = b"",
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 CIP 服务:拼装请求 + 走 :meth:`_transact` 收发,返回服务数据域裸字节。

        适用于非标签语义但同属 CIP 服务族的探测/通用对象访问(如 Identity
        Object、Message Router、Connection Manager 其它服务)。返回的 bytes
        是已剥掉 service 回显与 status 的纯数据域——调用方按各服务的应答
        布局自行解码;便捷方法 :meth:`get_attribute_all` / :meth:`get_attribute_list`
        / :meth:`get_plc_info` 已覆盖 Identity Object 常见用法。

        :returns: ``(True, 数据域)`` 或 ``(False, None)``(失败时 ``last_error`` 有消息)
        """
        request = codec_cip._service_request(
            service,
            codec_cip.build_class_instance_path(class_id, instance),
            body,
        )
        return self._execute(
            lambda: self._transact(request, service), is_write=False
        )

    def list_identity(self) -> Tuple[bool, Optional[dict]]:
        """ListIdentity(ENIP 0x63)单播:无 CIP 会话也能调用。

        设备侧无需连接也能应答(同于 ENIP ListIdentity 协议),但本客户端需
        处于已连接态以走 :meth:`_recv_frame` 收应答。
        """
        return self._execute(
            lambda: codec_cip.parse_list_identity_reply(self._send_recv_raw_enip(
                codec_cip.build_list_identity()
            )),
            is_write=False,
        )

    def get_plc_info(self) -> Tuple[bool, Optional[dict]]:
        """GetAttributesAll on Identity Object(class=0x01 instance=0x01)。

        返回 vendor / product_type / product_code / revision(major, minor) /
        status / serial(8 位十六进制字符串建议调用方 ``f"{serial:08X}"``) /
        product_name。
        """
        ok, payload = self.generic_message(
            codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL,
            CIP_CLASS_IDENTITY,
            CIP_INSTANCE_IDENTITY,
        )
        if not ok or payload is None:
            return False, None
        try:
            return True, codec_cip.parse_module_identity_payload(payload)
        except Exception as exc:
            with self._lock:
                self._set_error(f"GetAttributesAll 解码失败:{exc}", _categorize(exc), _extract_code(exc))
            return False, None

    def get_attribute_all(
        self, class_id: int, instance: int
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 GetAttributesAll:返回裸属性数据(N 字节,不带 2 字节 type code)。"""
        return self.generic_message(
            codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL, class_id, instance
        )

    def get_attribute_list(
        self,
        class_id: int,
        instance: int,
        attributes: Sequence[int],
    ) -> Tuple[bool, Optional[List[Tuple[int, object]]]]:
        """通用 GetAttributeList:按 ``attributes`` 顺序解对应属性值。

        默认按 Identity Object 7 字段布局解(长度前缀 UINT + UINT/UINT/... +
        SHORT_STRING);其它对象的属性解器需调用方自行展开(用
        :meth:`generic_message` + 自定义 :func:`parse_get_attribute_list_payload`
        解码)。

        :returns: ``(True, [(属性号, 值), ...])`` 或 ``(False, None)``
        """
        attrs = tuple(attributes)
        # 直接复用 build_get_attribute_list 已做长度校验;此处只为取 reply 解码
        ok, payload = self._execute(
            lambda: self._transact(
                codec_cip.build_get_attribute_list(class_id, instance, attrs),
                codec_cip.CIP_SERVICE_GET_ATTRIBUTE_LIST,
            ),
            is_write=False,
        )
        if not ok or payload is None:
            return False, None
        # Identity Object 7 字段默认布局(vendor..product_name);长度前缀 UINT
        identity_decoders = (
            (1, lambda d: struct.unpack_from("<H", d, 0)[0]),
            (2, lambda d: struct.unpack_from("<H", d, 0)[0]),
            (3, lambda d: struct.unpack_from("<H", d, 0)[0]),
            (4, lambda d: (d[0], d[1])),
            (5, lambda d: struct.unpack_from("<H", d, 0)[0]),
            (6, lambda d: struct.unpack_from("<I", d, 0)[0]),
            (7, codec_cip.decode_identity_string),
        )
        if class_id == CIP_CLASS_IDENTITY and instance == CIP_INSTANCE_IDENTITY \
                and len(attrs) == 7:
            decoder_map = dict(identity_decoders)
            try:
                decoders = tuple((a, decoder_map[a]) for a in attrs)
                return True, codec_cip.parse_get_attribute_list_payload(
                    payload, decoders
                )
            except Exception as exc:
                with self._lock:
                    self._set_error(f"GetAttributeList 解码失败:{exc}", _categorize(exc), _extract_code(exc))
                return False, None
        # 非 Identity 对象:返回原始 payload,调用方自行解
        return True, [(a, payload) for a in attrs]

    def _send_recv_raw_enip(self, frame: bytes) -> bytes:
        """发送裸 ENIP 帧并按长度域收完整应答(内部辅助,ListIdentity 用)。"""
        transport = self._require_transport()
        transport.send(frame)
        return self._recv_frame()

    # ------------------------------------------------------------------
    # 类型发现(标签自描述,按基名缓存)
    # ------------------------------------------------------------------

    def _ensure_type(self, parsed: AbTag) -> int:
        """确认标签实际类型码:读末级下标置 0 的 1 个元素并缓存(内部方法)。"""
        base = parsed.base
        cached = self._known_types.get(base)
        if cached is not None:
            return cached
        request = codec_cip.build_tag_read(
            codec_cip.tag_type_path(parsed, zero_last_index=True), 1
        )
        cip_type, _ = codec_cip.parse_tag_read_payload(
            self._transact(request, codec_cip.CIP_SERVICE_READ_TAG)
        )
        self._known_types[base] = cip_type
        return cip_type

    @staticmethod
    def _check_type(address: str, actual: int, expected: int) -> None:
        """实际类型与请求类型一致性校验(内部方法)。

        :raises ValueError: 类型不符(调用方参数错误)
        """
        if actual != expected:
            raise ValueError(
                "标签 {!r} 实际类型 {}(0x{:02X})与请求 {}(0x{:02X})不符".format(
                    address,
                    codec_cip.type_name(actual),
                    actual,
                    codec_cip.type_name(expected),
                    expected,
                )
            )

    # ------------------------------------------------------------------
    # 读原语
    # ------------------------------------------------------------------

    def _read_tag_values(
        self, parsed: AbTag, elements: int
    ) -> Tuple[int, bytes]:
        """标签读:返回 ``(实际类型码, 值数据域)``,并按基名缓存类型(内部)。"""
        request = codec_cip.build_tag_read(
            codec_cip.tag_type_path(parsed), elements
        )
        cip_type, data = codec_cip.parse_tag_read_payload(
            self._transact(request, codec_cip.CIP_SERVICE_READ_TAG)
        )
        if parsed.bit is None:
            self._known_types.setdefault(parsed.base, cip_type)
        return cip_type, data

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """AB 读原语:标签名 → Tag Read → 按实际类型解码并校验请求类型。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None and data_type is not DataType.BOOL:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if data_type is DataType.STRING:
            return self._read_string(address, INT32_MAX, "utf-8")
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        expected = codec_cip.data_type_code(data_type)
        cip_type, data = self._read_tag_values(parsed, 1)
        self._check_type(address, cip_type, expected)
        return codec_cip.decode_values(data, cip_type, 1)[0]

    def _read_bool_impl(self, parsed: AbTag) -> bool:
        """布尔读:BOOL 直读;整型带位号读词提位;BOOL 数组元素定词提位。"""
        if parsed.bit is not None:
            cip_type = self._ensure_type(parsed)
            if cip_type == codec_cip.CIP_TYPE_BOOL:
                raise ValueError(
                    f"BOOL 标签不支持位号后缀:{parsed.name!r}"
                )
            return self._read_bit_of_word(parsed, cip_type)
        cip_type = self._ensure_type(parsed)
        if cip_type == codec_cip.CIP_TYPE_BOOL:
            _, data = self._read_tag_values(parsed, 1)
            return bool(codec_cip.decode_values(data, cip_type, 1)[0])
        if cip_type == codec_cip.CIP_TYPE_DWORD:
            return self._read_bool_array_element(parsed)
        raise ValueError(
            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                parsed.name, codec_cip.type_name(cip_type)
            )
        )

    def _read_bit_of_word(self, parsed: AbTag, cip_type: int) -> bool:
        """整型标签位读:读元素后按位号提取(内部方法)。"""
        bit = parsed.bit or 0
        _check_bit_range(parsed, cip_type, bit)
        _, data = self._read_tag_values(_strip_bit(parsed), 1)
        return bool((codec_cip.decode_word(data, cip_type) >> bit) & 1)

    def _read_bool_array_element(self, parsed: AbTag) -> bool:
        """BOOL 数组元素读:DWORD 字按 32 位打包,``下标//32`` 定词(内部)。"""
        index = _single_array_index(parsed)
        word_path = _word_index_path(parsed, index)
        _, data = self._read_tag_values(word_path, 1)
        return bool((codec_cip.decode_word(data, codec_cip.CIP_TYPE_DWORD) >> (index % 32)) & 1)

    def _batch_bool_array_address(self, parsed: AbTag, index: int) -> Tuple[AbTag, int]:
        """批量读 BOOL 数组元素的读取路径与位提取口径(继承定制点)。

        Logix 口径:BOOL 数组按 DWORD 位打包,读 ``下标//32`` 字、
        提 ``下标%32`` 位;应答仍带实际类型(元素应答 BOOL 时按本体解码)。
        NJ/NX 等不做打包的设备覆写为直读元素本体。
        """
        return _word_index_path(parsed, index), index % 32

    def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """读 STRING 标签:结构体应答 ``len(u32) + 字符``。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None:
            raise ValueError(f"字符串标签不支持位访问:{address!r}")
        cip_type, data = self._read_tag_values(parsed, 1)
        if cip_type != codec_cip.CIP_TYPE_STRUCT:
            raise ValueError(
                "标签 {!r} 实际类型 {},字符串读取需要 STRING".format(
                    address, codec_cip.type_name(cip_type)
                )
            )
        return codec_cip.decode_string_payload(data, encoding)[:length]

    # ------------------------------------------------------------------
    # 批量读取(0x0A 多服务包,单事务)
    # ------------------------------------------------------------------

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 0x0A 多服务包(单事务)。

        与基类逐点独立容错不同:任一标签非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = self.read_batch([(address, data_type_enum) for address in addresses])
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多服务包批量读取(0x0A,混读多个标签;自动按预算拆分事务)。

        利用 CIP 原生 Multiple Service Packet 能力:每条 ``(标签, 数据类型)``
        内嵌为一个 0x4C 标签读,一帧往返取回全部值;unconnected/connected
        与 NJ/NX 直发路径均适用(经继承定制点分发)。条目数超上限或标签名
        较长导致估算字节超出未连接缓冲预算(:data:`AB_MAX_BATCH_PAYLOAD`)
        时,自动拆为多个 0x0A 事务按序执行,结果仍按 ``items`` 顺序返回。

        - 标量/字符串:应答自带实际类型码,类型不符抛 ``ValueError``(同单点读)
        - 布尔:BOOL 标签直读、整型位号读词提位、BOOL 数组元素定词提位;
          未知类型首次批量读会先做一次类型发现(按基名缓存,之后无额外往返)
        - 整批语义:任一事务任一条被 PLC 拒绝则整批失败(原因见 ``last_error``)

        :raises ValueError: 列表为空/地址或类型非法
        """
        if not items:
            raise ValueError("read_batch 至少需要一个 (标签, 数据类型) 项")

        def operation() -> List[PrimitiveValue]:
            requests: List[bytes] = []
            # 解码计划:(类别, 标签地址, 位号/数组下标, 数据类型)
            plan: List[Tuple[str, str, int, DataType]] = []
            for address, data_type in items:
                data_type_enum = DataType.coerce(data_type)
                parsed = parse_ab_tag(address)
                if data_type_enum is DataType.BOOL:
                    cip_type = self._ensure_type(parsed)
                    if parsed.bit is not None:
                        if cip_type == codec_cip.CIP_TYPE_BOOL:
                            raise ValueError(
                                f"BOOL 标签不支持位号后缀:{parsed.name!r}"
                            )
                        bit = parsed.bit or 0
                        _check_bit_range(parsed, cip_type, bit)
                        requests.append(codec_cip.build_tag_read(
                            codec_cip.tag_type_path(_strip_bit(parsed)), 1
                        ))
                        plan.append(("bitofword", address, bit, data_type_enum))
                    elif cip_type == codec_cip.CIP_TYPE_DWORD:
                        index = _single_array_index(parsed)
                        read_parsed, bit_index = self._batch_bool_array_address(parsed, index)
                        requests.append(codec_cip.build_tag_read(
                            codec_cip.tag_type_path(read_parsed), 1
                        ))
                        plan.append(("boolarray", address, bit_index, data_type_enum))
                    else:
                        requests.append(codec_cip.build_tag_read(
                            codec_cip.tag_type_path(parsed), 1
                        ))
                        plan.append(("booltag", address, 0, data_type_enum))
                    continue
                requests.append(codec_cip.build_tag_read(
                    codec_cip.tag_type_path(parsed), 1
                ))
                if data_type_enum is DataType.STRING:
                    plan.append(("string", address, 0, data_type_enum))
                else:
                    plan.append(("scalar", address, 0, data_type_enum))
            payloads: List[bytes] = []
            for chunk in _chunk_batch_requests(requests):
                packet = codec_cip.build_multiple_service_packet(chunk)
                payloads.extend(codec_cip.parse_multiple_service_payload(
                    self._transact(packet, codec_cip.CIP_SERVICE_MULTIPLE),
                    [codec_cip.CIP_SERVICE_READ_TAG] * len(chunk),
                ))
            values: List[PrimitiveValue] = []
            for (kind, address, extra, data_type_enum), payload in zip(plan, payloads):
                parsed = parse_ab_tag(address)
                cip_type, data = codec_cip.parse_tag_read_payload(payload)
                if parsed.bit is None:
                    self._known_types.setdefault(parsed.base, cip_type)
                if kind == "scalar":
                    expected = codec_cip.data_type_code(data_type_enum)
                    self._check_type(address, cip_type, expected)
                    values.append(codec_cip.decode_values(data, cip_type, 1)[0])
                elif kind == "string":
                    if cip_type != codec_cip.CIP_TYPE_STRUCT:
                        raise ValueError(
                            "标签 {!r} 实际类型 {},字符串读取需要 STRING".format(
                                address, codec_cip.type_name(cip_type)
                            )
                        )
                    values.append(codec_cip.decode_string_payload(data, "utf-8"))
                elif kind == "booltag":
                    if cip_type != codec_cip.CIP_TYPE_BOOL:
                        raise ValueError(
                            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                                address, codec_cip.type_name(cip_type)
                            )
                        )
                    values.append(bool(codec_cip.decode_values(data, cip_type, 1)[0]))
                elif kind == "bitofword":
                    values.append(bool((codec_cip.decode_word(data, cip_type) >> extra) & 1))
                else:  # boolarray
                    if cip_type == codec_cip.CIP_TYPE_DWORD:
                        values.append(bool(
                            (codec_cip.decode_word(data, cip_type) >> (extra % 32)) & 1
                        ))
                    elif cip_type == codec_cip.CIP_TYPE_BOOL:
                        # NJ/NX 等按元素自描述的设备:元素应答即 BOOL 本体
                        values.append(bool(codec_cip.decode_values(data, cip_type, 1)[0]))
                    else:
                        raise ValueError(
                            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                                address, codec_cip.type_name(cip_type)
                            )
                        )
            return values

        return self._execute(operation)

    # ------------------------------------------------------------------
    # 写原语
    # ------------------------------------------------------------------

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """AB 写原语:写请求携带类型码,故先确认实际类型再组写帧。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None and data_type is not DataType.BOOL:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if data_type is DataType.STRING:
            self._write_string(address, str(value), "utf-8")
            return
        if data_type is DataType.BOOL:
            self._write_bool_impl(parsed, require_bool(value))
            return
        expected = codec_cip.data_type_code(data_type)
        cip_type = self._ensure_type(parsed)
        self._check_type(address, cip_type, expected)
        payload = codec_cip.encode_value(data_type, value)
        self._transact(
            codec_cip.build_tag_write(codec_cip.tag_type_path(parsed), cip_type, payload),
            codec_cip.CIP_SERVICE_WRITE_TAG,
        )

    def _write_bool_impl(self, parsed: AbTag, flag: bool) -> None:
        """布尔写:BOOL 直写;整型/BOOL 数组走 0x4E 原子读-改-写。"""
        if parsed.bit is not None:
            cip_type = self._ensure_type(parsed)
            if cip_type == codec_cip.CIP_TYPE_BOOL:
                raise ValueError(
                    f"BOOL 标签不支持位号后缀:{parsed.name!r}"
                )
            bit = parsed.bit or 0
            _check_bit_range(parsed, cip_type, bit)
            self._modify_word(_strip_bit(parsed), cip_type, bit, flag)
            return
        cip_type = self._ensure_type(parsed)
        if cip_type == codec_cip.CIP_TYPE_BOOL:
            self._transact(
                codec_cip.build_tag_write(
                    codec_cip.tag_type_path(parsed),
                    cip_type,
                    b"\x01" if flag else b"\x00",
                ),
                codec_cip.CIP_SERVICE_WRITE_TAG,
            )
            return
        if cip_type == codec_cip.CIP_TYPE_DWORD:
            index = _single_array_index(parsed)
            self._modify_word(_word_index_path(parsed, index), cip_type, index % 32, flag)
            return
        raise ValueError(
            "标签 {!r} 实际类型 {} 不是 BOOL".format(
                parsed.name, codec_cip.type_name(cip_type)
            )
        )

    def _modify_word(self, parsed: AbTag, cip_type: int, bit: int, flag: bool) -> None:
        """0x4E 原子位修改:设备侧执行 ``(原值 | OR) & AND``(内部方法)。"""
        or_mask, and_mask = codec_cip.bit_masks(cip_type, bit, flag)
        self._transact(
            codec_cip.build_read_modify_write(
                codec_cip.tag_type_path(parsed), cip_type, or_mask, and_mask
            ),
            codec_cip.CIP_SERVICE_READ_MODIFY_WRITE,
        )

    def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """写 STRING 标签:结构体类型域(0xA0 + 模板 0x0FCE)+ 88 字节布局。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None:
            raise ValueError(f"字符串标签不支持位访问:{address!r}")
        cip_type = self._ensure_type(parsed)
        if cip_type != codec_cip.CIP_TYPE_STRUCT:
            raise ValueError(
                "标签 {!r} 实际类型 {},字符串写入需要 STRING".format(
                    address, codec_cip.type_name(cip_type)
                )
            )
        payload = codec_cip.encode_string_struct(value, encoding)
        self._transact(
            codec_cip.build_string_write(codec_cip.tag_type_path(parsed), payload),
            codec_cip.CIP_SERVICE_WRITE_TAG,
        )
        return value


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

# 0x0A 请求在 UC-Send 信封(路由段+超时+服务头)之外的估算余量(字节)
_BATCH_ENVELOPE_MARGIN: int = 24


def _chunk_batch_requests(requests: Sequence[bytes]) -> List[List[bytes]]:
    """把 0x0A 内嵌服务请求按 (条数, 字节预算) 切成多个事务(内部函数)。

    数据段 = 条数(2) + 偏移表(2×n) + Σ(内嵌请求 + 偶对齐);加上
    信封余量后须落在 Logix 未连接缓冲(504 字节)内。短标签名典型
    单事务即可承载全部条目,长标签名自动拆分。
    """
    chunks: List[List[bytes]] = []
    current: List[bytes] = []
    used = 2  # 条数域
    for request in requests:
        entry = len(request) + len(request) % 2  # 偏移项 + 偶对齐后的请求
        if current and (
            len(current) >= AB_MAX_BATCH_SERVICES
            or used + entry + _BATCH_ENVELOPE_MARGIN > AB_MAX_BATCH_PAYLOAD
        ):
            chunks.append(current)
            current = []
            used = 2
        current.append(request)
        used += entry
    if current:
        chunks.append(current)
    return chunks


def _strip_bit(parsed: AbTag) -> AbTag:
    """去掉末尾位号(词读/RMW 路径用,内部函数)。"""
    return parsed._replace(bit=None)


def _single_array_index(parsed: AbTag) -> int:
    """取末级成员的单一下标(BOOL 数组元素访问,内部函数)。

    :raises ValueError: 末级成员无下标或多维下标
    """
    indices = parsed.indices[-1] if parsed.indices else ()
    if len(indices) != 1:
        raise ValueError(
            f"BOOL 数组元素访问需要单一下标:{parsed.name!r}(示例 Bits[12])"
        )
    return indices[0]


def _word_index_path(parsed: AbTag, index: int) -> AbTag:
    """BOOL 数组字路径:末级下标替换为 ``index // 32``(内部函数)。"""
    members = list(parsed.indices)
    members[-1] = (index // 32,)
    return parsed._replace(indices=tuple(members))


def _check_bit_range(parsed: AbTag, cip_type: int, bit: int) -> None:
    """位号越界校验(整型位宽由实际类型决定,内部函数)。

    :raises ValueError: 位号超出类型位宽或不支持位访问
    """
    if not codec_cip.is_bit_access_type(cip_type):
        raise ValueError(
            "标签 {!r} 实际类型 {} 不支持位访问".format(
                parsed.name, codec_cip.type_name(cip_type)
            )
        )
    width = codec_cip.type_size(cip_type) * 8
    if bit >= width:
        raise ValueError(
            "位号 {} 超出 {} 位宽 {}:{!r}".format(
                bit, codec_cip.type_name(cip_type), width, parsed.name
            )
        )
