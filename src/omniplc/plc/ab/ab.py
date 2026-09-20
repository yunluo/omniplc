"""罗克韦尔 Allen-Bradley EtherNet/IP(CIP)客户端——Logix 标签读写。

协议要点(三参考库交叉核证,见 architecture.md §8.1):

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
UDT 整体读取、批量多服务(0x0A)、分片读写在 v1.x 规划。
"""
from __future__ import annotations

import random
from typing import Optional, Tuple

from . import codec_cip
from .address import AbTag, parse_ab_tag
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    AB_EIP_DEFAULT_PORT,
    AB_EIP_DEFAULT_SLOT,
    AB_EIP_ORIGINATOR_VENDOR_ID,
    AB_EIP_SLOT_MAX,
)
from ...core.errors import OmniPLCInternalError, ProtocolFrameError
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
    ) -> None:
        """初始化 AB EtherNet/IP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,EtherNet/IP 默认 44818
        :param slot: CPU 槽号(内置以太网口机型为 0;1756 背板按实际槽位)
        :param connected_messaging: True 走 connected 消息(Forward Open +
            SendUnitData);默认 False 走 unconnected 消息
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        BaseClient.__init__(self, ip_address, port)
        slot = int(slot)
        if not 0 <= slot <= AB_EIP_SLOT_MAX:
            raise ValueError(
                "槽号超出范围 0~{}:{}".format(AB_EIP_SLOT_MAX, slot)
            )
        self._slot = slot
        self._connected_messaging = bool(connected_messaging)
        self._session_handle = 0
        self._known_types: dict = {}
        self._connection_serial = 0
        self._ot_connection_id: Optional[int] = None
        self._to_connection_id = 0
        self._connection_size: Optional[int] = None
        self._sequence = 0
        self._originator_serial = random.randrange(1, 0xFFFF)

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
            to_connection_id = random.randrange(1, 0xFFFF)
            request = codec_cip.build_forward_open(
                is_large,
                size,
                self._connection_serial,
                to_connection_id,
                AB_EIP_ORIGINATOR_VENDOR_ID,
                self._originator_serial,
                self._slot,
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
        """尽力发送 Forward Close(应答与异常一律忽略,内部方法)。"""
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
                        self._slot,
                    ),
                )
            )
            self._recv_frame()
        except (OSError, OmniPLCInternalError):
            pass

    def _unregister_session(self) -> None:
        """尽力发送 UnregisterSession(应答与异常一律忽略,内部方法)。"""
        transport = self._transport
        handle = self._session_handle
        self._session_handle = 0
        if transport is None or not handle:
            return
        try:
            transport.send(codec_cip.build_unregister_session(handle))
            self._recv_frame()
        except (OSError, OmniPLCInternalError):
            pass

    def _recv_frame(self) -> bytes:
        """按 ENIP 长度域收完整帧:24 字节头 + 声明长度(内部方法)。"""
        transport = self._require_transport()
        head = transport.recv(codec_cip.EIP_HEADER_SIZE)
        length = int.from_bytes(head[2:4], "little")
        return head + transport.recv(length)

    def _next_sequence(self) -> int:
        """connected 序列号递增(1~65535 回绕,内部方法)。"""
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    def _transact(self, cip_request: bytes, request_service: int) -> bytes:
        """CIP 事务:按消息通道封装发送并解析应答数据域(内部方法)。

        unconnected:UC Send 包裹 → RRData;connected:SendUnitData。

        :raises DeviceError: CIP 状态非 0(不断线)
        :raises ProtocolFrameError: 坏帧(标记断开惰性重连)
        """
        transport = self._require_transport()
        if self._ot_connection_id is None:
            frame = codec_cip.build_rr_data(
                self._session_handle,
                codec_cip.build_uc_send(cip_request, self._slot),
            )
            transport.send(frame)
            return codec_cip.parse_service_reply(self._recv_frame(), request_service)
        sequence = self._next_sequence()
        frame = codec_cip.build_send_unit_data(
            self._session_handle, self._ot_connection_id, sequence, cip_request
        )
        transport.send(frame)
        return codec_cip.parse_send_unit_data_reply(
            self._recv_frame(), request_service, self._to_connection_id, sequence
        )

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)

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
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if data_type is DataType.STRING:
            return self._read_string(address, 0x7FFFFFFF, "utf-8")
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
                    "BOOL 标签不支持位号后缀:{!r}".format(parsed.name)
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

    def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """读 STRING 标签:结构体应答 ``len(u32) + 字符``。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None:
            raise ValueError("字符串标签不支持位访问:{!r}".format(address))
        cip_type, data = self._read_tag_values(parsed, 1)
        if cip_type != codec_cip.CIP_TYPE_STRUCT:
            raise ValueError(
                "标签 {!r} 实际类型 {},字符串读取需要 STRING".format(
                    address, codec_cip.type_name(cip_type)
                )
            )
        return codec_cip.decode_string_payload(data, encoding)[:length]

    # ------------------------------------------------------------------
    # 写原语
    # ------------------------------------------------------------------

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """AB 写原语:写请求携带类型码,故先确认实际类型再组写帧。"""
        parsed = parse_ab_tag(address)
        if parsed.bit is not None and data_type is not DataType.BOOL:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
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
                    "BOOL 标签不支持位号后缀:{!r}".format(parsed.name)
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
            raise ValueError("字符串标签不支持位访问:{!r}".format(address))
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
            "BOOL 数组元素访问需要单一下标:{!r}(示例 Bits[12])".format(parsed.name)
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
