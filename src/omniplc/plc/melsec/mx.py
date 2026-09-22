"""三菱 MX Component 客户端(Windows 专用,COM 通道,comtypes 实现)。

不走路由报文,而是调用三菱 MX Component 的 ``ActUtlType`` COM 控件
(实用程序设置型):通信参数(IP/端口/协议/口令等)全部在
**通信设置实用程序**(Communication Setup Utility)中预先配置为逻辑站号,
本客户端只需要逻辑站号即可收发。

依赖:``pip install omniplc[mx]``(安装 comtypes),且目标机器已安装
MX Component Version 4 运行时。

手册要点(``MX Component Version 4 编程手册``,5.2 节):

- ``Open``/``Close``:返回值 0 = 正常,非 0 = 出错代码(第 7 章)
- ``ReadDeviceBlock``/``WriteDeviceBlock``:LONG 数组,每个元素的
  **低 16 位**为一个字软元件值(高位存储 0)
- ``GetDevice``/``SetDevice``:单点读/写;位软元件取/置最低位
- 位软元件的批量访问必须以 16 点为单位(位数指定),本驱动不使用
  位块批量,位操作一律走 ``GetDevice``/``SetDevice`` 单点
"""
from __future__ import annotations
import ctypes
from typing import Any, List, Optional, Sequence, Tuple, Union

from ... import convert
from ...core.base_client import BaseClient
from ...core.constants import (
    MX_BIT_DEVICES,
    MX_DEFAULT_LOGICAL_STATION,
    MX_LOGICAL_STATION_MAX,
    MX_MAX_BLOCK_WORDS,
    MX_PROG_ID,
)
from ...core.debug import log_op
from ...core.errors import OmniPLCInternalError, TransportClosedError
from ...core.validation import check_int16, check_uint16, require_bool
from ...transport import BaseTransport
from ...types import DataType, PrimitiveValue
from .address import McAddress, parse_mc_address
from .melsec import _decode_32, _decode_64, _encode_32, _encode_64


# ----------------------------------------------------------------------
# COM 交互辅助(模块级,单测以假对象替换;依赖 comtypes 延迟导入)
# ----------------------------------------------------------------------

def _com_initialize() -> None:
    """在使用线程上初始化 COM(ActUtlType 为 STA 控件,重复调用安全)。"""
    import comtypes

    comtypes.CoInitialize()


def _com_uninitialize() -> None:
    """配对 :func:`_com_initialize`:释放使用线程的 COM 初始化计数(尽力而为)。

    线程未初始化过 COM 时 CoUninitialize 会报错,静默忽略——aio 单工作
    线程与同步主线程场景计数可正常归零,避免连接/关闭循环累积计数。
    """
    try:
        import comtypes

        comtypes.CoUninitialize()
    except Exception:
        pass


def _new_com_object(logical_station_number: int) -> Any:
    """创建 ActUtlType COM 控件并设置逻辑站号。"""
    import comtypes.client

    com = comtypes.client.CreateObject(MX_PROG_ID)
    com.ActLogicalStationNumber = logical_station_number
    return com


def _check_rc(code: int, method: str) -> None:
    """校验控件方法返回码(0 = 正常,内部函数)。

    :raises OmniPLCInternalError: 返回非 0 出错代码(标记断开,
        下一次操作惰性重连)
    """
    if code != 0:
        raise OmniPLCInternalError(
            "MX Component {} 失败:返回码 {}".format(method, _format_code(code))
        )


def _com_get_device(com: Any, device_text: str) -> int:
    """单点读(GetDevice),返回 0~65535 原始值。"""
    value = ctypes.c_long()
    _check_rc(int(com.GetDevice(device_text, ctypes.byref(value))), "GetDevice")
    return int(value.value) & 0xFFFF


def _com_set_device(com: Any, device_text: str, value: int) -> None:
    """单点写(SetDevice);位软元件取最低位。"""
    _check_rc(int(com.SetDevice(device_text, int(value))), "SetDevice")


def _com_read_words(com: Any, device_text: str, count: int) -> List[int]:
    """批量读字软元件(ReadDeviceBlock),返回 0~65535 原始字列表。"""
    buffer = (ctypes.c_long * count)()
    _check_rc(int(com.ReadDeviceBlock(device_text, count, buffer)), "ReadDeviceBlock")
    return [int(word) & 0xFFFF for word in buffer]


def _com_write_words(com: Any, device_text: str, words: Sequence[int]) -> None:
    """批量写字软元件(WriteDeviceBlock)。"""
    buffer = (ctypes.c_long * len(words))(*[int(word) for word in words])
    _check_rc(int(com.WriteDeviceBlock(device_text, len(words), buffer)), "WriteDeviceBlock")


