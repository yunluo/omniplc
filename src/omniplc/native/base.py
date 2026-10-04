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
内部 ``_*_locked`` 助手假定"锁已持有"。跨线程/跨事件循环使用同一实例不支持:
锁按"首次使用时所在循环"惰性创建,换循环时若**原循环正持有锁**(真并发),
显式抛 ``RuntimeError``;原循环空闲(如逐个调用各起一次 ``asyncio.run``)
则照常工作。

**首批能力面**:单点读/写 + 类型化方法 + 字符串 + 点位表;批量
(``read_many``/``read_batch`` 等)与各驱动扩展方法留后续批次。
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import weakref
from abc import ABC, abstractmethod
from types import TracebackType
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from .transport import AsyncBaseTransport
from ..core.base_client import (
    ClientStats,
    _categorize,
    _describe,
    _extract_code,
    _narrow_float,
    _narrow_int,
)
from ..core.debug import log_warning
from ..core.constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
    DEFAULT_STRING_ENCODING,
    HEARTBEAT_INTERVAL_DEFAULT,
    READ_STRING_DEFAULT_LENGTH,
    RECONNECT_BACKOFF_BASE,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_MAX_EXPONENT,
)
from ..core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    TransportClosedError,
    TransportTimeoutError,
    _CANCELLED_ERRORS,
)
from ..core.tag import Tag, TagTable
from ..core.types import DataType, PrimitiveValue
from ..core.i18n import _

_T = TypeVar("_T")
_C = TypeVar("_C", bound="AsyncBaseClient")


