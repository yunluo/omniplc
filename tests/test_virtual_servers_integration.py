"""外部模拟器端到端联测(可选环境,无模拟器自动跳过)。

针对本地运行的**第三方 PLC 模拟器网关**(HTTP 8731)的端到端读写验证:
本库作客户端直连模拟器提供的各协议虚拟服务器,做「写 → 读 → 精确对拍」。
覆盖十类数据(SHORT/USHORT/INT/UINT/LONG/ULONG/FLOAT/DOUBLE/BOOL/STRING)
与批量写、区间读。

运行条件与降级:

- 网关未运行(8731 不可达)或某协议虚拟服务器未创建 → 对应用例 **skip**,
  不影响常规门禁(CI 环境恒 skip);
- 各服务器端口由网关 REST 动态发现(按服务器类型查端口),模拟器里
  重建服务器导致端口变化无需改本文件;
- 已知模拟器侧限制(非本库问题,断言口径已按此调整):
  - MC 模拟器不支持 0403 随机读(0406 区间读正常);
  - 欧姆龙 CIP 模拟器 connected 写应答的 CPF 数据项长度域与实际字节不符,
    本库按 CIP 规范严格拒绝——写后以**读回值**断言(值实际写入)。

对拍口径:FLOAT 期望值按 32 位浮点位型折算(3.14 等无法精确表示,
写读位型一致即通过);其余类型精确相等。
"""

from __future__ import annotations

import json
import socket
import struct
import urllib.request

import pytest

from omniplc import (
    AllenBradleyEthIpClient,
    InovanceTcpClient,
    MelsecMcTcpClient,
    ModbusTcpClient,
    OmronCipClient,
    OmronFinsTcpClient,
    OmronFinsUdpClient,
    PanasonicMewtocolTcpClient,
    SiemensS7Client,
    ToyopucTcpClient,
)
from omniplc.plc.melsec.melsec import McFrame
from omniplc.plc.siemens.client import S7Model

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8731

STRING_TEXT = "OMNIPLC"
"""字符串对拍样本(ASCII 7 字符)。"""

_LONG_VALUE = -1234567890123456789
_ULONG_VALUE = 9876543210987654321
_DOUBLE_VALUE = 2.718281828459045
_FLOAT_RAW = 3.14


def _f32(value: float) -> float:
    """按 32 位浮点位型折算期望值(写读位型一致即对拍通过)。"""
    return struct.unpack(">f", struct.pack(">f", value))[0]


_FLOAT_VALUE = _f32(_FLOAT_RAW)

# 数值类型 × 测试值(地址由各协议用例自行规划,互不重叠)
_NUMERIC_CASES = [
    ("short", -12345),
    ("ushort", 56789),
    ("int", -123456789),
    ("uint", 3456789012),
    ("long", _LONG_VALUE),
    ("ulong", _ULONG_VALUE),
    ("float", _FLOAT_VALUE),
    ("double", _DOUBLE_VALUE),
]


def _numeric_roundtrip(client: object, cases: list) -> None:
    """数值类型逐项写读对拍(写与读方法名按小写类型名拼接)。"""
    mismatches = []
    for type_name, address, expected in cases:
        write = getattr(client, "write_" + type_name)
        read = getattr(client, "read_" + type_name)
        written = write(address, expected)
        got = read(address)
        if written is not True or got != (True, expected):
            mismatches.append(
                "{}@{} 写={} 读={}".format(type_name, address, written, got)
            )
    assert not mismatches, ";".join(mismatches)


# ----------------------------------------------------------------------
# 模拟器发现:网关在线探测 + 按服务器类型动态解析端口
# ----------------------------------------------------------------------


def _gateway_online() -> bool:
    try:
        socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=0.5).close()
        return True
    except OSError:
        return False


_SERVER_CACHE: dict = {}
"""网关服务器清单缓存(模块级,一次发现全程复用)。"""


