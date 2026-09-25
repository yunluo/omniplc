"""原生异步三菱 MC 客户端测试:同步/异步**对拍** + 真服务端 + 双事件循环。

对拍覆盖 TCP/UDP × 1E/3E:同一批响应分片喂同步与异步客户端,断言请求帧逐字节
相同、结果与错误口径一致(异步侧只重写了薄分发层,帧语义必须与同步层一致)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, NamedTuple, Optional, Sequence

import pytest

from omniplc import MelsecMcTcpClient, MelsecMcUdpClient
from omniplc.core.constants import MC_DEFAULT_MONITOR_TIMER
from omniplc.core.errors import ErrorCategory
from omniplc.native import AsyncMelsecMcTcpClient, AsyncMelsecMcUdpClient
from omniplc.plc.melsec import codec_qna
from omniplc.plc.melsec.address import parse_mc_address
from omniplc.plc.melsec.melsec import _encode_32
from omniplc.types import DataType, PrimitiveValue
from scripted import ScriptedTransport
from scripted_async import RawTcpServer, ScriptedAsyncTransport, loop_names, make_loop


# ----------------------------------------------------------------------
# 响应构造脚手架(与同步侧 test_mc_clients.py 同口径)
# ----------------------------------------------------------------------


def _qna_read_response(values: Sequence[int], serial: int = 0, end_code: int = 0) -> bytes:
    """构造 3E 读响应。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + end_code.to_bytes(2, "little") + data


def _qna_write_response(serial: int = 0) -> bytes:
    """构造 3E 写响应。"""
    return _qna_read_response([], serial=serial)


