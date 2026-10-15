"""写安全与信号原语测试(review-1020 §七甲/乙/丙)。

覆盖:read_only 只读模式(sync/native/aio)、写白名单、
write_and_verify 写后回读(float 位型比对)、wait_value 等信号、
拆连/设备错误现场快照(incident_frames)、黑匣子导出文本。
"""

from __future__ import annotations

import asyncio
import struct
from typing import List, Optional, Sequence, Tuple, Union

import pytest

from omniplc import (
    export_recorded_frames,
    format_frame_records,
    recorded_frames,
    set_frame_recorder,
)
from omniplc.aio import AModbusTcpClient
from omniplc.core.base_client import BaseClient
from omniplc.core.debug import (
    RECV_MARK,
    SEND_MARK,
    clear_recorded_frames,
    log_frame,
)
from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.core.tag import Tag, TagTable
from omniplc.core.types import DataType, PrimitiveValue
from omniplc.native import AsyncModbusTcpClient
from omniplc.transport import BaseTransport


# ----------------------------------------------------------------------
# 脚本化驱动(同 test_base_client 手法;读写值可注入)
# ----------------------------------------------------------------------


class _Transport(BaseTransport):
    """带黑匣子挂钩的传输(真实走线传输在 send/recv 调 log_frame,同款)。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: List[bytes] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        log_frame("script://client", SEND_MARK, data)
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        data = b"\x00" * size
        log_frame("script://client", RECV_MARK, data)
        return data


class _ScriptedClient(BaseClient):
    """脚本化驱动:回读值独立可注入;读写共用失败注入队列。

    ``_read`` 经传输做一次 send/recv(真实驱动形态,黑匣子挂钩才会
    留存帧,事故快照测试依赖此)。
    """

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 502)
        self.written: List[PrimitiveValue] = []
        self.readback: PrimitiveValue = 1
        self.read_calls = 0
        self._fail_next_op: List[BaseException] = []

    def script_failure(self, exc: BaseException) -> None:
        """注入一次读/写失败。"""
        self._fail_next_op.append(exc)

    def _create_transport(self) -> BaseTransport:
        return _Transport()

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        self.read_calls += 1
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        self._require_transport().send(b"\x01\x02")
        self._require_transport().recv(2)
        if data_type is DataType.BOOL:
            return bool(self.readback)
        return self.readback

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        self.written.append(value)


async def _noop() -> None:
    return None


# ----------------------------------------------------------------------
# 甲1:read_only 只读模式
# ----------------------------------------------------------------------


class TestReadOnly:
    def test_default_off_and_type_guard(self) -> None:
        client = _ScriptedClient()
        assert client.read_only is False
        with pytest.raises(ValueError):
            client.read_only = 1  # type: ignore[assignment]
        assert client.read_only is False

    def test_write_entry_rejected(self) -> None:
        client = _ScriptedClient()
        client.read_only = True
        with pytest.raises(RuntimeError, match="只读"):
            client.write("hr0", "ushort", 1)
        with pytest.raises(RuntimeError, match="只读"):
            client.write_ushort("hr0", 1)
        with pytest.raises(RuntimeError, match="只读"):
            client.write_string("hr0", "abc")
        assert client.written == []  # 零下发

    def test_driver_specific_write_rejected_at_execute(self) -> None:
        """驱动特有写(不经 write() 的 _execute(is_write=True) 路径)同样被拒。"""
        client = _ScriptedClient()
        client.read_only = True

        async def _pass() -> None:
            return None

        with pytest.raises(RuntimeError, match="只读"):
            client._execute(_pass, is_write=True)
        # 读不受影响
        client.read_only = False
        ok, _value = client.read("hr0", "ushort")
        assert ok is True

    def test_native_mirror(self) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        assert client.read_only is False
        with pytest.raises(ValueError):
            client.read_only = "yes"  # type: ignore[assignment]
        client.read_only = True

        async def _pass() -> None:
            return None

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_ushort("hr0", 1)
            with pytest.raises(RuntimeError, match="只读"):
                await client._execute(_pass, is_write=True)

        asyncio.run(scenario())

    def test_aio_mirror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sync_client = _ScriptedClient()

        def _fake_transport() -> BaseTransport:
            return _Transport()

        monkeypatch.setattr(sync_client, "_create_transport", _fake_transport)
        aio_client = AModbusTcpClient.__new__(AModbusTcpClient)
        AModbusTcpClient.__bases__[0].__init__(aio_client, sync_client)
        assert aio_client.read_only is False
        aio_client.read_only = True
        assert sync_client.read_only is True

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await aio_client.write_ushort("hr0", 1)

        asyncio.run(scenario())


# ----------------------------------------------------------------------
# 甲2:写白名单
# ----------------------------------------------------------------------


class TestWriteWhitelist:
    def test_enabled_without_table_rejects_all(self) -> None:
        client = _ScriptedClient()
        client.write_whitelist = True
        with pytest.raises(ValueError, match="bind_tags"):
            client.write("hr0", "ushort", 1)
        assert client.written == []

    def test_outside_table_rejected(self) -> None:
        client = _ScriptedClient()
        client.bind_tags(TagTable([Tag("炉温", "D100", "float")]))
        client.write_whitelist = True
        with pytest.raises(ValueError, match="白名单"):
            client.write("hr0", "ushort", 1)
        assert client.written == []
        # 表内地址放行
        assert client.write("D100", "float", 1.0) is True
        assert client.written == [1.0]

    def test_write_tag_passes_table_address(self) -> None:
        client = _ScriptedClient()
        client.bind_tags(TagTable([Tag("启停", "M10", "bool")]))
        client.write_whitelist = True
        assert client.write_tag("启停", True) is True

    def test_type_guard_and_rebind_refreshes(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError):
            client.write_whitelist = "on"  # type: ignore[assignment]
        client.bind_tags(TagTable([Tag("a", "D0", "short")]))
        client.bind_tags(TagTable([Tag("b", "D1", "short")]))
        client.write_whitelist = True
        assert client.write("D1", "short", 1) is True
        with pytest.raises(ValueError, match="白名单"):
            client.write("D0", "short", 1)  # 旧表地址已随重绑失效

    def test_native_mirror(self) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        client.write_whitelist = True
        with pytest.raises(ValueError, match="bind_tags"):
            asyncio.run(client.write_ushort("hr0", 1))

    def test_type_width_mismatch_rejected(self) -> None:
        """白名单第二道判据:写入类型宽度须与点位表声明一致(review-1043 乙-2)。

        点位表声明 ``D100 = ushort``(2 字节)时,``write("D100", "double")``
        会从该地址起连写 4 字、覆盖邻点——原判据只比地址字面量,拦不住这类
        「点位表里类型写错」的现场高频误操作。同宽不同名(SHORT↔USHORT)
        不拦:那是范围问题,由类型转换层负责。
        """
        client = _ScriptedClient()
        client.bind_tags(TagTable([Tag("液位", "D100", "ushort")]))
        client.write_whitelist = True
        with pytest.raises(ValueError, match="类型宽度不符"):
            client.write("D100", "double", 1.0)
        with pytest.raises(ValueError, match="类型宽度不符"):
            client.write("D100", "float", 1.0)
        assert client.written == []  # 零下发
        assert client.write("D100", "ushort", 7) is True  # 同宽放行
        assert client.write("D100", "short", -7) is True  # 同宽异名不拦
        assert client.written == [7, -7]

    def test_type_width_mismatch_native_mirror(self) -> None:
        """native 层同口径(review-1043 乙-2 镜像)。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        client.bind_tags(TagTable([Tag("液位", "hr100", "ushort")]))
        client.write_whitelist = True
        with pytest.raises(ValueError, match="类型宽度不符"):
            asyncio.run(client.write("hr100", "double", 1.0))

    def test_write_many_prevalidates_values_zero_send(self) -> None:
        """``write_many`` 前置阶段校验**值**(review-1043 乙-3)。

        原实现只校验地址与类型名,值非法要等写到该点才抛 ⇒
        「值非法 = 前几个点已写 + 抛 ValueError」,而 Modbus 族覆写一直在
        步骤 1 就把全部值校验完——同一公开 API 两种安全度。现基类补齐值
        预检:任一值非法即零发送。
        """
        client = _ScriptedClient()
        client.bind_tags(
            TagTable([Tag("a", "hr0", "ushort"), Tag("b", "hr1", "ushort")])
        )
        client.write_whitelist = True
        with pytest.raises(ValueError, match="int/float"):
            client.write_many([("hr0", "ushort", 1), ("hr1", "ushort", "abc")])
        assert client.written == []  # 零发送:第一个点也没写出去
        # 全合法 → 正常逐点写
        assert client.write_many([("hr0", "ushort", 1), ("hr1", "ushort", 2)]) == [
            True,
            True,
        ]
        assert client.written == [1, 2]

    def test_validate_value_type_matrix(self) -> None:
        """``_validate_value`` 类型矩阵(BOOL/STRING/数值)。"""
        client = _ScriptedClient()
        client._validate_value(DataType.BOOL, True)
        client._validate_value(DataType.BOOL, 1)
        client._validate_value(DataType.STRING, "abc")
        client._validate_value(DataType.STRING, b"abc")
        client._validate_value(DataType.USHORT, 5)
        client._validate_value(DataType.FLOAT, 1.5)
        for data_type, bad in (
            (DataType.BOOL, "yes"),
            (DataType.STRING, 5),
            (DataType.USHORT, "5"),
            (DataType.USHORT, True),
            (DataType.FLOAT, None),
        ):
            with pytest.raises(ValueError):
                client._validate_value(data_type, bad)


