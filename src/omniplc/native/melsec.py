"""原生 asyncio 三菱 MC 客户端(A 兼容 1E / QnA 兼容 3E;TCP 与 UDP 走线)。

:class:`AsyncMelsecMcTcpClient` / :class:`AsyncMelsecMcUdpClient` 是同步
:class:`~omniplc.plc.melsec.MelsecMcTcpClient` / ``MelsecMcUdpClient`` 的原生
异步孪生:软元件码表、地址解析、组帧与解析**全部复用**同步侧纯模块
(:mod:`~omniplc.plc.melsec.codec_qna` / ``codec_a`` / ``address`` 与既有的
``_build_frame`` / ``_parse_*`` 同源实现),本模块只把 ``_transact`` 与位/字
原语换成 ``await`` 版本。

首发能力面:单点读/写(位、字、字符串)+ 类型化方法 + 点位表。

**尚未包含**:4E 帧与串口 1C/3C/4C 走线、``read_many``/``read_batch``
(0406 多块批量读)——留后续批次,可用同步客户端或 :mod:`omniplc.aio` 过渡。

:example::

    from omniplc.native import AsyncMelsecMcTcpClient

    async with AsyncMelsecMcTcpClient("192.168.3.39", 2000, "3E") as client:
        ok, value = await client.read_ushort("D100")
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Union

from .base import AsyncBaseClient
from .transport import AsyncBaseTransport, AsyncTcpTransport, AsyncUdpTransport
from .. import convert
from ..core.base_client import validate_endpoint
from ..core.constants import (
    MC_1E_ERROR_EXTRA,
    MC_1E_ERROR_EXTRA_SIZE,
    MC_1E_RESPONSE_HEAD_SIZE,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
    MC_MAX_DATAGRAM,
    MC_RESPONSE_HEAD_SIZE,
)
from ..core.validation import (
    check_byte_field,
    check_int16,
    check_uint16,
    require_bool,
)
from ..plc.melsec import codec_a, codec_qna
from ..plc.melsec.address import McAddress, parse_mc_address
from ..plc.melsec.melsec import (
    _MC_DEVICE_CODES_FX5U_XY,
    _coerce_frame,
    _decode_32,
    _decode_64,
    _encode_32,
    _encode_64,
)
from ..types import DataType, McFrame, PrimitiveValue


class AsyncMelsecMcBase(AsyncBaseClient):
    """三菱 MC 原生异步基类:帧分发 + 位/字原语(1E/3E)。

    走线子类只实现 :meth:`_create_transport` 与 :meth:`_transact`。
    帧级地址换算钩子 :meth:`_translate_address` 与同步层同名同义(品牌兼容
    子类覆写;批量路径同样必经该钩子)。
    """

    _SUPPORTED_FRAMES: Tuple[McFrame, ...] = (McFrame.FRAME_1E, McFrame.FRAME_3E)
    """原生首批支持的帧型(4E 留后续批次)。"""

    def __init__(
        self,
        ip_address: str,
        port: int,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
        xy_octal: bool = False,
    ) -> None:
        """初始化 MC 客户端公共参数。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``FRAME_3E`` 为 QnA 兼容、``FRAME_1E`` 为 A 兼容);也兼容
            ``"3E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E 使用)
        :param pc_number: PC 编号(仅 3E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径,默认 False)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._xy_octal = bool(xy_octal)
        self._frame = _coerce_frame(frame)
        if self._frame not in self._SUPPORTED_FRAMES:
            supported = "/".join(member.value for member in self._SUPPORTED_FRAMES)
            raise ValueError(
                "{} 暂不支持帧型 {},首批支持:{}(4E 与串口帧请用同步客户端或"
                " omniplc.aio 过渡)".format(
                    type(self).__name__, self._frame.value, supported
                )
            )
        self._network_number = check_byte_field("网络编号", network_number)
        self._pc_number = check_byte_field("PC 编号", pc_number)
        self._serial = 0

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._frame

    @property
    def network_number(self) -> int:
        """当前网络编号。"""
        return self._network_number

    @property
    def pc_number(self) -> int:
        """当前 PC 编号。"""
        return self._pc_number

    # ------------------------------------------------------------------
    # 协议原语(基类类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """MC 读原语:软元件地址 → 成批读请求 → 按类型解码(MC 字序小端)。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if data_type is DataType.BOOL:
            return await self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            data = await self._read_words(parsed, 1)
            return data[0] if data_type is DataType.USHORT else convert.to_signed(data[0], 16)
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            data = await self._read_words(parsed, 2)
            return _decode_32(data, data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            data = await self._read_words(parsed, 4)
            return _decode_64(data, data_type)
        raise ValueError(f"MC 不支持的数据类型:{data_type}")

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """MC 写原语:成批写请求。位软元件按位写;字软元件按位写用读-改-写。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(f"仅布尔类型支持位访问:{address!r}")
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            _, is_bit_device, _ = self._device_info(parsed.device)
            if is_bit_device:
                await self._write_bits(parsed, [1 if flag else 0])
            else:
                words = await self._read_words(parsed, 1)
                await self._write_words(
                    parsed, [convert.set_bit(words[0], parsed.bit or 0, flag)]
                )
            return
        if data_type is DataType.SHORT:
            await self._write_words(parsed, [check_int16(value)])
            return
        if data_type is DataType.USHORT:
            await self._write_words(parsed, [check_uint16(value)])
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            await self._write_words(parsed, _encode_32(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            await self._write_words(parsed, _encode_64(value, data_type))
            return
        raise ValueError(f"MC 不支持的数据类型:{data_type}")

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """从字软元件读字符串:逐字小端拼字节后解码(MC 字序约定)。"""
        parsed = parse_mc_address(address)
        words = await self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "little") for word in words)[:length]
        return convert.decode_string(data, encoding)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """向字软元件写字符串:编码 → 补齐偶数字节 → 逐字小端。"""
        parsed = parse_mc_address(address)
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        await self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 位/字原语(核心命令 + 帧封装)
    # ------------------------------------------------------------------

    async def _read_bool_impl(self, parsed: McAddress) -> bool:
        """位软元件按点位成批读;字软元件读 1 字后按位提取。"""
        _, is_bit_device, _ = self._device_info(parsed.device)
        if is_bit_device:
            return bool((await self._read_bits(parsed, 1))[0])
        words = await self._read_words(parsed, 1)
        return convert.get_bit(words[0], parsed.bit or 0)

    async def _read_bits(self, parsed: McAddress, count: int) -> List[int]:
        """位软元件成批读(位单位核心命令)。"""
        request = self._build_frame(parsed, count, is_bit=True, is_write=False)
        tail = self._read_tail_size(count, is_bit=True)
        return self._parse_read(await self._transact(request, tail), count, is_bit=True)

    async def _read_words(self, parsed: McAddress, word_count: int) -> List[int]:
        """成批读字软元件(字单位核心命令),返回 0~65535 逐字数据。"""
        request = self._build_frame(parsed, word_count, is_bit=False, is_write=False)
        tail = self._read_tail_size(word_count, is_bit=False)
        return self._parse_read(
            await self._transact(request, tail), word_count, is_bit=False
        )

    def _read_tail_size(self, points: int, is_bit: bool) -> int:
        """1E/TCP 读响应头之后的数据字节数(3E 由长度域决定,传 0)。"""
        if self._frame is not McFrame.FRAME_1E:
            return 0
        return (points + 1) // 2 if is_bit else points * 2

    async def _write_bits(self, parsed: McAddress, values: List[int]) -> None:
        """位软元件成批写(位单位核心命令)。"""
        request = self._build_frame(
            parsed, len(values), is_bit=True, is_write=True, data=values
        )
        self._parse_write(await self._transact(request), True)

    async def _write_words(self, parsed: McAddress, words: List[int]) -> None:
        """字软元件成批写(字单位核心命令)。"""
        request = self._build_frame(
            parsed, len(words), is_bit=False, is_write=True, data=words
        )
        self._parse_write(await self._transact(request), False)

    # ------------------------------------------------------------------
    # 帧组装/解析分发(1E 与 3E 两套;与同步层同源)
    # ------------------------------------------------------------------

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """按当前帧型查软元件码表(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.device_info(device)
        return codec_qna.device_info(device)

    def _translate_address(self, parsed: McAddress) -> McAddress:
        """帧级地址换算钩子,默认透传(内部方法,品牌兼容子类覆写)。"""
        return parsed

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
                self._pc_number,
                MC_DEFAULT_MONITOR_TIMER,
                parsed,
                points,
                is_bit,
                is_write,
                data,
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
            self._effective_codes(),
        )

    def _effective_codes(self) -> Optional[Dict[str, Tuple[int, int, int]]]:
        """生效软元件码表:xy_octal 时换 X/Y 八进制口径的 FX5U 变体(内部)。"""
        if not self._xy_octal:
            return None
        return _MC_DEVICE_CODES_FX5U_XY

    def _parse_read(self, response: bytes, points: int, is_bit: bool) -> List[int]:
        """按当前帧型解析读响应(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.parse_response(response, points, is_bit, True)
        return codec_qna.parse_response(
            response,
            self._frame.value,
            points,
            is_bit,
            True,
            expected_serial=self._serial,
        )

    def _parse_write(self, response: bytes, is_bit: bool) -> None:
        """按当前帧型校验写响应(结束码非 0 抛 DeviceError,内部方法)。

        1E 写响应副头部 = 请求副头部 + 0x80(位写 0x82 / 字写 0x83),须随操作
        传入是否位单位;3E 读写响应副头部同为 D0 00,不区分。
        """
        if self._frame is McFrame.FRAME_1E:
            codec_a.parse_response(response, 0, is_bit, False)
        else:
            codec_qna.parse_response(
                response,
                self._frame.value,
                0,
                False,
                False,
                expected_serial=self._serial,
            )

    def _next_serial(self) -> int:
        """3E 序列号递增(0~65535 回绕,内部方法)。"""
        return self._bump_id("_serial", 16)

    async def _transact(self, request: bytes, tail_size: int = 0) -> bytes:
        """发送请求并接收完整响应帧(内部方法;走线差异只在传输对象)。

        与同步层同构:TCP 3E 收 9 字节头 + 应答数据长所示内容;1E 收 2 字节头
        + ``tail_size`` 数据(结束码 0x5B 时改收 2 字节扩展);UDP 一次 recv
        整包(长度校验交给解析层)。
        """
        transport = self._require_transport()
        await transport.send(request)
        if transport.datagram:
            return await transport.recv(MC_MAX_DATAGRAM)
        if self._frame is McFrame.FRAME_1E:
            head = await transport.recv(MC_1E_RESPONSE_HEAD_SIZE)
            if head[1] != 0 and head[1] != MC_1E_ERROR_EXTRA:
                return head  # 错误响应只有 2 字节头,无数据段(1E 帧无长度域)
            if head[1] == MC_1E_ERROR_EXTRA:
                return head + await transport.recv(MC_1E_ERROR_EXTRA_SIZE)
            if tail_size:
                return head + await transport.recv(tail_size)
            return head
        head = await transport.recv(MC_RESPONSE_HEAD_SIZE)
        return head + await transport.recv(
            codec_qna.parse_response_head(head, self._frame.value)
        )


class AsyncMelsecMcTcpClient(AsyncMelsecMcBase):
    """三菱 MC 客户端(TCP 走线,1E/3E 帧)。

    :example: ``client = AsyncMelsecMcTcpClient("192.168.3.39", 2000, "3E")``
    """

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
        xy_octal: bool = False,
    ) -> None:
        """初始化 MC TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型(``FRAME_3E`` / ``FRAME_1E``,兼容 ``"3E"``/``"1E"``)
        :param network_number: 网络编号(仅 3E 使用)
        :param pc_number: PC 编号(仅 3E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径,默认 False)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, frame, network_number, pc_number, xy_octal)

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncTcpTransport(self._ip_address, self._port)


class AsyncMelsecMcUdpClient(AsyncMelsecMcBase):
    """三菱 MC 客户端(UDP 走线),帧格式与 TCP 相同,一问一答一数据报。

    :example: ``client = AsyncMelsecMcUdpClient("192.168.3.39", 2000, "3E")``
    """

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
        xy_octal: bool = False,
    ) -> None:
        """初始化 MC UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型(``FRAME_3E`` / ``FRAME_1E``,兼容 ``"3E"``/``"1E"``)
        :param network_number: 网络编号(仅 3E 使用)
        :param pc_number: PC 编号(仅 3E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径,默认 False)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, frame, network_number, pc_number, xy_octal)

    def _create_transport(self) -> AsyncBaseTransport:
        return AsyncUdpTransport(self._ip_address, self._port)
