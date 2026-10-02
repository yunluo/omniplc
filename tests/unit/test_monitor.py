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
    mon, _, _, _, _ = _make(monkeypatch, points={"a": ("hr0", "ushort"), "b": ("hr1", "ushort")})
    assert mon.get("a") == PointSnapshot(MonitorQuality.INITIAL, None, None)
    assert set(mon.get_all()) == {"a", "b"}
    with pytest.raises(KeyError, match="未知点位"):
        mon.get("nope")


def test_first_success_fires_change_with_old_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """首轮成功 INITIAL→GOOD:事件触发,old=None 表示首拍。"""
    mon, _, calls, events, _ = _make(monkeypatch, results=[[(True, 20)]])
    mon._cycle()
    snapshot = mon.get("a")
    assert snapshot.quality is MonitorQuality.GOOD
    assert snapshot.value == 20 and isinstance(snapshot.value, int)  # 恒等缩放直通,int 不动
    assert snapshot.updated_at == mon.stats["last_ok_at"]
    assert len(events) == 1
    assert events[0].tag_id == "a" and events[0].old is None and events[0].new == 20  # type: ignore[union-attr]
    assert mon.stats["cycle_count"] == 1 and mon.stats["change_events"] == 1
    assert calls == [["hr0"]]


def test_stable_value_and_quality_drop_stay_quiet_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert mon.get("a") == PointSnapshot(MonitorQuality.STALE, 10, mon.get("a").updated_at)
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
    monitor = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.05, on_change=on_change)
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
        points={"u1": ("hr0", "ushort"), "f1": ("hr2", "float"), "u2": ("hr1", "ushort")},
        results=[[(True, 1), (True, 2)], [(True, 0.5)]],
    )
    mon._cycle()
    assert calls == [["hr0", "hr1"], ["hr2"]]
    assert mon.get("u2").value == 2
    assert mon.get("f1").value == 0.5


def test_tagtable_scale_applied_and_identity_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_disconnect_episode_refires_after_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败期沿:恢复复位,再失败再触发(每次失败期恰好一次)。"""
    mon, _, _, _, fires = _make(
        monkeypatch,
        results=[[(False, None)], [(True, 5)], [(False, None)]],
    )
    for _ in range(3):
        mon._cycle()
    assert len(fires) == 2
    assert mon.stats["consecutive_fails"] == 1


def test_cycle_failures_write_client_shared_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享账口径(设计决策 1):周期失败照常写客户端 last_error/error_count。

    走真事务路径(ScriptedTransport 空脚本),锁死"监视器不开记账旁路"。
    """
    client = _client()
    client.reconnect_backoff = False
    monkeypatch.setattr(client, "_create_transport", lambda: ScriptedTransport([]))
    assert client.connect() is True
    fires: List[bool] = []
    mon = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.05,
                  on_disconnect=lambda: fires.append(True))
    mon._cycle()
    assert client.last_error is not None
    assert client.stats["error_count"] >= 1
    assert fires == [True]
    assert mon.stats["fail_count"] == 1


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


def test_callback_exceptions_swallowed_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
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
    mon = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.05,
                  on_change=bad, on_disconnect=bad_disconnect)
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
    with pytest.raises(RuntimeError, match="已在运行"):
        mon.start()
    assert first_seen.wait(5.0)
    mon.stop()
    assert not mon.running
    mon.start()  # stop 后可重启(重建线程)
    assert mon.running
    mon.stop()
    assert not mon.running


def test_stop_from_inside_callback_does_not_self_join(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monitor = Monitor(client, {"a": ("hr0", "ushort")}, interval=0.05, on_change=on_change)
    monitor.start()
    thread = monitor._thread
    assert thread is not None
    thread.join(5.0)
    assert stopped.is_set()
    assert not thread.is_alive()
    assert monitor.stats["callback_errors"] == 0
    assert not monitor.running


def test_client_disconnect_stops_monitor_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_stop_detaches_from_registry() -> None:
    """工厂登记 + stop 摘除;未启动过的监视器 stop 幂等安全。"""
    client = _client()
    monitor = client.create_monitor({"a": ("hr0", "ushort")})
    assert client._monitors == [monitor]
    monitor.stop()
    assert client._monitors == []
    assert not monitor.running


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
