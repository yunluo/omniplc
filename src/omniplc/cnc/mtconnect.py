"""CNC MTConnect 客户端(HTTP/XML 只读数采,Agent 默认端口 5000)。

依据:MTConnect Part1 Overview and Fundamentals v1.5——§8.2 `/probe` p.13-14、
§8.3.1 `/current`、§8.3.3 Sample Request p.106-111、§8.3.4 Asset Request
p.114-115、§6.5.2.2 Streams Header p.77-78(`nextSequence` 等)。

MTConnect 是机床数控领域开放的互联标准:机器侧运行 MTConnect **Agent**
(HTTP 服务,由 FANUC/三菱等控制器的适配器喂数),客户端以普通 HTTP GET
读取 XML 文档——``/probe``(设备清单)、``/current``(全量当前值快照)、
``/sample``(按序号的历史流)、``/assets``·``/asset/{id}``(资产)。本驱动用
Python 标准库 ``http.client`` + ``xml.etree`` 直接实现,**零第三方依赖、
跨平台**(Agent 与控制器品牌解耦);FANUC FOCAS / 三菱 EZSocket 类
Windows DLL 封装留后续版本。

类继承::

    BaseClient
    └── MTConnectClient   HTTP 会话(keep-alive;会话适配见 _MtConnectSession)

地址即**数据项 id**(兼容其 ``name`` 属性),如 ``Sspeed``/``Xact``/
``execution``;值为 Agent 返回的文本,按显式 DataType 收窄。数据项当前
不可用(``UNAVAILABLE``/``NOT_AVAILABLE``/**空元素**)或不存在 → DeviceError
(设备侧条件,不断线);XML 非法/非 MTConnect 文档 → ProtocolFrameError
断线;HTTP 4xx/5xx 携带 MTConnectError 文档 → DeviceError,否则 OSError。

v1 范围:**只读监控**——类型化 ``read_*``、``snapshot()`` 全量快照、
``read_conditions()`` 条件项(报警/警告/正常)、``probe()``/``probe_all()``
设备信息、``read_sample()`` 历史流、``read_assets()`` 资产。写入、
`/sample?interval` 流式持续推送、订阅留后续版本。
"""
from __future__ import annotations

import http.client
import xml.etree.ElementTree as ElementTree
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from ..core.base_client import BaseClient, validate_endpoint
from ..core.constants import (
    INT16_MAX,
    INT16_MIN,
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    INT64_MIN,
    MTCONNECT_DEFAULT_PORT,
    MTCONNECT_MAX_BODY,
    MTCONNECT_MAX_NUMERIC_TEXT,
    MTCONNECT_READ_CHUNK,
    UINT16_MAX,
    UINT32_MAX,
    UINT64_MAX,
)
from ..core.debug import log_op
from ..core.errors import DeviceError, ProtocolFrameError, TransportClosedError
from ..types import DataType, PrimitiveValue
from ..transport.base import BaseTransport

_CURRENT_PATH = "/current"
_PROBE_PATH = "/probe"
_SAMPLE_PATH = "/sample"
_ASSETS_PATH = "/assets"
_ASSET_PATH = "/asset"

_UNAVAILABLE_VALUES = ("unavailable", "not_available")
"""MTConnect 约定的"当前无值"文本(比较用小写)。"""

_STALE_CONNECTION_ERRORS = (ConnectionResetError, BrokenPipeError)
"""keep-alive 连接被 Agent 空闲超时静默关闭后,复用时抛的连接层异常。

``http.client.RemoteDisconnected`` 同时继承 ``ConnectionResetError`` 与
``BadStatusLine``,故归入此类;这类失效按 GET 幂等重建连接重试一次。
"""

_BOOL_TRUE = ("true", "1", "yes")
_BOOL_FALSE = ("false", "0", "no")
"""MTConnect 布尔值域:Part1 数据类型定义 Boolean 表示为 ``YES``/``NO``,
Agent 亦广泛输出 true/false(小写比较),双口径并收。"""

_SUPPORTED_TYPES = (
    DataType.BOOL,
    DataType.SHORT,
    DataType.USHORT,
    DataType.INT,
    DataType.UINT,
    DataType.LONG,
    DataType.ULONG,
    DataType.FLOAT,
    DataType.DOUBLE,
    DataType.STRING,
)


