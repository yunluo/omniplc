"""西门子 S7 客户端(封装 python-snap7,ISO-on-TCP 102)。

依据:Siemens S7-1500 Communication Function Manual §3.5 p.22(ISO-on-TCP
端口 102,RFC 1006,ES/HMI/OPC 等 S7 通信)、§7 p.50(PUT/GET 指令,仅绝对
寻址数据块、需在 CPU 保护组态开启该服务)、§6.4(开放通信);S7comm 数据项
编码(TPKT/COTP/S7 PDU、数据长度/DB 寻址、多变量 ReadMultiVars 每请求 20
项)公开手册未逐条收录,依赖 python-snap7(含其 MAX_VARS),**待核**。

S7comm 是完整私有协议栈(TPKT/COTP/S7 PDU、机架/槽位路由、
S7-1200/1500 的 PUT-GET 授权与优化块限制),**不自研**,封装成熟库
`python-snap7`,依赖按解释器版本二选一(``s7`` extra 环境标记自动生效,
核心库本体仍为 3.7.9+):

- Python 3.7~3.9 → **1.3**:C 库封装末版线,wheel 捆绑 64 位原生库,
  32 位 Python 需自备 32 位 snap7.dll 并经 ``dll_path`` 指定
- Python 3.10+ → **3.x**:3.0 起纯 Python 实现,不再需要原生 DLL

两线 API 有差异,均在边界处适配:错误类 1.x/2.x 抛 RuntimeError、
3.x 抛 ``S7Error`` 谱系(见 ``_SNAP7_ERRORS``);area 参数 1.x/2.x 要求
``Areas`` 枚举成员、3.x 收裸 int(统一经 :func:`_snap7_area` 转换);
``read_multi_vars`` 1.x/2.x 收 ctypes ``S7DataItem`` 数组、3.x 收 dict
列表(内部优化器,``MAX_VARS=20`` 上限);构造参数 1.x/2.x 真实加载原生库、
3.x 忽略 ``lib_location``。

类继承::

    BaseClient
    └── SiemensS7Client   S7 会话(默认 rack 0 / slot 1 / 端口 102;适配见 _S7Session)

地址语法见 :mod:`omniplc.plc.siemens.address`(DB/I/Q/M,尺寸由显式
DataType 决定,大端序)。S7-1200/1500 侧需勾选"允许来自远程对象的
PUT/GET 通信访问",且 DB 须为**非优化块**(绝对寻址)。

错误边界:snap7 抛错无统一类型区分,以 ``Cli_GetConnected``
连接态判别——在线 → DeviceError(PLC 拒绝/地址错,不断线),断连 →
OSError(惰性重连);连接建立失败 → OSError。

v1 范围:单点读写(位读改写)+ S7 String/WString + 多变量批量读
(``read_batch``/``read_many``,snap7 ``read_multi_vars`` 双线适配);
块操作、SZL 系统状态留后续版本。
"""
from __future__ import annotations

import os
import struct
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple, Union

from ...core import convert
from ...core.base_client import BaseClient, DEFAULT_STRING_ENCODING, validate_endpoint
from ...core.constants import (
    S7_DEFAULT_PORT,
    S7_DEFAULT_RACK,
    S7_DEFAULT_SLOT,
    S7_MAX_MULTI_VARS,
    S7_RACK_MAX,
    S7_SLOT_MAX,
    S7_WSTRING_DEFAULT_LENGTH,
)
from ...core.debug import log_op, log_warning
from ...core.errors import DeviceError, OmniPLCInternalError, TransportClosedError
from ...core.validation import require_bool, require_float, require_int
from ...core.types import DataType, PrimitiveValue
from ...transport.base import BaseTransport
from .address import area_code, parse_s7_address
from ...core.i18n import _

_SIZES = {
    DataType.BOOL: 1,
    DataType.SHORT: 2,
    DataType.USHORT: 2,
    DataType.INT: 4,
    DataType.UINT: 4,
    DataType.LONG: 8,
    DataType.ULONG: 8,
    DataType.FLOAT: 4,
    DataType.DOUBLE: 8,
}
"""数值 DataType → 字节数(S7 大端序)。"""

_INT_FORMATS = {
    DataType.SHORT: ">h",
    DataType.USHORT: ">H",
    DataType.INT: ">i",
    DataType.UINT: ">I",
    DataType.LONG: ">q",
    DataType.ULONG: ">Q",
}
"""整数 DataType → struct 大端格式(含符号语义)。"""

