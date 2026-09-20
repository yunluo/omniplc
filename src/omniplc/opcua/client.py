"""OPC-UA 客户端(封装 asyncua,opc.tcp 会话)。

OPC-UA 是完整规范栈(二进制编码、会话/订阅、X.509 安全栈),
**不自研协议**,封装成熟库 `asyncua`(python-opcua 的官方继任者;
1.1.5 为最后支持 Python 3.7 的版本)。本驱动只做两件事:
NodeId 寻址 + 读写值映射到本库的统一契约
(读 ``(bool, value)``、写 ``bool``、失败进 :attr:`last_error`)。

类继承::

    BaseClient
    └── OpcUaClient   opc.tcp 会话(默认端口 4840;会话适配见 _OpcUaSession)

数据类型映射(DataType → OPC-UA VariantType):
BOOL→Boolean、SHORT→Int16、USHORT→UInt16、INT→Int32、UINT→UInt32、
LONG→Int64、ULONG→UInt64、FLOAT→Float、DOUBLE→Double、STRING→String。
读写均走服务端原生类型编解码,无字序/字节序问题。

v0.8 范围:匿名/NoSecurity 连接下的节点读写;安全策略配置、
订阅/浏览(不符合本库拉模式)留后续版本。
"""
from __future__ import annotations

import re
import struct
from typing import Any, Tuple

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import OPCUA_DEFAULT_PORT
from ..core.errors import DeviceError, OmniPLCInternalError, TransportClosedError
from ..core.validation import require_bool, require_float, require_int
from ..types import DataType, PrimitiveValue
from ..transport.base import BaseTransport
from .address import parse_opcua_nodeid

_VARIANT_TYPE_NAMES = {
    DataType.BOOL: "Boolean",
    DataType.SHORT: "Int16",
    DataType.USHORT: "UInt16",
    DataType.INT: "Int32",
    DataType.UINT: "UInt32",
    DataType.LONG: "Int64",
    DataType.ULONG: "UInt64",
    DataType.FLOAT: "Float",
    DataType.DOUBLE: "Double",
    DataType.STRING: "String",
}
"""DataType → asyncua ``ua.VariantType`` 成员名(惰性解析,保持核心零导入)。"""

_ENDPOINT_RE = re.compile(r"^opc\.tcp://([^/\s:]+|\[[0-9A-Fa-f:]+\])(?::(\d{1,5}))?(?:/.*)?$", re.IGNORECASE)
"""opc.tcp 端点 URL:主机(IPv4/IPv6 字面量/主机名)+ 可选端口 + 可选路径。"""


def _translate_ua_error(exc: BaseException) -> OmniPLCInternalError:
    """把 asyncua 异常翻译为本库内部异常(内部函数)。

    - ``UaError``(服务端返回 Bad 状态码)→ :class:`DeviceError`,
      ``code`` 携带原始 StatusCode——链路是好的,不断线不重试
    - 其余(asyncua 内部/超时/会话中断)→ :class:`OmniPLCInternalError`,
      由基类标记断走惰性重连
    """
    try:
        from asyncua.ua import uaerrors
    except ImportError:
        return OmniPLCInternalError("OPC-UA 调用失败:{}".format(exc))
    if isinstance(exc, uaerrors.UaError):
        code = int(getattr(exc, "code", 0) or 0) & 0xFFFFFFFF
        return DeviceError(
            "OPC-UA 出错 0x{:08X}:{}".format(code, exc),
            code,
        )
    return OmniPLCInternalError(
        "OPC-UA 调用失败:{}:{}".format(type(exc).__name__, exc)
    )


