"""OPC-UA 客户端(封装 asyncua,opc.tcp 会话)。

OPC-UA 是完整规范栈(二进制编码、会话/订阅、X.509 安全栈),
**不自研协议**,封装成熟库 `asyncua`(python-opcua 的官方继任者;
1.1.5 为最后支持 Python 3.7 的版本)。本驱动在统一契约
(读 ``(bool, value)``、写 ``bool``、失败进 :attr:`last_error`)上做四件事:

- NodeId 寻址 + 读写值映射
- :meth:`OpcUaClient.browse` 递归枚举节点树
- :meth:`OpcUaClient.subscribe_data_change` 数据变化订阅(DataChange)
- :meth:`OpcUaClient.subscribe_event` 事件订阅(Event)

类继承::

    BaseClient
    └── OpcUaClient   opc.tcp 会话(默认端口 4840;会话适配见 _OpcUaSession)

数据类型映射(DataType → OPC-UA VariantType):
BOOL→Boolean、SHORT→Int16、USHORT→UInt16、INT→Int32、UINT→UInt32、
LONG→Int64、ULONG→UInt64、FLOAT→Float、DOUBLE→Double、STRING→String。
读写均走服务端原生类型编解码,无字序/字节序问题。

v0.8 范围:匿名/NoSecurity 连接下的节点读写;v0.35 新增 Browse +
订阅(数据变化 + 事件)。安全策略配置 / 聚合采样订阅留后续。
"""
from __future__ import annotations