_SNAP7_ERRORS: Tuple[Any, ...] = (RuntimeError,)
"""snap7 错误类元组(会话边界捕获):1.x/2.x 抛 RuntimeError;3.x 纯
Python 抛 ``S7Error`` 谱系(基类挂在 snap7.client 命名空间,由
:func:`_new_client` 探测并入表)。"""

_SNAP7_TRANSPORT_ERROR_CODES: Tuple[int, ...] = (
    0x00090000,  # errIsoSendPacket:ISO-on-TCP 发送失败(链路死亡/半开)
    0x000A0000,  # errIsoRecvPacket:ISO-on-TCP 接收失败(链路死亡/半开)
    0x02000000,  # errCliJobTimeout:作业超时(响应未达,典型半开形态)
)
"""表示**传输层故障**的 snap7 错误码(经 snap7.error 错误码表核实):
半开连接(拔线/断电)下 ``get_connected()`` 本地标志不翻转,这些码是
判"真断连"的第一依据,命中即 OSError(惰性重连),不看连接标志。"""

_SNAP7_ERROR_CODE_NAMES: Dict[int, str] = {
    0x00090000: "errIsoSendPacket",
    0x000A0000: "errIsoRecvPacket",
    0x02000000: "errCliJobTimeout",
}
"""传输类错误码 → snap7 官方名称(异常文本通常携带该名称)。"""

_SNAP7_TRANSPORT_TEXT_PATTERNS: Tuple[str, ...] = (
    "an error occurred during send",  # 1.x C 库 errIsoSendPacket 描述文本
    "an error occurred during recv",  # 1.x C 库 errIsoRecvPacket 描述文本
    "job timeout",  # 1.x C 库 errCliJobTimeout 描述文本
)
"""1.x(≤2.x)C 库 ``Cli_ErrorText`` 对上述传输码返回的**人类描述文本**
小写子串(snap7 C 源 Cli_ErrorText 表;已知码不走 "Unknown error" 回退分支,
故十六进制码与 err* 名在该线文本中恒不出现)。1.3 ``check_error`` 抛
``RuntimeError(bytes)``,``str()`` 后即 bytes repr(如
``b' ISO : An error occurred during recv'``),子串匹配不受影响。"""


def _is_snap7_transport_error(exc: BaseException) -> bool:
    """判断 snap7 异常是否携带传输类错误码(内部函数)。

    错误码藏在异常文本里:1.x 的 ``RuntimeError`` 文本来自 **C 库
    ``Cli_ErrorText`` 的人类描述**(形如 ``b' ISO : An error occurred
    during recv'``,不含 err* 名与十六进制码);3.x ``S7Error`` 文本则
    携带 err* 名或十六进制码。按码表(hex/名称/描述文本)三线匹配;
    匹配不上返回 False(回退连接标志判据)。
    """
    text = str(exc).lower()
    for code in _SNAP7_TRANSPORT_ERROR_CODES:
        hex_text = "0x{:08X}".format(code)
        name = _SNAP7_ERROR_CODE_NAMES.get(code, "")
        if hex_text.lower() in text or (name and name.lower() in text):
            return True
    for pattern in _SNAP7_TRANSPORT_TEXT_PATTERNS:
        if pattern in text:
            return True
    return False

_AREAS_ENUM: Any = False
"""snap7 ``Areas`` 枚举类缓存:False = 未探测,None = 探测失败(裸 int
透传),否则为枚举类。1.x 在 ``snap7.types``、2.x/3.x 在 ``snap7.type``。"""


_SNAP7_RECV_TIMEOUT_PARAM: Any = False
"""snap7 RecvTimeout 参数号缓存:False = 未探测,None = 探测失败(跳过下发),
否则为参数号。1.x 为 ``snap7.types.RecvTimeout = 5``(模块级 int),3.x 为
``snap7.type.Parameter.RecvTimeout = 5``(枚举成员);数值同源 snap7 C 库
``P_U16_RCV_TIMEOUT``,双轨键型不同必须分别取号(3.x ``set_param`` 按
Parameter 枚举键存取 ``_params``,裸 int 不命中)。"""


