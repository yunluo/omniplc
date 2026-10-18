"""review-1021 §五 收口:aio 镜像接线守卫 + UDP 防串扰 + 边界对拍 + FINS 路由。

- **P0-3 接线守卫**:每个 ``A*`` 类 ``__init__`` 注入的同步类必须是
  「去 A 前缀」的同名类(AST 静态守卫,34 类全覆盖)——复制粘贴接错
  TCP↔UDP / Modbus↔TCP 在此暴露,镜像守卫的 ``dir()``/签名比对兜不住这层
- **P0-3 同源风险**:四个 aio UDP 类的 drain-before-send 走线——aio UDP
  类若接错成 TCP 同步类,``drain`` 语义静默消失而全部守卫仍绿
- **P1-7 边界对拍**:aio↔sync 边界入参给出完全一致的异常签名(复用
  native edge parity 的用例形态,锁「包装层不吞不改 ValueError」)
- **P1-8**:aio 独有 ``_AFinsRoutingClient``——路由 property 转发、
  时钟转发、外来同步实例 TypeError(此前仅构造冒烟)
- **P1-9**:同步透传豁免项抽样(``configure_serial`` 与同步侧同断言)
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Tuple

import pytest
from scripted import ScriptedTransport

from omniplc import (
    AllenBradleyEthIpClient,
    InovanceTcpClient,
    MelsecMcTcpClient,
    ModbusRtuClient,
    ModbusTcpClient,
    OmronFinsTcpClient,
    OpcUaClient,
    SiemensS7Client,
)
from omniplc.aio import (
    AAllenBradleyEthIpClient,
    AInovanceTcpClient,
    AKeyenceHostLinkUdpClient,
    AMelsecMcTcpClient,
    AMelsecMcUdpClient,
    AModbusRtuClient,
    AModbusTcpClient,
    AOmronFinsTcpClient,
    AOmronFinsUdpClient,
    AOpcUaClient,
    APanasonicMewtocolUdpClient,
    ASiemensS7Client,
    AToyopucUdpClient,
)

AIO_INIT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "omniplc"
    / "aio"
    / "__init__.py"
)


# ----------------------------------------------------------------------
# P0-3:接线守卫(AST 静态,全类覆盖)
# ----------------------------------------------------------------------


class TestAsyncWiringGuard:
    def test_async_client_wires_expected_sync_class(self) -> None:
        """A* 类 __init__ 构造的 *Client 名 == 去 A 前缀(接错在此红)。

        中间基类(如 ``AModbusBaseClient``)无自有 ``__init__``,跳过。
        """
        tree = ast.parse(AIO_INIT.read_text(encoding="utf-8"))
        checked = 0
        for cls in tree.body:
            if (
                not isinstance(cls, ast.ClassDef)
                or not cls.name.startswith("A")
                or cls.name == "ABaseClient"
            ):
                continue
            inits = [
                n
                for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__"
            ]
            if not inits:
                continue  # 中间基类:接线由子类完成
            called = {
                n.func.id
                for n in ast.walk(inits[0])
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id.endswith("Client")
            }
            expected = cls.name[1:]
            assert called == {expected}, (
                f"{cls.name} 注入 {sorted(called)} != {{{expected}}}"
            )
            checked += 1
        assert checked >= 25, f"接线守卫覆盖类数异常:{checked}"


# ----------------------------------------------------------------------
# P0-3 同源风险:aio UDP drain-before-send(四族)
# ----------------------------------------------------------------------


class TestAioUdpDrain:
    @pytest.mark.parametrize(
        "aio_cls,sync_name,address",
        [
            (AMelsecMcUdpClient, "MelsecMcUdpClient", "D100"),
            (AKeyenceHostLinkUdpClient, "KeyenceHostLinkUdpClient", "DM100"),
            (APanasonicMewtocolUdpClient, "PanasonicMewtocolUdpClient", "D100"),
            (AToyopucUdpClient, "ToyopucUdpClient", "D0100"),
        ],
        ids=["mc", "kv-hl", "mewtocol", "toyopuc"],
    )
    def test_udp_drains_stale_before_send(
        self,
        monkeypatch: pytest.MonkeyPatch,
        aio_cls: type,
        sync_name: str,
        address: str,
    ) -> None:
        """aio UDP 事务仍走「发送前排空陈旧数据报」走线(P0-3 同源风险)。"""
        client = aio_cls("127.0.0.1")
        sync = client._sync
        assert type(sync).__name__ == sync_name
        stale = b"\xde\xad\xbe\xef"
        scripted = ScriptedTransport([], datagram=True, stale=[stale])
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        ok, _value = asyncio.run(client.read(address, "ushort"))
        assert ok is False  # 无应答脚本:事务失败,但走线全程可观测
        assert scripted.events[0] == "drain" and "send" in scripted.events
        assert scripted.drained == [stale]


# ----------------------------------------------------------------------
# P1-7:aio↔sync 边界入参对拍(七族)
# ----------------------------------------------------------------------

_EDGE_CASES = [
    ("read_string_zero_len", lambda c: c.read_string("hr0", 0)),
    ("write_bool_int5", lambda c: c.write_bool("hr0", 5)),
    ("write_bool_str", lambda c: c.write_bool("hr0", "1")),
    ("read_unknown_type", lambda c: c.read("hr0", "NOT_A_TYPE")),
]


def _signature(outcome: object) -> Tuple[str, str]:
    if isinstance(outcome, BaseException):
        return ("raise", f"{type(outcome).__name__}:{str(outcome)}")
    return ("return", repr(outcome))


class TestAioSyncEdgeParity:
    def test_edge_inputs_identical_across_layers(self) -> None:
        """同一边界入参在 sync 与 aio 两层给出完全一致的异常/返回签名。"""
        pairs = [
            ("modbus", ModbusTcpClient("127.0.0.1", 502, 1), AModbusTcpClient),
            ("melsec", MelsecMcTcpClient("127.0.0.1"), AMelsecMcTcpClient),
            ("fins", OmronFinsTcpClient("127.0.0.1"), AOmronFinsTcpClient),
            ("inovance", InovanceTcpClient("127.0.0.1"), AInovanceTcpClient),
            ("s7", SiemensS7Client("127.0.0.1"), ASiemensS7Client),
            (
                "ab",
                AllenBradleyEthIpClient("127.0.0.1"),
                AAllenBradleyEthIpClient,
            ),
            ("opcua", OpcUaClient("127.0.0.1", 4840), AOpcUaClient),
        ]
        for family, sync_client, aio_cls in pairs:
            for case_name, op in _EDGE_CASES:
                try:
                    sync_out = op(sync_client)
                except BaseException as exc:
                    sync_out = exc
                aio_client = aio_cls("127.0.0.1")
                try:
                    aio_out = asyncio.run(op(aio_client))
                except BaseException as exc:
                    aio_out = exc
                assert _signature(sync_out) == _signature(aio_out), (
                    f"{family}-{case_name} 两层签名不一致"
                )


# ----------------------------------------------------------------------
# P1-8:aio 独有 _AFinsRoutingClient
# ----------------------------------------------------------------------


_FINS_ECHO_HEAD = b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00"


def _fins_clock_response() -> bytes:
    """0701 时钟读 UDP 应答(布局依 W342 §5-3-19 印刷页 198,同族测试手法)。"""
    fields = [26, 10, 3, 14, 9, 5, 6]
    data = bytes((v // 10) << 4 | (v % 10) for v in fields)
    return _FINS_ECHO_HEAD + b"\x01" + b"\x07\x01" + (0).to_bytes(2, "big") + data


class TestAioFinsRouting:
    def test_routing_props_forward(self) -> None:
        """六个路由 property 转发同步实例(P1-8:显式配置值原样可见)。"""
        client = AOmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
        sync = client._sync
        assert client.destination_node == sync.destination_node == 5
        assert client.source_node == sync.source_node == 10
        assert client.destination_network == sync.destination_network
        assert client.destination_unit == sync.destination_unit
        assert client.source_network == sync.source_network
        assert client.source_unit == sync.source_unit

    def test_read_clock_forwards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """时钟读经 aio 走同步事务,FinsClock 语义一致(P1-8)。"""
        client = AOmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
        scripted = ScriptedTransport([_fins_clock_response()], datagram=True)
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        ok, clock = asyncio.run(client.read_clock())
        assert ok is True
        assert (clock.year, clock.month, clock.day) == (26, 10, 3)
        assert (clock.hour, clock.minute, clock.second) == (14, 9, 5)

    def test_rejects_foreign_sync_instance(self) -> None:
        """外来同步实例在路由 property 处 TypeError(P1-8:类型守卫无用例)。"""
        from omniplc.aio import ABaseClient

        client = AOmronFinsUdpClient.__new__(AOmronFinsUdpClient)
        ABaseClient.__init__(client, ModbusTcpClient("127.0.0.1", 502, 1))
        with pytest.raises(TypeError):
            client.destination_network  # noqa: B018 —— 故意求值触发 TypeError


# ----------------------------------------------------------------------
# P1-9:同步透传豁免项抽样(configure_serial)
# ----------------------------------------------------------------------


class TestAioConfigureSerialMirror:
    def test_modbus_rtu_configure_serial_passthrough(self) -> None:
        """AModbusRtuClient.configure_serial 与同步侧同断言(P1-9 覆盖对称)。"""
        rtu = AModbusRtuClient(station=1)
        rtu.configure_serial("COM3")
        sync = rtu._sync
        assert sync._serial_config is not None
        sync_ref = ModbusRtuClient(station=1)
        sync_ref.configure_serial("COM3")
        assert sync._serial_config.baud_rate == sync_ref._serial_config.baud_rate
        assert sync._serial_config.stop_bits == sync_ref._serial_config.stop_bits


class TestAioTimeoutAsyncSetter:
    """review-1043 丁-1:async 安全入口在事件循环内设置超时,不冻结 loop。"""

    def test_set_receive_and_connect_timeout_effective(self) -> None:
        client = AModbusTcpClient("127.0.0.1", 502)

        async def scenario() -> None:
            await client.set_receive_timeout(2.5)
            assert client._sync.receive_timeout == 2.5
            await client.set_connect_timeout(3.5)
            assert client._sync.connect_timeout == 3.5

        asyncio.run(scenario())

    def test_setter_rejected_after_close(self) -> None:
        client = AModbusTcpClient("127.0.0.1", 502)

        async def scenario() -> None:
            await client.close()
            with pytest.raises(RuntimeError):
                await client.set_receive_timeout(2.5)

        asyncio.run(scenario())
