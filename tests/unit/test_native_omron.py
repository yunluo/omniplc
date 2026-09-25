"""原生异步欧姆龙 FINS 客户端测试:同步/异步**对拍** + 握手 + 节点推导。

对拍覆盖 TCP(含 FINS/TCP 握手)与 UDP:同一批响应分片喂同步与异步客户端,
断言请求帧逐字节相同、结果与错误口径一致。UDP 节点推导单独验证(自动模式下
每次连接从 IP 重新推导,是连接钩子由同步改协程后最容易走样的地方)。
"""
from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional, Sequence

import pytest

from omniplc import OmronFinsTcpClient, OmronFinsUdpClient
from omniplc.core.errors import ErrorCategory
from omniplc.native import AsyncOmronFinsTcpClient, AsyncOmronFinsUdpClient
from omniplc.plc.omron import codec
from omniplc.plc.omron import omron as omron_module
from omniplc.plc.omron.address import parse_fins_address
from omniplc.native import omron as native_omron_module
from omniplc.tag import Tag, TagTable
from omniplc.types import DataType, PrimitiveValue
from scripted import ScriptedTransport
from scripted_async import ScriptedAsyncTransport, loop_names, make_loop

# 请求/响应里的路由字段(用例统一显式指定节点号,避免依赖本机出口 IP)
_DEST_NODE = 5
_SRC_NODE = 10


def _fins_response(
    sid: int, command: int, data: bytes = b"", end_code: int = 0
) -> bytes:
    """构造 FINS 响应:回显 ICF(0xC0)/SID/命令码,节点字段固定。"""
    head = b"\xc0\x00\x00\x02\x00" + bytes([_DEST_NODE]) + b"\x00\x00\x05\x00"
    head = head[:9] + bytes([sid])
    return head + command.to_bytes(2, "big") + end_code.to_bytes(2, "big") + data


def _handshake_response() -> bytes:
    """构造 FINS/TCP 握手响应(本地节点 11,PLC 节点 5)。"""
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


def _tcp_chunks(*fins_frames: bytes) -> Sequence[bytes]:
    """TCP 收包阶段切分:握手响应(8+16)+ 每个事务响应(8+长度域)。"""
    handshake = _handshake_response()
    chunks: list = [handshake[:8], handshake[8:]]
    for fins_frame in fins_frames:
        wrapped = _tcp_wrap(fins_frame)
        chunks.append(wrapped[:8])
        chunks.append(wrapped[8:])
    return chunks


def _words_be(values: Sequence[int]) -> bytes:
    """字序列按 FINS 大端字序拼字节(测试脚手架)。"""
    return b"".join(value.to_bytes(2, "big") for value in values)


_READ_RESP = _fins_response(1, 0x0101, data=_words_be([20]))  # D100 = 20
_BIT_READ_RESP = _fins_response(1, 0x0101, data=b"\x01")  # 1 点 ON
_WRITE_RESP = _fins_response(1, 0x0102)
_ERROR_RESP = _fins_response(1, 0x0101, end_code=0x0001)
_SID_MISMATCH_RESP = _fins_response(99, 0x0101, data=_words_be([20]))
_INT_RESP = _fins_response(1, 0x0101, data=_words_be([0xFFFF, 0xFFFE]))  # -2(补码)
_LONG_RESP = _fins_response(
    1, 0x0101, data=_words_be([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFE])
)
_FLOAT_RESP = _fins_response(1, 0x0101, data=_words_be([0x3FC0, 0x0000]))  # 1.5f
_DOUBLE_RESP = _fins_response(
    1, 0x0101, data=_words_be([0x3FF8, 0x0000, 0x0000, 0x0000])
)  # 1.5d
_STRING_RESP = _fins_response(1, 0x0101, data=_words_be([0x4F4D, 0x4E49]))  # "OMNI"

# 点位表用例:scale/offset 取整数倍,保证逆缩放无浮点误差
_TAG = Tag(tag_id="flow", address="D100", data_type="ushort", scale=2.0, offset=10.0)


class Case(NamedTuple):
    """一条对拍用例(响应按收包阶段切分喂入)。"""

    name: str
    datagram: bool
    op: str
    address: str
    data_type: DataType
    value: Optional[PrimitiveValue]
    responses: Sequence[bytes]
    expect_ok: bool
    expect_value: Optional[PrimitiveValue]
    expect_connected: bool
    expect_category: Optional[ErrorCategory]
    length: int = 4
    tag: Optional[Tag] = None


