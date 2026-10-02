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

import math
import random
import socket
import sys
import threading
import time
from abc import ABC, abstractmethod
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type, TypeVar, Union, cast

from .constants import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECEIVE_TIMEOUT,
    DEFAULT_STRING_ENCODING,
    HEARTBEAT_INTERVAL_DEFAULT,
    PORT_MAX,
    PORT_MIN,
    READ_STRING_DEFAULT_LENGTH,
    RECONNECT_BACKOFF_BASE,
    RECONNECT_BACKOFF_FACTOR,
    RECONNECT_BACKOFF_MAX,
    RECONNECT_BACKOFF_MAX_EXPONENT,
)
from .debug import log_warning
from .errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    ProtocolFrameError,
    TransportClosedError,
    TransportTimeoutError,
)
from .tag import Tag, TagTable
from .validation import require_int
from ..transport import BaseTransport
from .types import DataType, PrimitiveValue
from .i18n import _

_T = TypeVar("_T")
_C = TypeVar("_C", bound="BaseClient")

if sys.version_info >= (3, 8):
    from typing import TypedDict
else:  # pragma: no cover - Python 3.7 无 typing.TypedDict,退化为 dict 子类
    TypedDict = dict


class ClientStats(TypedDict):
    """连接健康统计快照(:attr:`BaseClient.stats` 的返回类型)。

    字段语义见 :attr:`BaseClient.stats`。运行期**就是普通 dict**
    (Python 3.7 无 ``typing.TypedDict``,退化为 dict 子类),声明字段只为
    类型检查与 IDE 补全服务;下游项目可直接::

        from omniplc import ClientStats

        def dump(s: ClientStats) -> None: ...
    """

    connect_count: int
    disconnect_count: int
    transactions: int
    error_count: int
    device_error_count: int
    heartbeat_ok: int
    heartbeat_fail: int
    last_error_at: Optional[float]
    last_connect_at: Optional[float]
    last_success_at: Optional[float]
    last_heartbeat_at: Optional[float]
    last_rtt: Optional[float]


