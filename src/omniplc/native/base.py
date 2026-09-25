"""原生 asyncio 客户端基类:异步事务模板。

:class:`AsyncBaseClient` 是同步 :class:`~omniplc.core.BaseClient` 的
**原生异步孪生**——不是线程池包装,而是把同一套事务模板(惰性重连 + 退避
门控 + 重试 + 错误三分口径 + 连接健康统计)用 ``asyncio`` 重写一遍。协议
编解码、地址解析、错误分类助手全部与同步侧**共用同一份实现**,不存在第二套
帧语义。

与 :mod:`omniplc.aio`(同步 I/O + 线程池包装)的差别,是本层存在的理由:

- **属性读取不阻塞事件循环**:``connected``/``stats``/``last_error*`` 等直接
  读字段、不取锁——单线程事件循环里字段更新与读取之间没有 ``await`` 间隙,
  是**真原子快照**;包装层的同步属性要抢事务锁,最长会阻塞一个
  ``receive_timeout``。
- **原生取消**:``await`` 被取消就真的中断。取消时按"是否已发出请求"决定
  链路处理(见 :meth:`_execute`),而不是像包装层那样"放弃等待、事务照跑完"。
- 同一事件循环内多客户端天然并发;同一客户端仍按 FIFO 串行。

**锁纪律**(``asyncio.Lock`` 非重入,与同步层 ``RLock`` 不同):只有公开
入口(:meth:`connect`/:meth:`disconnect`/:meth:`close`/:meth:`_execute`)取锁,
内部 ``_*_locked`` 助手假定"锁已持有"。跨线程/跨事件循环使用同一实例不支持。

**首批能力面**:单点读/写 + 类型化方法 + 字符串 + 点位表;批量
(``read_many``/``read_batch`` 等)与各驱动扩展方法留后续批次。
"""
from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from concurrent.futures import CancelledError
from types import TracebackType
from typing import Awaitable, Callable, Dict, Optional, Tuple, Type, TypeVar, Union, cast

from .transport import AsyncBaseTransport
from ..core.base_client import (
    ClientStats,
    _categorize,
    _describe,
    _extract_code,
    _narrow_float,
    _narrow_int,
)
from ..core.constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
    DEFAULT_STRING_ENCODING,
    READ_STRING_DEFAULT_LENGTH,
    RECONNECT_BACKOFF_BASE,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_BACKOFF_MAX,
)
from ..core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    TransportClosedError,
    TransportTimeoutError,
)
from ..tag import Tag, TagTable
from ..types import DataType, PrimitiveValue

_T = TypeVar("_T")
_C = TypeVar("_C", bound="AsyncBaseClient")


