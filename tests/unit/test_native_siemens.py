"""原生异步 S7 客户端测试:同步/异步**对拍**(帧逐字节一致)+ 会话专属行为。

异步侧只把传输调用换 await(会话逻辑镜像同步 _S7Session),帧语义必须与
同步层一致——对拍断言两侧 ``sent`` 全序列逐字节相同(握手/协商/事务);
会话型协议的假 TCP 用 size 感知连续池(与 test_siemens_s7_clients 同构)。
"""

from __future__ import annotations

import asyncio
import inspect
import struct
from typing import Any, Callable, Dict, List

import pytest

from omniplc import SiemensS7Client
from omniplc.native import AsyncSiemensS7Client
from test_siemens_s7_clients import (
    _FakeS7Tcp,
    _cotp_cc,
    _dt,
    _negotiate_ack,
    _read_ack,
    _szl_ack,
    _szl_cpu_record,
    _tpkt,
    _write_ack,
)
from scripted_async import loop_names, make_loop


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

    event_loop.run_until_complete(_cancel_pending())
    event_loop.close()


def _drive_sync(monkeypatch: pytest.MonkeyPatch, responses: List[bytes]) -> Any:
    """同步侧:挂假 TCP → 喂握手应答 → connect(返回 (client, fake))。"""
    client = SiemensS7Client("127.0.0.1", 102, 0, 1)
    fake: Any = _FakeS7Tcp("127.0.0.1", 102)
    monkeypatch.setattr(
        "omniplc.plc.siemens.client.TcpTransport", lambda ip, port: fake
    )
    for chunk in (_tpkt(_cotp_cc()), _tpkt(_dt(_negotiate_ack(1)))):
        fake.queue(chunk)
    assert client.connect() is True, client.last_error
    for chunk in responses:
        fake.queue(chunk)
    return client, fake


