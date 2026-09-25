"""FINS 客户端帧收发测试:脚本化传输验证 TCP(握手)与 UDP 全链路。"""
from __future__ import annotations

import asyncio

import pytest

from omniplc import OmronFinsTcpClient, OmronFinsUdpClient
from omniplc.aio import AOmronFinsUdpClient
from omniplc.plc.omron import codec
from omniplc.plc.omron.address import parse_fins_address
from omniplc.plc.omron import omron as omron_module
from scripted import ScriptedTransport

_FINS_ECHO_HEAD = b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00"


def _fins_read_response(values: list) -> bytes:
    """构造区域读响应(测试脚手架,回显节点与 SID=1)。"""
    data = b"".join(value.to_bytes(2, "big") for value in values)
    return _FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + b"\x00\x00" + data


def _fins_write_response() -> bytes:
    """构造区域写响应(测试脚手架,SID=2)。"""
    return b"\xc0\x00\x02\x00\x0a\x00\x00\x05\x00\x02" + b"\x01\x02" + b"\x00\x00"


def _fins_error_response(end_code: int) -> bytes:
    """构造带结束码的读响应(测试脚手架)。"""
    return _FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + end_code.to_bytes(2, "big")


def _handshake_response() -> bytes:
    """构造握手响应(本地节点 11,PLC 节点 5)。"""
    return (
        b"FINS"
        + (16).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00\x0b"
        + b"\x00\x00\x00\x05"
    )


def _tcp_wrap(fins_frame: bytes) -> bytes:
    """FINS/TCP 封帧(测试脚手架)。"""
    body = (2).to_bytes(4, "big") + (0).to_bytes(4, "big") + fins_frame
    return b"FINS" + len(body).to_bytes(4, "big") + body


def test_udp_nodes_default_derived_from_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP 缺省节点号:目标 = PLC IP 末段,源 = 本机出口 IP 末段,进帧 DA1/SA1。"""
    client = OmronFinsUdpClient("192.168.250.1")
    monkeypatch.setattr(omron_module, "_local_ip_for", lambda host, port: "10.1.2.33")
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert client._destination_node == 1  # 192.168.250.1 末段
    assert client._source_node == 33  # 本机出口 IP 末段
    sent = bytes(scripted.sent)
    assert sent[4] == 1  # DA1
    assert sent[7] == 33  # SA1


def test_udp_nodes_explicit_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP 显式节点号:连接后原样保留,不被 IP 推导覆盖。"""
    client = OmronFinsUdpClient("192.168.250.1", destination_node=5, source_node=10)
    monkeypatch.setattr(omron_module, "_local_ip_for", lambda host, port: "10.1.2.33")
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert (client._destination_node, client._source_node) == (5, 10)
    sent = bytes(scripted.sent)
    assert sent[4] == 5 and sent[7] == 10


def test_udp_routing_properties_reflect_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路由参数只读属性:自动推导后反映最新节点号(属性面镜像同步侧)。"""
    client = OmronFinsUdpClient("192.168.250.1")
    monkeypatch.setattr(omron_module, "_local_ip_for", lambda host, port: "10.1.2.33")
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert client.destination_network == 0
    assert client.destination_node == 1
    assert client.destination_unit == 0
    assert client.source_network == 0
    assert client.source_node == 33
    assert client.source_unit == 0


def test_node_from_host_resolves_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """节点号推导:IPv4 直接取末段;主机名先解析再取末段。"""
    import socket

    monkeypatch.setattr(socket, "gethostbyname", lambda host: "192.168.7.9")
    assert omron_module._node_from_host("plc-omron") == 9
    assert omron_module._node_from_host("192.168.250.1") == 1


def test_udp_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:请求帧组帧正确(节点/SID 进帧),响应解析出字数据。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    expected = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("D100"), 1, False
    )
    assert bytes(scripted.sent) == expected


def test_tcp_length_field_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:长度域超限(0xFFFFFFFF)在 recv 前快失败,按坏帧断线不阻塞。"""
    client = OmronFinsTcpClient("127.0.0.1")
    hs = _handshake_response()
    evil = b"FINS" + (0xFFFFFFFF).to_bytes(4, "big")
    scripted = ScriptedTransport([hs[:8], hs[8:], evil])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "长度域超限" in client.last_error