def validate_endpoint(ip_address: str, port: int) -> None:
    """网络型客户端的构造参数校验(供各驱动共用)。

    :param ip_address: IP 或主机名
    :param port: 端口号
    :raises ValueError: 地址为空或端口不在 1~65535
    """
    if not ip_address or not ip_address.strip():
        raise ValueError(_("ip_address 不能为空"))
    if not PORT_MIN <= int(port) <= PORT_MAX:
        raise ValueError(_("port 必须在 {}~{} 之间,收到:{}").format(PORT_MIN, PORT_MAX, port))


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

    # 类属性默认 False;支持探活的驱动覆写为 True(MC 系按帧型在构造期
    # 以实例属性覆盖——1E/3C/4C 帧无 0101 探测命令,不启用)
    _has_ping: bool = False

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
        # 状态锁:仅保护错误三件套与统计计数(短临界区,绝不包 I/O)。
        # 与事务锁 _lock 分离——订阅回调在 asyncua 自己的线程里调用
        # _set_error,不能去抢可能被长事务持有的 _lock(会阻塞通知处理)。
        self._state_lock = threading.RLock()
        self._transport: Optional[BaseTransport] = None
        self._connected: bool = False
        self._last_error: Optional[str] = None
        self._last_error_category: Optional[ErrorCategory] = None
        self._last_error_code: Optional[int] = None
        self._reconnect_backoff: bool = True
        self._next_connect_at: float = 0.0
        self._connect_fail_count: int = 0
        self._tag_table: Optional[TagTable] = None
        # 连接健康统计:计数器(int)与时间戳(float)分两组,避免 mypy 在
        # `Dict[str, Union[int, float, None]]` 上把 `+= 1` 误判为非法运算;
        # 键集即公开契约 ClientStats,由 :attr:`stats` 合并为只读快照。
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
        # 应用层心跳:connect 成功后由守护线程按间隔驱动 ping()(见
        # 「心跳保活」节);显式 disconnect 停止,传输失败拆连**不**停止——
        # 心跳正是靠下一 tick 的 _execute 惰性重连实现自愈。
        # _heartbeat_life_lock 仅保护线程启停与间隔写入(微秒级,绝不包 I/O),
        # 与事务锁 _lock 的获取顺序恒为 _lock → _heartbeat_life_lock
        # (connect 成功路径),无反向获取,不成环。
        self._heartbeat_interval: float = HEARTBEAT_INTERVAL_DEFAULT
        self._heartbeat_stop: Optional[threading.Event] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_life_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立连接(幂等:已连接时直接返回 True)。

        失败后进入**指数退避门控**(v0.34.0):第 n 次连续失败后,
        ``uniform(0, min(0.5 × 2ⁿ, 30))`` 秒内的再次连接直接拒绝——
        时间戳比较,不发包、不 sleep。连接成功或显式
        :meth:`disconnect` 后门控与失败计数全部重置;
        :attr:`reconnect_backoff` 置 False 可整体关闭。

        :return: 是否成功
        """
        with self._lock:
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
                transport.connect()
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
                self._after_connect()
            except Exception as exc:
                # 握手/会话初始化失败:清理到干净状态,下次事务惰性重连
                self._register_connect_failure()
                self._set_error(
                    _("连接初始化失败:{}").format(_describe(exc)),
                    _categorize(exc),
                    _extract_code(exc),
                )
                # 清理钩子须在关传输**之前**(注销帧要发得出去)
                try:
                    self._after_connect_failure()
                except Exception:
                    pass
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
            with self._state_lock:
                # 计数器口径统一:与 stats 快照同锁,避免快照读到跨锁序中间态
                self._counters["connect_count"] += 1
                self._timestamps["last_connect_at"] = time.monotonic()
            # 心跳在连接成功后按需启动(幂等;不支持探活/间隔 0 时不启动)
            self._start_heartbeat()
            return True

    def disconnect(self) -> bool:
        """断开连接(幂等)。

        显式断开会**停止心跳线程**——"断开"是调用方的明确意图,心跳不得
        违背它重新建连;传输失败触发的拆连(:meth:`_mark_disconnected`)
        不停心跳,下一 tick 自动重连。

        :return: 是否成功
        """
        with self._lock:
            self._stop_heartbeat()
            transport = self._transport
            self._transport = None
            self._connected = False
            self._reset_backoff()
            if transport is None:
                return True
            try:
                transport.close()
            except OSError as exc:
                self._set_error(_("关闭连接失败:{}").format(exc), _categorize(exc), _extract_code(exc))
                # 连接事实上已终结(transport 引用已清),失败也计入断开
                with self._state_lock:
                    self._counters["disconnect_count"] += 1
                return False
            with self._state_lock:
                self._counters["disconnect_count"] += 1
            return True

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态(无锁快照,不发报文)。

        只读原子布尔量,不取事务锁——aio 层属性转发依赖此保证:事务进行
        中读取不得阻塞事件循环。
        """
        return self._connected

    # ------------------------------------------------------------------
    # 心跳保活(ping 探活 + 守护线程自动心跳)
    # ------------------------------------------------------------------

    @property
    def ping_supported(self) -> bool:
        """当前驱动是否支持 ping 探活(类级能力,无锁快照)。

        支持探活的驱动覆写了零副作用探测命令(Modbus FC08 回显、MC 0101
        CPU 型号、FINS 0601 状态读、AB Identity 读取等);不支持者
        (MX COM 会话、SDK 会话、无探测命令的串口驱动)``ping()`` 恒
        ``False`` 且自动心跳不启用。
        """
        return self._has_ping

    def ping(self, *, heartbeat: bool = False) -> bool:
        """探活:执行驱动的零副作用探测命令,返回链路与设备是否健康。

        走与读写完全相同的事务契约(:meth:`_execute`:惰性重连、退避门控、
        重试、``last_error`` 三件套):探测命令成功返回 ``True``;传输失败
        按事务口径处理(OSError 拆连、超时保留连接);PLC 报错(DeviceError)
        不断线——能应答错误码本身就证明链路活着。失败不抛异常,参数类
        错误(如 Modbus 广播站号)照库约定上抛 ``ValueError``。

        未实现探测命令的驱动恒返回 ``False`` 并记录 ``last_error``
        (不计算失败统计)。

        :keyword heartbeat: 自动心跳 tick 内部标记(review-1002 P1-2)——
            语义见 :meth:`_execute`;手动探测保持默认 ``False``,成功照旧
            清 ``last_error`` 并刷新成功戳
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
        ok, _unused = self._execute(self._ping_probe, heartbeat=heartbeat)
        return ok

    @property
    def heartbeat_interval(self) -> float:
        """应用层心跳间隔(秒),默认 30(:data:`HEARTBEAT_INTERVAL_DEFAULT`);0 = 关闭。

        连接建立后由**守护线程**按本间隔自动调用 :meth:`ping`;写入立即
        生效(运行中的心跳线程按新间隔重启)。仅对 ``ping_supported`` 为
        True 的驱动生效——不支持探活的驱动置多少都不会有线程运行。

        心跳不是免费功能,启用方应知晓:

        - 每 tick 一次完整事务,计入 ``stats["transactions"]``;结果计
          ``heartbeat_ok``/``heartbeat_fail``,时间戳记 ``last_heartbeat_at``;
        - 探测与业务事务互斥(同一把事务锁),业务长事务期间心跳排队等待,
          反之探测期间(最长 ``connect_timeout + receive_timeout`` 量级)
          业务事务也要等;
        - 失败的 tick 会写入 ``last_error``(与真实事务一致);
        - **自动重连**:传输失败拆连后,下一 tick 经 ``_execute`` 惰性重连
          (受 :attr:`reconnect_backoff` 退避门控约束,不会形成重连风暴);
        - 显式 :meth:`disconnect` 停止心跳(断开是调用方的明确意图)。
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
        with self._heartbeat_life_lock:
            self._heartbeat_interval = value
            thread = self._heartbeat_thread
            if thread is not None and thread.is_alive():
                # 运行中的心跳按新间隔重启;置 0 时 _start_heartbeat_locked
                # 因间隔非正直接返回,等效"仅停止"
                self._stop_heartbeat_locked()
                self._start_heartbeat_locked()

    def _start_heartbeat(self) -> None:
        """按需启动心跳守护线程(内部方法;connect 成功后调用,幂等)。"""
        with self._heartbeat_life_lock:
            self._start_heartbeat_locked()

    def _start_heartbeat_locked(self) -> None:
        """启动心跳线程(内部方法;须持 ``_heartbeat_life_lock``)。"""
        if self._heartbeat_interval <= 0 or not self._has_ping:
            return
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            return
        stop = threading.Event()
        self._heartbeat_stop = stop
        # stop 事件按值传入循环体:间隔写入重启线程时,旧线程持有的是旧
        # 事件引用,置位后即退出,不会残留第二个循环
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop,),
            name="omniplc-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        """停止心跳守护线程(内部方法;显式断开/间隔写入时调用,幂等)。"""
        with self._heartbeat_life_lock:
            self._stop_heartbeat_locked()

    def _stop_heartbeat_locked(self) -> None:
        """停止心跳线程(内部方法;须持 ``_heartbeat_life_lock``)。

        只置位不 join:线程可能正持有事务锁在途探测,join 会把 disconnect
        卡满一个事务时长;置位后线程退出,而**尚未进入探测**的 tick 会在
        :meth:`_heartbeat_tick` 的锁内复查中直接放弃(见该方法),因此
        disconnect 返回后不会再有心跳探测发出。
        """
        stop = self._heartbeat_stop
        if stop is not None:
            stop.set()
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        """心跳循环主体(内部方法;守护线程入口)。"""
        while not stop.wait(self._heartbeat_interval):
            self._heartbeat_tick(stop)

    def _heartbeat_tick(self, stop: threading.Event) -> None:
        """单次心跳:探测一次并计数(内部方法;任何异常都不终止循环)。

        在事务锁内复查停止事件:disconnect 与 tick 竞争同一把事务锁,锁内
        复查保证"显式断开后不再发起心跳探测"——否则在途 tick 会在
        disconnect 之后经 :meth:`_execute` 惰性重连复活连接,违背断开意图。
        ping 内层再取事务锁为 RLock 重入,同线程直接放行。
        """
        with self._lock:
            if stop.is_set():
                return
            try:
                ok = self.ping(heartbeat=True)
            except Exception:
                # ping 的参数类错误(如广播站号)也不终止心跳:计为失败后继续
                ok = False
        with self._state_lock:
            self._timestamps["last_heartbeat_at"] = time.monotonic()
            self._counters["heartbeat_ok" if ok else "heartbeat_fail"] += 1

    # ------------------------------------------------------------------
    # 可配置属性(超时/重试)
    # ------------------------------------------------------------------

    @property
    def connect_timeout(self) -> float:
        """连接超时(秒)。可在连接建立后修改,立即生效。

        **作用域分类**(第八轮 P2-5):走线型(TCP/UDP/串口)立即生效;
        会话型驱动(S7/ADS/OPC-UA/MX/MTConnect)由各驱动声明实际作用范围
        (基类只保证把新值写到传输/会话对象)。
        """
        return self._connect_timeout

    @connect_timeout.setter
    def connect_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(_("connect_timeout 必须大于 0,收到:{}").format(seconds))
        with self._lock:
            self._connect_timeout = float(seconds)
            if self._transport is not None:
                self._transport.connect_timeout = self._connect_timeout

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒)。可在连接建立后修改,立即生效。

        **加锁口径**:与在途事务互斥更新传输对象——同步层直接调用无碍;
        aio 层**不要**在事件循环线程写本属性(同步 setter 取事务锁,慢事务
        期间会阻塞循环),经 :meth:`~omniplc.aio.ABaseClient.configure`
        或事务路径设置。

        **作用域分类**(第八轮 P2-5):走线型(TCP/UDP/串口)立即生效;
        会话型驱动见各驱动 docstring(S7 经 snap7 RecvTimeout 热生效、
        ADS 经 pyads set_timeout 重发;OPC-UA/MX 不作用于已建立会话)。
        """
        return self._receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(_("receive_timeout 必须大于 0,收到:{}").format(seconds))
        with self._lock:
            self._receive_timeout = float(seconds)
            if self._transport is not None:
                self._transport.receive_timeout = self._receive_timeout

    @property
    def retries(self) -> int:
        """读操作失败后的重试次数(默认 0 = 不重试)。

        重试与**惰性重连**配合:传输失败会标记断开,重试前自动重建连接。
        接收超时(``TransportTimeoutError``,串口/UDP 的 0 字节超时)不拆连,
        直接在原连接上重发——与 TCP 超时的拆连重试口径一致。
        """
        return self._retries

    @retries.setter
    def retries(self, count: int) -> None:
        if count < 0:
            raise ValueError(_("retries 不能为负数,收到:{}").format(count))
        self._retries = require_int(count)

    @property
    def write_retries(self) -> int:
        """写操作失败后的重试次数(默认 0,防止重复写入危险动作)。

        **仅对幂等写安全**(覆盖写、位写入):写响应超时说明请求可能已被
        PLC 执行,重试即写第二次——计数累加、脉冲、步进类非幂等写开启
        本项会双写。超时重试还存在"迟到响应被当重试应答消费"的竞态窗口
        (见 :class:`~omniplc.core.errors.TransportTimeoutError`)。
        """
        return self._write_retries

    @write_retries.setter
    def write_retries(self, count: int) -> None:
        if count < 0:
            raise ValueError(_("write_retries 不能为负数,收到:{}").format(count))
        self._write_retries = require_int(count)

    @property
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(默认开)。

        PLC 断电/网线松动时,紧密轮询的调用方不再形成高频重连风暴;
        置 False 恢复"失败后立即可重连"的旧行为。
        """
        return self._reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(_("reconnect_backoff 必须为布尔值,收到:{!r}").format(enabled))
        self._reconnect_backoff = enabled

    @property
    def next_connect_in(self) -> Optional[float]:
        """距下次允许连接的剩余秒数;None = 无门控,可立即连接(无锁快照)。"""
        if not self._reconnect_backoff:
            return None
        remaining = self._next_connect_at - time.monotonic()
        return remaining if remaining > 0 else None

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败的错误描述;成功执行读写后清空为 None(无锁快照)。"""
        return self._last_error

    @property
    def last_error_category(self) -> Optional[ErrorCategory]:
        """最近一次失败的分类(成功读写后清空为 None,无锁快照)。

        取值见 :class:`~omniplc.core.errors.ErrorCategory`;传输类错误
        为 ``TRANSPORT``,PLC 明确报错为 ``DEVICE``(配
        :attr:`last_error_code` 取原始码),超时独立为 ``TIMEOUT``。
        """
        return self._last_error_category

    @property
    def last_error_code(self) -> Optional[int]:
        """最近一次失败的原始错误码(成功读写后清空为 None,无锁快照)。

        PLC 报错取协议原始码(MC 结束码/FINS 结束码/Modbus 异常码),
        传输类错误取 ``errno``,无码为 ``None``。
        """
        return self._last_error_code

    @property
    def stats(self) -> ClientStats:
        """连接健康统计快照(:class:`ClientStats`,无锁快照)。

        返回的是 ``ClientStats``(TypedDict,运行期即普通 dict;字段见下),
        每次拷贝一份,改动返回值不影响内部计数。

        - ``connect_count``:成功建连次数(含惰性重连)
        - ``disconnect_count``:关闭的连接数(显式 disconnect 与
          传输失败后的拆连都计)
        - ``transactions``:已执行的协议事务数(含失败尝试)
        - ``error_count``:失败总数(设备错误 + 传输错误 + 建连失败)
        - ``device_error_count``:PLC 明确返回错误码的次数(链路完好;
          接收超时与无码失败——能力缺失、设备侧条件——不计入)
        - ``heartbeat_ok`` / ``heartbeat_fail``:自动心跳成功/失败 tick 数
          (手动 :meth:`ping` 不计入;间隔 0 或驱动不支持探活时恒为 0)
        - ``last_error_at`` / ``last_connect_at`` / ``last_success_at`` /
          ``last_heartbeat_at``:``time.monotonic()`` 时间戳(秒)
        - ``last_rtt``:最近一次成功事务的往返耗时(秒,含 PLC 等待)

        时间戳为单调钟相对值,跨重启无意义;用于现场判断"多久前
        出错/多久没成功"。

        读取在**状态锁**内拷贝(短临界区、不涉 I/O):跨线程调用的订阅
        回调不会阻塞事件循环,也不会读到撕裂的 ``error_count``/``last_error``。
        """
        with self._state_lock:
            return cast(ClientStats, dict(self._counters, **self._timestamps))

    def _record_error(self) -> None:
        """登记一次失败(错误计数 + 时间戳,内部方法;状态锁保护)。"""
        with self._state_lock:
            self._counters["error_count"] += 1
            self._timestamps["last_error_at"] = time.monotonic()

    def _set_error(
        self,
        message: str,
        category: ErrorCategory,
        code: Optional[int],
        record: bool = True,
    ) -> None:
        """登记失败原因三件套(内部方法;状态锁保护,可从任意线程调用)。

        :param record: 是否同时计一次失败统计(门控拒绝等无网络动作的
            失败传 False,不污染 ``stats["error_count"]``)
        """
        with self._state_lock:
            self._last_error = message
            self._last_error_category = category
            self._last_error_code = code
            if record:
                self._record_error()

    def _clear_error(self) -> None:
        """清空失败原因三件套(内部方法;状态锁保护)。"""
        with self._state_lock:
            self._last_error = None
            self._last_error_category = None
            self._last_error_code = None

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
        ok, _unused = self._execute(
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

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """连续批量读:从 ``address`` 起连续 ``count`` 个同类型元素一笔取回。

        与 :meth:`read_many` 的差异:本方法按"**起始地址 + 数量**"表达,
        无需逐个列出地址——``read_range("hr0", 100, "ushort")`` 一次取回
        hr0 起连续 100 个字,与 pymodbus ``read_holding_registers(0, 100)``
        同型。"连续"依赖**数值化地址按协议步进**,只在有块读原语的驱动上
        有定义,已覆写为协议单事务:Modbus(FC 01~04)、MC(0401 成批读)、
        FINS(0101 Area Read)、S7(snap7 ``read_area``)、MX(ReadDeviceBlock)、
        TOYOPUC(CMD 1C)、MEWTOCOL(RD)。标签/节点号类寻址协议(AB CIP
        符号标签、OPC-UA NodeId、ADS 名字)与 XML 查询(MTConnect)没有
        "连续地址"概念,不提供本方法。

        基类默认实现**不支持**该操作(明确抛 :class:`ValueError`,不猜
        地址递增规则——静默重读同址会返回错值);参数校验先行(count /
        类型非法先于能力错误报告)。

        :param address: 起始协议地址(语法由驱动定义,如 ``"hr0"``、``"D100"``)
        :param count: 连续元素个数(按 ``data_type`` 计,如 FLOAT×10 = 10 个
            浮点;必须 ≥ 1)
        :param data_type: 数据类型,推荐用 :class:`omniplc.types.DataType` 枚举
        :return: ``(是否成功, 与地址升序对应的值列表)``;失败为 ``(False, None)``
        :raises ValueError: ``count`` 非正整数 / 类型非法 / 当前驱动未实现
            连续批量读
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(_("count 必须是 ≥1 的整数,收到:{!r}").format(count))
        DataType.coerce(data_type)
        raise ValueError(
            _("当前驱动 {} 不支持连续批量读 read_range(起始地址+数量),"
            "请改用 read_many/read_batch 逐点列出地址").format(type(self).__name__)
        )

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
        narrowed: Tuple[bool, Optional[PrimitiveValue]] = _narrow(
            self.read(address, DataType.BOOL), bool, "bool"
        )
        return cast(Tuple[bool, Optional[bool]], narrowed)

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
            raise ValueError(_("length 必须大于 0,收到:{}").format(length))
        ok, value = self._execute(lambda: self._read_string(address, length, encoding))
        if not ok or value is None:
            return False, None
        if not isinstance(value, str):
            # 驱动 _read_string 返回非 str(如 bytes)属库内缺陷:repr 包装
            # 会把二进制噪声伪装成"读到的字符串",显式拒绝并记录
            self._set_error(
                _("read_string 内部类型错误:驱动返回 {} 而非 str").format(type(value).__name__),
                ErrorCategory.UNKNOWN,
                None,
            )
            return False, None
        return True, value

    def write_bool(self, address: str, value: bool) -> bool:
        """写入布尔量(位)。

        :param value: 真值(``bool``;兼容 ``int`` 0/1 —— 其他整数显式拒绝:
            写布尔量却传 5 属于调用方笔误,静默按真值写入会掩盖现场问题)
        :raises ValueError: value 不是 bool/int 0/1——``"0"``/``"false"`` 这类
            非空字符串会被 ``bool()`` 吞成 ``True`` 而写反,显式拒绝而不是
            静默写错
        """
        if isinstance(value, str) or not isinstance(value, int) or value not in (0, 1):
            raise ValueError(_("布尔量必须是 bool 或 int 0/1,收到:{!r}").format(value))
        return self.write(address, DataType.BOOL, bool(value))

    def write_short(self, address: str, value: int) -> bool:
        """写入 16 位有符号整数(非 int 显式拒绝,不做静默截断——第八轮 P2-6)。"""
        return self.write(address, DataType.SHORT, require_int(value))

    def write_ushort(self, address: str, value: int) -> bool:
        """写入 16 位无符号整数(非 int 显式拒绝,不做静默截断)。"""
        return self.write(address, DataType.USHORT, require_int(value))

    def write_int(self, address: str, value: int) -> bool:
        """写入 32 位有符号整数(非 int 显式拒绝,不做静默截断)。"""
        return self.write(address, DataType.INT, require_int(value))

    def write_uint(self, address: str, value: int) -> bool:
        """写入 32 位无符号整数(非 int 显式拒绝,不做静默截断)。"""
        return self.write(address, DataType.UINT, require_int(value))

    def write_long(self, address: str, value: int) -> bool:
        """写入 64 位有符号整数(非 int 显式拒绝,不做静默截断)。"""
        return self.write(address, DataType.LONG, require_int(value))

    def write_ulong(self, address: str, value: int) -> bool:
        """写入 64 位无符号整数(非 int 显式拒绝,不做静默截断)。"""
        return self.write(address, DataType.ULONG, require_int(value))

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
            raise ValueError(_("value 不能为空字符串"))
        ok, _unused = self._execute(
            lambda: self._write_string(address, str(value), encoding), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 点位表(Tag)
    # ------------------------------------------------------------------

    def bind_tags(self, table: TagTable) -> None:
        """绑定点位表,之后可用点位标识读写::``client.read_tag("furnace_temp")``。

        :param table: :class:`omniplc.tag.TagTable` 实例
        """
        self._tag_table = table

    def read_tag(self, tag: Union[str, Tag]) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按点位(或标识)读取,数值自动应用 ``scale``/``offset``。

        ``scale=1.0`` 且 ``offset=0.0``(默认)时原值直通,不做 float64
        往返,保留 64 位整数(``LONG``/``ULONG``)精度。

        :param tag: :class:`omniplc.tag.Tag` 实例,或已绑定表中的点位标识
        :raises ValueError: 传入标识但未绑定 TagTable,或标识不存在
        """
        resolved = self._resolve_tag(tag)
        ok, value = self.read(resolved.address, resolved.data_type)
        if not ok or value is None:
            return False, None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True, value
        if resolved.scale == 1.0 and resolved.offset == 0.0:
            return True, value
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53:
            # 非恒等缩放必经 float64:|值| > 2^53 时低位静默丢失,至少告警
            log_warning(
                getattr(self, "_debug_label", "omniplc"),
                "read_tag 点位 %s 为 64 位整数且 |值|>2^53,非恒等缩放将丢精度:%d",
                resolved.tag_id,
                value,
            )
        return True, value * resolved.scale + resolved.offset

    def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool:
        """按点位(或标识)写入,数值自动做逆缩放 ``值 = (目标 - offset) / scale``。

        ``scale=1.0`` 且 ``offset=0.0``(默认)时不做逆缩放 float64 往返,
        保留 64 位整数精度。

        :param tag: Tag 实例或已绑定表中的点位标识
        :param value: 目标工程量
        :raises ValueError: 同 :meth:`read_tag`;或点位 ``scale`` 为 0
        """
        resolved = self._resolve_tag(tag)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(resolved.scale) or not math.isfinite(resolved.offset):
                # TagTable 校验只覆盖表构造路径;直接传 Tag 实例可绕过——
                # scale=inf 时逆缩放结果恒 0(静默写 0 触发设备动作)、NaN 写 nan
                raise ValueError(
                    _("点位 {!r} 的 scale/offset 必须为有限数:scale={!r}, offset={!r}").format(
                        resolved.tag_id, resolved.scale, resolved.offset
                    )
                )
            if resolved.scale == 0:
                raise ValueError(_("点位 {!r} 的 scale 不能为 0,无法逆缩放").format(resolved.tag_id))
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
                    value = int(round(scaled))
                else:
                    value = scaled
        return self.write(resolved.address, resolved.data_type, value)

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
    # 事务执行:惰性重连 + 重试 + 错误转换(线程安全核心)
    # ------------------------------------------------------------------

    def _execute(
        self, operation: Callable[[], _T], is_write: bool = False, heartbeat: bool = False
    ) -> Tuple[bool, Optional[_T]]:
        """事务模板:在事务锁内执行一次协议操作(内部方法)。

        - 断线时先惰性重连(失败则本次直接返回失败)
        - 传输/协议失败标记断开,并按 ``retries``/``write_retries`` 重试
        - 接收超时(:class:`TransportTimeoutError`,0 字节已读 = 链路无残渣)
          不拆连、不计 ``device_error_count``(它不是 PLC 错误码),但与其他
          传输失败一样按 ``retries``/``write_retries`` 重试
        - PLC 明确返回错误码(DeviceError)不断线、不重试——链路是好的
        - 传输/协议/设备类异常转换为 ``(False, None)``,原因写入
          :attr:`last_error`;调用方错误(``ValueError`` 等)与编程错误
          (``struct.error``/``KeyError``)仍直接抛出(锁正常释放)

        :param operation: 无参可调用,成功返回值,失败抛内部异常/OSError
        :param is_write: 是否写操作(决定重试次数与防重复写入语义)
        :param heartbeat: 心跳 tick 标记(review-1002 P1-2)——成功**不清**
            :attr:`last_error`、不刷 ``last_success_at``/``last_rtt``(业务
            监测字段不被心跳劫持);失败写 ``last_error`` 但不计
            ``error_count``/``device_error_count``(不支持探活命令的从站
            每 tick 确定性报错是已知形态,不是链路劣化)。传输类真实故障
            (OSError 拆连)不受本标记影响,照常计数。
        :return: ``(是否成功, 值)``
        """
        retries = self._write_retries if is_write else self._retries
        with self._lock:
            with self._state_lock:
                # 计数器口径统一:全部计数只在状态锁内变更,stats 快照
                # (状态锁)不再可能读到跨锁序中间态
                self._counters["transactions"] += 1
            started = time.perf_counter()
            for attempt in range(retries + 1):
                if not self._connected:
                    if self.next_connect_in is not None:
                        # 退避门控激活:窗口内重试只会空转,直接结束——
                        # last_error 保留武装门控的那次真实失败根因
                        break
                    if not self.connect():
                        # connect() 内部已记录 last_error;标记断开后重试即重连
                        continue
                try:
                    value = operation()
                    with self._state_lock:
                        if not heartbeat:
                            # 心跳 tick 成功不清业务 last_error、不刷成功戳
                            # (review-1002 P1-2:监测字段语义不被心跳劫持)
                            self._clear_error()
                            self._timestamps["last_success_at"] = time.monotonic()
                            self._timestamps["last_rtt"] = time.perf_counter() - started
                    return True, value
                except TransportTimeoutError as exc:
                    # 超时但 0 字节已读:链路无残渣,不拆连也不计设备错误码;
                    # 与其他传输失败一致进入重试(串口/UDP 与 TCP 口径统一)。
                    # 注意:重试与重试之间存在"迟到响应落入接收缓冲"的竞态
                    # 窗口(旧响应被当重试应答消费),写重试另有双写风险——
                    # 见 TransportTimeoutError docstring 与 write_retries。
                    self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
                    continue
                except DeviceError as exc:
                    code = _extract_code(exc)
                    # 心跳失败写 last_error 但两计数豁免(review-1002 P1-2);
                    # record=False 同时豁免 error_count 与 last_error_at
                    self._set_error(
                        _describe(exc), _categorize(exc), code, record=not heartbeat
                    )
                    if not heartbeat and code is not None and code >= 0:
                        # 只计"PLC 明确返回错误码"的次数:code=0 的无码失败
                        # (能力缺失、设备侧条件、超时)与负码诊断
                        # (本地缓冲/配置问题,如 UDP 报文超长 -10040)不计入
                        with self._state_lock:
                            self._counters["device_error_count"] += 1
                    return False, None
                except (OSError, OmniPLCInternalError) as exc:
                    self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
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
            with self._state_lock:
                self._counters["disconnect_count"] += 1

    def _register_connect_failure(self) -> None:
        """登记一次建连失败并推进退避门控(内部方法,须锁内调用)。"""
        exponent = min(self._connect_fail_count, RECONNECT_BACKOFF_MAX_EXPONENT)
        cap = min(
            RECONNECT_BACKOFF_BASE * (RECONNECT_BACKOFF_FACTOR ** exponent),
            RECONNECT_BACKOFF_MAX,
        )
        self._next_connect_at = time.monotonic() + random.uniform(0.0, cap)
        self._connect_fail_count += 1

    def _reset_backoff(self) -> None:
        """清空退避门控(连接成功或显式断开时,内部方法,须锁内调用)。"""
        self._next_connect_at = 0.0
        self._connect_fail_count = 0

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self: _C) -> _C:
        """进入 with 时自动连接,失败抛 ConnectionError(常见约定)。"""
        if not self.connect():
            raise ConnectionError(_("连接失败:{}").format(self._last_error))
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
            raise TransportClosedError(_("连接未建立"))
        return self._transport

    @abstractmethod
    def _create_transport(self) -> BaseTransport:
        """创建与走线对应的传输对象(每次连接新建)。"""

    def _after_connect(self) -> None:
        """连接建立后的钩子,默认无操作(FINS/TCP 用它做握手)。"""

    def _after_connect_failure(self) -> None:
        """连接初始化失败后的尽力清理钩子,默认无操作(内部方法)。

        持有 PLC 侧会话等资源的驱动覆写(如 AB 注销 CIP 会话)。调用点在
        :meth:`connect` 的 :meth:`_after_connect` 失败分支、**传输关闭之前**
        ——传输此时仍可用,注销帧才发得出去;本方法抛出的异常由调用方吞掉
        (清理失败不得掩盖原始连接失败)。
        """

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
        raise DeviceError(_("当前驱动暂不支持字符串读取"), 0)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """字符串写原语,默认不支持,由驱动覆写(内部方法)。

        缺省实现语义同 :meth:`_read_string`。
        """
        raise DeviceError(_("当前驱动暂不支持字符串写入"), 0)

    def _ping_probe(self) -> Any:
        """零副作用探测命令(内部方法;支持探活的驱动覆写)。

        缺省实现抛 :class:`DeviceError`(链路正常,由 :meth:`_execute` 转
        ``(False, None)`` + ``last_error``,不逃逸裸异常),与
        :meth:`_read_string` 同款;``_has_ping`` 为 False 时不会被调用。
        返回值只作探活成功依据(:meth:`ping` 不消费具体值)。
        """
        raise DeviceError(_("当前驱动未实现 ping 探活"), 0)

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
    """把异常转换为可读的 last_error 文本(内部函数)。

    超时类文本不加类名前缀:``TransportTimeoutError`` 的消息由传输层自撰
    且已含上下文(如"串口读取超时(receive_timeout=1.0)"),
    ``socket.timeout`` 统一表述为"通信超时:…"。
    """
    text = str(exc).strip()
    if isinstance(exc, TransportTimeoutError):
        return text or _("通信超时")
    if isinstance(exc, socket.timeout):
        return _("通信超时:{}").format(text or _("receive_timeout 到期"))
    return _("{}:{}").format(type(exc).__name__, text) if text else type(exc).__name__