class TestLifecycleLogging:
    """连接生命周期事件不受 ``set_debug`` 门控(review-1043 戊-1)。"""

    def test_events_without_debug_enabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """已连接 / 已断开 / 链路断开 / 心跳失败都能在日志里看到。

        生产推荐组合是「黑匣子常驻 + 实时报文日志关闭」——此时日志里原本
        **永远看不到**何时断、断了多久、重连几次(全走 ``log_op``,受
        ``_enabled`` 门控),夜间无人 poll ``stats`` 就等于没有证据。
        """
        import logging

        from omniplc.core.debug import LOGGER_NAME, debug_enabled

        client = _ScriptedClient()
        assert debug_enabled() is False  # 未开报文调试
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            assert client.connect() is True
            assert client.disconnect() is True
            # 拆连事件(WARNING,异常级)
            client.connect()
            client.script_failure(OSError("链路炸了"))
            assert client.read("hr0", "ushort") == (False, None)
        text = caplog.text
        assert "已连接" in text
        assert "已断开" in text
        assert "链路断开" in text


class TestBatchErrorAddressContext:
    """批量失败带点位地址(review-1043 戊-2)。"""

    def test_read_many_reports_failed_addresses(self) -> None:
        """一个地址配错时,``last_error`` 要说出是哪个地址。"""
        client = _ScriptedClient()
        for _ in range(2):
            client.script_failure(
                DeviceError("Modbus 异常码 0x02(ILLEGAL DATA ADDRESS)", 2)
            )
        results = client.read_many(["hr100", "hr200"], "ushort")
        assert results == [(False, None), (False, None)]
        assert client.last_error is not None
        assert "hr100" in client.last_error and "hr200" in client.last_error
        assert "read_many 失败点位" in client.last_error

    def test_write_many_reports_failed_addresses(self) -> None:
        client = _ScriptedClient()
        client.script_failure(DeviceError("写被拒", 0))
        assert client.write_many([("hr1", "ushort", 1)]) == [False]
        assert client.last_error is not None and "hr1" in client.last_error

    def test_long_list_truncated(self) -> None:
        """点位很多时只列前 3 个 + "等 N 点"(不把 last_error 撑爆)。"""
        client = _ScriptedClient()
        for _ in range(5):
            client.script_failure(DeviceError("坏", 0))
        client.read_many(["hr0", "hr1", "hr2", "hr3", "hr4"], "ushort")
        assert client.last_error is not None
        assert "hr0, hr1, hr2" in client.last_error
        assert "等 5 点" in client.last_error


