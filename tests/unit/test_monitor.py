"""监视器(Monitor)单元测试:质量三态/变更事件/失败期/退避联动/生命周期。

驱动手法:绝大多数用例直接调 ``monitor._cycle()`` 驱动单周期(无线程、
无真实等待);``read_many`` 按调用次序打桩。共享账(周期失败写客户端
``last_error``/``error_count``)走真事务路径(ScriptedTransport)锁死;
慢周期调度注入假时钟(``omniplc.core.monitor._monotonic``);仅生命周期
用例起真线程,用 ``threading.Event`` 等待、零固定 sleep。
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

import pytest

from omniplc import ModbusTcpClient, Monitor, MonitorQuality, PointSnapshot
from omniplc.core import monitor as monitor_mod
from omniplc.core.tag import Tag, TagTable
from scripted import ScriptedTransport


def _client() -> ModbusTcpClient:
    return ModbusTcpClient("127.0.0.1", 502, 1)


def _make(
    monkeypatch: pytest.MonkeyPatch,
    points: Optional[dict] = None,
    results: Optional[List[List[Tuple[bool, object]]]] = None,
    on_change: bool = True,
    on_disconnect: bool = True,
    deadband: object = 0.0,
):
    """搭一个最小监视器:真 ModbusTcpClient + read_many 按调用次序打桩。

    :return: ``(monitor, client, calls, events, disconnect_fires)``
    """
    client = _client()
    calls: List[List[str]] = []
    queue: List[List[Tuple[bool, object]]] = list(results or [])

    def fake_read_many(
        addresses: List[str], data_type: object
    ) -> List[Tuple[bool, object]]:
        calls.append(list(addresses))
        if queue:
            return queue.pop(0)
        return [(False, None)] * len(addresses)

    monkeypatch.setattr(client, "read_many", fake_read_many)
    events: List[object] = []
    fires: List[bool] = []
    monitor = Monitor(
        client,
        points or {"a": ("hr0", "ushort")},
        interval=0.05,
        on_change=events.append if on_change else None,
        on_disconnect=(lambda: fires.append(True)) if on_disconnect else None,
        deadband=deadband,
    )
    return monitor, client, calls, events, fires


# ----------------------------------------------------------------------
# 构造期校验
# ----------------------------------------------------------------------


def test_construction_rejects_bad_points_and_interval() -> None:
    """points 形态/区间下限/回调可调用性全部构造期拒绝。"""
    client = _client()
    with pytest.raises(ValueError, match="points 不能为空"):
        Monitor(client, {})
    with pytest.raises(ValueError, match="点位映射"):
        Monitor(client, [("a", "hr0", "ushort")])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="二元组"):
        Monitor(client, {"a": "hr0"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="interval"):
        Monitor(client, {"a": ("hr0", "ushort")}, interval=0.04)
    with pytest.raises(ValueError, match="interval"):
        Monitor(client, {"a": ("hr0", "ushort")}, interval=True)
    with pytest.raises(ValueError, match="interval"):
        Monitor(client, {"a": ("hr0", "ushort")}, interval=float("inf"))
    with pytest.raises(ValueError, match="可调用"):
        Monitor(client, {"a": ("hr0", "ushort")}, on_change=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="可调用"):
        Monitor(client, {"a": ("hr0", "ushort")}, on_disconnect="x")  # type: ignore[arg-type]


def test_construction_rejects_string_and_unknown_type() -> None:
    """STRING 构造期拒绝(批量读不收变长);未知类型名透传 ValueError。"""
    client = _client()
    with pytest.raises(ValueError, match="STRING"):
        Monitor(client, {"s": ("hr0", "string")})
    with pytest.raises(ValueError):
        Monitor(client, {"x": ("hr0", "nope")})


def test_interval_frozen_and_property() -> None:
    """周期构造期冻结,只读属性回读。"""
    mon = Monitor(_client(), {"a": ("hr0", "ushort")}, interval=0.05)
    assert mon.interval == 0.05


# ----------------------------------------------------------------------
# 快照与事件
# ----------------------------------------------------------------------


def test_initial_snapshot_and_unknown_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """首拍前快照恒 INITIAL/None/None;未知标识 KeyError(dict 惯例)。"""
    mon, _, _, _, _ = _make(
        monkeypatch, points={"a": ("hr0", "ushort"), "b": ("hr1", "ushort")}
    )
    assert mon.get("a") == PointSnapshot(MonitorQuality.INITIAL, None, None)
    assert set(mon.get_all()) == {"a", "b"}
    with pytest.raises(KeyError, match="未知点位"):
        mon.get("nope")


def test_first_success_fires_change_with_old_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首轮成功 INITIAL→GOOD:事件触发,old=None 表示首拍。"""
    mon, _, calls, events, _ = _make(monkeypatch, results=[[(True, 20)]])
    mon._cycle()
    snapshot = mon.get("a")
    assert snapshot.quality is MonitorQuality.GOOD
    assert snapshot.value == 20 and isinstance(
        snapshot.value, int
    )  # 恒等缩放直通,int 不动
    assert snapshot.updated_at == mon.stats["last_ok_at"]
    assert len(events) == 1
    assert events[0].tag_id == "a" and events[0].old is None and events[0].new == 20  # type: ignore[union-attr]
    assert mon.stats["cycle_count"] == 1 and mon.stats["change_events"] == 1
    assert calls == [["hr0"]]


