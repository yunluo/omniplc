"""三菱 MELSEC MC 协议客户端(3E/4E/1E 帧 × TCP/UDP 走线 + 1C/3C/4C 串口帧)。

类继承::

    BaseClient
    ├── MelsecMcTcpClient     3E/4E/1E 帧 over TCP(默认端口 2000)
    ├── MelsecMcUdpClient     3E/4E/1E 帧 over UDP(默认端口 2000)
    └── MelsecMcSerialClient  1C/3C/4C 帧 over 串口(C24,9600,需 pyserial)

以太网与串口走线共享同一套软元件码表与核心命令(:mod:`.codec_qna`),
帧封装按帧型分发:1E → :mod:`.codec_a`,3E/4E → :mod:`.codec_qna`,
3C/4C → :mod:`.codec_serial`,1C(A 兼容,命令 BR/WR/BW/WW)→
:mod:`.codec_serial_a`;接收策略按走线区分:TCP 按响应头长度
分段收包,UDP 整包接收,串口按控制码与长度域逐段收包。
"""

from __future__ import annotations

from abc import abstractmethod
import time
from typing import Dict, List, Optional, Sequence, Tuple, Union

from . import codec_a, codec_qna, codec_serial, codec_serial_a
from .address import McAddress, parse_mc_address
from ...core import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    MC_1C_DEFAULT_MESSAGE_WAIT,
    MC_1E_ERROR_EXTRA,
    MC_1E_ERROR_EXTRA_SIZE,
    MC_1E_RESPONSE_HEAD_SIZE,
    MC_4E_RESPONSE_HEAD_SIZE,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
    MC_1C_MAX_BIT_READ_POINTS,
    MC_1C_MAX_WORD_POINTS,
    MC_1E_MAX_POINTS,
    MC_1E_MAX_WORD_READ_POINTS,
    MC_MAX_DATAGRAM,
    MC_MAX_TRANSFER_POINTS,
    MC_MODULE_IO_MAX,
    MC_RESPONSE_HEAD_SIZE,
    MC_SERIAL_DEFAULT_MODULE_IO,
    MC_SERIAL_DEFAULT_MODULE_STATION,
    MC_SERIAL_DEFAULT_NETWORK_NUMBER,
    MC_SERIAL_DEFAULT_PC_NUMBER,
    MC_SERIAL_DEFAULT_SELF_STATION,
    MC_SERIAL_DEFAULT_STATION,
    MC_SERIAL_FRAME_ID_4C,
    MC_DEVICE_CODES,
    MC_SERIAL_MAX_FRAME,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
)
from ...core.errors import ProtocolFrameError, TransportClosedError
from ...core.validation import (
    check_byte_field,
    check_int16,
    check_uint16,
    require_bool,
    require_int,
)
from ...transport import (
    BaseTransport,
    SerialConfig,
    SerialTransport,
    TcpTransport,
    UdpTransport,
)
from ...core.types import ByteOrder, DataType, McFrame, PrimitiveValue, SerialParity
from ...core.i18n import _

