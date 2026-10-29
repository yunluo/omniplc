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
from contextlib import suppress
from functools import lru_cache
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from ...core import convert
from ...core.base_client import BaseClient
from ...core.constants import (
    INT16_MIN,
    MX_BIT_DEVICES,
    MX_DEFAULT_LOGICAL_STATION,
    MX_LOGICAL_STATION_MAX,
    MX_MAX_BLOCK_WORDS,
    MX_PROG_ID,
    MX_SUPPORT_MSG_PROG_ID,
    UINT16_MAX,
)
from ...core.debug import log_op
from ...core.errors import DeviceError, OmniPLCInternalError, TransportClosedError
from ...core.i18n import _
from ...core.monitor import MonitorEvent
from ...core.tag import TagTable
from ...core.types import DataType, PrimitiveValue
from ...core.validation import check_int16, check_uint16, require_bool, require_count
from ...transport import BaseTransport
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
    with suppress(Exception):
        import comtypes

        comtypes.CoUninitialize()


def _new_com_object(logical_station_number: int) -> Any:
    """创建 ActUtlType COM 控件并设置逻辑站号。"""
    import comtypes.client

    com = comtypes.client.CreateObject(MX_PROG_ID)
    com.ActLogicalStationNumber = logical_station_number
    return com


def _com_error() -> Type[BaseException]:
    """取 comtypes.COMError 类型(延迟导入,内部函数)。"""
    import comtypes

    return cast(Type[BaseException], comtypes.COMError)


def _return_code(result: Any) -> int:
    """取返回码:纯 LONG 直接用;(出参数组, 码) 元组取首个 int(内部函数)。"""
    items = result if isinstance(result, tuple) else (result,)
    for item in items:
        if isinstance(item, int):
            return item
    raise OmniPLCInternalError(_("MX Component 返回码缺失:{!r}").format(result))


# COM 类型数量有限(ActUtlType/ActProgType × 类型库版本),上限 32 足够
@lru_cache(maxsize=32)
def _locate_raw_com_attr(com_type: Any, method: str) -> str:
    """定位原始 vtable 方法的**属性名**(内部函数,结果按类型记忆化)。

    挂载名含接口类名(依类型库生成结果而定),按 ``__com_<方法名>``
    后缀沿 MRO 扫描定位,大小写不敏感。同一 COM 类型扫描结果恒定,
    :func:`functools.lru_cache` 记忆化免除每笔块操作的重复全扫描
    (review-1019 P1-3:120 条 float 批曾 = 120 次扫描);类型作为缓存
    键天然兼容不同类型库版本。``com_type`` 收 ``Any``:mypy 的
    ``type[Any]`` 与 ``Hashable`` 存在已知不相容误报,运行时类对象恒可哈希。

    :raises OmniPLCInternalError: COM 控件未按类型库绑定(动态派发降级,
        无原始方法挂载;异常不入缓存,每次照常定位)
    """
    suffix = f"__com_{method}".lower()
    for klass in com_type.__mro__:
        for key in vars(klass):
            if key.lower().endswith(suffix):
                return str(key)
    raise OmniPLCInternalError(
        _(
            "MX Component 接口缺少原始方法 {}(COM 控件未按类型库绑定,块读写无法走原始 vtable 通道)"
        ).format(method)
    )


def _raw_com_method(com: Any, method: str) -> Any:
    """取 comtypes 类型库包装挂载的**原始 vtable 方法**(内部函数)。

    comtypes 为类型库接口的每个方法挂载了绕过参数旗标处理的原始函数
    (属性名形如 ``_<接口类名>__com_<方法名>``,纯 ctypes 语义,vtable
    参数原样全收)。高层包装对 [out]-only 数组参数(ActUtlType 块读写的
    ``Data``)不收缓冲区实参、省略时又只分配单元素缓冲,块操作必须经
    此原始通道传入自备缓冲。

    只记忆化**属性名**、每次 :func:`getattr` 重新绑定实例:断线重连会
    ``CreateObject`` 新 COM 对象,缓存绑定方法会拿到旧实例已释放的
    接口指针(缓存名不缓存方法,review-1019 P1-3)。
    """
    # cast:mypy 对 type[Any] 与 lru_cache 形参 Hashable 的已知误报,
    # 类对象运行时恒可哈希
    return getattr(com, _locate_raw_com_attr(cast(Any, type(com)), method))