def test_tcp_handshake_and_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:握手 → 节点自动补齐(SA1/DA1)→ 读事务。"""
    client = OmronFinsTcpClient("127.0.0.1")
    hs = _handshake_response()
    tx = _tcp_wrap(_fins_read_response([20]))
    scripted = ScriptedTransport([hs[:8], hs[8:], tx[:8], tx[8:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.local_node == 11
    assert client.read_ushort("D100") == (True, 20)
    sent = bytes(scripted.sent)
    assert sent[:20] == codec.build_handshake(0)
    fins_request = sent[20 + 16:]  # 跳过握手请求(20)与 FINS/TCP 头(16)
    assert fins_request[4] == 5  # DA1 = 握手分配的 PLC 节点
    assert fins_request[7] == 11  # SA1 = 握手分配的本地节点


def test_udp_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:D 区位写 = 读字 →改位→ 写字两段事务。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([0x0004]), _fins_write_response()])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("D100"), 1, False
    ) + codec.build_area_write(
        0, 5, 0, 0, 10, 0, 2, parse_fins_address("D100"), [0x000C], False
    )
    assert bytes(scripted.sent) == expected


def test_udp_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:结束码非 0 → DeviceError,不断线。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_error_response(1)])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "结束码 0x0001" in client.last_error


def test_udp_timer_counter_word_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:T/C 区:字访问为当前值 PV(操作码 0x89),可读写。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_read_response([150]), _fins_write_response()])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("T0") == (True, 150)
    expected_read = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("T0"), 1, False
    )
    assert bytes(scripted.sent[: len(expected_read)]) == expected_read
    assert bytes(scripted.sent)[12] == 0x89  # T/C 字操作码
    assert client.write_ushort("C10", 200) is True
    expected_write = codec.build_area_write(
        0, 5, 0, 0, 10, 0, 2, parse_fins_address("C10"), [200], False
    )
    assert bytes(scripted.sent)[len(expected_read):] == expected_write


def test_udp_timer_flag_read_and_write_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:T/C 完成标志:位读(操作码 0x09)可用;位写与位号后缀拒绝。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_FINS_ECHO_HEAD + b"\x01" + b"\x01\x01" + b"\x00\x00" + b"\x01"])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_bool("T0") == (True, True)
    flag_read = codec.build_area_read(
        0, 5, 0, 0, 10, 0, 1, parse_fins_address("T0"), 1, True
    )
    assert bytes(scripted.sent) == flag_read
    assert bytes(scripted.sent)[12] == 0x09
    with pytest.raises(ValueError):
        client.write_bool("T0", True)  # 完成标志只读
    with pytest.raises(ValueError):
        client.read_bool("T0.3")  # 不带位号
    with pytest.raises(ValueError):
        client.write_bool("C10.2", True)


def test_fins_end_code_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:全表结束码文本进 last_error(0x1101 存储区码非法)。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport([_fins_error_response(0x1101)])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.last_error is not None and "存储区码非法" in client.last_error


def test_tcp_bad_magic_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:响应魔数非法按坏帧处理,标记断开。"""
    client = OmronFinsTcpClient("127.0.0.1")
    hs = _handshake_response()
    scripted = ScriptedTransport([hs[:8], hs[8:], b"\x00" * 8])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "帧头非法" in client.last_error


def _fins_multiple_read_response(entries: list) -> bytes:
    """构造多存储区读响应(测试脚手架,SID=1):每条 = 区码回显 + 字数据。"""
    data = b"".join(
        code.to_bytes(1, "big") + value.to_bytes(2, "big") for code, value in entries
    )
    return _FINS_ECHO_HEAD + b"\x01" + b"\x01\x04" + b"\x00\x00" + data