import logging
import re
import struct
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    INT16_MAX,
    INT16_MIN,
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    OPCUA_DEFAULT_PORT,
    OPCUA_DEFAULT_SAMPLING_INTERVAL_MS,
    PORT_MAX,
    PORT_MIN,
    UINT16_MAX,
    UINT32_MAX,
    UINT64_MAX,
)
from ..core.debug import log_op
from ..core.errors import DeviceError, ErrorCategory, OmniPLCInternalError, TransportClosedError
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
        return OmniPLCInternalError(f"OPC-UA 调用失败:{exc}")
    if isinstance(exc, uaerrors.UaError):
        code = int(getattr(exc, "code", 0) or 0) & 0xFFFFFFFF
        return DeviceError(
            f"OPC-UA 出错 0x{code:08X}:{exc}",
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
        """OPC-UA 会话适配器。

        :param endpoint: opc.tcp 端点 URL(由 :class:`OpcUaClient` 组装或显式覆盖)
        """
        super().__init__()
        self._endpoint = endpoint
        self._client: Any = None
        self._debug_label = f"opcua://{endpoint}"

    def connect(self) -> None:
        """建立 opc.tcp 会话(每次连接新建 asyncua 客户端与后台事件循环)。

        :raises OSError: 会话创建/连接失败(asyncua 未安装也归入此类)
        """
        import asyncua.sync

        try:
            client = asyncua.sync.Client(self._endpoint, timeout=self._receive_timeout)
        except Exception as exc:
            raise OSError(f"OPC-UA 会话创建失败:{exc}(请确认已 pip install omniplc[opcua])")
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
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """断开 opc.tcp 会话,幂等。"""
        client, self._client = self._client, None
        if client is None:
            return
        _safe_disconnect(client)
        log_op(self._debug_label, "会话已断开")

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
            value = self.client.get_node(node_text).read_value()
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ua_error(exc) from exc
        log_op(self._debug_label, "读 %s → %r", node_text, value)
        return value

    def read_values(self, node_texts: List[str]) -> List[Any]:
        """批量读节点当前值(单次 Read 服务,asyncua 异常在此翻译)。"""
        try:
            values = self.client.read_values(
                [self.client.get_node(text) for text in node_texts]
            )
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ua_error(exc) from exc
        log_op(self._debug_label, "批量读 %d 节点", len(node_texts))
        return values

    def write_value(self, node_text: str, value: Any, variant_name: str) -> None:
        """按 VariantType 写节点值(会话调用,asyncua 异常在此翻译)。"""
        import asyncua.ua

        try:
            variant_type = getattr(asyncua.ua.VariantType, variant_name)
        except Exception:
            raise OmniPLCInternalError(f"OPC-UA VariantType 解析失败:{variant_name}")
        try:
            self.client.get_node(node_text).write_value(value, variant_type)
        except OSError:
            raise
        except Exception as exc:
            raise _translate_ua_error(exc) from exc
        log_op(self._debug_label, "写 %s ← %r(%s)", node_text, value, variant_name)


def _safe_disconnect(client: Any) -> None:
    """尽力断开会话(回收后台事件循环线程),静默失败(内部函数)。"""
    try:
        client.disconnect()
    except Exception:
        pass


# ----------------------------------------------------------------------
# 订阅句柄与回调桥(v0.35 新增)
# ----------------------------------------------------------------------

_UA_LOGGER = logging.getLogger("omniplc.opcua")
"""异步回调内出错日志出口(用户回调异常不杀订阅,记日志 + last_error)。"""


class _DataChangeHandler:
    """asyncua DataChange 回调适配器:把内部通知翻译为 ``(value, node_id, ts)`` 三元组。

    asyncua 期望 handler 是有 ``datachange_notification`` 方法的对象;
    我们的公共 API 是 ``Callable[[Any, str, Optional[float]], None]``,
    故此处包一层。**用户回调异常被吞掉**(log + 写 last_error),不杀订阅。
    """

    def __init__(
        self,
        client: "OpcUaClient",
        on_change: Callable[[Any, str, Optional[float]], None],
    ) -> None:
        self._client = client
        self._on_change = on_change

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        try:
            ts = None
            try:
                src = data.monitored_item.Value.SourceTimestamp
                # asyncua DateTime 支持 Python int/float 直接;若返回 datetime 再调 timestamp()
                if hasattr(src, "timestamp"):
                    ts = float(src.timestamp())
                elif src is not None:
                    ts = float(src)
            except Exception:
                ts = None
            self._on_change(val, str(node), ts)
        except Exception as exc:
            # 用户回调异常:不杀订阅,走 _set_error 三件套(category=UNKNOWN)
            try:
                self._client._set_error(  # noqa: SLF001
                    "OPC-UA DataChange 回调异常:{}:{}".format(
                        type(exc).__name__, exc
                    ),
                    ErrorCategory.UNKNOWN,
                    None,
                )
            except Exception:
                pass
            _UA_LOGGER.exception("OPC-UA DataChange 回调异常")

    def status_change_notification(self, status: Any) -> None:
        """连接断开时 asyncua 逐订阅通知状态(静默:资源由 disconnect 统一清理)。"""
        _UA_LOGGER.debug("OPC-UA 订阅状态变化:%s", status)


class _EventHandler:
    """asyncua Event 回调适配器:把内部通知翻译为 ``(fields_dict, node_id, ts)``。"""

    def __init__(
        self,
        client: "OpcUaClient",
        on_event: Callable[[dict, str, Optional[float]], None],
    ) -> None:
        self._client = client
        self._on_event = on_event

    def event(self, event: Any) -> None:
        try:
            # asyncua event 对象:dict-like via _fields;安全兜底 dict()
            try:
                fields = {key: event[key] for key in event.keys()}
            except Exception:
                fields = {"raw": str(event)}
            node_id = ""
            try:
                node_id = str(event.SourceNode)
            except Exception:
                pass
            ts = None
            try:
                src = event.Time
                if hasattr(src, "timestamp"):
                    ts = float(src.timestamp())
                elif src is not None:
                    ts = float(src)
            except Exception:
                ts = None
            self._on_event(fields, node_id, ts)
        except Exception as exc:
            try:
                self._client._set_error(  # noqa: SLF001
                    "OPC-UA Event 回调异常:{}:{}".format(
                        type(exc).__name__, exc
                    ),
                    ErrorCategory.UNKNOWN,
                    None,
                )
            except Exception:
                pass
            _UA_LOGGER.exception("OPC-UA Event 回调异常")

    def status_change_notification(self, status: Any) -> None:
        """连接断开时 asyncua 逐订阅通知状态(静默:资源由 disconnect 统一清理)。"""
        _UA_LOGGER.debug("OPC-UA 订阅状态变化:%s", status)


class OpcUaSubscription:
    """OPC-UA 订阅句柄(由 :meth:`OpcUaClient.subscribe_data_change` /
    :meth:`OpcUaClient.subscribe_event` 返回,v0.35 新增)。

    唯一公开方法 :meth:`unsubscribe` —— **幂等**,线程安全,失败返回 False
    (已断开 / 已取消)。订阅是 transient 状态:客户端断开后所有未显式
    unsubscribe 的句柄自动失效;不提供重连后自动重订(简化生命周期)。
    """

    def __init__(
        self,
        *,
        node_id: str,
        subscription_id: int,
        unsub: Callable[[], bool],
        _asyncua_subscription: Any,
        _monitored_items: List[Any],
    ) -> None:
        self._node_id = node_id
        self._subscription_id = subscription_id
        self._unsub = unsub
        self._asyncua_subscription = _asyncua_subscription
        self._monitored_items = _monitored_items
        self._unsub_done = False

    @property
    def node_id(self) -> str:
        """订阅的 NodeId 字符串(订阅时传入)。"""
        return self._node_id

    @property
    def subscription_id(self) -> int:
        """asyncua 内部 Subscription id(debug / trace 用)。"""
        return self._subscription_id

    def unsubscribe(self) -> bool:
        """取消订阅(幂等)。

        :return: ``True`` 此次调用真正执行了取消;``False`` 已取消 / 已断开。
        """
        if self._unsub_done:
            return False
        self._unsub_done = True
        return self._unsub()


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
        # 活跃订阅句柄(由 subscribe_* 加入;disconnect 清空)
        self._active_subscriptions: Dict[int, OpcUaSubscription] = {}

    @property
    def endpoint(self) -> str:
        """opc.tcp 端点 URL(由 ip_address/port/path 组装或显式覆盖)。"""
        return self._endpoint

    @property
    def active_subscriptions(self) -> Dict[int, OpcUaSubscription]:
        """活跃订阅快照(``subscription_id`` → :class:`OpcUaSubscription`)。只读。"""
        with self._lock:
            return dict(self._active_subscriptions)

    def disconnect(self) -> bool:
        """断开 opc.tcp 会话;同时清空所有活跃订阅(幂等)。

        重写基类:在父类释放传输之前先清订阅,服务端 MonitoredItem
        与 Subscription 立即释放;句柄 mark 为 unsub_done,后续再调
        ``unsubscribe()`` 静默返回 False。
        """
        with self._lock:
            for handle in list(self._active_subscriptions.values()):
                try:
                    handle.unsubscribe()
                except Exception:
                    pass
            self._active_subscriptions.clear()
        return super().disconnect()

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
            raise ValueError(f"OPC-UA 不支持的数据类型:{data_type}")
        parsed = parse_opcua_nodeid(address)
        value = self._session().read_value(parsed.text)
        return _coerce_read(value, data_type, address)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """按数据类型对应的 VariantType 写节点值。"""
        if data_type not in _VARIANT_TYPE_NAMES:
            raise ValueError(f"OPC-UA 不支持的数据类型:{data_type}")
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

    # ------------------------------------------------------------------
    # 批量读取(UA Read 服务原生多节点,单请求)
    # ------------------------------------------------------------------

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取:覆写为 UA Read 服务单请求。

        与基类逐点独立容错不同:任一节点非法或服务端拒绝则**整批失败**
        (原因见 :attr:`last_error`);需要逐点容错请逐点调用 :meth:`read`。
        """
        data_type_enum = DataType.coerce(data_type)
        ok, values = self.read_batch([(address, data_type_enum) for address in addresses])
        if not ok or values is None:
            return [(False, None) for _ in addresses]
        return [(True, value) for value in values]

    def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多节点批量读取:一次 UA Read 服务读回全部节点值。

        OPC-UA Read 服务原生支持一次携带多个 NodeId,``read_many``/
        ``read_batch`` 均为单请求往返;任一节点 Bad 状态即整批失败
        (asyncua ``read_values`` 语义)。服务端对单请求节点数上限不一,
        超限时按服务端报错处理。字符串节点为变长 Unicode,
        ``length``/``encoding`` 参数不适用。

        :param items: ``(NodeId, 数据类型)`` 序列
        :return: ``(是否成功, 与 items 顺序对应的值列表)``
        :raises ValueError: 列表为空或数据类型非法
        """
        if not items:
            raise ValueError("read_batch 至少需要一个 (NodeId, 数据类型) 项")
        plan: List[Tuple[str, DataType]] = []
        for address, data_type in items:
            data_type_enum = DataType.coerce(data_type)
            if data_type_enum not in _VARIANT_TYPE_NAMES:
                raise ValueError(
                    f"OPC-UA 不支持的数据类型:{data_type_enum}"
                )
            plan.append((parse_opcua_nodeid(address).text, data_type_enum))

        def operation() -> List[PrimitiveValue]:
            values = self._session().read_values([text for text, _ in plan])
            return [
                _coerce_read(value, data_type, text)
                for (text, data_type), value in zip(plan, values)
            ]

        return self._execute(operation)

    def _create_transport(self) -> BaseTransport:
        return _OpcUaSession(self._endpoint)

    # ------------------------------------------------------------------
    # Browse(v0.35 新增)
    # ------------------------------------------------------------------

    def browse(
        self,
        node_text: str = "Root",
        *,
        recursive: bool = True,
        max_depth: Optional[int] = None,
    ) -> Tuple[bool, Optional[dict]]:
        """枚举节点树。

        :param node_text: 起始 NodeId 字符串(默认 ``"Root"`` 即服务端根节点)
        :param recursive: True 递归到叶子;False 仅顶层
        :param max_depth: 递归深度上限(``None`` = 无限制);防服务端巨大树爆栈
        :return: ``(成功, 嵌套 dict)``;嵌套结构::

            {node_id_str: {
                "browse_name": str,
                "node_class": str,           # "Object"/"Variable"/"Method"/...
                "children": dict | None,     # recursive=False 或到达 max_depth 时 None
            }}

        单个子节点无权限/超时 → 跳过该节点,**不中断**整树;若起始节点本身
        失败,整次返回 ``(False, None)``,错误三件套进 :attr:`last_error`。
        :raises ValueError: ``max_depth`` 为负
        """
        if max_depth is not None and max_depth < 0:
            raise ValueError(f"max_depth 不能为负,收到:{max_depth}")
        if not node_text:
            raise ValueError("node_text 不能为空")
        node_text_resolved = _resolve_browse_alias(node_text)

        def operation() -> dict:
            session = self._session()
            ua_client = session.client
            try:
                start_node = ua_client.get_node(node_text_resolved)
            except Exception as exc:
                raise _translate_ua_error(exc) from exc
            return self._browse_node(start_node, recursive, 0, max_depth)

        return self._execute(operation)

    def _browse_node(
        self,
        node: Any,
        recursive: bool,
        current_depth: int,
        max_depth: Optional[int],
    ) -> dict:
        """递归枚举单层;子节点失败跳过,不影响父级(内部方法)。"""
        try:
            children = node.get_children()
        except Exception:
            return {}
        out: Dict[str, dict] = {}
        for child in children:
            try:
                # asyncua.sync.SyncNode 的 get_* 是缓存(刚枚举无缓存值);
                # read_* 才是真的服务端读。browse_name 是 QualifiedName,取 .Name
                qn = child.read_browse_name()
                bn = str(getattr(qn, "Name", qn))
            except Exception:
                bn = ""
            try:
                nc = child.read_node_class()
                nc_name = nc.name if hasattr(nc, "name") else str(nc)
            except Exception:
                nc_name = "Unknown"
            entry: dict = {"browse_name": bn, "node_class": nc_name}
            if recursive and (max_depth is None or current_depth < max_depth):
                try:
                    entry["children"] = self._browse_node(
                        child, recursive, current_depth + 1, max_depth
                    )
                except Exception:
                    entry["children"] = {}  # 子层失败 → 空 dict,不挂外层
            else:
                entry["children"] = None
            out[str(child)] = entry
        return out

    # ------------------------------------------------------------------
    # Subscribe — DataChange(v0.35 新增)
    # ------------------------------------------------------------------

    def subscribe_data_change(
        self,
        node_text: str,
        on_change: Callable[[Any, str, Optional[float]], None],
        *,
        sampling_interval_ms: int = OPCUA_DEFAULT_SAMPLING_INTERVAL_MS,
    ) -> Tuple[bool, Optional[OpcUaSubscription]]:
        """订阅节点值变化(DataChange)。

        :param node_text: 节点 NodeId 字符串
        :param on_change: 回调签名 ``(value, node_id_str, source_timestamp)``;
            **回调异常被吞掉**(log + 写 last_error,category=UNKNOWN),
            **不杀订阅**
        :param sampling_interval_ms: 采样间隔(毫秒,默认 1000)
        :return: ``(成功, 订阅句柄)``;失败时 ``(False, None)``
        :raises ValueError: ``sampling_interval_ms <= 0``
        """
        if sampling_interval_ms <= 0:
            raise ValueError(
                f"sampling_interval_ms 必须大于 0,收到:{sampling_interval_ms}"
            )
        if not node_text:
            raise ValueError("node_text 不能为空")
        if not callable(on_change):
            raise ValueError(f"on_change 必须是可调用对象,收到:{type(on_change)!r}")

        def operation() -> OpcUaSubscription:
            session = self._session()
            ua_client = session.client
            handler = _DataChangeHandler(self, on_change)
            try:
                # asyncua 1.1.5:sync.Client 直接暴露 create_subscription(非 uaclient);
                # handler 在订阅级传入,subscribe_data_change 只收节点 + 采样间隔
                ua_sub = ua_client.create_subscription(
                    sampling_interval_ms / 1000.0, handler
                )
            except Exception as exc:
                raise _translate_ua_error(exc) from exc
            try:
                handles = ua_sub.subscribe_data_change(
                    [ua_client.get_node(parse_opcua_nodeid(node_text).text)],
                    sampling_interval=sampling_interval_ms / 1000.0,
                )
                monitored = list(handles) if isinstance(handles, (list, tuple)) else [handles]
            except Exception as exc:
                try:
                    ua_sub.delete()
                except Exception:
                    pass
                raise _translate_ua_error(exc) from exc
            sub_id = int(getattr(ua_sub, "subscription_id", id(ua_sub)))

            def _do_unsubscribe() -> bool:
                ok = True
                try:
                    ua_sub.delete()  # 删除订阅即取消其全部 monitored item
                except Exception:
                    ok = False
                # 从 client 索引中移除
                with self._lock:
                    self._active_subscriptions.pop(sub_id, None)
                return ok

            handle = OpcUaSubscription(
                node_id=node_text,
                subscription_id=sub_id,
                unsub=_do_unsubscribe,
                _asyncua_subscription=ua_sub,
                _monitored_items=monitored,
            )
            with self._lock:
                self._active_subscriptions[sub_id] = handle
            return handle

        return self._execute(operation)

    # ------------------------------------------------------------------
    # Subscribe — Event(v0.35 新增)
    # ------------------------------------------------------------------

    def subscribe_event(
        self,
        node_text: str,
        on_event: Callable[[dict, str, Optional[float]], None],
        *,
        event_filter: Optional[Any] = None,
    ) -> Tuple[bool, Optional[OpcUaSubscription]]:
        """订阅事件(Event)。

        :param node_text: 节点 NodeId 字符串(节点须 EventNotifier = SubscribeToEvents;
            否则服务端拒订阅)
        :param on_event: 回调签名 ``(event_fields_dict, node_id_str, source_timestamp)``
        :param event_filter: 透传 asyncua 的 EventFilter(``None`` = 不过滤)
        :return: ``(成功, 订阅句柄)``
        :raises ValueError: 参数非法
        """
        if not node_text:
            raise ValueError("node_text 不能为空")
        if not callable(on_event):
            raise ValueError(f"on_event 必须是可调用对象,收到:{type(on_event)!r}")

        def operation() -> OpcUaSubscription:
            session = self._session()
            ua_client = session.client
            handler = _EventHandler(self, on_event)
            try:
                # asyncua 1.1.5:sync.Client 直接暴露 create_subscription(非 uaclient)
                ua_sub = ua_client.create_subscription(0, handler)
            except Exception as exc:
                raise _translate_ua_error(exc) from exc
            try:
                # subscribe_events(sourcenode, evtypes, evfilter, ...):
                # handler 已在 create_subscription 订阅级传入;这里只给源节点与过滤
                node = ua_client.get_node(parse_opcua_nodeid(node_text).text)
                handle_ev = ua_sub.subscribe_events(node, evfilter=event_filter)
                monitored = [handle_ev]
            except Exception as exc:
                try:
                    ua_sub.delete()
                except Exception:
                    pass
                raise _translate_ua_error(exc) from exc
            sub_id = int(getattr(ua_sub, "subscription_id", id(ua_sub)))

            def _do_unsubscribe() -> bool:
                ok = True
                try:
                    ua_sub.delete()  # 删除订阅即取消其全部 monitored item
                except Exception:
                    ok = False
                with self._lock:
                    self._active_subscriptions.pop(sub_id, None)
                return ok

            handle = OpcUaSubscription(
                node_id=node_text,
                subscription_id=sub_id,
                unsub=_do_unsubscribe,
                _asyncua_subscription=ua_sub,
                _monitored_items=monitored,
            )
            with self._lock:
                self._active_subscriptions[sub_id] = handle
            return handle

        return self._execute(operation)


# ----------------------------------------------------------------------
# 模块级辅助函数
# ----------------------------------------------------------------------

def _build_endpoint(ip_address: str, port: int, path: str) -> str:
    """由 IP/端口/路径组装 opc.tcp 端点 URL(内部函数)。

    :raises ValueError: 组装结果非法
    """
    cleaned = path.strip().strip("/")
    url = "opc.tcp://{}:{}{}".format(ip_address.strip(), port, f"/{cleaned}" if cleaned else "")
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
    if port_text is not None and not PORT_MIN <= int(port_text) <= PORT_MAX:
        raise ValueError(f"OPC-UA 端点端口必须在 {PORT_MIN}~{PORT_MAX} 之间,收到:{port_text}")


def _coerce_read(value: Any, data_type: DataType, address: str) -> PrimitiveValue:
    """把服务端返回值收窄为本库基础类型(内部函数)。

    :raises ValueError: 返回值类型与目标数据类型不符(调用方参数错误,
        与 AB 标签"实际类型不符"同口径,直接抛出,不断线)
    :raises DeviceError: 节点值为空(设备侧条件,链路正常,不断线不重试)
    """
    if value is None:
        raise DeviceError(f"OPC-UA 节点值为空:{address}", 0)
    if data_type is DataType.BOOL:
        if not isinstance(value, bool):
            raise ValueError(
                f"OPC-UA 节点返回类型不符(期望布尔):{address} ← {value!r}"
            )
        return value
    if data_type in (DataType.SHORT, DataType.USHORT, DataType.INT, DataType.UINT,
                     DataType.LONG, DataType.ULONG):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"OPC-UA 节点返回类型不符(期望整数):{address} ← {value!r}"
            )
        return value
    if data_type in (DataType.FLOAT, DataType.DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"OPC-UA 节点返回类型不符(期望数值):{address} ← {value!r}"
            )
        return float(value)
    if not isinstance(value, str):
        raise ValueError(
            f"OPC-UA 节点返回类型不符(期望字符串):{address} ← {value!r}"
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
            raise ValueError(f"float 超出 float32 范围:{value}") from exc
        return number_f, variant_name
    if data_type is DataType.DOUBLE:
        return require_float(value), variant_name
    if not isinstance(value, str):
        raise ValueError("字符串必须是 str,收到:{}".format(type(value).__name__))
    return value, variant_name


_INT_RANGES = {
    DataType.SHORT: (INT16_MIN, INT16_MAX),
    DataType.USHORT: (0, UINT16_MAX),
    DataType.INT: (INT32_MIN, INT32_MAX),
    DataType.UINT: (0, UINT32_MAX),
    DataType.LONG: (INT64_MIN, INT64_MAX),
    DataType.ULONG: (0, UINT64_MAX),
}
"""整数DataType → (下限, 上限)。"""


def _require_int_range(number: int, data_type: DataType) -> None:
    """整数范围校验(内部函数)。"""
    low, high = _INT_RANGES[data_type]
    if not low <= number <= high:
        raise ValueError(f"{data_type.name} 超出范围 {low}~{high}:{number}")


_BROWSE_ALIASES = {
    "Root": "i=84",       # OPC-UA RootFolder
    "Objects": "i=85",     # ObjectsFolder
    "Types": "i=86",       # TypesFolder
    "Views": "i=87",       # ViewsFolder
}
"""OPC-UA 顶层语义别名 → 标准 NodeId(供 :meth:`OpcUaClient.browse` 用;
``parse_opcua_nodeid`` 不识别非 ``ns=X;...`` 格式,翻译后才能拿到 Node)。"""


def _resolve_browse_alias(node_text: str) -> str:
    """将 browse 接受的别名/NodeId 文本解析为 asyncua ``get_node`` 接受的字符串。

    - 命中别名表(``Root``/``Objects``/``Types``/``Views``)→ 返回对应标准 NodeId
    - 已是标准 NodeId 格式(``ns=X;...``/``i=...``/``s=...``)→ 通过 :func:`parse_opcua_nodeid` 校验后返回
    - 都不是 → 让 :func:`parse_opcua_nodeid` 抛出 ValueError(原契约)
    """
    if node_text in _BROWSE_ALIASES:
        return _BROWSE_ALIASES[node_text]
    # 标准 NodeId 格式:经校验后返回 .text
    return parse_opcua_nodeid(node_text).text
