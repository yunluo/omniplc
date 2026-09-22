"""客户端基类:模板方法模式的公共核心。

所有协议客户端(同步)都继承 :class:`BaseClient`,公共逻辑在这里收口:

- 连接状态机与**惰性自动重连**(断线后在下一次读写时重建连接)
- **线程安全**:一把 ``threading.RLock`` 保证"重连→组帧→收发→校验"
  整个事务原子完成
- **错误约定**:读失败返回 ``(False, None)``,写失败返回 ``False``,
  原因记录在 :attr:`last_error`,公共 API 不抛自定义异常
- 类型化读写方法 ``read_float``/``write_short``/… 只在这里实现一次,
  委托给驱动子类实现的协议原语 :meth:`_read` / :meth:`_write`
"""
from __future__ import annotations

import socket
import threading
import time
from abc import ABC, abstractmethod
from types import TracebackType
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Type, TypeVar, Union

from .constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
    DEFAULT_STRING_ENCODING,
    READ_STRING_DEFAULT_LENGTH,
)
from .errors import DeviceError, OmniPLCInternalError, TransportClosedError
from ..tag import Tag, TagTable
from ..transport import BaseTransport
from ..types import DataType, PrimitiveValue

_T = TypeVar("_T")
_C = TypeVar("_C", bound="BaseClient")


def validate_endpoint(ip_address: str, port: int) -> None:
    """网络型客户端的构造参数校验(供各驱动共用)。

    :param ip_address: IP 或主机名
    :param port: 端口号
    :raises ValueError: 地址为空或端口不在 1~65535
    """
    if not ip_address or not ip_address.strip():
        raise ValueError("ip_address 不能为空")
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"port 必须在 1~65535 之间,收到:{port}")


