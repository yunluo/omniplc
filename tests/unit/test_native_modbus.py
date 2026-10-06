"""原生异步 Modbus TCP 客户端测试:同步/异步**对拍** + 取消 + 并发 + 关闸。

对拍是防漂移的关键手段:同一张用例表分别喂同步客户端(``scripted`` 假传输)
与异步客户端(``scripted_async`` 假传输),断言**请求帧逐字节相同**、解析结果
相同、``last_error``/``last_error_category``/``last_error_code``/``stats`` 一致
——异步层重写的只是薄分发层,帧语义与错误口径必须与同步层完全一致。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any, Dict, NamedTuple, Optional, Sequence, Tuple

import pytest

from omniplc import ModbusTcpClient
from omniplc.core.errors import ErrorCategory
from omniplc.plc.modbus import codec
from omniplc.native import AsyncModbusTcpClient
from omniplc.core.tag import Tag, TagTable
from omniplc.transport.base import BaseTransport
from omniplc.core.types import DataType, PrimitiveValue
from scripted import ScriptedTransport
from scripted_async import ScriptedAsyncTransport, TcpResponder, loop_names, make_loop

# 黄金响应 PDU(与同步侧同一批样本口径)
_RESP_ONE_REGISTER = bytes([3, 2, 0x00, 0x14])  # FC03 读 1 寄存器 = 20
_RESP_ONE_COIL = bytes([1, 1, 0x01])  # FC01 读 1 线圈 = ON
_RESP_FLOAT = bytes([3, 4, 0xBF, 0xC0, 0x00, 0x00])  # FC03 读 2 寄存器 = -1.5f
_RESP_STRING = bytes([3, 4, 0x4F, 0x4D, 0x4E, 0x49])  # FC03 读 2 寄存器 = "OMNI"
_RESP_WRITE_OK = bytes([6, 0x00, 0x00, 0x00, 0x14])  # FC06 回显
_RESP_COIL_OK = bytes([5, 0x00, 0x00, 0xFF, 0x00])  # FC05 回显
_RESP_DEVICE_ERROR = bytes([0x83, 0x02])  # FC03 | 0x80,异常码 02


def _resp_registers(data: bytes) -> bytes:
    """按 FC03 规范把大端字节串包成读响应 PDU(测试脚手架)。"""
    return bytes([3, len(data)]) + data


# 点位表用例:scale/offset 取整数倍,保证逆缩放无浮点误差(读写两侧都可精确还原)
_TAG = Tag(tag_id="flow", address="hr0", data_type="ushort", scale=2.0, offset=10.0)
# 恒等缩放点位(scale=1/offset=0 默认):64 位精度直通,不过 float64 往返
_TAG_IDENTITY_LONG = Tag(tag_id="累计", address="hr0", data_type="long")


class Case(NamedTuple):
    """一条对拍用例:同一批响应分片喂同步与异步客户端,比对全部可观测结果。

    ``op`` 取 ``"read"`` / ``"write"`` / ``"read_string"`` / ``"read_tag"`` /
    ``"write_tag"``(字符串在两侧都走 ``read_string`` 入口,不经
    ``read(DataType.STRING)``——同步层同样如此;点位表用例经 ``tag`` 字段绑定)。
    """

    name: str
    op: str
    address: str
    data_type: DataType
    value: Optional[PrimitiveValue]
    responses: Sequence[Tuple[int, bytes]]
    """``(事务号, 响应 PDU)`` 序列:按事务号合成 MBAP 应答帧(依次喂给 recv)。"""
    expect_ok: bool
    expect_value: Optional[PrimitiveValue]
    expect_connected: bool
    expect_category: Optional[ErrorCategory]
    expect_code: Optional[int]
    length: int = 4
    tag: Optional[Tag] = None


_READ_CASES = [
    Case(
        "ushort",
        "read",
        "hr0",
        DataType.USHORT,
        None,
        ((1, _RESP_ONE_REGISTER),),
        True,
        20,
        True,
        None,
        None,
    ),
    Case(
        "bool_coil",
        "read",
        "c0",
        DataType.BOOL,
        None,
        ((1, _RESP_ONE_COIL),),
        True,
        True,
        True,
        None,
        None,
    ),
    Case(
        "short",
        "read",
        "hr0",
        DataType.SHORT,
        None,
        ((1, _resp_registers(struct.pack(">h", -2))),),
        True,
        -2,
        True,
        None,
        None,
    ),
    Case(
        "int",
        "read",
        "hr0",
        DataType.INT,
        None,
        ((1, _resp_registers(struct.pack(">i", -2))),),
        True,
        -2,
        True,
        None,
        None,
    ),
    Case(
        "uint",
        "read",
        "hr0",
        DataType.UINT,
        None,
        ((1, _resp_registers(struct.pack(">I", 4294967290))),),
        True,
        4294967290,
        True,
        None,
        None,
    ),
    Case(
        "float",
        "read",
        "hr0",
        DataType.FLOAT,
        None,
        ((1, _RESP_FLOAT),),
        True,
        -1.5,
        True,
        None,
        None,
    ),
    Case(
        "long",
        "read",
        "hr0",
        DataType.LONG,
        None,
        ((1, _resp_registers(struct.pack(">q", -2))),),
        True,
        -2,
        True,
        None,
        None,
    ),
    Case(
        "ulong",
        "read",
        "hr0",
        DataType.ULONG,
        None,
        ((1, _resp_registers(struct.pack(">Q", 2**64 - 5))),),
        True,
        2**64 - 5,
        True,
        None,
        None,
    ),
    Case(
        "double",
        "read",
        "hr0",
        DataType.DOUBLE,
        None,
        ((1, _resp_registers(struct.pack(">d", 1.5))),),
        True,
        1.5,
        True,
        None,
        None,
    ),
    Case(
        "string",
        "read_string",
        "hr0",
        DataType.STRING,
        None,
        ((1, _RESP_STRING),),
        True,
        "OMNI",
        True,
        None,
        None,
    ),
    # 点位表:scale/offset 正向与逆缩放(同一次读写的两条路径)
    Case(
        "read_tag",
        "read_tag",
        "hr0",
        DataType.USHORT,
        None,
        ((1, _RESP_ONE_REGISTER),),
        True,
        50.0,
        True,
        None,
        None,
        tag=_TAG,
    ),
    # 恒等缩放:64 位整数原值直通(回归:native 曾强制 float64 往返丢低位)
    Case(
        "read_tag_identity_long",
        "read_tag",
        "hr0",
        DataType.LONG,
        None,
        ((1, _resp_registers(struct.pack(">q", 2**62 + 1))),),
        True,
        2**62 + 1,
        True,
        None,
        None,
        tag=_TAG_IDENTITY_LONG,
    ),
    # PLC 明确报错:不断线、分类 DEVICE、错误码原样落
    Case(
        "device_error",
        "read",
        "hr0",
        DataType.USHORT,
        None,
        ((1, _RESP_DEVICE_ERROR),),
        False,
        None,
        True,
        ErrorCategory.DEVICE,
        2,
    ),
    # 事务号不匹配(迟到帧/网关错配):坏帧 → 拆连,分类 PROTOCOL
    Case(
        "tid_mismatch",
        "read",
        "hr0",
        DataType.USHORT,
        None,
        ((99, _RESP_ONE_REGISTER),),
        False,
        None,
        False,
        ErrorCategory.PROTOCOL,
        None,
    ),
]

_WRITE_CASES = [
    Case(
        "write_ushort",
        "write",
        "hr0",
        DataType.USHORT,
        20,
        ((1, _RESP_WRITE_OK),),
        True,
        None,
        True,
        None,
        None,
    ),
    Case(
        "write_short",
        "write",
        "hr0",
        DataType.SHORT,
        -2,
        ((1, bytes([6, 0x00, 0x00, 0xFF, 0xFE])),),
        True,
        None,
        True,
        None,
        None,
    ),
    Case(
        "write_float",
        "write",
        "hr0",
        DataType.FLOAT,
        -1.5,
        ((1, bytes([0x10, 0x00, 0x00, 0x00, 0x02])),),
        True,
        None,
        True,
        None,
        None,
    ),
    Case(
        "write_double",
        "write",
        "hr0",
        DataType.DOUBLE,
        1.5,
        ((1, bytes([0x10, 0x00, 0x00, 0x00, 0x04])),),
        True,
        None,
        True,
        None,
        None,
    ),
    Case(
        "write_string",
        "write_string",
        "hr0",
        DataType.STRING,
        "OMNI",
        ((1, bytes([0x10, 0x00, 0x00, 0x00, 0x02])),),
        True,
        None,
        True,
        None,
        None,
    ),
    Case(
        "write_tag",
        "write_tag",
        "hr0",
        DataType.USHORT,
        50.0,
        ((1, _RESP_WRITE_OK),),
        True,
        None,
        True,
        None,
        None,
        tag=_TAG,
    ),
    # 恒等缩放:64 位 int 原样下发,不过 float64 往返(回归:native 曾丢低位)
    Case(
        "write_tag_identity_long",
        "write_tag",
        "hr0",
        DataType.LONG,
        2**62 + 1,
        ((1, bytes([0x10, 0x00, 0x00, 0x00, 0x04])),),
        True,
        None,
        True,
        None,
        None,
        tag=_TAG_IDENTITY_LONG,
    ),
    Case(
        "write_bool_coil",
        "write",
        "c0",
        DataType.BOOL,
        True,
        ((1, _RESP_COIL_OK),),
        True,
        None,
        True,
        None,
        None,
    ),
    # 寄存器位写 = 读-改-写两段事务(读响应 + 写回显)
    Case(
        "write_bool_rmw",
        "write",
        "hr0.3",
        DataType.BOOL,
        True,
        ((1, bytes([3, 2, 0x00, 0x00])), (2, bytes([6, 0x00, 0x00, 0x00, 0x08]))),
        True,
        None,
        True,
        None,
        None,
    ),
]

_CASES = _READ_CASES + _WRITE_CASES


def _chunks(responses: Sequence[Tuple[int, bytes]]) -> list:
    """按同步层的 recv 尺寸切分响应帧(头 7 字节 + 其余)。"""
    chunks = []
    for tid, pdu in responses:
        frame = codec.build_mbap(tid, 1, pdu)
        chunks.append(frame[:7])
        chunks.append(frame[7:])
    return chunks


def _call(client: Any, case: Case, is_async: bool) -> Any:
    """按用例调用客户端(读/写/字符串/点位),返回同步结果或协程。"""
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
    """客户端可观测状态快照(对拍比对用)。"""
    stats = client.stats
    return {
        "connected": client.connected,
        "last_error_category": client.last_error_category,
        "last_error_code": client.last_error_code,
        "transactions": stats["transactions"],
        "error_count": stats["error_count"],
        "device_error_count": stats["device_error_count"],
        "has_error_text": client.last_error is not None,
    }


def _run_sync(
    monkeypatch: pytest.MonkeyPatch, case: Any, invoke: Any = None
) -> Tuple[bytes, Any, Dict[str, Any], str]:
    call = invoke or _call
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    tag = getattr(case, "tag", None)
    if tag is not None:
        client.bind_tags(TagTable([tag]))
    scripted = ScriptedTransport(_chunks(case.responses))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    result = call(client, case, is_async=False)
    return bytes(scripted.sent), result, _snapshot(client), client.last_error or ""


def _run_async(
    monkeypatch: pytest.MonkeyPatch, case: Any, loop: Any, invoke: Any = None
) -> Tuple[bytes, Any, Dict[str, Any], str]:
    call = invoke or _call
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        tag = getattr(case, "tag", None)
        if tag is not None:
            client.bind_tags(TagTable([tag]))
        scripted = ScriptedAsyncTransport(_chunks(case.responses))
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        result = await call(client, case, is_async=True)
        holder["sent"] = bytes(scripted.sent)
        holder["result"] = result
        holder["snapshot"] = _snapshot(client)
        holder["error"] = client.last_error or ""

    loop.run_until_complete(scenario())
    return holder["sent"], holder["result"], holder["snapshot"], holder["error"]


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
    sync_sent, sync_result, sync_snapshot, _sync_error = _run_sync(monkeypatch, case)
    async_sent, async_result, async_snapshot, _async_error = _run_async(
        monkeypatch, case, loop
    )
    assert async_sent == sync_sent, "请求帧必须逐字节相同"
    assert async_result == sync_result
    assert async_snapshot == sync_snapshot
    # 结果形状也按用例声明核对一遍(对拍相等只证明"两侧一样",不证明"符合期望")
    if case.op in ("write", "write_string", "write_tag"):
        assert async_result is case.expect_ok
    else:
        assert async_result[0] is case.expect_ok
        assert async_result[1] == case.expect_value


# ----------------------------------------------------------------------
# 超时口径对拍(TCP 超时 = socket.timeout → 拆连)
# ----------------------------------------------------------------------


class _TimeoutTransport(BaseTransport):
    """recv 抛 ``socket.timeout`` 的假传输(同步侧 TCP 超时口径)。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent = bytearray()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        import socket as _socket

        raise _socket.timeout("TCP 接收超时(0.1s)")