class TestSetDebugReversible:
    """``set_debug(False)`` 须还原 handler 与 propagate(review-1043 戊-3)。"""

    def test_handler_and_propagate_restored(self) -> None:
        """先开 debug 再配应用日志:应用 handler 仍应收到库日志。

        原实现关闭时只把级别退回 ``NOTSET``,自己挂的 stderr handler 与
        ``propagate=False`` 都留着 ⇒ 应用 root handler 收到 0 条,排障时
        "手抖开过一次 debug"之后日志静默绕开应用日志体系。

        用例把两侧既有 handler 临时摘净,**确保真正走到"挂 handler"分支**
        (``_attach_default_handler`` 见到已有 handler 会直接返回——不摘的话
        本用例在旧实现下也会通过,是假绿)。
        """
        import logging

        from omniplc.core.debug import LOGGER_NAME, set_debug

        logger = logging.getLogger(LOGGER_NAME)
        root = logging.getLogger()
        saved_logger_handlers = list(logger.handlers)
        saved_root_handlers = list(root.handlers)
        try:
            for handler in saved_logger_handlers:
                logger.removeHandler(handler)
            for handler in saved_root_handlers:
                root.removeHandler(handler)

            set_debug(True)
            assert logger.handlers, "前置条件:set_debug(True) 应挂上 stderr handler"
            assert logger.propagate is False

            set_debug(False)
            assert logger.propagate is True  # 恢复挂载前状态
            assert logger.handlers == []  # 自挂 handler 已摘

            records: List[logging.LogRecord] = []

            class _Sink(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record)

            sink = _Sink()
            old_level = root.level
            root.addHandler(sink)
            root.setLevel(logging.INFO)
            try:
                logger.info("测试事件")
            finally:
                root.removeHandler(sink)
                root.setLevel(old_level)
            assert [record.getMessage() for record in records] == ["测试事件"]
        finally:
            set_debug(False)
            for handler in saved_logger_handlers:
                logger.addHandler(handler)
            for handler in saved_root_handlers:
                root.addHandler(handler)