def _check_rc(code: int, method: str) -> None:
    """校验控件方法返回码(0 = 正常,内部函数)。

    :raises OmniPLCInternalError: 返回非 0 出错代码(标记断开,
        下一次操作惰性重连)
    """
    if code != 0:
        raise OmniPLCInternalError(
            _("MX Component {} 失败:返回码 {}").format(method, _format_code(code))
        )


def _com_get_device(com: Any, device_text: str) -> int:
    """单点读(GetDevice),返回 0~65535 原始值。

    走 :func:`_raw_com_method` 原始 vtable 通道:参数布局
    ``(szDevice, lplData, lplRetCode)``(本机 ActUtlType.dll dump
    与手册 §5.2.4「GetDevice」自定义 I/F 吻合;子代理用假 vtable
    探针证原始通道可绑可用)。``lplData`` 与 ``lplRetCode`` 同为
    出参缓冲,自备 ctypes LONG 经原始 vtable 传入,业务出错码
    (如 0x010A42A0 访问口令不符)写入 ``lplRetCode`` 而**不抛**
    COMError——这是第八轮 P1-2 历史盲区,raw 通道凭
    ``lplRetCode`` 显式校验可感知。

    review-1024 §9.2-1 P1:参数布局已由类型库静态确定, raw 通道
    现在可实现;真机行为"业务出错时 HRESULT 是否始终维持 S_OK"
    仍是真机核证项(review-1024 §9.5)。
    """
    buffer = ctypes.c_long()
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "GetDevice")
    try:
        result = raw(device_text, ctypes.byref(buffer), ctypes.byref(retcode))
    except _com_error() as exc:
        raise OmniPLCInternalError(
            _("MX Component GetDevice 失败:{}").format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "GetDevice")
    return int(buffer.value) & 0xFFFF


def _com_set_device(com: Any, device_text: str, value: int) -> None:
    """单点写(SetDevice);位软元件取最低位。纯入参,返回码照常可得。"""
    try:
        code = com.SetDevice(device_text, int(value))
    except _com_error() as exc:
        raise OmniPLCInternalError(
            _("MX Component SetDevice 失败:{}").format(exc)
        ) from exc
    _check_rc(int(code), "SetDevice")


def _check_com_call(result: Any, retcode: int, method: str) -> None:
    """校验块读写原始调用:COM 层 HRESULT + 通信函数返回值(lplRetCode)。

    原始 vtable 方法返回 HRESULT(COM 层结果),业务出错码在尾参
    ``lplRetCode`` 出参缓冲中(手册"自定义 I/F"格式)——HRESULT 失败
    是 COM 层问题,通信函数返回值非 0 是 PLC 出错代码(第 7 章)。

    review-1024 §9.2-2 P2:旧用 ``hresult != 0`` 把 ``S_FALSE``(0x00000001)
    判为 COM 失败——手册 §7.3(印刷 529)原文:「S_FALSE 正常结束——
    函数处理正常结束,但操作(至 PLC 的访问)失败」,真实码经 ``lplRetCode``
    传。修法:HRESULT 失败位仅在 32 位有符号下判断,Python int 无 32 位
    上限,先 ``& 0xFFFFFFFF`` 转无符号再按有符号比较——
    ``(hresult & 0xFFFFFFFF) >= 0x80000000`` 即 FAILED(E_FAIL/E_NOTIMPL/
    自定义错误段),S_OK=0 与 S_FALSE=1 视为 COM 层成功。
    """
    hresult = _return_code(result) & 0xFFFFFFFF  # 32 位无符号 HRESULT
    if hresult >= 0x80000000:
        raise OmniPLCInternalError(
            _("MX Component {} COM 调用失败:HRESULT 0x{:08X}").format(method, hresult)
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
            _("MX Component ReadDeviceBlock 失败:{}").format(exc)
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
            _("MX Component WriteDeviceBlock 失败:{}").format(exc)
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
            _("MX Component ReadDeviceRandom 失败:{}").format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "ReadDeviceRandom")
    return [int(word) & 0xFFFF for word in buffer]


def _com_write_random(
    com: Any, device_list: str, count: int, words: Sequence[int]
) -> None:
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
            _("MX Component WriteDeviceRandom 失败:{}").format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "WriteDeviceRandom")