def test_stable_value_and_quality_drop_stay_quiet_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """值不变不响;GOOD→STALE 边界响一次;STALE 维持不响(STALE 内部不打扰)。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        results=[
            [(True, 10)],  # 首拍:响
            [(True, 10)],  # 值不变:不响
            [(False, None)],  # GOOD→STALE 边界:响
            [(False, None)],  # STALE 维持(旧值仍在):不响
        ],
    )
    for _ in range(4):
        mon._cycle()
    assert [ev.quality for ev in events] == [MonitorQuality.GOOD, MonitorQuality.STALE]  # type: ignore[attr-defined]
    # 失败周期保旧值且不刷新时间戳(仍是周期 2 的成功时刻)
    after_good = mon.get("a")
    assert after_good.quality is MonitorQuality.STALE and after_good.value == 10
    assert after_good.updated_at == mon.stats["last_ok_at"]
    assert mon.stats["change_events"] == 2
    assert mon.stats["consecutive_fails"] == 2


def test_recovery_crosses_quality_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """STALE→GOOD 恢复:边界事件触发,连续失败清零。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        results=[[(True, 10)], [(False, None)], [(True, 10)]],
    )
    for _ in range(3):
        mon._cycle()
    assert [ev.quality for ev in events] == [  # type: ignore[attr-defined]
        MonitorQuality.GOOD,
        MonitorQuality.STALE,
        MonitorQuality.GOOD,
    ]
    assert mon.get("a").quality is MonitorQuality.GOOD
    assert mon.stats["consecutive_fails"] == 0


def test_nan_changes_do_not_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    """双 NaN 视为未变(NaN != NaN 恒真,不特判会逐周期误报);NaN→数值照常响。"""
    nan = float("nan")
    mon, _, _, events, _ = _make(
        monkeypatch,
        results=[[(True, nan)], [(True, nan)], [(True, 1.5)]],
    )
    for _ in range(3):
        mon._cycle()
    assert len(events) == 2  # 首拍 + NaN→1.5,第二拍静默
    assert events[1].new == 1.5  # type: ignore[union-attr]


