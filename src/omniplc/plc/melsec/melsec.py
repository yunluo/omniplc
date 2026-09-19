"""三菱 MELSEC MC 协议客户端(3E/4E/1E 帧 × TCP/UDP 走线)。

类继承::

    BaseClient
    ├── MelsecMcTcpClient   3E/4E/1E 帧 over TCP(默认端口 2000)
    └── MelsecMcUdpClient   3E/4E/1E 帧 over UDP(默认端口 2000)

两走线共享同一套帧编解码(:mod:`.codec_qna` 与 :mod:`.codec_a`),
接收策略按走线区分:TCP 按响应头长度分段收包,UDP 整包接收。
"""
from __future__ import annotations

import struct
from abc import abstractmethod
from typing import List, Optional, Sequence, Tuple, Union

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    MC_1E_ERROR_EXTRA,
    MC_1E_ERROR_EXTRA_SIZE,
    MC_1E_RESPONSE_HEAD_SIZE,
    MC_4E_RESPONSE_HEAD_SIZE,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
    MC_MAX_DATAGRAM,
    MC_RESPONSE_HEAD_SIZE,
)
from ...core.validation import (
    check_int16,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ...transport import BaseTransport, TcpTransport, UdpTransport
from ...types import DataType, McFrame, PrimitiveValue
from . import codec_a, codec_qna
from .address import McAddress, parse_mc_address


class _MelsecMcBase(BaseClient):
    """MC 客户端公共基类:帧型/序列号管理与软元件地址分发(私有)。"""

    def __init__(
        self,
        ip_address: str,
        port: int,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._frame = _coerce_frame(frame)
        self._network_number = int(network_number)
        self._pc_number = int(pc_number)
        self._serial = 0

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._frame

    # ------------------------------------------------------------------
    # 协议原语(BaseClient 类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """MC 读原语:软元件地址 → 成批读请求 → 按类型解码(MC 字序小端)。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            data = self._read_words(parsed, 1)
            return data[0] if data_type is DataType.USHORT else _to_int16(data[0])
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            data = self._read_words(parsed, 2)
            return _decode_32(data, data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            data = self._read_words(parsed, 4)
            return _decode_64(data, data_type)
        raise ValueError("MC 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """MC 写原语:成批写请求。位软元件按位写;字软元件按位写用读-改-写。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError("仅布尔类型支持位访问:{!r}".format(address))
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            _, is_bit_device, _ = self._device_info(parsed.device)
            if is_bit_device:
                self._write_bits(parsed, [1 if flag else 0])
            else:
                words = self._read_words(parsed, 1)
                self._write_words(parsed, [convert.set_bit(words[0], parsed.bit or 0, flag)])
            return
        if data_type is DataType.SHORT:
            self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            self._write_words(parsed, [check_uint16(value)])
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            self._write_words(parsed, _encode_32(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            self._write_words(parsed, _encode_64(value, data_type))
            return
        raise ValueError("MC 不支持的数据类型:{}".format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """从字软元件读字符串:逐字小端拼字节后解码(MC 字序约定)。"""
        parsed = parse_mc_address(address)
        words = self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "little") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """向字软元件写字符串:编码 → 补齐偶数字节 → 逐字小端。"""
        parsed = parse_mc_address(address)
        raw = convert.encode_string(value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding)
        words = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 位/字原语(核心命令 + 帧封装)
    # ------------------------------------------------------------------

    def _read_bool_impl(self, parsed: McAddress) -> bool:
        """位软元件按点位成批读;字软元件读 1 字后按位提取。"""
        _, is_bit_device, _ = self._device_info(parsed.device)
        if is_bit_device:
            return bool(self._read_bits(parsed, 1)[0])
        words = self._read_words(parsed, 1)
        return convert.get_bit(words[0], parsed.bit or 0)

    def _read_bits(self, parsed: McAddress, count: int) -> List[int]:
        """位软元件成批读(位单位核心命令)。"""
        request = self._build_frame(parsed, count, is_bit=True, is_write=False)
        tail = self._read_tail_size(count, is_bit=True)
        return self._parse_read(self._transact(request, tail), count, is_bit=True)

    def _read_words(self, parsed: McAddress, word_count: int) -> List[int]:
        """成批读字软元件(字单位核心命令),返回 0~65535 逐字数据。"""
        request = self._build_frame(parsed, word_count, is_bit=False, is_write=False)
        tail = self._read_tail_size(word_count, is_bit=False)
        return self._parse_read(self._transact(request, tail), word_count, is_bit=False)

    def _read_tail_size(self, points: int, is_bit: bool) -> int:
        """1E/TCP 读响应头之后的数据字节数(其余帧型由长度域决定,传 0)。"""
        if self._frame is not McFrame.FRAME_1E:
            return 0
        return (points + 1) // 2 if is_bit else points * 2

    def _write_bits(self, parsed: McAddress, values: List[int]) -> None:
        """位软元件成批写(位单位核心命令)。"""
        request = self._build_frame(parsed, len(values), is_bit=True, is_write=True, data=values)
        self._parse_write(self._transact(request))

    def _write_words(self, parsed: McAddress, words: List[int]) -> None:
        """字软元件成批写(字单位核心命令)。"""
        request = self._build_frame(parsed, len(words), is_bit=False, is_write=True, data=words)
        self._parse_write(self._transact(request))

    # ------------------------------------------------------------------
    # 帧组装/解析分发与收包(3E/4E 与 1E 两套)
    # ------------------------------------------------------------------

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """按当前帧型查软元件码表(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.device_info(device)
        return codec_qna.device_info(device)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """按当前帧型构造完整请求帧(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.build_request(
                self._pc_number, MC_DEFAULT_MONITOR_TIMER, parsed, points, is_bit, is_write, data
            )
        return codec_qna.build_request(
            self._frame.value,
            self._next_serial(),
            self._network_number,
            self._pc_number,
            MC_DEFAULT_MONITOR_TIMER,
            parsed,
            points,
            is_bit,
            is_write,
            data,
        )

    def _parse_read(self, response: bytes, points: int, is_bit: bool) -> List[int]:
        """按当前帧型解析读响应(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.parse_response(response, points, is_bit, True)
        return codec_qna.parse_response(
            response, self._frame.value, points, is_bit, True, expected_serial=self._serial
        )

    def _parse_write(self, response: bytes) -> None:
        """按当前帧型校验写响应(结束码非 0 抛 DeviceError,内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            codec_a.parse_response(response, 0, False, False)
        else:
            codec_qna.parse_response(
                response, self._frame.value, 0, False, False, expected_serial=self._serial
            )

    def _next_serial(self) -> int:
        """4E 序列号递增(0~65535 回绕,内部方法)。"""
        self._serial = (self._serial + 1) & 0xFFFF
        return self._serial

    def _transact(self, request: bytes, tail_size: int = 0) -> bytes:
        """发送请求并接收完整响应帧(内部方法)。

        TCP:3E 收 9 字节头 + 应答数据长所示内容;4E 收 13 字节头;
        1E 收 2 字节头 + ``tail_size`` 数据(结束码 0x5B 时改收 2 字节扩展)。
        UDP:一次 recv 整包,长度校验交给解析层。
        """
        transport = self._require_transport()
        transport.send(request)
        if transport.datagram:
            return transport.recv(MC_MAX_DATAGRAM)
        if self._frame is McFrame.FRAME_1E:
            head = transport.recv(MC_1E_RESPONSE_HEAD_SIZE)
            if head[1] == MC_1E_ERROR_EXTRA:
                return head + transport.recv(MC_1E_ERROR_EXTRA_SIZE)
            if tail_size:
                return head + transport.recv(tail_size)
            return head
        head_size = MC_4E_RESPONSE_HEAD_SIZE if self._frame is McFrame.FRAME_4E else MC_RESPONSE_HEAD_SIZE
        head = transport.recv(head_size)
        return head + transport.recv(codec_qna.parse_response_head(head, self._frame.value))

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """由走线子类实现。"""


class MelsecMcTcpClient(_MelsecMcBase):
    """三菱 MC 客户端(TCP 走线)。

    :example: ``client = MelsecMcTcpClient("192.168.3.39", 2000, frame=McFrame.FRAME_3E)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC TCP 客户端,参数说明见 :class:`_MelsecMcBase`。"""
        super().__init__(ip_address, port, frame, network_number, pc_number)

    def _create_transport(self) -> BaseTransport:
        return TcpTransport(self._ip_address, self._port)


class MelsecMcUdpClient(_MelsecMcBase):
    """三菱 MC 客户端(UDP 走线),帧格式与 TCP 相同,一问一答一数据报。"""

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 MC UDP 客户端,参数说明见 :class:`_MelsecMcBase`。"""
        super().__init__(ip_address, port, frame, network_number, pc_number)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _coerce_frame(value: Union[McFrame, str]) -> McFrame:
    """把枚举成员或字符串统一解析为 McFrame(内部函数)。"""
    if isinstance(value, McFrame):
        return value
    try:
        return McFrame(str(value).strip().upper())
    except ValueError:
        supported = "/".join(member.value for member in McFrame)
        raise ValueError("不支持的 MC 帧型:{!r},支持:{}".format(value, supported))


def _to_int16(raw: int) -> int:
    """0~65535 原始字 → 有符号 16 位(内部函数)。"""
    return raw - 0x10000 if raw >= 0x8000 else raw


def _decode_32(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """两字数据按类型解码(MC 为小端字序:低字在前,内部函数)。"""
    raw = b"".join(word.to_bytes(2, "little") for word in data)
    if data_type is DataType.INT:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.UINT:
        return int.from_bytes(raw, "little", signed=False)
    return convert.registers_to_float32(
        [int.from_bytes(word.to_bytes(2, "little"), "big") for word in data],
    )


def _decode_64(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """四字数据按类型解码(小端字序,内部函数)。"""
    raw = b"".join(word.to_bytes(2, "little") for word in data)
    if data_type is DataType.LONG:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.ULONG:
        return int.from_bytes(raw, "little", signed=False)
    return convert.registers_to_float64(
        [int.from_bytes(word.to_bytes(2, "little"), "big") for word in data],
    )


def _encode_32(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 32 位值编码为 2 个字(小端字节序,内部函数)。"""
    if data_type is DataType.FLOAT:
        raw = struct.pack("<f", require_float(value))
    else:
        number = require_int(value)
        if data_type is DataType.INT:
            if not -2147483648 <= number <= 2147483647:
                raise ValueError("int 超出 32 位范围:{}".format(number))
            raw = number.to_bytes(4, "little", signed=True)
        else:
            if not 0 <= number <= 4294967295:
                raise ValueError("uint 超出 32 位范围:{}".format(number))
            raw = number.to_bytes(4, "little", signed=False)
    return [int.from_bytes(raw[0:2], "little"), int.from_bytes(raw[2:4], "little")]


def _encode_64(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 64 位值编码为 4 个字(小端字节序,内部函数)。"""
    if data_type is DataType.DOUBLE:
        raw = struct.pack("<d", require_float(value))
    else:
        number = require_int(value)
        if data_type is DataType.LONG:
            if not -9223372036854775808 <= number <= 9223372036854775807:
                raise ValueError("long 超出 64 位范围:{}".format(number))
            raw = number.to_bytes(8, "little", signed=True)
        else:
            if not 0 <= number <= 18446744073709551615:
                raise ValueError("ulong 超出 64 位范围:{}".format(number))
            raw = number.to_bytes(8, "little", signed=False)
    return [
        int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)
    ]