def _discover_servers() -> dict:
    """拉网关服务器清单,返回 ``{服务器类型: (端口, 网络模式)}``。"""
    if _SERVER_CACHE:
        return _SERVER_CACHE
    url = "http://{}:{}/api/servers".format(GATEWAY_HOST, GATEWAY_PORT)
    with urllib.request.urlopen(url, timeout=2) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    for item in payload.get("servers", []):
        if item.get("tcpRunning"):
            _SERVER_CACHE[item["type"]] = (
                int(item.get("tcpPort") or 0),
                str(item.get("networkMode") or "tcp"),
            )
    return _SERVER_CACHE


def _skip_if_missing(server_type: str):
    """目标类型虚拟服务器不在(或网关未运行)时 skip 的 pytest 标记。

    返回 TCP 端口;UDP 走线服务器由 :func:`_udp_port` 处理。
    """
    port, mode = _require_server(server_type)
    if mode == "udp":
        pytest.skip("{} 为 UDP 走线,本用例仅覆盖 TCP".format(server_type))
    return port


def _udp_port(server_type: str) -> int:
    """取 UDP 走线虚拟服务器的端口(不在则 skip)。"""
    port, mode = _require_server(server_type)
    if mode != "udp":
        pytest.skip("{} 当前为 TCP 走线,本用例仅覆盖 UDP".format(server_type))
    return port


def _require_server(server_type: str):
    """按类型取 ``(端口, 网络模式)``,网关未运行或服务器不存在时 skip。"""
    if not _gateway_online():
        pytest.skip("模拟器网关未运行(127.0.0.1:8731)")
    info = _discover_servers().get(server_type)
    if info is None:
        pytest.skip("模拟器未创建 {} 虚拟服务器".format(server_type))
    return info


# ----------------------------------------------------------------------
# 各协议用例
# ----------------------------------------------------------------------


def test_modbus_tcp_server_types() -> None:
    """Modbus:八种数值类型 + 线圈/寄存器位 + 批量写/区间读/字符串。"""
    port = _skip_if_missing("ModbusTcpServer")
    client = ModbusTcpClient(GATEWAY_HOST, port, 1)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "hr400", -12345),
                ("ushort", "hr402", 56789),
                ("int", "hr404", -123456789),
                ("uint", "hr406", 3456789012),
                ("long", "hr410", _LONG_VALUE),
                ("ulong", "hr414", _ULONG_VALUE),
                ("float", "hr430", _FLOAT_VALUE),
                ("double", "hr420", _DOUBLE_VALUE),
            ],
        )
        assert client.write_bool("c20", True) is True
        assert client.read_bool("c20") == (True, True)
        assert client.write_bool("hr500.5", True) is True
        assert client.read_bool("hr500.5") == (True, True)
        assert client.write_string("hr440", STRING_TEXT) is True
        assert client.read_string("hr440", len(STRING_TEXT)) == (True, STRING_TEXT)
        assert client.write_many(
            [("hr300", "short", 11), ("hr301", "short", 22), ("hr302", "short", 33)]
        ) == [True, True, True]
        assert client.read_range("hr300", 3, "short") == (True, [11, 22, 33])
    finally:
        client.disconnect()


def test_melsec_mc_3e_server_types() -> None:
    """MC 3E:八种数值类型 + M 位 + 批量写/区间读/字符串。"""
    port = _skip_if_missing("MelsecMcServer")
    client = MelsecMcTcpClient(GATEWAY_HOST, port)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "D400", -12345),
                ("ushort", "D402", 56789),
                ("int", "D404", -123456789),
                ("uint", "D406", 3456789012),
                ("long", "D410", _LONG_VALUE),
                ("ulong", "D414", _ULONG_VALUE),
                ("float", "D430", _FLOAT_VALUE),
                ("double", "D420", _DOUBLE_VALUE),
            ],
        )
        assert client.write_bool("M200", True) is True
        assert client.read_bool("M200") == (True, True)
        assert client.write_string("D500", STRING_TEXT) is True
        assert client.read_string("D500", len(STRING_TEXT)) == (True, STRING_TEXT)
        assert client.write_many(
            [("D200", "ushort", 7), ("D201", "ushort", 8), ("D202", "ushort", 9)]
        ) == [True, True, True]
        assert client.read_range("D200", 3, "ushort") == (True, [7, 8, 9])
    finally:
        client.disconnect()


