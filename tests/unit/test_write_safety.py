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
from omniplc.core.errors import DeviceError
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


# ----------------------------------------------------------------------
# 甲3:write_and_verify 写后回读
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