def _categorize(exc: BaseException) -> ErrorCategory:
    """把异常映射为失败分类(内部函数;规则顺序敏感,勿调换)。

    ``TransportTimeoutError`` 是 ``DeviceError`` 子类、``socket.timeout``
    是 ``OSError`` 子类,必须先判窄类型;连接被拒/被重置/DNS 失败
    (``ConnectionRefusedError``/``ConnectionResetError``/``socket.gaierror``)
    同为 ``OSError`` 子类,归入传输类无需单列;新增异常类型须同步本表
    (见 ``ErrorCategory`` docstring)。
    """
    if isinstance(exc, (TransportTimeoutError, socket.timeout)):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, ProtocolFrameError):
        return ErrorCategory.PROTOCOL
    if isinstance(exc, DeviceError):
        return ErrorCategory.DEVICE
    if isinstance(exc, (OSError, TransportClosedError)):
        return ErrorCategory.TRANSPORT
    return ErrorCategory.UNKNOWN


def _extract_code(exc: BaseException) -> Optional[int]:
    """提取原始错误码:DeviceError 取协议码,OSError 取 errno,其余 None。

    ``DeviceError`` 的 ``code=0`` 表示"无具体错误码"(链路正常——能力缺失、
    设备应答异常、超时等),按无码返回 ``None``;正码照原样返回(协议原始
    码);**负码为库内诊断码**(本地缓冲/配置类问题,如 UDP 报文超长的
    ``-WSAEMSGSIZE``),透传给 ``last_error_code`` 但基类不把它计入
    ``device_error_count``(非 PLC 报错)。
    """
    if isinstance(exc, DeviceError):
        return exc.code or None
    if isinstance(exc, OSError):
        return exc.errno
    return None