def test_event_sees_new_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """先替换快照、后发事件:回调里 get 看到的就是新值。"""
    client = _client()
    seen: List[PointSnapshot] = []

    def on_change(event: object) -> None:
        seen.append(monitor.get("a"))

    queue = [[(True, 7)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0)

    monkeypatch.setattr(client, "read_many", fake)
    monitor = Monitor(
        client, {"a": ("hr0", "ushort")}, interval=0.05, on_change=on_change
    )
    monitor._cycle()
    assert seen == [monitor.get("a")]
    assert seen[0].value == 7


def test_partial_failure_is_per_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐点独立容错:单点失败只降质自己,有点成功不进失败期。"""
    mon, _, _, _, fires = _make(
        monkeypatch,
        points={"a": ("hr0", "ushort"), "b": ("hr1", "ushort")},
        results=[[(True, 1), (False, None)]],
    )
    mon._cycle()
    assert mon.get("a").quality is MonitorQuality.GOOD
    assert mon.get("b").quality is MonitorQuality.INITIAL  # 从未成功,维持 INITIAL
    assert fires == []
    assert mon.stats["consecutive_fails"] == 0


def test_points_grouped_by_data_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """按数据类型分组:同类型一次 read_many,组间保持首次出现序。"""
    mon, _, calls, _, _ = _make(
        monkeypatch,
        points={
            "u1": ("hr0", "ushort"),
            "f1": ("hr2", "float"),
            "u2": ("hr1", "ushort"),
        },
        results=[[(True, 1), (True, 2)], [(True, 0.5)]],
    )
    mon._cycle()
    assert calls == [["hr0", "hr1"], ["hr2"]]
    assert mon.get("u2").value == 2
    assert mon.get("f1").value == 0.5


def test_tagtable_scale_applied_and_identity_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TagTable 点位:scale/offset 按 read_tag 同口径;恒等缩放 int 直通。"""
    client = _client()
    queue = [[(True, 20), (True, 30)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0)

    monkeypatch.setattr(client, "read_many", fake)
    table = TagTable(
        [
            Tag(tag_id="t1", address="hr0", data_type="ushort", scale=0.1, offset=1.0),
            Tag(tag_id="t2", address="hr1", data_type="ushort"),
        ]
    )
    mon = Monitor(client, table, interval=0.05)
    mon._cycle()
    assert mon.get("t1").value == pytest.approx(3.0)
    assert mon.get("t2").value == 30
    assert isinstance(mon.get("t2").value, int)


# ----------------------------------------------------------------------
# 死区(deadband,2026-10-03 现场调研「数据质量三害」)
# ----------------------------------------------------------------------


def test_construction_rejects_bad_deadband() -> None:
    """负数/NaN/inf/bool/非数值形态/未知点位/映射值非法一律构造期拒绝。"""
    client = _client()
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband=-0.1)
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband=float("nan"))
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband=float("inf"))
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband=True)
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband="0.5")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="未知点位"):
        Monitor(client, {"a": ("hr0", "float")}, deadband={"b": 0.5})
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband={"a": -1})
    with pytest.raises(ValueError, match="deadband"):
        Monitor(client, {"a": ("hr0", "float")}, deadband={"a": True})


def test_deadband_suppresses_jitter_snapshot_still_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """死区内抖动不发事件不计 change_events,但快照值/质量照常刷新。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"t": ("hr0", "float")},
        results=[[(True, 20.0)], [(True, 20.3)], [(True, 20.4)]],
        deadband=0.5,
    )
    mon._cycle()  # 首拍:INITIAL→GOOD 照发
    assert len(events) == 1 and events[0].new == 20.0  # type: ignore[union-attr]
    mon._cycle()  # |20.3-20.0|=0.3 压住
    mon._cycle()  # |20.4-20.0|=0.4 压住
    assert len(events) == 1
    assert mon.stats["change_events"] == 1
    snap = mon.get("t")
    assert snap.quality is MonitorQuality.GOOD and snap.value == pytest.approx(20.4)


def test_deadband_anchor_is_last_reported_not_previous_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点 = 上次报告值(非上一拍快照):缓慢漂移累计越过死区才分段报告。

    20.4→20.8 单步差 0.4 压不到锚点口径(与锚点差 0.8 超死区必须报);
    若按"与上一拍比"实现,此漂移会永远压在死区内漏报——本用例锁死口径。
    """
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"t": ("hr0", "float")},
        results=[[(True, 20.0)], [(True, 20.4)], [(True, 20.8)], [(True, 21.0)]],
        deadband=0.5,
    )
    mon._cycle()  # 首拍 20.0,锚点=20.0
    mon._cycle()  # 20.4:|20.4-20.0|=0.4 压住
    mon._cycle()  # 20.8:与上一拍差 0.4 也 <0.5,但与锚点差 0.8 → 报告
    assert len(events) == 2
    # 事件载荷 old 仍是上一拍快照值(MonitorEvent 既有语义);锚点 20.0
    # 只决定"是否触发",不改变载荷
    assert events[1].old == pytest.approx(20.4) and events[1].new == pytest.approx(20.8)  # type: ignore[union-attr]
    mon._cycle()  # 21.0:与新锚点 20.8 差 0.2 压住
    assert len(events) == 2


