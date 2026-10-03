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
from typing import Any, Optional, Tuple

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
from ...core.types import DataType, PrimitiveValue
from ...core.i18n import _

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
    """加载 pyads 模块;未安装/缺 TcAdsDll 返回 None(内部函数)。

    只捕 ImportError(未安装)与 OSError(Windows 缺 Beckhoff 运行库时
    ``import pyads`` 抛 OSError 而非 ImportError);其余异常(MemoryError
    等)向上抛——真正的运行环境崩溃不得静默伪装成"pyads 不可用"
    (review-1005 P2)。
    """
    try:
        import pyads

        return pyads
    except (ImportError, OSError):
        return None


_ADS_TRANSPORT_ERROR_CODES = frozenset(
    {
        0x06,  # ERR_TARGETPORTNOTFOUND  ADS 服务未启动/不可达
        0x07,  # ERR_TARGETMACHINENOTFOUND  未找到 AMS 路由
        0x0D,  # ERR_PORTNOTCONNECTED  端口未连接
        0x12,  # ERR_PORTDISABLED  TwinCAT 系统服务未启动
        0x1A,  # ERR_TCPSEND  TCP 发送失败(路由中断/网线断)
        0x1B,  # ERR_HOSTUNREACHABLE  主机不可达
        0x1D,  # ERR_TLSSEND  安全 ADS 连接建立失败
    }
) | frozenset(range(0x0500, 0x050E))
"""transport 类 ADS 错误码(TE1000 §8「ADS Return Codes」p.128-129):链路/路由/
服务不可用,出现即标记断线走惰性重连。

- 全局组 ``0x06/0x07/0x0D/0x12/0x1A/0x1B/0x1D``:目标端口未找到 / 目标机器
  未找到 / 端口未连接 / 端口禁用 / **TCP 发送失败(0x1A ERR_TCPSEND)** /
  主机不可达 / TLS 发送失败。
- Router 组 ``0x0500~0x050D``:本机 AMS 路由器侧错误。

注意 ``0x0705``(参数尺寸错)/``0x0706``(数据非法)/``0x0725``(许可过期)
属**设备语义错误**,不得归入此类——否则普通参数错误会误判断线重连。
"""