def _snap7_recv_timeout_param() -> Any:
    """探测当前 snap7 轨道的 RecvTimeout 参数号(内部函数)。"""
    global _SNAP7_RECV_TIMEOUT_PARAM
    if _SNAP7_RECV_TIMEOUT_PARAM is False:
        _SNAP7_RECV_TIMEOUT_PARAM = None
        for module_name in ("snap7.types", "snap7.type"):
            try:
                module = __import__(module_name, fromlist=["Parameter"])
            except ImportError:
                continue
            candidate = getattr(module, "RecvTimeout", None)
            if candidate is not None:
                _SNAP7_RECV_TIMEOUT_PARAM = candidate
                break
            parameter = getattr(module, "Parameter", None)
            if parameter is not None and getattr(parameter, "RecvTimeout", None) is not None:
                _SNAP7_RECV_TIMEOUT_PARAM = parameter.RecvTimeout
                break
    return _SNAP7_RECV_TIMEOUT_PARAM


_SNAP7_1X_TYPES: Any = False
"""snap7 1.x ctypes 多变量读类型对缓存:False = 未探测,None = 探测失败
(1.x 类型面不可用),否则为 ``(S7DataItem, S7WLByte)``。仅 1.x/2.x(C 封装
线)可达——``read_multi_vars`` 按 ``MAX_VARS`` 分流,3.x 纯 Python 线走
dict 通道不取此类型;1.x 在 ``snap7.types``、2.x 在 ``snap7.type``。以
``__import__`` 动态探测而非静态 import:python-snap7 3.2.0 已移除
``snap7.types`` 模块,静态写法在 3.10+ 环境被类型检查器判未解析导入。"""


def _snap7_1x_types() -> Any:
    """探测当前 snap7 轨道的 ``(S7DataItem, S7WLByte)`` 类型对(内部函数)。"""
    global _SNAP7_1X_TYPES
    if _SNAP7_1X_TYPES is False:
        _SNAP7_1X_TYPES = None
        for module_name in ("snap7.types", "snap7.type"):
            try:
                module = __import__(module_name, fromlist=["S7DataItem"])
            except ImportError:
                continue
            data_item = getattr(module, "S7DataItem", None)
            wl_byte = getattr(module, "S7WLByte", None)
            if data_item is not None and wl_byte is not None:
                _SNAP7_1X_TYPES = (data_item, wl_byte)
                break
    if _SNAP7_1X_TYPES is None:
        raise OmniPLCInternalError(
            _("snap7 类型导入失败:{}").format("snap7.types/S7WLByte 不可用")
        )
    return _SNAP7_1X_TYPES


def _snap7_area(area: int) -> Any:
    """协议区码 int → snap7 ``Areas`` 枚举成员(内部函数)。

    1.x 的 ``read_area`` 对 area 做枚举成员校验(裸 int 抛 ValueError)、
    ``write_area`` 直接取 ``area.value``(裸 int 抛 AttributeError),
    2.x 校验更严;3.x 虽收裸 int,统一转枚举全兼容。探测不到枚举时
    (假 Client 单测环境)原样返回 int。

    :param area: 协议区码(0x81 PE / 0x82 PA / 0x83 MK / 0x84 DB)
    """
    global _AREAS_ENUM
    if _AREAS_ENUM is False:
        _AREAS_ENUM = None
        for module_name in ("snap7.type", "snap7.types"):
            try:
                module = __import__(module_name, fromlist=["Areas"])
            except ImportError:
                continue
            areas = getattr(module, "Areas", None)
            if areas is not None:
                _AREAS_ENUM = areas
                break
    if _AREAS_ENUM is not None:
        try:
            return _AREAS_ENUM(area)
        except ValueError:
            pass
    return area


def _new_client(dll_path: str) -> Any:
    """创建 snap7 Client(模块级,单测以假对象替换;内部函数)。

    :param dll_path: 原生库路径(仅 1.x/2.x 生效;3.x 纯 Python 忽略)
    :raises OSError: python-snap7 未安装或 snap7 原生库加载失败
    """
    global _SNAP7_ERRORS
    try:
        import snap7.client
    except Exception as exc:
        raise OSError(
            _("python-snap7 加载失败(pip install omniplc[s7]):{}").format(exc)
        ) from exc
    error_base = getattr(snap7.client, "S7Error", None)
    if error_base is not None:
        _SNAP7_ERRORS = (RuntimeError, error_base)
    try:
        return snap7.client.Client(dll_path or None)
    except (OSError, RuntimeError) as exc:
        raise OSError(
            _("snap7 原生库加载失败:{}(3.7~3.9 用 python-snap7 1.3:64 位"
            " Python 可用捆绑 DLL,32 位需自备 32 位 snap7.dll 经 dll_path"
            " 指定;3.10+ 为纯 Python 实现无需 DLL)").format(exc)
        ) from exc