def test_deadband_per_point_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """映射形态逐点生效:配置了死区的点压抖动,未配置的点照常逐拍报告。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"a": ("hr0", "float"), "b": ("hr1", "float")},
        results=[
            [(True, 20.0), (True, 100.0)],
            [(True, 20.3), (True, 100.4)],
            [(True, 20.1), (True, 100.8)],
        ],
        deadband={"a": 0.5},
    )
    mon._cycle()  # 双首拍
    mon._cycle()  # a 压住;b 100.4 照发
    mon._cycle()  # a |20.1-20.0|=0.1 压住;b 100.8 照发
    a_events = [e for e in events if e.tag_id == "a"]
    b_events = [e for e in events if e.tag_id == "b"]
    assert len(a_events) == 1
    assert len(b_events) == 3


def test_deadband_does_not_suppress_quality_crossing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首拍与 GOOD↔STALE 质量跨界不受死区压制(掉线恢复必须能通知)。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"t": ("hr0", "float")},
        results=[[(True, 20.0)], [(False, None)], [(True, 20.2)]],
        deadband=5.0,
    )
    mon._cycle()  # 首拍
    mon._cycle()  # GOOD→STALE 跨界:发(值保持旧值)
    mon._cycle()  # STALE→GOOD 跨界:发(值 20.2 与死区无关,跨界短路在前)
    assert len(events) == 3
    assert events[1].quality is MonitorQuality.STALE  # type: ignore[union-attr]
    assert events[2].quality is MonitorQuality.GOOD  # type: ignore[union-attr]
    assert events[2].new == pytest.approx(20.2)  # type: ignore[union-attr]


def test_deadband_skips_bool_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """bool 点不走死区(精确状态无"抖动死区"可言):变化照发。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"m": ("hr0", "bool")},
        results=[[(True, False)], [(True, True)]],
        deadband=5.0,
    )
    mon._cycle()
    mon._cycle()
    assert len(events) == 2


def test_deadband_does_not_suppress_nan_jump(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常值 ↔ NaN 跳变不受死区压制(NaN 数学比较恒 False,天然穿透)。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"t": ("hr0", "float")},
        results=[[(True, 20.0)], [(True, float("nan"))], [(True, 20.1)]],
        deadband=5.0,
    )
    mon._cycle()  # 首拍 20.0
    mon._cycle()  # → NaN:发
    mon._cycle()  # NaN → 20.1:发
    assert len(events) == 3


def test_deadband_default_zero_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 0 = 关闭:抖动逐拍报告,与无死区行为逐字节一致(回归锁)。"""
    mon, _, _, events, _ = _make(
        monkeypatch,
        points={"t": ("hr0", "float")},
        results=[[(True, 20.0)], [(True, 20.3)], [(True, 20.4)]],
    )
    mon._cycle()
    mon._cycle()
    mon._cycle()
    assert len(events) == 3


def test_deadband_with_tagtable(monkeypatch: pytest.MonkeyPatch) -> None:
    """TagTable 形态 + deadband 映射同样生效(死区参数与点位形态正交)。"""
    client = _client()
    queue = [[(True, 200)], [(True, 203)], [(True, 204)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0)

    monkeypatch.setattr(client, "read_many", fake)
    table = TagTable([Tag(tag_id="t1", address="hr0", data_type="float", scale=0.1)])
    events: List[object] = []
    mon = Monitor(
        client, table, interval=0.05, on_change=events.append, deadband={"t1": 0.5}
    )
    mon._cycle()  # 200*0.1=20.0 首拍
    mon._cycle()  # 20.3 压住
    mon._cycle()  # 20.4 压住
    assert len(events) == 1
    assert mon.get("t1").value == pytest.approx(20.4)


def test_aio_create_monitor_forwards_deadband(monkeypatch: pytest.MonkeyPatch) -> None:
    """aio 镜像 create_monitor 透传 deadband(异步面死区可用性守卫)。

    回归:deadband 落地时 ABaseClient.create_monitor 为固定签名,未透传
    新参数——aio 面传 deadband 会 TypeError(能力在异步面不可达,与
    v0.41.0 xy_octal 构造缺口同型)。
    """
    import inspect

    from omniplc.aio import AModbusTcpClient

    sig = inspect.signature(AModbusTcpClient.create_monitor)
    assert "deadband" in sig.parameters
    assert sig.parameters["deadband"].default == 0.0

    aclient = AModbusTcpClient("127.0.0.1", 502, 1)
    sync = aclient._sync
    monkeypatch.setattr(sync, "read_many", lambda addresses, t: [(True, 20)])
    mon = aclient.create_monitor({"a": ("hr0", "float")}, interval=0.05, deadband=0.5)
    assert mon._points["a"].deadband == 0.5


# ----------------------------------------------------------------------
# 失败期(on_disconnect)与共享账
# ----------------------------------------------------------------------


def test_first_cycle_fail_fires_disconnect_without_prior_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开局即坏:首轮失败即进失败期(不等"成功过"),快照维持 INITIAL。"""
    mon, _, _, _, fires = _make(monkeypatch, results=[[(False, None)]])
    mon._cycle()
    assert fires == [True]
    assert mon.get("a").quality is MonitorQuality.INITIAL
    assert mon.stats["fail_count"] == 1 and mon.stats["consecutive_fails"] == 1