def _narrow_int(
    result: Tuple[bool, Optional[PrimitiveValue]]
) -> Tuple[bool, Optional[int]]:
    """把通用读结果收窄为整数签名(内部函数)。"""
    narrowed: Tuple[bool, Optional[PrimitiveValue]] = _narrow(result, int, "整数")
    return cast(Tuple[bool, Optional[int]], narrowed)


def _narrow_float(
    result: Tuple[bool, Optional[PrimitiveValue]]
) -> Tuple[bool, Optional[float]]:
    """把通用读结果收窄为浮点签名(内部函数)。"""
    narrowed: Tuple[bool, Optional[PrimitiveValue]] = _narrow(result, float, "浮点数")
    return cast(Tuple[bool, Optional[float]], narrowed)


def _narrow(
    result: Tuple[bool, Optional[PrimitiveValue]],
    expected_type: Type[PrimitiveValue],
    type_name: str,
) -> Tuple[bool, Optional[PrimitiveValue]]:
    """类型收窄的统一实现(内部函数)。

    底层读成功(``ok=True``)而值类型不符时返回 ``(False, None)``——
    此时 ``_clear_error`` 已在成功路径执行,``last_error`` 为空。类型
    不符属**驱动返回了与声明类型不符的值**(库内缺陷)而非通信失败,
    不伪造通信错误。

    ``bool`` 排除仅对整数收窄生效(bool 是 int 子类,``read_ushort``
    不得把 True 当 1 放行);``expected_type is bool`` 时布尔值恰是
    目标类型,必须放行。
    """
    ok, value = result
    if not ok or value is None:
        return False, None
    if expected_type is bool:
        if not isinstance(value, bool):
            return False, None
        return True, value
    if isinstance(value, bool) or not isinstance(value, expected_type):
        return False, None
    return True, value
