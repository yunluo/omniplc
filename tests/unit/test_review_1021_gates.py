"""review-1021 修复批回归测试:写安全收口 + 契约一致性。

覆盖(review-1021 §十 第 1/2 批随批用例):

- **A 形态补测锁定**:批量/随机/掩码/字符串写入口在 ``read_only=True`` 下
  零字节拒发(行为已在——``is_write=True`` 经 ``_execute`` 总闸,本轮
  复核批注①;用例把 13 处入口逐个锁死,防回归)与白名单逐地址过闸
- **C 形态收口**:AB ``generic_message`` 按服务码判写(0x4D/0x4E)+ 显式
  覆盖;读码器 TCP ``command`` 按 Get/Set/Exec 分流
- **契约批**:\ ``_set_error``\ code=0 归 None 单点收口、TIMEOUT ⇒ code=None
  类型级保证(P1-2)、心跳不覆盖事故快照(P1-1)、基类 ``write_many`` 入参
  前置校验(P1-4)、aio ``write_whitelist`` 转发在位(§七条目 1 反证锁定)
- **写方法登记表守卫**:全库客户端 ``write*``/``*write``/``set_*`` 命名面
  ⊆ 登记表,新增写方法漏登记即门禁红(§2.6 修法 5 的守卫形态)
"""

from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest

import omniplc
from omniplc import (
    AllenBradleyEthIpClient,
    HikrobotIdTcpClient,
    MelsecMcTcpClient,
    MelsecMxClient,
    ModbusTcpClient,
    SiemensS7Client,
    set_frame_recorder,
)
from omniplc.aio import AModbusTcpClient
from omniplc.core.base_client import BaseClient, _extract_code
from omniplc.core.debug import (
    RECV_MARK,
    SEND_MARK,
    clear_recorded_frames,
    log_frame,
)
from omniplc.core.errors import DeviceError, ErrorCategory, TransportTimeoutError
from omniplc.core.tag import Tag, TagTable
from omniplc.core.types import DataType, PrimitiveValue
from omniplc.native import (
    AsyncMelsecMcTcpClient,
    AsyncModbusTcpClient,
    AsyncSiemensS7Client,
)
from omniplc.plc.modbus import codec as modbus_codec
from omniplc.transport import BaseTransport
from scripted import ScriptedTransport


# ----------------------------------------------------------------------
# 基类契约用例的脚本化桩(同 test_write_safety 手法)
# ----------------------------------------------------------------------