def _async_parity(
    loop: Any,
    monkeypatch: pytest.MonkeyPatch,
    responses: List[bytes],
    async_action: Callable[[Any], Any],
) -> Dict[str, Any]:
    """异步侧:挂假 TCP → 喂握手应答 → connect → await 动作 → 收集现场。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncSiemensS7Client("127.0.0.1", 102, 0, 1)
        fake = _FakeAsyncS7Tcp()
        monkeypatch.setattr(
            "omniplc.native.siemens.AsyncTcpTransport", lambda ip, port: fake
        )
        for chunk in (_tpkt(_cotp_cc()), _tpkt(_dt(_negotiate_ack(1)))):
            fake.queue(chunk)
        assert await client.connect() is True, client.last_error
        for chunk in responses:
            fake.queue(chunk)
        result = async_action(client)
        holder["result"] = await result if inspect.isawaitable(result) else result
        holder["sent"] = bytes(fake.sent)
        holder["state"] = {
            "connected": client.connected,
            "transactions": client.stats["transactions"],
        }
        await client.close()
        holder["dr_sent"] = b"\x06\x80\x00\x0a\x00\x01\x00\x00" in bytes(fake.sent)

    loop.run_until_complete(scenario())
    return holder


class _FakeAsyncS7Tcp:
    """S7 会话假 TCP(size 感知连续池;同步 _FakeS7Tcp 的异步版)。"""

    def __init__(self) -> None:
        self.sent = bytearray()
        self._pool = bytearray()
        self.closed = False
        self.connect_timeout: Any = None
        self.receive_timeout: Any = None

    def queue(self, data: bytes) -> None:
        self._pool.extend(data)

    async def connect(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def send(self, data: bytes) -> None:
        self.sent.extend(data)

    async def recv(self, size: int) -> bytes:
        if not self._pool:
            raise ConnectionError("应答字节池已耗尽")
        chunk = bytes(self._pool[:size])
        del self._pool[:size]
        return chunk


def _parity_case(
    monkeypatch: pytest.MonkeyPatch,
    loop: Any,
    responses: List[bytes],
    sync_action: Callable[[Any], Any],
    async_action: Callable[[Any], Any],
) -> None:
    """同步/异步两侧跑同一脚本:断言 sent 全序列逐字节一致 + 结果/状态一致。"""
    sync_client, sync_fake = _drive_sync(monkeypatch, responses)
    sync_result = sync_action(sync_client)
    sync_sent = bytes(sync_fake.sent)
    sync_state = {
        "connected": sync_client.connected,
        "transactions": sync_client.stats["transactions"],
    }
    sync_client.disconnect()

    holder = _async_parity(loop, monkeypatch, responses, async_action)
    assert holder["sent"] == sync_sent
    assert holder["result"] == sync_result
    assert holder["state"] == sync_state
    assert holder["dr_sent"] is True


# ----------------------------------------------------------------------
# 对拍:握手 / 单点读 / 位写 RMW / multi / read_range / SZL / WString
# ----------------------------------------------------------------------


def test_handshake_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """连接三步:CR/协商请求帧逐字节一致,PDU 尺寸一致。"""
    sync_client, sync_fake = _drive_sync(monkeypatch, [])
    sync_pdu = sync_client._transport.pdu_size  # type: ignore[attr-defined]
    sync_sent = bytes(sync_fake.sent)
    sync_client.disconnect()

    holder = _async_parity(loop, monkeypatch, [], lambda _client: None)
    assert holder["sent"] == sync_sent
    # CR:远端 TSAP 0x0101(rack0 slot1);协商:功能 0xF0 + PDU 480
    assert b"\xc2\x02\x01\x01" in holder["sent"]
    assert b"\xf0\x00\x00\x01\x00\x01\x01\xe0" in holder["sent"]
    assert sync_pdu == 480


def test_read_float_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """DB1.DBD6 读 FLOAT:请求帧与解码值两侧一致(100.5)。"""
    responses = [_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00"])))]
    _parity_case(
        monkeypatch,
        loop,
        responses,
        lambda client: client.read_float("DB1.DBD6"),
        lambda client: client.read_float("DB1.DBD6"),
    )


def test_write_bool_rmw_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """位写 RMW(读整字节→置位写回):两事务帧序两侧一致。"""
    responses = [
        _tpkt(_dt(_read_ack(2, [b"\x00"]))),
        _tpkt(_dt(_write_ack(3))),
    ]
    _parity_case(
        monkeypatch,
        loop,
        responses,
        lambda client: client.write_bool("DB1.DBX0.3", True),
        lambda client: client.write_bool("DB1.DBX0.3", True),
    )


def test_read_batch_multi_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """read_batch 2 项混读(BOOL 提位 + SHORT):multi 请求帧与值两侧一致。"""
    # 项1 DB1.DBX0.1 BYTE 1B(bit1)→ 值 0x02;项2 DB1.DBB2 SHORT 2B → -2
    data = b"\xff\x04\x00\x08\x02\x00" + b"\xff\x04\x00\x10\xff\xfe"
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, 2, 2, len(data), 0, 0)
    responses = [_tpkt(_dt(header + b"\x04\x02" + data))]

    def sync_action(client: Any):
        return client.read_batch([("DB1.DBX0.1", "BOOL"), ("DB1.DBB2", "SHORT")])

    async def async_action(client: Any):
        return await client.read_batch([("DB1.DBX0.1", "BOOL"), ("DB1.DBB2", "SHORT")])

    _parity_case(monkeypatch, loop, responses, sync_action, async_action)


def test_read_range_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """read_range SHORT×2(单 Item 4 字节):帧与切片解码两侧一致。"""
    responses = [_tpkt(_dt(_read_ack(2, [b"\x00\x01\x00\x02"])))]
    _parity_case(
        monkeypatch,
        loop,
        responses,
        lambda client: client.read_range("DB1.DBB0", 2, "SHORT"),
        lambda client: client.read_range("DB1.DBB0", 2, "SHORT"),
    )


def test_get_cpu_state_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """SZL 0x0424 状态读:USERDATA 帧 + Run 判定两侧一致;ping 同命令。"""
    responses = [
        _tpkt(_dt(_szl_ack(2, _szl_cpu_record(0x08)))),
        _tpkt(_dt(_szl_ack(3, _szl_cpu_record(0x08)))),  # ping 探测(seq 3)
    ]

    def sync_action(client: Any):
        ok, name = client.get_cpu_state()
        return (ok, name, client.ping())

    async def async_action(client: Any):
        ok, name = await client.get_cpu_state()
        return (ok, name, await client.ping())

    _parity_case(monkeypatch, loop, responses, sync_action, async_action)


def test_wstring_roundtrip_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """WString 读写(UTF-16BE 4 字节头):与同步侧同帧同值。"""
    # 读:声明长 4 + 实际长 1 + "温"(请求 20 字节,应答 20 字节含补零)
    blob = (
        (4).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + "温".encode("utf-16-be")
        + b"\x00" * 14
    )
    # 写前预读声明长(2 字节)
    responses = [
        _tpkt(_dt(_read_ack(2, [blob], [160]))),
        _tpkt(_dt(_read_ack(3, [(4).to_bytes(2, "big")], [16]))),
        _tpkt(_dt(_write_ack(4))),
    ]

    def sync_action(client: Any):
        ok, text = client.read_wstring("DB1.DBW40", length=8)
        wrote = client.write_wstring("DB1.DBW40", "温")
        return (ok, text, wrote)

    async def async_action(client: Any):
        ok, text = await client.read_wstring("DB1.DBW40", length=8)
        wrote = await client.write_wstring("DB1.DBW40", "温")
        return (ok, text, wrote)

    _parity_case(monkeypatch, loop, responses, sync_action, async_action)


# ----------------------------------------------------------------------
# 原生层专属行为
# ----------------------------------------------------------------------


def test_native_disconnect_sends_dr(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """disconnect 钩子:先发 COTP DR(dst_ref=CC 回显 0x000A)再关传输。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncSiemensS7Client("127.0.0.1", 102, 0, 1)
        fake = _FakeAsyncS7Tcp()
        monkeypatch.setattr(
            "omniplc.native.siemens.AsyncTcpTransport", lambda ip, port: fake
        )
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        assert await client.connect() is True
        fake.sent.clear()
        assert await client.disconnect() is True
        holder["sent"] = bytes(fake.sent)
        holder["closed"] = fake.closed

    loop.run_until_complete(scenario())
    # DR 帧(8 字节):LI 6 + 0x80 + dst_ref 0x000A + src_ref 0x0001 + class 0,
    # TPKT 包裹后总长 12
    assert b"\x06\x80\x00\x0a\x00\x01\x00\x00" in holder["sent"]
    assert holder["closed"] is True