def _translate_ads_error(exc: BaseException) -> OmniPLCInternalError:
    """把 pyads 异常翻译为本库内部异常(内部函数)。

    - ``ADSError`` 且错误码属 transport 类(:data:`_ADS_TRANSPORT_ERROR_CODES`,
      TwinCAT 重启/路由器断开/TCP 发送失败等)→ :class:`TransportClosedError`
      (OmniPLCInternalError 子类,基类归 **TRANSPORT** 分类并标记断线、
      下次事务惰性重连)
    - 其余 ``ADSError``(符号不存在/长度不符等设备语义错误)→
      :class:`DeviceError`,``code`` 携带原始 ADS 错误码——链路是好的,
      不断线不重试
    - pyads 缺失/连接中断/内部错误 → :class:`OmniPLCInternalError`
    """
    pyads = _load_pyads()
    error_class = getattr(pyads, "ADSError", None) if pyads is not None else None
    if error_class is not None and isinstance(exc, error_class):
        try:
            code = int(getattr(exc, "err_code", 0) or 0)
        except (TypeError, ValueError):
            # err_code 非数值(pyads 版本差异/绑定变化)按 0 兜底:翻译函数
            # 自身绝不能抛,否则 TypeError/ValueError 从 _execute 的
            # OSError/OmniPLCInternalError/DeviceError 三分支之外逃逸
            # (review-1005 P1)
            code = 0
        if code in _ADS_TRANSPORT_ERROR_CODES:
            return TransportClosedError(
                _("ADS 连接失效 0x{:08X}:{}(下次事务将重连)").format(code, exc)
            )
        return DeviceError(_("ADS 出错 0x{:08X}:{}").format(code, exc), code)
    return OmniPLCInternalError(
        _("ADS 调用失败:{}:{}").format(type(exc).__name__, exc)
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
                _("pyads 加载失败(ADS 走线需 pip install omniplc[ads];"
                "Windows 还需 Beckhoff TcAdsDll 运行库)")
            )
        try:
            connection = pyads.Connection(self._net_id, self._ads_port)
        except Exception as exc:
            raise OSError(_("ADS 连接对象创建失败:{}").format(exc)) from exc
        try:
            connection.open()
        except OSError:
            _safe_close(connection)
            raise
        except Exception as exc:
            _safe_close(connection)
            raise OSError(
                _("ADS 连接失败:{}({})").format(type(exc).__name__, exc)
            ) from exc
        self._connection = connection
        try:
            connection.set_timeout(int(self._receive_timeout * 1000))
        except Exception as exc:
            log_warning(
                self._debug_label,
                "set_timeout 下发异常(该路由/固件可能不支持,按 pyads 默认超时):%s",
                exc,
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
        raise TransportClosedError(_("ADS 走会话通道,无字节流收发"))

    def recv(self, size: int) -> bytes:
        """ADS 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError(_("ADS 走会话通道,无字节流收发"))

    @property
    def connection(self) -> Any:
        """当前 pyads Connection(仅连接成功后可用,内部属性)。"""
        if self._connection is None:
            raise TransportClosedError(_("ADS 连接未建立"))
        return self._connection

    def read_by_name(self, address: str, plctype_name: str) -> Any:
        """按变量名读值(会话调用,pyads 异常在此翻译)。"""
        pyads = _load_pyads()
        if pyads is None:
            raise OmniPLCInternalError(_("pyads 加载失败,无法读取 ADS 变量"))
        plctype = getattr(pyads, plctype_name, None)
        if plctype is None:
            raise OmniPLCInternalError(_("ADS 类型解析失败:{}").format(plctype_name))
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
            raise OmniPLCInternalError(_("pyads 加载失败,无法写入 ADS 变量"))
        plctype = getattr(pyads, plctype_name, None)
        if plctype is None:
            raise OmniPLCInternalError(_("ADS 类型解析失败:{}").format(plctype_name))
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

    def read_state(self) -> Tuple[int, int]:
        """读 ADS 状态与设备状态(pyads read_state,会话调用,异常在此翻译)。

        返回 ``(adsState, deviceState)``,取值见 Beckhoff ADS 规范
        (5 = RUN、7 = STOP 等);零副作用,同时是探活探测命令。
        """
        try:
            state = self.connection.read_state()
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ads_error(exc) from exc
        if state is None:
            raise OmniPLCInternalError(_("ADS 状态读取返回空(pyads 会话未就绪)"))
        ads_state, device_state = state
        log_op(self._debug_label, "状态读 → ADS=%d 设备=%d", ads_state, device_state)
        return int(ads_state), int(device_state)


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

    # 探活:pyads read_state(ADS 状态读,零副作用系统级读,见 _ping_probe)
    _has_ping = True

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

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒)。"""
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒):存储并对**已建立会话**重发 pyads set_timeout。

        ADS 为会话型走线(pyads 连接对象持有 socket 超时),基类只写
        传输属性不动 pyads——此处补重发,会话期修改即时生效(第八轮 P2-5)。
        未连接时仅存储,connect 时统一下发。
        """
        if seconds <= 0:
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
        with self._lock:
            self._receive_timeout = float(seconds)
            transport = self._transport
            if transport is None:
                return
            transport.receive_timeout = self._receive_timeout
            connection = getattr(transport, "_connection", None)
            if connection is not None:
                try:
                    connection.set_timeout(int(self._receive_timeout * 1000))
                except Exception as exc:
                    log_warning(
                        "ads://",
                        "set_timeout 重发异常(按 pyads 当前超时继续):%s",
                        exc,
                    )

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _AdsSession:
        """取当前会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _AdsSession):
            raise TransportClosedError(_("内部错误:传输对象不是 ADS 会话"))
        return link

    # ------------------------------------------------------------------
    # 状态读与探活(pyads read_state)
    # ------------------------------------------------------------------

    def read_state(self) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """读 ADS 状态与设备状态,返回 ``(是否成功, (adsState, deviceState))``。

        取值见 Beckhoff ADS 规范(5 = RUN、7 = STOP 等);零副作用,
        同时是 :meth:`ping` 的探测命令。
        """
        return self._execute(lambda: self._session().read_state())

    def _ping_probe(self) -> Tuple[int, int]:
        """探活探测命令:ADS 状态读(内部方法)。"""
        return self._session().read_state()

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读变量值并按数据类型收窄。"""
        if data_type not in _PLCTYPE_NAMES:
            raise ValueError(_("ADS 不支持的数据类型:{}").format(data_type))
        text = _check_address(address)
        value = self._session().read_by_name(text, _PLCTYPE_NAMES[data_type])
        return _coerce_read(value, data_type, text)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型对应的 PLCTYPE 写变量值。

        STRING 走 :meth:`_write_string`(含 PLC 侧声明长度预检,防溢出污染
        相邻变量);其余类型按 PLCTYPE 编码并先做范围校验。
        """
        if data_type not in _PLCTYPE_NAMES:
            raise ValueError(_("ADS 不支持的数据类型:{}").format(data_type))
        if data_type is DataType.STRING:
            if not isinstance(value, str):
                raise ValueError(
                    _("字符串必须是 str,收到:{}").format(type(value).__name__)
                )
            self._write_string(address, value, "utf-8")
            return
        text = _check_address(address)
        coerced = _coerce_write(value, data_type)
        self._session().write_by_name(text, coerced, _PLCTYPE_NAMES[data_type])

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串变量(编码由 pyads 固定,length 仅库侧再截断)。

        pyads 读缓冲 ``STRING_BUFFER = 1024`` 字节(2026-09-30 订正:原
        docstring 误称"按 STRING(80) 静默截断到 80 字符"——pyads 3.5.1
        常量即 1024,``STRING(120)`` 等可完整读回);>1023 字符才会截。
        ``length`` 是本库读出后的再截断,不影响传输。
        """
        if encoding.lower().replace("-", "").replace("_", "") not in ("utf8", "u8", "ascii"):
            # pyads 读串固定按 UTF-8 解码:显式多字节编码(gb2312 等)拒绝,
            # 防"传 gb2312 静默按 utf-8 解码"的编码错配;ascii 为基类缺省且
            # ASCII ⊂ UTF-8(纯 ASCII 内容无错配),放行保兼容(review-1005 P1)
            raise ValueError(
                _("ADS STRING 编码固定 utf-8,收到:{}").format(encoding)
            )
        text = _check_address(address)
        value = self._session().read_by_name(text, _PLCTYPE_NAMES[DataType.STRING])
        if not isinstance(value, str):
            raise ValueError(
                _("ADS 变量返回类型不符(期望字符串):{} ← {!r}").format(text, value)
            )
        return value[:length]

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串变量(写前按 PLC 侧声明长度预检,防溢出污染相邻变量)。

        pyads 按 **UTF-8 字节**编码后写入,而声明长度按 **字符数** 口径——
        预检必须同样按字节数比较,否则 80 个汉字(240 字节)会以
        ``len=80 <= 80`` 绕过检查,溢出污染相邻变量。``encoding`` 仅接受
        utf-8 家族与基类缺省 ``ascii``(ASCII ⊂ UTF-8,无错配),其余
        ValueError(读侧同口径)。
        """
        if not isinstance(value, str):
            raise ValueError(_("字符串必须是 str,收到:{}").format(type(value).__name__))
        if encoding.lower().replace("-", "").replace("_", "") not in ("utf8", "u8", "ascii"):
            # pyads 写串固定按 UTF-8 编码:显式多字节编码(gb2312 等)拒绝,
            # 防"传 gb2312 静默按 utf-8 落盘";ascii 为基类缺省且 ASCII ⊂
            # UTF-8,放行保兼容(review-1005 P1)
            raise ValueError(
                _("ADS STRING 编码固定 utf-8,收到:{}").format(encoding)
            )
        text = _check_address(address)
        declared = _declared_string_chars(self._session().symbol_type(text))
        byte_len = len(value.encode("utf-8"))
        if declared is not None and byte_len > declared:
            raise ValueError(
                _("ADS STRING 写入值超 PLC 侧声明长度(按 UTF-8 字节计):{} 字节 > {} 字符({!r})"
                ";若含非 ASCII 字符请按字节预算或改用 ASCII 内容").format(
                    byte_len, declared, text
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

    显式给出时校验 6 段 0~255 数字;缺省由 IP 拼 ``.1.1`` 后缀——自动
    拼装要求 ``ip_address`` 为 **IPv4 字面量**(ADS 连接经本机 AMS 路由器
    按 NetId 路由,主机名不参与解析,拼出 ``"plc01.1.1"`` 必然是死路由),
    主机名目标请显式传 ``net_id``。

    :raises ValueError: NetId 格式非法 / 自动拼装时 IP 非 IPv4 字面量
    """
    text = net_id.strip() if net_id else ""
    if not text:
        host = ip_address.strip()
        if not _is_dotted_number(host, 4):
            raise ValueError(
                _("ADS 自动 NetId 需 IPv4 字面量 IP:{!r}——ADS 经本机 AMS 路由器按 "
                "NetId 路由(主机名不解析),请改用 IP 或显式传 net_id").format(host)
            )
        return "{}{}".format(host, ADS_NET_ID_SUFFIX)
    if not _is_dotted_number(text, 6):
        raise ValueError(
            _("AMS NetId 非法(应为 6 段 0~255 数字):{!r}(示例:192.168.0.10.1.1)").format(
                net_id
            )
        )
    return text


def _is_dotted_number(text: str, count: int) -> bool:
    """校验 ``count`` 段 0~255 十进制数字的 IP/NetId 字面量(内部函数)。"""
    parts = text.split(".")
    return len(parts) == count and all(
        part.isdigit() and 0 <= int(part) <= 255 for part in parts
    )


def _check_address(address: str) -> str:
    """变量名非空校验,返回去首尾空白的原文(内部函数)。

    :raises ValueError: 变量名为空
    """
    text = address.strip() if isinstance(address, str) else ""
    if not text:
        raise ValueError(_("ADS 变量名不能为空"))
    return text


def _coerce_read(value: Any, data_type: DataType, address: str) -> PrimitiveValue:
    """把 ADS 返回值收窄为本库基础类型(内部函数)。

    :raises ValueError: 返回值类型与目标数据类型不符(调用方参数错误,
        与 OPC-UA"返回类型不符"同口径,直接抛出,不断线)
    """
    if value is None:
        raise DeviceError(_("ADS 变量值为空:{}").format(address), 0)
    if data_type is DataType.BOOL:
        if not isinstance(value, bool):
            raise ValueError(
                _("ADS 变量返回类型不符(期望布尔):{} ← {!r}").format(address, value)
            )
        return value
    if data_type in (DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT,
                     DataType.LONG, DataType.ULONG):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                _("ADS 变量返回类型不符(期望整数):{} ← {!r}").format(address, value)
            )
        return value
    if data_type in (DataType.FLOAT, DataType.DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                _("ADS 变量返回类型不符(期望数值):{} ← {!r}").format(address, value)
            )
        return float(value)
    if not isinstance(value, str):
        raise ValueError(
            _("ADS 变量返回类型不符(期望字符串):{} ← {!r}").format(address, value)
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
            raise ValueError(_("float 超出 float32 范围:{}").format(value)) from exc
        return number_f
    if data_type is DataType.DOUBLE:
        return require_float(value)
    if not isinstance(value, str):
        raise ValueError(_("字符串必须是 str,收到:{}").format(type(value).__name__))
    return value
