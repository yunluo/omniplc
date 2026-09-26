"""倍福 TwinCAT ADS 客户端(封装 pyads,AMS/ADS 会话)。

ADS 是倍福 TwinCAT 的设备访问协议(AMS 路由 + 符号句柄读写)。
**不自研协议**,封装成熟库 `pyads`(3.5.1 为最后支持 Python 3.7 的
版本线;Windows 侧还需 Beckhoff 的 ADS 运行库 ``TcAdsDll``——随
TwinCAT/ADS 安装,缺库时 ``import pyads`` 即失败)。本驱动只做两件事:
变量名寻址 + 读写值映射到本库统一契约
(读 ``(bool, value)``、写 ``bool``、失败进 :attr:`last_error`)。

类继承::

    BaseClient
    └── BeckhoffAdsClient   AMS/ADS 会话(默认 AMS 端口 851;会话适配见 _AdsSession)

数据类型映射(DataType → pyads PLCTYPE):
BOOL→BOOL、SHORT→INT(IEC INT = 16 位)、USHORT→UINT、INT→DINT、
UINT→UDINT、LONG→LINT、ULONG→ULINT、FLOAT→REAL、DOUBLE→LREAL、STRING→STRING。
写入值先按本库范围校验,再交 pyads 按类型编码。

错误边界:pyads ``ADSError``(ADS 状态码:符号不存在/长度不符等)→
DeviceError(code=ADS 错误码)不断线;连接失败、未装 pyads 或缺
TcAdsDll → 断线待重连。

字符串:pyads 写只发 ``len+1`` 字节(含结束符)——不得超过 PLC 变量
声明长度(STRING 默认 80);字节编码由 pyads 固定(utf-8),``encoding``
参数不生效。数组下标/结构体/通知(SUM 读、AdsSymbol)留后续版本。
"""
from __future__ import annotations

import re
import struct
from typing import Any, Optional

from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import (
    ADS_DEFAULT_ADS_PORT,
    ADS_NET_ID_SUFFIX,
    INT16_MAX,
    INT16_MIN,
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    UINT16_MAX,
    UINT32_MAX,
    UINT64_MAX,
)
from ...core.debug import log_op, log_warning
from ...core.errors import DeviceError, OmniPLCInternalError, TransportClosedError
from ...core.validation import (
    check_range,
    require_bool,
    require_float,
    require_int,
)
from ...transport.base import BaseTransport
from ...types import DataType, PrimitiveValue

_PLCTYPE_NAMES = {
    DataType.BOOL: "PLCTYPE_BOOL",
    DataType.SHORT: "PLCTYPE_INT",
    DataType.USHORT: "PLCTYPE_UINT",
    DataType.INT: "PLCTYPE_DINT",
    DataType.UINT: "PLCTYPE_UDINT",
    DataType.LONG: "PLCTYPE_LINT",
    DataType.ULONG: "PLCTYPE_ULINT",
    DataType.FLOAT: "PLCTYPE_REAL",
    DataType.DOUBLE: "PLCTYPE_LREAL",
    DataType.STRING: "PLCTYPE_STRING",
}
"""DataType → pyads ``PLCTYPE`` 成员名(惰性解析,保持核心零导入)。"""

_INT_RANGES = {
    DataType.SHORT: (INT16_MIN, INT16_MAX),
    DataType.USHORT: (0, UINT16_MAX),
    DataType.INT: (INT32_MIN, INT32_MAX),
    DataType.UINT: (0, UINT32_MAX),
    DataType.LONG: (INT64_MIN, INT64_MAX),
    DataType.ULONG: (0, UINT64_MAX),
}
"""整数 DataType → (下限, 上限)。"""


def _load_pyads() -> Optional[Any]:
    """加载 pyads 模块;任何失败(未安装/缺 TcAdsDll)返回 None(内部函数)。

    注意 Windows 缺 Beckhoff 运行库时 ``import pyads`` 抛 OSError
    而非 ImportError,故按 Exception 全量捕获。
    """
    try:
        import pyads

        return pyads
    except Exception:
        return None


_ADS_TRANSPORT_ERROR_CODES = frozenset({0x00000705, 0x00000706, 0x00000725})
"""transport 类 ADS 错误码:TwinCAT 侧连接失效(不断线会导致后续调用
持续失败)——0x0705 目标端口未找到 / 0x0706 目标 AMS NetId 不可达 /
0x0725 本机路由器端口已关闭,出现即标记断线走惰性重连。"""