_CASES = [
    Case("udp_read", True, "read", "D100", DataType.USHORT, None, (_READ_RESP,), True, 20, True, None),
    Case("udp_read_bit", True, "read", "CIO0.5", DataType.BOOL, None, (_BIT_READ_RESP,), True, True, True, None),
    Case("udp_read_int", True, "read", "D100", DataType.INT, None, (_INT_RESP,), True, -2, True, None),
    Case("udp_read_double", True, "read", "D100", DataType.DOUBLE, None, (_DOUBLE_RESP,), True, 1.5, True, None),
    Case("udp_read_string", True, "read_string", "D100", DataType.STRING, None, (_STRING_RESP,), True, "OMNI", True, None),
    Case("udp_write", True, "write", "D100", DataType.USHORT, 20, (_WRITE_RESP,), True, None, True, None),
    Case("udp_write_string", True, "write_string", "D100", DataType.STRING, "OMNI", (_WRITE_RESP,), True, None, True, None),
    Case("udp_read_tag", True, "read_tag", "D100", DataType.USHORT, None, (_READ_RESP,), True, 50.0, True, None, tag=_TAG),
    Case("udp_write_tag", True, "write_tag", "D100", DataType.USHORT, 50.0, (_WRITE_RESP,), True, None, True, None, tag=_TAG),
    Case("udp_device_error", True, "read", "D100", DataType.USHORT, None, (_ERROR_RESP,), False, None, True, ErrorCategory.DEVICE),
    Case("udp_sid_mismatch", True, "read", "D100", DataType.USHORT, None, (_SID_MISMATCH_RESP,), False, None, False, ErrorCategory.PROTOCOL),
    Case("tcp_read", False, "read", "D100", DataType.USHORT, None, _tcp_chunks(_READ_RESP), True, 20, True, None),
    Case("tcp_read_bit", False, "read", "CIO0.5", DataType.BOOL, None, _tcp_chunks(_BIT_READ_RESP), True, True, True, None),
    Case("tcp_read_long", False, "read", "D100", DataType.LONG, None, _tcp_chunks(_LONG_RESP), True, -2, True, None),
    Case("tcp_read_float", False, "read", "D100", DataType.FLOAT, None, _tcp_chunks(_FLOAT_RESP), True, 1.5, True, None),
    Case("tcp_read_string", False, "read_string", "D100", DataType.STRING, None, _tcp_chunks(_STRING_RESP), True, "OMNI", True, None),
    Case("tcp_write", False, "write", "D100", DataType.USHORT, 20, _tcp_chunks(_WRITE_RESP), True, None, True, None),
    Case("tcp_write_string", False, "write_string", "D100", DataType.STRING, "OMNI", _tcp_chunks(_WRITE_RESP), True, None, True, None),
    Case("tcp_read_tag", False, "read_tag", "D100", DataType.USHORT, None, _tcp_chunks(_READ_RESP), True, 50.0, True, None, tag=_TAG),
    Case("tcp_write_tag", False, "write_tag", "D100", DataType.USHORT, 50.0, _tcp_chunks(_WRITE_RESP), True, None, True, None, tag=_TAG),
    Case("tcp_device_error", False, "read", "D100", DataType.USHORT, None, _tcp_chunks(_ERROR_RESP), False, None, True, ErrorCategory.DEVICE),
    Case("tcp_sid_mismatch", False, "read", "D100", DataType.USHORT, None, _tcp_chunks(_SID_MISMATCH_RESP), False, None, False, ErrorCategory.PROTOCOL),
]


def _call(client: Any, case: Case) -> Any:
    if case.op == "write":
        return client.write(case.address, case.data_type, case.value)
    if case.op == "write_string":
        return client.write_string(case.address, str(case.value))
    if case.op == "read_string":
        return client.read_string(case.address, case.length)
    if case.op == "read_tag":
        assert case.tag is not None
        return client.read_tag(case.tag.tag_id)
    if case.op == "write_tag":
        assert case.tag is not None
        return client.write_tag(case.tag.tag_id, case.value)
    return client.read(case.address, case.data_type)


def _snapshot(client: Any) -> Dict[str, Any]:
    stats = client.stats
    return {
        "connected": client.connected,
        "last_error": client.last_error,
        "last_error_category": client.last_error_category,
        "last_error_code": client.last_error_code,
        "transactions": stats["transactions"],
        "error_count": stats["error_count"],
        "device_error_count": stats["device_error_count"],
    }


def _make_sync_client(case: Case) -> Any:
    cls = OmronFinsUdpClient if case.datagram else OmronFinsTcpClient
    return cls(
        "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
    )


def _make_async_client(case: Case) -> Any:
    cls = AsyncOmronFinsUdpClient if case.datagram else AsyncOmronFinsTcpClient
    return cls(
        "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
    )


@pytest.fixture(params=loop_names())
def loop(request: pytest.FixtureRequest) -> Any:
    """按事件循环类参数化的循环(Windows 上 Selector/Proactor 双跑)。"""
    event_loop = make_loop(request.param)
    yield event_loop
    event_loop.close()


