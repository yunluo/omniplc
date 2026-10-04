"""tools/manual_common.build_client 全驱动构造冒烟守卫。

背景(第六/八轮连续命中同一盲区):门禁四件套只覆盖 ``src tests``,
tools/ 构造传参漂移无守卫——S7 位序错位曾潜伏五个版本(v0.32.0 起联机
脚本 S7 驱动完全不可用,review-0929-2 P1)。本测试把 27 个驱动的
build_client 构造全部冒烟一遍:构造成功即证明**位序/参数名/缺省值**
与现行库签名一致;再对关键字段的传递做抽查。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import manual_common  # noqa: E402

# 27 驱动与 manual_test.json 同步(构造型参数全部给合法值)
_CONN_BASE = {"ip": "192.0.2.10", "port": None}


def _connections() -> list:
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
    """全部驱动 build_client 构造冒烟:签名漂移(位序/参数名)在此暴露。"""
    client = manual_common.build_client(conn)
    assert client is not None


def test_s7_positional_order_matches_current_signature() -> None:
    """S7 专项:显式给 rack/slot/port,断言按现行签名 (ip, port, rack, slot)。"""
    conn = {"ip": "192.0.2.10", "port": 102, "driver": "siemens_s7",
            "params": {"rack": 3, "slot": 5}}
    client = manual_common.build_client(conn)
    assert client.rack == 3
    assert client.slot == 5


def test_mc_tcp_xy_octal_passthrough() -> None:
    """MC TCP xy_octal 透传(FX5U 真机核证通道,防静默错址)。"""
    conn = {"ip": "192.0.2.10", "driver": "melsec_mc_tcp",
            "params": {"xy_octal": True}}
    client = manual_common.build_client(conn)
    assert client._xy_octal is True
    # 缺省 False,配置显式性一致
    client2 = manual_common.build_client(
        {"ip": "192.0.2.10", "driver": "melsec_mc_tcp"}
    )
    assert client2._xy_octal is False


def test_fins_tcp_local_node_none_keeps_auto_mode() -> None:
    """FINS TCP local_node 缺省 None = 握手自动推导(与库默认一致,勿回 0)。

    未连接时 ``local_node`` 恒 0(握手后刷新),自动/显式以
    ``_auto_local_node`` 标志区分。
    """
    conn = {"ip": "192.0.2.10", "driver": "omron_fins_tcp"}
    client = manual_common.build_client(conn)
    assert client._auto_local_node is True
    # 显式节点原样透传,不进自动模式
    client2 = manual_common.build_client(
        {"ip": "192.0.2.10", "driver": "omron_fins_tcp",
         "params": {"local_node": 10}}
    )
    assert client2._auto_local_node is False
    assert client2.local_node == 10
