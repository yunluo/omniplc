"""原生 × 同步**边界入参**对拍:同一输入下两层的异常类型/文案/返回形状必须一致。

各驱动的对拍表(``test_native_modbus``/``_melsec``/``_omron``)覆盖的是正常路径与
若干错误响应;这里锁的是**入参校验**这一类:

- **校验时机**——"入参期就抛" 与 "先连上、发帧、再失败" 是两种可观察行为;
  两层若把校验挪位,本表立刻红(一侧 ValueError,另一侧走到 I/O 失败)。
- **错误类型与文案**——两层共用同一批校验助手(``validation``/``convert``/
  各驱动 codec),文案必须逐字相同,否则同一份上层代码在两层会看到不同错误。

假传输挂**空脚本**:纯校验类用例根本不发帧;真正走到 I/O 的用例(如字软元件
无位号读 BOOL)两侧都以"脚本分片耗尽"失败——只要两侧**失败方式相同**即为通过,
这正是"校验时机一致"的判据。FINS/TCP 用例额外喂一条合法的握手应答,让两侧都
真的进入已连接状态(否则 FINS 用例会因为握手失败而"一致地失败",失去意义)。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, List, Tuple

import pytest

import omniplc as pkg
import omniplc.native as native
from omniplc.types import DataType
from scripted import ScriptedTransport
from scripted_async import ScriptedAsyncTransport

Outcome = Tuple[str, ...]
_Op = Callable[[Any], Any]


def _fins_handshake_reply(local_node: int = 11, plc_node: int = 5) -> bytes:
    """FINS/TCP 握手应答(命令 1、错误码 0,节点号在偏移 19 / 23)。

    长度域 **16**(应答含两个 4 字节节点字段);注意握手**请求**的长度域是 12,
    且 ``parse_tcp_head`` 有 v0.36 的"长度域 ≥ 14"坏帧防线,写成 12 会被判坏帧。
    """
    return (
        b"FINS"
        + (16).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00\x0b"
        + b"\x00\x00\x00\x05"
    )


def _fins_chunks() -> List[bytes]:
    """握手应答按"8 字节头 + 其余"切片(TCP 两段式收包)。"""
    reply = _fins_handshake_reply()
    return [reply[:8], reply[8:]]


def _cases() -> List[Tuple[str, str, str, str, _Op]]:
    """(协议族, 同步类名, 原生类名, 用例名, 操作)。"""
    shared: List[Tuple[str, str, _Op]] = [
        ("read_string_zero_len", "read_string 长度 0", lambda c: c.read_string("hr0", 0)),
        ("write_string_empty", "write_string 空串", lambda c: c.write_string("hr0", "")),
        ("write_bool_int5", "write_bool 拒 int 5", lambda c: c.write_bool("hr0", 5)),
        ("write_bool_str", "write_bool 拒字符串", lambda c: c.write_bool("hr0", "1")),
        ("read_unknown_type", "未知数据类型", lambda c: c.read("hr0", "NOT_A_TYPE")),
        ("read_type_string", "read 传 STRING", lambda c: c.read("hr0", DataType.STRING)),
    ]
    per_family = {
        "modbus": [
            ("bad_address", "坏地址", lambda c: c.read("xx9", DataType.USHORT)),
            ("write_ushort_range", "ushort 越界", lambda c: c.write_ushort("hr0", 70000)),
            ("write_short_range", "short 越界", lambda c: c.write_short("hr0", -40000)),
            ("string_on_coil", "位区读字符串", lambda c: c.read_string("coil0", 4)),
        ],
        "melsec": [
            ("bad_device", "未知软元件", lambda c: c.read("QQ100", DataType.USHORT)),
            ("write_short_range", "short 越界", lambda c: c.write_short("D100", 40000)),
            ("bool_word_no_bit", "字软元件无位号读 BOOL", lambda c: c.read_bool("D100")),
            ("bit_address_suffix", "位软元件带位号", lambda c: c.read("M10.3", DataType.BOOL)),
        ],
        "fins": [
            ("bad_area", "未知区域", lambda c: c.read("QQ100", DataType.USHORT)),
            ("write_short_range", "short 越界", lambda c: c.write_short("D100", 40000)),
            ("bool_word_no_bit", "字软元件无位号读 BOOL", lambda c: c.read_bool("D100")),
        ],
    }
    table: List[Tuple[str, str, str, str, _Op]] = []
    for family, sync_name, async_name in (
        ("modbus", "ModbusTcpClient", "AsyncModbusTcpClient"),
        ("melsec", "MelsecMcTcpClient", "AsyncMelsecMcTcpClient"),
        ("fins", "OmronFinsTcpClient", "AsyncOmronFinsTcpClient"),
    ):
        for case_name, _desc, op in shared + per_family[family]:
            table.append((family, sync_name, async_name, case_name, op))
    return table


def _make_sync(family: str, cls_name: str) -> Any:
    cls = getattr(pkg, cls_name)
    client = (
        cls("127.0.0.1", 502, 1)
        if family == "modbus"
        else cls("127.0.0.1", 9600)
    )
    chunks = _fins_chunks() if family == "fins" else []
    scripted = ScriptedTransport(list(chunks))
    client._create_transport = lambda: scripted  # type: ignore[method-assign]
    return client


def _make_async(family: str, cls_name: str) -> Any:
    cls = getattr(native, cls_name)
    client = (
        cls("127.0.0.1", 502, 1) if family == "modbus" else cls("127.0.0.1", 9600)
    )
    chunks = _fins_chunks() if family == "fins" else []
    scripted = ScriptedAsyncTransport(list(chunks))
    client._create_transport = lambda: scripted  # type: ignore[method-assign]
    return client


def _sync_outcome(family: str, cls_name: str, op: _Op) -> Outcome:
    client = _make_sync(family, cls_name)
    client.connect()
    try:
        value = op(client)
    except Exception as exc:  # noqa: BLE001 - 探针就是要看异常类型与文案
        return ("error", type(exc).__name__, str(exc))
    return ("value", repr(value))


def _async_outcome(family: str, cls_name: str, op: _Op) -> Outcome:
    client = _make_async(family, cls_name)

    async def scenario() -> Outcome:
        await client.connect()
        try:
            value = await op(client)
        except Exception as exc:  # noqa: BLE001
            return ("error", type(exc).__name__, str(exc))
        return ("value", repr(value))

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "family, sync_name, async_name, case_name, op",
    _cases(),
    ids=["{}-{}".format(case[0], case[3]) for case in _cases()],
)
def test_edge_input_parity(
    family: str, sync_name: str, async_name: str, case_name: str, op: _Op
) -> None:
    """同一边界入参在两层必须给出完全相同的可观察结果。"""
    sync_out = _sync_outcome(family, sync_name, op)
    async_out = _async_outcome(family, async_name, op)
    assert sync_out == async_out, "{} 两层结果不一致".format(case_name)
