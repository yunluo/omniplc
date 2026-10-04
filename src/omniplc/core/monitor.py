"""内置监视器:客户端下的周期轮询采集(默认不启动,须显式 :meth:`Monitor.start`)。

定位:**采集节奏与消费节奏解耦**——监视器后台线程按固定周期批量读点位,
把"最新值 + 质量 + 时间戳"落成本地快照;消费方(界面/记录器)读快照,
永不阻塞、不发报文。一个周期按数据类型分组各调一次客户端
:meth:`~omniplc.core.base_client.BaseClient.read_many`(逐点独立容错,
协议级合并由各驱动白拿),N 个点位不产生 N 笔事务。

用法::

    client = ModbusTcpClient("192.168.0.10", 502, 1)
    mon = client.create_monitor(
        points={"炉温": ("hr0", "float"), "压力": ("hr2", "ushort")},
        interval=1.0,
        on_change=lambda event: print(event),
        on_disconnect=lambda: print("采集失败期开始"),
    )
    client.connect()
    mon.start()
    ...
    snap = mon.get("炉温")     # PointSnapshot(quality, value, updated_at),不发报文
    mon.stop()

口径(设计闭版 2026-10-02,逐条对齐):

- **质量三态**::attr:`MonitorQuality.INITIAL`(从未成功)/ ``GOOD`` /
  ``STALE``(有旧值,最近周期失败)。失败**保留旧值降质**,不用 ``None``
  冲掉;"失败多久了"由 :attr:`Monitor.stats` 的 ``consecutive_fails``
  表达,不进质量态;
- **变更事件**:``on_change`` 收 :class:`MonitorEvent`,触发条件 = 值变化
  (双 ``NaN`` 视为未变,防浮点 NaN 逐周期误报)或质量跨越 GOOD↔非GOOD
  边界(掉线恢复也能通知);首轮成功(INITIAL→GOOD)触发一次,
  ``old=None`` 表示首拍无旧值;
- **死区(deadband)**:数值点可配 ``deadband``(数值 = 全点统一,映射
  ``Dict[tag_id, 死区]`` = 逐点指定,0 = 关闭)——新值与**该点上次报告
  值**的绝对差小于死区视为未变(OPC-UA DataChangeFilter 同款锚点口径,
  不与上一拍比,防"单步差永远压线、漂移累计漏报"),死区内快照照常
  刷新仅压事件;首拍/质量跨界/bool/NaN 跳变不受压制;
- **事件次序**:周期末**先整体替换快照、再发事件**——回调里
  :meth:`Monitor.get` 看到的就是新值;
- **断连事件**:``on_disconnect`` 在"采集失败期"开始时触发(整周期无一点
  成功,**含 start 后首轮即失败**),恢复后复位,下个失败期再触发;
- **回调线程**:回调在监视器线程执行,异常一律吞掉并计
  ``callback_errors``,**绝不误判为断连**;回调内调用 :meth:`Monitor.get` /
  :meth:`Monitor.get_all` 随意(纯本地),调用客户端读写会排在事务锁后
  拖慢周期,回调内调用 :meth:`Monitor.stop` 合法(自动降级为只置标志不
  join,防自我 join 死锁);
- **共享账**:监视器周期与业务共用同一本客户端账——周期失败照常写
  ``last_error`` / ``error_count``;混用同一客户端时业务侧错误文本会被
  采集周期冲掉,建议**监视器独占客户端实例**;
- **坏地址不杀线程,组间隔离**:构造期校验不了地址内容(协议层无公开
  校验入口),参数类错误(如坏地址)首周期从 ``read_many`` 上抛——读取
  段**按组**兜底 ``Exception``:单组异常只废本组(组内点失败降质),
  其余组照常读取,不饿同周期后续组;全部组都失败才计整周期失败。异常
  文本入 ``omniplc.debug`` 日志(WARNING),监视线程不死;
- **退避联动**:客户端重连退避窗口内跳 tick(不发报文、不记账),计
  ``skipped_ticks``;重连本身由客户端惰性重连负责,监视器不插手;
- **生命周期**:默认不启动;``start()`` 重复调用报错;``stop()`` 后可再次
  ``start()``(重建线程,**不摘注册表**——在册即"未终态",重启无需重
  登记);客户端
  :meth:`~omniplc.core.base_client.BaseClient.disconnect` 联动停掉所有在跑
  监视器,停掉即**终态**、不可再 ``start``(要监控请重建);
- **周期调度**:monotonic 绝对 deadline 对齐防累计漂移;单周期读取耗时
  超过 ``interval`` 记 ``slow_cycles`` 并立即进下一轮(不叠加等待);
- **STRING 不支持**:批量读层口径"变长不适合混读"(MC ``read_many``
  明示字符串走 ``read_string``),构造期即拒绝。
"""