class AsyncBaseClient(ABC):
    """所有原生异步 PLC 客户端的抽象基类。

    子类需要实现:

    - :meth:`_create_transport`:创建与走线对应的**异步**传输对象
    - :meth:`_read` / :meth:`_write`:协议原语(协程;失败抛内部异常,
      由基类转换为 ``(False, None)``/``False``)
    - 可选 :meth:`_read_string` / :meth:`_write_string`:字符串原语
    - 可选 :meth:`_after_connect`:连接建立后的**异步**钩子(如 FINS/TCP 握手)

    :example: ``async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client: ...``
    """

    def __init__(self, ip_address: str = "", port: int = 0) -> None:
        """初始化公共状态(子类在完成自身参数校验后调用)。

        :param ip_address: IP 或主机名
        :param port: 端口号
        """
        self._ip_address = ip_address
        self._port = int(port)
        self._connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
        self._receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT
        self._retries: int = 0
        self._write_retries: int = 0
        self._transport: Optional[AsyncBaseTransport] = None
        self._connected: bool = False
        self._closed: bool = False
        self._last_error: Optional[str] = None
        self._last_error_category: Optional[ErrorCategory] = None
        self._last_error_code: Optional[int] = None
        self._reconnect_backoff: bool = True
        self._next_connect_at: float = 0.0
        self._connect_fail_count: int = 0
        self._tag_table: Optional[TagTable] = None
        # 事务锁**惰性创建**:asyncio.Lock 在 3.7 构造时就绑定当时的事件循环,
        # 模块级构造客户端(尚未 asyncio.run)会绑到错误的循环上;按"首次
        # 使用时所在循环"创建即可规避,同循环复用同一把锁。
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        # 连接健康统计:键集即公开契约 ClientStats(与同步层同一类型);
        # 计数器与时间戳分两组,避免 mypy 在 Union 上把 `+= 1` 判为非法运算
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

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """建立连接(幂等:已连接时直接返回 True)。

        失败后进入**指数退避门控**(与同步版同口径):第 n 次连续失败后,
        ``uniform(0, min(0.5 × 2ⁿ, 30))`` 秒内的再次连接直接拒绝(时间戳
        比较,不发包、不 sleep)。连接成功或显式 :meth:`disconnect` 后门控与
        失败计数全部重置;:attr:`reconnect_backoff` 置 False 可整体关闭。

        :return: 是否成功
        """
        self._ensure_open()
        async with self._guard():
            return await self._connect_locked()

    async def _connect_locked(self) -> bool:
        """连接实现(内部方法,**调用方须已持有事务锁**)。"""
        if self._connected:
            return True
        now = time.monotonic()
        if self._reconnect_backoff and now < self._next_connect_at:
            delay = self._next_connect_at - now
            self._set_error(
                f"连接退避中:{delay:.1f} 秒后允许重连",
                ErrorCategory.TRANSPORT,
                None,
                record=False,
            )
            return False
        # 传输对象创建独立于 try:参数类错误(如未配置串口参数)照常上抛
        transport = self._create_transport()
        try:
            transport.connect_timeout = self._connect_timeout
            transport.receive_timeout = self._receive_timeout
            await transport.connect()
        except CancelledError:
            # 取消必须传播:半开传输直接关掉(尚未发过字节,链路无需重同步)
            try:
                transport.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            # 建连失败:任何异常都清理为"未连接"(防脏 socket/传输逃逸)
            self._connected = False
            self._register_connect_failure()
            self._set_error(
                "连接 {}:{} 失败:{}".format(
                    self._ip_address or "-", self._port or "-", exc
                ),
                _categorize(exc),
                _extract_code(exc),
            )
            try:
                transport.close()
            except Exception:
                pass
            return False
        self._transport = transport
        try:
            await self._after_connect()
        except CancelledError:
            try:
                transport.close()
            except Exception:
                pass
            self._transport = None
            raise
        except Exception as exc:
            # 握手/会话初始化失败:清理到干净状态,下次事务惰性重连
            self._register_connect_failure()
            self._set_error(
                "连接初始化失败:{}".format(_describe(exc)),
                _categorize(exc),
                _extract_code(exc),
            )
            try:
                transport.close()
            except Exception:
                pass
            self._transport = None
            self._connected = False
            return False
        self._connected = True
        self._clear_error()
        self._reset_backoff()
        self._counters["connect_count"] += 1
        self._timestamps["last_connect_at"] = time.monotonic()
        return True

    async def disconnect(self) -> bool:
        """断开连接(幂等)。

        :return: 是否成功
        """
        self._ensure_open()
        async with self._guard():
            return self._disconnect_locked()

    def _disconnect_locked(self) -> bool:
        """断开实现(内部方法,**调用方须已持有事务锁**)。"""
        transport = self._transport
        self._transport = None
        self._connected = False
        self._reset_backoff()
        if transport is None:
            return True
        try:
            transport.close()
        except OSError as exc:
            self._set_error(f"关闭连接失败:{exc}", _categorize(exc), _extract_code(exc))
            return False
        self._counters["disconnect_count"] += 1
        return True

    async def close(self) -> None:
        """关闸并断开(幂等;语义对齐 :meth:`omniplc.aio.ABaseClient.close`)。

        **关闸**:调用后任何协议方法立即抛 ``RuntimeError``(不再受理新事务);
        随后断开连接。与线程池包装层不同,本层没有待排空的任务队列——
        ``await`` 就是事务本身,取消它会真中断(见 :meth:`_execute`)。
        """
        self._closed = True
        async with self._guard():
            self._disconnect_locked()

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态(直接读字段,不发报文、不阻塞)。

        单线程事件循环下字段更新无 ``await`` 间隙,读取即原子快照;
        线程间共享同一实例不支持(见模块 docstring)。
        """
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
        self._receive_timeout = float(seconds)
        if self._transport is not None:
            self._transport.receive_timeout = self._receive_timeout

    @property
    def retries(self) -> int:
        """读操作失败后的重试次数(默认 0 = 不重试)。

        重试与**惰性重连**配合:传输失败会标记断开,重试前自动重建连接。
        接收超时(:class:`TransportTimeoutError`,UDP 的 0 字节超时)不拆连,
        直接在原连接上重发——与 TCP 超时(socket.timeout)的拆连重试口径一致。
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
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(默认开)。"""
        return self._reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(f"reconnect_backoff 必须为布尔值,收到:{enabled!r}")
        self._reconnect_backoff = enabled

    @property
    def next_connect_in(self) -> Optional[float]:
        """距下次允许连接的剩余秒数;None = 无门控,可立即连接。"""
        if not self._reconnect_backoff:
            return None
        remaining = self._next_connect_at - time.monotonic()
        return remaining if remaining > 0 else None

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败的错误描述;成功执行读写后清空为 None。"""
        return self._last_error

    @property
    def last_error_category(self) -> Optional[ErrorCategory]:
        """最近一次失败的分类(成功读写后清空为 None)。

        取值与同步层完全一致:传输类 ``TRANSPORT``、PLC 明确报错 ``DEVICE``
        (配 :attr:`last_error_code` 取原始码)、超时独立 ``TIMEOUT``。
        """
        return self._last_error_category

    @property
    def last_error_code(self) -> Optional[int]:
        """最近一次失败的原始错误码(成功读写后清空为 None)。"""
        return self._last_error_code

    @property
    def stats(self) -> ClientStats:
        """连接健康统计快照(字段语义与同步层 :attr:`BaseClient.stats` 一致)。

        返回拷贝,改动返回值不影响内部计数;字段见
        :class:`~omniplc.core.base_client.ClientStats`。
        """
        return cast(ClientStats, dict(self._counters, **self._timestamps))

    # ------------------------------------------------------------------
    # 通用读写(模板方法,公共 API)
    # ------------------------------------------------------------------

    async def read(
        self, address: str, data_type: Union[DataType, str]
    ) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按数据类型读取一个点。

        :param address: 协议地址,语法由驱动定义,如 ``"hr0"``、``"D100"``
        :param data_type: 数据类型,推荐用 :class:`omniplc.types.DataType`
            枚举;也兼容名称字符串
        :return: ``(是否成功, 值)``
        :raises ValueError: 地址/类型参数非法(参数校验错误直接抛出)
        """
        data_type_enum = DataType.coerce(data_type)
        return await self._execute(lambda: self._read(address, data_type_enum))

    async def write(
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
        ok, _ = await self._execute(
            lambda: self._write(address, data_type_enum, value), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 类型化读写(一次实现,全协议共享;口径与同步基类逐条对齐)
    # ------------------------------------------------------------------

    async def read_bool(self, address: str) -> Tuple[bool, Optional[bool]]:
        """读取布尔量(位)。"""
        ok, value = await self.read(address, DataType.BOOL)
        if not ok or value is None or not isinstance(value, bool):
            return False, None
        return True, value

    async def read_short(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位有符号整数。"""
        return _narrow_int(await self.read(address, DataType.SHORT))

    async def read_ushort(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位无符号整数。"""
        return _narrow_int(await self.read(address, DataType.USHORT))

    async def read_int(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位有符号整数。"""
        return _narrow_int(await self.read(address, DataType.INT))

    async def read_uint(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位无符号整数。"""
        return _narrow_int(await self.read(address, DataType.UINT))

    async def read_long(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位有符号整数。"""
        return _narrow_int(await self.read(address, DataType.LONG))

    async def read_ulong(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位无符号整数。"""
        return _narrow_int(await self.read(address, DataType.ULONG))

    async def read_float(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 32 位浮点数(float32)。"""
        return _narrow_float(await self.read(address, DataType.FLOAT))

    async def read_double(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 64 位浮点数(float64)。"""
        return _narrow_float(await self.read(address, DataType.DOUBLE))

    async def read_string(
        self,
        address: str,
        length: int = READ_STRING_DEFAULT_LENGTH,
        encoding: str = DEFAULT_STRING_ENCODING,
    ) -> Tuple[bool, Optional[str]]:
        """读取字符串。

        :param address: 协议地址(从该地址起的连续字节)
        :param length: 目标字节长度,默认 32
        :param encoding: 字符编码,默认 ASCII
        :raises ValueError: length 非正
        """
        if length <= 0:
            raise ValueError(f"length 必须大于 0,收到:{length}")
        ok, value = await self._execute(
            lambda: self._read_string(address, length, encoding)
        )
        if not ok or value is None:
            return False, None
        return True, str(value)

    async def write_bool(self, address: str, value: bool) -> bool:
        """写入布尔量(位)。

        :param value: 真值(``bool``;兼容 ``int`` 0/1)
        :raises ValueError: value 不是 bool/int(与同步版同口径显式拒绝,
            不把 ``"0"`` 这类非空字符串静默吞成 ``True``)
        """
        if isinstance(value, str) or not isinstance(value, int):
            raise ValueError(f"布尔量必须是 bool,收到:{type(value).__name__}")
        return await self.write(address, DataType.BOOL, bool(value))

    async def write_short(self, address: str, value: int) -> bool:
        """写入 16 位有符号整数。"""
        return await self.write(address, DataType.SHORT, int(value))

    async def write_ushort(self, address: str, value: int) -> bool:
        """写入 16 位无符号整数。"""
        return await self.write(address, DataType.USHORT, int(value))

    async def write_int(self, address: str, value: int) -> bool:
        """写入 32 位有符号整数。"""
        return await self.write(address, DataType.INT, int(value))

    async def write_uint(self, address: str, value: int) -> bool:
        """写入 32 位无符号整数。"""
        return await self.write(address, DataType.UINT, int(value))

    async def write_long(self, address: str, value: int) -> bool:
        """写入 64 位有符号整数。"""
        return await self.write(address, DataType.LONG, int(value))

    async def write_ulong(self, address: str, value: int) -> bool:
        """写入 64 位无符号整数。"""
        return await self.write(address, DataType.ULONG, int(value))

    async def write_float(self, address: str, value: float) -> bool:
        """写入 32 位浮点数(float32)。"""
        return await self.write(address, DataType.FLOAT, float(value))

    async def write_double(self, address: str, value: float) -> bool:
        """写入 64 位浮点数(float64)。"""
        return await self.write(address, DataType.DOUBLE, float(value))

    async def write_string(
        self, address: str, value: str, encoding: str = DEFAULT_STRING_ENCODING
    ) -> bool:
        """写入字符串(按驱动默认布局补齐/截断)。

        :param address: 协议地址
        :param value: 待写入字符串(不支持空串,与同步版同口径)
        :param encoding: 字符编码,默认 ASCII
        :raises ValueError: value 为空字符串
        """
        if not value:
            raise ValueError("value 不能为空字符串")
        ok, _ = await self._execute(
            lambda: self._write_string(address, str(value), encoding), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 点位表(Tag)
    # ------------------------------------------------------------------

    def bind_tags(self, table: TagTable) -> None:
        """绑定点位表,之后可用点位标识读写::``await client.read_tag("furnace_temp")``。

        :param table: :class:`omniplc.tag.TagTable` 实例
        """
        self._tag_table = table

    async def read_tag(
        self, tag: Union[str, Tag]
    ) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按点位(或标识)读取,数值自动应用 ``scale``/``offset``。

        :param tag: :class:`omniplc.tag.Tag` 实例,或已绑定表中的点位标识
        :raises ValueError: 传入标识但未绑定 TagTable,或标识不存在
        """
        resolved = self._resolve_tag(tag)
        ok, value = await self.read(resolved.address, resolved.data_type)
        if not ok or value is None:
            return False, None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True, value
        return True, value * resolved.scale + resolved.offset

    async def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool:
        """按点位(或标识)写入,数值自动做逆缩放 ``值 = (目标 - offset) / scale``。

        :param tag: Tag 实例或已绑定表中的点位标识
        :param value: 目标工程量
        :raises ValueError: 同 :meth:`read_tag`;或点位 ``scale`` 为 0
        """
        resolved = self._resolve_tag(tag)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if resolved.scale == 0:
                raise ValueError(f"点位 {resolved.tag_id!r} 的 scale 不能为 0,无法逆缩放")
            value = (value - resolved.offset) / resolved.scale
            if isinstance(value, float) and value.is_integer():
                value = int(value)
        return await self.write(resolved.address, resolved.data_type, value)

    def _resolve_tag(self, tag: Union[str, Tag]) -> Tag:
        """把点位标识或 Tag 统一解析为 Tag(内部方法)。"""
        if isinstance(tag, Tag):
            return tag
        if self._tag_table is None:
            raise ValueError(f"未绑定 TagTable,无法按点位标识读写:{tag!r}")
        try:
            return self._tag_table[tag]
        except KeyError:
            raise ValueError(f"点位表中不存在:{tag!r}")

    # ------------------------------------------------------------------
    # 事务执行:惰性重连 + 重试 + 错误转换(事件循环内串行的核心)
    # ------------------------------------------------------------------

    async def _execute(
        self, operation: Callable[[], Awaitable[_T]], is_write: bool = False
    ) -> Tuple[bool, Optional[_T]]:
        """事务模板:在事务锁内 ``await`` 一次协议操作(内部方法)。

        口径逐条对齐同步 :meth:`BaseClient._execute`:

        - 断线时先惰性重连(失败则本次直接返回失败)
        - 传输/协议失败标记断开,并按 ``retries``/``write_retries`` 重试
        - 接收超时(:class:`TransportTimeoutError`,0 字节已读 = 链路无残渣)
          不拆连、不计 ``device_error_count``,但与其他传输失败一样参与重试
        - PLC 明确返回错误码(DeviceError)不断线、不重试——链路是好的
        - 调用方错误(``ValueError`` 等)与编程错误仍直接抛出(锁正常释放)

        **取消语义**(原生层特有):``await`` 被取消时,若本次事务**已发出
        请求**(传输层 ``pending``),链路可能残留未配对的应答 → 保守拆连
        重同步;请求尚未发出则保持连接。随后 ``CancelledError`` **原样传播**
        (取消必须让调用方可见,不吞)。

        :param operation: 无参协程工厂,成功返回值,失败抛内部异常/OSError
        :param is_write: 是否写操作(决定重试次数与防重复写入语义)
        :return: ``(是否成功, 值)``
        """
        self._ensure_open()
        retries = self._write_retries if is_write else self._retries
        async with self._guard():
            self._counters["transactions"] += 1
            started = time.perf_counter()
            for attempt in range(retries + 1):
                if not self._connected:
                    if self.next_connect_in is not None:
                        # 退避门控激活:窗口内重试只会空转,直接结束——
                        # last_error 保留武装门控的那次真实失败根因
                        break
                    if not await self._connect_locked():
                        continue
                transport = self._transport
                try:
                    value = await operation()
                except CancelledError:
                    if transport is not None and transport.pending:
                        self._mark_disconnected()
                    raise
                except TransportTimeoutError as exc:
                    # 超时但 0 字节已读:链路无残渣,不拆连也不计设备错误码;
                    # 与其他传输失败一致进入重试(与同步层同口径)
                    self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
                    continue
                except DeviceError as exc:
                    code = _extract_code(exc)
                    self._set_error(_describe(exc), _categorize(exc), code)
                    if code is not None:
                        # 只计"PLC 明确返回错误码"的次数
                        self._counters["device_error_count"] += 1
                    return False, None
                except (OSError, OmniPLCInternalError) as exc:
                    self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
                    self._mark_disconnected()
                else:
                    if transport is not None:
                        transport.mark_synced()
                    self._clear_error()
                    self._timestamps["last_success_at"] = time.monotonic()
                    self._timestamps["last_rtt"] = time.perf_counter() - started
                    return True, value
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

    def _register_connect_failure(self) -> None:
        """登记一次建连失败并推进退避门控(内部方法,须锁内调用)。"""
        cap = min(
            RECONNECT_BACKOFF_BASE
            * (RECONNECT_BACKOFF_FACTOR ** self._connect_fail_count),
            RECONNECT_BACKOFF_MAX,
        )
        self._next_connect_at = time.monotonic() + random.uniform(0.0, cap)
        self._connect_fail_count += 1

    def _reset_backoff(self) -> None:
        """清空退避门控(连接成功或显式断开时,内部方法,须锁内调用)。"""
        self._next_connect_at = 0.0
        self._connect_fail_count = 0

    def _record_error(self) -> None:
        """登记一次失败(错误计数 + 时间戳,内部方法,须锁内调用)。"""
        self._counters["error_count"] += 1
        self._timestamps["last_error_at"] = time.monotonic()

    def _set_error(
        self,
        message: str,
        category: ErrorCategory,
        code: Optional[int],
        record: bool = True,
    ) -> None:
        """登记失败原因三件套(内部方法,须锁内调用)。

        :param record: 是否同时计一次失败统计(门控拒绝等无网络动作的
            失败传 False,不污染 ``stats["error_count"]``)
        """
        self._last_error = message
        self._last_error_category = category
        self._last_error_code = code
        if record:
            self._record_error()

    def _clear_error(self) -> None:
        """清空失败原因三件套(内部方法,须锁内调用)。"""
        self._last_error = None
        self._last_error_category = None
        self._last_error_code = None

    def _ensure_open(self) -> None:
        """关闸检查(内部方法)::meth:`close` 之后不再受理协议调用。"""
        if self._closed:
            raise RuntimeError("客户端已关闭(close 之后不再受理调用)")

    def _guard(self) -> asyncio.Lock:
        """取当前事件循环的事务锁(内部方法,惰性创建)。"""
        loop = asyncio.get_event_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # ------------------------------------------------------------------
    # 驱动子类需要实现的协议原语
    # ------------------------------------------------------------------

    def _require_transport(self) -> AsyncBaseTransport:
        """取当前传输对象(仅事务锁内调用,内部方法)。

        :raises TransportClosedError: 连接未建立(正常流程下由基类先重连)
        """
        if self._transport is None:
            raise TransportClosedError("连接未建立")
        return self._transport

    @abstractmethod
    def _create_transport(self) -> AsyncBaseTransport:
        """创建与走线对应的**异步**传输对象(每次连接新建)。"""

    async def _after_connect(self) -> None:
        """连接建立后的**异步**钩子,默认无操作(如 FINS/TCP 握手)。"""

    @abstractmethod
    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """协议读原语(协程;内部方法)。

        成功返回解码后的值;失败抛 OSError 或内部异常
        (:mod:`omniplc.core.errors`),由 :meth:`_execute` 统一转换。
        """

    @abstractmethod
    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """协议写原语(协程;内部方法),失败语义同 :meth:`_read`。"""

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """字符串读原语,默认不支持,由驱动覆写(协程;内部方法)。

        缺省实现抛 :class:`DeviceError`(链路正常,由基类转
        ``(False, None)`` + ``last_error``),不逃逸裸异常。
        """
        raise DeviceError("当前驱动暂不支持字符串读取", 0)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """字符串写原语,默认不支持,由驱动覆写(协程;内部方法)。"""
        raise DeviceError("当前驱动暂不支持字符串写入", 0)

    def _bump_id(self, attr: str, bits: int = 16) -> int:
        """递增指定字段的协议序列号(回绕到 0),返回新值(内部方法)。

        :param attr: 内部计数器字段名(以下划线开头,如 ``"_serial"``)
        :param bits: 位宽(默认 16,即 0~65535 循环)
        """
        current = getattr(self, attr)
        next_val = (current + 1) & ((1 << bits) - 1)
        setattr(self, attr, next_val)
        return next_val

    # ------------------------------------------------------------------
    # 异步上下文管理器
    # ------------------------------------------------------------------

    async def __aenter__(self: _C) -> _C:
        """进入 ``async with`` 时自动连接,失败抛 ConnectionError(常见约定)。"""
        if not await self.connect():
            raise ConnectionError(f"连接失败:{self._last_error}")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        """退出 ``async with`` 时断开(幂等)。"""
        await self.disconnect()