def test_disconnect_episode_refires_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败期沿:恢复复位,再失败再触发(每次失败期恰好一次)。"""
    mon, _, _, _, fires = _make(
        monkeypatch,
        results=[[(False, None)], [(True, 5)], [(False, None)]],
    )
    for _ in range(3):
        mon._cycle()
    assert len(fires) == 2
    assert mon.stats["consecutive_fails"] == 1


def test_cycle_failures_write_client_shared_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享账口径(设计决策 1):周期失败照常写客户端 last_error/error_count。

    走真事务路径(ScriptedTransport 空脚本),锁死"监视器不开记账旁路"。
    """
    client = _client()
    client.reconnect_backoff = False
    monkeypatch.setattr(client, "_create_transport", lambda: ScriptedTransport([]))
    assert client.connect() is True
    fires: List[bool] = []
    mon = Monitor(
        client,
        {"a": ("hr0", "ushort")},
        interval=0.05,
        on_disconnect=lambda: fires.append(True),
    )
    mon._cycle()
    assert client.last_error is not None
    assert client.stats["error_count"] >= 1
    assert fires == [True]
    assert mon.stats["fail_count"] == 1


def test_cycle_survives_bad_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """坏地址不杀监视线程(审查 1003 P0):参数类 ValueError 按库契约从
    read_many 上抛,读取段兜底——整周期失败记账降质,线程不死。

    走真事务路径:非法地址语法在 parse_address 抛 ValueError(_execute
    不转换调用方错误),若无兜底 _cycle 直接炸出 _run、线程静默死亡。
    """
    client = _client()
    client.reconnect_backoff = False
    monkeypatch.setattr(client, "_create_transport", lambda: ScriptedTransport([]))
    assert client.connect() is True
    fires: List[bool] = []
    mon = Monitor(
        client,
        {"bad": ("no-such-address!!", "ushort")},
        interval=0.05,
        on_disconnect=lambda: fires.append(True),
    )
    mon._cycle()  # 不抛:兜底生效
    assert mon.get("bad").quality is MonitorQuality.INITIAL
    assert mon.get("bad").value is None
    assert mon.stats["fail_count"] == 1
    assert mon.stats["cycle_count"] == 1
    assert fires == [True]
    # 第二周期继续采集(线程不死),失败期不重复触发
    mon._cycle()
    assert mon.stats["fail_count"] == 2
    assert len(fires) == 1


def test_group_failure_isolates_other_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """组间隔离(review-1006 P2):坏组只废本组,同周期后续组照读不饿。

    组序 = 声明序:坏地址组排前;修复前单层 try 让其后全部组当周期不读
    (float 点恒 INITIAL),修复后按组兜底,好组照常采到。
    """
    client = _client()
    calls: List[List[str]] = []

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        calls.append(list(addresses))
        if "no-such-address!!" in addresses[0]:
            raise ValueError("无法解析地址:no-such-address!!")
        return [(True, 1.5)]

    monkeypatch.setattr(client, "read_many", fake)
    mon = Monitor(
        client,
        {"bad": ("no-such-address!!", "ushort"), "ok": ("hr0", "float")},
        interval=0.05,
    )
    mon._cycle()
    # 两组都发了读取(组 1 炸不拦组 2,组序 = 声明序)
    assert calls == [["no-such-address!!"], ["hr0"]]
    assert mon.get("bad").quality is MonitorQuality.INITIAL  # 坏组:从未成功
    assert mon.get("ok").quality is MonitorQuality.GOOD  # 好组照读
    assert mon.get("ok").value == 1.5
    assert mon.stats["fail_count"] == 0  # 有点成功不进失败期
    assert mon.stats["cycle_count"] == 1