# iQ-F(FX5U)X/Y 八进制口径码表(其余软元件与 Q/L/R 同):xy_octal=True 时
# 经 :meth:`_MelsecMcBase._effective_codes` 生效(组帧与校验共用,见 SH-080008)
_MC_DEVICE_CODES_FX5U_XY: Dict[str, Tuple[int, int, int]] = {
    **MC_DEVICE_CODES,
    "X": (0x9C, 1, 8),
    "Y": (0x9D, 1, 8),
}


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

    # X/Y 八进制口径(FX5U);串口走线不适用,类属性兜底(False)
    _xy_octal: bool = False

    # 位软元件是否允许按字单位成批访问(0406 字块)。默认 False = 拒绝
    # (三菱 M/X/Y… 字单位访问须 16 点对齐,易错读);品牌兼容子类按其
    # 地址模型覆写(如松下 R 为"字号×16+位号"位软元件,按字访问是正常用法)。
    _bit_device_word_access_allowed: bool = False

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
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径;
            默认 False = Q/L/R 十六进制口径)。仅 3E/4E 帧生效
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, port)
        self._xy_octal = bool(xy_octal)
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
                _("{} 不支持帧型 {},支持:{}").format(
                    type(self).__name__, self._frame.value, supported
                )
            )
        # 探活按帧型启用:0101 CPU 型号读仅 3E/4E 帧支持(SH-080008 §11.2),
        # 1E/3C/4C 帧实例不启用 ping 与自动心跳。实例属性覆盖类属性,
        # 子类(如汇川 H5U)可在 __init__ 中再置 False 显式关闭。
        self._has_ping = self._frame in (McFrame.FRAME_3E, McFrame.FRAME_4E)
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
    # 协议原语(BaseClient 类型化方法只调用 _read/_write)
    # ------------------------------------------------------------------

    def _check_bit_device_word_access(self, parsed: McAddress) -> None:
        """位软元件字单位访问门控(读/写共用,内部方法)。

        位软元件塞进字单位请求会被 PLC 拒绝或按 16 点/字错读写——读侧
        (第八轮 P2-14)与写侧(review-1018 P2-1)共用此防线,与
        read_batch 的 0406 字块同口径。未知名不在此处报错(事务组帧路径
        的 ``_device_info`` 原样上抛)。
        """
        try:
            _code, is_bit_device, _base = self._device_info(parsed.device)
        except ValueError:
            return
        if is_bit_device and not self._bit_device_word_access_allowed:
            raise ValueError(
                _(
                    "MC 位软元件 {}{} 只支持 BOOL,字单位请改用字软元件"
                    "(如 D)或逐点位读"
                ).format(parsed.device, parsed.number)
            )

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """MC 读原语:软元件地址 → 成批读请求 → 按类型解码(MC 字序小端)。

        位软元件按**字单位**访问(``read("M16", SHORT)``)受
        :attr:`_bit_device_word_access_allowed` 门控(第八轮 P2-14:单点
        路径原绕过门控,与 read_batch 的 0406 字块同防线);字软元件读
        BOOL 不受影响。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        # 位软元件按字单位访问受门控(第八轮 P2-14):_device_info 对未知
        # 软元件的报错时机与旧实现一致(事务组帧路径),此处兜底不前移
        self._check_bit_device_word_access(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            data = self._read_words(parsed, 1)
            return (
                data[0]
                if data_type is DataType.USHORT
                else convert.to_signed(data[0], 16)
            )
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            data = self._read_words(parsed, 2)
            return _decode_32(data, data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            data = self._read_words(parsed, 4)
            return _decode_64(data, data_type)
        raise ValueError(_("MC 不支持的数据类型:{}").format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """MC 写原语:成批写请求。位软元件按位写;字软元件按位写用读-改-写。

        位软元件按**字单位**写(``write_short("M16", 5)``)受
        :attr:`_bit_device_word_access_allowed` 门控(review-1018 P2-1:
        写侧原缺门控静默按 16 点/字写,与读侧第八轮 P2-14 同防线)。"""
        parsed = parse_mc_address(address)
        if data_type is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        if data_type is DataType.BOOL:
            flag = require_bool(value)
            _unused, is_bit_device, _unused = self._device_info(parsed.device)
            if is_bit_device:
                self._write_bits(parsed, [1 if flag else 0])
            else:
                words = self._read_words(parsed, 1)
                self._write_words(
                    parsed, [convert.set_bit(words[0], parsed.bit or 0, flag)]
                )
            return
        self._check_bit_device_word_access(parsed)
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
        raise ValueError(_("MC 不支持的数据类型:{}").format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """从字软元件读字符串:逐字小端拼字节后解码(MC 字序约定)。"""
        parsed = parse_mc_address(address)
        if parsed.bit is not None:
            raise ValueError(_("字符串地址不支持位号后缀:{!r}").format(address))
        words = self._read_words(parsed, (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "little") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """向字软元件写字符串:编码 → 补齐偶数字节 → 逐字小端。"""
        parsed = parse_mc_address(address)
        if parsed.bit is not None:
            raise ValueError(_("字符串地址不支持位号后缀:{!r}").format(address))
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [
            int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)
        ]
        self._write_words(parsed, words)
        return value

    # ------------------------------------------------------------------
    # 批量读取(3E/4E 走 0406 多块批量读,单事务)
    # ------------------------------------------------------------------

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:同软元件起连续 ``count`` 个元素,0401 成批读单事务。

        位软元件 = 位单位成批读(SH-080008 §8.2;3E/4E/3C/4C 按
        :data:`MC_MAX_TRANSFER_POINTS` 分块口径收紧为**单笔直读**,1E 帧
        位上限 256/字上限 64 字、1C 帧 BR 上限 256——帧型分流,超限入参期
        拒绝);字软元件
        = 字单位成批读,16 位类型 1 字/元素、32 位 2 字、64 位 4 字
        (SH-080008 §8.2 成批读;1C 帧 WR 上限 64 字)。所有帧型
        (3E/4E/1E/3C/4C)均支持——0401 是各帧共有的核心命令
        (1E 副头部位/字读 = MC_1E_READ_BIT/WORD,3C/4C 走 codec_serial)。

        :param address: 起始软元件地址(如 ``"D100"``、``"M0"``、``"X1F"``)
        :param count: 元素个数(按 ``data_type`` 计,INT×10 = 20 字)
        :param data_type: 数据类型(数值类型需字软元件;位软元件仅 BOOL;
            字软元件位号后缀 ``D100.3`` 仅 BOOL 单点,不支持 range)
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 点数超限 /
            位软元件按字单位访问被门控
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        data_type_enum = DataType.coerce(data_type)
        if data_type_enum is DataType.STRING:
            raise ValueError(_("read_range 不支持 STRING,请用 read_string"))
        # 帧型上限分流(review-1002 P2;review-1009 P2-1:1E 位/字分口——
        # Appendix 5 印刷页 466 位单位 256/字单位 64,原共用 255 会把 65~255
        # 字超限帧放行给 PLC 拒)——入口统一 900 会放行 1E/1C 超限帧——
        # codec ValueError 在事务锁内抛出穿透 _execute(其不捕 ValueError),
        # 违反整批 (False, None) 契约,故按帧型在入参期拒绝
        if self._frame is McFrame.FRAME_1E:
            bit_limit = MC_1E_MAX_POINTS
            word_limit = MC_1E_MAX_WORD_READ_POINTS
        elif self._frame is McFrame.FRAME_1C:
            bit_limit = MC_1C_MAX_BIT_READ_POINTS
            word_limit = MC_1C_MAX_WORD_POINTS
        else:
            bit_limit = word_limit = MC_MAX_TRANSFER_POINTS
        if count > bit_limit:
            raise ValueError(
                _("MC read_range 点数超上限 {}(帧型 {}):{}").format(
                    bit_limit, self._frame.value, count
                )
            )
        # 注意:此处**不**预调 _translate_address——原语 _read_bits/_read_words
        # 经 _build_frame 组帧,品牌兼容子类(松下/汇川)在 _build_frame 内
        # 恰好换算一次;预换算会二次应用非幂等线性化(松下 R1003→1603→2563
        # 静默错软元件,汇川 X/Y 十六进制编号二次八进制解析抛错),与单点
        # _read(不预换算)对齐,门控也须按品牌原记号判定
        parsed = parse_mc_address(address)
        if data_type_enum is not DataType.BOOL and parsed.bit is not None:
            raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
        _code, is_bit_device, _base = self._device_info(parsed.device)
        if data_type_enum is DataType.BOOL:
            if not is_bit_device and parsed.bit is None:
                # 字软元件无位号:连续"字 bit0"语义不明,拒绝(同 Modbus 口径)
                raise ValueError(
                    _(
                        "MC read_range 的 BOOL 需要位软元件(如 M0)或字软元件位号"
                        "单点读(如 D100.3),收到:{!r}"
                    ).format(address)
                )
            if not is_bit_device:
                raise ValueError(
                    _("MC read_range 不支持字软元件位号后缀:{!r}(请逐点读)").format(
                        address
                    )
                )
            codec_qna.reject_bit_suffix_on_bit_device(parsed)

            def operation_bits() -> List[PrimitiveValue]:
                return [bool(bit) for bit in self._read_bits(parsed, count)]

            ok, values = self._execute(operation_bits)
        else:
            if is_bit_device and not self._bit_device_word_access_allowed:
                raise ValueError(
                    _(
                        "MC 位软元件 {}{} 只支持 BOOL,字单位请改用字软元件"
                        "(如 D)或逐点位读"
                    ).format(parsed.device, parsed.number)
                )
            width = 1
            if data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
                width = 2
            elif data_type_enum in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
                width = 4
            if count * width > word_limit:
                raise ValueError(
                    _("MC read_range 字数超上限 {}(帧型 {}):{}×{}={}").format(
                        word_limit, self._frame.value, count, width, count * width
                    )
                )

            def operation_words() -> List[PrimitiveValue]:
                words = self._read_words(parsed, count * width)
                values: List[PrimitiveValue] = []
                for index in range(count):
                    chunk = words[index * width : (index + 1) * width]
                    if data_type_enum in (DataType.SHORT, DataType.USHORT):
                        values.append(
                            chunk[0]
                            if data_type_enum is DataType.USHORT
                            else convert.to_signed(chunk[0], 16)
                        )
                    elif data_type_enum in (
                        DataType.INT,
                        DataType.UINT,
                        DataType.FLOAT,
                    ):
                        values.append(_decode_32(chunk, data_type_enum))
                    else:
                        values.append(_decode_64(chunk, data_type_enum))
                return values

            ok, values = self._execute(operation_words)
        if not ok or values is None:
            return False, None
        return True, values

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:3E/4E 帧覆写为 0406 多块批量读(单事务)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        其余帧型(1E/3C/4C)沿用基类逐点独立事务。

        :param addresses: 地址列表(软元件可各不相同)
        :param data_type: 统一数据类型
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表
        """
        if self._frame not in (McFrame.FRAME_3E, McFrame.FRAME_4E):
            return super().read_many(addresses, data_type)
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
        """多块批量读取:0406 单事务混读多个字/位软元件(仅 3E/4E 帧)。

        利用 MC 协议原生"多块批量读"能力,一帧内软元件/类型可各不相同
        (SH-080008 §8.4):32/64 位类型各占 2/4 字,**BOOL 位软元件:同软元件
        且编号连续的位请求合并为一个位块(1 点 = 16 位)**,BOOL 字软元件占
        1 个字块后本地提位;字块 + 位块总数上限 120(子命令 0000;不支持
        iQ-R 扩展 0002 的链接直接/模块访问)。字符串请用
        :meth:`read_string`(变长不适合混读)。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空/帧型不支持/地址或类型非法
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        if self._frame not in (McFrame.FRAME_3E, McFrame.FRAME_4E):
            raise ValueError(
                _("多块批量读仅支持 3E/4E 帧,当前帧型:{}").format(self._frame.value)
            )
        word_blocks: List[Tuple[int, int, int]] = []
        bit_requests: List[Tuple[int, int, int]] = []  # (软元件码, 起始编号, plan 下标)
        # 解码计划:(类别, 字/位索引, 位号或字数, 数据类型)
        plan: List[Tuple[str, int, int, DataType]] = []
        word_index = 0
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            parsed = self._translate_address(parse_mc_address(address))
            if data_type_enum is not DataType.BOOL and parsed.bit is not None:
                raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
            code, is_bit_device, base = self._device_info(parsed.device)
            if (
                is_bit_device
                and data_type_enum is not DataType.BOOL
                and not self._bit_device_word_access_allowed
            ):
                # 位软元件塞进 0406 字块会被 PLC 拒绝或按 16 点/字错读;
                # 位软元件只用 BOOL,字单位请改字软元件(如 D)
                raise ValueError(
                    _(
                        "MC 批量读:位软元件 {}{} 只支持 BOOL,字单位请改用字软元件"
                        "(如 D)或逐点读取"
                    ).format(parsed.device, parsed.number)
                )
            number = codec_qna.device_number(parsed.device, parsed.number, base)
            if data_type_enum is DataType.BOOL:
                if is_bit_device:
                    codec_qna.reject_bit_suffix_on_bit_device(parsed)
                    plan_index = len(plan)
                    plan.append(("bit", 0, 0, data_type_enum))  # 占位,合并后回填
                    bit_requests.append((code, number, plan_index))
                else:
                    word_blocks.append((code, number, 1))
                    plan.append(
                        ("wordbit", word_index, parsed.bit or 0, data_type_enum)
                    )
                    word_index += 1
                continue
            if data_type_enum in (DataType.SHORT, DataType.USHORT):
                word_blocks.append((code, number, 1))
                plan.append(("word", word_index, 1, data_type_enum))
                word_index += 1
            elif data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
                word_blocks.append((code, number, 2))
                plan.append(("word", word_index, 2, data_type_enum))
                word_index += 2
            elif data_type_enum in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
                word_blocks.append((code, number, 4))
                plan.append(("word", word_index, 4, data_type_enum))
                word_index += 4
            else:
                raise ValueError(
                    _("MC 批量读取不支持的数据类型:{}").format(data_type_enum)
                )
        word_points = word_index
        bit_blocks, bit_points = _merge_bit_blocks(bit_requests, plan)

        def operation() -> List[PrimitiveValue]:
            request = codec_qna.build_random_read(
                self._frame.value,
                self._next_serial(),
                self._network_number,
                self._pc_number,
                MC_DEFAULT_MONITOR_TIMER,
                word_blocks,
                bit_blocks,
            )
            words, bits = codec_qna.parse_random_read_response(
                self._transact(request),
                self._frame.value,
                word_points,
                bit_points,
                expected_serial=self._serial,
            )
            values: List[PrimitiveValue] = []
            for kind, index, extra, item_type in plan:
                if kind == "bit":
                    values.append(bool(bits[index] >> extra & 1))
                elif kind == "wordbit":
                    values.append(bool(convert.get_bit(words[index], extra)))
                elif item_type in (DataType.SHORT, DataType.USHORT):
                    values.append(
                        words[index]
                        if item_type is DataType.USHORT
                        else convert.to_signed(words[index], 16)
                    )
                elif item_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
                    values.append(_decode_32(words[index : index + 2], item_type))
                else:
                    values.append(_decode_64(words[index : index + 4], item_type))
            return values

        return self._execute(operation)

    def random_read(
        self,
        word_items: Sequence[Tuple[str, Union[DataType, str]]],
        double_word_items: Sequence[Tuple[str, Union[DataType, str]]] = (),
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """随机读:0403 单事务乱序读取不连续软元件(仅 3E/4E 帧)。

        与 :meth:`read_batch`(0406 多块,**同软元件连续地址合并**)互补:
        0403 支持任意不连续编号、逐点指定(如 ``D0``/``D500``/``M100``,
        无需连续),响应按请求顺序返回(SH-080008 §8.3 印刷页 97-100)。
        字访问点数 + 双字访问点数 ≤ 192(iQ-R/L/Q/L 子命令 0000;QnA 96)。
        位软元件按 16 点/字、双字访问按 32 点/双字指定;长定时器/长计数器
        不可访问。字符串请用 :meth:`read_string`。

        :param word_items: 字访问 ``(地址, 数据类型)`` 序列;类型限
            **SHORT/USHORT**(16 位)/ BOOL(位软元件);32 位类型(INT/UINT/
            FLOAT)须放 ``double_word_items``(0403 字访问 1 字/点,双字访问
            2 字/点;SH-080008 §8.3)
        :param double_word_items: 双字访问 ``(地址, 数据类型)`` 序列;
            类型限 INT/UINT/FLOAT(按 32 位读取,响应 4 字节/点;
            0403 双字 = 32 位,LONG/ULONG/DOUBLE 64 位类型不支持)
        :return: ``(是否成功, 与 word_items + double_word_items 顺序
            对应的值列表)``
        :raises ValueError: 列表为空/帧型不支持/地址或类型非法
        """
        if not word_items and not double_word_items:
            raise ValueError(_("random_read 至少需要一个字访问或双字访问软元件"))
        if self._frame not in (McFrame.FRAME_3E, McFrame.FRAME_4E):
            raise ValueError(
                _("随机读仅支持 3E/4E 帧,当前帧型:{}").format(self._frame.value)
            )
        word_devices, word_plan = self._random_plan(word_items, is_double=False)
        dword_devices, dword_plan = self._random_plan(double_word_items, is_double=True)
        # 解码计划为全局连续索引:字项索引 = words 下标;双字项索引 =
        # len(word_devices) 起的 dwords 下标
        word_count = len(word_devices)
        plan = [(index, item_type) for index, item_type in word_plan] + [
            (index + word_count, item_type) for index, item_type in dword_plan
        ]

        def operation() -> List[PrimitiveValue]:
            request = codec_qna.build_random_read_devices(
                self._frame.value,
                self._next_serial(),
                self._network_number,
                self._pc_number,
                MC_DEFAULT_MONITOR_TIMER,
                word_devices,
                dword_devices,
            )
            words, dwords = codec_qna.parse_random_read_devices_response(
                self._transact(request),
                self._frame.value,
                len(word_devices),
                len(dword_devices),
                expected_serial=self._serial,
            )
            values: List[PrimitiveValue] = []
            for index, item_type in plan:
                if item_type in (DataType.BOOL, DataType.SHORT, DataType.USHORT):
                    raw = words[index]
                    if item_type is DataType.BOOL:
                        values.append(bool(raw & 1))
                    elif item_type is DataType.SHORT:
                        values.append(convert.to_signed(raw, 16))
                    else:
                        values.append(raw)
                else:
                    # INT/UINT/FLOAT:字访问走两字解码;双字访问按 32 位原始值还原
                    if index < word_count:
                        values.append(
                            _decode_32(list(words[index : index + 2]), item_type)
                        )
                    else:
                        values.append(
                            _decode_dword(dwords[index - word_count], item_type)
                        )
            return values

        return self._execute(operation)

    def _random_plan(
        self, items: Sequence[Tuple[str, Union[DataType, str]]], is_double: bool
    ) -> Tuple[List[Tuple[int, int]], List[Tuple[int, DataType]]]:
        """随机读/写入参规划:地址→(码, 编号) 并生成解码计划(内部方法)。

        :raises ValueError: 地址/类型与访问宽度不符
        """
        devices: List[Tuple[int, int]] = []
        plan: List[Tuple[int, DataType]] = []
        allowed = (
            (DataType.INT, DataType.UINT, DataType.FLOAT)
            if is_double
            else (DataType.BOOL, DataType.SHORT, DataType.USHORT)
        )
        for index, (address, data_type) in enumerate(items):
            data_type_enum = DataType.coerce(data_type)
            if data_type_enum not in allowed:
                raise ValueError(
                    _("MC 随机访问{}软元件类型不符:{}(允许:{})").format(
                        "双字" if is_double else "字",
                        data_type_enum,
                        "/".join(t.value for t in allowed),
                    )
                )
            parsed = self._translate_address(parse_mc_address(address))
            code, is_bit_device, base = self._device_info(parsed.device)
            if data_type_enum is not DataType.BOOL and parsed.bit is not None:
                raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
            if data_type_enum is DataType.BOOL:
                if not is_bit_device:
                    raise ValueError(
                        _("MC 随机读 BOOL 需要位软元件:{!r}").format(address)
                    )
                codec_qna.reject_bit_suffix_on_bit_device(parsed)
            if data_type_enum is not DataType.BOOL and is_bit_device:
                raise ValueError(
                    _("MC 随机读:位软元件 {}{} 只支持 BOOL").format(
                        parsed.device, parsed.number
                    )
                )
            number = codec_qna.device_number(parsed.device, parsed.number, base)
            devices.append((code, number))
            plan.append((index, data_type_enum))
        return devices, plan

    def random_write(
        self,
        word_items: Sequence[Tuple[str, PrimitiveValue]],
        double_word_items: Sequence[Tuple[str, PrimitiveValue]] = (),
    ) -> bool:
        """随机写(测试):1402 单事务乱序写不连续软元件(仅 3E/4E 帧)。

        命令名 "test" 为手册原文(SH-080008 §8.3 印刷页 104-106):双字
        直接按 32 位写入。**本命令无响应数据**,PLC 只回应答头;加权点数
        (字 ×12 + 双字 ×14) ≤ 1920(QnA 960)。位软元件按 16 点/字指定,
        写值 0/1 写入该字单元的 bit0。

        :param word_items: 字访问 ``(地址, 值)`` 序列(16 位:0~65535;
            位软元件 0/1)
        :param double_word_items: 双字访问 ``(地址, 值)`` 序列(32 位:
            0~0xFFFFFFFF)
        :return: 是否成功
        :raises ValueError: 两列表均空/帧型不支持/地址或数值非法
        """
        if not word_items and not double_word_items:
            raise ValueError(_("随机写至少需要一个字访问或双字访问软元件"))
        if self._frame not in (McFrame.FRAME_3E, McFrame.FRAME_4E):
            raise ValueError(
                _("随机写仅支持 3E/4E 帧,当前帧型:{}").format(self._frame.value)
            )

        def plan_devices(
            items: Sequence[Tuple[str, PrimitiveValue]], byte_count: int
        ) -> List[Tuple[int, int, int]]:
            out: List[Tuple[int, int, int]] = []
            for address, value in items:
                parsed = self._translate_address(parse_mc_address(address))
                if parsed.bit is not None:
                    raise ValueError(
                        _(
                            "MC 随机写地址不支持位号后缀:{!r}(位软元件直接写编号,按 16 点/字)"
                        ).format(address)
                    )
                code, is_bit_device, base = self._device_info(parsed.device)
                number = codec_qna.device_number(parsed.device, parsed.number, base)
                # 位软元件按访问宽度指定点数:字访问 16 点/设备、双字访问
                # 32 点/设备——末编号 = 编号 + (每设备点数 - 1) 不得超过
                # 3 字节域(review-1009 P3-1:原双字分支误用字访问的 -15,
                # 偏保守不越规,顺手对齐)
                if is_bit_device and not 0 <= number <= 0xFFFFFF - (byte_count * 8 - 1):
                    raise ValueError(
                        _("MC 随机写位软元件编号越界:{}{}").format(
                            parsed.device, parsed.number
                        )
                    )
                number_value = require_int(value)
                if not 0 <= number_value <= (1 << (byte_count * 8)) - 1:
                    raise ValueError(
                        _("随机写值超出 {} 字节无符号范围:{}={}").format(
                            byte_count, address, number_value
                        )
                    )
                out.append((code, number, number_value))
            return out

        word_devices = plan_devices(word_items, 2)
        dword_devices = plan_devices(double_word_items, 4)

        def operation() -> None:
            request = codec_qna.build_random_write_devices(
                self._frame.value,
                self._next_serial(),
                self._network_number,
                self._pc_number,
                MC_DEFAULT_MONITOR_TIMER,
                word_devices,
                dword_devices,
            )
            # 1402 响应无数据但有结束码(SH-080008 §5.3 印刷页 46);
            # 结束码非 0 须抛 DeviceError,不可当成功
            response = self._transact(request)
            self._parse_write(response, False)

        ok, _unused = self._execute(operation, is_write=True)
        return ok

    def get_cpu_type(self) -> Tuple[bool, Optional[Tuple[str, int]]]:
        """读 CPU 型号(0101,SH-080008 §11.2 印刷页 176-178;仅 3E/4E 帧)。

        返回 ``(是否成功, (模型名, 模型代码))``;模型名去尾部空格
        (通信例:Q02UCPU → ``("Q02UCPU", 0x0263)``);模型代码对照
        手册 §11.1 印刷页 166。

        :raises ValueError: 帧型不支持
        """
        if self._frame not in (McFrame.FRAME_3E, McFrame.FRAME_4E):
            raise ValueError(
                _("CPU 型号读取仅支持 3E/4E 帧,当前帧型:{}").format(self._frame.value)
            )
        return self._execute(self._cpu_model_operation)

    def _ping_probe(self) -> Tuple[str, int]:
        """探活探测命令:0101 CPU 型号读(内部方法;仅 3E/4E 帧启用)。

        依据:SH-080008 §11.2 印刷页 176-178——只读系统信息,零副作用。
        KV MC 兼容(KeyenceMc*)与松下 MC 兼容(PanasonicMc*)继承本探针,
        两家对 0101 的支持面待真机核证(docs/real-machine-checklist.md)。
        """
        return self._cpu_model_operation()

    def _cpu_model_operation(self) -> Tuple[str, int]:
        """0101 CPU 型号读的协议操作(内部方法;公开方法与探活共用)。"""
        # 0101 同样按客户端路由字段组帧(跨网/他站访问他站 CPU);
        # 4E 响应按序列号回显校验
        request = codec_qna.build_read_cpu_model(
            self._frame.value,
            self._next_serial(),
            self._network_number,
            self._pc_number,
            MC_DEFAULT_MONITOR_TIMER,
        )
        return codec_qna.parse_read_cpu_model_response(
            self._transact(request), self._frame.value, expected_serial=self._serial
        )

    def _read_bool_impl(self, parsed: McAddress) -> bool:
        """位软元件按点位成批读;字软元件读 1 字后按位提取(合法路径)。

        位软元件按**字单位**访问(如 ``read("M16", SHORT)`` 走 0401 字
        单位)受 :attr:`_bit_device_word_access_allowed` 门控——与
        read_batch 的 0406 字块同防线(第八轮 P2-14:单点路径原绕过门控,
        组出的字单位读编号非 16 对齐会被 PLC 拒或按 16 点/字错读);
        字软元件读 BOOL(``D100`` 无位号)按字提取 bit0,不在门控范围。
        """
        _code, is_bit_device, _base = self._device_info(parsed.device)
        if is_bit_device:
            return bool(self._read_bits(parsed, 1)[0])
        words = self._read_words(parsed, 1)
        return convert.get_bit(words[0], parsed.bit or 0)

    def _read_bits(self, parsed: McAddress, count: int) -> List[int]:
        """位软元件成批读(位单位核心命令)。

        位号后缀校验在 :func:`codec_qna.build_core`(组帧层),与 native
        层及串口 3C/4C 共用同一防线;品牌兼容子类的记号换算先于该校验。
        """
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
        """位软元件成批写(位单位核心命令,位号后缀校验在组帧层)。"""
        request = self._build_frame(
            parsed, len(values), is_bit=True, is_write=True, data=values
        )
        self._parse_write(self._transact(request), True)

    def _write_words(self, parsed: McAddress, words: List[int]) -> None:
        """字软元件成批写(字单位核心命令)。"""
        request = self._build_frame(
            parsed, len(words), is_bit=False, is_write=True, data=words
        )
        self._parse_write(self._transact(request), False)

    # ------------------------------------------------------------------
    # 帧组装/解析分发与收包(3E/4E 与 1E 两套)
    # ------------------------------------------------------------------

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """按当前帧型查软元件码表(内部方法)。"""
        if self._frame is McFrame.FRAME_1E:
            return codec_a.device_info(device)
        if self._frame is McFrame.FRAME_1C:
            return codec_serial_a.device_info(device)
        return codec_qna.device_info(device, self._effective_codes())

    def _effective_codes(self) -> Optional[Dict[str, Tuple[int, int, int]]]:
        """生效软元件码表:xy_octal 时换 X/Y 八进制口径的 FX5U 变体(内部)。"""
        if not self._xy_octal:
            return None
        return _MC_DEVICE_CODES_FX5U_XY

    def _translate_address(self, parsed: McAddress) -> McAddress:
        """帧级地址换算钩子,默认透传(内部方法)。

        品牌兼容子类覆写(如汇川 R/X/Y 记号换算),批量读取路径与
        :meth:`_build_frame` 必须经同一钩子,保证两路地址语义一致。
        """
        return parsed

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """按当前帧型构造完整请求帧(内部方法)。

        **单点路径地址换算契约**:基类默认实现在此**不**调用
        :meth:`_translate_address`——单点 `_read/_write` 的调用方已先换算。
        品牌兼容子类(汇川/松下)若覆写 `_build_frame` 组帧,必须同时保证
        单点路径的地址换算(覆写 `_translate_address` 并在 `_build_frame`
        内换算,参照 ``plc/panasonic/mc.py``);只覆写其一会让单点或批量
        一路用未换算地址静默发帧。
        """
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

        1E 写响应副头部 = 请求副头部 + 0x80(位写 0x82 / 字写 0x83),
        须随操作传入是否位单位;3E/4E 读写响应副头部同为 D0 00,不区分。
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
        """4E 序列号递增(0~65535 回绕,内部方法)。"""
        return self._bump_id("_serial", 16)

    def _transact(self, request: bytes, tail_size: int = 0) -> bytes:
        """发送请求并接收完整响应帧(内部方法)。

        TCP:3E 收 9 字节头 + 应答数据长所示内容;4E 收 13 字节头;
        1E 收 2 字节头 + ``tail_size`` 数据(结束码 0x5B 时改收 2 字节扩展)。
        UDP:一次 recv 整包;解析层除校验长度域与数据长一致外,还拒绝
        **尾部多余字节**(数据报边界异常,防止把串包/多余载荷当正常帧)。
        """
        transport = self._require_transport()
        if transport.datagram:
            # 数据报走线发送前排空陈旧帧:上一事务超时后迟到的响应会被
            # 本轮 recv 误当应答;MC 帧无事务号,同帧型迟到响应可绕过
            # 解析校验造成静默错值,排空是唯一防线(陈旧帧防护)
            transport.drain()
        transport.send(request)
        if transport.datagram:
            return transport.recv(MC_MAX_DATAGRAM)
        if self._frame is McFrame.FRAME_1E:
            head = transport.recv(MC_1E_RESPONSE_HEAD_SIZE)
            if head[1] != 0 and head[1] != MC_1E_ERROR_EXTRA:
                return head  # 错误响应只有 2 字节头,无数据段(1E 帧无长度域)
            if head[1] == MC_1E_ERROR_EXTRA:
                return head + transport.recv(MC_1E_ERROR_EXTRA_SIZE)
            if tail_size:
                return head + transport.recv(tail_size)
            return head
        head_size = (
            MC_4E_RESPONSE_HEAD_SIZE
            if self._frame is McFrame.FRAME_4E
            else MC_RESPONSE_HEAD_SIZE
        )
        head = transport.recv(head_size)
        return head + transport.recv(
            codec_qna.parse_response_head(head, self._frame.value)
        )

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
        xy_octal: bool = False,
    ) -> None:
        """初始化 MC TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径,默认 False)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, frame, network_number, pc_number, xy_octal)

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
        xy_octal: bool = False,
    ) -> None:
        """初始化 MC UDP 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :param xy_octal: X/Y 编号按八进制解释(iQ-F/FX5U 口径,默认 False)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, frame, network_number, pc_number, xy_octal)

    def _create_transport(self) -> BaseTransport:
        return UdpTransport(self._ip_address, self._port)


class MelsecMcSerialClient(_MelsecMcBase):
    """三菱 MC 客户端(串口走线,C24 等串口通信模块,需要 pyserial)。

    - ``McFrame.FRAME_1C``:A 兼容 1C 帧,ASCII 通信格式 4(命令 BR/WR/BW/WW)
    - ``McFrame.FRAME_3C``:QnA 兼容 3C 帧,ASCII 通信格式 4(默认)
    - ``McFrame.FRAME_4C``:QnA 扩展 4C 帧,二进制通信格式 5

    串口参数须在连接前配置(与 :class:`~omniplc.plc.modbus.ModbusRtuClient`
    一致的 ``configure_serial`` 惯例);波特率/校验位等须与 C24 侧
    "传送设定" 一致。

    :example::

        client = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
        client.configure_serial("COM3", 9600)
        client.connect()
    """

    _SUPPORTED_FRAMES: Tuple[McFrame, ...] = (
        McFrame.FRAME_1C,
        McFrame.FRAME_3C,
        McFrame.FRAME_4C,
    )

    def __init__(
        self,
        frame: Union[McFrame, str] = McFrame.FRAME_3C,
        station_number: int = MC_SERIAL_DEFAULT_STATION,
        network_number: int = MC_SERIAL_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_SERIAL_DEFAULT_PC_NUMBER,
        self_station_number: int = MC_SERIAL_DEFAULT_SELF_STATION,
        module_io: int = MC_SERIAL_DEFAULT_MODULE_IO,
        module_station: int = MC_SERIAL_DEFAULT_MODULE_STATION,
        message_wait: int = MC_1C_DEFAULT_MESSAGE_WAIT,
    ) -> None:
        """初始化 MC 串口客户端(默认访问连接站 CPU)。

        :param frame: 帧型,``McFrame.FRAME_1C``(A 兼容 ASCII 格式 4)、
            ``McFrame.FRAME_3C``(QnA 兼容 ASCII 格式 4)或
            ``McFrame.FRAME_4C``(QnA 扩展二进制格式 5);
            也兼容 ``"1C"``/``"3C"``/``"4C"`` 字符串
        :param station_number: 站号 0~31(0 = 连接站/主机站)
        :param network_number: 网络编号(0 = 本网络;仅 3C/4C 使用)
        :param pc_number: PC 编号(0~3 或 0xFF;0xFF = 连接站 CPU)
        :param self_station_number: 本站号(m:n 多点连接时外部设备自身站号;仅 3C/4C 使用)
        :param module_io: 请求目标模块 I/O 编号(4C 帧使用,CPU 直连 0x03FF)
        :param module_station: 请求目标模块局号(4C 帧使用,CPU 直连 0)
        :param message_wait: 消息等待(仅 1C 帧,0~15,单位 10ms;
            C24 收到请求后开始发送响应的最短延迟,慢速外设可调大)
        :raises ValueError: 参数非法
        """
        BaseClient.__init__(self, "", 0)
        self._init_frame(frame, network_number, pc_number)
        self._pc_number = codec_serial.check_pc_number(pc_number)
        self._station_number = codec_serial.check_station_number(station_number)
        self._self_station_number = check_byte_field("本站号", self_station_number)
        self._module_io = check_byte_field(
            "目标模块 I/O 编号", module_io, MC_MODULE_IO_MAX
        )
        self._module_station = check_byte_field("目标模块局号", module_station)
        self._message_wait = codec_serial_a.check_message_wait(message_wait)
        self._serial_config: Optional[SerialConfig] = None

    @property
    def station_number(self) -> int:
        """当前站号。"""
        return self._station_number

    @property
    def module_io(self) -> int:
        """请求目标模块 I/O 编号(仅 4C 帧)。"""
        return self._module_io

    @property
    def self_station_number(self) -> int:
        """本站号(m:n 多点连接时外部设备自身站号)。"""
        return self._self_station_number

    @property
    def module_station(self) -> int:
        """请求目标模块局号(仅 4C 帧)。"""
        return self._module_station

    @property
    def message_wait(self) -> int:
        """消息等待(仅 1C 帧,0~15,单位 10ms)。"""
        return self._message_wait

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
            raise ValueError(_("请先调用 configure_serial() 配置串口参数"))
        return SerialTransport(self._serial_config)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """按 1C/3C/4C 帧型构造完整请求帧(内部方法)。"""
        if self._frame is McFrame.FRAME_1C:
            return codec_serial_a.build_1c_request(
                self._station_number,
                self._pc_number,
                self._message_wait,
                parsed,
                points,
                is_bit,
                is_write,
                data,
            )
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

    def _parse_write(self, response: bytes, is_bit: bool) -> None:
        """按当前串口帧型校验写响应(错误代码非 0 抛 DeviceError,内部方法)。"""
        self._parse_serial(response, 0, is_bit, is_read=False)

    def _parse_serial(
        self, response: bytes, points: int, is_bit: bool, is_read: bool
    ) -> List[int]:
        """串口帧响应解析分发(内部方法)。"""
        if self._frame is McFrame.FRAME_1C:
            return codec_serial_a.parse_1c_response(
                response, points, is_bit, is_read, self._station_number, self._pc_number
            )
        if self._frame is McFrame.FRAME_3C:
            return codec_serial.parse_3c_response(
                response,
                points,
                is_bit,
                is_read,
                self._station_number,
                self._network_number,
                self._pc_number,
                self._self_station_number,
            )
        return codec_serial.parse_4c_response(
            response,
            points,
            is_bit,
            is_read,
            self._station_number,
            self._network_number,
            self._pc_number,
            self._module_io,
            self._module_station,
            self._self_station_number,
        )

    def _read_tail_size(self, points: int, is_bit: bool) -> int:
        """1C/3C 读响应 ETX 之前的数据字符数(4C 由长度域决定,传 0)。"""
        if self._frame in (McFrame.FRAME_1C, McFrame.FRAME_3C):
            return points if is_bit else points * 4
        return 0

    def _transact(self, request: bytes, tail_size: int = 0) -> bytes:
        """发送请求并按串口帧格式接收完整响应(内部方法)。

        1C/3C:首字节分流控制码——STX 收正文+ETX+和校验+CR LF,
        ACK 收路由(+错误代码)+CR LF,NAK 另加错误代码;
        4C:DLE STX 起始,按长度域(处理附加码)收正文至 DLE ETX+和校验,
        并重组为未填充的逻辑帧交解析层。
        """
        transport = self._require_transport()
        transport.send(request)
        if self._frame is McFrame.FRAME_4C:
            return self._transact_4c(transport)
        if self._frame is McFrame.FRAME_1C:
            return self._transact_1c(transport, tail_size)
        return self._transact_3c(transport, tail_size)

    @staticmethod
    def _transact_1c(transport: BaseTransport, tail_size: int) -> bytes:
        """1C 收包:控制码分流(内部方法)。

        STX 后 = 站号/PC 号回显(4) + 数据 + ETX(1) + 和校验(2) + CR LF(2);
        ACK 后 = 回显(4) + CR LF(2);NAK 后 = 回显(4) + 错误代码(2) + CR LF(2)。
        """
        head = transport.recv(1)
        code = head[0]
        if code == codec_serial.STX:
            return head + transport.recv(4 + tail_size + 5)
        if code == codec_serial.ACK:
            return head + transport.recv(6)
        if code == codec_serial.NAK:
            return head + transport.recv(8)
        raise ProtocolFrameError(_("1C 响应控制码非法:0x{:02X}").format(code))

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
        raise ProtocolFrameError(_("3C 响应控制码非法:0x{:02X}").format(code))

    @staticmethod
    def _transact_4c(transport: BaseTransport) -> bytes:
        """4C 收包:长度域 + 附加码还原,重组逻辑帧(内部方法)。

        整帧受一次 ``receive_timeout`` 预算约束——不逐字节重置超时,防
        慢速对端长占事务锁。正文**成块**读取后在本地缓冲内解 DLE 附加码:
        每次请求"剩余未解字节数 + 4 字节尾部"上限内的块(附加码只增不减,
        该预算不会越过帧尾,不会把下一帧读进缓冲),系统调用从逐字节降为
        O(块数);DLE 配对跨块边界时由补读兜底。已消费帧头与若干正文后
        超时,残渣留在串口缓冲必致下一帧错位,按串口的截断语义拆连重同步
        (0 字节已读才算"链路无残渣",见 ``TransportTimeoutError``)。
        """
        previous_timeout = transport.receive_timeout
        deadline = time.monotonic() + previous_timeout
        buf = bytearray()
        pos = 0  # 已消费的线缆字节偏移

        def _fill(want: int) -> None:
            """按块读入缓冲(短读容忍;deadline 到点或传输空返回按帧截断断连)。

            传输合约要求"收满请求量或抛错"(串口/TCP 均如此),空返回属
            违约兜底:再无数据可来,按帧截断拆连,防 while 补读忙轮询烧 CPU。
            """
            remaining = deadline - time.monotonic()
            if remaining > 0:
                transport.receive_timeout = remaining
                chunk = transport.recv(want)
                if chunk:
                    buf.extend(chunk)
                    return
            raise TransportClosedError(
                _(
                    "4C 收包超时({}s),帧已截断(已收 {} 字节),"
                    "已放弃本帧,下次事务将重连以重新同步"
                ).format(previous_timeout, len(buf))
            )

        def _ensure(count: int) -> None:
            """确保缓冲内至少还有 count 个未消费字节(内部闭包)。"""
            while len(buf) - pos < count:
                _fill(count - (len(buf) - pos))

        try:
            _ensure(2)
            head = bytes(buf[pos : pos + 2])
            pos += 2
            if head != bytes([codec_serial.DLE, codec_serial.STX]):
                raise ProtocolFrameError(
                    _("4C 响应必须以 DLE STX 开头:0x{:02X} 0x{:02X}").format(
                        head[0], head[1]
                    )
                )
            _ensure(1)
            first = buf[pos]
            pos += 1
            if first == codec_serial.DLE:
                _ensure(1)
                first = buf[pos]
                pos += 1
            _ensure(1)
            second = buf[pos]
            pos += 1
            if second == codec_serial.DLE:
                _ensure(1)
                second = buf[pos]
                pos += 1
            length = first | second << 8
            if length < 12:
                raise ProtocolFrameError(
                    _(
                        "4C 应答数据长非法(至少含帧识别码+路由+应答识别码+结束代码):{}"
                    ).format(length)
                )
            if length > MC_SERIAL_MAX_FRAME:
                raise ProtocolFrameError(
                    _("4C 应答数据长超限:{} > {}").format(length, MC_SERIAL_MAX_FRAME)
                )
            _ensure(1)
            frame_id = bytes(buf[pos : pos + 1])
            pos += 1
            if frame_id[0] != MC_SERIAL_FRAME_ID_4C:
                raise ProtocolFrameError(
                    _("4C 帧识别码不符:期望 F8H,收到 0x{:02X}").format(frame_id[0])
                )
            body = bytearray()
            while len(body) < length - 1:
                if pos >= len(buf):
                    # 成块补充:剩余未解字节 + 4 字节尾部为安全上限(附加码
                    # 只增不减,该预算不会越过帧尾,避免阻塞读入下一帧)
                    _fill(length - 1 - len(body) + 4)
                raw = buf[pos]
                pos += 1
                if raw == codec_serial.DLE:
                    _ensure(1)
                    following = buf[pos]
                    pos += 1
                    if following != codec_serial.DLE:
                        raise ProtocolFrameError(
                            _("4C 附加码之后必须是 10H,收到 0x{:02X}").format(following)
                        )
                body.append(raw)
            _ensure(4)
            trailer = bytes(buf[pos : pos + 4])
            return length.to_bytes(2, "little") + frame_id + bytes(body) + trailer
        finally:
            transport.receive_timeout = previous_timeout


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------


def _merge_bit_blocks(
    bit_requests: Sequence[Tuple[int, int, int]],
    plan: List[Tuple[str, int, int, DataType]],
) -> Tuple[List[Tuple[int, int, int]], int]:
    """把连续的位软元件 BOOL 请求合并为 0406 位块(内部函数)。

    0406 位块 1 点 = 16 位软元件(响应 1 字,块内首软元件在 **bit0**,
    SH-080008 §8.4 印刷页 113-114 位图:字 0x2030 标注 M4/M5/M13 ON,
    即设备 k = 第 k//16 字的 bit k%16);同软元件且编号连续的请求合并为
    一块。逐块把解码项回填进 ``plan``(占位项),返回 ``(位块列表,
    位块总点数)``。非连续/跨软元件/乱序的请求各自成块。

    :param bit_requests: ``(软元件码, 起始编号, plan 下标)`` 序列(按请求顺序)
    :param plan: 解码计划(占位项将被回填为 ``("bit", 字索引, 位号, BOOL)``)
    """
    bit_blocks: List[Tuple[int, int, int]] = []
    base_word = 0
    index = 0
    total = len(bit_requests)
    while index < total:
        code, start, _unused = bit_requests[index]
        run = [bit_requests[index]]
        follow = index + 1
        while (
            follow < total
            and bit_requests[follow][0] == code
            and bit_requests[follow][1] == run[-1][1] + 1
        ):
            run.append(bit_requests[follow])
            follow += 1
        points = (len(run) + 15) // 16  # 1 点 = 16 位软元件
        bit_blocks.append((code, start, points))
        for offset, request in enumerate(run):
            plan[request[2]] = (
                "bit",
                base_word + offset // 16,
                offset % 16,
                DataType.BOOL,
            )
        base_word += points
        index = follow
    return bit_blocks, base_word


def _coerce_frame(value: Union[McFrame, str]) -> McFrame:
    """把枚举成员或字符串统一解析为 McFrame(内部函数)。"""
    if isinstance(value, McFrame):
        return value
    try:
        return McFrame(str(value).strip().upper())
    except ValueError:
        supported = "/".join(member.value for member in McFrame)
        raise ValueError(_("不支持的 MC 帧型:{!r},支持:{}").format(value, supported))


def _decode_32(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """两字数据按类型解码(MC 为小端字序:低字在前,内部函数)。"""
    return convert.words_to_value(data, data_type, ByteOrder.LITTLE)


def _decode_64(data: Sequence[int], data_type: DataType) -> PrimitiveValue:
    """四字数据按类型解码(小端字序,内部函数)。"""
    return convert.words_to_value(data, data_type, ByteOrder.LITTLE)


def _decode_dword(raw: int, data_type: DataType) -> PrimitiveValue:
    """随机读双字数据(32 位原始值)按类型解码(内部函数)。

    0403 双字访问响应为 4 字节小端原始值;有符号/浮点按位型还原。
    """
    data = convert.bytes_to_words(raw.to_bytes(4, "little"))
    return convert.words_to_value(data, data_type, ByteOrder.LITTLE)


def _encode_32(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 32 位值编码为 2 个字(小端字节序,内部函数)。"""
    return convert.value_to_words(value, data_type, ByteOrder.LITTLE)


def _encode_64(value: PrimitiveValue, data_type: DataType) -> List[int]:
    """按类型把 64 位值编码为 4 个字(小端字节序,内部函数)。"""
    return convert.value_to_words(value, data_type, ByteOrder.LITTLE)