def _local_name(tag: str) -> str:
    """去掉 XML 命名空间取局部名(内部函数,兼容 MTConnect 各版本 ns)。"""
    return tag.rsplit("}", 1)[-1]


def _fromstring_rejecting_doctype(body: bytes) -> ElementTree.Element:
    """``ElementTree.fromstring`` 的安全包装:拒绝任何 DOCTYPE 子集(内部助手)。

    ``<!DOCTYPE>`` 子集是 XML 实体炸弹(XXE / Billion Laughs)的唯一载体——
    恶意 Agent 借此声明内部实体或 SYSTEM 外部实体,expat 会**默认展开**内部
    实体到解析流中。Python 3.8 起 ``XMLParser`` 提供 ``forbid_dtd``/
    ``forbid_external`` 关键字参数(纵深防御),3.7.9 缺失这两参,只能靠
    解析前扫描字节拒绝。MTConnect 协议合法负载从不使用 DOCTYPE,扫描
    零误伤。命中 DOCTYPE 抛 :class:`xml.etree.ElementTree.ParseError`,由
    调用方既有 ``ParseError`` 处理路径收口为 :class:`ProtocolFrameError` 或
    :class:`OSError`(响应路径语义不变)。
    """
    # UTF-16/UTF-32 的 ASCII 字符高字节夹 0x00,裸 ASCII 扫描会漏判;
    # 同时扫描剔除 NUL 字节后的形式(拒绝是安全方向,无漏放风险)。
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!doctype" in lowered.replace(b"\x00", b""):
        raise ElementTree.ParseError(
            "DOCTYPE 不允许(XML 实体炸弹 / XXE 防御:纵深防护)"
        )
    return ElementTree.fromstring(body)


def _parse_document(body: bytes) -> ElementTree.Element:
    """解析 XML 并校验为 MTConnect 文档,返回根元素(内部函数)。

    :raises ProtocolFrameError: 非法 XML 或非 MTConnect 文档(断线重同步)
    """
    try:
        root = _fromstring_rejecting_doctype(body)
    except ElementTree.ParseError as exc:
        raise ProtocolFrameError(f"MTConnect 响应不是合法 XML:{exc}") from exc
    name = _local_name(root.tag)
    if name not in (
        "MTConnectStreams",
        "MTConnectDevices",
        "MTConnectAssets",
        "MTConnectError",
    ):
        raise ProtocolFrameError(f"MTConnect 响应文档类型非法:{name}")
    return root


def _error_of_document(root: ElementTree.Element) -> Optional[Tuple[str, str]]:
    """提取 MTConnectError 文档的 ``(errorCode, 描述)``,非错误文档返回 None。"""
    if _local_name(root.tag) != "MTConnectError":
        return None
    for elem in root.iter():
        if _local_name(elem.tag) == "Error":
            return elem.attrib.get("errorCode", "UNKNOWN"), (elem.text or "").strip()
    return "UNKNOWN", ""


def _new_connection(
    ip_address: str, port: int, timeout: float
) -> http.client.HTTPConnection:
    """创建 HTTP 连接(模块级,单测以假连接替换;内部函数)。"""
    return http.client.HTTPConnection(ip_address, port, timeout=timeout)


