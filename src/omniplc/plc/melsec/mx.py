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

comtypes 调用口径(真机联测核证,分两条路径):``GetDevice`` 的
``Value`` 出参被 comtypes 收进返回值——单参调用直接返回数据,传 byref
缓冲会报参数个数 TypeError,失败以 ``COMError`` 形态出现(翻译为内部
异常,断线惰性重连);``SetDevice`` 为纯入参,返回码照常校验。块读写
(``ReadDeviceBlock``/``WriteDeviceBlock``/``ReadDeviceRandom``/
``WriteDeviceRandom``)的 ``Data`` 数组参数一律走 comtypes 挂载的
**原始 vtable 方法**(:func:`_raw_com_method`):高层包装按类型库旗标
裁剪实参——[out]-only 数组参数不收缓冲区(真机报参数个数 TypeError,
真机核证于 ReadDeviceRandom),省略缓冲时其自动分配的单元素缓冲又会被
服务端越界写;原始方法按 vtable 原样收全部参数——块读写为
``(设备/列表, 点数, 数据缓冲, 出错码缓冲)`` 4 参,尾参 ``lplRetCode``
([out,retval],通信函数的返回值)同样须自备 ``ctypes.c_long`` 缓冲
传入(真机 3 参调用报 "this function takes 4 arguments (3 given)");
方法返回值是 COM 层 HRESULT,业务出错码以尾缓冲内容为准。
"""
from __future__ import annotations
import ctypes
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, Union

from ... import convert
from ...core.base_client import BaseClient
from ...core.constants import (
    MX_BIT_DEVICES,
    MX_DEFAULT_LOGICAL_STATION,
    MX_LOGICAL_STATION_MAX,
    MX_MAX_BLOCK_WORDS,
    MX_PROG_ID,
    MX_SUPPORT_MSG_PROG_ID,
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

_CLOCK_FIELDS = ("year", "month", "day", "day_of_week", "hour", "minute", "second")
"""GetClockData/SetClockData 的七字段名(手册 5.2.11 顺序:年/月/日/星期/时/分/秒)。"""


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


def _com_error() -> Type[BaseException]:
    """取 comtypes.COMError 类型(延迟导入,内部函数)。"""
    import comtypes

    return comtypes.COMError


def _first(result: Any) -> Any:
    """comtypes 含出参方法可能回 (数据, 码) 元组,取首个业务值(内部函数)。"""
    return result[0] if isinstance(result, tuple) else result


def _return_code(result: Any) -> int:
    """取返回码:纯 LONG 直接用;(出参数组, 码) 元组取首个 int(内部函数)。"""
    items = result if isinstance(result, tuple) else (result,)
    for item in items:
        if isinstance(item, int):
            return item
    raise OmniPLCInternalError(f"MX Component 返回码缺失:{result!r}")


def _raw_com_method(com: Any, method: str) -> Any:
    """取 comtypes 类型库包装挂载的**原始 vtable 方法**(内部函数)。

    comtypes 为类型库接口的每个方法挂载了绕过参数旗标处理的原始函数
    (属性名形如 ``_<接口类名>__com_<方法名>``,纯 ctypes 语义,vtable
    参数原样全收)。高层包装对 [out]-only 数组参数(ActUtlType 块读写的
    ``Data``)不收缓冲区实参、省略时又只分配单元素缓冲,块操作必须经
    此原始通道传入自备缓冲。挂载名含接口类名(依类型库生成结果而定),
    故按 ``__com_<方法名>`` 后缀沿 MRO 扫描定位,大小写不敏感。

    :raises OmniPLCInternalError: COM 控件未按类型库绑定(动态派发降级,
        无原始方法挂载)
    """
    suffix = f"__com_{method}".lower()
    for klass in type(com).__mro__:
        for key in vars(klass):
            if key.lower().endswith(suffix):
                return getattr(com, key)
    raise OmniPLCInternalError(
        f"MX Component 接口缺少原始方法 {method}"
        "(COM 控件未按类型库绑定,块读写无法走原始 vtable 通道)"
    )


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
    """单点读(GetDevice),返回 0~65535 原始值。

    comtypes 生成的包装把 ``[out]`` 参数收进返回值(真机核证):
    ``GetDevice(软元件)`` 直接返回数据,传 byref 缓冲会报参数个数
    TypeError;出错以 COMError 形态出现。
    """
    try:
        result = com.GetDevice(device_text)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component GetDevice 失败:{}".format(exc)
        ) from exc
    return int(_first(result)) & 0xFFFF


def _com_set_device(com: Any, device_text: str, value: int) -> None:
    """单点写(SetDevice);位软元件取最低位。纯入参,返回码照常可得。"""
    try:
        code = com.SetDevice(device_text, int(value))
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component SetDevice 失败:{}".format(exc)
        ) from exc
    _check_rc(int(code), "SetDevice")


def _check_com_call(result: Any, retcode: int, method: str) -> None:
    """校验块读写原始调用:COM 层 HRESULT + 通信函数返回值(lplRetCode)。

    原始 vtable 方法返回 HRESULT(COM 层调用结果),业务出错码在尾参
    ``lplRetCode`` 出参缓冲中(手册"自定义 I/F"格式)——HRESULT 失败
    是 COM 层问题,通信函数返回值非 0 是 PLC 出错代码(第 7 章)。
    """
    hresult = _return_code(result)  # tuple 防御:取首个 int
    if hresult != 0:
        raise OmniPLCInternalError(
            "MX Component {} COM 调用失败:HRESULT 0x{:08X}".format(
                method, hresult & 0xFFFFFFFF
            )
        )
    _check_rc(retcode, method)


def _com_read_words(com: Any, device_text: str, count: int) -> List[int]:
    """批量读字软元件(ReadDeviceBlock),返回 0~65535 原始字列表。

    ``Data`` 数组参数经原始 vtable 方法传入自备 ctypes LONG 缓冲、
    原地填充(高层包装会剥离 [out]-only 数组实参);尾参 ``lplRetCode``
    (通信函数的返回值)同为出参,须自备 ``ctypes.c_long`` 缓冲传入,
    业务出错码以缓冲内容为准,方法返回值是 COM 层 HRESULT。
    """
    buffer = (ctypes.c_long * count)()
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "ReadDeviceBlock")
    try:
        result = raw(device_text, count, buffer, retcode)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component ReadDeviceBlock 失败:{}".format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "ReadDeviceBlock")
    return [int(word) & 0xFFFF for word in buffer]


def _com_write_words(com: Any, device_text: str, words: Sequence[int]) -> None:
    """批量写字软元件(WriteDeviceBlock);数据以 ctypes LONG 数组经原始方法传入。"""
    buffer = (ctypes.c_long * len(words))(*[int(word) for word in words])
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "WriteDeviceBlock")
    try:
        result = raw(device_text, len(words), buffer, retcode)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component WriteDeviceBlock 失败:{}".format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "WriteDeviceBlock")


def _com_read_random(com: Any, device_list: str, count: int) -> List[int]:
    """随机读(ReadDeviceRandom):软元件列表以换行符分隔,返回原始字列表。

    ``Data`` 为 [out]-only 数组指针:高层包装不收缓冲区实参(真机报
    参数个数 TypeError),走原始 vtable 方法传自备 ctypes LONG 缓冲、
    原地填充;``lplRetCode`` 出参缓冲同前,出错码照常校验。
    """
    buffer = (ctypes.c_long * count)()
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "ReadDeviceRandom")
    try:
        result = raw(device_list, count, buffer, retcode)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component ReadDeviceRandom 失败:{}".format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "ReadDeviceRandom")
    return [int(word) & 0xFFFF for word in buffer]


def _com_write_random(com: Any, device_list: str, count: int, words: Sequence[int]) -> None:
    """随机写(WriteDeviceRandom):软元件列表换行分隔,数据以 ctypes LONG 数组经原始方法传入。

    返回码照常校验(手册 5.2.6,数据低 16 位为一个字软元件值)。
    """
    buffer = (ctypes.c_long * len(words))(*[int(word) for word in words])
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "WriteDeviceRandom")
    try:
        result = raw(device_list, count, buffer, retcode)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component WriteDeviceRandom 失败:{}".format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "WriteDeviceRandom")


def _com_get_cpu_type(com: Any) -> Tuple[str, int]:
    """读 CPU 型号(GetCpuType),返回 (型号字符串, 型号代码)。

    双出参方法,comtypes 口径真机待核证(方法级差异已被 GetDevice/块读
    证明):先按"出参收进返回值"零参调用(同 GetDevice);包装保留出参
    时报参数个数 TypeError,退回 byref VARIANT 形态(手册 5.2.13:
    szCpuName、lCpuType 均 Output)。
    """
    from comtypes.automation import VARIANT

    try:
        result = com.GetCpuType()
    except TypeError:
        name = VARIANT()
        code = VARIANT()
        try:
            raw = com.GetCpuType(ctypes.byref(name), ctypes.byref(code))
        except _com_error() as exc:
            raise OmniPLCInternalError(
                "MX Component GetCpuType 失败:{}".format(exc)
            ) from exc
        _check_rc(_return_code(raw), "GetCpuType")
        return str(name.value), int(code.value)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component GetCpuType 失败:{}".format(exc)
        ) from exc
    values = result if isinstance(result, tuple) else (result,)
    texts = [value for value in values if isinstance(value, str)]
    codes = [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if not texts:
        raise OmniPLCInternalError(
            f"MX Component GetCpuType 返回形态未识别:{result!r}"
        )
    return texts[0], codes[0] if codes else 0


def _com_get_clock_data(com: Any) -> Dict[str, int]:
    """读 CPU 时钟(GetClockData),返回七字段字典。

    手册 5.2.11:七字段全出参,顺序为年/月/日/星期/时/分/秒。
    comtypes 口径真机待核证:零参调用(出参收进返回值)优先,
    TypeError 时退回 byref VARIANT×7 形态。
    """
    from comtypes.automation import VARIANT

    try:
        result = com.GetClockData()
    except TypeError:
        variants = [VARIANT() for _ in _CLOCK_FIELDS]
        try:
            raw = com.GetClockData(*[ctypes.byref(variant) for variant in variants])
        except _com_error() as exc:
            raise OmniPLCInternalError(
                "MX Component GetClockData 失败:{}".format(exc)
            ) from exc
        _check_rc(_return_code(raw), "GetClockData")
        values = [variant.value for variant in variants]
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component GetClockData 失败:{}".format(exc)
        ) from exc
    else:
        listed = list(result if isinstance(result, tuple) else (result,))
        if len(listed) < len(_CLOCK_FIELDS):
            raise OmniPLCInternalError(
                f"MX Component GetClockData 返回形态未识别:{result!r}"
            )
        values = listed[: len(_CLOCK_FIELDS)]
    return {
        name: int(value)
        for name, value in zip(_CLOCK_FIELDS, values)
    }


def _com_set_clock_data(
    com: Any,
    year: int,
    month: int,
    day: int,
    day_of_week: int,
    hour: int,
    minute: int,
    second: int,
) -> None:
    """写 CPU 时钟(SetClockData);七字段全入参(顺序:年/月/日/星期/时/分/秒)。"""
    fields = [year, month, day, day_of_week, hour, minute, second]
    try:
        result = com.SetClockData(*[int(value) for value in fields])
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component SetClockData 失败:{}".format(exc)
        ) from exc
    _check_rc(_return_code(result), "SetClockData")


def _new_support_msg_com(logical_station_number: int) -> Any:
    """创建 ActSupportMsg 控件(GetErrorMessage 专用,与 ActUtlType 独立)。"""
    import comtypes.client

    com = comtypes.client.CreateObject(MX_SUPPORT_MSG_PROG_ID)
    try:
        com.ActLogicalStationNumber = logical_station_number
    except Exception:
        pass  # SupportMsg 的站号属性依版本而异,设置失败不阻断文本查询
    return com


def _com_get_error_message(com: Any, code: int) -> str:
    """出错代码转官方文本(GetErrorMessage,经 ActSupportMsg 控件)。

    (lErrorCode 入参、szErrorMessage 出参):先按"出参收进返回值"
    单参调用,TypeError 退回 byref VARIANT 形态。
    """
    from comtypes.automation import VARIANT

    try:
        result = com.GetErrorMessage(int(code))
    except TypeError:
        message = VARIANT()
        try:
            raw = com.GetErrorMessage(int(code), ctypes.byref(message))
        except _com_error() as exc:
            raise OmniPLCInternalError(
                "MX Component GetErrorMessage 失败:{}".format(exc)
            ) from exc
        _check_rc(_return_code(raw), "GetErrorMessage")
        return str(message.value)
    except _com_error() as exc:
        raise OmniPLCInternalError(
            "MX Component GetErrorMessage 失败:{}".format(exc)
        ) from exc
    return str(_first(result))


class _MxComLink(BaseTransport):
    """COM 会话适配器:把 ActUtlType 封装成传输对象外形。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 打开
    COM 通信线路,``close`` 关闭;超时属性由基类存储(MX Component
    的超时在通信设置实用程序中配置,控件不单独暴露)。无字节流收发。
    """

    def __init__(self, logical_station_number: int) -> None:
        """MX Component 通信线路适配器。

        :param logical_station_number: 通信设置实用程序中配置的逻辑站号
        """
        super().__init__()
        self._logical_station_number = logical_station_number
        self._com: Any = None
        self._debug_label = f"mx://站号{logical_station_number}"

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
        self._debug_label = f"mx://站号{self._logical_station_number}"

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
                f"批量读取字数超过上限 {MX_MAX_BLOCK_WORDS}:{count}"
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

    def _write_random(self, device_texts: List[str], words: List[int]) -> None:
        """随机写(WriteDeviceRandom),软元件列表换行分隔(内部方法)。"""
        _com_write_random(self._com(), "\n".join(device_texts), len(device_texts), words)
        log_op(
            self._debug_label, "WriteDeviceRandom %d 点 ← %s", len(device_texts), words
        )

    def _get_cpu_type(self) -> Tuple[str, int]:
        """读 CPU 型号(GetCpuType),返回 (型号字符串, 型号代码)(内部方法)。"""
        name, code = _com_get_cpu_type(self._com())
        log_op(self._debug_label, "GetCpuType → %s (0x%04X)", name, code)
        return name, code

    def _get_clock_data(self) -> Dict[str, int]:
        """读 CPU 时钟(GetClockData),返回七字段字典(内部方法)。"""
        clock = _com_get_clock_data(self._com())
        log_op(self._debug_label, "GetClockData → %s", clock)
        return clock

    def _set_clock_data(
        self,
        year: int,
        month: int,
        day: int,
        day_of_week: int,
        hour: int,
        minute: int,
        second: int,
    ) -> None:
        """写 CPU 时钟(SetClockData)(内部方法)。"""
        _com_set_clock_data(
            self._com(), year, month, day, day_of_week, hour, minute, second
        )
        log_op(
            self._debug_label,
            "SetClockData ← %d-%d-%d 星期%d %d:%d:%d",
            year, month, day, day_of_week, hour, minute, second,
        )

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
        raise ValueError(f"MX Component 不支持的数据类型:{data_type}")

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
        raise ValueError(f"MX Component 不支持的数据类型:{data_type}")

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

    # ------------------------------------------------------------------
    # 批量写入(WriteDeviceRandom 随机写,单事务)
    # ------------------------------------------------------------------

    def write_batch(self, items: Sequence[Tuple[str, PrimitiveValue]]) -> bool:
        """随机批量写入(WriteDeviceRandom,单事务写多个软元件)。

        与 :meth:`read_batch` 对称:软元件列表换行分隔,单事务一次写入。
        仅支持 16 位量——**位软元件**(bool 写 1/0)与**整数值**(int,
        收窄到 -32768~65535 后按字写入,负数按补码);字软元件位号
        (如 ``D100.3``)与 32/64 位类型不支持(随机写每条 1 字,且
        位号写入需读-改-写语义),请逐点 :meth:`write_bool`/:meth:`write_int`。

        :param items: ``(地址, 值)`` 序列;bool → 位软元件,int → 字软元件
        :return: 是否成功;任一地址非法、值越界或控件返回出错代码则整批失败
            (原因见 :attr:`last_error`)
        :raises ValueError: 列表为空/值类型不支持/条数超限
        """
        if not items:
            raise ValueError("write_batch 至少需要一个 (地址, 值) 项")
        if len(items) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                "write_batch 条目数超出上限 {}:{}".format(MX_MAX_BLOCK_WORDS, len(items))
            )
        texts: List[str] = []
        words: List[int] = []
        for address, value in items:
            parsed = _check_address(address)
            if parsed.bit is not None:
                raise ValueError(
                    f"write_batch 不支持字软元件位号 {address!r}"
                    "(位写入需读-改-写语义,请逐点 write_bool)"
                )
            if isinstance(value, bool):
                if not _is_bit_device(parsed.device):
                    raise ValueError(
                        f"write_batch 的 bool 值仅支持位软元件:{address!r} ← {value!r}"
                    )
                texts.append(_device_text(parsed))
                words.append(1 if value else 0)
                continue
            if isinstance(value, int):
                if not -32768 <= value <= 65535:
                    raise ValueError(
                        f"write_batch 整数值超出 16 位范围(-32768~65535):{value!r}"
                    )
                texts.append(_device_text(parsed))
                words.append(value & 0xFFFF)
                continue
            raise ValueError(
                "write_batch 仅支持 16 位量(bool/int),{} 收到:{!r}"
                "(32/64 位请逐点写入)".format(type(value).__name__, value)
            )

        def operation() -> None:
            self._write_random(texts, words)

        ok, _ = self._execute(operation, is_write=True)
        return ok

    # ------------------------------------------------------------------
    # CPU 型号 / 时钟 / 出错文本
    # ------------------------------------------------------------------

    def get_cpu_type(self) -> Tuple[bool, Optional[Tuple[str, int]]]:
        """读取 PLC CPU 型号字符串与型号代码(GetCpuType)。

        :return: ``(是否成功, (型号字符串, 型号代码))``,如
            ``("Q06HCPU", 333)``(代码依 CPU 系列而定,手册附录);
            失败为 ``(False, None)``
        """

        def operation() -> Tuple[str, int]:
            return self._get_cpu_type()

        ok, value = self._execute(operation)
        return ok, (value if ok else None)

    def get_clock(self) -> Tuple[bool, Optional[Dict[str, int]]]:
        """读取 PLC CPU 时钟(GetClockData)。

        :return: ``(是否成功, {year, month, day, day_of_week, hour,
            minute, second})``;``day_of_week`` 为 0~6(0=星期日,依
            PLC 侧定义);失败为 ``(False, None)``
        """
        ok, value = self._execute(self._get_clock_data)
        return ok, (value if ok else None)

    def set_clock(
        self,
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        day_of_week: int = 0,
    ) -> bool:
        """写入 PLC CPU 时钟(SetClockData)——**会改变 PLC 系统时钟**。

        :param year: 年(按 PLC 侧口径,Q/R 系列通常 4 位,如 2026)
        :param month: 月 1~12
        :param day: 日 1~31
        :param hour: 时 0~23
        :param minute: 分 0~59
        :param second: 秒 0~59
        :param day_of_week: 星期 0~6(0=星期日,依 PLC 侧定义),默认 0
        :return: 是否成功(PLC 侧时钟校验失败会返回出错代码)
        """

        def operation() -> None:
            self._set_clock_data(year, month, day, day_of_week, hour, minute, second)

        ok, _ = self._execute(operation, is_write=True)
        return ok

    def get_error_message(self, code: int) -> Tuple[bool, Optional[str]]:
        """把 MX 出错代码转为官方文本(GetErrorMessage)。

        经独立的 **ActSupportMsg** 控件查询(手册 5.2.26:该功能不在
        ActUtlType 上),控件按需创建,ProgID 见
        :data:`omniplc.core.constants.MX_SUPPORT_MSG_PROG_ID`(真机待核证)。

        :param code: 出错代码(手册第 7 章,如 ``0xC0500100``)
        :return: ``(是否成功, 出错文本)``(文本含出错内容及处理方法);
            失败为 ``(False, None)``
        """

        def operation() -> str:
            return _com_get_error_message(
                _new_support_msg_com(self._logical_station_number), int(code)
            )

        ok, value = self._execute(operation)
        return ok, (value if ok else None)

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
            f"位软元件不支持位号后缀:{address!r}(示例:M10 或 D100.3)"
        )
    return parsed


def _device_text(parsed: McAddress) -> str:
    """把解析后的地址还原为控件软元件名(如 ``"D100"``/``"M10"``/``"D100.3"``)。"""
    if parsed.bit is not None:
        return f"{parsed.device}{parsed.number}.{parsed.bit}"
    return f"{parsed.device}{parsed.number}"


def _base_text(parsed: McAddress) -> str:
    """字软元件本体名(不含位号后缀,供读-改-写,如 ``"D100"``)。"""
    return f"{parsed.device}{parsed.number}"


def _is_bit_device(device: str) -> bool:
    """判断是否为位软元件;表外软元件按字软元件处理。"""
    return device in MX_BIT_DEVICES


def _require_word_device(address: str) -> McAddress:
    """字符串存取只允许字软元件(内部函数)。"""
    parsed = _check_address(address)
    if parsed.bit is not None or _is_bit_device(parsed.device):
        raise ValueError(f"字符串只能从字软元件存取,收到:{address!r}")
    return parsed


def _format_code(code: int) -> str:
    """把 MX 出错代码格式化为十六进制(内部函数)。"""
    return "0x{:08X}".format(code & 0xFFFFFFFF)