def test_native_connect_timeout_flows(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """connect_timeout:建会话时下发到底层 TCP 传输(与同步修复同口径)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncSiemensS7Client("127.0.0.1", 102, 0, 1)
        client.connect_timeout = 2.0
        fake = _FakeAsyncS7Tcp()
        monkeypatch.setattr(
            "omniplc.native.siemens.AsyncTcpTransport", lambda ip, port: fake
        )
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        assert await client.connect() is True
        holder["connect_timeout"] = fake.connect_timeout

    loop.run_until_complete(scenario())
    assert holder["connect_timeout"] == 2.0


def test_native_receive_timeout_flows(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """receive_timeout:连接时下发初值,运行期修改热下发到当前传输。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncSiemensS7Client("127.0.0.1", 102, 0, 1)
        client.receive_timeout = 2.5
        fake = _FakeAsyncS7Tcp()
        monkeypatch.setattr(
            "omniplc.native.siemens.AsyncTcpTransport", lambda ip, port: fake
        )
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        assert await client.connect() is True
        holder["initial"] = fake.receive_timeout
        client.receive_timeout = 3.0
        holder["hot"] = fake.receive_timeout

    loop.run_until_complete(scenario())
    assert holder["initial"] == 2.5
    assert holder["hot"] == 3.0


def test_native_lazy_reconnect(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """半开(池耗尽=OSError)→ 拆连,下次事务惰性重连重走握手。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncSiemensS7Client("127.0.0.1", 102, 0, 1)
        client.reconnect_backoff = False
        fake = _FakeAsyncS7Tcp()
        monkeypatch.setattr(
            "omniplc.native.siemens.AsyncTcpTransport", lambda ip, port: fake
        )
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        assert await client.connect() is True
        fake._pool.clear()  # 应答池耗尽 → OSError → 拆连
        ok, _value = await client.read_float("DB1.DBD6")
        assert (ok, client.connected) == (False, False)
        # 恢复应答(重走握手 seq 从 1 起 + 事务 seq 2)
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        fake.queue(_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00"]))))
        ok, value = await client.read_float("DB1.DBD6")
        holder["result"] = (ok, value, client.connected, client.stats["connect_count"])

    loop.run_until_complete(scenario())
    assert holder["result"] == (True, 100.5, True, 2)