class TestDisconnectIsTerminal:
    """``disconnect()`` 即终态:不再经惰性重连复活(review-1043 丙-2)。"""

    def test_no_lazy_reconnect_after_disconnect(self) -> None:
        """断开后读写都直接 ``RuntimeError``,且**零发送**。

        现场形态:采集线程在 ``read_many`` 循环、控制线程在 ``write_many``
        循环时另一线程点「断开」——旧实现只把 ``_connected`` 置 False,
        循环的下一个点会经 ``_execute`` 的惰性重连分支**重新建连继续收发**
        (写循环即「已断开仍向 PLC 写」)。native 层早有 ``_ensure_open``
        关闸,aio ``close()`` 亦是终态,同步层此前独缺。
        """
        client = _ScriptedClient()
        assert client.connect() is True
        transport = client._transport
        assert client.disconnect() is True
        sent_before = len(getattr(transport, "sent", []))
        with pytest.raises(RuntimeError, match="已断开"):
            client.read("hr0", "ushort")
        with pytest.raises(RuntimeError, match="已断开"):
            client.write("hr0", "ushort", 1)
        with pytest.raises(RuntimeError, match="已断开"):
            client.read_many(["hr0"], "ushort")
        assert len(getattr(transport, "sent", [])) == sent_before  # 零发送
        # 显式 connect 解除关闸后照常可用
        assert client.connect() is True
        ok, _value = client.read("hr0", "ushort")
        assert ok is True

    def test_native_mirror(self) -> None:
        """native 层的终态关闸由 ``close()`` 承担(``disconnect`` 仍可重连)。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

        async def scenario() -> None:
            await client.close()
            with pytest.raises(RuntimeError):
                await client.read("hr0", "ushort")

        asyncio.run(scenario())


class TestWaitValueTimeBounds:
    """``wait_value`` 时间参数须为有限数(review-1043 丙-4)。"""

    def test_non_finite_rejected(self) -> None:
        client = _ScriptedClient()
        client.readback = 1
        for bad in (float("nan"), float("inf"), -1.0, 0.0):
            with pytest.raises(ValueError, match="有限数|大于 0"):
                client.wait_value("hr0", "ushort", lambda v: v == 1, timeout=bad)
        with pytest.raises(ValueError, match="有限数|大于 0"):
            client.wait_value(
                "hr0", "ushort", lambda v: v == 1, timeout=1.0, interval=float("nan")
            )

    def test_native_mirror(self) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

        async def scenario() -> None:
            with pytest.raises(ValueError, match="有限数|大于 0"):
                await client.wait_value(
                    "hr0", "ushort", lambda v: v == 1, timeout=float("inf")
                )

        asyncio.run(scenario())


# ----------------------------------------------------------------------
# 甲2:write_and_verify 写后回读
# ----------------------------------------------------------------------


class TestWriteAndVerify:
    def test_ok_roundtrip(self) -> None:
        client = _ScriptedClient()
        client.readback = 100
        ok, value = client.write_and_verify("hr0", "ushort", 100)
        assert ok is True and value == 100
        assert client.written == [100]

    def test_mismatch_reports_both_values(self) -> None:
        client = _ScriptedClient()
        client.readback = 99
        ok, value = client.write_and_verify("hr0", "ushort", 100)
        assert ok is False and value == 99
        assert "回读不符" in (client.last_error or "")

    def test_float_compares_bit_pattern(self) -> None:
        """float32 按位型比对:3.14 写读回 3.140000104904175 应判相符。"""
        client = _ScriptedClient()
        client.readback = struct.unpack("<f", struct.pack("<f", 3.14))[0]
        ok, value = client.write_and_verify("D100", "float", 3.14)
        assert ok is True
        assert value == pytest.approx(3.140000104904175)

    def test_verify_false_skips_read(self) -> None:
        client = _ScriptedClient()
        ok, value = client.write_and_verify("hr0", "ushort", 1, verify=False)
        assert ok is True and value is None
        assert client.read_calls == 0

    def test_write_failure_short_circuits(self) -> None:
        client = _ScriptedClient()
        client.script_failure(DeviceError("PLC 拒绝", 2))
        ok, value = client.write_and_verify("hr0", "ushort", 1)
        assert ok is False and value is None
        assert client.read_calls == 0

    def test_native_read_only_gate(self) -> None:
        """native write_and_verify 受只读闸拒绝(契约镜像)。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        client.read_only = True

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_and_verify("hr0", "ushort", 5)

        asyncio.run(scenario())

    def test_cross_type_readback_rejected(self) -> None:
        """异型回读显式拒绝(review-1043 乙-6):原实现按数值直接比较会恒判不符
        或碰巧一致——「按文档用就是坏的」。native 侧同构,通过 :meth:`native.BaseClient.write_and_verify`
        实现分支与 sync 同步复制,不再单独测(走全链路必先过未连接 client 的
        write=False 短路,无法触达该检查)。"""
        client = _ScriptedClient()
        client.readback = 100
        with pytest.raises(ValueError, match="异型回读"):
            client.write_and_verify("hr0", "long", 100, readback_type="ushort")


