"""原生异步欧姆龙 FINS 客户端测试:同步/异步**对拍** + 握手 + 节点推导。

对拍覆盖 TCP(含 FINS/TCP 握手)与 UDP:同一批响应分片喂同步与异步客户端,
断言请求帧逐字节相同、结果与错误口径一致。UDP 节点推导单独验证(自动模式下
每次连接从 IP 重新推导,是连接钩子由同步改协程后最容易走样的地方)。
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, Dict, NamedTuple, Optional, Sequence, Tuple

import pytest

from omniplc import OmronFinsTcpClient, OmronFinsUdpClient
from omniplc.core.errors import ErrorCategory
from omniplc.native import AsyncOmronFinsTcpClient, AsyncOmronFinsUdpClient
from omniplc.plc.omron import codec
from omniplc.plc.omron import omron as omron_module
from omniplc.plc.omron.address import parse_fins_address
from omniplc.native import omron as native_omron_module
from omniplc.core.tag import Tag, TagTable
from omniplc.core.types import DataType, PrimitiveValue
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


def _fins_requests(data: bytes, datagram: bool) -> list:
    """拆出脚本记录到的 FINS 请求帧,返回 ``[(命令 2 字节, 帧字节), ...]``。

    命令域在 FINS 帧偏移 10(帧头 10 字节:ICF..SID);TCP 走线先有握手请求
    (命令+错误码+节点共 12 字节,无 FINS 载荷,按"剩余不足帧头 10 字节"跳过)。
    """
    if datagram:
        return [(data[10:12], data)] if data else []
    frames = []
    offset = 0
    while offset < len(data):
        length = int.from_bytes(data[offset + 4:offset + 8], "big")
        body = data[offset + 8:offset + 8 + length]
        if len(body) >= 8 + 10:  # 命令(4)+错误码(4)+ 至少一个完整 FINS 帧头
            frames.append((body[18:20], body[8:]))
        offset += 8 + length
    return frames


_READ_RESP = _fins_response(1, 0x0101, data=_words_be([20]))  # D100 = 20
_BIT_READ_RESP = _fins_response(1, 0x0101, data=b"\x01")  # 1 点 ON
_WRITE_RESP = _fins_response(1, 0x0102)
_ERROR_RESP = _fins_response(1, 0x0101, end_code=0x0001)
_SID_MISMATCH_RESP = _fins_response(99, 0x0101, data=_words_be([20]))
_INT_RESP = _fins_response(1, 0x0101, data=_words_be([0xFFFE, 0xFFFF]))  # -2(补码,低字在前)
_LONG_RESP = _fins_response(
    1, 0x0101, data=_words_be([0xFFFE, 0xFFFF, 0xFFFF, 0xFFFF])
)
_FLOAT_RESP = _fins_response(1, 0x0101, data=_words_be([0x0000, 0x3FC0]))  # 1.5f
_DOUBLE_RESP = _fins_response(
    1, 0x0101, data=_words_be([0x0000, 0x0000, 0x0000, 0x3FF8])
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

    async def _cancel_pending() -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 收尾取消残留任务(如用例未显式断开的客户端心跳任务):loop.close()
    # 对未完成任务会报 "Task was destroyed" 噪声,掩盖真实失败
    event_loop.run_until_complete(_cancel_pending())
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


def test_udp_auto_nodes_from_hostname_without_blocking_dns(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """主机名目标:节点推导复用传输层已解析的对端 IP,事件循环内不做阻塞 DNS。

    修复前 ``_after_connect`` 把主机名直接交给 ``_node_from_host``(内部
    ``socket.gethostbyname``)与 ``_local_ip_for``(内部 UDP ``connect`` 也要
    解析),实测主机名场景把事件循环卡住 260ms(慢 DNS 更久)。现在一律用
    :attr:`AsyncUdpTransport.peer_ip`;把 ``gethostbyname`` 换成"一调就炸"即可
    锁死这条路径,且取值与 IP 字面量目标逐值一致(localhost → 127.0.0.1)。
    """

    def _boom(host: str) -> str:
        raise AssertionError("事件循环里不应出现阻塞解析:{}".format(host))

    monkeypatch.setattr(socket, "gethostbyname", _boom)

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient("localhost")  # 真实 UDP 传输(不挂假传输)
        assert await client.connect() is True
        assert client.destination_node == 1  # PLC 侧:127.0.0.1 末段
        assert client.source_node == 1  # 本机出口 IP 同段(127.0.0.1)
        await client.close()

    loop.run_until_complete(scenario())


def test_word_area_bit_write_direct_parity(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """字区(D)按位写直接位写(0102 位码,手册 §5-3-3 可写表);同步/异步帧一致。"""
    write_resp = _fins_response(1, 0x0102)
    chunks = list(_tcp_chunks(write_resp))

    sync_client = OmronFinsTcpClient(
        "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
    )
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    assert sync_client.write_bool("D100.3", True) is True
    # 首笔事务为位码直写:D 区位码 0x02,地址含位号 3(帧布局:码[12] 字[13:15] 位[15])
    first_fins = bytes(sync_scripted.sent)[20 + 16:]
    assert first_fins[12] == 0x02
    assert first_fins[15] == 3

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
    # 两帧带魔数:握手请求 + 位写(各自的 TCP 封装)
    assert holder["sent"].count(b"FINS") == 2


def test_d_area_bit_write_falls_back_on_1101_parity(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """字区(D)位写遇 0x1101(老固件不支持位区码)回退读-改-写;同步/异步一致。"""
    err = _fins_response(1, 0x0102, end_code=0x1101)
    read_resp = _fins_response(2, 0x0101, data=b"\x00\x00")
    write_resp = _fins_response(3, 0x0102)
    chunks = list(_tcp_chunks(err, read_resp, write_resp))

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
    # 四帧带魔数:握手 + 直写(0102)+ 读(0101)+ 写(0102)
    assert holder["sent"].count(b"FINS") == 4


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


# ----------------------------------------------------------------------
# 批量读(0104 多存储区读):与同步层同帧同解析
# ----------------------------------------------------------------------

_F32_1_5_BE = [0x0000, 0x3FC0]  # 1.5f:FINS 字内大端、低字在前(D100=0000/D101=3FC0)


class ExtCase(NamedTuple):
    """批量读对拍用例。"""

    name: str
    datagram: bool
    op: str
    args: Tuple[Any, ...]
    responses: Sequence[bytes]
    expect_fc: Tuple[str, ...]
    """按序期望的命令域(线序十六进制;0104 在线序即 ``01 04``)。"""


_EXT_CASES = [
    ExtCase(
        "udp_read_batch_mixed", True, "read_batch",
        ((("D100", "ushort"), ("D101", "float"), ("CIO0.5", "bool")),),
        (_fins_response(1, 0x0104, data=_words_be([7] + _F32_1_5_BE + [0x0020])),),
        ("0104",),
    ),
    ExtCase(
        "udp_read_many", True, "read_many", (("D100", "D101"), "ushort"),
        (_fins_response(1, 0x0104, data=_words_be([10, 20])),), ("0104",),
    ),
    ExtCase(
        "tcp_read_batch", False, "read_batch",
        ((("D100", "long"),),),
        tuple(_tcp_chunks(_fins_response(1, 0x0104, data=_words_be([0xFFFE, 0xFFFF, 0xFFFF, 0xFFFF])))),
        ("0104",),
    ),
    # T/C 完成标志是位区,0104 只有字码 → 入参期拒绝(零字节发送)
    ExtCase("read_batch_tc_rejected", True, "read_batch", ((("T0", "bool"),),), (), ()),
    # 不支持的类型(字符串变长)同样入参期拒绝
    ExtCase("read_batch_string_rejected", True, "read_batch", ((("D100", "STRING"),),), (), ()),
]


def _call_ext(client: Any, case: ExtCase) -> Any:
    """按用例调用批量读(同步返回结果,异步返回协程)。"""
    op, args = case.op, case.args
    if op == "read_many":
        return client.read_many(*args)
    if op == "read_batch":
        return client.read_batch(*args)
    raise AssertionError("未知扩展用例操作:{}".format(op))


@pytest.mark.parametrize("case", _EXT_CASES, ids=[case.name for case in _EXT_CASES])
def test_sync_async_parity_batch(
    monkeypatch: pytest.MonkeyPatch, loop: Any, case: ExtCase
) -> None:
    """批量读对拍:请求帧逐字节相同 + 结果/错误口径一致 + 命令域符合期望。"""
    sync_client = _make_sync_client(case)
    sync_scripted = ScriptedTransport(list(case.responses), datagram=case.datagram)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    assert sync_client.connect() is True
    if case.expect_fc:
        sync_result = _call_ext(sync_client, case)
    else:
        with pytest.raises(ValueError):
            _call_ext(sync_client, case)
        sync_result = None
    sync_state = _snapshot(sync_client)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = _make_async_client(case)
        scripted = ScriptedAsyncTransport(list(case.responses), datagram=case.datagram)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        if case.expect_fc:
            holder["result"] = await _call_ext(client, case)
        else:
            with pytest.raises(ValueError):
                await _call_ext(client, case)
            holder["result"] = None
        holder["sent"] = bytes(scripted.sent)
        holder["state"] = _snapshot(client)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["sent"] == bytes(sync_scripted.sent), "请求帧必须逐字节相同"
    assert holder["result"] == sync_result
    assert holder["state"] == sync_state
    if case.expect_fc:
        assert tuple(
            command.hex()
            for command, _frame in _fins_requests(bytes(sync_scripted.sent), case.datagram)
        ) == case.expect_fc


def test_d_area_bit_read_falls_back_on_1101(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """native D 区位读遇 0x1101:回退字读 + 本地提位(镜像同步侧)。

    回归:native ``_read_bit_impl`` 未镜像同步侧的 0x1101 回退,老固件
    (CP1E/部分 CS1)D/EM 区位读直接失败。
    """

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        error = _fins_response(1, 0x0101, end_code=0x1101)  # 位读(SID=1)被拒
        word = _fins_response(2, 0x0101, data=_words_be([0x0008]))  # bit3 置位
        scripted = ScriptedAsyncTransport([error, word], datagram=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_bool("D100.3") == (True, True)
        assert client.connected is True
        await client.close()

    loop.run_until_complete(scenario())


# ----------------------------------------------------------------------
# CPU Unit Status Read(0601)与探活
# ----------------------------------------------------------------------

_CPU_STATUS_DATA = (
    bytes([0x01, 0x04])
    + (0).to_bytes(2, "big") * 4
    + b"\x20" * 16
)
"""0601 应答数据(SID 无关):RUN + RUN 模式 + 错误字全 0 + 空错误消息。"""
_CPU_STATUS_RESP_1 = _fins_response(1, 0x0601, data=_CPU_STATUS_DATA)
_CPU_STATUS_RESP_2 = _fins_response(2, 0x0601, data=_CPU_STATUS_DATA)


def test_native_0601_status_and_ping(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """原生 0601:命令帧仅命令码,应答按 W342 §5-3-17 解码;ping 复用同一命令。"""

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        assert client.ping_supported is True
        scripted = ScriptedAsyncTransport(
            [_CPU_STATUS_RESP_1, _CPU_STATUS_RESP_2], datagram=True
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_cpu_unit_status() == (
            True,
            {
                "status": 0x01,
                "run": True,
                "mode": 0x04,
                "fatal_error": 0,
                "nonfatal_error": 0,
                "message_flags": 0,
                "error_code": 0,
                "error_message": "",
            },
        )
        assert await client.ping() is True
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    holder: Dict[str, Any] = {}
    loop.run_until_complete(scenario())
    assert holder["sent"] == (
        codec.build_cpu_unit_status_read(0, _DEST_NODE, 0, 0, _SRC_NODE, 0, 1)
        + codec.build_cpu_unit_status_read(0, _DEST_NODE, 0, 0, _SRC_NODE, 0, 2)
    )


def test_native_clock_read_write(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """原生 0701/0702:帧与同步侧同构,BCD 解码 + datetime 换算 + 范围门控。"""
    import datetime as dt_module

    from omniplc.plc.omron.codec import FinsClock

    clock_data = bytes([0x26, 0x10, 0x03, 0x14, 0x09, 0x05, 0x06])
    resp_read = _fins_response(1, 0x0701, data=clock_data)
    resp_write = _fins_response(2, 0x0702)

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        scripted = ScriptedAsyncTransport([resp_read, resp_write], datagram=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, clock = await client.read_clock()
        assert ok is True
        assert clock == FinsClock(26, 10, 3, 14, 9, 5, 6)
        assert clock is not None and clock.to_datetime() == dt_module.datetime(
            2026, 10, 3, 14, 9, 5
        )
        ok_write = await client.write_clock(dt_module.datetime(2026, 10, 3, 14, 9, 5))
        assert ok_write is True
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    holder: Dict[str, Any] = {}
    loop.run_until_complete(scenario())
    assert holder["sent"] == (
        codec.build_clock_read(0, _DEST_NODE, 0, 0, _SRC_NODE, 0, 1)
        + codec.build_clock_write(
            0, _DEST_NODE, 0, 0, _SRC_NODE, 0, 2, FinsClock(26, 10, 3, 14, 9, 5, 6)
        )
    )


def test_native_clock_write_range_validation() -> None:
    """原生 0702 字段越界:入参期 ValueError(未连接即抛,零字节发送)。"""
    from omniplc.plc.omron.codec import FinsClock

    client = AsyncOmronFinsUdpClient(
        "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
    )
    with pytest.raises(ValueError):
        asyncio.run(client.write_clock(FinsClock(26, 13, 3, 14, 9, 5, 6)))
    with pytest.raises(ValueError):
        asyncio.run(client.write_clock(FinsClock(26, 10, 3, 14, 9, 5, 7)))


def test_native_read_range_words_and_bits(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """native read_range:D 区字读(低字在前解码)+ CIO 位区连续位读。"""

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        # 字读响应:32 位值低字存低地址(100 = [100, 0],200 = [200, 0])
        words_data = b"".join(v.to_bytes(2, "big") for v in [100, 0, 200, 0])
        resp_words = _fins_response(1, 0x0101, words_data)
        bits_data = bytes([1, 0, 1, 1, 0, 0, 1, 0])
        resp_bits = _fins_response(2, 0x0101, bits_data)
        scripted = ScriptedAsyncTransport([resp_words, resp_bits], datagram=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, values = await client.read_range("D0", 2, DataType.UINT)
        assert ok is True
        assert values == [100, 200]
        ok, values = await client.read_range("CIO0", 8, DataType.BOOL)
        assert ok is True
        assert values == [True, False, True, True, False, False, True, False]
        await client.close()

    loop.run_until_complete(scenario())


def test_native_read_range_rejects(loop: Any) -> None:
    """native read_range 入参校验:T/C 完成标志、字区 BOOL、位号、STRING、超限。"""

    async def scenario() -> None:
        client = AsyncOmronFinsUdpClient(
            "192.168.250.1", 9600, destination_node=_DEST_NODE, source_node=_SRC_NODE
        )
        with pytest.raises(ValueError):
            await client.read_range("T0", 2, DataType.BOOL)
        with pytest.raises(ValueError):
            await client.read_range("D100", 2, DataType.BOOL)
        with pytest.raises(ValueError):
            await client.read_range("D100.3", 2, DataType.BOOL)
        with pytest.raises(ValueError):
            await client.read_range("D100", 2, DataType.STRING)
        with pytest.raises(ValueError):
            await client.read_range("D0", 500, DataType.UINT)  # 1000 字 > 999
        with pytest.raises(ValueError):
            await client.read_range("CIO0", 1000, DataType.BOOL)  # 位数同受 999
        await client.close()

    loop.run_until_complete(scenario())