from __future__ import annotations

import math
import sys
import threading
import time
from enum import Enum
from itertools import count
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from .debug import log_warning
from .i18n import _
from .tag import TagTable
from .types import DataType, PrimitiveValue

if sys.version_info >= (3, 8):
    from typing import TypedDict
else:  # pragma: no cover - Python 3.7 无 typing.TypedDict,退化为 dict 子类
    TypedDict = dict

if TYPE_CHECKING:
    from .base_client import BaseClient

_monotonic = time.monotonic
"""模块级时钟别名(测试注入假时钟用,实现内勿直接调 ``time.monotonic``)。"""

_thread_seq = count(1)
"""监视线程名序号(跨实例递增;GIL 下 ``next`` 原子,免锁,排障可分线程)。"""


class MonitorQuality(Enum):
    """点位质量(三态)。

    - ``INITIAL``:从未成功读到(初始态;失败周期不改变它);
    - ``GOOD``:最近周期成功读到,``value`` 即最新值;
    - ``STALE``:曾经读到过,但最近周期失败——**旧值保留**,仅供参考,
      "旧到什么程度"看快照 ``updated_at`` 与 ``stats.consecutive_fails``。
    """

    INITIAL = "initial"
    GOOD = "good"
    STALE = "stale"


class PointSnapshot(NamedTuple):
    """单点位快照(:meth:`Monitor.get` 的返回类型,不可变)。

    :ivar quality: 点质量,见 :class:`MonitorQuality`
    :ivar value: 最新值(成功周期刷新;失败周期保留旧值,INITIAL 恒 ``None``)
    :ivar updated_at: 最近一次成功读取的时刻(monotonic 秒;从未成功为 ``None``)
    """

    quality: MonitorQuality
    value: Optional[PrimitiveValue]
    updated_at: Optional[float]


class MonitorEvent(NamedTuple):
    """数据变更事件(``on_change`` 回调参数,不可变;加字段不破坏回调签名)。

    :ivar tag_id: 点位标识
    :ivar old: 上一快照值(首拍为 ``None``)
    :ivar new: 本次值(失败降质周期 = 保留的旧值)
    :ivar quality: 本次质量(质量跨 GOOD↔非GOOD 边界也会触发事件)
    :ivar updated_at: 本次成功读取时刻(失败降质周期 = 旧时间戳)
    """

    tag_id: str
    old: Optional[PrimitiveValue]
    new: Optional[PrimitiveValue]
    quality: MonitorQuality
    updated_at: Optional[float]


class MonitorStats(TypedDict):
    """监视器采集健康统计快照(:attr:`Monitor.stats` 的返回类型)。

    与客户端的 :class:`~omniplc.core.base_client.ClientStats` 分立——连接
    健康与采集健康是两本账。运行期就是普通 dict(3.7 同款退化),键集即契约。

    键语义备注(review-1006 P3①):``change_events`` 计**检测到的变更数**
    ——未设置 ``on_change`` 回调同样计数(语义是"检测到",不是"已通知")。
    """

    cycle_count: int
    fail_count: int
    consecutive_fails: int
    last_ok_at: Optional[float]
    last_duration: Optional[float]
    slow_cycles: int
    change_events: int
    callback_errors: int
    skipped_ticks: int


class _Point(NamedTuple):
    """监视器内部点位记录(构造期冻结)。"""

    tag_id: str
    address: str
    data_type: DataType
    scale: float
    offset: float
    deadband: float