async def _heartbeat_loop_weak(client_ref: "weakref.ref[Any]") -> None:
    """心跳循环主体(弱引用宿主,模块级协程;review-1004 P1-1)。

    任务是循环唯一的强引用链——宿主客户端无他处引用(用户丢弃且未
    disconnect)时,下一轮解引用为 ``None`` 即静默退出,宿主可被 GC,
    不会留下持续 ping 设备的僵尸任务;与同步层 ``_heartbeat_target``
    (``core/base_client.py``)同款防线。**sleep 必须留在循环层**(逐轮
    解引用之后)——挂起点若持宿主引用,弱引用防线即失效;cancel 从
    挂起点(sleep/ping)进入,解引用保证取消后不触碰已回收宿主。
    """
    while True:
        client = client_ref()
        if client is None:
            return
        interval = client._heartbeat_interval
        if interval <= 0:
            return
        # 挂起点(sleep)之前置空局部引用:协程帧在 await 期间仍持有
        # 局部变量,不清掉则宿主在睡眠窗口内无法回收(同步层 wait 期
        # del client 同款口径);醒来后重新解引用再做事
        client = None
        await asyncio.sleep(interval)
        client = client_ref()
        if client is None:
            return
        if client._reconnect_backoff and time.monotonic() < client._next_connect_at:
            # 退避门控激活:本 tick 不发包,「未尝试」≠「尝试失败」,
            # 不计 heartbeat_fail(review-1002 P3,与同步层同口径)
            continue
        try:
            ok = await client.ping(heartbeat=True)
        except _CANCELLED_ERRORS:
            # 3.7 的 CancelledError 是 Exception 子类,显式重抛防被吞后
            # 任务带伤续跑(cancel 只投递一次)
            raise
        except Exception:
            # 参数类错误也不终止心跳:计为失败后继续
            ok = False
        client._counters["heartbeat_ok" if ok else "heartbeat_fail"] += 1
        client._timestamps["last_heartbeat_at"] = time.monotonic()


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
            "heartbeat_ok": 0,
            "heartbeat_fail": 0,
        }
        self._timestamps: Dict[str, Optional[float]] = {
            "last_error_at": None,
            "last_connect_at": None,
            "last_success_at": None,
            "last_heartbeat_at": None,
            "last_rtt": None,
        }
        # 应用层心跳(asyncio 任务形态):connect 成功后启动,_disconnect_locked
        # 中取消;间隔与语义与同步基类一致(见其「心跳保活」节)
        self._heartbeat_interval: float = HEARTBEAT_INTERVAL_DEFAULT
        self._heartbeat_task: Optional["asyncio.Task[None]"] = None

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
        # 已连接时 connect() 同样受跨循环检查:下方 _guard 的换锁分支会
        # 静默改写 _lock_loop,把事务入口 _check_loop_affinity 的比对基准
        # 洗掉——之后在新循环上对旧循环的传输收发(静默失败/串帧)永远放行
        self._check_loop_affinity()
        async with self._guard():
            # 关闸复查:close() 在等锁期间已置 _closed,排队的 connect
            # 不得复活建连(与 _execute 的锁内复查同款窗口)
            self._ensure_open()
            return await self._connect_locked()

    async def _connect_locked(self) -> bool:
        """连接实现(内部方法,**调用方须已持有事务锁**)。"""
        if self._connected:
            return True
        now = time.monotonic()
        if self._reconnect_backoff and now < self._next_connect_at:
            delay = self._next_connect_at - now
            self._set_error(
                _("连接退避中:{:.1f} 秒后允许重连").format(delay),
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
        except _CANCELLED_ERRORS:
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
                _("连接 {}:{} 失败:{}").format(
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
        except _CANCELLED_ERRORS:
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
                _("连接初始化失败:{}").format(_describe(exc)),
                _categorize(exc),
                _extract_code(exc),
            )
            # 清理钩子须在关传输**之前**(注销帧要发得出去)。
            # 取消在 3.7 是 Exception(会被本 handler 捕获)、3.8+ 是
            # BaseException(在本 handler 内部抛出)——两种形态都必须:
            # ①不吞取消(上抛);②清理照常落地(否则半开传输泄漏 FD)。
            cancelled: Optional[BaseException] = None
            try:
                await self._after_connect_failure()
            except _CANCELLED_ERRORS as cancel_exc:
                cancelled = cancel_exc
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
            self._transport = None
            self._connected = False
            if cancelled is not None:
                raise cancelled
            return False
        self._connected = True
        self._clear_error()
        self._reset_backoff()
        self._counters["connect_count"] += 1
        self._timestamps["last_connect_at"] = time.monotonic()
        # 心跳在连接成功后按需启动(幂等;不支持探活/间隔 0 时不启动)
        self._start_heartbeat()
        return True

    async def disconnect(self) -> bool:
        """断开连接(幂等)。

        :return: 是否成功
        """
        # 亲和检查先于取锁(第八轮 P2-15):跨循环 disconnect 会在错误
        # 循环上触发 transport.close() 的 call_soon(非线程安全,debug
        # 模式必炸),不能靠 _guard 静默换锁兜底
        self._check_loop_affinity()
        self._ensure_open()
        async with self._guard():
            return await self._disconnect_locked()

    async def _disconnect_locked(self) -> bool:
        """断开实现(内部方法,**调用方须已持有事务锁**)。

        尽力而为:跨循环/已关循环的传输 ``close()`` 可能抛
        ``RuntimeError``(不止 OSError)——吞掉并记错误,不中断断开流程
        (否则清理半途而废,``self._transport`` 已置 None,连接泄漏)。
        显式断开同时取消心跳任务(与同步基类同口径:断开是调用方的
        明确意图,心跳不得违背;传输失败拆连不停心跳)。
        """
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            # cancel 后 await 其真正退出(review-1002 P2):不残留 pending
            # 任务,防 run_until_complete(disconnect) 收到「Task was
            # destroyed」噪声。本协程持有事务锁 ⇒ 心跳 tick 必不在事务中
            # (锁互斥),其挂起点(sleep/等锁)均可取消,await 不会死锁。
            # CancelledError 可能来自任务取消或本协程被取消,此处收尾阶段
            # 吞掉继续清理;任务自身的其他异常同样不阻断断开流程
            task.cancel()
            try:
                await task
            except _CANCELLED_ERRORS:
                pass
            except Exception:
                pass
        transport = self._transport
        self._transport = None
        self._connected = False
        self._reset_backoff()
        if transport is None:
            return True
        try:
            transport.close()
        except Exception as exc:
            self._set_error(
                _("关闭连接失败:{}").format(exc), _categorize(exc), _extract_code(exc)
            )
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
            await self._disconnect_locked()

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态(直接读字段,不发报文、不阻塞)。

        单线程事件循环下字段更新无 ``await`` 间隙,读取即原子快照;
        线程间共享同一实例不支持(见模块 docstring)。
        """
        return self._connected

    # ------------------------------------------------------------------
    # 心跳保活(ping 探活 + 自动心跳 asyncio 任务)
    # ------------------------------------------------------------------

    # 类属性默认 False;支持探活的驱动覆写为 True(与同步基类同约定)
    _has_ping: bool = False

    @property
    def ping_supported(self) -> bool:
        """当前驱动是否支持 ping 探活(类级能力快照,语义同同步基类)。"""
        return self._has_ping

    async def ping(self, *, heartbeat: bool = False) -> bool:
        """探活:执行驱动的零副作用探测命令(语义同同步 :meth:`BaseClient.ping`)。

        走同一事务契约(:meth:`_execute`:惰性重连、退避门控、重试、
        last_error 三件套);未实现探测命令的驱动恒返回 ``False`` 并记录
        ``last_error``(不计算失败统计)。

        :keyword heartbeat: 自动心跳 tick 内部标记(review-1002 P1-2)——
            语义见 :meth:`_execute`;手动探测保持默认 ``False``
        :return: 探测命令是否成功
        :raises ValueError: 探测命令的参数类错误(如广播站号下的读)
        """
        if not self._has_ping:
            self._set_error(
                _("当前驱动未实现 ping 探活(无零副作用探测命令)"),
                ErrorCategory.UNKNOWN,
                None,
                record=False,
            )
            return False
        ok, _unused = await self._execute(self._ping_probe, heartbeat=heartbeat)
        return ok

    async def _ping_probe(self) -> Any:
        """零副作用探测命令(内部方法;支持探活的驱动覆写为协程)。"""
        raise DeviceError(_("当前驱动未实现 ping 探活"), 0)

    @property
    def heartbeat_interval(self) -> float:
        """应用层心跳间隔(秒),默认 30;0 = 关闭(语义同同步基类)。

        连接建立后由 **asyncio 任务**按本间隔自动调用 :meth:`ping`;
        写入对运行中的任务下一 tick 生效。失败计数与自愈语义与同步层
        一致:每 tick 计入 ``heartbeat_ok``/``heartbeat_fail``,传输失败
        拆连后下一 tick 经 :meth:`_execute` 惰性重连;显式
        :meth:`disconnect` 取消任务。
        """
        return self._heartbeat_interval

    @heartbeat_interval.setter
    def heartbeat_interval(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError(
                _("heartbeat_interval 必须为数字,收到:{!r}").format(seconds)
            )
        value = float(seconds)
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                _("heartbeat_interval 必须为非负有限数,收到:{!r}").format(seconds)
            )
        self._heartbeat_interval = value
        task = self._heartbeat_task
        if task is not None and not task.done():
            # 与同步层同语义(review-1002 P2):运行中的心跳按新间隔立即
            # 重启;置 0 时 _start_heartbeat 因间隔非正直接返回,等效
            # "仅停止"。旧任务在下个挂起点收到取消即退出,不残留双 tick。
            # 须在事件循环线程调用(任务在跑即说明调用方处于该循环);
            # 未连接(无任务)时仅写值,与循环无关
            task.cancel()
            self._heartbeat_task = None
            self._reap_task(task)
            self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        """按需启动心跳 asyncio 任务(内部方法;connect 成功后调用,幂等)。"""
        if self._heartbeat_interval <= 0 or not self._has_ping:
            return
        task = self._heartbeat_task
        if task is not None and not task.done():
            return
        # 弱引用宿主(review-1004 P1-1,与同步层 _heartbeat_target 同款
        # 防线):任务是循环唯一的强引用链,宿主客户端无他处引用(用户
        # 丢弃且未 disconnect)时下一轮解引用为 None 即退出,可被 GC,
        # 不会留下持续 ping 设备的僵尸任务
        client_ref = weakref.ref(self)
        self._heartbeat_task = asyncio.get_running_loop().create_task(
            _heartbeat_loop_weak(client_ref)
        )

    def _reap_task(self, task: "asyncio.Task[None]") -> None:
        """为已取消的旧心跳任务挂一次性收割(内部方法,同步上下文用)。

        cancel 只投递请求,任务实际退出在下个挂起点;setter 是同步属性
        无法 await——用 fire-and-forget 收割任务 ``await`` 到旧任务终止
        并吞掉取消异常(review-1004 P2-1:防「Task was destroyed but it
        is pending!」噪声,用户连按 setter 后立即关循环的窗口)。
        """
        if task.done():
            return

        async def _reap() -> None:
            try:
                await task
            except _CANCELLED_ERRORS:
                pass

        asyncio.get_running_loop().create_task(_reap())

    async def _heartbeat_loop(self) -> None:
        """心跳循环主体(内部协程;仅测试直接驱动用——正式入口是
        :meth:`_start_heartbeat` 起的模块级弱引用循环)。"""
        await _heartbeat_loop_weak(weakref.ref(self))

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
            raise ValueError(_("connect_timeout 必须大于 0,收到:{}").format(seconds))
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
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
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
            raise ValueError(_("retries 不能为负数,收到:{}").format(count))
        self._retries = int(count)

    @property
    def write_retries(self) -> int:
        """写操作失败后的重试次数(默认 0,防止重复写入危险动作)。"""
        return self._write_retries

    @write_retries.setter
    def write_retries(self, count: int) -> None:
        if count < 0:
            raise ValueError(_("write_retries 不能为负数,收到:{}").format(count))
        self._write_retries = int(count)

    @property
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(默认开)。"""
        return self._reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(
                _("reconnect_backoff 必须为布尔值,收到:{!r}").format(enabled)
            )
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
        ok, _unused = await self._execute(
            lambda: self._write(address, data_type_enum, value), is_write=True
        )
        return ok

    async def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取,逐点独立容错:单点失败不影响其他点。

        契约与同步 :meth:`~omniplc.core.BaseClient.read_many` 一致——基类实现
        为**逐点独立事务**,驱动可覆写为协议级批量合并(接口不变):Modbus
        按 (区, 类型) 合笔、MC 0406 位块合并、FINS 0104 多存储区读均已覆写
        (整批容错,见各驱动 ``read_many`` docstring)。

        与同步版的差别只在**逐点之间让出事件循环**:N 个点仍各成 N 笔事务,
        但每个 ``await`` 之间同循环的其他任务照常推进。这不是"并行读"——
        同一客户端上的并发仍由事务锁串行 FIFO。

        :param addresses: 地址列表
        :param data_type: 数据类型,推荐 :class:`omniplc.types.DataType` 枚举
        :return: 与地址顺序对应的 ``[(是否成功, 值)]`` 列表
        :raises ValueError: 地址/类型参数非法(任一点非法即抛出)
        """
        return [await self.read(address, data_type) for address in addresses]

    async def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读(起始地址 + 数量;契约与同步层同名方法一致)。

        基类默认实现**不支持**(明确抛 :class:`ValueError`,与同步基类
        同口径——地址递增规则协议各异,不猜);已覆写块读原语的驱动
        (Modbus/MC/FINS)单事务取回,见各驱动 ``read_range`` docstring。

        :param address: 起始协议地址
        :param count: 连续元素个数(必须 ≥ 1)
        :param data_type: 数据类型
        :return: ``(是否成功, 与地址升序对应的值列表)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 驱动未实现
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        DataType.coerce(data_type)
        raise ValueError(
            _(
                "当前驱动 {} 不支持连续批量读 read_range(起始地址+数量),"
                "请改用 read_many/read_batch 逐点列出地址"
            ).format(type(self).__name__)
        )

    async def write_many(
        self, items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]]
    ) -> List[bool]:
        """批量写入,逐点独立容错(基类实现为逐点独立事务)。

        Modbus 覆写为按 (区, 类型) 合笔;MC / FINS 维持逐点(协议无跨软元件
        单事务写原语),与同步层同面同语义。

        :param items: ``(地址, 数据类型, 值)`` 三元组序列
        :return: 与 items 顺序对应的布尔结果列表
        :raises ValueError: 地址/类型/值参数非法(任一项非法即抛出)
        """
        return [
            await self.write(address, data_type, value)
            for address, data_type, value in items
        ]

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
            raise ValueError(_("length 必须大于 0,收到:{}").format(length))
        ok, value = await self._execute(
            lambda: self._read_string(address, length, encoding)
        )
        if not ok or value is None:
            return False, None
        return True, str(value)

    async def write_bool(self, address: str, value: bool) -> bool:
        """写入布尔量(位)。

        :param value: 真值(``bool``;兼容 ``int`` 0/1 —— 其他整数显式拒绝)
        :raises ValueError: value 不是 bool/int 0/1(与同步 :meth:`BaseClient.write_bool`
            同口径:不把 ``"0"`` 这类非空字符串静默吞成 ``True``,也不把 5 当 True)
        """
        if isinstance(value, str) or not isinstance(value, int) or value not in (0, 1):
            raise ValueError(_("布尔量必须是 bool 或 int 0/1,收到:{!r}").format(value))
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
            raise ValueError(_("value 不能为空字符串"))
        ok, _unused = await self._execute(
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

        ``scale=1.0`` 且 ``offset=0.0``(默认)时原值直通,不做 float64
        往返,保留 64 位整数(``LONG``/``ULONG``)精度。

        :param tag: :class:`omniplc.tag.Tag` 实例,或已绑定表中的点位标识
        :raises ValueError: 传入标识但未绑定 TagTable,或标识不存在
        """
        resolved = self._resolve_tag(tag)
        ok, value = await self.read(resolved.address, resolved.data_type)
        if not ok or value is None:
            return False, None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True, value
        if resolved.scale == 1.0 and resolved.offset == 0.0:
            return True, value
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > 2**53
        ):
            # 非恒等缩放必经 float64:|值| > 2^53 时低位静默丢失,至少告警
            # (与同步层 read_tag 同口径,第八轮 P1-5)
            log_warning(
                getattr(self, "_debug_label", "omniplc"),
                "read_tag 点位 %s 为 64 位整数且 |值|>2^53,非恒等缩放将丢精度:%d",
                resolved.tag_id,
                value,
            )
        return True, value * resolved.scale + resolved.offset

    async def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool:
        """按点位(或标识)写入,数值自动做逆缩放 ``值 = (目标 - offset) / scale``。

        ``scale=1.0`` 且 ``offset=0.0``(默认)时不做逆缩放 float64 往返,
        保留 64 位整数精度;整数值 float 仍还原为 int(与同步版一致)。

        :param tag: Tag 实例或已绑定表中的点位标识
        :param value: 目标工程量
        :raises ValueError: 同 :meth:`read_tag`;或点位 ``scale`` 为 0
        """
        resolved = self._resolve_tag(tag)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(resolved.scale) or not math.isfinite(resolved.offset):
                # TagTable 构造校验拦不住直传 Tag 实例:scale=inf 时逆缩放
                # 结果恒 0(静默写 0 触发设备动作)、NaN 写 nan(与同步层同守卫)
                raise ValueError(
                    _(
                        "点位 {!r} 的 scale/offset 必须为有限数:scale={!r}, offset={!r}"
                    ).format(resolved.tag_id, resolved.scale, resolved.offset)
                )
            if resolved.scale == 0:
                raise ValueError(
                    _("点位 {!r} 的 scale 不能为 0,无法逆缩放").format(resolved.tag_id)
                )
            if resolved.scale == 1.0 and resolved.offset == 0.0:
                # 恒等缩放不过 float64 往返(保 64 位整数精度);但整数值 float
                # 仍要还原为 int——现场"算得 float 再写整数点位"依赖该行为
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
            else:
                scaled = (value - resolved.offset) / resolved.scale
                if DataType.coerce(resolved.data_type) in (
                    DataType.SHORT,
                    DataType.USHORT,
                    DataType.INT,
                    DataType.UINT,
                    DataType.LONG,
                    DataType.ULONG,
                ):
                    # 整数点位:工程量按 scale 逆算后取最近整数,避免
                    # (0.3-0)/0.1=2.9999… 这类浮点误差被底层整数校验拒收
                    # (与同步层 write_tag 同口径,第八轮 P1-5)
                    value = int(round(scaled))
                else:
                    value = scaled
        return await self.write(resolved.address, resolved.data_type, value)

    def _resolve_tag(self, tag: Union[str, Tag]) -> Tag:
        """把点位标识或 Tag 统一解析为 Tag(内部方法)。"""
        if isinstance(tag, Tag):
            return tag
        if self._tag_table is None:
            raise ValueError(_("未绑定 TagTable,无法按点位标识读写:{!r}").format(tag))
        try:
            return self._tag_table[tag]
        except KeyError:
            raise ValueError(_("点位表中不存在:{!r}").format(tag))

    # ------------------------------------------------------------------
    # 事务执行:惰性重连 + 重试 + 错误转换(事件循环内串行的核心)
    # ------------------------------------------------------------------

    async def _execute(
        self,
        operation: Callable[[], Awaitable[_T]],
        is_write: bool = False,
        heartbeat: bool = False,
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
        :param heartbeat: 心跳 tick 标记(review-1002 P1-2)——成功**不清**
            :attr:`last_error`、不刷 ``last_success_at``/``last_rtt``;失败
            写 ``last_error`` 但不计 ``error_count``/``device_error_count``。
            传输类真实故障(OSError 拆连)不受本标记影响,照常计数。
        :return: ``(是否成功, 值)``
        """
        self._ensure_open()
        self._check_loop_affinity()
        retries = self._write_retries if is_write else self._retries
        async with self._guard():
            # 关闸复查:close() 在等锁期间已置 _closed,排队进锁的事务
            # 不得复活建连(锁外检查与拿锁之间的窗口)
            self._ensure_open()
            self._counters["transactions"] += 1
            started = time.perf_counter()
            for attempt in range(retries + 1):
                if not self._connected:
                    # 每次重连前再查关闸:close() 在事务执行中被调用时,
                    # 循环体内不再重建连接
                    self._ensure_open()
                    if self.next_connect_in is not None:
                        # 退避门控激活:窗口内重试只会空转,直接结束——
                        # last_error 保留武装门控的那次真实失败根因
                        break
                    if not await self._connect_locked():
                        continue
                transport = self._transport
                try:
                    value = await operation()
                except _CANCELLED_ERRORS:
                    if transport is not None and transport.pending:
                        self._mark_disconnected()
                    raise
                except TransportTimeoutError as exc:
                    # 超时但 0 字节已读:链路无残渣,不拆连也不计设备错误码;
                    # 与其他传输失败一致进入重试(与同步层同口径)
                    self._set_error(
                        _describe(exc), _categorize(exc), _extract_code(exc)
                    )
                    continue
                except DeviceError as exc:
                    code = _extract_code(exc)
                    # 心跳失败写 last_error 但两计数豁免(review-1002 P1-2)
                    self._set_error(
                        _describe(exc), _categorize(exc), code, record=not heartbeat
                    )
                    if not heartbeat and code is not None and code >= 0:
                        # 只计"PLC 明确返回错误码"的次数(负码为库内诊断码,
                        # 与同步层同口径;native 侧当前无负码来源,防御一致)
                        self._counters["device_error_count"] += 1
                    return False, None
                except (OSError, OmniPLCInternalError) as exc:
                    self._set_error(
                        _describe(exc), _categorize(exc), _extract_code(exc)
                    )
                    self._mark_disconnected()
                else:
                    if transport is not None:
                        transport.mark_synced()
                    if not heartbeat:
                        # 心跳 tick 成功不清业务 last_error、不刷成功戳
                        # (review-1002 P1-2,与同步层同口径)
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
        """登记一次建连失败并推进退避门控(内部方法,须锁内调用)。

        指数先按 :data:`RECONNECT_BACKOFF_MAX_EXPONENT` 封顶再算(与同步
        基类同口径):长跑轮询下失败次数可持续增长,``2.0 ** 大指数`` 会抛
        ``OverflowError`` 逃出连接路径(退避门控本意是节流,不该把调用方
        打断)。
        """
        exponent = min(self._connect_fail_count, RECONNECT_BACKOFF_MAX_EXPONENT)
        cap = min(
            RECONNECT_BACKOFF_BASE * (RECONNECT_BACKOFF_FACTOR**exponent),
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
            raise RuntimeError(_("客户端已关闭(close 之后不再受理调用)"))

    def _guard(self) -> asyncio.Lock:
        """取当前事件循环的事务锁(内部方法,惰性创建)。

        锁在**首次使用时**按当时的事件循环创建——3.7 的 ``asyncio.Lock`` 构造
        即绑循环,若在 ``__init__`` 里建,模块级构造客户端再 ``asyncio.run`` 会
        直接炸。换循环且**原循环正持有锁**(另一循环/线程的事务在途)时抛
        ``RuntimeError``:静默换锁会让两个循环各自"串行"却互不排斥,同一连接上
        的收发会交错。连接在用但循环已换的检查在事务入口
        (:meth:`_check_loop_affinity`),那条路更要紧(传输对象绑在旧循环上)。
        """
        loop = asyncio.get_event_loop()
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        elif self._lock_loop is not loop:
            if self._lock.locked():
                self._raise_loop_mismatch()
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _check_loop_affinity(self) -> None:
        """事务入口的跨循环检查(内部方法)。

        **连接已建立**却换到别的事件循环发起事务 → 报错:传输对象(``StreamReader``
        / 已连接 UDP 套接字)绑在旧循环上,在别的循环里读写要么静默失败、要么
        串帧。未连接时换循环是安全的(会新建传输),故不拦——"逐个调用各起一次
        ``asyncio.run``"的写法在未连接场景下照常工作。

        :raises RuntimeError: 连接在用而当前循环不是它的创建循环
        """
        if not self._connected or self._lock_loop is None:
            return
        if self._lock_loop is not asyncio.get_event_loop():
            self._raise_loop_mismatch()

    @staticmethod
    def _raise_loop_mismatch() -> None:
        """抛跨循环使用的统一错误(内部方法,便于措辞单点维护)。"""
        raise RuntimeError(
            _(
                "该客户端已在另一个事件循环中使用(连接/在途事务绑在那个循环上):"
                "跨循环/跨线程共享同一实例不支持。请在原循环内 close 后重建实例"
            )
        )

    # ------------------------------------------------------------------
    # 驱动子类需要实现的协议原语
    # ------------------------------------------------------------------

    def _require_transport(self) -> AsyncBaseTransport:
        """取当前传输对象(仅事务锁内调用,内部方法)。

        :raises TransportClosedError: 连接未建立(正常流程下由基类先重连)
        """
        if self._transport is None:
            raise TransportClosedError(_("连接未建立"))
        return self._transport

    @abstractmethod
    def _create_transport(self) -> AsyncBaseTransport:
        """创建与走线对应的**异步**传输对象(每次连接新建)。"""

    async def _after_connect(self) -> None:
        """连接建立后的**异步**钩子,默认无操作(如 FINS/TCP 握手)。"""

    async def _after_connect_failure(self) -> None:
        """连接初始化失败后的尽力清理钩子,默认无操作(协程;内部方法)。

        与同步基类同名同义:持有 PLC 侧会话等资源的驱动覆写,调用点在
        :meth:`_connect_locked` 的 :meth:`_after_connect` 失败分支、**传输关闭
        之前**;取消分支不调用(取消路径不能再 await)。
        """

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
        raise DeviceError(_("当前驱动暂不支持字符串读取"), 0)

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """字符串写原语,默认不支持,由驱动覆写(协程;内部方法)。"""
        raise DeviceError(_("当前驱动暂不支持字符串写入"), 0)

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
            raise ConnectionError(_("连接失败:{}").format(self._last_error))
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        """退出 ``async with`` 时**关闸并断开**(幂等)。

        与 :meth:`omniplc.aio.ABaseClient.__aexit__` 同口径:走
        :meth:`close` 而非 :meth:`disconnect`——退出后同一实例再被调用
        必须抛 ``RuntimeError``(块外继续用已关闭客户端的 bug 立即暴露),
        而不是"重新惰性连接、悄悄成功"。
        """
        await self.close()