class _OpcUaSession(BaseTransport):
    """OPC-UA 会话适配器:asyncua 会话适配为传输对象外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 建立
    opc.tcp 会话,``close`` 断开;OPC-UA 无字节流收发,读写经
    :meth:`read_value` / :meth:`write_value` 会话方法完成,asyncua
    异常在此边界统一翻译。

    :ivar client: asyncua.sync.Client 实例(仅连接成功后可用)
    """

    def __init__(self, endpoint: str) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._client: Any = None

    def connect(self) -> None:
        """建立 opc.tcp 会话(每次连接新建 asyncua 客户端与后台事件循环)。

        :raises OSError: 会话创建/连接失败(asyncua 未安装也归入此类)
        """
        import asyncua.sync

        try:
            client = asyncua.sync.Client(self._endpoint, timeout=self._receive_timeout)
        except Exception as exc:
            raise OSError("OPC-UA 会话创建失败:{}(请确认已 pip install omniplc[opcua])".format(exc))
        try:
            client.connect()
        except OSError:
            _safe_disconnect(client)
            raise
        except Exception as exc:
            _safe_disconnect(client)
            raise OSError(
                "OPC-UA 连接失败:{}({})".format(type(exc).__name__, exc)
            )
        self._client = client

    def close(self) -> None:
        """断开 opc.tcp 会话,幂等。"""
        client, self._client = self._client, None
        if client is None:
            return
        _safe_disconnect(client)

    def send(self, data: bytes) -> None:
        """OPC-UA 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("OPC-UA 走会话通道,无字节流收发")

    def recv(self, size: int) -> bytes:
        """OPC-UA 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("OPC-UA 走会话通道,无字节流收发")

    @property
    def client(self) -> Any:
        """当前 asyncua 同步客户端(仅连接成功后可用,内部属性)。"""
        if self._client is None:
            raise TransportClosedError("OPC-UA 会话未建立")
        return self._client

    def read_value(self, node_text: str) -> Any:
        """读节点当前值(会话调用,asyncua 异常在此翻译)。"""
        try:
            return self.client.get_node(node_text).read_value()
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ua_error(exc) from exc

    def write_value(self, node_text: str, value: Any, variant_name: str) -> None:
        """按 VariantType 写节点值(会话调用,asyncua 异常在此翻译)。"""
        import asyncua.ua

        try:
            variant_type = getattr(asyncua.ua.VariantType, variant_name)
        except Exception:
            raise OmniPLCInternalError("OPC-UA VariantType 解析失败:{}".format(variant_name))
        try:
            self.client.get_node(node_text).write_value(value, variant_type)
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ua_error(exc) from exc


def _safe_disconnect(client: Any) -> None:
    """尽力断开会话(回收后台事件循环线程),静默失败(内部函数)。"""
    try:
        client.disconnect()
    except Exception:
        pass