def _com_get_cpu_type(com: Any) -> Tuple[str, int]:
    """读 CPU 型号(GetCpuType),返回 (型号字符串, 型号代码)。

    走 :func:`_raw_com_method` 原始 vtable 通道:参数布局
    ``(szCpuName, lCpuType, lplRetCode)``(本机类型库 dump 与手册
    §5.2.13 自定义 I/F 吻合)。真类型库(comtypes/gen ActUtlType dump)
    声明 ``szCpuName = POINTER(BSTR)``、``lCpuType/lplRetCode =
    POINTER(c_int)``——**BSTR 出参必须传 :class:`comtypes.BSTR` 缓冲**:
    ctypes 指针形参严格查型,``byref(create_unicode_buffer(N))`` 是
    ``POINTER(c_wchar_Array_N)``,必抛 ``ArgumentError: expected LP_BSTR
    instance instead of pointer to c_wchar_Array_N``(2026-10-28 现场
    GetCpuType 必败的根因,报错里的 "pointer … Array" 即此;整数缓冲
    不受此限,``byref(c_long)`` 对 ``POINTER(c_int)`` 同宽放行,故其余
    方法无恙)。BSTR 由服务端 ``SysAllocString``,读后 ``SysFreeString``
    释放(NULL 释放为安全空操作)。

    review-1024 §9.2-1 P1:与 _com_get_device 同批改 raw 通道,
    凭 ``lplRetCode`` 显式校验可感知高位 0 出错码;当时仅假 vtable
    测试(收什么都能写),查型缺陷由现场首用暴露(§9.5 真机核证项)。
    """
    from comtypes import BSTR

    name_buffer = BSTR()
    code = ctypes.c_long()
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "GetCpuType")
    try:
        result = raw(
            ctypes.byref(name_buffer), ctypes.byref(code), ctypes.byref(retcode)
        )
    except _com_error() as exc:
        raise OmniPLCInternalError(
            _("MX Component GetCpuType 失败:{}").format(exc)
        ) from exc
    try:
        _check_com_call(result, retcode.value, "GetCpuType")
        return str(name_buffer.value or ""), int(code.value) & 0xFFFF
    finally:
        ctypes.oledll.oleaut32.SysFreeString(name_buffer)


def _com_get_clock_data(com: Any) -> Dict[str, int]:
    """读 CPU 时钟(GetClockData),返回七字段字典。

    走 :func:`_raw_com_method` 原始 vtable 通道:参数布局
    ``(iYear, iMonth, iDay, iDayOfWeek, iHour, iMinute, iSecond,
    lplRetCode)``(本机类型库 dump 与手册 §5.2.11 吻合)。七个
    字段全为出参,自备 7 个 ctypes.c_short 缓冲(手册仅描述
    范围 0~9999/1~12/1~31/0~6/0~23/0~59/0~59,实际类型按类型
    库 dump = SHORT)。

    review-1024 §9.2-1 P1:与 GetDevice/GetCpuType 同批改 raw 通道。
    真机核证项:年份 2/4 位口径(手册 §5.2.11(6) 印刷 352:R/Q
    (Q 模式) 返回 4 位, 其余低 2 位; 本库若仅披露 0~6 day_of_week
    则暂未校验年份位宽, 留 review-1024 §9.5 真机批核证)。
    """
    # 7 个出参缓冲:ctypes 数组 ``(c_short * 7)()`` 索引返回 int 而非
    # ctypes 实例,byref 失败;用 7 个独立 c_short 实例,byref 每个指针
    year, month, day, day_of_week, hour, minute, second = (
        ctypes.c_short() for _ in range(7)
    )
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "GetClockData")
    try:
        result = raw(
            ctypes.byref(year),
            ctypes.byref(month),
            ctypes.byref(day),
            ctypes.byref(day_of_week),
            ctypes.byref(hour),
            ctypes.byref(minute),
            ctypes.byref(second),
            ctypes.byref(retcode),
        )
    except _com_error() as exc:
        raise OmniPLCInternalError(
            _("MX Component GetClockData 失败:{}").format(exc)
        ) from exc
    _check_com_call(result, retcode.value, "GetClockData")
    values = [
        int(field.value)
        for field in (year, month, day, day_of_week, hour, minute, second)
    ]
    return dict(zip(_CLOCK_FIELDS, values))


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
            _("MX Component SetClockData 失败:{}").format(exc)
        ) from exc
    _check_rc(_return_code(result), "SetClockData")