def _translate_ads_error(exc: BaseException) -> OmniPLCInternalError:
    """把 pyads 异常翻译为本库内部异常(内部函数)。

    - ``ADSError`` 且错误码属 transport 类(:data:`_ADS_TRANSPORT_ERROR_CODES`,
      TwinCAT 重启/路由器断开等)→ :class:`OmniPLCInternalError`,
      由基类标记断线、下次事务惰性重连
    - 其余 ``ADSError``(符号不存在/长度不符等设备语义错误)→
      :class:`DeviceError`,``code`` 携带原始 ADS 错误码——链路是好的,
      不断线不重试
    - pyads 缺失/连接中断/内部错误 → :class:`OmniPLCInternalError`
    """
    pyads = _load_pyads()
    error_class = getattr(pyads, "ADSError", None) if pyads is not None else None
    if error_class is not None and isinstance(exc, error_class):
        code = int(getattr(exc, "err_code", 0) or 0)
        if code in _ADS_TRANSPORT_ERROR_CODES:
            return OmniPLCInternalError(
                "ADS 连接失效 0x{:08X}:{}(下次事务将重连)".format(code, exc)
            )
        return DeviceError(f"ADS 出错 0x{code:08X}:{exc}", code)
    return OmniPLCInternalError(
        "ADS 调用失败:{}:{}".format(type(exc).__name__, exc)
    )


_ADS_DEFAULT_STRING_CHARS: int = 80
"""TwinCAT STRING 未标长度时的默认声明长度(STRING ≡ STRING(80))。"""


def _declared_string_chars(symbol_type: Optional[str]) -> Optional[int]:
    """从 ADS 符号类型字符串解析 STRING 声明长度(取不到返回 None,内部函数)。

    TwinCAT 符号类型形如 ``"STRING(80)"``(显式长度)或 ``"STRING"``(默认 80)。
    """
    if not symbol_type:
        return None
    match = re.match(r"STRING\((\d+)\)", symbol_type)
    if match:
        return int(match.group(1))
    if symbol_type == "STRING":
        return _ADS_DEFAULT_STRING_CHARS
    return None


def _safe_close(connection: Any) -> None:
    """尽力断开 ADS 连接,静默失败(内部函数)。"""
    try:
        connection.close()
    except Exception:
        pass


class _AdsSession(BaseTransport):
    """ADS 会话适配器:pyads Connection 适配为传输对象外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 打开
    AMS 连接,``close`` 关闭;ADS 无字节流收发,读写经
    :meth:`read_by_name` / :meth:`write_by_name` 会话方法完成,
    pyads 异常在此边界统一翻译。

    :ivar connection: pyads Connection 实例(仅连接成功后可用)
    """

    def __init__(self, net_id: str, ads_port: int) -> None:
        """ADS 会话适配器。

        :param net_id: 目标 AMS NetId(由 :class:`BeckhoffAdsClient` 组装或显式覆盖)
        :param ads_port: 目标 AMS 端口
        """
        super().__init__()
        self._net_id = net_id
        self._ads_port = ads_port
        self._connection: Any = None
        self._debug_label = f"ads://{net_id}:{ads_port}"

    def connect(self) -> None:
        """打开 AMS 连接(每次连接新建 pyads Connection)。

        :raises OSError: pyads 不可用或连接失败
        """
        pyads = _load_pyads()
        if pyads is None:
            raise OSError(
                "pyads 加载失败(ADS 走线需 pip install omniplc[ads];"
                "Windows 还需 Beckhoff TcAdsDll 运行库)"
            )
        try:
            connection = pyads.Connection(self._net_id, self._ads_port)
        except Exception as exc:
            raise OSError(f"ADS 连接对象创建失败:{exc}")
        try:
            connection.open()
        except OSError:
            _safe_close(connection)
            raise
        except Exception as exc:
            _safe_close(connection)
            raise OSError(
                "ADS 连接失败:{}({})".format(type(exc).__name__, exc)
            )
        self._connection = connection
        try:
            applied = connection.set_timeout(int(self._receive_timeout * 1000))
        except Exception as exc:
            log_warning(
                self._debug_label,
                "set_timeout 下发异常(该路由/固件可能不支持,按 pyads 默认超时):%s",
                exc,
            )
        else:
            if applied is False:
                log_warning(
                    self._debug_label,
                    "set_timeout 未被接受(部分路由器固件无效),实际按 pyads 默认超时",
                )
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """关闭 AMS 连接,幂等。"""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        _safe_close(connection)
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """ADS 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("ADS 走会话通道,无字节流收发")

    def recv(self, size: int) -> bytes:
        """ADS 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("ADS 走会话通道,无字节流收发")

    @property
    def connection(self) -> Any:
        """当前 pyads Connection(仅连接成功后可用,内部属性)。"""
        if self._connection is None:
            raise TransportClosedError("ADS 连接未建立")
        return self._connection

    def read_by_name(self, address: str, plctype_name: str) -> Any:
        """按变量名读值(会话调用,pyads 异常在此翻译)。"""
        pyads = _load_pyads()
        if pyads is None:
            raise OmniPLCInternalError("pyads 加载失败,无法读取 ADS 变量")
        plctype = getattr(pyads, plctype_name, None)
        if plctype is None:
            raise OmniPLCInternalError(f"ADS 类型解析失败:{plctype_name}")
        try:
            value = self.connection.read_by_name(address, plctype)
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ads_error(exc) from exc
        log_op(self._debug_label, "读 %s(%s) → %r", address, plctype_name, value)
        return value

    def write_by_name(self, address: str, value: Any, plctype_name: str) -> None:
        """按变量名写值(会话调用,pyads 异常在此翻译)。"""
        pyads = _load_pyads()
        if pyads is None:
            raise OmniPLCInternalError("pyads 加载失败,无法写入 ADS 变量")
        plctype = getattr(pyads, plctype_name, None)
        if plctype is None:
            raise OmniPLCInternalError(f"ADS 类型解析失败:{plctype_name}")
        try:
            self.connection.write_by_name(address, value, plctype)
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ads_error(exc) from exc
        log_op(self._debug_label, "写 %s(%s) ← %r", address, plctype_name, value)

    def symbol_type(self, address: str) -> Optional[str]:
        """查 PLC 侧符号类型字符串(如 ``"STRING(80)"``);取不到返回 None(内部方法)。

        用于写前预检字符串声明长度;符号信息不可用(旧路由/固件不支持)时
        返回 None,调用方跳过预检(不改变原有行为)。
        """
        pyads = _load_pyads()
        if pyads is None:
            return None
        try:
            symbol = self.connection.get_symbol(address)
        except Exception:
            return None
        value = getattr(symbol, "symbol_type", None)
        return value if isinstance(value, str) else None