# ----------------------------------------------------------------------
# 乙4:写统计计数 + 写审计回调(review-1043 乙-4)+ 分类计数(戊-5)
# ----------------------------------------------------------------------


class TestWriteStats:
    def test_write_count_increments_on_success(self) -> None:
        """写成功计 ``write_count`` 并刷 ``last_write_at``(review-1043 乙-4)。"""
        client = _ScriptedClient()
        before = client.stats["write_count"]
        before_at = client.stats["last_write_at"]
        client.write("hr0", "ushort", 100)
        after = client.stats["write_count"]
        after_at = client.stats["last_write_at"]
        assert after == before + 1
        assert after_at is not None
        if before_at is not None:
            assert after_at >= before_at

    def test_write_failure_does_not_count_but_updates_last_write_at(self) -> None:
        """写失败不增 ``write_count``,但 ``last_write_at`` 仍刷
        (后者真实反映「最近一次写动作」,不管其成败)。"""
        client = _ScriptedClient()
        client.script_failure(DeviceError("PLC 拒绝", 2))
        before = client.stats["write_count"]
        before_at = client.stats["last_write_at"]
        result = client.write("hr0", "ushort", 100)
        assert result is False
        assert client.stats["write_count"] == before
        # last_write_at 已刷(单调钟非降序)
        if before_at is not None:
            assert client.stats["last_write_at"] >= before_at

    def test_audit_hook_called_with_address_type_value_ok(self) -> None:
        """写审计回调(地址, 类型名, 值, 是否成功)四元组(review-1043 乙-4)。"""
        client = _ScriptedClient()
        captured: List[Tuple[str, str, object, bool]] = []

        def hook(address: str, dtype: str, value: object, ok: bool) -> None:
            captured.append((address, dtype, value, ok))

        client.write_audit_hook = hook
        client.write("hr0", "ushort", 100)
        assert captured == [("hr0", "ushort", 100, True)]

    def test_audit_hook_exception_swallowed(self) -> None:
        """回调内部异常不影响写事务结果(只 ``log_warning``)。"""
        client = _ScriptedClient()

        def bad_hook(address: str, dtype: str, value: object, ok: bool) -> None:
            raise RuntimeError("用户回调写崩了")

        client.write_audit_hook = bad_hook
        # 写事务仍成功,异常被吞
        result = client.write("hr0", "ushort", 100)
        assert result is True

    def test_audit_hook_setter_validates(self) -> None:
        """非可调用对象直接拒。"""
        client = _ScriptedClient()
        with pytest.raises(ValueError, match="可调用"):
            client.write_audit_hook = 123  # type: ignore[assignment]
        client.write_audit_hook = None  # 关掉

    def test_error_count_by_category_tracks_buckets(self) -> None:
        """分类计数(review-1043 戊-5):DeviceError 计入 device,TransportTimeout
        计入 timeout;五桶初始全 0,失败后只动对应桶。"""
        client = _ScriptedClient()
        # 初始:五桶 0
        assert client.stats["error_count_by_category"] == {
            "transport": 0,
            "protocol": 0,
            "device": 0,
            "timeout": 0,
            "unknown": 0,
        }
        # DeviceError 走 device 桶
        client.script_failure(DeviceError("PLC 拒绝", 2))
        client.read("hr0", "ushort")
        bucket = client.stats["error_count_by_category"]
        assert bucket["device"] == 1
        assert bucket["timeout"] == 0
        assert bucket["transport"] == 0
        # error_count 总数也对
        assert client.stats["error_count"] == 1

    def test_audit_hook_native_mirrored(self) -> None:
        """native 写审计回调属性存在(契约镜像)。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        # 默认 None
        assert client.write_audit_hook is None
        # 设置可调用
        captured: List[Tuple[str, str, object, bool]] = []

        def hook(address: str, dtype: str, value: object, ok: bool) -> None:
            captured.append((address, dtype, value, ok))

        client.write_audit_hook = hook
        assert client.write_audit_hook is hook


# ----------------------------------------------------------------------
# 丙3:wait_value 等信号原语
# ----------------------------------------------------------------------


class TestWaitValue:
    def test_matched_immediately(self) -> None:
        client = _ScriptedClient()
        client.readback = 1
        ok, value = client.wait_value("M100", "bool", bool, timeout=1.0)
        assert ok is True and value is True

    def test_timeout_returns_last_value(self) -> None:
        client = _ScriptedClient()
        client.readback = 0
        ok, value = client.wait_value("M100", "bool", bool, timeout=0.2, interval=0.05)
        assert ok is False and value == 0

    def test_predicate_exception_propagates(self) -> None:
        client = _ScriptedClient()
        client.readback = 1

        def _boom(_value: PrimitiveValue) -> bool:
            raise RuntimeError("谓词自身错误")

        with pytest.raises(RuntimeError, match="谓词自身错误"):
            client.wait_value("M100", "bool", _boom, timeout=1.0)

    def test_parameter_validation(self) -> None:
        client = _ScriptedClient()
        with pytest.raises(ValueError, match="可调用"):
            client.wait_value("M0", "bool", "x", timeout=1.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="timeout"):
            client.wait_value("M0", "bool", bool, timeout=0)
        with pytest.raises(ValueError, match="interval"):
            client.wait_value("M0", "bool", bool, timeout=1.0, interval=0)

    def test_native_mirror_matched(self) -> None:
        """native wait_value:打桩基类 read 入口,首拍命中。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

        async def _fake_read(
            address: str, data_type: Union[DataType, str]
        ) -> Tuple[bool, Optional[PrimitiveValue]]:
            return True, 7

        client.read = _fake_read  # type: ignore[method-assign]

        async def scenario() -> None:
            ok, value = await client.wait_value(
                "hr0", "ushort", lambda v: v == 7, timeout=0.5
            )
            assert ok is True and value == 7

        asyncio.run(scenario())