def _one_e_read_response(values: Sequence[int]) -> bytes:
    """构造 1E 字读响应(2 字节副头 + 数据)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    return bytes([0x81, 0x00]) + data


def _one_e_write_response() -> bytes:
    """构造 1E 字写响应(副头 = 字写 0x03 + 0x80)。"""
    return bytes([0x83, 0x00])


class Case(NamedTuple):
    """一条对拍用例(响应按 recv 阶段切分喂入)。"""

    name: str
    frame: str
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


_3E_READ = _qna_read_response([20])
_3E_WRITE = _qna_write_response()
_3E_FLOAT = _qna_read_response(_encode_32(-1.5, DataType.FLOAT))
_1E_READ = _one_e_read_response([20])
_1E_WRITE = _one_e_write_response()
# 1E 位读响应副头 = 位读请求副头(0x00) + 0x80;1 点位打包在高半字节(bit4)
_1E_BIT_READ = bytes([0x80, 0x00, 0x10])
_3E_BIT_READ = b"\xd0\x00\x00\xff\xff\x03\x00\x03\x00\x00\x00\x10"
_3E_BAD_SUBHEAD = bytes([0x50, 0x00, 0x00, 0xFF, 0xFF, 0x03, 0x00, 0x02, 0x00, 0x00, 0x00])


def _split_3e(frame: bytes) -> Sequence[bytes]:
    return (frame[:9], frame[9:])


def _split_1e(frame: bytes) -> Sequence[bytes]:
    return (frame[:2], frame[2:])


_CASES = [
    Case("tcp_3e_read", "3E", False, "read", "D100", DataType.USHORT, None, _split_3e(_3E_READ), True, 20, True, None),
    Case("tcp_3e_read_float", "3E", False, "read", "D100", DataType.FLOAT, None, _split_3e(_3E_FLOAT), True, -1.5, True, None),
    Case("tcp_3e_write", "3E", False, "write", "D100", DataType.USHORT, 20, _split_3e(_3E_WRITE), True, None, True, None),
    Case("tcp_3e_read_bit", "3E", False, "read", "M10", DataType.BOOL, None, _split_3e(_3E_BIT_READ), True, True, True, None),
    Case("tcp_1e_read", "1E", False, "read", "D100", DataType.USHORT, None, _split_1e(_1E_READ), True, 20, True, None),
    Case("tcp_1e_write", "1E", False, "write", "D100", DataType.USHORT, 20, _split_1e(_1E_WRITE), True, None, True, None),
    Case("tcp_1e_read_bit", "1E", False, "read", "M10", DataType.BOOL, None, _split_1e(_1E_BIT_READ), True, True, True, None),
    Case("udp_3e_read", "3E", True, "read", "D100", DataType.USHORT, None, (_3E_READ,), True, 20, True, None),
    Case("udp_1e_read", "1E", True, "read", "D100", DataType.USHORT, None, (_1E_READ,), True, 20, True, None),
    # 结束码非 0(PLC 明确报错):不断线、分类 DEVICE
    Case("tcp_3e_device_error", "3E", False, "read", "D100", DataType.USHORT, None, _split_3e(_qna_read_response([], end_code=0xC059)), False, None, True, ErrorCategory.DEVICE),
    # 副头部非法(串话/迟到帧/网关错配):坏帧拆连,分类 PROTOCOL
    Case("tcp_3e_bad_subhead", "3E", False, "read", "D100", DataType.USHORT, None, (_3E_BAD_SUBHEAD,), False, None, False, ErrorCategory.PROTOCOL),
]


def _call(client: Any, case: Case) -> Any:
    if case.op == "write":
        return client.write(case.address, case.data_type, case.value)
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
    cls = MelsecMcUdpClient if case.datagram else MelsecMcTcpClient
    return cls("127.0.0.1", 2000, case.frame)


def _make_async_client(case: Case) -> Any:
    cls = AsyncMelsecMcUdpClient if case.datagram else AsyncMelsecMcTcpClient
    return cls("127.0.0.1", 2000, case.frame)


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
    sync_scripted = ScriptedTransport(list(case.responses), datagram=case.datagram)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    assert sync_client.connect() is True
    sync_result = _call(sync_client, case)
    sync_state = _snapshot(sync_client)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = _make_async_client(case)
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
    if case.op == "write":
        assert holder["result"] is case.expect_ok
    else:
        assert holder["result"][0] is case.expect_ok
        assert holder["result"][1] == case.expect_value
    assert holder["state"] == sync_state
    assert holder["state"]["connected"] is case.expect_connected
    assert holder["state"]["last_error_category"] is case.expect_category


def test_register_bit_write_is_read_modify_write(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """字软元件位写 = 读-改-写**两帧请求**(同一次公开写调用,一个事务计数)。

    协议上是两笔收发(读 1 字 → 按位改 → 写回),但都属于同一次 ``write``
    事务,故 ``stats["transactions"] == 1``;请求字节须与同步层逐字节相同。
    """
    read_resp = _qna_read_response([0x0000])
    write_resp = _qna_write_response()
    chunks = list(_split_3e(read_resp)) + list(_split_3e(write_resp))
    expected_read = codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"), 1, False, False,
    )
    expected_write = codec_qna.build_request(
        "3E", 2, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"), 1, False, True, data=[0x0008],
    )

    sync_client = MelsecMcTcpClient("127.0.0.1", 2000, "3E")
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    assert sync_client.write_bool("D100.3", True) is True

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncMelsecMcTcpClient("127.0.0.1", 2000, "3E")
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        holder["ok"] = await client.write_bool("D100.3", True)
        holder["sent"] = bytes(scripted.sent)
        holder["transactions"] = client.stats["transactions"]
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["ok"] is True
    assert holder["sent"] == bytes(sync_scripted.sent)
    assert holder["sent"] == expected_read + expected_write, "应是读-改-写两帧"
    assert holder["transactions"] == 1


def test_bad_subhead_is_protocol_error_and_disconnects(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """3E 副头部非法(串话/迟到帧/网关错配)→ 坏帧拆连,分类 PROTOCOL。"""
    chunks = list(_split_3e(_3E_BAD_SUBHEAD))

    sync_client = MelsecMcTcpClient("127.0.0.1", 2000, "3E")
    monkeypatch.setattr(
        sync_client, "_create_transport", lambda: ScriptedTransport(chunks)
    )
    sync_client.connect()
    assert sync_client.read_ushort("D100") == (False, None)
    sync_state = (sync_client.connected, sync_client.last_error_category)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncMelsecMcTcpClient("127.0.0.1", 2000, "3E")
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        assert await client.read_ushort("D100") == (False, None)
        holder["state"] = (client.connected, client.last_error_category)
        await client.close()

    loop.run_until_complete(scenario())
    assert sync_state == holder["state"] == (False, ErrorCategory.PROTOCOL)


def test_unsupported_frame_rejected_on_construction() -> None:
    """首批只支持 1E/3E:4E 构造期显式拒绝并指路(不做静默半成品)。"""
    with pytest.raises(ValueError) as excinfo:
        AsyncMelsecMcTcpClient("127.0.0.1", 2000, "4E")
    assert "4E" in str(excinfo.value)
    assert AsyncMelsecMcTcpClient("127.0.0.1", 2000, "1E").frame.value == "1E"


def test_real_server_roundtrip_3e(loop: Any) -> None:
    """真内核路径:进程内 asyncio 服务端回放 3E 读响应(双循环各跑一遍)。"""

    async def scenario() -> None:
        async def handle(reader: Any, writer: Any) -> None:
            try:
                while True:
                    head = await reader.readexactly(9)
                    length = int.from_bytes(head[7:9], "little")
                    await reader.readexactly(length)
                    writer.write(_3E_READ)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass

        server = RawTcpServer(handle)
        await server.start()
        client = AsyncMelsecMcTcpClient("127.0.0.1", server.port, "3E")
        try:
            results = [await client.read_ushort("D100") for _ in range(3)]
            assert results == [(True, 20)] * 3
            assert client.stats["transactions"] == 3
        finally:
            await client.close()
            await server.stop()

    loop.run_until_complete(scenario())


def test_tcp_and_udp_clients_share_frame_logic(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """TCP/UDP 只差传输对象:同一请求在同一帧型下字节相同(与同步层同构)。"""

    async def scenario() -> None:
        tcp = AsyncMelsecMcTcpClient("127.0.0.1", 2000, "3E")
        udp = AsyncMelsecMcUdpClient("127.0.0.1", 2000, "3E")
        tcp_scripted = ScriptedAsyncTransport(_split_3e(_3E_READ))
        udp_scripted = ScriptedAsyncTransport((_3E_READ,), datagram=True)
        monkeypatch.setattr(tcp, "_create_transport", lambda: tcp_scripted)
        monkeypatch.setattr(udp, "_create_transport", lambda: udp_scripted)
        await tcp.connect()
        await udp.connect()
        assert await tcp.read_ushort("D100") == (True, 20)
        assert await udp.read_ushort("D100") == (True, 20)
        assert bytes(tcp_scripted.sent) == bytes(udp_scripted.sent)
        await tcp.close()
        await udp.close()

    loop.run_until_complete(scenario())


def test_expected_request_frame_matches_codec() -> None:
    """异步请求帧与 codec 直接构造的期望帧逐字节一致(独立于同步客户端的第二重校验)。"""
    expected = codec_qna.build_request(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D100"),
        1,
        False,
        False,
    )

    async def scenario() -> None:
        client = AsyncMelsecMcTcpClient("127.0.0.1", 2000, "3E")
        scripted = ScriptedAsyncTransport(_split_3e(_3E_READ))
        client._create_transport = lambda: scripted  # type: ignore[method-assign]
        await client.connect()
        assert await client.read_ushort("D100") == (True, 20)
        assert bytes(scripted.sent) == expected
        await client.close()

    asyncio.run(scenario())