class OpcUaClient(BaseClient):
    """OPC-UA 客户端(封装 asyncua,opc.tcp 会话,默认端口 4840)。

    地址为标准 NodeId 字符串(``ns=2;s=Device.Tag``/``i=2258``,
    见 :mod:`omniplc.opcua.address`)。数据类型显式指定
    (推荐 :class:`~omniplc.types.DataType` 枚举),写入按对应
    VariantType 编码,读取按该类型校验返回值。

    构造入口与其他客户端一致(IP + 端口);OPC-UA 端点 URL 由此组装,
    ``path`` 对应 URL 路径(如 ``"UA/Server"``);服务器发现得到的
    完整 URL 可用 ``endpoint`` 显式覆盖(高级用法)。

    :example::

        client = OpcUaClient("192.168.0.10", 4840)
        client.connect()
        ok, value = client.read_float("ns=2;s=Device.Temperature")
        ok = client.write_ushort("ns=2;s=Device.Speed", 1200)
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = OPCUA_DEFAULT_PORT,
        path: str = "",
        endpoint: str = "",
    ) -> None:
        """初始化 OPC-UA 客户端。

        :param ip_address: 服务器 IP 或主机名
        :param port: 端口,标准默认 4840
        :param path: 端点 URL 路径(可空,如 ``"UA/Server"``)
        :param endpoint: 完整端点 URL 显式覆盖(以 ``opc.tcp://`` 开头;
            用于服务器发现返回的完整 URL,设置后忽略 ip/port/path)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if endpoint:
            _validate_endpoint_url(endpoint)
            self._endpoint = endpoint.strip()
        else:
            self._endpoint = _build_endpoint(ip_address, int(port), path)

    @property
    def endpoint(self) -> str:
        """opc.tcp 端点 URL(由 ip_address/port/path 组装或显式覆盖)。"""
        return self._endpoint

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _OpcUaSession:
        """取当前会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _OpcUaSession):
            raise TransportClosedError("内部错误:传输对象不是 OPC-UA 会话")
        return link

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读节点值并按数据类型校验/收窄。"""
        if data_type not in _VARIANT_TYPE_NAMES:
            raise ValueError("OPC-UA 不支持的数据类型:{}".format(data_type))
        parsed = parse_opcua_nodeid(address)
        value = self._session().read_value(parsed.text)
        return _coerce_read(value, data_type, address)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型对应的 VariantType 写节点值。"""
        if data_type not in _VARIANT_TYPE_NAMES:
            raise ValueError("OPC-UA 不支持的数据类型:{}".format(data_type))
        parsed = parse_opcua_nodeid(address)
        coerced, variant_name = _coerce_write(value, data_type)
        self._session().write_value(parsed.text, coerced, variant_name)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读字符串节点(OPC-UA 字符串为变长 Unicode,length/encoding 不适用)。"""
        parsed = parse_opcua_nodeid(address)
        value = self._session().read_value(parsed.text)
        coerced = _coerce_read(value, DataType.STRING, address)
        return str(coerced)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写字符串节点(OPC-UA 字符串为变长 Unicode,按 String 编码)。"""
        if not isinstance(value, str):
            raise ValueError("字符串必须是 str,收到:{}".format(type(value).__name__))
        parsed = parse_opcua_nodeid(address)
        self._session().write_value(parsed.text, value, _VARIANT_TYPE_NAMES[DataType.STRING])
        return value

    def _create_transport(self) -> BaseTransport:
        return _OpcUaSession(self._endpoint)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _build_endpoint(ip_address: str, port: int, path: str) -> str:
    """由 IP/端口/路径组装 opc.tcp 端点 URL(内部函数)。

    :raises ValueError: 组装结果非法
    """
    cleaned = path.strip().strip("/")
    url = "opc.tcp://{}:{}{}".format(ip_address.strip(), port, "/{}".format(cleaned) if cleaned else "")
    _validate_endpoint_url(url)
    return url


def _validate_endpoint_url(endpoint: str) -> None:
    """校验 opc.tcp 端点 URL(内部函数)。

    :raises ValueError: 为空、缺 ``opc.tcp://`` 前缀、主机为空或端口越界
    """
    if not endpoint or not endpoint.strip():
        raise ValueError("endpoint 不能为空")
    text = endpoint.strip()
    match = _ENDPOINT_RE.match(text)
    if match is None:
        raise ValueError(
            "OPC-UA 端点 URL 非法:{!r}(示例:opc.tcp://192.168.0.10:{})".format(
                endpoint, OPCUA_DEFAULT_PORT
            )
        )
    port_text = match.group(2)
    if port_text is not None and not 1 <= int(port_text) <= 65535:
        raise ValueError("OPC-UA 端点端口必须在 1~65535 之间,收到:{}".format(port_text))


def _coerce_read(value: Any, data_type: DataType, address: str) -> PrimitiveValue:
    """把服务端返回值收窄为本库基础类型(内部函数)。

    :raises OmniPLCInternalError: 返回值类型与目标数据类型不符
        (转 ``(False, None)`` + ``last_error``,标记断开待惰性重连)
    """
    if value is None:
        raise OmniPLCInternalError("OPC-UA 节点值为空:{}".format(address))
    if data_type is DataType.BOOL:
        if not isinstance(value, bool):
            raise OmniPLCInternalError(
                "OPC-UA 节点返回类型不符(期望布尔):{} ← {!r}".format(address, value)
            )
        return value
    if data_type in (DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT,
                     DataType.LONG, DataType.ULONG):
        if isinstance(value, bool) or not isinstance(value, int):
            raise OmniPLCInternalError(
                "OPC-UA 节点返回类型不符(期望整数):{} ← {!r}".format(address, value)
            )
        return value
    if data_type in (DataType.FLOAT, DataType.DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OmniPLCInternalError(
                "OPC-UA 节点返回类型不符(期望数值):{} ← {!r}".format(address, value)
            )
        return float(value)
    if not isinstance(value, str):
        raise OmniPLCInternalError(
            "OPC-UA 节点返回类型不符(期望字符串):{} ← {!r}".format(address, value)
        )
    return value


def _coerce_write(value: PrimitiveValue, data_type: DataType) -> Tuple[Any, str]:
    """校验写入值并给出 VariantType 成员名(内部函数)。

    :return: ``(编码值, VariantType 成员名)``
    :raises ValueError: 值类型或范围非法(参数错误约定,直接抛出)
    """
    variant_name = _VARIANT_TYPE_NAMES[data_type]
    if data_type is DataType.BOOL:
        return require_bool(value), variant_name
    if data_type in (DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT):
        number = require_int(value)
        _require_int_range(number, data_type)
        return number, variant_name
    if data_type in (DataType.LONG, DataType.ULONG):
        number = require_int(value)
        _require_int_range(number, data_type)
        return number, variant_name
    if data_type is DataType.FLOAT:
        number_f = require_float(value)
        try:
            struct.pack("<f", number_f)
        except (OverflowError, ValueError) as exc:
            raise ValueError("float 超出 float32 范围:{}".format(value)) from exc
        return number_f, variant_name
    if data_type is DataType.DOUBLE:
        return require_float(value), variant_name
    if not isinstance(value, str):
        raise ValueError("字符串必须是 str,收到:{}".format(type(value).__name__))
    return value, variant_name


_INT_RANGES = {
    DataType.SHORT: (-0x8000, 0x7FFF),
    DataType.USHORT: (0, 0xFFFF),
    DataType.INT: (-0x80000000, 0x7FFFFFFF),
    DataType.UINT: (0, 0xFFFFFFFF),
    DataType.LONG: (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    DataType.ULONG: (0, 0xFFFFFFFFFFFFFFFF),
}
"""整数DataType → (下限, 上限)。"""


def _require_int_range(number: int, data_type: DataType) -> None:
    """整数范围校验(内部函数)。"""
    low, high = _INT_RANGES[data_type]
    if not low <= number <= high:
        raise ValueError("{} 超出范围 {}~{}:{}".format(data_type.name, low, high, number))