def _query(path: Optional[str], params: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    """组装 HTTP 查询串(内部函数)。

    :param path: 可选 XPath(``path=<XPath>``,URL 编码)
    :param params: 其他 ``(键, 值)`` 对(原样按 URL 编码拼接)
    :return: ``"?k=v&..."``;无参数时返回空串
    """
    parts = []
    if path:
        parts.append("path=" + quote(path, safe=""))
    for key, value in params or ():
        parts.append("{}={}".format(key, quote(str(value), safe="")))
    return "?" + "&".join(parts) if parts else ""


class _MtConnectSession(BaseTransport):
    """MTConnect HTTP 会话适配器:keep-alive 连接适配为传输外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 创建
    ``http.client.HTTPConnection``(惰性建链,首次请求才真正握手),
    ``close`` 关闭;无字节流收发,HTTP 请求经 :meth:`request` 完成。
    keep-alive 连接被 Agent 空闲超时静默关闭时,request 内先原位重建
    连接透明重试一次(GET 幂等),不惊动上层状态机;仍失败才按断线
    上抛。HTTP 库协议异常归一为 OSError(断线惰性重连);socket 层
    异常本就是 OSError,直接上抛。
    """

    def __init__(self, ip_address: str, port: int) -> None:
        """MTConnect 会话适配器。

        :param ip_address: Agent 所在 IP 或主机名(机床或工控机)
        :param port: Agent HTTP 端口,默认 5000
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._conn: Optional[http.client.HTTPConnection] = None
        self._debug_label = f"mtc://{ip_address}:{port}"

    def connect(self) -> None:
        """创建 HTTP 会话(每次连接新建连接对象)。

        :raises OSError: 连接对象创建失败
        """
        self._conn = _new_connection(self._ip_address, self._port, self._receive_timeout)
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """关闭 HTTP 会话,幂等。"""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        conn.close()
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """MTConnect 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("MTConnect 走 HTTP 会话通道,无字节流收发")

    def recv(self, size: int) -> bytes:
        """MTConnect 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("MTConnect 走 HTTP 会话通道,无字节流收发")

    def request(self, path: str) -> bytes:
        """执行一次 HTTP GET,返回 200 响应体(会话调用,异常在此翻译)。

        keep-alive 连接可能已被 Agent 空闲超时静默关闭:遇到连接层
        失效(连接被重置/管道破裂)先原位重建连接再重试一次(GET 幂等,
        同 urllib3 的 stale 连接重试策略),重试仍失败才按断线上抛。

        :raises DeviceError: HTTP 非 200 且响应体为 MTConnectError 文档
        :raises OSError: socket/超时/HTTP 协议异常,或非 200 且无错误文档
        """
        conn = self._require_conn()
        try:
            return self._exchange(conn, path)
        except _STALE_CONNECTION_ERRORS as exc:
            log_op(self._debug_label, "keep-alive 连接已失效,重建后重试:%s", exc)
            self._recreate()
            return self._exchange(self._require_conn(), path)

    def _exchange(self, conn: http.client.HTTPConnection, path: str) -> bytes:
        """发送 GET 并处理响应(单次尝试,连接失效异常交上层重试)。

        :raises DeviceError: HTTP 非 200 且响应体为 MTConnectError 文档
        :raises OSError: 连接被重置/超时/HTTP 协议异常,或非 200 且无错误文档
        """
        if conn.sock is not None:
            conn.sock.settimeout(self._receive_timeout)
        try:
            conn.request("GET", path, headers={"Accept": "application/xml"})
            response = conn.getresponse()
            body = self._read_body(response)
            status = int(response.status)
        except _STALE_CONNECTION_ERRORS:
            raise  # 连接层失效,由 request() 原位重建后重试
        except http.client.HTTPException as exc:
            raise OSError(f"MTConnect HTTP 协议异常:{exc}") from exc
        log_op(self._debug_label, "GET %s → HTTP %d(%dB)", path, status, len(body))
        if status == 200:
            return body
        try:
            root = _fromstring_rejecting_doctype(body)
        except ElementTree.ParseError:
            raise OSError(
                f"MTConnect HTTP 状态 {status}:{body[:120].decode('utf-8', 'replace')}"
            )
        info = _error_of_document(root)
        if info is None:
            raise OSError(f"MTConnect HTTP 状态 {status} 响应非错误文档")
        raise DeviceError(f"MTConnect HTTP {status} {info[0]}:{info[1]}", 0)

    def _read_body(self, response: http.client.HTTPResponse) -> bytes:
        """分块读取响应体,总量超上限按坏帧断线(防恶意 Agent 无限灌数据)。

        :raises ProtocolFrameError: 响应体超过 :data:`MTCONNECT_MAX_BODY`
        """
        body = bytearray()
        while True:
            chunk = response.read(MTCONNECT_READ_CHUNK)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MTCONNECT_MAX_BODY:
                raise ProtocolFrameError(
                    f"MTConnect 响应体超过 {MTCONNECT_MAX_BODY} 字节上限"
                )
        return bytes(body)

    def _recreate(self) -> None:
        """关闭当前 HTTP 连接并原位重建(keep-alive 失效重试用,内部方法)。"""
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
        self.connect()

    def _require_conn(self) -> http.client.HTTPConnection:
        """取当前 HTTP 连接,未建立则抛出(内部方法)。"""
        if self._conn is None:
            raise TransportClosedError("MTConnect 会话未建立")
        return self._conn


class MTConnectClient(BaseClient):
    """CNC MTConnect 客户端(HTTP/XML,只读,Agent 默认端口 5000)。

    地址为 Agent 数据项 id(兼容其 ``name`` 属性),如 ``Sspeed``(主轴
    转速)、``Xact``(X 实际位置)、``execution``(执行状态)、``program``
    (当前程序)。数据项因 Agent 版本/适配器配置而异,可用 :meth:`snapshot`
    先查看机器实际提供哪些项。数值文本按显式 DataType 收窄,返回
    ``UNAVAILABLE`` 视为设备侧无值(失败但不断线)。

    :example::

        client = MTConnectClient("192.168.0.10", 5000)
        client.connect()
        ok, speed = client.read_float("Sspeed")
        ok, program = client.read_string("program")
        ok, items = client.snapshot()
        ok, alarms = client.read_conditions()
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MTCONNECT_DEFAULT_PORT,
    ) -> None:
        """初始化 MTConnect 客户端。

        :param ip_address: Agent 所在 IP 或主机名(机床或工控机)
        :param port: Agent HTTP 端口,默认 5000
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _MtConnectSession:
        """取当前会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _MtConnectSession):
            raise TransportClosedError("内部错误:传输对象不是 MTConnect 会话")
        return link

    def _create_transport(self) -> BaseTransport:
        return _MtConnectSession(self._ip_address, self._port)

    # ------------------------------------------------------------------
    # 文档获取(仅事务锁内)
    # ------------------------------------------------------------------

    def _fetch(self, path: str) -> ElementTree.Element:
        """GET 一个 MTConnect 文档并解析(内部方法)。"""
        return _parse_document(self._session().request(path))

    def _fetch_items(self, path: Optional[str] = None) -> Dict[str, str]:
        """读取 /current 快照,展开为 id/name → 文本值(内部方法)。

        :param path: 可选 XPath 查询(``/current?path=<XPath>`` 过滤)
        """
        root = self._fetch(_CURRENT_PATH + _query(path))
        if _local_name(root.tag) != "MTConnectStreams":
            raise ProtocolFrameError(
                f"MTConnect /current 返回了 {_local_name(root.tag)} 文档"
            )
        items: Dict[str, str] = {}
        for elem in root.iter():
            item_id = elem.attrib.get("dataItemId")
            if item_id is None:
                continue
            # 空元素/空文本是"当前无值"的合法形态(UNAVAILABLE 等价),
            # 存空串而非跳过,供 _read 报"不可用"而非"不存在"
            text = (elem.text or "").strip()
            items[item_id] = text
            name = elem.attrib.get("name")
            if name is not None and name not in items:
                items[name] = text
        return items

    def _fetch_conditions(self, path: Optional[str] = None) -> List[Dict[str, str]]:
        """读取 /current 条件项(报警/警告/正常)列表(内部方法)。"""
        root = self._fetch(_CURRENT_PATH + _query(path))
        if _local_name(root.tag) != "MTConnectStreams":
            raise ProtocolFrameError(
                f"MTConnect /current 返回了 {_local_name(root.tag)} 文档"
            )
        conditions: List[Dict[str, str]] = []
        for elem in root.iter():
            level = _local_name(elem.tag)
            if level not in ("Fault", "Warning", "Normal"):
                continue
            conditions.append(
                {
                    "level": level,
                    "id": elem.attrib.get("dataItemId", ""),
                    "text": (elem.text or "").strip(),
                    "code": elem.attrib.get("nativeCode", ""),
                    "severity": elem.attrib.get("severity", ""),
                    "qualifier": elem.attrib.get("qualifier", ""),
                    "type": elem.attrib.get("type", ""),
                }
            )
        return conditions

    def _fetch_probe(self) -> Dict[str, str]:
        """读取 /probe,返回第一个 Device 的属性(内部方法,兼容保留)。"""
        devices = self._fetch_probe_devices()
        if not devices:
            raise DeviceError("MTConnect /probe 未包含 Device", 0)
        return devices[0]

    def _fetch_probe_devices(self) -> List[Dict[str, str]]:
        """读取 /probe,返回全部 Device 的属性列表(内部方法)。"""
        root = self._fetch(_PROBE_PATH)
        if _local_name(root.tag) != "MTConnectDevices":
            raise ProtocolFrameError(
                f"MTConnect /probe 返回了 {_local_name(root.tag)} 文档"
            )
        devices: List[Dict[str, str]] = []
        for elem in root.iter():
            if _local_name(elem.tag) == "Device":
                devices.append(dict(elem.attrib))
        return devices

    def _fetch_sample(
        self,
        from_sequence: Optional[int],
        count: int,
        path: Optional[str],
        at: Optional[int],
    ) -> Dict[str, object]:
        """读取 /sample 历史流(内部方法)。

        Query 依 Part1 §8.3.3.2 p.108-109:``from``(uint64 起始序号)、
        ``count``(样本数,缺省 100)、``path``(XPath 过滤)、``at``(指定序号)。
        响应 Header 的 ``nextSequence``(= lastSequence+1)供游标续拉。
        """
        params: List[Tuple[str, str]] = []
        if at is not None:
            params.append(("at", str(int(at))))
        elif from_sequence is not None:
            params.append(("from", str(int(from_sequence))))
        params.append(("count", str(int(count))))
        root = self._fetch(_SAMPLE_PATH + _query(path, params))
        if _local_name(root.tag) != "MTConnectStreams":
            raise ProtocolFrameError(
                f"MTConnect /sample 返回了 {_local_name(root.tag)} 文档"
            )
        next_sequence: Optional[int] = None
        samples: List[Dict[str, object]] = []
        for elem in root.iter():
            tag = _local_name(elem.tag)
            if tag == "Header":
                raw_next = elem.attrib.get("nextSequence")
                if raw_next is not None:
                    try:
                        next_sequence = int(raw_next)
                    except ValueError:
                        next_sequence = None
                continue
            sequence = elem.attrib.get("sequence")
            data_item_id = elem.attrib.get("dataItemId")
            if sequence is None or data_item_id is None:
                continue
            try:
                seq_number: Optional[int] = int(sequence)
            except ValueError:
                continue
            samples.append(
                {
                    "sequence": seq_number,
                    "data_item_id": data_item_id,
                    "name": elem.attrib.get("name", ""),
                    "type": tag,
                    "sub_type": elem.attrib.get("subType", ""),
                    "timestamp": elem.attrib.get("timestamp", ""),
                    "value": (elem.text or "").strip(),
                }
            )
        return {"next_sequence": next_sequence, "samples": samples}

    def _fetch_assets(self, asset_ids: Optional[List[str]]) -> List[Dict[str, object]]:
        """读取 /assets(全量)或 /asset/{id;id}(指定)(内部方法)。

        asset id 是任意文本(规范允许空格/``?``/``#`` 等),路径侧必须
        ``quote(..., safe="")``——与 :meth:`_query` 的值编码口径一致;
        不编码时含特殊字符的 id 会破坏请求行,甚至把请求打到非资产端点。
        多 id 分隔符 ``;`` 在拼好路径**之后**保留,不参与编码。
        """
        if asset_ids:
            encoded = ";".join(quote(_require_asset_id(item), safe="") for item in asset_ids)
            path = _ASSET_PATH + "/" + encoded
        else:
            path = _ASSETS_PATH
        root = self._fetch(path)
        if _local_name(root.tag) != "MTConnectAssets":
            raise ProtocolFrameError(
                f"MTConnect /asset 返回了 {_local_name(root.tag)} 文档"
            )
        assets_elem = None
        for elem in root.iter():
            if _local_name(elem.tag) == "Assets":
                assets_elem = elem
                break
        if assets_elem is None:
            return []
        assets: List[Dict[str, object]] = []
        for elem in list(assets_elem):
            tag = _local_name(elem.tag)
            assets.append(
                {
                    "type": tag,
                    "id": elem.attrib.get("id", ""),
                    "attributes": dict(elem.attrib),
                    "text": (elem.text or "").strip(),
                }
            )
        return assets

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读数据项并按数据类型收窄(值不可用时抛 DeviceError,不断线)。

        :raises ValueError: 数据类型不在 :data:`_SUPPORTED_TYPES` 内——
            属**参数错误**,按库约定直接抛给调用方(与"设备侧条件"的
            DeviceError 区分;正常调用经 ``DataType.coerce`` 后不会走到)
        :raises DeviceError: 数据项不存在或当前不可用(设备侧条件,不断线)
        """
        if data_type not in _SUPPORTED_TYPES:
            raise ValueError(f"MTConnect 不支持的数据类型:{data_type}")
        text = _check_address(address)
        items = self._fetch_items()
        if text not in items:
            raise DeviceError(f"MTConnect 数据项不存在:{text}", 0)
        value = items[text]
        if value == "" or value.lower() in _UNAVAILABLE_VALUES:
            raise DeviceError(
                f"MTConnect 数据项当前不可用:{text}={value or 'UNAVAILABLE'}", 0
            )
        return _coerce(value, data_type, address)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """MTConnect 为只读采集协议,不支持写入(不断线契约)。"""
        raise DeviceError("MTConnect 为只读采集协议,不支持写入", 0)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读文本数据项(值本身即字符串,length/encoding 不适用)。"""
        return self._read(address, DataType.STRING)

    # ------------------------------------------------------------------
    # 公共采集接口
    # ------------------------------------------------------------------

    def snapshot(
        self, *, path: Optional[str] = None
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读取 ``/current`` 全量数据项快照(id/name → 文本值)。

        :param path: 可选 XPath 查询(``/current?path=<XPath>`` 过滤节点)
        :return: ``(是否成功, {数据项 id 或 name: 文本值})``;失败为
            ``(False, None)``
        """
        return self._execute(lambda: self._fetch_items(path))

    def read_conditions(
        self, *, path: Optional[str] = None
    ) -> Tuple[bool, Optional[List[Dict[str, str]]]]:
        """读取 ``/current`` 条件项(报警 Fault/警告 Warning/正常 Normal)。

        :param path: 可选 XPath 查询过滤条件节点
        :return: ``(是否成功, [{level, id, text, code, severity,
            qualifier, type}])``;无活动报警时为空列表,失败为 ``(False, None)``
        """
        return self._execute(lambda: self._fetch_conditions(path))

    def probe(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读取 ``/probe`` 设备信息(多设备时取第一个 Device 的属性)。

        :return: ``(是否成功, {属性: 值})``,如 ``name``/``uuid``/
            ``manufacturer`` 等(Agent 适配器填写而定);失败为 ``(False, None)``
        """
        return self._execute(self._fetch_probe)

    def probe_all(self) -> Tuple[bool, Optional[List[Dict[str, str]]]]:
        """读取 ``/probe`` 全部 Device 的属性(多设备 Agent;Part1 §8.2)。

        :return: ``(是否成功, [{属性: 值}, ...])``;无 Device 时为空列表,
            失败为 ``(False, None)``
        """
        return self._execute(self._fetch_probe_devices)

    def read_sample(
        self,
        from_sequence: Optional[int] = None,
        count: int = 100,
        *,
        path: Optional[str] = None,
        at: Optional[int] = None,
    ) -> Tuple[bool, Optional[Dict[str, object]]]:
        """读取 ``/sample`` 历史流(Part1 §8.3.3 p.106-111)。

        单次拉取样本序列;游标续拉:取返回值 ``next_sequence`` 作为下一次
        ``from_sequence``。``count`` 缺省 100(手册默认);``at`` 与
        ``from_sequence`` 互斥(``at`` 取指定序号的单点值)。

        :param from_sequence: 起始序号(uint64;``None`` = 从缓冲区最早样本开始)
        :param count: 本次最多返回的样本数(``>=1``)
        :param path: 可选 XPath 过滤(``/sample?path=<XPath>``)
        :param at: 取指定序号的值(与 ``from_sequence`` 互斥)
        :return: ``(是否成功, {"next_sequence": int|None, "samples":
            [{sequence, data_item_id, name, type, sub_type, timestamp, value}]})``
        :raises ValueError: ``count < 1``,或 ``from_sequence`` 与 ``at`` 同时给出
        """
        if count < 1:
            raise ValueError(f"count 必须大于等于 1,收到:{count}")
        if from_sequence is not None and at is not None:
            raise ValueError("from_sequence 与 at 互斥,只能给其一")
        if from_sequence is not None and from_sequence < 0:
            raise ValueError(f"from_sequence 不能为负,收到:{from_sequence}")
        if at is not None and at < 0:
            raise ValueError(f"at 不能为负,收到:{at}")
        return self._execute(
            lambda: self._fetch_sample(from_sequence, int(count), path, at)
        )

    def read_assets(
        self, asset_ids: Optional[List[str]] = None
    ) -> Tuple[bool, Optional[List[Dict[str, object]]]]:
        """读取 ``/assets``(全量)或 ``/asset/{id;id}``(指定,Part1 §8.3.4)。

        :param asset_ids: 指定资产 id 列表;``None``/空 = 全量 ``/assets``
        :return: ``(是否成功, [{type, id, attributes, text}, ...])``;
            失败为 ``(False, None)``
        """
        return self._execute(lambda: self._fetch_assets(asset_ids))


def _require_asset_id(asset_id: str) -> str:
    """资产 id 校验:非空字符串,返回去除首尾空白后的 id(内部函数)。"""
    text = asset_id.strip() if isinstance(asset_id, str) else ""
    if not text:
        raise ValueError("MTConnect 资产 id 不能为空")
    return text


def _check_address(address: str) -> str:
    """地址校验:非空字符串,返回去除首尾空白后的数据项标识(内部函数)。"""
    text = address.strip() if isinstance(address, str) else ""
    if not text:
        raise ValueError("MTConnect 数据项地址不能为空")
    return text


def _coerce(value: str, data_type: DataType, address: str) -> PrimitiveValue:
    """把 Agent 文本值收窄为本库基础类型(内部函数)。

    :raises ValueError: 值与目标数据类型不符(调用方参数错误,直接抛出)
    """
    if data_type is DataType.BOOL:
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        raise ValueError(f"MTConnect 数据项不是布尔量:{address} ← {value!r}")
    if data_type is DataType.STRING:
        return value
    if len(value) > MTCONNECT_MAX_NUMERIC_TEXT:
        # 3.11 之前的 int/float 对超长数字串是超线性开销,先按长度快拒
        raise ValueError(
            f"MTConnect 数据项数值文本过长:{address} ← {len(value)} 字符"
        )
    if data_type in (DataType.FLOAT, DataType.DOUBLE):
        try:
            return float(value)
        except ValueError:
            raise ValueError(
                f"MTConnect 数据项不是数值:{address} ← {value!r}"
            )
    try:
        number = int(value)
    except ValueError:
        raise ValueError(
            f"MTConnect 数据项不是整数:{address} ← {value!r}"
        )
    # 按声明类型收窄范围:Agent 文本超出该类型属设备侧条件(不断线)
    bounds = _INT_RANGES.get(data_type)
    if bounds is not None and not bounds[0] <= number <= bounds[1]:
        raise DeviceError(
            "MTConnect 数据项超出声明类型范围:{}({} 允许 {}~{})".format(
                address, data_type.name, bounds[0], bounds[1]
            ),
            0,
        )
    return number


_INT_RANGES = {
    DataType.SHORT: (INT16_MIN, INT16_MAX),
    DataType.USHORT: (0, UINT16_MAX),
    DataType.INT: (INT32_MIN, INT32_MAX),
    DataType.UINT: (0, UINT32_MAX),
    DataType.LONG: (INT64_MIN, INT64_MAX),
    DataType.ULONG: (0, UINT64_MAX),
}
"""整数 DataType → (下限, 上限);_coerce 读侧按此收窄。"""