def test_timeout_semantics_parity(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """TCP 读超时:同步抛 ``socket.timeout``、异步同样口径 → 都是 TIMEOUT 分类且拆连。"""
    sync_client = ModbusTcpClient("127.0.0.1", 502, 1)
    monkeypatch.setattr(sync_client, "_create_transport", _TimeoutTransport)
    sync_client.connect()
    assert sync_client.read_ushort("hr0") == (False, None)
    sync_state = (
        sync_client.connected,
        sync_client.last_error_category,
        sync_client.last_error_code,
    )

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        client.receive_timeout = 0.1
        scripted = ScriptedAsyncTransport([], hang=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        assert await client.read_ushort("hr0") == (False, None)
        holder["state"] = (
            client.connected,
            client.last_error_category,
            client.last_error_code,
        )

    loop.run_until_complete(scenario())
    assert sync_state == holder["state"] == (False, ErrorCategory.TIMEOUT, None)


# ----------------------------------------------------------------------
# 取消语义(原生层核心收益)
# ----------------------------------------------------------------------


def test_cancel_mid_flight_disconnects(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """请求已发出后取消:真中断 + 保守拆连(链路可能残留未配对应答)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport([], hang=True)
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        task = asyncio.ensure_future(client.read_ushort("hr0"))
        await asyncio.sleep(0.05)  # 让请求发出去
        task.cancel()
        try:
            await task
            holder["cancelled"] = False
        except asyncio.CancelledError:
            holder["cancelled"] = True
        holder["connected"] = client.connected
        holder["disconnect_count"] = client.stats["disconnect_count"]
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["cancelled"] is True
    assert holder["connected"] is False
    assert holder["disconnect_count"] == 1


def test_cancel_while_queued_keeps_connection(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """排队等锁时被取消:尚未发出任何字节 → 不拆连,首个事务照常完成。"""

    class _SlowTransport(ScriptedAsyncTransport):
        async def send(self, data: bytes) -> None:
            await super().send(data)
            await asyncio.sleep(0.2)  # 拖住事务,让第二个请求排队

    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = _SlowTransport(
            _chunks([(1, _RESP_ONE_REGISTER), (2, _RESP_ONE_REGISTER)])
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        first = asyncio.ensure_future(client.read_ushort("hr0"))
        await asyncio.sleep(0.05)
        second = asyncio.ensure_future(client.read_ushort("hr0"))
        await asyncio.sleep(0.02)  # 第二个在等锁
        second.cancel()
        try:
            await second
            holder["cancelled"] = False
        except asyncio.CancelledError:
            holder["cancelled"] = True
        holder["first"] = await first
        holder["connected"] = client.connected
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["cancelled"] is True
    assert holder["first"] == (True, 20)
    assert holder["connected"] is True


# ----------------------------------------------------------------------
# 扩展面平展:批量合并与扩展功能码(FC 07/08/11/12/17/20/21/22/23/24、FC 43)
# ----------------------------------------------------------------------

_DEVICE_ID_PAGE1 = (
    bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x04]) + b"ACME"
)
"""FC43 流式第 1 页:读码 1、符合级别 1、无后续、1 个对象(0x00 厂商名)。"""
_DEVICE_ID_PAGE2 = bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x03, 0x01, 0x01, 0x02]) + b"PC"
"""FC43 第 2 页(请求对象号 0x03 起):1 个对象(0x01 产品代码)。"""


class ExtCase(NamedTuple):
    """一条扩展面对拍用例(参数化调用客户端,两侧比对帧/结果/错误口径)。

    ``expect_fc`` 为**按序**期望的请求功能码列表——逐字节对拍只能证明"两层
    一样",这里额外把"该合并的确实合并成了一笔"钉住(如 read_many 三点连续
    地址 = 1 笔 FC03,而非 3 笔)。
    """

    name: str
    op: str
    args: Tuple[Any, ...]
    responses: Sequence[Tuple[int, bytes]]
    expect_fc: Tuple[int, ...]


_EXT_CASES = [
    # 批量读:连续地址合一笔;留空洞各起一笔
    ExtCase(
        "read_many_coalesced",
        "read_many",
        (("hr0", "hr1", "hr2"), "ushort"),
        ((1, _resp_registers(struct.pack(">HHH", 10, 20, 30))),),
        (3,),
    ),
    ExtCase(
        "read_many_gap",
        "read_many",
        (("hr0", "hr5"), "ushort"),
        (
            (1, _resp_registers(struct.pack(">H", 10))),
            (2, _resp_registers(struct.pack(">H", 50))),
        ),
        (3, 3),
    ),
    ExtCase(
        "read_batch_mixed",
        "read_batch",
        ((("hr0", "ushort"), ("hr1", "float"), ("c0", "bool")),),
        (
            (1, _resp_registers(struct.pack(">H", 7))),
            (2, _resp_registers(struct.pack(">f", 1.5))),
            (3, bytes([1, 1, 0x01])),
        ),
        (3, 3, 1),
    ),
    # 批量写:FC16 合并;寄存器位写走 RMW(FC03 读 + FC06 写)
    ExtCase(
        "write_many_coalesced",
        "write_many",
        ((("hr0", "ushort", 10), ("hr1", "ushort", 20)),),
        ((1, bytes([0x10, 0x00, 0x00, 0x00, 0x02])),),
        (0x10,),
    ),
    ExtCase(
        "write_many_rmw_then_chunk",
        "write_many",
        ((("hr0.3", "bool", True), ("hr1", "ushort", 20)),),
        (
            (1, bytes([3, 2, 0x00, 0x00])),
            (2, bytes([6, 0x00, 0x00, 0x00, 0x08])),
            (3, bytes([0x10, 0x00, 0x01, 0x00, 0x01])),
        ),
        (3, 6, 0x10),
    ),
    ExtCase(
        "write_batch_mixed",
        "write_batch",
        ((("c0", "bool", True), ("hr1", "ushort", 20)),),
        (
            (1, bytes([0x0F, 0x00, 0x00, 0x00, 0x01])),
            (2, bytes([0x10, 0x00, 0x01, 0x00, 0x01])),
        ),
        (0x0F, 0x10),
    ),
    # FC22 掩码写:正常响应 = 请求 PDU 逐字节回显
    ExtCase(
        "write_mask_register",
        "mask_write",
        ("hr10", 0x00F0, 0x000F),
        ((1, codec.build_mask_write_pdu(10, 0x00F0, 0x000F, "big")),),
        (0x16,),
    ),
    # FC23 单事务"先写后读"
    ExtCase(
        "read_write_registers",
        "rw",
        ("hr0", 2, "hr10", [1, 2]),
        ((1, bytes([0x17, 0x04]) + struct.pack(">HH", 100, 200)),),
        (0x17,),
    ),
    # FC43 流式访问:单页;两页(MoreFollows)自动翻页
    ExtCase(
        "read_device_id_one_page",
        "device_id",
        ("basic",),
        ((1, _DEVICE_ID_PAGE1),),
        (0x2B,),
    ),
    ExtCase(
        "read_device_id_paged",
        "device_id",
        ("basic",),
        (
            (
                1,
                bytes([0x2B, 0x0E, 0x01, 0x01, 0xFF, 0x03, 0x01, 0x00, 0x04]) + b"ACME",
            ),
            (2, _DEVICE_ID_PAGE2),
        ),
        (0x2B, 0x2B),
    ),
    # FC43 个体访问(读取码 4)
    ExtCase(
        "read_device_object",
        "device_object",
        (0x01,),
        ((1, bytes([0x2B, 0x0E, 0x04, 0x01, 0x00, 0x00, 0x01, 0x01, 0x02]) + b"PC"),),
        (0x2B,),
    ),
    # 串行线诊断族:FC07 / FC17 / FC08 / FC11 / FC12
    ExtCase("read_exception_status", "fc07", (), ((1, bytes([0x07, 0xAB])),), (0x07,)),
    ExtCase(
        "report_server_id",
        "fc17",
        (),
        ((1, bytes([0x11, 0x04, 0x42, 0x00, 0xAA, 0xBB])),),
        (0x11,),
    ),
    ExtCase(
        "diagnostics",
        "fc08",
        (0x0000, 0x1234),
        ((1, bytes([0x08, 0x00, 0x00, 0x12, 0x34])),),
        (0x08,),
    ),
    ExtCase(
        "get_comm_event_counter",
        "fc11",
        (),
        ((1, bytes([0x0B, 0x00, 0x00, 0x12, 0x34])),),
        (0x0B,),
    ),
    ExtCase(
        "get_comm_event_log",
        "fc12",
        (),
        ((1, bytes([0x0C, 0x08, 0x00, 0x00, 0x00, 0x05, 0x00, 0x03, 0xAA, 0xBB])),),
        (0x0C,),
    ),
    # FC20 读文件记录:子响应 = File resp. length(1) + 引用类型(1) + 数据(2N)
    ExtCase(
        "read_file_record",
        "fc20",
        (((1, 0, 2),),),
        ((1, bytes([0x14, 0x06, 0x05, 0x06]) + struct.pack(">HH", 111, 222)),),
        (0x14,),
    ),
    # FC21 写文件记录:正常响应 = 请求 PDU 逐字节回显
    ExtCase(
        "write_file_record",
        "fc21",
        (((1, 0, [1, 2]),),),
        ((1, codec.build_write_file_record_pdu([(1, 0, [1, 2])])),),
        (0x15,),
    ),
    # FC24 读 FIFO 队列(byte count = 2 + 2×FIFO 数)
    ExtCase(
        "read_fifo_queue",
        "fc24",
        ("hr10",),
        ((1, bytes([0x18, 0x00, 0x06, 0x00, 0x02]) + struct.pack(">HH", 7, 8)),),
        (0x18,),
    ),
]


def _call_ext(client: Any, case: ExtCase, is_async: bool = False) -> Any:
    """按用例调用扩展方法(同步客户端返回结果,异步客户端返回协程)。

    ``is_async`` 与 :func:`_call` 同形参但同样不使用——调用哪个方法由客户端
    类型决定,协程由异步运行器 ``await``。
    """
    op, args = case.op, case.args
    if op == "read_many":
        return client.read_many(*args)
    if op == "read_batch":
        return client.read_batch(*args)
    if op == "write_many":
        return client.write_many(*args)
    if op == "write_batch":
        return client.write_batch(*args)
    if op == "mask_write":
        return client.write_mask_register(*args)
    if op == "rw":
        return client.read_write_registers(*args)
    if op == "device_id":
        return client.read_device_id(*args)
    if op == "device_object":
        return client.read_device_object(*args)
    if op == "fc07":
        return client.read_exception_status()
    if op == "fc17":
        return client.report_server_id()
    if op == "fc08":
        return client.diagnostics(*args)
    if op == "fc11":
        return client.get_comm_event_counter()
    if op == "fc12":
        return client.get_comm_event_log()
    if op == "fc20":
        return client.read_file_record(*args)
    if op == "fc21":
        return client.write_file_record(*args)
    if op == "fc24":
        return client.read_fifo_queue(*args)
    raise AssertionError("未知扩展用例操作:{}".format(op))


def _frame_pdus(data: bytes) -> list:
    """把脚本记录到的连续 MBAP 帧拆成 ``[(事务号, PDU), ...]``(测试脚手架)。"""
    frames = []
    offset = 0
    while offset < len(data):
        length = int.from_bytes(data[offset + 4 : offset + 6], "big")
        total = 6 + length  # MBAP 头 6 字节 + 长度域(含 Unit ID)
        frames.append(
            (
                int.from_bytes(data[offset : offset + 2], "big"),
                data[offset + 7 : offset + total],
            )
        )
        offset += total
    return frames


@pytest.mark.parametrize("case", _EXT_CASES, ids=[case.name for case in _EXT_CASES])
def test_sync_async_parity_extended(
    monkeypatch: pytest.MonkeyPatch, loop: Any, case: ExtCase
) -> None:
    """扩展面对拍:请求帧逐字节相同 + 结果/错误口径/计数一致 + 合并笔数符合期望。"""
    sync_sent, sync_result, sync_snapshot, _sync_error = _run_sync(
        monkeypatch, case, invoke=_call_ext
    )
    async_sent, async_result, async_snapshot, _async_error = _run_async(
        monkeypatch, case, loop, invoke=_call_ext
    )

    assert async_sent == sync_sent, "请求帧必须逐字节相同"
    assert async_result == sync_result
    assert async_snapshot == sync_snapshot
    assert tuple(pdu[0] for _tid, pdu in _frame_pdus(sync_sent)) == case.expect_fc


def test_read_many_fails_whole_batch(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """任一笔 FC 失败 → 整批 ``(False, None)``(不放出部分值,与同步 read_many 同口径)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(
            _chunks(
                [(1, _resp_registers(struct.pack(">H", 10))), (2, _RESP_DEVICE_ERROR)]
            )
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        # hr0 与 hr5 留空洞 → 两笔 FC03;第二笔 PLC 回异常码 02
        holder["result"] = await client.read_many(("hr0", "hr5"), DataType.USHORT)
        holder["category"] = client.last_error_category
        holder["code"] = client.last_error_code
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["result"] == [(False, None), (False, None)]
    assert holder["category"] == ErrorCategory.DEVICE
    assert holder["code"] == 2


def test_fc11_busy_code_zero_mirror(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """native FC11 忙态(0xFFFF)→ code=0 无码口径(与同步侧 R9-2 镜像)。

    review-1018 P1-3:native 曾透传 0xFFFF 作错误码计入 device_error_count;
    忙态是设备侧条件而非 PLC 返回的协议错误码,两侧统一 code=0。
    """
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(
            _chunks([(1, bytes([0x0B, 0xFF, 0xFF, 0x00, 0x00]))])
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        holder["result"] = await client.get_comm_event_counter()
        holder["connected"] = client.connected
        holder["code"] = client.last_error_code
        holder["text"] = client.last_error
        holder["device_errors"] = client.stats["device_error_count"]
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["result"] == (False, None)
    assert holder["connected"] is True
    assert holder["code"] is None  # code=0 无码口径(与同步侧一致)
    assert "0xFFFF" in (holder["text"] or "")
    assert holder["device_errors"] == 0


def test_read_range_single_fc(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """native read_range:hr0 起 3 个 SHORT = 单笔 FC 03 读 3 字(与同步帧一致)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(
            _chunks([(1, _resp_registers(struct.pack(">hhh", 10, 20, 30)))])
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        holder["result"] = await client.read_range("hr0", 3, DataType.SHORT)
        holder["sent"] = bytes(scripted.sent)
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["result"] == (True, [10, 20, 30])
    assert holder["sent"] == codec.build_mbap(1, 1, codec.build_read_pdu(3, 0, 3))


def test_read_range_rejects_before_frame(loop: Any) -> None:
    """native read_range 入参校验:超上限/位号后缀/寄存器区 BOOL/基类拒绝。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        with pytest.raises(ValueError):
            await client.read_range("hr0", 126, DataType.SHORT)
        with pytest.raises(ValueError):
            await client.read_range("hr0.3", 2, DataType.BOOL)
        with pytest.raises(ValueError):
            await client.read_range("hr0", 4, DataType.BOOL)
        with pytest.raises(ValueError):
            await client.read_range("hr0", 0, DataType.SHORT)

        class _Unsupported(AsyncModbusTcpClient):
            pass

        from omniplc.native.base import AsyncBaseClient

        with pytest.raises(ValueError):
            await AsyncBaseClient.read_range(client, "hr0", 2, DataType.USHORT)
        holder["done"] = True

    loop.run_until_complete(scenario())
    assert holder["done"] is True


def test_write_many_chunks_fail_independently(
    monkeypatch: pytest.MonkeyPatch, loop: Any
) -> None:
    """``write_many`` 逐 chunk 独立事务:失败的 chunk 置 False,其余照常写入。

    与 ``write_batch`` 的"整批 (False, None)"是两种刻意不同的契约
    (见同步层 ``_coalesce_and_write`` 的 ``fail_fast`` 分流)。
    """
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport(
            _chunks(
                [
                    (1, bytes([0x10, 0x00, 0x00, 0x00, 0x02])),  # hr0~hr1 写成功
                    (2, _RESP_DEVICE_ERROR),  # hr5 那笔被 PLC 拒绝
                ]
            )
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        await client.connect()
        holder["result"] = await client.write_many(
            (
                ("hr0", DataType.USHORT, 1),
                ("hr1", DataType.USHORT, 2),
                ("hr5", DataType.USHORT, 3),
            )
        )
        await client.close()

    loop.run_until_complete(scenario())
    assert holder["result"] == [True, True, False]


# ----------------------------------------------------------------------
# 并发:同客户端串行 FIFO;多客户端真并发
# ----------------------------------------------------------------------


def test_same_client_concurrent_is_serial_fifo(loop: Any) -> None:
    """同一客户端并发 5 笔:全部成功、计数=事务数、顺序串行(真事件循环)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        responder = TcpResponder(lambda _pdu: _RESP_ONE_REGISTER)
        await responder.start()
        client = AsyncModbusTcpClient("127.0.0.1", responder.port, 1)
        try:
            results = await asyncio.gather(
                *[client.read_ushort("hr0") for _ in range(5)]
            )
            holder["values"] = [value for _ok, value in results]
            holder["transactions"] = client.stats["transactions"]
            holder["connected"] = client.connected
            holder["requests"] = len(responder.sent)
        finally:
            await client.close()
            await responder.stop()

    loop.run_until_complete(scenario())
    assert holder["values"] == [20] * 5
    assert holder["transactions"] == 5
    assert holder["connected"] is True
    assert holder["requests"] == 5


def test_multiple_clients_run_concurrently(loop: Any) -> None:
    """多客户端并发:总耗时 ≈ 单笔(串行应为 10 倍)——原生层真并发证据。"""
    holder: Dict[str, Any] = {}
    delay = 0.2

    async def scenario() -> None:
        responder = TcpResponder(lambda _pdu: _RESP_ONE_REGISTER, delay=delay)
        await responder.start()
        clients = [
            AsyncModbusTcpClient("127.0.0.1", responder.port, 1) for _ in range(6)
        ]
        started = loop.time()
        try:
            results = await asyncio.gather(
                *[client.read_ushort("hr0") for client in clients]
            )
            holder["elapsed"] = loop.time() - started
            holder["values"] = [value for _ok, value in results]
        finally:
            for client in clients:
                await client.close()
            await responder.stop()

    loop.run_until_complete(scenario())
    assert holder["values"] == [20] * 6
    assert holder["elapsed"] < delay * 3, "6 个客户端应并行等待,而非串行 6×delay"


# ----------------------------------------------------------------------
# 生命周期:关闸、惰性重连、事件循环外构造
# ----------------------------------------------------------------------


def test_close_gate_blocks_further_calls(loop: Any) -> None:
    """``close()`` 关闸:之后任何协议调用抛 ``RuntimeError``,自身幂等。"""

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 1, 1)
        await client.close()
        await client.close()  # 幂等
        with pytest.raises(RuntimeError):
            await client.read_ushort("hr0")
        with pytest.raises(RuntimeError):
            await client.connect()

    loop.run_until_complete(scenario())


def test_lazy_reconnect_after_transport_loss(loop: Any) -> None:
    """传输掉线后下一次事务惰性重连(与同步层同语义)。"""
    holder: Dict[str, Any] = {}

    async def scenario() -> None:
        responder = TcpResponder(lambda _pdu: _RESP_ONE_REGISTER)
        await responder.start()
        client = AsyncModbusTcpClient("127.0.0.1", responder.port, 1)
        try:
            assert await client.read_ushort("hr0") == (True, 20)
            transport = client._transport
            assert transport is not None
            transport.close()  # 模拟断线
            client._connected = False
            holder["second"] = await client.read_ushort("hr0")
            holder["connect_count"] = client.stats["connect_count"]
        finally:
            await client.close()
            await responder.stop()

    loop.run_until_complete(scenario())
    assert holder["second"] == (True, 20)
    assert holder["connect_count"] == 2


def test_client_constructed_outside_loop(loop: Any) -> None:
    """模块级构造(尚未 asyncio.run)后再用:惰性事务锁不得绑错循环。

    3.7 的 ``asyncio.Lock`` 构造即绑定当时的事件循环,若在构造函数里建锁,
    顶层 ``client = AsyncModbusTcpClient(...)`` + ``asyncio.run(...)`` 会直接炸。
    """
    client = AsyncModbusTcpClient("127.0.0.1", 1, 1)  # 事件循环之外构造

    async def scenario() -> None:
        assert client.next_connect_in is None
        assert client.connected is False
        await client.close()

    loop.run_until_complete(scenario())


def test_string_rejects_bit_suffix(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """native 字符串读写同同步侧口径:拒绝位号后缀(.bit)。

    回归:native 未镜像同步侧 ``modbus/modbus.py`` 的校验,``hr0.3`` 被
    静默当作 ``hr0`` 整字读写。
    """

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        scripted = ScriptedAsyncTransport([])
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        with pytest.raises(ValueError):
            await client.read_string("hr0.3", 4)
        with pytest.raises(ValueError):
            await client.write_string("hr0.3", "AB")
        await client.close()

    loop.run_until_complete(scenario())


# ----------------------------------------------------------------------
# 探活(FC08 回显)
# ----------------------------------------------------------------------


def test_ping_fc08(monkeypatch: pytest.MonkeyPatch, loop: Any) -> None:
    """原生 ping:FC08 回显(规范 §6.8)为探测命令;异常码应答不断线。"""

    async def scenario() -> None:
        client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
        assert client.ping_supported is True
        scripted = ScriptedAsyncTransport(
            _chunks([(1, bytes([8, 0x00, 0x00, 0x00, 0x00])), (2, bytes([0x88, 0x01]))])
        )
        monkeypatch.setattr(client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.ping() is True
        # 从站不支持 FC08 → 异常码 01 应答:ping False 但链路完好
        assert await client.ping() is False
        assert client.connected is True
        await client.close()

    loop.run_until_complete(scenario())