def _com_read_random(com: Any, device_list: str, count: int) -> List[int]:
    """随机读(ReadDeviceRandom):软元件列表以换行符分隔,返回原始字列表。"""
    buffer = (ctypes.c_long * count)()
    _check_rc(int(com.ReadDeviceRandom(device_list, count, buffer)), "ReadDeviceRandom")
    return [int(word) & 0xFFFF for word in buffer]


class _MxComLink(BaseTransport):
    """COM 会话适配器:把 ActUtlType 封装成传输对象外形。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 打开
    COM 通信线路,``close`` 关闭;超时属性由基类存储(MX Component
    的超时在通信设置实用程序中配置,控件不单独暴露)。无字节流收发。
    """

    def __init__(self, logical_station_number: int) -> None:
        super().__init__()
        self._logical_station_number = logical_station_number
        self._com: Any = None
        self._debug_label = "mx://站号{}".format(logical_station_number)

    def connect(self) -> None:
        """打开 COM 通信线路(Open)。

        :raises OSError: COM 初始化/控件创建失败,或 Open 返回非 0 出错代码
        """
        try:
            # ActUtlType 是 STA 控件:在使用线程上初始化 COM(幂等)
            _com_initialize()
            com = _new_com_object(self._logical_station_number)
        except Exception as exc:
            raise OSError(
                "MX Component 初始化失败:{}(请确认已安装 MX Component 运行时,"
                "并执行 pip install omniplc[mx])".format(exc)
            )
        code = int(com.Open())
        if code != 0:
            raise OSError(
                "MX Component Open 失败(逻辑站号 {}):返回码 {}".format(
                    self._logical_station_number, _format_code(code)
                )
            )
        self._com = com
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """关闭 COM 通信线路(Close),幂等。

        先调用 Close 再清内部引用;Close 失败时引用照清并抛 OSError
        (下一次 connect 重建全新控件对象),同时配对
        :func:`_com_uninitialize` 释放本线程 COM 初始化计数。
        """
        com = self._com
        if com is None:
            return
        try:
            code = int(com.Close())
        except Exception:
            code = -1
        self._com = None
        _com_uninitialize()
        if code != 0:
            raise OSError("MX Component Close 失败:返回码 {}".format(_format_code(code)))
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """MX 通道无字节流收发(不调用)。"""
        raise TransportClosedError("MX Component 走 COM 通道,无字节流收发")

    def recv(self, size: int) -> bytes:
        """MX 通道无字节流收发(不调用)。"""
        raise TransportClosedError("MX Component 走 COM 通道,无字节流收发")

    @property
    def com(self) -> Any:
        """当前 COM 控件实例(仅连接成功后可用,内部属性)。"""
        if self._com is None:
            raise TransportClosedError("MX Component 通信线路未打开")
        return self._com