@pytest.mark.parametrize("case", _CASES, ids=[case.name for case in _CASES])
def test_sync_async_parity(
    monkeypatch: pytest.MonkeyPatch, loop: Any, case: Case
) -> None:
    """对拍:请求帧逐字节相同 + 结果/连接态/错误口径/计数完全一致。"""
    sync_client = _make_sync_client(case)
    if case.tag is not None:
        sync_client.bind_tags(TagTable([case.tag]))
    sync_scripted = ScriptedTransport(list(case.responses), datagram=case.datagram)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    assert sync_client.connect() is True
    sync_result = _call(sync_client, case)
    sync_state = _snapshot(sync_client)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = _make_async_client(case)
        if case.tag is not None:
            client.bind_tags(TagTable([case.tag]))
        scripted = ScriptedAsyncTransport(list(case.responses), datagram=case.datagram)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await _call(client, case)
        holder["sent"] = bytes(scripted.sent)
        holder["state"] = _snapshot(client)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["sent"] == bytes(sync_scripted.sent), "请求帧必须逐字节相同"
    assert holder["result"] == sync_result
    if case.op in ("write", "write_string", "write_tag"):
        assert holder["result"] is case.expect_ok
    else:
        assert holder["result"][0] is case.expect_ok
        assert holder["result"][1] == case.expect_value
    assert holder["state"] == sync_state
    assert holder["state"]["connected"] is case.expect_connected
    assert holder["state"]["last_error_category"] is case.expect_category


def test_tcp_handshake_populates_auto_nodes(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """TCP 握手:自动模式的本地/源/目标节点取握手分配值,显式值不被覆盖。"""

    async def scenario() -> None:
        client = AsyncOmronFinsTcpClient("192.168.250.1")
        scripted = ScriptedAsyncTransport(
            list(_tcp_chunks(_READ_RESP)), datagram=False
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert client.local_node == 11  # 握手分配
        assert client.destination_node == 5  # PLC 节点
        assert client.source_node == 11
        assert await client.read_ushort("D100") == (True, 20)
        await client.close()

    loop.run_until_complete(scenario())


def test_tcp_explicit_nodes_not_overridden(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """TCP 握手:显式配置的节点号不被握手结果覆盖(与同步层同口径)。"""

    async def scenario() -> None:
        client = AsyncOmronFinsTcpClient(
            "192.168.250.1", destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        scripted = ScriptedAsyncTransport(list(_tcp_chunks(_READ_RESP)))
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert client.destination_node == _DEST_NODE
        assert client.source_node == _SRC_NODE
        await client.close()

    loop.run_until_complete(scenario())


def test_udp_auto_nodes_derived_from_ip(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """UDP 无握手:自动模式目标节点取 PLC IP 末段、源节点取本机出口 IP 末段。"""
    monkeypatch.setattr(
        omron_module, "_local_ip_for", lambda host, port: "10.1.2.33"
    )
    monkeypatch.setattr(
        native_omron_module, "_local_ip_for", lambda host, port: "10.1.2.33"
    )

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient("192.168.250.1")
        scripted = ScriptedAsyncTransport([_READ_RESP], datagram=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert client.destination_node == 1  # 192.168.250.1 末段
        assert client.source_node == 33  # 本机出口 IP 末段
        assert await client.read_ushort("D100") == (True, 20)
        sent = bytes(scripted.sent)
        assert sent[4] == 1  # DA1
        assert sent[7] == 33  # SA1
        await client.close()

    loop.run_until_complete(scenario())


def test_word_area_bit_write_is_read_modify_write(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """字区(D)按位写走读-改-写两帧;位区(CIO)直接位写(与同步层一致)。"""
    read_resp = _fins_response(1, 0x0101, data=b"\x00\x00")
    write_resp = _fins_response(2, 0x0102)
    chunks = list(_tcp_chunks(read_resp, write_resp))

    sync_client = OmronFinsTcpClient(
        "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
    )
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    assert sync_client.write_bool("D100.3", True) is True

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncOmronFinsTcpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        holder["ok"] = await client.write_bool("D100.3", True)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["ok"] is True
    assert holder["sent"] == bytes(sync_scripted.sent)
    # 三帧带魔数:握手请求 + 读 0401 + 写 0102(各自的 TCP 封装)
    assert holder["sent"].count(b"FINS") == 3


def test_expected_request_frame_matches_codec(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """异步请求帧与 codec 直接构造的期望帧逐字节一致(独立于同步客户端)。"""
    expected = codec.build_area_read(
        0,
        _DEST_NODE,
        0,
        0,
        _SRC_NODE,
        0,
        1,
        parse_fins_address("D100"),
        1,
        False,
    )

    async def scenario() -> None:
        client = AsyncOmronFinsTcpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        scripted = ScriptedAsyncTransport(list(_tcp_chunks(_READ_RESP)))
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        assert await client.read_ushort("D100") == (True, 20)
        # 去掉握手(20 字节)与 TCP 封装(魔数 4 + 长度 4 + 命令 4 + 错误 4)
        # 后的 FINS 载荷即事务请求
        payload = bytes(scripted.sent)[20 + 16 :]
        assert payload == expected
        await client.close()

    loop.run_until_complete(scenario())