# ----------------------------------------------------------------------
# 乙1:事故快照与黑匣子导出
# ----------------------------------------------------------------------


class TestIncidentFrames:
    def setup_method(self) -> None:
        clear_recorded_frames()

    def teardown_method(self) -> None:
        clear_recorded_frames()
        set_frame_recorder(False)

    def test_device_error_captures_snapshot(self) -> None:
        set_frame_recorder(True, capacity=16)
        client = _ScriptedClient()
        assert client.read("hr0", "ushort") == (True, 1)  # 产生一次收发
        client.script_failure(DeviceError("PLC 拒绝", 2))
        ok, _value = client.read("hr0", "ushort")
        assert ok is False
        assert len(client.incident_frames) >= 2  # 事故前的 SEND/RECV 已入快照

    def test_recorder_off_keeps_empty(self) -> None:
        client = _ScriptedClient()
        client.script_failure(DeviceError("PLC 拒绝", 2))
        ok, _value = client.read("hr0", "ushort")
        assert ok is False
        assert client.incident_frames == []

    def test_format_and_export(self, tmp_path) -> None:
        set_frame_recorder(True, capacity=16)
        client = _ScriptedClient()
        client.read("hr0", "ushort")
        text = format_frame_records(recorded_frames())
        assert "共 2 帧" in text
        assert SEND_MARK in text and RECV_MARK in text
        assert "01 02" in text  # 数据转储行
        target = tmp_path / "frames.txt"
        exported = export_recorded_frames(str(target))
        assert exported == text
        assert SEND_MARK in target.read_text(encoding="utf-8")

    def test_incident_frames_returns_snapshot_copy(self) -> None:
        """返回快照拷贝(review-1022 P2-1):调用方原地修改不污染库内现场。"""
        set_frame_recorder(True, capacity=16)
        client = _ScriptedClient()
        client.read("hr0", "ushort")
        client.script_failure(DeviceError("PLC 拒绝", 2))
        client.read("hr0", "ushort")
        snapshot = client.incident_frames
        assert len(snapshot) >= 2
        snapshot.clear()  # 模拟调用方误改
        assert len(client.incident_frames) >= 2  # 库内现场未被改写
        # native 镜像同口径
        native_client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        native_client._incident_frames = list(recorded_frames())
        native_snapshot = native_client.incident_frames
        native_snapshot.clear()
        assert len(native_client.incident_frames) >= 2


