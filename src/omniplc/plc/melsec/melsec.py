"""三菱 MELSEC MC 协议客户端(3E/4E/1E 帧 × TCP/UDP 走线 + 3C/4C 串口帧)。

类继承::

    BaseClient
    ├── MelsecMcTcpClient     3E/4E/1E 帧 over TCP(默认端口 2000)
    ├── MelsecMcUdpClient     3E/4E/1E 帧 over UDP(默认端口 2000)
    └── MelsecMcSerialClient  3C/4C 帧 over 串口(C24,9600,需 pyserial)

以太网与串口走线共享同一套软元件码表与核心命令(:mod:`.codec_qna`),
帧封装按帧型分发:1E → :mod:`.codec_a`,3E/4E → :mod:`.codec_qna`,
3C/4C → :mod:`.codec_serial`;接收策略按走线区分:TCP 按响应头长度
分段收包,UDP 整包接收,串口按控制码与长度域逐段收包。
"""
from __future__ import annotations

import struct
from abc import abstractmethod
from typing import List, Optional, Sequence, Tuple, Union

from . import codec_a, codec_qna, codec_serial
from .address import McAddress, parse_mc_address
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
    MC_SERIAL_DEFAULT_MODULE_IO,
    MC_SERIAL_DEFAULT_MODULE_STATION,
    MC_SERIAL_DEFAULT_NETWORK_NUMBER,
    MC_SERIAL_DEFAULT_PC_NUMBER,
    MC_SERIAL_DEFAULT_SELF_STATION,
    MC_SERIAL_DEFAULT_STATION,
    MC_SERIAL_FRAME_ID_4C,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
)
from ...core.errors import ProtocolFrameError
from ...core.validation import (
    check_int16,
    check_uint16,
    require_bool,
    require_float,
    require_int,
)
from ...transport import BaseTransport, SerialConfig, SerialTransport, TcpTransport, UdpTransport
from ...types import DataType, McFrame, PrimitiveValue, SerialParity


class _MelsecMcBase(BaseClient):
    """MC 客户端公共基类:帧型/序列号管理与软元件地址分发(私有)。

    各走线子类通过 ``_SUPPORTED_FRAMES`` 声明可用帧型,跨走线使用帧型
    在构造时报错(如 TCP 走线传 3C 帧、串口走线传 3E 帧)。
    """

    _SUPPORTED_FRAMES: Tuple[McFrame, ...] = (
        McFrame.FRAME_3E,
        McFrame.FRAME_4E,
        McFrame.FRAME_1E,
    )

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
        self._init_frame(frame, network_number, pc_number)

    def _init_frame(
        self,
        frame: Union[McFrame, str],
        network_number: int,
        pc_number: int,
    ) -> None:
        """校验并登记帧型与网络路由参数(串口走线复用,内部方法)。"""
        self._frame = _coerce_frame(frame)
        if self._frame not in self._SUPPORTED_FRAMES:
            supported = "/".join(member.value for member in self._SUPPORTED_FRAMES)
            raise ValueError(
                "{} 不支持帧型 {},支持:{}".format(
                    type(self).__name__, self._frame.value, supported
                )
            )
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
            return data[0] if data_type is DataType.USHORT else convert.to_signed(data[0], 16)
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