def test_inovance_tcp_server_batch_tokens() -> None:
    """汇川批量双记号:汇川软元件记号(D7021)批量读/写直连 Modbus 虚拟服务器。

    现场场景回归:D7021 单点可读而批量报「无法解析 Modbus 地址」——批量
    路径现按「Modbus 记号优先」翻译(汇川记号换算、Modbus 记号原样),
    与单点同收口。
    """
    port = _skip_if_missing("ModbusTcpServer")
    client = InovanceTcpClient(GATEWAY_HOST, port, 1)
    assert client.connect() is True, client.last_error
    try:
        ok = client.write_many([("D400", "short", 11), ("D401", "short", 22)])
        assert ok == [True, True], ok
        rng = client.read_range("D400", 2, "short")
        assert rng == (True, [11, 22]), rng
        values = client.read_many(["D7021", "D7022"], "ushort")
        assert all(ok for ok, _ in values), values
        client.write_ushort("D500", 99)
        batch_ok, batch_values = client.read_batch(
            [("D500", "ushort"), ("D501", "ushort")]
        )
        assert batch_ok is True and batch_values[0] == 99, (batch_ok, batch_values)
    finally:
        client.disconnect()


def test_melsec_mc_1e_server_types() -> None:
    """MC 1E(A1E):字与位读写。"""
    port = _skip_if_missing("MelsecA1EServer")
    client = MelsecMcTcpClient(GATEWAY_HOST, port, frame=McFrame.FRAME_1E)
    assert client.connect() is True, client.last_error
    try:
        assert client.write_ushort("D100", 777) is True
        assert client.read_ushort("D100") == (True, 777)
        assert client.write_bool("M50", True) is True
        assert client.read_bool("M50") == (True, True)
    finally:
        client.disconnect()


def test_omron_fins_tcp_server_types() -> None:
    """FINS/TCP:八种数值类型 + CIO 位 + 区间读/字符串。"""
    port = _skip_if_missing("OmronFinsServer")
    client = OmronFinsTcpClient(GATEWAY_HOST, port)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "D400", -12345),
                ("ushort", "D402", 56789),
                ("int", "D404", -123456789),
                ("uint", "D406", 3456789012),
                ("long", "D410", _LONG_VALUE),
                ("ulong", "D414", _ULONG_VALUE),
                ("float", "D430", _FLOAT_VALUE),
                ("double", "D420", _DOUBLE_VALUE),
            ],
        )
        assert client.write_bool("CIO10.5", True) is True
        assert client.read_bool("CIO10.5") == (True, True)
        assert client.write_string("D500", STRING_TEXT) is True
        assert client.read_string("D500", len(STRING_TEXT)) == (True, STRING_TEXT)
        assert client.read_range("D200", 4, "ushort") == (True, [0, 0, 0, 0])
    finally:
        client.disconnect()


def test_omron_fins_udp_server_types() -> None:
    """FINS/UDP:代表性数值类型(UDP 走线)。"""
    port = _udp_port("OmronFinsUdpServer")
    client = OmronFinsUdpClient(GATEWAY_HOST, port)
    assert client.connect() is True, client.last_error
    try:
        assert client.write_ushort("D300", 1122) is True
        assert client.read_ushort("D300") == (True, 1122)
        assert client.write_float("D310", _FLOAT_VALUE) is True
        assert client.read_float("D310") == (True, _FLOAT_VALUE)
        assert client.write_long("D320", _LONG_VALUE) is True
        assert client.read_long("D320") == (True, _LONG_VALUE)
    finally:
        client.disconnect()


