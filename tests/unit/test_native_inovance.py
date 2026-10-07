"""原生异步汇川客户端测试:同步/异步**对拍** + 地址翻译 + 记号边界。

对拍覆盖两条走线:Modbus TCP(汇川记号翻译后逐字节同帧)与 MC 兼容 3E
(R 统一编址/X/Y 八进制换算后逐字节同帧);异步侧只重写薄层,帧语义必须
与同步层一致。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from omniplc import InovanceMcTcpClient, InovanceTcpClient
from omniplc.native import AsyncInovanceMcTcpClient, AsyncInovanceTcpClient
from omniplc.plc.melsec import codec_qna
from omniplc.plc.melsec.address import parse_mc_address
from omniplc.core.constants import MC_DEFAULT_MONITOR_TIMER
from omniplc.core.types import DataType
from scripted import ScriptedTransport
from scripted_async import ScriptedAsyncTransport, loop_names, make_loop


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


# ----------------------------------------------------------------------
# 响应构造脚手架(与 test_modbus_clients / test_native_melsec 同口径)
# ----------------------------------------------------------------------


def _mbap(transaction_id: int, pdu: bytes) -> bytes:
    """构造 MBAP 帧(事务号 + 协议 0 + 长度含 Unit ID + 站号 1)。"""
    return (
        transaction_id.to_bytes(2, "big")
        + b"\x00\x00"
        + (len(pdu) + 1).to_bytes(2, "big")
        + b"\x01"
        + pdu
    )


def _modbus_read_regs(values) -> bytes:
    """构造 FC03 读寄存器正常响应。"""
    data = b"".join(int(v).to_bytes(2, "big") for v in values)
    return _mbap(1, bytes([3, len(data)]) + data)


def _modbus_write_resp(fc: int, address: int, value: int) -> bytes:
    """构造 FC05/06/16 写正常响应(请求前 5 字节回显)。"""
    body = address.to_bytes(2, "big")
    if fc in (6, 5):
        body += value.to_bytes(2, "big")
    else:
        body += value.to_bytes(2, "big")
    return _mbap(1, bytes([fc]) + body)


def _qna_read_response(values) -> bytes:
    """构造 3E 读响应。"""
    data = b"".join(int(v).to_bytes(2, "little") for v in values)
    head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return (
        head + (2 + len(data)).to_bytes(2, "little") + (0).to_bytes(2, "little") + data
    )


def _split_3e(frame: bytes):
    return (frame[:9], frame[9:])


def _split_mbap(frame: bytes):
    return (frame[:8], frame[8:])


# ----------------------------------------------------------------------
# Modbus TCP 走线:汇川记号翻译对拍
# ----------------------------------------------------------------------


def test_modbus_tcp_read_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """D100 读 USHORT:汇川记号翻译后请求帧与同步层逐字节相同。"""
    resp = _modbus_read_regs([20])
    chunks = list(_split_mbap(resp))

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read("D100", DataType.USHORT)
    sync_state = {
        "connected": sync_client.connected,
        "transactions": sync_client.stats["transactions"],
    }

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read("D100", DataType.USHORT)
        holder["sent"] = bytes(scripted.sent)
        holder["state"] = {
            "connected": client.connected,
            "transactions": client.stats["transactions"],
        }
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, 20)
    assert holder["sent"] == bytes(sync_scripted.sent)
    # D100 → hr100(基址 0,编号即偏移):帧内寄存器偏移 = 100
    assert holder["sent"][8:10] == (100).to_bytes(2, "big")
    assert holder["state"] == sync_state


def test_modbus_tcp_counter_translate(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """C205(32 位计数器)读 INT:翻译到 hr63242 双寄存器展开(与同步同帧)。"""
    resp = _modbus_read_regs([0, 0])
    chunks = list(_split_mbap(resp))

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read("C205", DataType.INT)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read("C205", DataType.INT)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, 0)
    assert holder["sent"] == bytes(sync_scripted.sent)
    assert holder["sent"][8:10] == (63242).to_bytes(2, "big")


def test_modbus_tcp_bit_read_octal(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """X17 读 BOOL:八进制命名翻译(17₈ = 15)后走线圈读(与同步同帧)。"""
    resp = _mbap(1, bytes([1, 1]) + b"\x01")
    chunks = list(_split_mbap(resp))

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read("X17", DataType.BOOL)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read("X17", DataType.BOOL)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, True)
    assert holder["sent"] == bytes(sync_scripted.sent)
    # X17(八进制 17 = 15)→ 线圈基址 0xF800 + 15 = 0xF80F(H3U 9.4.3)
    assert holder["sent"][8:10] == (0xF80F).to_bytes(2, "big")


def test_modbus_tcp_counter_type_gate(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """C205 以 16 位类型访问:事务路径 ValueError(32 位门控;两层口径一致)。

    门控在 ``_translate``(事务内)——连接成功后才走到;未连接时
    ``_execute`` 连不上直接 ``(False, None)``,门控不执行(两层同构,
    同步侧同法实测)。
    """
    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: ScriptedTransport([]))
    sync_client.connect()
    with pytest.raises(ValueError):
        sync_client.read("C205", DataType.USHORT)

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        monkeypatch.setattr(
            client, "_create_transport", lambda: ScriptedAsyncTransport([])
        )
        assert await client.connect() is True
        with pytest.raises(ValueError):
            await client.read("C205", DataType.USHORT)
        await client.close()

    loop.run_until_complete(scenario())


def test_modbus_tcp_batch_translates_inovance_tokens(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """批量方法双记号(与同步侧同收口):D 记号翻译合并为同帧,逐字节对拍。"""
    response = _mbap(1, bytes([3, 4, 0x00, 0x14, 0x00, 0x1E]))
    chunks = [response[:7], response[7:]]

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read_many(["D7021", "D7022"], DataType.USHORT)
    sync_sent = bytes(sync_scripted.sent)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read_many(["D7021", "D7022"], DataType.USHORT)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == [(True, 20), (True, 30)]
    assert holder["sent"] == sync_sent
    # D7021/D7022 连续 → 合并一笔 FC03@7021 数量 2
    assert holder["sent"][7:12] == b"\x03\x1b\x6d\x00\x02"


def test_modbus_tcp_batch_c_token_is_modbus_coil(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """批量 ``C`` 记号歧义按 Modbus 优先锁定(与同步侧同裁决)。"""
    response = _mbap(1, bytes([1, 1, 1]))
    chunks = [response[:7], response[7:]]

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read_many(["C10"], DataType.BOOL)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read_many(["C10"], DataType.BOOL)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == [(True, True)]
    # C10 → Modbus 线圈 c10(FC01@10),非汇川计数器
    assert holder["sent"][7:12] == b"\x01\x00\x0a\x00\x01"


def test_modbus_tcp_read_range_d_token(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """区间读双记号:D7021 翻译后一笔 FC03@7021,同步/异步逐字节对拍。"""
    response = _mbap(1, bytes([3, 4, 0x00, 0x14, 0x00, 0x1E]))
    chunks = [response[:7], response[7:]]

    sync_client = InovanceTcpClient("127.0.0.1", 502, 1)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read_range("D7021", 2, DataType.USHORT)
    sync_sent = bytes(sync_scripted.sent)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read_range("D7021", 2, DataType.USHORT)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, [20, 30])
    assert holder["sent"] == sync_sent
    # D7021 起始 ×2 元素 → FC03@7021 数量 2
    assert holder["sent"][7:12] == b"\x03\x1b\x6d\x00\x02"


# ----------------------------------------------------------------------
# MC 兼容 3E 走线:码表/记号换算对拍
# ----------------------------------------------------------------------


def test_mc_read_parity_r_notation(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """R100 读 USHORT:R→D8100 统一编址后请求帧与同步层逐字节相同。"""
    resp = _qna_read_response([20])
    chunks = list(_split_3e(resp))

    sync_client = InovanceMcTcpClient("127.0.0.1", 2000)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read("R100", DataType.USHORT)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceMcTcpClient("127.0.0.1", 2000)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read("R100", DataType.USHORT)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, 20)
    assert holder["sent"] == bytes(sync_scripted.sent)
    expected = codec_qna.build_request(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("D8100"),
        1,
        False,
        False,
    )
    assert holder["sent"] == expected, "R100 应按 D8100 组帧"


def test_mc_read_parity_xy_octal(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """X17 读 BOOL:汇川八进制命名换算为帧内十六进制(17₈ → 0xF)。"""
    resp = _qna_read_response([0x10])
    chunks = list(_split_3e(resp))

    sync_client = InovanceMcTcpClient("127.0.0.1", 2000)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read("X17", DataType.BOOL)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceMcTcpClient("127.0.0.1", 2000)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read("X17", DataType.BOOL)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result
    assert holder["sent"] == bytes(sync_scripted.sent)
    expected = codec_qna.build_request(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("XF"),
        1,
        True,
        False,
    )
    assert holder["sent"] == expected, "X17(八进制)应按 XF(十六进制)组帧"


def test_mc_read_range_xy_octal(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """read_range X17:入口不再预换算(review-1007 P1-2),与单点读同帧。

    修复前:native read_range 入口 ``_translate_address`` 预换算(X17→XF)
    后 ``_build_frame`` 内二次换算 ``int('F', 8)`` 抛 ValueError——同步层
    review-1002 P1-1 同款缺陷在 native 复活,且旧测试零 read_range 覆盖。
    """
    # 位单位读 1 点:响应数据 = 1 字节半字节打包(第 1 点在高半字节,SH-080008)
    resp = (
        b"\xd0\x00"
        + b"\x00\xff\xff\x03\x00"
        + (3).to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + b"\x10"
    )
    chunks = list(_split_3e(resp))

    sync_client = InovanceMcTcpClient("127.0.0.1", 2000)
    sync_scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(sync_client, "_create_transport", lambda: sync_scripted)
    sync_client.connect()
    sync_result = sync_client.read_range("X17", 1, DataType.BOOL)

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncInovanceMcTcpClient("127.0.0.1", 2000)
        scripted = ScriptedAsyncTransport(chunks)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        holder["result"] = await client.read_range("X17", 1, DataType.BOOL)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())

    assert holder["result"] == sync_result == (True, [True])
    assert holder["sent"] == bytes(sync_scripted.sent)
    expected = codec_qna.build_request(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("XF"),
        1,
        True,
        False,
    )
    assert holder["sent"] == expected, "read_range X17 应按 XF 恰好一次换算组帧"


def test_mc_ping_disabled(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """探活显式关闭(H5U 手册 16.4 无 0101):ping 恒 False 不发包,与同步同口径。"""

    async def scenario() -> None:
        client = AsyncInovanceMcTcpClient("127.0.0.1", 2000)
        assert client.ping_supported is False
        scripted = ScriptedAsyncTransport([])
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.ping() is False
        assert client._transport is None  # 兜底路径不发包、不建连

    loop.run_until_complete(scenario())


def test_mc_frame_fixed_3e() -> None:
    """frame 属性恒 3E:构造签名不收 frame 参数,覆写点不引入帧型漂移。"""
    client = AsyncInovanceMcTcpClient("127.0.0.1", 2000)
    assert client.frame.value == "3E"
    # 走线即 TCP(与同步侧一致);原生 MC UDP 版不提供汇川子类
    from omniplc.native import AsyncMelsecMcUdpClient

    assert not issubclass(AsyncInovanceMcTcpClient, AsyncMelsecMcUdpClient)


# ----------------------------------------------------------------------
# 守卫面:构造签名与同步孪生逐一对齐
# ----------------------------------------------------------------------


def test_constructors_match_sync_twin() -> None:
    """两对孪生客户端的构造签名(参数名 + 默认值)与同步侧逐一对齐。"""
    import inspect

    import omniplc as pkg
    import omniplc.native as native

    for sync_name, async_name in (
        ("InovanceTcpClient", "AsyncInovanceTcpClient"),
        ("InovanceMcTcpClient", "AsyncInovanceMcTcpClient"),
    ):
        sync_sig = inspect.signature(getattr(pkg, sync_name).__init__)
        async_sig = inspect.signature(getattr(native, async_name).__init__)
        assert list(sync_sig.parameters) == list(async_sig.parameters), sync_name
        assert [p.default for p in sync_sig.parameters.values()] == [
            p.default for p in async_sig.parameters.values()
        ], sync_name


def test_async_methods_are_coroutines() -> None:
    """汇川原生客户端公开异步方法必须都是协程(防手误漏 async)。

    ``bind_tags`` 豁免:纯配置透传,与 aio 层同款有意同步
    (test_native_surface 同口径)。
    """
    import inspect

    import omniplc.native as native

    sync_passthrough = {"bind_tags"}
    for cls in (native.AsyncInovanceTcpClient, native.AsyncInovanceMcTcpClient):
        for attr in dir(cls):
            if attr.startswith("_") or attr in sync_passthrough:
                continue
            member = inspect.getattr_static(cls, attr)
            if isinstance(member, (staticmethod, property)):
                continue
            func = getattr(member, "func", member)
            if inspect.isfunction(func):
                assert inspect.iscoroutinefunction(func), "{}.{} 不是协程".format(
                    cls.__name__, attr
                )