def test_udp_read_batch_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP read_batch:混类型混软元件单事务,0104 逐条字读后按计划解码。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    # D100=0xFFFE(short -2)、D102-D103=float 1.0(大端 3F80 0000)、CIO0 bit3=1
    scripted = ScriptedTransport(
        [
            _fins_multiple_read_response(
                [(0x82, 0xFFFE), (0x82, 0x3F80), (0x82, 0x0000), (0xB0, 0x0008)]
            )
        ]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, values = client.read_batch(
        [
            ("D100", "short"),
            ("D102", "float"),
            ("CIO0.3", "bool"),
        ]
    )
    assert ok is True and values == [-2, 1.0, True]
    sent = bytes(scripted.sent)
    assert sent[10:12] == b"\x01\x04"
    assert sent[12:] == bytes.fromhex("82006400" "82006600" "82006700" "b0000000")


def test_udp_read_many_single_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP read_many:覆写为 0104 单事务(协议原生批量合并)。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    scripted = ScriptedTransport(
        [_fins_multiple_read_response([(0x82, 7), (0x82, 9)])]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_many(["D0", "D2"], "short") == [(True, 7), (True, 9)]
    sent = bytes(scripted.sent)
    assert sent[10:12] == b"\x01\x04"
    assert sent[12:] == bytes.fromhex("82000000" "82000200")


def test_read_batch_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_batch 拒绝路径:空列表、T/C 完成标志、条目数超限。"""
    client = OmronFinsUdpClient("127.0.0.1", destination_node=5, source_node=10)
    with pytest.raises(ValueError):
        client.read_batch([])
    with pytest.raises(ValueError):
        client.read_batch([("T0", "bool")])
    with pytest.raises(ValueError):
        client.read_batch([("D{}".format(index), "short") for index in range(168)])


def test_async_mirror_read_batch() -> None:
    """异步镜像 read_batch:混类型批量读往返。"""

    async def scenario() -> None:
        client = AOmronFinsUdpClient("127.0.0.1")
        scripted = ScriptedTransport(
            [_fins_multiple_read_response([(0x82, 0xFFFE), (0x82, 0x3F80), (0x82, 0x0000)])]
        )
        client._sync._transport = scripted
        client._sync._connected = True
        assert await client.read_batch([("D100", "short"), ("D102", "float")]) == (
            True,
            [-2, 1.0],
        )
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 校验加固回归测试(v0.36 候选):构造期范围 + 推导范围 + 应答身份回显
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "label"),
    [
        ({"destination_network": 128}, "目标网络号"),
        ({"destination_network": -1}, "目标网络号"),
        ({"destination_node": 128}, "目标节点号"),
        ({"destination_unit": 256}, "目标单元号"),
        ({"source_network": 128}, "源网络号"),
        ({"source_node": 128}, "源节点号"),
        ({"source_unit": 256}, "源单元号"),
    ],
)
def test_fins_constructor_rejects_out_of_range_route(
    kwargs: dict, label: str
) -> None:
    """构造期路由字段范围校验:network 0~127,node 0~127,unit 0~255,越界 ValueError。"""
    with pytest.raises(ValueError) as exc_info:
        OmronFinsUdpClient("192.168.250.1", **kwargs)
    assert label in exc_info.value.args[0]


def test_tcp_constructor_rejects_local_node_out_of_range() -> None:
    """TCP local_node:0~127 范围校验,越界 ValueError。"""
    with pytest.raises(ValueError):
        OmronFinsTcpClient("192.168.250.1", local_node=128)


def test_fins_constructor_accepts_auto_mode_zero() -> None:
    """构造期路由字段合法边界值:0(自动标记)/127(上限)原样接受。"""
    # node=0 表示自动(node 号后续由握手或 IP 推导刷新,见 v0.32 决议)
    client = OmronFinsUdpClient(
        "192.168.250.1",
        destination_node=0,
        source_node=0,
        destination_network=127,
        destination_unit=255,
    )
    assert client.destination_network == 127
    assert client.destination_unit == 255
    assert client._auto_destination_node is True
    assert client._auto_source_node is True


def test_node_from_host_rejects_out_of_range_last_octet() -> None:
    """IP 末段推导节点号超 1~126(以太网 FINS 合法范围)抛 ValueError,提示显式指定。"""
    with pytest.raises(ValueError) as exc_info:
        omron_module._node_from_host("192.168.250.200")
    assert "节点号" in exc_info.value.args[0]
    with pytest.raises(ValueError):
        omron_module._node_from_host("192.168.250.127")
    with pytest.raises(ValueError):
        omron_module._node_from_host("192.168.250.0")


def test_udp_connect_fails_when_derived_node_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UDP 自动模式:PLC IP 末段超 1~126 时,connect 经推导失败语义拒绝。

    与 v0.30 A4 一致——``_after_connect`` 在 connect 的异常收口段调用,
    ValueError 经清理落到干净状态,``connected`` 仍为 False。
    """
    client = OmronFinsUdpClient("192.168.250.200")  # 末段 200 超出 1~126
    scripted = ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is False
    assert client.connected is False
    assert client.last_error is not None and "节点号" in client.last_error


def test_udp_explicit_node_bypasses_derivation_range_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UDP 显式 destination_node:跳过推导,即使 PLC IP 末段超限也照常组帧。"""
    client = OmronFinsUdpClient(
        "192.168.250.200", destination_node=5, source_node=10
    )
    scripted = ScriptedTransport([_fins_read_response([20])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert client._destination_node == 5