class _S7Session(BaseTransport):
    """S7 会话适配器:snap7 Client 适配为传输对象外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 加载
    snap7 库并连 CPU,``close`` 断开并销毁;无字节流收发,区域读写经
    :meth:`read_area` / :meth:`write_area` 完成,snap7 错误(1.x/2.x
    RuntimeError、3.x S7Error 谱系)在此边界按连接态翻译
    (在线→DeviceError 不断线,断连→OSError 惰性重连)。

    ``receive_timeout`` 经 snap7 ``SetParam(RecvTimeout)`` 下发(秒×1000,
    C 库默认 5000 ms / 3.x 默认 3000 ms),连接建立时与属性修改时都生效
    (第八轮 P2-4:原实现完全未接线)。
    """

    def __init__(
        self,
        ip_address: str,
        rack: int,
        slot: int,
        port: int,
        dll_path: str,
    ) -> None:
        """S7 会话适配器。

        :param ip_address: PLC 的 IP 或主机名
        :param rack: 机架号
        :param slot: 槽位号
        :param port: ISO-on-TCP 端口,标准 102
        :param dll_path: snap7 原生库路径(1.x/2.x 生效,留空用捆绑库)
        """
        super().__init__()
        self._ip_address = ip_address
        self._rack = rack
        self._slot = slot
        self._port = port
        self._dll_path = dll_path
        self._client: Optional[Any] = None
        self._debug_label = "s7://{}:{}(机架{}槽位{})".format(
            ip_address, port, rack, slot
        )

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒)。"""
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒):存储并热下发 snap7 RecvTimeout(毫秒)。"""
        if seconds <= 0:
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
        self._receive_timeout = float(seconds)
        self._apply_recv_timeout()

    def _apply_recv_timeout(self) -> None:
        """把当前 receive_timeout 下发为 snap7 RecvTimeout(内部方法)。

        参数号按轨道探测(1.x int / 3.x Parameter 枚举);探测失败或
        set_param 异常按告警降级(snap7 默认超时),不影响连接。
        """
        client = self._client
        if client is None:
            return
        param = _snap7_recv_timeout_param()
        if param is None:
            return
        try:
            client.set_param(param, int(self._receive_timeout * 1000))
        except Exception as exc:
            log_warning(
                self._debug_label,
                "RecvTimeout 下发失败(按 snap7 默认超时):%s",
                exc,
            )

    def connect(self) -> None:
        """加载 snap7 库并连接 CPU(每次连接新建 Client)。

        :raises OSError: 库加载失败或连接失败(拒绝/超时/路由参数不符)
        """
        client = _new_client(self._dll_path)
        try:
            client.connect(self._ip_address, self._rack, self._slot, self._port)
        except _SNAP7_ERRORS as exc:
            raise OSError(
                _("S7 连接失败:{}(可能原因:① IP/机架/槽位不符;② 网络/防火墙阻断 "
                "ISO-on-TCP 102;③ 1200/1500 未开启 PUT/GET 访问授权)").format(exc)
            ) from exc
        self._client = client
        self._apply_recv_timeout()
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """断开连接并销毁 Client,幂等。"""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.destroy()
        except Exception:
            pass
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    def recv(self, size: int) -> bytes:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("S7 走会话通道,无字节流收发"))

    def read_area(self, area: int, db_number: int, start: int, size: int) -> bytes:
        """读一块区域字节(会话调用,异常在此翻译)。"""
        try:
            data = self._require_client().read_area(
                _snap7_area(area), db_number, start, size
            )
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc, db_number)
        log_op(
            self._debug_label,
            "read area=0x%02X db=%d start=%d size=%d → %dB",
            area,
            db_number,
            start,
            size,
            len(data),
        )
        return bytes(data)

    def get_cpu_state(self) -> str:
        """读 CPU 运行状态(snap7 GetCpuState,会话调用,异常在此翻译)。

        返回 snap7 状态枚举名(``"S7CpuStatusRun"``/``"S7CpuStatusStop"``
        等)——1.x 与 3.x 的枚举成员数值不一致,库只透传名字不解析数值。
        零副作用,同时是探活探测命令(能应答即 CPU 会话存活)。
        """
        try:
            state = self._require_client().get_cpu_state()
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc)
        log_op(self._debug_label, "cpu state → %r", state)
        return getattr(state, "name", str(state))

    def read_multi_vars(
        self, specs: "Sequence[Tuple[int, int, int, int]]"
    ) -> "List[bytes]":
        """多变量一次读(会话调用,异常在此翻译)。

        :param specs: ``(区码, DB 号, 字节起点, 字节数)`` 列表,最多 20 条
            (snap7 MAX_VARS 上限)
        :return: 与 specs 顺序一致的逐条字节
        :raises DeviceError: 在线但单条目读取失败(条目级 Result 非 0)
        :raises OSError: 断连/整调用失败(惰性重连)

        双线适配:1.x/2.x(C 封装线)``Cli_ReadMultiVars`` 收 **ctypes
        ``S7DataItem`` 数组**(WordLen=BYTE,单 PDU 组包);3.x(纯 Python 线)
        收 **dict 列表**(内部优化器合并相邻读,``MAX_VARS=20`` 上限)。
        """
        client = self._require_client()
        if getattr(type(client), "MAX_VARS", None) is not None:
            items = [
                {
                    "area": getattr(_snap7_area(area), "value", _snap7_area(area)),
                    "db_number": db,
                    "start": start,
                    "size": size,
                }
                for area, db, start, size in specs
            ]
            try:
                _rc, results = client.read_multi_vars(items)
            except _SNAP7_ERRORS as exc:
                self._raise_link_aware(exc)
            return [bytes(item) for item in results]
        import ctypes

        data_item, wl_byte = _snap7_1x_types()
        array = (data_item * len(specs))()
        buffers = []
        for index, (area, db, start, size) in enumerate(specs):
            buffer = (ctypes.c_uint8 * size)()
            array[index].Area = getattr(_snap7_area(area), "value", _snap7_area(area))
            array[index].WordLen = int(wl_byte)
            array[index].DBNumber = db
            array[index].Start = start
            array[index].Amount = size
            array[index].pData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8))
            buffers.append(buffer)
        try:
            _rc, array = client.read_multi_vars(array)
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc)
        for index, item in enumerate(array):
            if item.Result != 0:
                raise DeviceError(
                    _("S7 多变量读条目 {} 失败,错误码 0x{:08X}").format(
                        index, int(item.Result)
                    ),
                    0,
                )
        return [bytes(buffer) for buffer in buffers]

    def write_area(self, area: int, db_number: int, start: int, data: bytes) -> None:
        """写一块区域字节(会话调用,异常在此翻译)。"""
        try:
            self._require_client().write_area(
                _snap7_area(area), db_number, start, bytearray(data)
            )
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc, db_number)
        log_op(
            self._debug_label,
            "write area=0x%02X db=%d start=%d %dB",
            area,
            db_number,
            start,
            len(data),
        )

    def _raise_link_aware(self, exc: BaseException, db_number: int = 0) -> NoReturn:
        """按 snap7 错误码与连接态翻译错误(内部方法,恒抛出)。

        分类依据(两层):
        1. **snap7 错误码**(优先)——1.3 的 ``Cli_GetConnected`` 读的是
           C 库本地标志,会话中途 socket 死亡(拔线/断电)时**不翻转**,
           不能单独作为断连判据;``errIsoSendPacket(0x00090000)``/
           ``errIsoRecvPacket(0x000A0000)``/``errCliJobTimeout(0x02000000)``
           三个码表示发送/接收失败或作业超时,属**传输层故障** → OSError
           (惰性重连)。
        2. 其余错误 → 在线视为 PLC 侧拒绝(DeviceError,不断线),DB
           访问附"优化块访问"提示;``get_connected()`` 为 False(显式
           disconnect 后)时同样按断连处理。
        """
        if not self._is_connected() or _is_snap7_transport_error(exc):
            raise OSError(_("S7 连接已断:{}").format(exc))
        message = _("S7 错误:{}").format(exc)
        if db_number:
            message += (
                _("(按绝对地址访问 DB 失败:若为 S7-1200/1500,请确认该 DB ")
                + _("已在 TIA 中取消 Optimized block access)")
            )
        raise DeviceError(message, 0)

    def _is_connected(self) -> bool:
        """取 snap7 本地连接态标志(不产生网络流量;异常视为断连)。

        注意:1.3 该标志在 socket 半开(拔线/断电)下不翻转,故
        :meth:`_raise_link_aware` 以 snap7 传输类错误码优先判定,本标志
        只兜底显式 disconnect 的场景;3.x 起为主动探测,两口径均安全。
        """
        try:
            return bool(self._require_client().get_connected())
        except Exception:
            return False

    def _require_client(self) -> Any:
        """取当前 snap7 Client,未建立则抛出(内部方法)。"""
        if self._client is None:
            raise TransportClosedError(_("S7 会话未建立"))
        return self._client