class MelsecMxClient(BaseClient):
    """三菱 MX Component 客户端(经 ActUtlType,按逻辑站号通信)。

    地址语法与 MC 驱动一致(``D100``/``M10``/``X1F``),软元件可用
    范围与进制由通信设置实用程序中配置的 CPU 决定,非法软元件由
    MX Component 返回出错代码并记入 :attr:`last_error`。

    :example::

        client = MelsecMxClient(logical_station_number=1)
        client.connect()
        ok, value = client.read_float("D100")
    """

    def __init__(self, logical_station_number: int = MX_DEFAULT_LOGICAL_STATION) -> None:
        """初始化 MX Component 客户端。

        :param logical_station_number: 通信设置实用程序中配置的逻辑站号(0~1023)
        :raises ValueError: 逻辑站号越界
        """
        super().__init__()
        if not 0 <= int(logical_station_number) <= MX_LOGICAL_STATION_MAX:
            raise ValueError(
                "逻辑站号必须在 0~{} 之间,收到:{}".format(
                    MX_LOGICAL_STATION_MAX, logical_station_number
                )
            )
        self._logical_station_number = int(logical_station_number)
        self._debug_label = "mx://站号{}".format(self._logical_station_number)

    @property
    def logical_station_number(self) -> int:
        """逻辑站号(与通信设置实用程序中的配置对应)。"""
        return self._logical_station_number

    # ------------------------------------------------------------------
    # COM 事务:在基类事务锁内直接调用控件方法
    # ------------------------------------------------------------------

    def _com(self) -> Any:
        """取当前 COM 控件(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _MxComLink):
            raise TransportClosedError("内部错误:传输对象不是 MX COM 会话")
        return link.com

    def _read_words(self, device_text: str, count: int) -> List[int]:
        """批量读字软元件(ReadDeviceBlock),返回 0~65535 原始字列表。"""
        if count > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                "批量读取字数超过上限 {}:{}".format(MX_MAX_BLOCK_WORDS, count)
            )
        words = _com_read_words(self._com(), device_text, count)
        log_op(self._debug_label, "ReadDeviceBlock %s×%d → %s", device_text, count, words)
        return words

    def _write_words(self, device_text: str, words: Sequence[int]) -> None:
        """批量写字软元件(WriteDeviceBlock)。"""
        if len(words) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                "批量写入字数超过上限 {}:{}".format(MX_MAX_BLOCK_WORDS, len(words))
            )
        _com_write_words(self._com(), device_text, words)
        log_op(self._debug_label, "WriteDeviceBlock %s×%d ← %s", device_text, len(words), words)

    def _get_device(self, device_text: str) -> int:
        """单点读(GetDevice),返回 0~65535 原始值。"""
        raw = _com_get_device(self._com(), device_text)
        log_op(self._debug_label, "GetDevice %s → %d", device_text, raw)
        return raw

    def _set_device(self, device_text: str, value: int) -> None:
        """单点写(SetDevice);位软元件取最低位。"""
        _com_set_device(self._com(), device_text, value)
        log_op(self._debug_label, "SetDevice %s ← %d", device_text, value)

    def _read_random(self, device_texts: List[str]) -> List[int]:
        """随机读(ReadDeviceRandom),软元件列表换行分隔,返回原始字列表。"""
        if len(device_texts) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                "随机读取点数超过上限 {}:{}".format(MX_MAX_BLOCK_WORDS, len(device_texts))
            )
        words = _com_read_random(self._com(), "\n".join(device_texts), len(device_texts))
        log_op(self._debug_label, "ReadDeviceRandom %d 点 → %s", len(device_texts), words)
        return words

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到单点/块读取原语。"""
        parsed = _check_address(address)
        if data_type is DataType.BOOL:
            return self._read_bool_impl(parsed)
        if data_type in (DataType.SHORT, DataType.USHORT):
            raw = self._get_device(_device_text(parsed))
            if data_type is DataType.SHORT:
                return convert.to_signed(raw, 16)
            return raw
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            return _decode_32(self._read_words(_device_text(parsed), 2), data_type)
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            return _decode_64(self._read_words(_device_text(parsed), 4), data_type)
        raise ValueError("MX Component 不支持的数据类型:{}".format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到单点/块写入原语。"""
        parsed = _check_address(address)
        if data_type is DataType.BOOL:
            self._write_bool_impl(parsed, require_bool(value))
            return
        if data_type is DataType.SHORT:
            self._set_device(_device_text(parsed), check_int16(value))
            return
        if data_type is DataType.USHORT:
            self._set_device(_device_text(parsed), check_uint16(value))
            return
        if data_type in (DataType.INT, DataType.UINT, DataType.FLOAT):
            self._write_words(_device_text(parsed), _encode_32(value, data_type))
            return
        if data_type in (DataType.LONG, DataType.ULONG, DataType.DOUBLE):
            self._write_words(_device_text(parsed), _encode_64(value, data_type))
            return
        raise ValueError("MX Component 不支持的数据类型:{}".format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:批量读字 → 小端拼字节 → 解码。"""
        parsed = _require_word_device(address)
        words = self._read_words(_device_text(parsed), (length + 1) // 2)
        data = b"".join(word.to_bytes(2, "little") for word in words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码 → 补齐偶数字节 → 小端拆字 → 批量写。"""
        parsed = _require_word_device(address)
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
        self._write_words(_device_text(parsed), words)
        return value

    # ------------------------------------------------------------------
    # 批量读取(ReadDeviceRandom 随机读,单事务)
    # ------------------------------------------------------------------

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 ReadDeviceRandom 随机读(单事务)。

        与基类逐点独立容错不同:任一地址非法或控件返回出错代码则
        **整批失败**(原因见 :attr:`last_error`);需要逐点容错请逐点
        调用 :meth:`read`。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = self.read_batch([(address, data_type_enum) for address in addresses])
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """随机批量读取(ReadDeviceRandom,单事务混读多个软元件)。

        ActUtlType 原生随机读(MX Component 手册 5.2.5):软元件列表以
        换行符分隔,每条读 1 点(字)。仅支持 16 位类型——BOOL(位软元件
        取最低位;字软元件读字提位)、SHORT/USHORT;**32/64 位类型不
        支持**:本驱动地址编号原文透传(进制由通信设置实用程序中的 CPU
        配置决定),无法安全把 32 位值拆分为相邻两条字读取,请逐点读取。
        条数上限与块读同口径(:data:`MX_MAX_BLOCK_WORDS`)。

        :raises ValueError: 列表为空/类型不支持/条数超限
        """
        if not items:
            raise ValueError("read_batch 至少需要一个 (地址, 数据类型) 项")
        if len(items) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                "read_batch 条目数超出上限 {}:{}".format(MX_MAX_BLOCK_WORDS, len(items))
            )
        texts: List[str] = []
        # 解码计划:(类别, 地址, 位号, 数据类型)
        plan: List[Tuple[str, str, int, DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            parsed = _check_address(address)
            if data_type_enum is DataType.BOOL:
                if _is_bit_device(parsed.device):
                    texts.append(_device_text(parsed))
                    plan.append(("bitdev", address, 0, data_type_enum))
                else:
                    texts.append(_base_text(parsed))
                    plan.append(("wordbit", address, parsed.bit or 0, data_type_enum))
                continue
            if data_type_enum in (DataType.SHORT, DataType.USHORT):
                texts.append(_device_text(parsed))
                plan.append(("word", address, 0, data_type_enum))
                continue
            raise ValueError(
                "MX 随机批量读仅支持 16 位类型(BOOL/SHORT/USHORT),"
                "{} 请逐点读取:随机读每条 1 字,地址编号原文透传无法"
                "安全拆分相邻字".format(data_type_enum)
            )

        def operation() -> List[PrimitiveValue]:
            raws = self._read_random(texts)
            values: List[PrimitiveValue] = []
            for (kind, _address, bit, data_type_enum), raw in zip(plan, raws):
                if kind == "bitdev":
                    values.append(bool(raw & 1))
                elif kind == "wordbit":
                    values.append(bool((raw >> bit) & 1))
                elif data_type_enum is DataType.SHORT:
                    values.append(convert.to_signed(raw, 16))
                else:
                    values.append(raw)
            return values

        return self._execute(operation)

    def _read_bool_impl(self, parsed: McAddress) -> bool:
        """读取一个布尔量:位软元件单点读;字软元件读字后提位。"""
        if _is_bit_device(parsed.device):
            return bool(self._get_device(_device_text(parsed)) & 1)
        raw = self._get_device(_base_text(parsed))
        return bool((raw >> (parsed.bit or 0)) & 1)

    def _write_bool_impl(self, parsed: McAddress, value: bool) -> None:
        """写入一个布尔量:位软元件单点写;字软元件读-改-写(锁内原子)。"""
        if _is_bit_device(parsed.device):
            self._set_device(_device_text(parsed), 1 if value else 0)
            return
        device_text = _base_text(parsed)
        raw = self._get_device(device_text)
        bit = parsed.bit or 0
        updated = (raw | (1 << bit)) if value else (raw & ~(1 << bit))
        self._set_device(device_text, updated & 0xFFFF)

    def _create_transport(self) -> _MxComLink:
        """创建 COM 会话适配器(每次连接新建)。"""
        return _MxComLink(self._logical_station_number)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _check_address(address: str) -> McAddress:
    """地址校验:解析 + 拒绝"位软元件带位号后缀"的非法写法。"""
    parsed = parse_mc_address(address)
    if parsed.bit is not None and _is_bit_device(parsed.device):
        raise ValueError(
            "位软元件不支持位号后缀:{!r}(示例:M10 或 D100.3)".format(address)
        )
    return parsed


def _device_text(parsed: McAddress) -> str:
    """把解析后的地址还原为控件软元件名(如 ``"D100"``/``"M10"``/``"D100.3"``)。"""
    if parsed.bit is not None:
        return "{}{}.{}".format(parsed.device, parsed.number, parsed.bit)
    return "{}{}".format(parsed.device, parsed.number)


def _base_text(parsed: McAddress) -> str:
    """字软元件本体名(不含位号后缀,供读-改-写,如 ``"D100"``)。"""
    return "{}{}".format(parsed.device, parsed.number)


def _is_bit_device(device: str) -> bool:
    """判断是否为位软元件;表外软元件按字软元件处理。"""
    return device in MX_BIT_DEVICES


def _require_word_device(address: str) -> McAddress:
    """字符串存取只允许字软元件(内部函数)。"""
    parsed = _check_address(address)
    if parsed.bit is not None or _is_bit_device(parsed.device):
        raise ValueError("字符串只能从字软元件存取,收到:{!r}".format(address))
    return parsed


def _format_code(code: int) -> str:
    """把 MX 出错代码格式化为十六进制(内部函数)。"""
    return "0x{:08X}".format(code & 0xFFFFFFFF)