def _new_support_msg_com(logical_station_number: int) -> Any:
    """创建 ActSupportMsg 控件(GetErrorMessage 专用,与 ActUtlType 独立)。

    ProgID 依手册 §1.2.1(`ActSupportMsg.dll` → `ActSupportMsg.ActSupportMsg`)。
    创建失败(未安装 MX Component 运行时 / 控件未注册)翻译为
    :class:`OmniPLCInternalError`,由公共 API 收口为 ``(False, None)``。
    """
    import comtypes.client

    try:
        com = comtypes.client.CreateObject(MX_SUPPORT_MSG_PROG_ID)
    except Exception as exc:
        # DeviceError(code=0):SupportMsg 是**独立控件**,其缺失不代表
        # ActUtlType 会话损坏——按内部异常抛会被基类拆线,上层轮询预翻译
        # 时反复重连抖动
        raise DeviceError(
            _(
                "MX Component ActSupportMsg 控件创建失败:{}(请确认已安装 MX Component "
                "运行时并执行 pip install omniplc[mx])"
            ).format(exc),
            0,
        ) from exc
    with suppress(Exception):
        com.ActLogicalStationNumber = logical_station_number
    return com


def _com_get_error_message(com: Any, code: int) -> str:
    """出错代码转官方文本(GetErrorMessage,经 ActSupportMsg 控件)。

    走 :func:`_raw_com_method` 原始 vtable 通道:参数布局
    ``(lErrorCode, szErrorMessage, lplRetCode)``(本机类型库 dump
    与手册 §5.2.26 印刷 398 吻合;review-1024 §9.2-1 P1)。
    ``szErrorMessage`` 是 BSTR 出参,必须传 :class:`comtypes.BSTR`
    缓冲——wchar 数组指针过不了 ctypes 指针查型(同
    :func:`_com_get_cpu_type`,2026-10-28 现场「point array」报错同根因)。SupportMsg
    控件独立于 ActUtlType(手册:「碼→文本只要求 ActSupportMsg,
    不要求 Open」),与 _com_get_device 解耦——本函数不需要 ActUtlType
    已 Open(review-1024 §9.2-5 P2 揭露的「get_error_message 不经
    _execute」已隐式满足,因 4C 路径外、链路不感知)。

    返回码非 0 = 文本查询失败(设备侧条件,链路完好),
    转 :class:`DeviceError`(code=0) 不拆线——避免上层轮询预翻译
    时反复重连抖动。

    退出时(**含异常路径**)置空本地 ``com`` 引用:STA 控件代理随引用
    计数立即释放,不留待 traceback 帧。
    """
    from comtypes import BSTR

    message = BSTR()
    retcode = ctypes.c_long()
    raw = _raw_com_method(com, "GetErrorMessage")
    try:
        # szErrorMessage 同为 BSTR 出参:必须传 BSTR 缓冲(同
        # _com_get_cpu_type,wchar 数组指针过不了 ctypes 查型)
        result = raw(int(code), ctypes.byref(message), ctypes.byref(retcode))
    except _com_error() as exc:
        raise DeviceError(
            _("MX Component GetErrorMessage 失败:{}").format(exc), 0
        ) from exc
    try:
        _check_com_call(result, retcode.value, "GetErrorMessage")
        text = str(message.value or "")
    except OmniPLCInternalError as exc:
        # SupportMsg 返回码非 0 同为文本查询失败(不断线),转码口径同上
        raise DeviceError(str(exc), 0) from exc
    finally:
        # BSTR 由服务端 SysAllocString,读后释放(NULL 释放为安全空操作);
        # raw = getattr(com, ...) 是 bound method, 持 self 引用——
        # 不置空, 控件代理随 GC 延迟释放可能扣到 traceback 帧。
        ctypes.oledll.oleaut32.SysFreeString(message)
        raw = None
        com = None
    return text


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
        except Exception as exc:
            raise OSError(
                _(
                    "MX Component COM 初始化失败:{}(请确认已安装 MX Component 运行时,"
                    "并执行 pip install omniplc[mx])"
                ).format(exc)
            ) from exc
        try:
            com = _new_com_object(self._logical_station_number)
        except Exception as exc:
            # 初始化成功但对象创建失败:必须配对释放,否则惰性重连循环
            # 每次在线程上累积一个 CoInitialize 引用计数
            _com_uninitialize()
            raise OSError(
                _(
                    "MX Component 控件创建失败:{}(请确认已安装 MX Component 运行时,"
                    "并执行 pip install omniplc[mx])"
                ).format(exc)
            ) from exc
        try:
            code = int(com.Open())
        except _com_error() as exc:
            # Open 抛 COMError 也必须配对释放(第八轮 P2-16):否则
            # self._com 保持 None 使 close 短路,惰性重连每轮累积一个
            # CoInitialize 引用计数
            _com_uninitialize()
            raise OSError(
                _("MX Component Open 失败(逻辑站号 {}):{}").format(
                    self._logical_station_number, exc
                )
            ) from exc
        if code != 0:
            _com_uninitialize()
            raise OSError(
                _("MX Component Open 失败(逻辑站号 {}):返回码 {}").format(
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
            raise OSError(
                _("MX Component Close 失败:返回码 {}").format(_format_code(code))
            )
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """MX 通道无字节流收发(不调用)。"""
        raise TransportClosedError(_("MX Component 走 COM 通道,无字节流收发"))

    def recv(self, size: int) -> bytes:
        """MX 通道无字节流收发(不调用)。"""
        raise TransportClosedError(_("MX Component 走 COM 通道,无字节流收发"))

    @property
    def com(self) -> Any:
        """当前 COM 控件实例(仅连接成功后可用,内部属性)。"""
        if self._com is None:
            raise TransportClosedError(_("MX Component 通信线路未打开"))
        return self._com


class MelsecMxClient(BaseClient):
    """三菱 MX Component 客户端(经 ActUtlType,按逻辑站号通信)。

    地址语法与 MC 驱动一致(``D100``/``M10``/``X1F``),软元件可用
    范围与进制由通信设置实用程序中配置的 CPU 决定,非法软元件由
    MX Component 返回出错代码并记入 :attr:`last_error`。

    **超时口径**(第八轮 P2-5):COM 通道无独立超时接线——
    ``receive_timeout`` / ``connect_timeout`` 不作用于 COM 调用
    (块/随机读写的等待与超时由 MX Component 运行时按逻辑站号配置管理)。

    :example::

        client = MelsecMxClient(logical_station_number=1)
        client.connect()
        ok, value = client.read_float("D100")
    """

    def __init__(
        self, logical_station_number: int = MX_DEFAULT_LOGICAL_STATION
    ) -> None:
        """初始化 MX Component 客户端。

        :param logical_station_number: 通信设置实用程序中配置的逻辑站号(0~1023)
        :raises ValueError: 逻辑站号越界
        """
        super().__init__()
        if not 0 <= int(logical_station_number) <= MX_LOGICAL_STATION_MAX:
            raise ValueError(
                _("逻辑站号必须在 0~{} 之间,收到:{}").format(
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
            raise TransportClosedError(_("内部错误:传输对象不是 MX COM 会话"))
        return link.com

    def _read_words(self, device_text: str, count: int) -> List[int]:
        """批量读字软元件(ReadDeviceBlock),返回 0~65535 原始字列表。"""
        if count > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                _("批量读取字数超过上限 {}:{}").format(MX_MAX_BLOCK_WORDS, count)
            )
        words = _com_read_words(self._com(), device_text, count)
        log_op(
            self._debug_label, "ReadDeviceBlock %s×%d → %s", device_text, count, words
        )
        return words

    def _write_words(self, device_text: str, words: Sequence[int]) -> None:
        """批量写字软元件(WriteDeviceBlock)。"""
        if len(words) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                _("批量写入字数超过上限 {}:{}").format(MX_MAX_BLOCK_WORDS, len(words))
            )
        _com_write_words(self._com(), device_text, words)
        log_op(
            self._debug_label,
            "WriteDeviceBlock %s×%d ← %s",
            device_text,
            len(words),
            words,
        )

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
                _("随机读取点数超过上限 {}:{}").format(
                    MX_MAX_BLOCK_WORDS, len(device_texts)
                )
            )
        words = _com_read_random(
            self._com(), "\n".join(device_texts), len(device_texts)
        )
        log_op(
            self._debug_label, "ReadDeviceRandom %d 点 → %s", len(device_texts), words
        )
        return words

    def _write_random(self, device_texts: List[str], words: List[int]) -> None:
        """随机写(WriteDeviceRandom),软元件列表换行分隔(内部方法)。"""
        _com_write_random(
            self._com(), "\n".join(device_texts), len(device_texts), words
        )
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
            year,
            month,
            day,
            day_of_week,
            hour,
            minute,
            second,
        )

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """按数据类型分发到单点/块读取原语。"""
        parsed = _check_address(address)
        _reject_non_bool_access(parsed, data_type, address)
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
        raise ValueError(_("MX Component 不支持的数据类型:{}").format(data_type))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型分发到单点/块写入原语。"""
        parsed = _check_address(address)
        _reject_non_bool_access(parsed, data_type, address)
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
        raise ValueError(_("MX Component 不支持的数据类型:{}").format(data_type))

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串:批量读字 → 小端拼字节 → 解码。"""
        parsed = _require_word_device(address)
        words = self._read_words(_device_text(parsed), (length + 1) // 2)
        data = convert.words_to_bytes(words)[:length]
        return convert.decode_string(data, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串:编码 → 补齐偶数字节 → 小端拆字 → 批量写。"""
        parsed = _require_word_device(address)
        raw = convert.encode_string(
            value, (len(value.encode(encoding)) + 1) // 2 * 2, encoding
        )
        words = convert.bytes_to_words(raw)
        self._write_words(_device_text(parsed), words)
        return value

    # ------------------------------------------------------------------
    # 批量读取(ReadDeviceRandom 随机读,单事务)
    # ------------------------------------------------------------------

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:同软元件起连续 ``count`` 个元素,ReadDeviceBlock 单事务。

        字软元件块读(MX Component 手册 5.2.5 ReadDeviceBlock),16 位类型
        1 字/元素、32 位 2 字、64 位 4 字;单事务上限
        :data:`MX_MAX_BLOCK_WORDS`(960 字)。BOOL 连续读不支持
        (MX 位块读按 16 点/字返回、与单点读语义不一致)。

        :param address: 起始软元件地址(如 ``"D100"``;数值类型须字软元件)
        :param count: 元素个数(按 ``data_type`` 计,INT×10 = 20 字)
        :param data_type: 数据类型(数值类型)
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 字数超限 /
            位软元件(MX 位块读 16 点/字语义与单点读不一致,统一拒绝)
        """
        require_count(count)
        data_type_enum = DataType.coerce(data_type)
        if data_type_enum is DataType.STRING:
            raise ValueError(_("read_range 不支持 STRING,请用 read_string"))
        parsed = _check_address(address)
        _reject_non_bool_access(parsed, data_type_enum, address)
        if data_type_enum is DataType.BOOL:
            # MX ReadDeviceBlock 位软元件按 16 点/字返回、GetDevice 单点 0/1,
            # 两种语义不一致;连续位读无一致实现口径,统一拒绝(同 MC 字门控精神)
            raise ValueError(
                _(
                    "MX read_range 不支持 BOOL 连续读(位块读语义不一致),请逐点读:{!r}"
                ).format(address)
            )
        width = data_type_enum.register_size
        if count * width > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                _("MX read_range 字数超上限 {}:{}×{}={}").format(
                    MX_MAX_BLOCK_WORDS, count, width, count * width
                )
            )
        device_text = _device_text(parsed)

        def operation_words() -> List[PrimitiveValue]:
            words = self._read_words(device_text, count * width)
            values: List[PrimitiveValue] = []
            for index in range(count):
                chunk = words[index * width : (index + 1) * width]
                if data_type_enum in (DataType.SHORT, DataType.USHORT):
                    values.append(
                        chunk[0]
                        if data_type_enum is DataType.USHORT
                        else convert.to_signed(chunk[0], 16)
                    )
                elif data_type_enum in (DataType.INT, DataType.UINT, DataType.FLOAT):
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
        """批量读取:覆写为 ReadDeviceRandom 随机读(单事务)。

        与基类逐点独立容错不同:任一地址非法或控件返回出错代码则
        **整批失败**(原因见 :attr:`last_error`);需要逐点容错请逐点
        调用 :meth:`read`。空列表返回 ``[]``(基类契约,review-1033 §七)。
        """
        if not addresses:
            return []
        data_type_enum = DataType.coerce(data_type)
        _ok, values = self.read_batch(
            [(address, data_type_enum) for address in addresses]
        )
        return self._pack_read_results(addresses, values)

    def read_batch(  # noqa: C901
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """批量读取:16 位条目合并随机读,32/64 位条目各走一笔块读。

        16 位条目(BOOL/SHORT/USHORT)合并为一笔 ``ReadDeviceRandom``
        (MX Component 手册 5.2.5:软元件列表换行分隔,每条 1 点);32/64 位
        条目(INT/UINT/FLOAT/LONG/ULONG/DOUBLE)因本驱动地址编号**原文透传**
        (进制由通信设置实用程序中的 CPU 配置决定),无法安全推导相邻字地址,
        改按条目各发一笔 ``ReadDeviceBlock``(手册 5.2.3,控件内部自行递增
        字号),同一 ``_execute`` 锁内完成,返回值顺序仍与 items 一致。

        注:手册的 ``ReadDeviceBlock2``/``ReadDeviceRandom2`` 为 **16 位
        (SHORT 元素)版本**(5.2.18/5.2.20「2 字节数据」),并非 32 位版,
        故 32/64 位不借助它们。条数上限与块读同口径
        (:data:`MX_MAX_BLOCK_WORDS`)。

        :raises ValueError: 列表为空/类型不支持/条数超限
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        if len(items) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                _("read_batch 条目数超出上限 {}:{}").format(
                    MX_MAX_BLOCK_WORDS, len(items)
                )
            )
        random_texts: List[str] = []
        # 解码计划:(类别, 地址, 位号, 数据类型, 字数);类别 random/block
        plan: List[Tuple[str, str, int, DataType, int]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            parsed = _check_address(address)
            _reject_non_bool_access(parsed, data_type_enum, address)
            if data_type_enum is DataType.BOOL:
                if _is_bit_device(parsed.device):
                    random_texts.append(_device_text(parsed))
                    plan.append(("random", address, 0, data_type_enum, 0))
                else:
                    random_texts.append(_base_text(parsed))
                    plan.append(("random", address, parsed.bit or 0, data_type_enum, 0))
                continue
            if data_type_enum in (DataType.SHORT, DataType.USHORT):
                random_texts.append(_device_text(parsed))
                plan.append(("random", address, 0, data_type_enum, 0))
                continue
            if data_type_enum is DataType.STRING:
                raise ValueError(
                    _("MX 批量读取不支持的数据类型:{}").format(data_type_enum)
                )
            words = data_type_enum.register_size
            # block 条目第 2 位直接存规范软元件文本:operation 免去每条目
            # 重复 _check_address + _device_text(review-1019 P3-4;random
            # 条目该位存原始地址,仅计划自述用,运行期不读)
            plan.append(("block", _device_text(parsed), 0, data_type_enum, words))

        def operation() -> List[PrimitiveValue]:
            raws = self._read_random(random_texts) if random_texts else []
            values: List[PrimitiveValue] = []
            cursor = 0
            for kind, text, bit, data_type_enum, words in plan:
                if kind == "random":
                    raw = raws[cursor]
                    cursor += 1
                    if data_type_enum is DataType.BOOL:
                        values.append(bool((raw >> bit) & 1))
                    elif data_type_enum is DataType.SHORT:
                        values.append(convert.to_signed(raw, 16))
                    else:
                        values.append(raw)
                    continue
                block = self._read_words(text, words)
                if words == 2:
                    values.append(_decode_32(block, data_type_enum))
                else:
                    values.append(_decode_64(block, data_type_enum))
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
        :raises RuntimeError: 只读模式(:attr:`read_only`)下拒绝一切写
        :raises ValueError: 列表为空/值类型不支持/条数超限,或写白名单开启时
            软元件地址不在已绑定点位表内(逐软元件前置校验,零发送)
        """
        if not items:
            raise ValueError(_("write_batch 至少需要一个 (地址, 值) 项"))
        if len(items) > MX_MAX_BLOCK_WORDS:
            raise ValueError(
                _("write_batch 条目数超出上限 {}:{}").format(
                    MX_MAX_BLOCK_WORDS, len(items)
                )
            )
        texts: List[str] = []
        words: List[int] = []
        for address, value in items:
            parsed = _check_address(address)
            # 写安全闸(review-1021 P0-1:随机批量写属写路径,逐软元件过闸)
            self._check_write_allowed(address)
            if parsed.bit is not None:
                raise ValueError(
                    _(
                        "write_batch 不支持字软元件位号 {!r}(位写入需读-改-写语义,请逐点 write_bool)"
                    ).format(address)
                )
            if isinstance(value, bool):
                if not _is_bit_device(parsed.device):
                    raise ValueError(
                        _("write_batch 的 bool 值仅支持位软元件:{!r} ← {!r}").format(
                            address, value
                        )
                    )
                texts.append(_device_text(parsed))
                words.append(1 if value else 0)
                continue
            if isinstance(value, int):
                if _is_bit_device(parsed.device):
                    if value not in (0, 1):
                        raise ValueError(
                            _("write_batch 位软元件 {!r} 的值必须为 0/1:{!r}").format(
                                address, value
                            )
                        )
                elif not INT16_MIN <= value <= UINT16_MAX:
                    raise ValueError(
                        _("write_batch 整数值超出 16 位范围(-32768~65535):{!r}").format(
                            value
                        )
                    )
                texts.append(_device_text(parsed))
                words.append(value & 0xFFFF)
                continue
            raise ValueError(
                _(
                    "write_batch 仅支持 16 位量(bool/int),{} 收到:{!r}"
                    "(32/64 位请逐点写入)"
                ).format(type(value).__name__, value)
            )

        def operation() -> None:
            self._write_random(texts, words)

        ok, _unused = self._execute(operation, is_write=True)
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

        ok, _unused = self._execute(operation, is_write=True)
        return ok

    def get_error_message(self, code: int) -> Tuple[bool, Optional[str]]:
        """把 MX 出错代码转为官方文本(GetErrorMessage)。

        经独立的 **ActSupportMsg** 控件查询(手册 5.2.26:该功能不在
        ActUtlType 上),控件按需创建、**用毕即时释放**(成功/失败路径
        都在 ``finally`` 置空引用,COM 代理随引用计数立即回收——每次
        调用新建是刻意的:STA 控件绑定创建线程,缓存跨线程复用反而
        不可用),ProgID 见
        :data:`omniplc.core.constants.MX_SUPPORT_MSG_PROG_ID`(依据手册 §1.2.1)。

        review-1024 §9.2-5 P2:本方法不依赖 ActUtlType 会话(仅读
        构造期固定的 :attr:`_logical_station_number`),不**经**
        :meth:`_execute` 包装——免触发 ActUtlType 链路 lazy 重连
        (现场最需要查码的"连不上"场景反而能查)与重连抖动。

        :param code: 出错代码(手册第 7 章,如 ``0xC0500100``)
        :return: ``(是否成功, 出错文本)``(文本含出错内容及处理方法);
            失败为 ``(False, None)``
        """
        try:
            com = _new_support_msg_com(self._logical_station_number)
        except OmniPLCInternalError:
            # 控件创建失败(如 COM 不可用)——按公共 API 契约返 (False, None)
            return False, None
        try:
            text = _com_get_error_message(com, int(code))
        except DeviceError:
            return False, None
        else:
            return True, text
        finally:
            com = None  # 与 CreateObject 配对:失败路径也不留代理引用

    def create_monitor(
        self,
        points: Union[Mapping[str, Sequence[str]], TagTable],
        interval: float = 1.0,
        on_change: Optional[Callable[[MonitorEvent], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        deadband: Union[float, int, Mapping[str, float]] = 0.0,
        jitter: float = 0.0,
    ) -> NoReturn:
        """MX Component 不支持周期监视器(STA 假设冲突)。

        review-1024 §9.2-4 P2 揭露:监视器自建线程驱动 ``self._client``
        的 ``_execute``,但 MX 仅在 connect 线程 :func:`_com_initialize`
        一次——监视器线程既不 ``CoInitialize`` 也不封送,真机可能
        正常也可能崩/错值(S_FALSE/高位 0 出错码跨线程传递
        未定义)。明示拒绝优于静默风险。
        """
        raise NotImplementedError(
            _(
                "MX Component 不支持 create_monitor——COM 通道在 connect 线程"
                " 初始化为 STA 假设,监视器自建线程会破坏该假设(真机未核证,"
                " 见 docs/real-machine-checklist.md §MX 监视器)。"
                " 用业务线程 sleep 轮询 + read_*() 替代。"
            )
        )

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
            _("位软元件不支持位号后缀:{!r}(示例:M10 或 D100.3)").format(address)
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
        raise ValueError(_("字符串只能从字软元件存取,收到:{!r}").format(address))
    return parsed


def _reject_non_bool_access(
    parsed: McAddress, data_type: DataType, address: str
) -> None:
    """非 BOOL 的「位号后缀 / 位软元件」访问入参期拒绝(内部函数)。

    MX 对位软元件的单点读(`GetDevice` 返回位 0/1)与块读(`ReadDeviceBlock`
    按 16 点/字)语义不一致,字软元件位号后缀亦无字访问含义;统一拒绝,
    与 MC 的 `read_batch` 口径一致(需要字访问请改用字软元件如 ``D``)。

    :raises ValueError: 非 BOOL 类型带位号后缀,或落在位软元件上
    """
    if data_type is DataType.BOOL:
        return
    if parsed.bit is not None:
        raise ValueError(_("仅布尔类型支持位访问:{!r}").format(address))
    if _is_bit_device(parsed.device):
        raise ValueError(
            _("位软元件 {}{} 只支持 BOOL,字/数值请改用字软元件(如 D)").format(
                parsed.device, parsed.number
            )
        )


def _format_code(code: int) -> str:
    """把 MX 出错代码格式化为十六进制(内部函数)。"""
    return f"0x{code & 0xFFFFFFFF:08X}"