def test_siemens_s7_server_types() -> None:
    """S7:八种数值类型(DB1 非重叠布局)+ M 位 + 字符串。"""
    _skip_if_missing("SiemensS7Server")
    client = SiemensS7Client(GATEWAY_HOST, model=S7Model.S7_1200)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "DB1.DBW40", -12345),
                ("ushort", "DB1.DBW42", 56789),
                ("int", "DB1.DBD44", -123456789),
                ("uint", "DB1.DBD48", 3456789012),
                ("float", "DB1.DBD52", _FLOAT_VALUE),
                ("long", "DB1.DBD60", _LONG_VALUE),
                ("ulong", "DB1.DBD68", _ULONG_VALUE),
                ("double", "DB1.DBD80", _DOUBLE_VALUE),
            ],
        )
        assert client.write_bool("M20.2", True) is True
        assert client.read_bool("M20.2") == (True, True)
        assert client.write_string("DB1.DBS100", STRING_TEXT) is True
        assert client.read_string("DB1.DBS100", 10) == (True, STRING_TEXT)
    finally:
        client.disconnect()


def test_panasonic_mewtocol_server_types() -> None:
    """MEWTOCOL:八种数值类型 + 字符串(直连站号 0xEE)。"""
    port = _skip_if_missing("PanasonicMewtocolServer")
    client = PanasonicMewtocolTcpClient(GATEWAY_HOST, port, station=0xEE)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "D400", -12345),
                ("ushort", "D402", 56789),
                ("int", "D404", -123456789),
                ("uint", "D406", 3456789012),
                ("long", "D410", _LONG_VALUE),
                ("ulong", "D414", _ULONG_VALUE),
                ("float", "D430", _FLOAT_VALUE),
                ("double", "D420", _DOUBLE_VALUE),
            ],
        )
        assert client.write_string("D500", STRING_TEXT) is True
        assert client.read_string("D500", len(STRING_TEXT)) == (True, STRING_TEXT)
    finally:
        client.disconnect()


def test_toyopuc_server_types() -> None:
    """TOYOPUC:八种数值类型 + M 位。"""
    port = _skip_if_missing("ToyoPucServer")
    client = ToyopucTcpClient(GATEWAY_HOST, port)
    assert client.connect() is True, client.last_error
    try:
        _numeric_roundtrip(
            client,
            [
                ("short", "D50", -12345),
                ("ushort", "D52", 56789),
                ("int", "D54", -123456789),
                ("uint", "D56", 3456789012),
                ("long", "D60", _LONG_VALUE),
                ("ulong", "D64", _ULONG_VALUE),
                ("float", "D80", _FLOAT_VALUE),
                ("double", "D70", _DOUBLE_VALUE),
            ],
        )
        assert client.write_bool("M0202", True) is True
        assert client.read_bool("M0202") == (True, True)
    finally:
        client.disconnect()


def test_allen_bradley_server_tag() -> None:
    """AB EtherNet/IP:INT 标签写读对拍(模拟器仅提供 INT 标签)。"""
    port = _skip_if_missing("AllenBradleyServer")
    client = AllenBradleyEthIpClient(GATEWAY_HOST, port)
    assert client.connect() is True, client.last_error
    try:
        first = client.read_short("A")
        assert first[0] is True, "首读 {}".format(first)
        assert client.write_short("A", 1234) is True
        assert client.read_short("A") == (True, 1234)
    finally:
        client.disconnect()


def test_omron_cip_server_tag() -> None:
    """欧姆龙 CIP(connected):INT 标签以读回值断言。

    模拟器 connected 写应答的 CPF 长度域不合规,本库严格拒绝(设计行为),
    写返回值不作断言;值实际写入,以读回值对拍。
    """
    port = _skip_if_missing("OmronCipServer")
    client = OmronCipClient(GATEWAY_HOST, port, connected_messaging=True)
    assert client.connect() is True, client.last_error
    try:
        first = client.read_short("A")
        assert first[0] is True, "首读 {}".format(first)
        client.write_short("A", 4321)
        assert client.read_short("A") == (True, 4321)
    finally:
        client.disconnect()