class BeckhoffAdsClient(BaseClient):
    """倍福 TwinCAT ADS 客户端(封装 pyads,变量名读写)。

    地址为 TwinCAT 变量名(``MAIN.nCounter`` / ``.gGlobal`` /
    ``GVL.MyVar``),原样透传给 ADS 符号服务。数据类型显式指定
    (推荐 :class:`~omniplc.types.DataType` 枚举),写入按对应
    PLCTYPE 编码并先做范围校验,读取按该类型收窄返回值。

    构造入口与其他客户端一致:IP + AMS 端口(TC3 运行时 1 为 851);
    NetId 默认由 IP 拼 ``.1.1`` 后缀(TC3 常规目标),可用 ``net_id``
    显式覆盖(6 段 0~255 数字,如 ``"10.1.100.5.1.1"``)。

    :example::

        client = BeckhoffAdsClient("192.168.0.10", 851)
        client.connect()
        ok, value = client.read_int("MAIN.nCounter")
        ok = client.write_bool("MAIN.bStart", True)
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        ads_port: int = ADS_DEFAULT_ADS_PORT,
        net_id: str = "",
    ) -> None:
        """初始化 TwinCAT ADS 客户端。

        :param ip_address: PLC 的 IP 或主机名(构造默认 NetId 用)
        :param ads_port: 目标 AMS 端口,TC3 PLC 运行时 1 默认 851
        :param net_id: 目标 AMS NetId 显式覆盖(6 段 0~255 数字);
            默认由 ``ip_address`` 拼 ``.1.1`` 后缀组装
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, ads_port)
        super().__init__(ip_address, int(ads_port))
        self._net_id = _build_net_id(ip_address, net_id)
        self._ads_port = int(ads_port)

    @property
    def net_id(self) -> str:
        """目标 AMS NetId(由 ip_address 组装或显式覆盖)。"""
        return self._net_id

    @property
    def ads_port(self) -> int:
        """目标 AMS 端口。"""
        return self._ads_port

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _AdsSession:
        """取当前会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _AdsSession):
            raise TransportClosedError("内部错误:传输对象不是 ADS 会话")
        return link

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读变量值并按数据类型收窄。"""
        if data_type not in _PLCTYPE_NAMES:
            raise ValueError(f"ADS 不支持的数据类型:{data_type}")
        text = _check_address(address)
        value = self._session().read_by_name(text, _PLCTYPE_NAMES[data_type])
        return _coerce_read(value, data_type, text)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型对应的 PLCTYPE 写变量值。"""
        if data_type not in _PLCTYPE_NAMES:
            raise ValueError(f"ADS 不支持的数据类型:{data_type}")
        text = _check_address(address)
        coerced = _coerce_write(value, data_type)
        self._session().write_by_name(text, coerced, _PLCTYPE_NAMES[data_type])

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串变量(编码由 pyads 固定,length 仅截断)。"""
        text = _check_address(address)
        value = self._session().read_by_name(text, _PLCTYPE_NAMES[DataType.STRING])
        if not isinstance(value, str):
            raise ValueError(
                f"ADS 变量返回类型不符(期望字符串):{text} ← {value!r}"
            )
        return value[:length]

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串变量(写前按 PLC 侧声明长度预检,防溢出污染相邻变量)。"""
        if not isinstance(value, str):
            raise ValueError("字符串必须是 str,收到:{}".format(type(value).__name__))
        text = _check_address(address)
        declared = _declared_string_chars(self._session().symbol_type(text))
        if declared is not None and len(value) > declared:
            raise ValueError(
                "ADS STRING 写入值超 PLC 侧声明长度:{} > {} 字符({!r})".format(
                    len(value), declared, text
                )
            )
        self._session().write_by_name(text, value, _PLCTYPE_NAMES[DataType.STRING])
        return value

    def _create_transport(self) -> BaseTransport:
        return _AdsSession(self._net_id, self._ads_port)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _build_net_id(ip_address: str, net_id: str) -> str:
    """组装目标 AMS NetId(内部函数)。

    显式给出时校验 6 段 0~255 数字;缺省由 IP 拼 ``.1.1`` 后缀。

    :raises ValueError: NetId 格式非法
    """
    text = net_id.strip() if net_id else ""
    if not text:
        return "{}{}".format(ip_address.strip(), ADS_NET_ID_SUFFIX)
    parts = text.split(".")
    if len(parts) != 6 or not all(
        part.isdigit() and 0 <= int(part) <= 255 for part in parts
    ):
        raise ValueError(
            "AMS NetId 非法(应为 6 段 0~255 数字):{!r}(示例:192.168.0.10.1.1)".format(
                net_id
            )
        )
    return text


def _check_address(address: str) -> str:
    """变量名非空校验,返回去首尾空白的原文(内部函数)。

    :raises ValueError: 变量名为空
    """
    text = address.strip() if isinstance(address, str) else ""
    if not text:
        raise ValueError("ADS 变量名不能为空")
    return text


def _coerce_read(value: Any, data_type: DataType, address: str) -> PrimitiveValue:
    """把 ADS 返回值收窄为本库基础类型(内部函数)。

    :raises ValueError: 返回值类型与目标数据类型不符(调用方参数错误,
        与 OPC-UA"返回类型不符"同口径,直接抛出,不断线)
    """
    if value is None:
        raise DeviceError(f"ADS 变量值为空:{address}", 0)
    if data_type is DataType.BOOL:
        if not isinstance(value, bool):
            raise ValueError(
                f"ADS 变量返回类型不符(期望布尔):{address} ← {value!r}"
            )
        return value
    if data_type in (DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT,
                     DataType.LONG, DataType.ULONG):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"ADS 变量返回类型不符(期望整数):{address} ← {value!r}"
            )
        return value
    if data_type in (DataType.FLOAT, DataType.DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"ADS 变量返回类型不符(期望数值):{address} ← {value!r}"
            )
        return float(value)
    if not isinstance(value, str):
        raise ValueError(
            f"ADS 变量返回类型不符(期望字符串):{address} ← {value!r}"
        )
    return value


def _coerce_write(value: PrimitiveValue, data_type: DataType) -> Any:
    """校验写入值(范围沿用库约定,内部函数)。

    :raises ValueError: 值类型或范围非法(参数错误约定,直接抛出)
    """
    if data_type is DataType.BOOL:
        return require_bool(value)
    if data_type in _INT_RANGES:
        number = require_int(value)
        low, high = _INT_RANGES[data_type]
        check_range(number, low, high, data_type.name)
        return number
    if data_type is DataType.FLOAT:
        number_f = require_float(value)
        try:
            struct.pack("<f", number_f)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"float 超出 float32 范围:{value}") from exc
        return number_f
    if data_type is DataType.DOUBLE:
        return require_float(value)
    if not isinstance(value, str):
        raise ValueError("字符串必须是 str,收到:{}".format(type(value).__name__))
    return value