class Monitor:
    """客户端下的周期轮询监视器(默认不启动)。

    通常经 :meth:`~omniplc.core.base_client.BaseClient.create_monitor`
    创建;直接构造等价(构造即向客户端注册表登记,``disconnect`` 联动
    一致)。语义口径见模块 docstring。

    :param client: 宿主客户端(只调其公开面 ``read_many`` / ``next_connect_in``
        / ``retries`` / ``receive_timeout``)
    :param points: 点位映射 ``Dict[tag_id, (地址, 数据类型)]`` 或
        :class:`~omniplc.core.tag.TagTable`(``scale``/``offset`` 按
        :meth:`~omniplc.core.base_client.BaseClient.read_tag` 同口径应用);
        STRING 类型构造期拒绝
    :param interval: 采集周期(秒),下限 0.05,构造期冻结
    :param on_change: 数据变更回调(收 :class:`MonitorEvent`,监视器线程执行,
        异常吞掉计数)
    :param on_disconnect: 采集失败期开始回调(无参,监视器线程执行,异常吞掉计数)
    :param deadband: 值变化死区(2026-10-03 现场调研「数据质量三害」):数值 =
        全部数值点统一死区;映射 ``Dict[tag_id, 死区]`` = 逐点指定,未列出的
        点不启用;0(默认)= 关闭。死区口径见模块 docstring「死区」条;
        非负有限数构造期校验,未知 ``tag_id`` 拒绝
    :raises ValueError: points/interval/回调/deadband 参数非法
    """

    _INTERVAL_MIN = 0.05
    """周期下限(秒):拦住 0/负数与高频打爆链路的误配。"""

    def __init__(
        self,
        client: "BaseClient",
        points: Union[Mapping[str, Sequence[str]], TagTable],
        interval: float = 1.0,
        on_change: Optional[Callable[[MonitorEvent], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        deadband: Union[float, int, Mapping[str, float]] = 0.0,
    ) -> None:
        if not isinstance(points, Mapping):
            raise ValueError(
                _("points 必须为点位映射(Dict 或 TagTable),收到:{}").format(
                    type(points).__name__
                )
            )
        if not points:
            raise ValueError(_("points 不能为空"))
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError(_("interval 必须为数值,收到:{!r}").format(interval))
        if not math.isfinite(interval) or interval < self._INTERVAL_MIN:
            raise ValueError(
                _("interval 必须为不小于 {} 秒的有限数,收到:{!r}").format(
                    self._INTERVAL_MIN, interval
                )
            )
        if on_change is not None and not callable(on_change):
            raise ValueError(_("on_change 必须为可调用对象或 None"))
        if on_disconnect is not None and not callable(on_disconnect):
            raise ValueError(_("on_disconnect 必须为可调用对象或 None"))

        self._client = client
        self._interval = float(interval)
        self._on_change = on_change
        self._on_disconnect = on_disconnect

        self._points: Dict[str, _Point] = {}
        if isinstance(points, TagTable):
            for tag_id, tag in points.items():
                self._add_point(
                    tag_id, tag.address, tag.data_type, tag.scale, tag.offset, 0.0
                )
        else:
            for tag_id, spec in points.items():
                if not isinstance(spec, (tuple, list)) or len(spec) != 2:
                    raise ValueError(
                        _(
                            "点位 {!r} 的取值必须为 (地址, 数据类型) 二元组,收到:{!r}"
                        ).format(tag_id, spec)
                    )
                address, data_type = spec
                self._add_point(tag_id, address, data_type, 1.0, 0.0, 0.0)
        self._apply_deadband(deadband)

        # 按数据类型分组(保持首次出现序):一组一次 read_many,
        # 逐点容错语义由 read_many 契约保证
        grouped: Dict[DataType, List[_Point]] = {}
        for point in self._points.values():
            grouped.setdefault(point.data_type, []).append(point)
        self._groups: List[Tuple[DataType, List[_Point]]] = list(grouped.items())

        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._terminal = False
        self._fail_episode = False
        self._snap: Dict[str, PointSnapshot] = {
            tag_id: PointSnapshot(MonitorQuality.INITIAL, None, None)
            for tag_id in self._points
        }
        # 死区锚点 = 该点"上次报告值"(最近一次事件里的 new 值):死区判定
        # 与它比而非与上一拍快照比——否则缓慢漂移的单步差永远压在死区内,
        # 值漂到天边也不响(OPC-UA DataChangeFilter「与上次发送值比」同款口径)
        self._anchors: Dict[str, Optional[PrimitiveValue]] = {
            tag_id: None for tag_id in self._points
        }
        self._counters: Dict[str, int] = {
            "cycle_count": 0,
            "fail_count": 0,
            "consecutive_fails": 0,
            "slow_cycles": 0,
            "change_events": 0,
            "callback_errors": 0,
            "skipped_ticks": 0,
        }
        self._timestamps: Dict[str, Optional[float]] = {
            "last_ok_at": None,
            "last_duration": None,
        }
        # 构造即注册:直接构造与工厂 create_monitor 行为一致——disconnect
        # 联动对两者同样生效,不存在"绕过注册表、线程活过客户端"的旁路
        self._client._register_monitor(self)

    # ------------------------------------------------------------------
    # 构造期校验与点位解析
    # ------------------------------------------------------------------

    def _add_point(
        self,
        tag_id: str,
        address: str,
        data_type: Union[str, DataType],
        scale: float,
        offset: float,
        deadband: float,
    ) -> None:
        """校验一个点位并写入内部字典(内部方法,仅构造期调用)。"""
        if not isinstance(tag_id, str) or not tag_id:
            raise ValueError(_("点位标识必须为非空字符串,收到:{!r}").format(tag_id))
        if not isinstance(address, str) or not address.strip():
            raise ValueError(_("点位 {!r} 的地址必须为非空字符串").format(tag_id))
        # 校验仅以 strip() 拒"全空白",地址按原文存取:含首尾空白的地址由
        # 各协议 parse 层裁断(多数 strip().upper(),个别显式报错),参数类
        # 失败由 _cycle 组级兜底接住,线程不死(有意保留,review-1006 §九 D7)
        try:
            dtype = DataType.coerce(data_type)
        except ValueError as exc:
            raise ValueError(
                _("点位 {!r} 的数据类型非法:{}").format(tag_id, exc)
            ) from exc
        if dtype is DataType.STRING:
            raise ValueError(
                _(
                    "点位 {!r} 不支持 STRING:批量读不收变长字符串,请用客户端 read_string 自行轮询"
                ).format(tag_id)
            )
        self._points[tag_id] = _Point(
            tag_id, address, dtype, float(scale), float(offset), deadband
        )

    def _apply_deadband(self, deadband: Union[float, int, Mapping[str, float]]) -> None:
        """校验 deadband 参数并回填到各点(内部方法,仅构造期调用)。

        数值形态 = 全点统一死区;映射形态 = 按 ``tag_id`` 逐点指定,未列出
        的点不启用。0 表示关闭(默认,行为与无死区逐字节一致);负数/
        NaN/inf 一律构造期拒绝。
        """
        if isinstance(deadband, bool):
            raise ValueError(
                _("deadband 必须为非负有限数值或点位映射,收到:{!r}").format(deadband)
            )
        if isinstance(deadband, (int, float)):
            if not math.isfinite(deadband) or deadband < 0:
                raise ValueError(
                    _("deadband 必须为非负有限数值,收到:{!r}").format(deadband)
                )
            value = float(deadband)
            self._points = {
                tag_id: point._replace(deadband=value)
                for tag_id, point in self._points.items()
            }
            return
        if not isinstance(deadband, Mapping):
            raise ValueError(
                _(
                    "deadband 必须为非负有限数值或点位映射(Dict[tag_id, 死区]),收到:{}"
                ).format(type(deadband).__name__)
            )
        for tag_id, value in deadband.items():
            if tag_id not in self._points:
                raise ValueError(_("deadband 引用了未知点位:{!r}").format(tag_id))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    _("点位 {!r} 的 deadband 必须为非负有限数值,收到:{!r}").format(
                        tag_id, value
                    )
                )
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    _("点位 {!r} 的 deadband 必须为非负有限数值,收到:{!r}").format(
                        tag_id, value
                    )
                )
            self._points[tag_id] = self._points[tag_id]._replace(deadband=float(value))

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def interval(self) -> float:
        """采集周期(秒,构造期冻结)。"""
        return self._interval

    @property
    def running(self) -> bool:
        """监视线程是否在跑(无锁快照)。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> MonitorStats:
        """采集健康统计快照(普通 dict,键集即契约;连接账在客户端 ``stats``)。

        无锁拷贝(review-1006 P3⑤;单写者线程 + GIL,仅观测级撕裂):
        跨键可能读到跨周期中间态,单键恒一致。
        """
        return cast(MonitorStats, dict(self._counters, **self._timestamps))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动监视线程(默认不启动,采集须显式调用)。

        ``stop()`` 后可再次 ``start()``(重建线程,无需重登记——``stop``
        不摘注册表);随客户端断开终止
        (:meth:`~omniplc.core.base_client.BaseClient.disconnect` 联动)后
        为终态,不可再启动。``start``/``stop`` 跨线程并发调用未加互斥
        (控制面单线程假设,review-1006 P3③),时序未定义。

        :raises RuntimeError: 已在运行,或已随客户端断开终止
        """
        with self._state_lock:
            if self._terminal:
                raise RuntimeError(_("监视器已随客户端断开终止,不能再次启动(请重建)"))
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(_("监视器已在运行"))
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._run,
                name="omniplc-monitor-%d" % next(_thread_seq),
                daemon=True,
            )
            self._thread = thread
            # start 放锁内:保证 start() 返回后线程必已启动,disconnect 联动
            # 的 join 不会撞上"未启动线程"(竞态窗口归零)
            thread.start()

    def stop(self) -> None:
        """停止监视线程并等待收尾(join 上界约一个周期最慢事务)。

        在 ``on_change``/``on_disconnect`` 回调内调用合法:检测到处于监视
        线程时自动降级为"只置停止标志",不自我 join(否则死锁)。

        **注册表保留**(review-1006 P1):``stop()`` 不向客户端注册表摘除
        ——在册即"未终态",重启(:meth:`start`)无需重登记;仅客户端
        :meth:`~omniplc.core.base_client.BaseClient.disconnect` 联动
        (:meth:`_terminate`)为终态并清空注册表。``stop``/``start`` 控制
        面未加互斥,请单线程控制面调用(并发时序未定义,review-1006 P3③)。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(self._join_budget())

    def get(self, tag_id: str) -> PointSnapshot:
        """读单点快照(纯本地,不发报文不阻塞)。

        :param tag_id: 建监视器时的点位标识
        :raises KeyError: 标识不存在
        """
        try:
            return self._snap[tag_id]
        except KeyError:
            raise KeyError(_("未知点位:{!r}").format(tag_id)) from None

    def get_all(self) -> Dict[str, PointSnapshot]:
        """读全部点位快照(浅拷贝;:class:`PointSnapshot` 本身不可变)。"""
        return dict(self._snap)

    # ------------------------------------------------------------------
    # 内部:注册表联动
    # ------------------------------------------------------------------

    def _terminate(self) -> None:
        """随客户端断开联动的终态停机(内部方法):置终态 + 停线程。"""
        with self._state_lock:
            self._terminal = True
        self.stop()

    def _join_budget(self) -> float:
        """join 上界(秒):一个周期 + 最慢事务(重试次数 × 收包超时)。

        预算未乘组数/点数因子(review-1006 P3②):最坏周期 ≈ 组数 ×
        ``(retries + 1) × receive_timeout``(基类逐点 ``read_many`` 时再乘
        点数);超预算时 :meth:`stop` 先返回、线程仍在收尾最后一个周期,
        随后自然退出,无害。
        """
        return (
            self._interval
            + (self._client.retries + 1) * self._client.receive_timeout
            + 1.0
        )

    # ------------------------------------------------------------------
    # 内部:采集周期
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """监视线程主循环:deadline 对齐调度,慢周期立即进下一轮(内部方法)。"""
        next_deadline = _monotonic() + self._interval
        while not self._stop_event.is_set():
            self._cycle()
            if self._stop_event.is_set():
                # 显式短路:终拍不再落到下方慢周期记账(删之则末拍恰为慢
                # 周期会多计一次 slow_cycles;有意保留,review-1006 §九 D2)
                break
            now = _monotonic()
            if now >= next_deadline:
                # 慢周期(读取耗时超 interval):不叠加等待,立即进下一轮
                self._counters["slow_cycles"] += 1
                next_deadline = now + self._interval
            else:
                self._stop_event.wait(next_deadline - now)
                next_deadline += self._interval

    def _cycle(self) -> None:
        """执行一个采集周期:批量读 → 记账 → 替换快照 → 发事件(内部方法)。

        读取段**按组兜底** :class:`Exception`:参数类错误(如坏地址)按库
        契约从 ``read_many`` 上抛(:meth:`~BaseClient._execute` 不转换
        ``ValueError``),监视器不能因此死线程——单组异常只废本组(组内点
        走失败降质),其余组照常读取(组间隔离);全部组都失败才计整周期
        失败。异常文本入 ``omniplc.debug`` 日志(WARNING)供排障。

        测试直接调用本方法驱动单周期(无线程、无真实等待)。
        """
        client = self._client
        if client.next_connect_in is not None:
            # 退避门控:窗口内跳 tick,不发报文不记账——共享账不被退避期噪音冲刷
            self._counters["skipped_ticks"] += 1
            return
        started = _monotonic()
        results: Dict[str, Tuple[bool, Optional[PrimitiveValue]]] = {}
        any_ok = False
        for data_type, group in self._groups:
            try:
                pairs = client.read_many([p.address for p in group], data_type)
            except Exception as exc:
                # 组间隔离(review-1006 P2):单组参数类错误(如坏地址)只废
                # 本组(组内点走下方失败降质),其余组照常读取——一个配置
                # 笔误不得饿死同周期后续组
                log_warning(
                    getattr(client, "_debug_label", "omniplc"),
                    "monitor 组读取异常(%d 点,类型 %s):%r",
                    len(group),
                    data_type,
                    exc,
                )
                continue
            for point, pair in zip(group, pairs):
                ok, value = pair
                # 防御性强制:read_many 契约返 bool,容忍驱动/打桩的非严格
                # 返回(有意保留,review-1006 §九 D4)
                ok = bool(ok)
                results[point.tag_id] = (ok, value)
                if self._is_good_read(ok, value):
                    any_ok = True
        self._timestamps["last_duration"] = _monotonic() - started
        self._counters["cycle_count"] += 1
        if any_ok:
            self._counters["consecutive_fails"] = 0
            self._timestamps["last_ok_at"] = _monotonic()
            self._fail_episode = False
        else:
            self._counters["fail_count"] += 1
            self._counters["consecutive_fails"] += 1
            if not self._fail_episode:
                # 进入采集失败期(含 start 后首轮即失败):沿触发一次
                self._fail_episode = True
                self._fire_disconnect()
        # 快照整体替换,再发事件:回调里 get() 看到的就是新值
        previous_snap = self._snap
        new_snap: Dict[str, PointSnapshot] = {}
        events: List[MonitorEvent] = []
        ok_at = self._timestamps["last_ok_at"]
        for tag_id, point in self._points.items():
            previous = previous_snap[tag_id]
            ok, raw = results.get(tag_id, (False, None))
            # 与 _is_good_read 同谓词(内联保 mypy 对 _apply_scale 非 None
            # 实参收窄;互锚防漂移,review-1006 §九 D5)
            if ok and raw is not None:
                quality = MonitorQuality.GOOD
                value = self._apply_scale(point, raw)
                updated = ok_at
            else:
                # 失败保旧值降质:GOOD→STALE、STALE 维持(旧值仍在),从未成功维持 INITIAL
                quality = (
                    MonitorQuality.INITIAL
                    if previous.quality is MonitorQuality.INITIAL
                    else MonitorQuality.STALE
                )
                value = previous.value
                updated = previous.updated_at
            snapshot = PointSnapshot(quality, value, updated)
            new_snap[tag_id] = snapshot
            if self._changed(point, previous, snapshot, self._anchors[tag_id]):
                events.append(
                    MonitorEvent(tag_id, previous.value, value, quality, updated)
                )
                # 锚点 = 上次事件的 new 值(含质量跨界事件):判定、报告同一值源
                self._anchors[tag_id] = value
        self._snap = new_snap
        for event in events:
            self._counters["change_events"] += 1
            self._fire_change(event)

    @staticmethod
    def _is_good_read(ok: bool, value: Optional[PrimitiveValue]) -> bool:
        """成功读判定(read_many 契约 ok=True 且值非 None)。

        周期读循环(any_ok 记账)与快照循环(降质判定)两处同谓词:
        读循环调本助手,快照循环保内联(mypy 为 ``_apply_scale`` 非 None
        形参收窄)——互锚防漂移(review-1006 §九 D5)。
        """
        return bool(ok) and value is not None

    def _changed(
        self,
        point: _Point,
        previous: PointSnapshot,
        current: PointSnapshot,
        anchor: Optional[PrimitiveValue],
    ) -> bool:
        """变更判定(内部方法):质量跨越 GOOD↔非GOOD 边界,或值越过死区。

        死区口径(OPC-UA DataChangeFilter「与上次发送值比」同款):数值点
        且 ``deadband > 0`` 时,新值与**锚点**(该点上次报告值)的绝对差
        小于死区视为未变——不与上一拍快照比,否则缓慢漂移的单步差永远
        压在死区内,值漂走也不响;死区内**快照照常刷新**,仅压制事件。
        首拍(锚点 None/旧值 None)、质量跨界、bool 点与 NaN 跳变不受
        死区压制。
        """
        if (previous.quality is MonitorQuality.GOOD) != (
            current.quality is MonitorQuality.GOOD
        ):
            return True
        old, new = previous.value, current.value
        if (
            isinstance(old, float)
            and isinstance(new, float)
            and math.isnan(old)
            and math.isnan(new)
        ):
            return False
        if old == new:
            return False
        if (
            point.deadband > 0.0
            and isinstance(old, (int, float))
            and not isinstance(old, bool)
            and isinstance(new, (int, float))
            and not isinstance(new, bool)
        ):
            # bool 是精确状态不走死区;锚点(上次报告值)同样须为非 bool
            # 数值才参与比较(isinstance 内联收窄,助手谓词 mypy 不认)
            base: Union[int, float] = old
            if isinstance(anchor, (int, float)) and not isinstance(anchor, bool):
                base = anchor
            if abs(new - base) < point.deadband:
                return False
        return True

    def _apply_scale(self, point: _Point, value: PrimitiveValue) -> PrimitiveValue:
        """缩放口径与客户端 :meth:`~BaseClient.read_tag` 对齐(恒等直通保精度)。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if point.scale == 1.0 and point.offset == 0.0:
            return value
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) > 2**53
        ):
            # 非恒等缩放必经 float64:|值| > 2^53 时低位静默丢失,至少告警。
            # not isinstance(value, bool) 在此恒真(bool 已在首分支直通)——
            # 保留以求与 base_client.read_tag 同构(有意保留,review-1006 §九 D1)
            log_warning(
                getattr(self._client, "_debug_label", "omniplc"),
                "monitor 点位 %s 为 64 位整数且 |值|>2^53,非恒等缩放将丢精度:%d",
                point.tag_id,
                value,
            )
        return value * point.scale + point.offset

    def _fire_change(self, event: MonitorEvent) -> None:
        """发变更事件:回调异常吞掉计数,绝不误判为断连(内部方法)。"""
        if self._on_change is None:
            return
        try:
            self._on_change(event)
        except Exception:
            self._counters["callback_errors"] += 1

    def _fire_disconnect(self) -> None:
        """发断连事件(采集失败期开始,内部方法)。"""
        if self._on_disconnect is None:
            return
        try:
            self._on_disconnect()
        except Exception:
            self._counters["callback_errors"] += 1