# ----------------------------------------------------------------------
# 丙2:read_many_strict 一致性批量读(整批成功才交付)
# ----------------------------------------------------------------------


class TestReadManyStrict:
    def test_all_ok_delivers(self) -> None:
        client = _ScriptedClient()
        client.readback = 7
        ok, values = client.read_many_strict(["hr0", "hr1", "hr2"], "ushort")
        assert ok is True and values == [7, 7, 7]

    def test_partial_failure_rejects_whole_batch(self) -> None:
        client = _ScriptedClient()

        def _fake_read(
            address: str, data_type: object
        ) -> Tuple[bool, Optional[PrimitiveValue]]:
            # hr1 混入失败点:逐点容错的 read_many 会给 (False, None)
            if address == "hr1":
                return False, None
            return True, 1

        client.read = _fake_read  # type: ignore[method-assign]
        pairs = client.read_many(["hr0", "hr1", "hr2"], "ushort")
        assert pairs[1] == (False, None)  # 逐点容错形态
        ok, values = client.read_many_strict(["hr0", "hr1", "hr2"], "ushort")
        assert ok is False and values is None

    def test_native_mirror(self) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

        async def _fake_read_many(
            addresses: Sequence[str], data_type: object
        ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
            # 桩打在 read_many(native Modbus 覆写了合并读,不逐点走 read)
            return [(True, 5) for _ in addresses]

        client.read_many = _fake_read_many  # type: ignore[method-assign]

        async def scenario() -> None:
            ok, values = await client.read_many_strict(["hr0", "hr1"], "ushort")
            assert ok is True and values == [5, 5]

        asyncio.run(scenario())

    def test_short_driver_result_raises_not_fake_success(self) -> None:
        """驱动少返条目 → ProtocolFrameError,不再 (True, 短列表) 假成功。

        review-1022 P1-1:该 API 的全部存在理由是"整批成功才交付",少返时
        既不失败也不报错会交付静默缺项的快照。
        """
        client = _ScriptedClient()
        client.read_many = lambda addresses, data_type: (
            [(True, 1)] * (len(addresses) - 1)
        )  # type: ignore[method-assign]

        with pytest.raises(ProtocolFrameError, match="条目数与请求不符"):
            client.read_many_strict(["hr0", "hr1", "hr2"], "ushort")

    def test_native_short_driver_result_raises(self) -> None:
        """native 镜像同守卫(review-1022 P1-1)。"""
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)

        async def _short_read_many(
            addresses: Sequence[str], data_type: object
        ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
            return [(True, 1)]

        client.read_many = _short_read_many  # type: ignore[method-assign]

        async def scenario() -> None:
            with pytest.raises(ProtocolFrameError, match="条目数与请求不符"):
                await client.read_many_strict(["hr0", "hr1"], "ushort")

        asyncio.run(scenario())

    def test_aio_mirror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sync_client = _ScriptedClient()

        def _fake_transport() -> BaseTransport:
            return _Transport()

        monkeypatch.setattr(sync_client, "_create_transport", _fake_transport)
        aio_client = AModbusTcpClient.__new__(AModbusTcpClient)
        AModbusTcpClient.__bases__[0].__init__(aio_client, sync_client)

        async def scenario() -> None:
            ok, values = await aio_client.read_many_strict(["hr0"], "ushort")
            assert ok is True and values == [1]

        asyncio.run(scenario())