class SiemensS7Client(BaseClient):
    """西门子 S7 客户端(封装 python-snap7,rack/slot 路由)。

    :example::

        client = SiemensS7Client("192.168.0.1", rack=0, slot=1)
        client.connect()
        ok, value = client.read_float("DB1.DBD6")
        ok = client.write_bool("DB1.DBX0.3", True)
        ok, text = client.read_string("DB1.DBS20", length=32)
    """

    # 探活:snap7 GetCpuState(零副作用系统级读,见 _ping_probe)
    _has_ping = True

    def __init__(
        self,
        ip_address: str = "192.168.0.1",
        port: int = S7_DEFAULT_PORT,
        rack: int = S7_DEFAULT_RACK,
        slot: int = S7_DEFAULT_SLOT,
        dll_path: str = "",
    ) -> None:
        """初始化 S7 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param rack: 机架号,S7_DEFAULT_RACK(0)
        :param slot: 槽位号,1200/1500 常用 1;300/400 的 CPU 常在 2
        :param dll_path: snap7 原生库路径显式覆盖,仅 1.x/2.x(C 封装线)
            生效——32 位 Python 需自备 32 位 snap7.dll;3.x 纯 Python 实现
            忽略此参数;留空用捆绑库
        :raises ValueError: 参数非法(dll_path 非空但文件不存在)
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if not 0 <= int(rack) <= S7_RACK_MAX:
            raise ValueError(_("机架号必须在 0~{} 之间,收到:{}").format(S7_RACK_MAX, rack))
        if not 0 <= int(slot) <= S7_SLOT_MAX:
            raise ValueError(_("槽位号必须在 0~{} 之间,收到:{}").format(S7_SLOT_MAX, slot))
        self._rack = int(rack)
        self._slot = int(slot)
        self._dll_path = dll_path.strip()
        if self._dll_path and not os.path.isfile(self._dll_path):
            raise ValueError(
                _("dll_path 指定的 snap7 原生库不存在:{!r}").format(self._dll_path)
            )

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

    def _session(self) -> _S7Session:
        """取当前 S7 会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _S7Session):
            raise TransportClosedError(_("内部错误:传输对象不是 S7 会话"))
        return link

    def _create_transport(self) -> BaseTransport:
        return _S7Session(
            self._ip_address, self._rack, self._slot, self._port, self._dll_path
        )

    # ------------------------------------------------------------------
    # 状态读与探活(snap7 GetCpuState)
    # ------------------------------------------------------------------

    def get_cpu_state(self) -> Tuple[bool, Optional[str]]:
        """读 CPU 运行状态(snap7 GetCpuState;零副作用)。

        返回 ``(是否成功, 状态枚举名)``,如 ``"S7CpuStatusRun"``/
        ``"S7CpuStatusStop"``(1.x/3.x 枚举数值不一致,统一按名字透传)。

        同时是 :meth:`ping` 的探测命令:能应答即 CPU 会话存活。
        """
        return self._execute(lambda: self._session().get_cpu_state())

    def _ping_probe(self) -> str:
        """探活探测命令:snap7 GetCpuState(内部方法)。"""
        return self._session().get_cpu_state()

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读数据项并按 DataType 尺寸收窄(大端序)。"""
        if data_type not in _SIZES:
            raise ValueError(_("S7 不支持的数据类型:{}").format(data_type))
        parsed = parse_s7_address(address)
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    _("S7 按位读取需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(address)
                )
        elif parsed.bit is not None:
            raise ValueError(
                _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(address)
            )
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, _SIZES[data_type]
        )
        if data_type is DataType.BOOL:
            # 前置校验已保证 bit 非空,or 0 仅供类型收窄
            return bool((data[0] >> (parsed.bit or 0)) & 1)
        if data_type is DataType.FLOAT:
            return struct.unpack(">f", data)[0]
        if data_type is DataType.DOUBLE:
            return struct.unpack(">d", data)[0]
        return int.from_bytes(data, "big", signed=data_type in (DataType.SHORT, DataType.INT, DataType.LONG))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
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
                self._write_string(address, value, DEFAULT_STRING_ENCODING)
                return
            raise ValueError(_("S7 不支持的数据类型:{}").format(data_type))
        parsed = parse_s7_address(address)
        session = self._session()
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    _("S7 按位写入需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(address)
                )
            flag = require_bool(value)
            raw = session.read_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
            )
            byte = (raw[0] | (1 << parsed.bit)) if flag else (raw[0] & ~(1 << parsed.bit))
            session.write_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, bytes([byte & 0xFF])
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
                _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(address)
            )
        session.write_area(area_code(parsed.area), parsed.db_number, parsed.byte_index, data)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读 S7 String(头 2 字节 = 声明长/实际长,正文按声明长)。

        实际长超出请求 ``length`` 时按 ``length`` 截断返回(不报错不丢帧)。
        """
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        size = length + 2
        data = self._session().read_area(
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
        return convert.decode_string(data[2:2 + actual], encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写 S7 String(声明长字节保留 PLC 侧现值,仅覆盖实际长字节)。

        先读 1 字节取 PLC 侧声明长(STRING[x] 的 x),写入值超声明长时
        拒绝(防溢出污染相邻变量);声明长字节读得 0(未初始化区)时
        按本次编码长度落盘(与旧版行为兼容)。
        """
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        encoded = convert.encode_string(value, len(value.encode(encoding)), encoding)
        head = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
        )
        declared_max = head[0] if head else 0
        if declared_max == 0:
            # 未初始化区(声明长为 0 非法):退回旧口径,声明长=实际长
            declared_max = len(encoded)
        if len(encoded) > declared_max:
            raise ValueError(
                _("S7 String 写入值超出 PLC 侧声明长:{} > {} 字符({!r})").format(
                    len(encoded), declared_max, address
                )
            )
        header = bytes([declared_max, len(encoded)])
        self._session().write_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, header + encoded
        )
        return value

    def read_wstring(
        self, address: str, length: int = S7_WSTRING_DEFAULT_LENGTH
    ) -> Tuple[bool, Optional[str]]:
        """读 S7 WString(UTF-16BE,支持中文/日文等非 ASCII 文本)。

        WString 布局:声明长(2 字节,字符数)+ 实际长(2 字节)+ 字符
        (每字符 2 字节,UTF-16BE 大端)。实际长超出请求 ``length`` 时按
        ``length`` 截断返回。

        :param address: 字符串起点地址,如 ``"DB1.DBW20"``/``"DB1.DBS20"``
        :param length: 最多读取的字符数,默认 64
        :return: ``(是否成功, 文本)``;失败为 ``(False, None)``
        :raises ValueError: 地址/长度非法
        """
        if length <= 0:
            raise ValueError(_("length 必须大于 0,收到:{}").format(length))
        ok, value = self._execute(
            lambda: self._read_wstring_impl(address, int(length))
        )
        if not ok or value is None:
            return False, None
        return True, str(value)

    def write_wstring(self, address: str, value: str) -> bool:
        """写 S7 WString(UTF-16BE,保留 PLC 侧声明长,超声明长拒绝)。

        :param address: 字符串起点地址
        :param value: 待写入文本(不能为空;须为 BMP 字符,避免代理对歧义)
        :return: 是否成功
        :raises ValueError: 地址/值非法或超出 PLC 侧声明长
        """
        if not value:
            raise ValueError(_("value 不能为空字符串"))
        ok, _unused = self._execute(
            lambda: self._write_wstring_impl(address, str(value)), is_write=True
        )
        return ok

    def _read_wstring_impl(self, address: str, length: int) -> PrimitiveValue:
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        size = 4 + length * 2
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, size
        )
        if len(data) < 4:
            raise DeviceError(_("S7 WString 响应过短:{}").format(len(data)), 0)
        actual = int.from_bytes(data[2:4], "big")
        if actual <= 0:
            return ""
        if actual > length:
            actual = length
        return convert.decode_string(data[4:4 + actual * 2], "utf-16-be")

    def _write_wstring_impl(self, address: str, value: str) -> PrimitiveValue:
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(_("S7 字符串地址不带位号:{!r}").format(address))
        encoded = value.encode("utf-16-be")
        if len(encoded) != len(value) * 2:
            raise ValueError(_("S7 WString 仅支持 BMP 字符(不含代理对):{!r}").format(address))
        head = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, 2
        )
        declared_max = int.from_bytes(head[:2], "big") if len(head) >= 2 else 0
        if declared_max == 0:
            declared_max = len(value)
        if len(value) > declared_max:
            raise ValueError(
                _("S7 WString 写入值超出 PLC 侧声明长:{} > {} 字符({!r})").format(
                    len(value), declared_max, address
                )
            )
        header = declared_max.to_bytes(2, "big") + len(value).to_bytes(2, "big")
        self._session().write_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, header + encoded
        )
        return value

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:同区域字节起点起连续 ``count`` 个元素,snap7 ``read_area`` 单事务。

        地址只定位**区域 + 字节起点**,总字节数 = ``count × 类型字节数``
        (SHORT/USHORT 2、INT/UINT/FLOAT 4、LONG/ULONG/DOUBLE 8,大端),
        按类型尺寸切片解码。
        总字节数不设入参上限(review-1002 P3):单事务容量受连接协商 PDU
        约束,超限时 snap7 运行期报错、按整批容错 ``(False, None)`` 返回
        (非入参期 ``ValueError``)——大跨度数据请调用方自行分段。
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
            raise ValueError(_("S7 read_range 不支持的数据类型:{}").format(data_type_enum))
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(
                _("S7 read_range 不支持位地址:{!r}(位访问请逐点读)").format(address)
            )
        size = _SIZES[data_type_enum]

        def operation() -> List[PrimitiveValue]:
            data = self._session().read_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, size * count
            )
            values: List[PrimitiveValue] = []
            for index in range(count):
                blob = data[index * size:(index + 1) * size]
                if data_type_enum is DataType.FLOAT:
                    values.append(struct.unpack(">f", blob)[0])
                elif data_type_enum is DataType.DOUBLE:
                    values.append(struct.unpack(">d", blob)[0])
                else:
                    values.append(
                        int.from_bytes(
                            blob,
                            "big",
                            signed=data_type_enum in (DataType.SHORT, DataType.INT, DataType.LONG),
                        )
                    )
            return values

        ok, values = self._execute(operation)
        if not ok or values is None:
            return False, None
        return True, values

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 snap7 ``read_multi_vars`` 单事务(多变量一次 PDU 组包)。

        与基类逐点独立容错不同:任一地址非法或 PLC 拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        条目上限 20(snap7 MAX_VARS;超限入参期 ``ValueError``)。
        """
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
        """多变量批量读取:snap7 ``read_multi_vars`` 单事务混读(DB/I/Q/M)。

        每个条目独立寻址(区域/DB/字节起点可不同);BOOL 读 1 字节后本地
        提位;STRING 为变长不支持批量(请用 :meth:`read_string`)。条目上限
        **20**(snap7 ``MAX_VARS``;S7 ReadMultiVars 每请求 20 项)。

        :param items: ``(地址, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空/地址或类型非法/条目数超限
        """
        if not items:
            raise ValueError(_("read_batch 至少需要一个 (地址, 数据类型) 项"))
        if len(items) > S7_MAX_MULTI_VARS:
            raise ValueError(
                _("S7 多变量读条目数超出上限 {}:{}(snap7 MAX_VARS)").format(
                    S7_MAX_MULTI_VARS, len(items)
                )
            )
        specs: List[Tuple[int, int, int, int]] = []
        plan: List[Tuple[str, int, Optional[int], DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            if data_type_enum not in _SIZES:
                raise ValueError(_("S7 批量读取不支持的数据类型:{}").format(data_type_enum))
            parsed = parse_s7_address(address)
            if data_type_enum is DataType.BOOL:
                if parsed.bit is None:
                    raise ValueError(
                        _("S7 按位读取需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)").format(address)
                    )
                plan.append(("bit", len(specs), parsed.bit, data_type_enum))
            else:
                if parsed.bit is not None:
                    raise ValueError(
                        _("S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)").format(address)
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

        def operation() -> List[PrimitiveValue]:
            blobs = self._session().read_multi_vars(specs)
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

        return self._execute(operation)

    @staticmethod
    def _pack(fmt: str, value: PrimitiveValue) -> bytes:
        """大端打包,越界 struct 报错统一转 ValueError(内部方法)。"""
        try:
            return struct.pack(fmt, value)
        except (struct.error, OverflowError) as exc:
            raise ValueError(_("S7 写入值超出类型范围:{}").format(value)) from exc