class _Transport(BaseTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent: List[bytes] = []

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        log_frame("script://gates", SEND_MARK, data)
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        data = b"\x00" * size
        log_frame("script://gates", RECV_MARK, data)
        return data


class _ScriptedClient(BaseClient):
    """最小脚本化驱动(基类契约用例)。"""

    def __init__(self) -> None:
        super().__init__("127.0.0.1", 502)
        self.written: List[PrimitiveValue] = []
        self._fail_next_op: List[BaseException] = []

    def script_failure(self, exc: BaseException) -> None:
        """注入一次读/写失败。"""
        self._fail_next_op.append(exc)

    def _create_transport(self) -> BaseTransport:
        return _Transport()

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        self._require_transport().send(b"\x01")
        self._require_transport().recv(1)
        return 1

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        if self._fail_next_op:
            raise self._fail_next_op.pop(0)
        self.written.append(value)


def _forbid_transport(monkeypatch: pytest.MonkeyPatch, client: BaseClient) -> None:
    """断言用例全程不创建传输(零 IO 的强断言:闸在入口抛,连接都不建)。"""

    def _boom() -> BaseTransport:
        raise AssertionError("闸门应在创建传输/建连之前拒绝")

    monkeypatch.setattr(client, "_create_transport", _boom)


# ----------------------------------------------------------------------
# A 形态:read_only 下批量/随机/掩码/字符串写零字节拒发(补测锁定)
# ----------------------------------------------------------------------


class TestReadOnlyBatchEntries:
    """复核批注①:13 处入口全部 is_write=True 进 _execute,总闸拦截在位。

    本组用例把行为**锁死**:任一入口丢失 is_write 声明或总闸被绕过即红。
    """

    def test_modbus_sync_four_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = ModbusTcpClient("127.0.0.1", 502, 1)
        client.read_only = True
        _forbid_transport(monkeypatch, client)
        with pytest.raises(RuntimeError, match="只读"):
            client.write_many([("hr0", "short", 1)])
        with pytest.raises(RuntimeError, match="只读"):
            client.write_batch([("hr0", "short", 1)])
        with pytest.raises(RuntimeError, match="只读"):
            client.write_mask_register("hr0", 0x00FF, 0x0010)
        with pytest.raises(RuntimeError, match="只读"):
            client.read_write_registers("hr0", 1, "hr1", [1])

    def test_mc_random_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MelsecMcTcpClient("127.0.0.1")
        client.read_only = True
        _forbid_transport(monkeypatch, client)
        with pytest.raises(RuntimeError, match="只读"):
            client.random_write([("D100", 1)])

    def test_mx_write_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = MelsecMxClient()
        client.read_only = True
        _forbid_transport(monkeypatch, client)
        with pytest.raises(RuntimeError, match="只读"):
            client.write_batch([("D100", 1)])

    def test_s7_write_wstring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = SiemensS7Client("127.0.0.1")
        client.read_only = True
        _forbid_transport(monkeypatch, client)
        with pytest.raises(RuntimeError, match="只读"):
            client.write_wstring("DB1.DBW0", "测")

    def test_native_modbus_four_entries(self) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        client.read_only = True

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_many([("hr0", "short", 1)])
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_batch([("hr0", "short", 1)])
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_mask_register("hr0", 0x00FF, 0x0010)
            with pytest.raises(RuntimeError, match="只读"):
                await client.read_write_registers("hr0", 1, "hr1", [1])

        asyncio.run(scenario())

    def test_native_mc_random_write(self) -> None:
        client = AsyncMelsecMcTcpClient("127.0.0.1")
        client.read_only = True

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await client.random_write([("D100", 1)])

        asyncio.run(scenario())

    def test_native_s7_write_wstring(self) -> None:
        client = AsyncSiemensS7Client("127.0.0.1")
        client.read_only = True

        async def scenario() -> None:
            with pytest.raises(RuntimeError, match="只读"):
                await client.write_wstring("DB1.DBW0", "测")

        asyncio.run(scenario())


# ----------------------------------------------------------------------
# A 形态:白名单逐地址过闸(P0-1 的新行为面)
# ----------------------------------------------------------------------


class TestWhitelistBatchEntries:
    def test_modbus_write_many_outside_rejected_inside_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_many 逐 item 过闸:表外地址零发送拒绝,表内放行。"""
        # 两点连续写合单笔 FC16;应答 = 功能码回显 + 起始地址 + 数量(规范 §6.12)
        # ScriptedTransport 脚本 = recv 分片序列(MBAP 头 7 字节 + 帧体)
        frame = modbus_codec.build_mbap(1, 1, bytes([0x10, 0x00, 0x00, 0x00, 0x02]))
        scripted = ScriptedTransport([frame[:7], frame[7:]])
        client = ModbusTcpClient("127.0.0.1", 502, 1)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        client.bind_tags(
            TagTable([Tag("整定值", "hr0", "short"), Tag("备用值", "hr1", "short")])
        )
        client.write_whitelist = True
        with pytest.raises(ValueError, match="白名单"):
            client.write_many([("hr0", "short", 5), ("hr9", "short", 5)])
        assert bytes(scripted.sent) == b""  # 零发送
        assert client.write_many([("hr0", "short", 5), ("hr1", "short", 6)]) == [
            True,
            True,
        ]
        # 表内放行:两点合单笔 FC16(fc/起始地址/数量/字节数/字序列)
        assert bytes(scripted.sent) == modbus_codec.build_mbap(
            1, 1, bytes([0x10, 0x00, 0x00, 0x00, 0x02, 0x04, 0x00, 0x05, 0x00, 0x06])
        )

    def test_base_write_many_front_validation(self) -> None:
        """基类 write_many 入参前置全量校验(P1-4):任一非法零写入。"""
        client = _ScriptedClient()
        # 第 2 项类型非法:第 1 项也不得下发(旧行为是先写后炸)
        with pytest.raises(ValueError):
            client.write_many([("a", "ushort", 1), ("b", "不存在", 1)])
        assert client.written == []
        # 白名单混合批量:表外项零写入
        client.bind_tags(TagTable([Tag("a", "D100", "ushort")]))
        client.write_whitelist = True
        with pytest.raises(ValueError, match="白名单"):
            client.write_many([("D100", "ushort", 1), ("hr9", "ushort", 1)])
        assert client.written == []
        client.write_whitelist = False
        # 只读:入口即拒
        client.read_only = True
        with pytest.raises(RuntimeError, match="只读"):
            client.write_many([("a", "ushort", 1)])
        assert client.written == []
        client.read_only = False
        # 合法批量行为不变
        assert client.write_many([("a", "ushort", 1), ("b", "ushort", 2)]) == [
            True,
            True,
        ]
        assert client.written == [1, 2]


# ----------------------------------------------------------------------
# C 形态:generic_message 按服务码判写 / command 按命令类型分流
# ----------------------------------------------------------------------


class _ExecuteCapture:
    """实例级 _execute 捕桩:记录 is_write,不发报文。"""

    def __init__(self) -> None:
        self.calls: List[bool] = []

    def __call__(
        self,
        operation: object,
        is_write: bool = False,
        heartbeat: bool = False,
    ) -> Tuple[bool, None]:
        self.calls.append(is_write)
        return True, None


class TestGenericMessageGate:
    def test_service_code_inference_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0x4D Write Tag / 0x4E Read-Modify-Write 判写,其余判读;显式可覆盖。"""
        client = AllenBradleyEthIpClient("127.0.0.1")
        capture = _ExecuteCapture()
        monkeypatch.setattr(client, "_execute", capture)
        client.generic_message(0x4D, 0x04, 1)  # Write Tag
        client.generic_message(0x4E, 0x04, 1)  # Read Modify Write Tag
        client.generic_message(0x01, 0x04, 1)  # Get Attributes All(读)
        assert capture.calls == [True, True, False]
        # 显式覆盖优先于服务码判定
        client.generic_message(0x4D, 0x04, 1, is_write=False)
        client.generic_message(0x01, 0x04, 1, is_write=True)
        assert capture.calls[-2:] == [False, True]

    def test_read_only_blocks_write_service(self) -> None:
        """判写服务在 read_only=True 下被拒(review-1021 C1)。"""
        client = AllenBradleyEthIpClient("127.0.0.1")
        client.read_only = True
        with pytest.raises(RuntimeError, match="只读"):
            client.generic_message(0x4D, 0x04, 1)


class TestCommandGate:
    def test_cmd_type_inference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set/Exec 按写事务,Get 按读事务(review-1021 C2)。"""
        client = HikrobotIdTcpClient("127.0.0.1")
        capture = _ExecuteCapture()
        monkeypatch.setattr(client, "_execute", capture)
        client.command("Get", "Acq")
        client.command("Set", "Acq", "1")
        client.command("Exec", "TriSoft")
        assert capture.calls == [False, True, True]

    def test_exec_read_only_blocked(self) -> None:
        """read_only=True 下 Exec(动作型命令)入口即拒。"""
        client = HikrobotIdTcpClient("127.0.0.1")
        client.read_only = True
        with pytest.raises(RuntimeError, match="只读"):
            client.command("Exec", "Reboot")


# ----------------------------------------------------------------------
# 契约批:P1-1 / P1-2 / §3.2 / aio 转发反证
# ----------------------------------------------------------------------


class TestContractBatch:
    def test_timeout_code_always_none(self) -> None:
        """TIMEOUT ⇒ code=None 类型级保证(P1-2):不依赖构造点手写 0。"""
        assert _extract_code(TransportTimeoutError("超时", 0)) is None
        assert _extract_code(TransportTimeoutError("超时", 4321)) is None
        # 非 TIMEOUT 的既有口径不变
        assert _extract_code(DeviceError("PLC 报错", 7)) == 7
        assert _extract_code(DeviceError("无码", 0)) is None

    def test_set_error_zero_code_normalized(self) -> None:
        """_set_error 直写 code=0 统一归 None(§3.2 单点收口)。"""
        client = MelsecMcTcpClient("127.0.0.1")
        client._set_error("设备侧失败", ErrorCategory.DEVICE, 0)
        assert client.last_error_code is None
        assert client.last_error_category is ErrorCategory.DEVICE
        client._set_error("本地诊断", ErrorCategory.DEVICE, -10040)
        assert client.last_error_code == -10040  # 负码诊断照原样保留

    def test_heartbeat_oserror_keeps_incident_snapshot(self) -> None:
        """心跳 tick 的拆连级失败不覆盖事故现场快照(P1-1,拆连计数照常)。"""
        set_frame_recorder(True, capacity=16)
        try:
            clear_recorded_frames()
            client = _ScriptedClient()
            client.read("hr0", "ushort")  # 产生一次收发,黑匣子留存 2 帧
            client.script_failure(DeviceError("PLC 拒绝", 2))
            ok, _value = client.read("hr0", "ushort")
            assert ok is False
            snapshot = client.incident_frames
            assert len(snapshot) >= 2

            def _boom() -> None:
                raise ConnectionResetError("对端重置")

            ok, _value = client._execute(_boom, heartbeat=True)
            assert ok is False
            assert client.incident_frames == snapshot  # 心跳不覆盖(P1-1)
            assert client.connected is False  # 传输类真实故障照常拆连
            assert "重置" in (client.last_error or "")
        finally:
            clear_recorded_frames()
            set_frame_recorder(False)

    def test_aio_write_whitelist_forwarding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """aio write_whitelist 转发在位(§七条目 1"未在 aio 暴露"反证锁定)。"""
        sync_client = _ScriptedClient()

        def _fake_transport() -> BaseTransport:
            return _Transport()

        monkeypatch.setattr(sync_client, "_create_transport", _fake_transport)
        aio_client = AModbusTcpClient.__new__(AModbusTcpClient)
        AModbusTcpClient.__bases__[0].__init__(aio_client, sync_client)
        aio_client.write_whitelist = True
        assert sync_client.write_whitelist is True
        assert aio_client.write_whitelist is True
        with pytest.raises(ValueError, match="布尔值"):
            aio_client.write_whitelist = 1  # type: ignore[assignment]


# ----------------------------------------------------------------------
# 写方法登记表守卫(§2.6 修法 5):新增写方法漏登记即门禁红
# ----------------------------------------------------------------------

# 全库公开写方法登记表:写安全闸(read_only 总闸 / 白名单)已核实覆盖的
# 方法名。命名面守卫只强制 write*/​*write/set_* 三族;scan/trigger/command
# 等家族特有名字登记备查,新增时须同步确认闸门并补零发送用例。
_WRITE_METHOD_REGISTRY = frozenset(
    {
        # 基类读写原语与类型化写(经 write()/write_string() 过闸)
        "write",
        "write_string",
        "write_and_verify",
        "write_tag",
        "write_many",
        "write_batch",
        "write_bool",
        "write_short",
        "write_ushort",
        "write_int",
        "write_uint",
        "write_long",
        "write_ulong",
        "write_float",
        "write_double",
        "write_area",
        # 协议特有写(review-1021 §二 A 形态收口面)
        "write_mask_register",
        "read_write_registers",
        "random_write",
        "write_wstring",
        "write_file_record",
        "write_clock",
        "set_clock",
        # 读码器家族(§二 B/C 形态收口面)
        "scan",
        "read_result",
        "trigger",
        "stop",
        "reset",
        "clear_error",
        "command",
        "set_acquisition",
        "set_enum_value",
        "set_command_value",
        "set_int_value",
        "set_bool_value",
        "set_float_value",
        "set_string_value",
    }
)


def _client_classes() -> List[type]:
    """根包 + aio + native 的全部公开客户端类。"""
    classes: List[type] = []
    for name in omniplc.__all__:
        obj = getattr(omniplc, name)
        if isinstance(obj, type) and name.endswith("Client"):
            classes.append(obj)
    for module in (omniplc.aio, omniplc.native):
        for name in getattr(module, "__all__", []):
            obj = getattr(module, name)
            if isinstance(obj, type) and name.endswith("Client"):
                classes.append(obj)
    return classes


class TestWriteMethodRegistry:
    def test_write_named_methods_are_registered(self) -> None:
        """write* / *write / set_* 命名的公开方法必须已登记(闸门核实过)。

        property(write_retries/write_whitelist 等配置面)不算写方法。
        """
        unknown: List[str] = []
        for cls in _client_classes():
            for name in dir(cls):
                if name.startswith("_"):
                    continue
                if isinstance(getattr(cls, name), property):
                    continue
                hit = (
                    name.startswith("write")
                    or name.endswith("write")
                    or name.startswith("set_")
                )
                if hit and name not in _WRITE_METHOD_REGISTRY:
                    unknown.append("{}.{}".format(cls.__name__, name))
        assert unknown == [], "新增写方法未登记写安全面:{}".format(unknown)

    def test_read_only_property_present_everywhere(self) -> None:
        """全部客户端类都有 read_only/write_whitelist 闸(含继承)。"""
        for cls in _client_classes():
            assert hasattr(cls, "read_only"), cls.__name__
            assert hasattr(cls, "write_whitelist"), cls.__name__