class BaseClient(ABC):
    """所有 PLC 客户端的抽象基类。

    子类需要实现:

    - :meth:`_create_transport`:创建与走线对应的传输对象
    - :meth:`_read` / :meth:`_write`:协议原语(失败抛内部异常,
      由基类转换为 ``(False, None)``/``False``)
    - 可选 :meth:`_read_string` / :meth:`_write_string`:字符串原语
    - 可选 :meth:`_after_connect`:连接建立后的钩子(如 FINS/TCP 握手)

    线程安全:实例方法可跨线程调用。同一客户端的并发调用被串行化
    (保证正确性而非并行吞吐;需要高并发请使用多个客户端实例)。

    :example: ``with ModbusTcpClient("192.168.0.10", 502, 1) as client: ...``
    """

    def __init__(self, ip_address: str = "", port: int = 0) -> None:
        """初始化公共状态(子类在完成自身参数校验后调用)。

        :param ip_address: IP 或主机名(串口客户端为空字符串)
        :param port: 端口号(串口客户端为 0)
        """
        self._ip_address = ip_address
        self._port = int(port)
        self._connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
        self._receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT
        self._retries: int = 0
        self._write_retries: int = 0
        self._lock = threading.RLock()
        self._transport: Optional[BaseTransport] = None
        self._connected: bool = False
        self._last_error: Optional[str] = None
        self._tag_table: Optional[TagTable] = None
        # 连接健康统计:计数器(int)与时间戳(float)分两组,避免 mypy 在
        # `Dict[str, Union[int, float, None]]` 上把 `+= 1` 误判为非法运算;
        # 公开 stats 属性再合并为单 dict 快照。
        self._counters: Dict[str, int] = {
            "connect_count": 0,
            "disconnect_count": 0,
            "transactions": 0,
            "error_count": 0,
            "device_error_count": 0,
        }
        self._timestamps: Dict[str, Optional[float]] = {
            "last_error_at": None,
            "last_connect_at": None,
            "last_success_at": None,
            "last_rtt": None,
        }
        """连接健康统计(锁内更新;公开只读快照见 :attr:`stats`)。"""

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立连接(幂等:已连接时直接返回 True)。

        :return: 是否成功
        """
        with self._lock:
            if self._connected:
                return True
            # 传输对象创建独立于 try:参数类错误(如未配置串口参数)照常上抛
            transport = self._create_transport()
            try:
                transport.connect_timeout = self._connect_timeout
                transport.receive_timeout = self._receive_timeout
                transport.connect()
            except Exception as exc:
                # 建连失败:任何异常都清理为"未连接"(防脏 socket/传输逃逸)
                self._connected = False
                self._last_error = "连接 {}:{} 失败:{}".format(
                    self._ip_address or "-", self._port or "-", exc
                )
                try:
                    transport.close()
                except Exception:
                    pass
                self._record_error()
                return False
            self._transport = transport
            try:
                self._after_connect()
            except Exception as exc:
                # 握手/会话初始化失败:清理到干净状态,下次事务惰性重连
                self._last_error = "连接初始化失败:{}".format(_describe(exc))
                try:
                    transport.close()
                except Exception:
                    pass
                self._transport = None
                self._connected = False
                self._record_error()
                return False
            self._connected = True
            self._last_error = None
            self._counters["connect_count"] += 1
            self._timestamps["last_connect_at"] = time.monotonic()
            return True

    def disconnect(self) -> bool:
        """断开连接(幂等)。

        :return: 是否成功
        """
        with self._lock:
            transport = self._transport
            self._transport = None
            self._connected = False
            if transport is None:
                return True
            try:
                transport.close()
            except OSError as exc:
                self._last_error = f"关闭连接失败:{exc}"
                self._record_error()
                return False
            self._counters["disconnect_count"] += 1
            return True

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态(加锁快照,不发报文)。"""
        with self._lock:
            return self._connected

    # ------------------------------------------------------------------
    # 可配置属性(超时/重试)
    # ------------------------------------------------------------------

    @property
    def connect_timeout(self) -> float:
        """连接超时(秒)。可在连接建立后修改,立即生效。"""
        return self._connect_timeout

    @connect_timeout.setter
    def connect_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"connect_timeout 必须大于 0,收到:{seconds}")
        with self._lock:
            self._connect_timeout = float(seconds)
            if self._transport is not None:
                self._transport.connect_timeout = self._connect_timeout

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒)。可在连接建立后修改,立即生效。"""
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"receive_timeout 必须大于 0,收到:{seconds}")
        with self._lock:
            self._receive_timeout = float(seconds)
            if self._transport is not None:
                self._transport.receive_timeout = self._receive_timeout

    @property
    def retries(self) -> int:
        """读操作失败后的重试次数(默认 0 = 不重试)。

        重试与**惰性重连**配合:传输失败会标记断开,重试前自动重建连接。
        """
        return self._retries

    @retries.setter
    def retries(self, count: int) -> None:
        if count < 0:
            raise ValueError(f"retries 不能为负数,收到:{count}")
        self._retries = int(count)

    @property
    def write_retries(self) -> int:
        """写操作失败后的重试次数(默认 0,防止重复写入危险动作)。"""
        return self._write_retries

    @write_retries.setter
    def write_retries(self, count: int) -> None:
        if count < 0:
            raise ValueError(f"write_retries 不能为负数,收到:{count}")
        self._write_retries = int(count)

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败的错误描述;成功执行读写后清空为 None。"""
        with self._lock:
            return self._last_error

    @property
    def stats(self) -> dict:
        """连接健康统计快照(只读 dict,锁内取)。

        字段:

        - ``connect_count``:成功建连次数(含惰性重连)
        - ``disconnect_count``:关闭的连接数(显式 disconnect 与
          传输失败后的拆连都计)
        - ``transactions``:已执行的协议事务数(含失败尝试)
        - ``error_count``:失败总数(设备错误 + 传输错误 + 建连失败)
        - ``device_error_count``:PLC 明确返回错误码的次数(链路完好)
        - ``last_error_at`` / ``last_connect_at`` / ``last_success_at``:
          ``time.monotonic()`` 时间戳(秒)
        - ``last_rtt``:最近一次成功事务的往返耗时(秒,含 PLC 等待)

        时间戳为单调钟相对值,跨重启无意义;用于现场判断"多久前
        出错/多久没成功"。
        """
        with self._lock:
            return dict(self._counters, **self._timestamps)

    def _record_error(self) -> None:
        """登记一次失败(错误计数 + 时间戳,内部方法,须锁内调用)。"""
        self._counters["error_count"] += 1
        self._timestamps["last_error_at"] = time.monotonic()

    # ------------------------------------------------------------------
    # 通用读写(模板方法,公共 API)
    # ------------------------------------------------------------------

    def read(
        self, address: str, data_type: Union[DataType, str]
    ) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按数据类型读取一个点。

        :param address: 协议地址,语法由驱动定义,如 ``"hr0"``、``"D100"``
        :param data_type: 数据类型,推荐用 :class:`omniplc.types.DataType`
            枚举(IDE 可自动补全),如 ``DataType.FLOAT``;也兼容名称字符串
        :return: ``(是否成功, 值)``
        :raises ValueError: 地址/类型参数非法(参数校验错误直接抛出)
        """
        data_type_enum = DataType.coerce(data_type)
        return self._execute(lambda: self._read(address, data_type_enum))

    def write(
        self, address: str, data_type: Union[DataType, str], value: PrimitiveValue
    ) -> bool:
        """按数据类型写入一个点。

        :param address: 协议地址
        :param data_type: 数据类型,推荐用 :class:`omniplc.types.DataType` 枚举
        :param value: 待写入值
        :return: 是否成功
        :raises ValueError: 参数非法
        """
        data_type_enum = DataType.coerce(data_type)
        ok, _ = self._execute(
            lambda: self._write(address, data_type_enum, value), is_write=True
        )
        return ok

    def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取,逐点独立容错:单点失败不影响其他点。

        基类实现为逐点独立事务;驱动可覆写为协议级批量合并(接口不变):
        MC 3E/4E(0406)、FINS(0104)、AB 0x0A 多服务包、OPC-UA UA Read、
        MX Component ReadDeviceRandom 均已覆写为单事务
        (整批容错,见各驱动 ``read_many`` docstring)。

        :param addresses: 地址列表
        :param data_type: 数据类型,推荐用 :class:`omniplc.types.DataType` 枚举
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表
        """
        return [self.read(address, data_type) for address in addresses]

    def write_many(
        self, items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]]
    ) -> List[bool]:
        """批量写入,逐点独立容错。

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: 与 items 顺序对应的布尔结果列表
        """
        return [self.write(address, data_type, value) for address, data_type, value in items]

    # ------------------------------------------------------------------
    # 类型化读写(一次实现,全协议共享)
    # ------------------------------------------------------------------

    def read_bool(self, address: str) -> Tuple[bool, Optional[bool]]:
        """读取布尔量(位)。"""
        ok, value = self.read(address, DataType.BOOL)
        if not ok or value is None or not isinstance(value, bool):
            return False, None
        return True, value

    def read_short(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位有符号整数。"""
        return _narrow_int(self.read(address, DataType.SHORT))

    def read_ushort(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位无符号整数。"""
        return _narrow_int(self.read(address, DataType.USHORT))

    def read_int(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位有符号整数。"""
        return _narrow_int(self.read(address, DataType.INT))

    def read_uint(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位无符号整数。"""
        return _narrow_int(self.read(address, DataType.UINT))

    def read_long(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位有符号整数。"""
        return _narrow_int(self.read(address, DataType.LONG))

    def read_ulong(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位无符号整数。"""
        return _narrow_int(self.read(address, DataType.ULONG))

    def read_float(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 32 位浮点数(float32)。"""
        return _narrow_float(self.read(address, DataType.FLOAT))

    def read_double(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 64 位浮点数(float64)。"""
        return _narrow_float(self.read(address, DataType.DOUBLE))

    def read_string(
        self,
        address: str,
        length: int = READ_STRING_DEFAULT_LENGTH,
        encoding: str = DEFAULT_STRING_ENCODING,
    ) -> Tuple[bool, Optional[str]]:
        """读取字符串。

        :param address: 协议地址(从该地址起的连续字节)
        :param length: 目标字节长度,默认 32
        :param encoding: 字符编码,默认 ASCII
        """
        if length <= 0:
            raise ValueError(f"length 必须大于 0,收到:{length}")
        ok, value = self._execute(lambda: self._read_string(address, length, encoding))
        if not ok or value is None:
            return False, None
        return True, str(value)

    def write_bool(self, address: str, value: bool) -> bool:
        """写入布尔量(位)。"""
        return self.write(address, DataType.BOOL, bool(value))

    def write_short(self, address: str, value: int) -> bool:
        """写入 16 位有符号整数。"""
        return self.write(address, DataType.SHORT, int(value))

    def write_ushort(self, address: str, value: int) -> bool:
        """写入 16 位无符号整数。"""
        return self.write(address, DataType.USHORT, int(value))

    def write_int(self, address: str, value: int) -> bool:
        """写入 32 位有符号整数。"""
        return self.write(address, DataType.INT, int(value))

    def write_uint(self, address: str, value: int) -> bool:
        """写入 32 位无符号整数。"""
        return self.write(address, DataType.UINT, int(value))

    def write_long(self, address: str, value: int) -> bool:
        """写入 64 位有符号整数。"""
        return self.write(address, DataType.LONG, int(value))

    def write_ulong(self, address: str, value: int) -> bool:
        """写入 64 位无符号整数。"""
        return self.write(address, DataType.ULONG, int(value))

    def write_float(self, address: str, value: float) -> bool:
        """写入 32 位浮点数(float32)。"""
        return self.write(address, DataType.FLOAT, float(value))

    def write_double(self, address: str, value: float) -> bool:
        """写入 64 位浮点数(float64)。"""
        return self.write(address, DataType.DOUBLE, float(value))

    def write_string(
        self, address: str, value: str, encoding: str = DEFAULT_STRING_ENCODING
    ) -> bool:
        """写入字符串(按驱动默认布局补齐/截断)。

        :param address: 协议地址
        :param value: 待写入字符串(不支持空串;字软元件型协议无法表达
            零长度写。支持空串的协议如 AB/OPC-UA 可用
            ``write(address, DataType.STRING, "")`` 写入)
        :param encoding: 字符编码,默认 ASCII
        """
        if not value:
            raise ValueError("value 不能为空字符串")
        ok, _ = self._execute(
            lambda: self._write_string(address, str(value), encoding), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 点位表(Tag)
    # ------------------------------------------------------------------

    def bind_tags(self, table: TagTable) -> None:
        """绑定点位表,之后可用名称读写::``client.read_tag("炉温")``。

        :param table: :class:`omniplc.tag.TagTable` 实例
        """
        self._tag_table = table

    def read_tag(self, tag: Union[str, Tag]) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按点位(或名称)读取,数值自动应用 ``scale``/``offset``。

        :param tag: :class:`omniplc.tag.Tag` 实例,或已绑定表中的名称
        :raises ValueError: 传入名称但未绑定 TagTable,或名称不存在
        """
        resolved = self._resolve_tag(tag)
        ok, value = self.read(resolved.address, resolved.data_type)
        if not ok or value is None:
            return False, None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True, value
        return True, value * resolved.scale + resolved.offset

    def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool:
        """按点位(或名称)写入,数值自动做逆缩放 ``值 = (目标 - offset) / scale``。

        :param tag: Tag 实例或已绑定表中的名称
        :param value: 目标工程量
        :raises ValueError: 同 :meth:`read_tag`;或点位 ``scale`` 为 0
        """
        resolved = self._resolve_tag(tag)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if resolved.scale == 0:
                raise ValueError(f"点位 {resolved.name!r} 的 scale 不能为 0,无法逆缩放")
            value = (value - resolved.offset) / resolved.scale
            if isinstance(value, float) and value.is_integer():
                # 真除法恒为 float,还原整数,否则底层整数类型校验拒收
                value = int(value)
        return self.write(resolved.address, resolved.data_type, value)

    def _resolve_tag(self, tag: Union[str, Tag]) -> Tag:
        """把名称或 Tag 统一解析为 Tag(内部方法)。"""
        if isinstance(tag, Tag):
            return tag
        if self._tag_table is None:
            raise ValueError(f"未绑定 TagTable,无法按名称读写:{tag!r}")
        try:
            return self._tag_table[tag]
        except KeyError:
            raise ValueError(f"点位表中不存在:{tag!r}")

    # ------------------------------------------------------------------
    # 事务执行:惰性重连 + 重试 + 错误转换(线程安全核心)
    # ------------------------------------------------------------------

    def _execute(
        self, operation: Callable[[], _T], is_write: bool = False
    ) -> Tuple[bool, Optional[_T]]:
        """事务模板:在事务锁内执行一次协议操作(内部方法)。

        - 断线时先惰性重连(失败则本次直接返回失败)
        - 传输/协议失败标记断开,并按 ``retries``/``write_retries`` 重试
        - PLC 明确返回错误码(DeviceError)不断线、不重试——链路是好的
        - 所有内部异常转换为 ``(False, None)``,原因写入 :attr:`last_error`

        :param operation: 无参可调用,成功返回值,失败抛内部异常/OSError
        :param is_write: 是否写操作(决定重试次数与防重复写入语义)
        :return: ``(是否成功, 值)``
        """
        retries = self._write_retries if is_write else self._retries
        with self._lock:
            self._counters["transactions"] += 1
            started = time.perf_counter()
            for attempt in range(retries + 1):
                if not self._connected and not self.connect():
                    # connect() 内部已记录 last_error;标记断开后重试即重连
                    continue
                try:
                    value = operation()
                    self._last_error = None
                    self._timestamps["last_success_at"] = time.monotonic()
                    self._timestamps["last_rtt"] = time.perf_counter() - started
                    return True, value
                except DeviceError as exc:
                    self._last_error = _describe(exc)
                    self._counters["device_error_count"] += 1
                    self._record_error()
                    return False, None
                except (OSError, OmniPLCInternalError) as exc:
                    self._last_error = _describe(exc)
                    self._record_error()
                    self._mark_disconnected()
            return False, None

    def _mark_disconnected(self) -> None:
        """标记断开并静默关闭传输(惰性重连发生在下一次事务,内部方法)。"""
        self._connected = False
        if self._transport is not None:
            try:
                self._transport.close()
            except OSError:
                pass
            self._transport = None
            self._counters["disconnect_count"] += 1

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self: _C) -> _C:
        """进入 with 时自动连接,失败抛 ConnectionError(与 pyhsl 一致)。"""
        if not self.connect():
            raise ConnectionError(f"连接失败:{self._last_error}")
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # 驱动子类需要实现的协议原语
    # ------------------------------------------------------------------

    def _require_transport(self) -> BaseTransport:
        """取当前传输对象(仅事务锁内调用,内部方法)。

        :raises TransportClosedError: 连接未建立(正常流程下由基类先重连)
        """
        if self._transport is None:
            raise TransportClosedError("连接未建立")
        return self._transport

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """创建与走线对应的传输对象(每次连接新建)。"""

    def _after_connect(self) -> None:
        """连接建立后的钩子,默认无操作(FINS/TCP 用它做握手)。"""

    @abstractmethod
    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """协议读原语(内部方法)。

        成功返回解码后的值;失败抛 OSError 或内部异常
        (:mod:`omniplc.core.errors`),由 :meth:`_execute` 统一转换。
        """

    @abstractmethod
    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """协议写原语(内部方法),失败语义同 :meth:`_read`。"""

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """字符串读原语,默认不支持,由驱动覆写(内部方法)。

        缺省实现抛 :class:`DeviceError`(链路正常,由基类转
        ``(False, None)`` + ``last_error``),不逃逸裸异常。
        """
        raise DeviceError("当前驱动暂不支持字符串读取", 0)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """字符串写原语,默认不支持,由驱动覆写(内部方法)。

        缺省实现语义同 :meth:`_read_string`。
        """
        raise DeviceError("当前驱动暂不支持字符串写入", 0)

    def _bump_id(self, attr: str, bits: int = 16) -> int:
        """递增指定字段的协议序列号(回绕到 0),返回新值(内部方法)。

        协议层(MC 4E 串行号、FINS SID、AB connected 序列号、Modbus 事务号等)
        自增的 1~16 位无符号循环计数器复用本工具,避免每客户端重复
        ``(x + 1) & mask`` 样板。

        :param attr: 内部计数器字段名(以下划线开头,如 ``"_serial"``)
        :param bits: 位宽(默认 16,即 0~65535 循环)
        """
        current = getattr(self, attr)
        next_val = (current + 1) & ((1 << bits) - 1)
        setattr(self, attr, next_val)
        return next_val


def _describe(exc: BaseException) -> str:
    """把异常转换为可读的 last_error 文本(内部函数)。"""
    text = str(exc).strip()
    if isinstance(exc, socket.timeout):
        return f"通信超时:{text or 'receive_timeout 到期'}"
    return f"{type(exc).__name__}:{text}" if text else type(exc).__name__


def _narrow_int(
    result: Tuple[bool, Optional[PrimitiveValue]]
) -> Tuple[bool, Optional[int]]:
    """把通用读结果收窄为整数签名(内部函数)。"""
    ok, value = result
    if not ok or value is None or isinstance(value, bool) or not isinstance(value, int):
        return False, None
    return True, value


def _narrow_float(
    result: Tuple[bool, Optional[PrimitiveValue]]
) -> Tuple[bool, Optional[float]]:
    """把通用读结果收窄为浮点签名(内部函数)。"""
    ok, value = result
    if not ok or value is None or isinstance(value, bool) or not isinstance(value, float):
        return False, None
    return True, value