def test_backoff_window_skips_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避窗口内跳 tick:不发报文、不记账,只计 skipped_ticks。"""
    mon, client, calls, _, _ = _make(monkeypatch)
    client._next_connect_at = time.monotonic() + 3600.0
    mon._cycle()
    assert calls == []
    assert mon.stats["skipped_ticks"] == 1
    assert mon.stats["cycle_count"] == 0
    assert mon.get("a").quality is MonitorQuality.INITIAL


# ----------------------------------------------------------------------
# 回调异常隔离与记账
# ----------------------------------------------------------------------


def test_callback_exceptions_swallowed_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回调异常吞掉计数,绝不误判为断连;失败期状态机不受回调影响。"""
    client = _client()

    def bad(_event: object) -> None:
        raise RuntimeError("回调炸了")

    def bad_disconnect() -> None:
        raise RuntimeError("断连回调也炸")

    queue = [[(True, 1)], [(True, 2)], [(False, None)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0)

    monkeypatch.setattr(client, "read_many", fake)
    mon = Monitor(
        client,
        {"a": ("hr0", "ushort")},
        interval=0.05,
        on_change=bad,
        on_disconnect=bad_disconnect,
    )
    mon._cycle()  # 首拍:change 回调炸 → 1
    mon._cycle()  # 值变:再炸 → 2
    mon._cycle()  # 全失败:GOOD→STALE 边界事件 + 失败期,两个回调各炸 → 4
    assert mon.stats["callback_errors"] == 4
    assert mon.get("a").quality is MonitorQuality.STALE
    assert mon.get("a").value == 2  # 旧值保留
    assert mon._fail_episode is True
    assert mon.stats["fail_count"] == 1


def test_stats_keys_are_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """MonitorStats 键集即契约(与 ClientStats 分立,两本账)。"""
    mon, _, _, _, _ = _make(monkeypatch, results=[[(True, 1)]])
    mon._cycle()
    expected = {
        "cycle_count",
        "fail_count",
        "consecutive_fails",
        "last_ok_at",
        "last_duration",
        "slow_cycles",
        "change_events",
        "callback_errors",
        "skipped_ticks",
        "duration_p50_ms",
        "duration_p95_ms",
        "duration_p99_ms",
    }
    assert set(mon.stats) == expected


# ----------------------------------------------------------------------
# 生命周期(真线程;Event 等待,零固定 sleep)
# ----------------------------------------------------------------------


def test_start_stop_and_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """start 幂等报错;stop 后可重启;线程随 stop 干净退出。"""
    client = _client()
    queue = [[(True, 1)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0) if queue else [(False, None)]

    monkeypatch.setattr(client, "read_many", fake)
    first_seen = threading.Event()
    events: List[object] = []

    def on_change(event: object) -> None:
        events.append(event)
        first_seen.set()

    mon = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.05, on_change=on_change)
    mon.start()
    assert mon.running
    assert client._monitors == [mon]
    with pytest.raises(RuntimeError, match="已在运行"):
        mon.start()
    assert first_seen.wait(5.0)
    mon.stop()
    assert not mon.running
    assert client._monitors == [mon]  # stop 不摘注册表(review-1006 P1)
    mon.start()  # stop 后可重启(重建线程,无需重登记)
    assert mon.running
    assert client._monitors == [mon]
    mon.stop()
    assert not mon.running


def test_stop_from_inside_callback_does_not_self_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回调内 stop:降级为只置标志不 join(自我 join 会 RuntimeError/死锁)。"""
    client = _client()
    queue = [[(True, 1)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0) if queue else [(False, None)]

    monkeypatch.setattr(client, "read_many", fake)
    stopped = threading.Event()

    def on_change(event: object) -> None:
        monitor.stop()
        stopped.set()

    monitor = Monitor(
        client, {"a": ("hr0", "ushort")}, interval=0.05, on_change=on_change
    )
    monitor.start()
    thread = monitor._thread
    assert thread is not None
    thread.join(5.0)
    assert stopped.is_set()
    assert not thread.is_alive()
    assert monitor.stats["callback_errors"] == 0
    assert not monitor.running


def test_client_disconnect_stops_monitor_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect 联动:停掉在跑监视器、清空注册表、终态不可再 start。"""
    client = _client()
    queue = [[(True, 1)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0) if queue else [(False, None)]

    monkeypatch.setattr(client, "read_many", fake)
    monitor = client.create_monitor({"a": ("hr0", "ushort")}, interval=0.05)
    assert client._monitors == [monitor]
    monitor.start()
    assert client.disconnect() is True
    assert not monitor.running
    assert client._monitors == []
    with pytest.raises(RuntimeError, match="终止"):
        monitor.start()


def test_stop_keeps_registry_entry() -> None:
    """工厂登记 + stop 保留(review-1006 P1 翻转):在册 = 未终态,重启
    无需重登记;仅 disconnect 联动终停并清空。未启动过的监视器 stop 幂等。"""
    client = _client()
    monitor = client.create_monitor({"a": ("hr0", "ushort")})
    assert client._monitors == [monitor]
    monitor.stop()  # 未启动过:幂等,线程本就不在
    assert client._monitors == [monitor]  # stop 不摘注册表
    assert not monitor.running
    monitor.start()  # 重启无需重登记
    assert client._monitors == [monitor]
    assert monitor.running
    assert client.disconnect() is True  # 联动终停 + 清空注册表
    assert client._monitors == []
    assert not monitor.running
    with pytest.raises(RuntimeError, match="终止"):
        monitor.start()


def test_direct_construction_registers_same_as_factory() -> None:
    """直接构造与工厂行为一致(审查 1003 P1):构造即注册,disconnect 联动。"""
    client = _client()
    monitor = Monitor(client, {"a": ("hr0", "ushort")})
    assert client._monitors == [monitor]
    assert client.disconnect() is True  # 未连接也幂等;联动先行
    assert client._monitors == []
    with pytest.raises(RuntimeError, match="终止"):
        monitor.start()  # 联动终态:直接构造同样生效,无旁路


def test_restart_then_disconnect_terminates_no_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """孤儿回归(review-1006 P1 场景 A2):stop→start→disconnect 后线程
    必须死——注册表在册才能被联动终停,显式断开的连接不得被监视器拖活。

    修复前 stop 即摘表、重启成孤儿:disconnect 找不到它,线程按周期
    ``read_many`` 惰性重连,把用户断开的连接复活(实测 connect_count
    0.5 秒内 2→11)。判别断言 = disconnect 返回即线程已死:联动 join
    兜底(在册才找得到),死线程不可能再发报文,"连接复活"无从发生。
    """
    client = _client()
    queue = [[(True, 1)]]

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        return queue.pop(0) if queue else [(False, None)]

    monkeypatch.setattr(client, "read_many", fake)
    mon = client.create_monitor({"a": ("hr0", "ushort")}, interval=0.05)
    mon.start()
    mon.stop()
    mon.start()  # 重启:修复前此处起孤儿线程(注册表已摘)
    assert client._monitors == [mon]  # 重启不重登记,仍在册
    assert client.disconnect() is True
    # disconnect 联动已 join:返回即线程终态
    assert client._monitors == []
    assert not mon.running
    thread = mon._thread
    assert thread is not None and not thread.is_alive()
    with pytest.raises(RuntimeError, match="终止"):
        mon.start()


# ----------------------------------------------------------------------
# 周期调度(假时钟注入,零真实等待)
# ----------------------------------------------------------------------


def test_slow_cycle_counts_and_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """读取耗时超 interval:记 slow_cycles 并立即进下一轮(不叠加等待)。

    假时钟脚本(每周期耗 2 笔:started + last_duration):start=100 →
    周期 1 读耗时到 105.5(超 deadline 101)→ slow_cycles=1 且不等待
    直接进周期 2;第 2 笔读后 stop 退出。
    """
    client = _client()
    state = {"calls": 0}

    def fake(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        state["calls"] += 1
        if state["calls"] >= 2:
            monitor.stop()
        return [(False, None)]

    monkeypatch.setattr(client, "read_many", fake)
    monitor = Monitor(client, {"a": ("hr0", "ushort")}, interval=1.0)
    times = iter([100.0, 105.0, 105.2, 105.5, 106.0, 106.1])
    monkeypatch.setattr(monitor_mod, "_monotonic", lambda: next(times))
    monitor._run()
    assert monitor.stats["slow_cycles"] == 1
    assert monitor.stats["cycle_count"] == 2


# ----------------------------------------------------------------------
# review-1020 §七丁1/丁2:错峰 jitter、耗时百分位、组异常告警合并
# ----------------------------------------------------------------------


def test_jitter_validation() -> None:
    """jitter:非数值/负数/超 interval 构造期拒绝;合法值接受。"""
    client = _client()
    with pytest.raises(ValueError, match="jitter 必须为数值"):
        Monitor(client, {"a": ("hr0", "ushort")}, jitter="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="jitter 必须为"):
        Monitor(client, {"a": ("hr0", "ushort")}, jitter=-0.1)
    with pytest.raises(ValueError, match="jitter 必须为"):
        Monitor(client, {"a": ("hr0", "ushort")}, interval=0.1, jitter=0.2)
    monitor = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.1, jitter=0.05)
    assert monitor._jitter == 0.05


def test_jitter_first_deadline_uniform_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首拍抖动取 [0, jitter) 均匀分布,且只抽取一次(相位建立后续恒定)。"""
    import omniplc.core.monitor as monitor_mod

    client = _client()
    monitor = Monitor(client, {"a": ("hr0", "ushort")}, interval=1.0, jitter=0.3)
    draws: List[float] = []

    def fake_uniform(low: float, high: float) -> float:
        assert (low, high) == (0.0, 0.3)
        draws.append(0.12)
        return 0.12

    monkeypatch.setattr(monitor_mod.random, "uniform", fake_uniform)
    times = iter([100.0 + i * 0.1 for i in range(20)])
    monkeypatch.setattr(monitor_mod, "_monotonic", lambda: next(times))

    def stop_after_first(
        addresses: List[str], data_type: object
    ) -> List[Tuple[bool, object]]:
        monitor.stop()  # 首拍后即停:只验证首轮相位,不做真实 Event 等待
        return [(False, None)]

    monkeypatch.setattr(client, "read_many", stop_after_first)
    monitor._run()
    assert len(draws) == 1, "抖动只在首拍抽取一次(相位建立后恒定)"


def test_duration_percentiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """周期耗时百分位:无周期为 None,有样本后 0<=p50<=p95<=p99。"""
    monitor, client, calls, events, fires = _make(
        monkeypatch, results=[[(True, 1)], [(True, 2)], [(True, 3)]]
    )
    assert monitor.stats["duration_p50_ms"] is None
    monitor._cycle()
    monitor._cycle()
    monitor._cycle()
    stats = monitor.stats
    p50 = stats["duration_p50_ms"]
    p95 = stats["duration_p95_ms"]
    p99 = stats["duration_p99_ms"]
    assert p50 is not None and p95 is not None and p99 is not None
    assert 0.0 <= p50 <= p95 <= p99


def test_group_error_warning_merge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """组异常告警合并:首条照发,持续期压制,达阈值发汇总报压制数,复位后直发。

    坏地址组每拍上抛(review-1006 组间隔离形态),旧实现每拍一条 WARNING
    ——长时间故障期万条刷屏(review-1020 §七丁2)。
    """
    import logging

    client = _client()

    def boom(addresses: List[str], data_type: object) -> List[Tuple[bool, object]]:
        raise ValueError("无法解析 MC 地址")

    monkeypatch.setattr(client, "read_many", boom)
    monitor = Monitor(
        client,
        {"a": ("hr0", "ushort")},
        interval=1.0,
        on_change=None,
        on_disconnect=None,
    )
    monkeypatch.setattr(Monitor, "_ALARM_MERGE_TICKS", 3)

    def warn_count() -> int:
        return sum(
            1
            for record in caplog.records
            if record.levelno == logging.WARNING and "组读取异常" in record.getMessage()
        )

    with caplog.at_level(logging.WARNING, logger="omniplc.debug"):
        monitor._cycle()  # 第 1 次:首条照发
        assert warn_count() == 1
        monitor._cycle()  # 第 2 次:压制
        monitor._cycle()  # 第 3 次:达阈值发汇总
        assert warn_count() == 2
        merged = [r for r in caplog.records if "压制" in r.getMessage()]
        assert any("已连续 3 次" in r.getMessage() for r in merged)
        # 无异常整拍复位:恢复后下次异常立即直发(不再压制)
        monkeypatch.setattr(client, "read_many", lambda a, t: [(True, 1)])
        monitor._cycle()
        assert warn_count() == 2
        monkeypatch.setattr(client, "read_many", boom)
        monitor._cycle()
        assert warn_count() == 3
