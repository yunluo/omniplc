"""全驱动构造冒烟守卫(26 驱动直连库类,原 tools/manual_common.build_client 口径)。

背景(第六/八轮连续命中同一盲区):门禁只覆盖 ``src tests``,联机脚本的构造
传参漂移无守卫——S7 位序错位曾潜伏五个版本(v0.32.0 起联机脚本 S7 驱动
完全不可用,review-0929-2 P1)。本测试把 26 个驱动**直接按现行签名构造**
一遍:构造成功即证明**位序/参数名/缺省值**与现行库签名一致;再对关键字段的
传递做抽查。

2026-10-14:联机脚本 `tools/manual_*.py` 按「永不入库」红线撤出仓库(本地
保留供真机联测),本测试随之去掉 ``tools/`` 依赖(原经 sys.path 注入 import
manual_common.build_client),构造口径内联为本模块 :func:`_build`。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from omniplc import (
    AllenBradleyEthIpClient,
    InovanceMcTcpClient,
    InovanceRtuClient,
    InovanceTcpClient,
    KeyenceHostLinkTcpClient,
    KeyenceHostLinkUdpClient,
    KeyenceMcTcpClient,
    KeyenceMcUdpClient,
    KeyenceSrClient,
    MelsecMcSerialClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
    ModbusRtuClient,
    ModbusTcpClient,
    MTConnectClient,
    OmronCipClient,
    OmronFinsTcpClient,
    OmronFinsUdpClient,
    OpcUaClient,
    PanasonicMcTcpClient,
    PanasonicMewtocolTcpClient,
    PanasonicMewtocolUdpClient,
    SiemensS7Client,
    ToyopucTcpClient,
    ToyopucUdpClient,
)

# 26 驱动(构造型参数全部给合法值);与 tools/manual_test.json 历史口径一致
_CONN_BASE: Dict[str, Any] = {"ip": "192.0.2.10", "port": None}


def _build(conn: Dict[str, Any]) -> Any:  # noqa: C901
    """按 driver 直接构造同步客户端(原 tools/manual_common.build_client 内联)。

    各驱动的**位置参数顺序与关键字形态**与原联机脚本构造口径一致——库签名
    一旦漂移(位序/参数名),本函数即抛 TypeError,冒烟用例随之失败。
    """
    d = conn["driver"]
    ip = conn.get("ip", "127.0.0.1")
    port = conn.get("port")
    p: Dict[str, Any] = conn.get("params", {})
    if d == "modbus_tcp":
        return ModbusTcpClient(ip, port or 502, p.get("station", 1))
    if d == "modbus_rtu":
        return ModbusRtuClient(p.get("station", 1))
    if d == "melsec_mc_tcp":
        return MelsecMcTcpClient(
            ip,
            port or 2000,
            p.get("frame", "3E"),
            p.get("network_number", 0),
            p.get("pc_number", 255),
            xy_octal=p.get("xy_octal", False),
        )
    if d == "melsec_mc_udp":
        return MelsecMcUdpClient(
            ip,
            port or 2000,
            p.get("frame", "3E"),
            p.get("network_number", 0),
            p.get("pc_number", 255),
            xy_octal=p.get("xy_octal", False),
        )
    if d == "melsec_mc_serial":
        return MelsecMcSerialClient(
            p.get("frame", "3C"),
            p.get("station_number", 0),
            p.get("network_number", 0),
            p.get("pc_number", 255),
            p.get("self_station_number", 0),
            p.get("module_io", 0x03FF),
            p.get("module_station", 0),
            message_wait=p.get("message_wait", 0),
        )
    if d == "melsec_mx":
        return MelsecMxClient(p.get("logical_station_number", 0))
    if d == "omron_fins_tcp":
        # local_node 缺省 None = 握手自动推导(库默认);传 0 会禁用自动
        return OmronFinsTcpClient(ip, port or 9600, p.get("local_node", None))
    if d == "omron_fins_udp":
        return OmronFinsUdpClient(ip, port or 9600)
    if d == "omron_cip":
        return OmronCipClient(ip, port or 44818, p.get("connected_messaging", False))
    if d == "keyence_hostlink_tcp":
        return KeyenceHostLinkTcpClient(ip, port or 8000)
    if d == "keyence_hostlink_udp":
        return KeyenceHostLinkUdpClient(ip, port or 8000)
    if d == "keyence_mc_tcp":
        return KeyenceMcTcpClient(
            ip, port or 5000, p.get("network_number", 0), p.get("pc_number", 255)
        )
    if d == "keyence_mc_udp":
        return KeyenceMcUdpClient(
            ip, port or 5000, p.get("network_number", 0), p.get("pc_number", 255)
        )
    if d == "keyence_sr":
        return KeyenceSrClient(
            ip,
            port or 9004,
            p.get("scan_dwell", 1.0),
            encoding=p.get("encoding", "utf-8"),
            encoding_errors=p.get("encoding_errors", "replace"),
        )
    if d == "inovance_tcp":
        return InovanceTcpClient(ip, port or 502, p.get("station", 1))
    if d == "inovance_rtu":
        return InovanceRtuClient(p.get("station", 1))
    if d == "inovance_mc_tcp":
        return InovanceMcTcpClient(
            ip, port or 2000, p.get("network_number", 0), p.get("pc_number", 255)
        )
    if d == "panasonic_mc_tcp":
        return PanasonicMcTcpClient(
            ip, port or 2000, p.get("network_number", 0), p.get("pc_number", 255)
        )
    if d == "panasonic_mewtocol_tcp":
        return PanasonicMewtocolTcpClient(ip, port or 1024, p.get("station", 1))
    if d == "panasonic_mewtocol_udp":
        return PanasonicMewtocolUdpClient(ip, port or 1024, p.get("station", 1))
    if d == "toyopuc_tcp":
        return ToyopucTcpClient(ip, port or 1025)
    if d == "toyopuc_udp":
        return ToyopucUdpClient(ip, port or 1025)
    if d == "ab_eip":
        return AllenBradleyEthIpClient(
            ip,
            port or 44818,
            p.get("slot", 0),
            p.get("connected_messaging", False),
            p.get("rpi_us", 100000),
        )
    if d == "siemens_s7":
        # 全关键字传参(v0.32.0 起防位置漂移);v0.53 自研栈后无 dll_path
        return SiemensS7Client(
            ip_address=ip,
            port=port or 102,
            rack=p.get("rack", 0),
            slot=p.get("slot", 1),
        )
    if d == "opcua":
        return OpcUaClient(ip, port or 4840, p.get("path", ""), p.get("endpoint", ""))
    if d == "mtconnect":
        return MTConnectClient(ip, port or 5000)
    raise ValueError("未知 driver:{!r}".format(d))


def _connections() -> List[Dict[str, Any]]:
    cases = [
        {"driver": "modbus_tcp"},
        {"driver": "modbus_rtu", "serial": {"port": "COM3"}},
        {"driver": "melsec_mc_tcp"},
        {"driver": "melsec_mc_udp"},
        {"driver": "melsec_mc_serial", "serial": {"port": "COM4"}},
        {"driver": "melsec_mx"},
        {"driver": "omron_fins_tcp"},
        {"driver": "omron_fins_udp"},
        {"driver": "omron_cip"},
        {"driver": "keyence_hostlink_tcp"},
        {"driver": "keyence_hostlink_udp"},
        {"driver": "keyence_mc_tcp"},
        {"driver": "keyence_mc_udp"},
        {"driver": "keyence_sr"},
        {"driver": "inovance_tcp"},
        {"driver": "inovance_rtu", "serial": {"port": "COM5"}},
        {"driver": "inovance_mc_tcp"},
        {"driver": "panasonic_mc_tcp"},
        {"driver": "panasonic_mewtocol_tcp"},
        {"driver": "panasonic_mewtocol_udp"},
        {"driver": "toyopuc_tcp"},
        {"driver": "toyopuc_udp"},
        {"driver": "opcua"},
        {"driver": "ab_eip"},
        {"driver": "mtconnect"},
        {"driver": "siemens_s7"},
    ]
    out = []
    for case in cases:
        conn = dict(_CONN_BASE)
        conn.update(case)
        out.append(conn)
    return out


@pytest.mark.parametrize("conn", _connections(), ids=lambda c: c["driver"])
def test_build_client_smoke_all_drivers(conn: dict) -> None:
    """全部驱动构造冒烟:签名漂移(位序/参数名)在此暴露。"""
    client = _build(conn)
    assert client is not None


def test_s7_positional_order_matches_current_signature() -> None:
    """S7 专项:显式给 rack/slot/port,断言按现行签名 (ip, port, rack, slot)。"""
    conn = {
        "ip": "192.0.2.10",
        "port": 102,
        "driver": "siemens_s7",
        "params": {"rack": 3, "slot": 5},
    }
    client = _build(conn)
    assert client.rack == 3
    assert client.slot == 5


def test_mc_tcp_xy_octal_passthrough() -> None:
    """MC TCP xy_octal 透传(FX5U 真机核证通道,防静默错址)。"""
    conn = {"ip": "192.0.2.10", "driver": "melsec_mc_tcp", "params": {"xy_octal": True}}
    client = _build(conn)
    assert client._xy_octal is True
    # 缺省 False,配置显式性一致
    client2 = _build({"ip": "192.0.2.10", "driver": "melsec_mc_tcp"})
    assert client2._xy_octal is False


def test_fins_tcp_local_node_none_keeps_auto_mode() -> None:
    """FINS TCP local_node 缺省 None = 握手自动推导(与库默认一致,勿回 0)。

    未连接时 ``local_node`` 恒 0(握手后刷新),自动/显式以
    ``_auto_local_node`` 标志区分。
    """
    conn = {"ip": "192.0.2.10", "driver": "omron_fins_tcp"}
    client = _build(conn)
    assert client._auto_local_node is True
    # 显式节点原样透传,不进自动模式
    client2 = _build(
        {"ip": "192.0.2.10", "driver": "omron_fins_tcp", "params": {"local_node": 10}}
    )
    assert client2._auto_local_node is False
    assert client2.local_node == 10