class MelsecMcSerialClient(_MelsecMcBase):
    """三菱 MC 客户端(串口走线,C24 等串口通信模块,需要 pyserial)。

    - ``McFrame.FRAME_3C``:QnA 兼容 3C 帧,ASCII 通信格式 4(默认)
    - ``McFrame.FRAME_4C``:QnA 扩展 4C 帧,二进制通信格式 5

    串口参数须在连接前配置(与 :class:`~omniplc.modbus.ModbusRtuClient`
    一致的 ``configure_serial`` 惯例);波特率/校验位等须与 C24 侧
    "传送设定" 一致。

    :example::

        client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
        client.configure_serial("COM3", 9600)
        client.connect()
    """

    _SUPPORTED_FRAMES: Tuple[McFrame, ...] = (McFrame.FRAME_3C, McFrame.FRAME_4C)

    def __init__(
        self,
        frame: Union[McFrame, str] = McFrame.FRAME_3C,
        station_number: int = MC_SERIAL_DEFAULT_STATION,
        network_number: int = MC_SERIAL_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_SERIAL_DEFAULT_PC_NUMBER,
        self_station_number: int = MC_SERIAL_DEFAULT_SELF_STATION,
        module_io: int = MC_SERIAL_DEFAULT_MODULE_IO,
        module_station: int = MC_SERIAL_DEFAULT_MODULE_STATION,
    ) -> None:
        """初始化 MC 串口客户端(默认访问连接站 CPU)。

        :param frame: 帧型,``McFrame.FRAME_3C``(ASCII 格式 4)或
            ``McFrame.FRAME_4C``(二进制格式 5);也兼容 ``"3C"``/``"4C"`` 字符串
        :param station_number: 站号 0~31(0 = 连接站/主机站)
        :param network_number: 网络编号(0 = 本网络)
        :param pc_number: PC 编号(0~3 或 0xFF;0xFF = 连接站 CPU)
        :param self_station_number: 本站号(m:n 多点连接时外部设备自身站号)
        :param module_io: 请求目标模块 I/O 编号(4C 帧使用,CPU 直连 0x03FF)
        :param module_station: 请求目标模块局号(4C 帧使用,CPU 直连 0)
        :raises ValueError: 参数非法
        """
        BaseClient.__init__(self, "", 0)
        self._init_frame(frame, network_number, pc_number)
        self._pc_number = codec_serial.check_pc_number(pc_number)
        self._station_number = codec_serial.check_station_number(station_number)
        self._self_station_number = codec_serial.check_byte_field(
            "本站号", self_station_number
        )
        self._module_io = codec_serial.check_byte_field(
            "目标模块 I/O 编号", module_io, 0xFFFF
        )
        self._module_station = codec_serial.check_byte_field(
            "目标模块局号", module_station
        )
        self._serial_config: Optional[SerialConfig] = None

    @property
    def station_number(self) -> int:
        """当前站号。"""
        return self._station_number

    @property
    def pc_number(self) -> int:
        """当前 PC 编号。"""
        return self._pc_number

    @property
    def module_io(self) -> int:
        """请求目标模块 I/O 编号(仅 4C 帧)。"""
        return self._module_io

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(必须在 connect 之前调用)。

        :param port_name: 串口名,如 ``"COM3"``(Windows)或 ``"/dev/ttyS0"``
        :param baud_rate: 波特率,默认 9600(须与 C24 传送设定一致)
        :param data_bits: 数据位 5~8,默认 8
        :param stop_bits: 停止位 1/1.5/2,默认 1
        :param parity: 校验位,推荐 :class:`omniplc.types.SerialParity` 枚举,
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

    def _create_transport(self) -> BaseTransport:
        if self._serial_config is None:
            raise ValueError("请先调用 configure_serial() 配置串口参数")
        return SerialTransport(self._serial_config)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """按 3C/4C 帧型构造完整请求帧(内部方法)。"""
        if self._frame is McFrame.FRAME_3C:
            return codec_serial.build_3c_request(
                self._station_number,
                self._network_number,
                self._pc_number,
                self._self_station_number,
                parsed,
                points,
                is_bit,
                is_write,
                data,
            )
        return codec_serial.build_4c_request(
            self._station_number,
            self._network_number,
            self._pc_number,
            self._module_io,
            self._module_station,
            self._self_station_number,
            parsed,
            points,
            is_bit,
            is_write,
            data,
        )

    def _parse_read(self, response: bytes, points: int, is_bit: bool) -> List[int]:
        """按当前串口帧型解析读响应(内部方法)。"""
        return self._parse_serial(response, points, is_bit, is_read=True)

    def _parse_write(self, response: bytes) -> None:
        """按当前串口帧型校验写响应(错误代码非 0 抛 DeviceError,内部方法)。"""
        self._parse_serial(response, 0, False, is_read=False)

    def _parse_serial(
        self, response: bytes, points: int, is_bit: bool, is_read: bool
    ) -> List[int]:
        """串口帧响应解析分发(内部方法)。"""
        if self._frame is McFrame.FRAME_3C:
            return codec_serial.parse_3c_response(response, points, is_bit, is_read)
        return codec_serial.parse_4c_response(response, points, is_bit, is_read)

    def _read_tail_size(self, points: int, is_bit: bool) -> int:
        """3C 读响应 ETX 之前的数据字符数(4C 由长度域决定,传 0)。"""
        if self._frame is McFrame.FRAME_3C:
            return points if is_bit else points * 4
        return 0

    def _transact(self, request: bytes, tail_size: int = 0) -> bytes:
        """发送请求并按串口帧格式接收完整响应(内部方法)。

        3C:首字节分流控制码——STX 收正文+ETX+和校验+CR LF,
        ACK 收帧识别码+路由+CR LF,NAK 另加错误代码;
        4C:DLE STX 起始,按长度域(处理附加码)收正文至 DLE ETX+和校验,
        并重组为未填充的逻辑帧交解析层。
        """
        transport = self._require_transport()
        transport.send(request)
        if self._frame is McFrame.FRAME_4C:
            return self._transact_4c(transport)
        return self._transact_3c(transport, tail_size)

    @staticmethod
    def _transact_3c(transport: BaseTransport, tail_size: int) -> bytes:
        """3C 收包:控制码分流(内部方法)。"""
        head = transport.recv(1)
        code = head[0]
        if code == codec_serial.STX:
            # 帧识别码(2) + 路由回显(8) + 数据 + ETX(1) + 和校验(2) + CR LF(2)
            return head + transport.recv(10 + tail_size + 5)
        if code == codec_serial.ACK:
            return head + transport.recv(12)
        if code == codec_serial.NAK:
            return head + transport.recv(16)
        raise ProtocolFrameError("3C 响应控制码非法:0x{:02X}".format(code))

    @staticmethod
    def _transact_4c(transport: BaseTransport) -> bytes:
        """4C 收包:长度域 + 附加码还原,重组逻辑帧(内部方法)。"""
        head = transport.recv(2)
        if head != bytes([codec_serial.DLE, codec_serial.STX]):
            raise ProtocolFrameError(
                "4C 响应必须以 DLE STX 开头:0x{:02X} 0x{:02X}".format(head[0], head[1])
            )
        first = transport.recv(1)[0]
        if first == codec_serial.DLE:
            first = transport.recv(1)[0]
        second = transport.recv(1)[0]
        if second == codec_serial.DLE:
            second = transport.recv(1)[0]
        length = first | second << 8
        if length < 12:
            raise ProtocolFrameError(
                "4C 应答数据长非法(至少含帧识别码+路由+应答识别码+结束代码):{}".format(
                    length
                )
            )
        frame_id = transport.recv(1)
        if frame_id[0] != MC_SERIAL_FRAME_ID_4C:
            raise ProtocolFrameError(
                "4C 帧识别码不符:期望 F8H,收到 0x{:02X}".format(frame_id[0])
            )
        body = bytearray()
        while len(body) < length - 1:
            raw = transport.recv(1)[0]
            if raw == codec_serial.DLE:
                following = transport.recv(1)[0]
                if following != codec_serial.DLE:
                    raise ProtocolFrameError(
                        "4C 附加码之后必须是 10H,收到 0x{:02X}".format(following)
                    )
            body.append(raw)
        trailer = transport.recv(4)
        return length.to_bytes(2, "little") + frame_id + bytes(body) + trailer


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


def _decode_32(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """两字数据按类型解码(MC 为小端字序:低字在前,内部函数)。"""
    raw = convert.words_to_bytes(data)
    if data_type is DataType.INT:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.UINT:
        return int.from_bytes(raw, "little", signed=False)
    return struct.unpack("<f", raw)[0]


def _decode_64(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """四字数据按类型解码(小端字序,内部函数)。"""
    raw = convert.words_to_bytes(data)
    if data_type is DataType.LONG:
        return int.from_bytes(raw, "little", signed=True)
    if data_type is DataType.ULONG:
        return int.from_bytes(raw, "little", signed=False)
    return struct.unpack("<d", raw)[0]


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
    return convert.bytes_to_words(raw)


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
    return convert.bytes_to_words(raw)
