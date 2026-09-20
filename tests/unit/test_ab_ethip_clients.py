"""AB EtherNet/IP 客户端测试:脚本化传输验证会话注册/标签读写/错误契约。

覆盖:会话注册与惰性重连重注册、类型发现(标签自描述)、字/位/BOOL 数组/
字符串读写、类型不符 ValueError、CIP 状态 DeviceError 不断线、异步镜像。
"""
from __future__ import annotations

import asyncio
import struct
from typing import List

import pytest

from omniplc import AllenBradleyEthIpClient, DataType
from omniplc.aio import AAllenBradleyEthIpClient
from omniplc.core.constants import (
    AB_EIP_DEFAULT_PORT,
    AB_EIP_ORIGINATOR_VENDOR_ID,
)
from omniplc.plc.ab import codec_cip
from omniplc.transport import TcpTransport
from omniplc.types import McFrame  # noqa: F401  (保持与其他测试一致的导入面)
from scripted import ScriptedTransport

_SESSION = 0x12345678


def _session_chunks() -> List[bytes]:
    """RegisterSession 应答按 recv 尺寸分片(24 头 + 4 载荷)。"""
    reply = struct.pack("<HHIIQIHH", 0x65, 4, _SESSION, 0, 0, 0, 1, 0)
    return [reply[:24], reply[24:]]


def _reply_chunks(
    payload: bytes = b"",
    service: int = codec_cip.CIP_SERVICE_READ_TAG,
    cip_status: int = 0,
    route_status: int = 0,
    enip_status: int = 0,
) -> List[bytes]:
    """完整 SendRRData 应答分片(24 头 + 剩余)。"""
    embedded = bytes((service | 0x80, 0, cip_status, 0)) + payload
    cip = bytes((0xD2, 0, route_status, 0)) + embedded
    header = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, enip_status, 0, 0)
    prefix = (
        struct.pack("<I", 0)
        + struct.pack("<H", 0)
        + struct.pack("<H", 2)
        + struct.pack("<HH", 0, 0)
        + struct.pack("<HH", 0xB2, len(cip))
    )
    frame = header + prefix + cip
    return [frame[:24], frame[24:]]


def _atomic_payload(cip_type: int, data: bytes) -> bytes:
    """原子类型读应答数据域:类型码 + 0x00 + 值。"""
    return bytes((cip_type, 0)) + data


def _string_payload(text: str) -> bytes:
    """STRING 读应答数据域:0xA0 + 模板号 + len(u32) + 字符(88 字节布局)。"""
    raw = text.encode("utf-8")
    return (
        bytes((0xA0, 0, 0xCE, 0x0F))
        + struct.pack("<I", len(raw))
        + raw
        + b"\x00" * (84 - 4 - len(raw) + 4)
    )


def _mount(
    monkeypatch: pytest.MonkeyPatch, client: AllenBradleyEthIpClient, scripted: ScriptedTransport
) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_defaults_and_transport() -> None:
    """默认端口 44818、槽号 0,走线为 TcpTransport;槽号越界拒绝。"""
    client = AllenBradleyEthIpClient()
    assert client._ip_address == "192.168.1.20"
    assert client._port == AB_EIP_DEFAULT_PORT == 44818
    assert client.slot == 0
    assert isinstance(client._create_transport(), TcpTransport)
    with pytest.raises(ValueError):
        AllenBradleyEthIpClient(slot=32)


def test_read_dint_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """DINT 读:注册会话 + 单事务往返,类型自描述并缓存。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    chunks = _session_chunks() + _reply_chunks(_atomic_payload(0xC4, b"\x39\x05\x00\x00"))
    scripted = ScriptedTransport(chunks)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (True, 1337)
    assert client._known_types["MyDint"] == 0xC4
    sent = bytes(scripted.sent)
    assert sent[:28] == codec_cip.build_register_session()
    expected = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_uc_send(
            codec_cip.build_tag_read(
                codec_cip.build_symbol_path(("MyDint",), ((),)), 1
            ),
            0,
        ),
    )
    assert sent[28:] == expected


def test_session_reused_across_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一连接的后续读不重复注册会话。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xC4, b"\x01\x00\x00\x00"))
        + _reply_chunks(_atomic_payload(0xC4, b"\x02\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("A") == (True, 1)
    assert client.read_int("A") == (True, 2)
    sent = bytes(scripted.sent)
    assert sent.count(codec_cip.build_register_session()) == 1


def test_read_type_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求类型与标签实际类型不符:ValueError(参数错误)。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks() + _reply_chunks(_atomic_payload(0xCA, b"\x00\x00\x60\x40"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_int("MyReal")
    assert "不符" in str(exc_info.value)


def test_read_bit_of_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """DINT.3 位读:类型发现 + 词读提位,两个事务。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xC4, b"\x00\x00\x00\x00"))
        + _reply_chunks(_atomic_payload(0xC4, b"\x08\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("MyDint.3") == (True, True)
    sent = bytes(scripted.sent)
    frames = sent[28:]
    assert len(frames) == 2 * (24 + 16 + 26)


def test_read_bool_array_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOOL 数组元素读:DWORD 存储字,下标 12 → 字 0 位 12。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xD3, b"\x00\x00\x00\x00"))
        + _reply_chunks(_atomic_payload(0xD3, b"\x00\x10\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("Bits[12]") == (True, True)


def test_write_dint_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """DINT 写:类型发现 + 携带类型码的写帧。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xC4, b"\x00\x00\x00\x00"))
        + _reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_int("MyDint", 1337) is True
    sent = bytes(scripted.sent)
    write_request = codec_cip.build_tag_write(
        codec_cip.build_symbol_path(("MyDint",), ((),)),
        0xC4,
        b"\x39\x05\x00\x00",
    )
    expected = codec_cip.build_rr_data(_SESSION, codec_cip.build_uc_send(write_request, 0))
    assert sent.endswith(expected)


def test_write_bool_direct_and_rmw(monkeypatch: pytest.MonkeyPatch) -> None:
    """布尔写:BOOL 直写 0x4D;DINT.3 走 0x4E 原子读-改-写。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xC1, b"\x00"))
        + _reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("MyBool", True) is True
    sent = bytes(scripted.sent)
    write_request = codec_cip.build_tag_write(
        codec_cip.build_symbol_path(("MyBool",), ((),)), 0xC1, b"\x01"
    )
    assert sent.endswith(codec_cip.build_rr_data(_SESSION, codec_cip.build_uc_send(write_request, 0)))

    rmw_client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    rmw_scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xC4, b"\x00\x00\x00\x00"))
        + _reply_chunks(service=codec_cip.CIP_SERVICE_READ_MODIFY_WRITE)
    )
    _mount(monkeypatch, rmw_client, rmw_scripted)
    rmw_client.connect()
    assert rmw_client.write_bool("MyDint.3", False) is True
    rmw_request = codec_cip.build_read_modify_write(
        codec_cip.build_symbol_path(("MyDint",), ((),)), 0xC4, 0, 0xFFFFFFF7
    )
    assert bytes(rmw_scripted.sent).endswith(
        codec_cip.build_rr_data(_SESSION, codec_cip.build_uc_send(rmw_request, 0))
    )


def test_write_bool_array_rmw(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOOL 数组元素写:位 12 → 字 0 的 OR/AND 掩码。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_atomic_payload(0xD3, b"\x00\x00\x00\x00"))
        + _reply_chunks(service=codec_cip.CIP_SERVICE_READ_MODIFY_WRITE)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("Bits[12]", True) is True
    sent = bytes(scripted.sent)
    assert b"\x4e" in sent[-66:]


def test_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """STRING 读(结构体应答)与写(模板 0x0FCE 类型域)。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(_string_payload("AB"))
        + _reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_string("MyString") == (True, "AB")
    assert client.write_string("MyString", "Hi") is True
    sent = bytes(scripted.sent)
    assert struct.pack("<I", 2) + b"Hi" in sent


def test_cip_status_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """CIP 状态 0x08:DeviceError 不断线,下一事务不重新注册。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(b"", cip_status=0x08)
        + _reply_chunks(_atomic_payload(0xC4, b"\x07\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (False, None)
    assert "服务不支持" in (client.last_error or "")
    assert client.read_int("MyDint") == (True, 7)
    assert bytes(scripted.sent).count(codec_cip.build_register_session()) == 1


def test_enip_error_triggers_reregister(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENIP 封装状态 0x64(会话失效):坏帧断线,下次读重注册。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _reply_chunks(b"", enip_status=0x64)
        + _session_chunks()
        + _reply_chunks(_atomic_payload(0xC4, b"\x09\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (False, None)
    assert client.read_int("MyDint") == (True, 9)
    assert bytes(scripted.sent).count(codec_cip.build_register_session()) == 2


def test_disconnect_unregisters(monkeypatch: pytest.MonkeyPatch) -> None:
    """disconnect 尽力发 UnregisterSession(0x0066)后关闭。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(_session_chunks())
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.disconnect() is True
    assert bytes(scripted.sent)[-24:-22] == b"\x66\x00"


def test_read_string_via_typed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """read(addr, DataType.STRING):标签自描述直接返回字符串。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks() + _reply_chunks(_string_payload("ABC"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read("MyString", DataType.STRING) == (True, "ABC")


def test_slot_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """槽号 2:路由段末字节为 02。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818, slot=2)
    scripted = ScriptedTransport(
        _session_chunks() + _reply_chunks(_atomic_payload(0xC4, b"\x01\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (True, 1)
    assert bytes(scripted.sent)[-1] == 2


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:单工作线程完成会话注册 + 读写往返。"""

    async def scenario() -> None:
        client = AAllenBradleyEthIpClient("127.0.0.1", 44818)
        assert client.slot == 0
        sync = client._sync
        scripted = ScriptedTransport(
            _session_chunks()
            + _reply_chunks(_atomic_payload(0xC4, b"\x05\x00\x00\x00"))
            + _reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
        )
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_int("MyDint") == (True, 5)
        assert await client.write_int("MyDint", 6) is True
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# connected 消息(Forward Open/Close + SendUnitData)
# ----------------------------------------------------------------------

_TO_ID = 0x11112222
_OT_ID = 0xAABBCCDD


def _rr_data_frame(cip: bytes) -> bytes:
    """裸 CIP 应答的 RRData 帧(测试脚手架)。"""
    header = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, 0, 0, 0)
    prefix = struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(cip))
    return header + prefix + cip


def _forward_open_chunks(service: int, status: int = 0) -> List[bytes]:
    """Forward Open 应答分片(成功带连接 ID 对)。"""
    if status == 0:
        cip = bytes((service | 0x80, 0, 0, 0)) + struct.pack("<II", _OT_ID, _TO_ID)
    else:
        cip = bytes((service | 0x80, 0, status, 0)) + b"\x00\x00"
    frame = _rr_data_frame(cip)
    return [frame[:24], frame[24:]]


def _connected_reply_chunks(
    payload: bytes,
    service: int,
    sequence: int,
    cip_status: int = 0,
) -> List[bytes]:
    """SendUnitData 应答分片(T->O ID/序列号回显)。"""
    cip = bytes((service | 0x80, 0, cip_status, 0)) + payload
    header = struct.pack("<HHIIQI", 0x70, 22 + len(cip), _SESSION, 0, 0, 0)
    prefix = (
        struct.pack("<IHH", 0, 0, 2)
        + struct.pack("<HHI", 0xA1, 4, _TO_ID)
        + struct.pack("<HHH", 0xB1, len(cip) + 2, sequence)
    )
    frame = header + prefix + cip
    return [frame[:24], frame[24:]]


def _connected_client() -> AllenBradleyEthIpClient:
    """connected 模式客户端,T->O 连接 ID 固定便于断言。"""
    client = AllenBradleyEthIpClient("127.0.0.1", 44818, connected_messaging=True)
    client._originator_serial = 42
    return client


def test_connected_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 读:Forward Open(Large)→ SendUnitData 往返,帧逐字节比对。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x39\x05\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            1,
        )
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.connection_size == codec_cip.CONNECTION_SIZE_LARGE
    assert client.read_int("MyDint") == (True, 1337)
    sent = bytes(scripted.sent)
    register = codec_cip.build_register_session()
    forward_open = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_forward_open(
            True,
            codec_cip.CONNECTION_SIZE_LARGE,
            client._connection_serial,
            _TO_ID,
            AB_EIP_ORIGINATOR_VENDOR_ID,
            42,
            0,
        ),
    )
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("MyDint",), ((),)), 1
    )
    unit_data = codec_cip.build_send_unit_data(_SESSION, _OT_ID, 1, tag_read)
    assert sent == register + forward_open + unit_data


def test_connected_fallback_to_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large 被拒(状态 0x01)回落普通 Forward Open,连接尺寸 504。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN, status=0x01)
        + _forward_open_chunks(codec_cip.CIP_SERVICE_FORWARD_OPEN)
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x06\x00\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            1,
        )
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.connection_size == codec_cip.CONNECTION_SIZE_NORMAL
    assert client.read_int("MyDint") == (True, 6)
    sent = bytes(scripted.sent)
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("MyDint",), ((),)), 1
    )
    large_open = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_forward_open(
            True,
            codec_cip.CONNECTION_SIZE_LARGE,
            client._connection_serial,
            _TO_ID,
            AB_EIP_ORIGINATOR_VENDOR_ID,
            42,
            0,
        ),
    )
    normal_open = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_forward_open(
            False,
            codec_cip.CONNECTION_SIZE_NORMAL,
            client._connection_serial,
            _TO_ID,
            AB_EIP_ORIGINATOR_VENDOR_ID,
            42,
            0,
        ),
    )
    unit_data = codec_cip.build_send_unit_data(_SESSION, _OT_ID, 1, tag_read)
    assert sent == (
        codec_cip.build_register_session() + large_open + normal_open + unit_data
    )
    assert sent[len(codec_cip.build_register_session()) + 24 + 16] == 0x5B
    assert sent[len(codec_cip.build_register_session()) + len(large_open) + 24 + 16] == 0x54


def test_connected_sequence_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 连续读:SendUnitData 序列号 1、2 递增。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x01\x00\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            1,
        )
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x02\x00\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            2,
        )
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (True, 1)
    assert client.read_int("MyDint") == (True, 2)
    sent = bytes(scripted.sent)
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("MyDint",), ((),)), 1
    )
    assert sent.endswith(
        codec_cip.build_send_unit_data(_SESSION, _OT_ID, 2, tag_read)
    )


def test_connected_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 模式 CIP 状态错误:DeviceError 不断线,不重建连接。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
        + _connected_reply_chunks(
            b"", codec_cip.CIP_SERVICE_READ_TAG, 1, cip_status=0x08
        )
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x09\x00\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            2,
        )
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("MyDint") == (False, None)
    assert "服务不支持" in (client.last_error or "")
    assert client.read_int("MyDint") == (True, 9)
    sent = bytes(scripted.sent)
    assert sent.count(codec_cip.build_register_session()) == 1
    # 仅一次 Forward Open(Large 签名:0x5B + CM 路径 + 优先级/超时)
    assert sent.count(b"\x5b\x02\x20\x06\x24\x01\x0a\x0e") == 1


def test_connected_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 写:类型发现读 + SendUnitData 写(回显 0x4D)。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
        + _connected_reply_chunks(
            _atomic_payload(0xC4, b"\x00\x00\x00\x00"),
            codec_cip.CIP_SERVICE_READ_TAG,
            1,
        )
        + _connected_reply_chunks(
            b"", codec_cip.CIP_SERVICE_WRITE_TAG, 2
        )
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_int("MyDint", 1337) is True
    sent = bytes(scripted.sent)
    write_request = codec_cip.build_tag_write(
        codec_cip.build_symbol_path(("MyDint",), ((),)),
        0xC4,
        b"\x39\x05\x00\x00",
    )
    assert sent.endswith(
        codec_cip.build_send_unit_data(_SESSION, _OT_ID, 2, write_request)
    )


def test_connected_disconnect_sends_forward_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 断开:先发 Forward Close 再注销会话(应答缺失被容忍)。"""
    monkeypatch.setattr(
        "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
    )
    client = _connected_client()
    scripted = ScriptedTransport(
        _session_chunks()
        + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.disconnect() is True
    sent = bytes(scripted.sent)
    forward_close = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_forward_close(
            client._connection_serial,
            AB_EIP_ORIGINATOR_VENDOR_ID,
            42,
            0,
        ),
    )
    assert sent.endswith(forward_close + codec_cip.build_unregister_session(_SESSION))


def test_async_mirror_connected_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:connected 模式单工作线程往返。"""

    async def scenario() -> None:
        monkeypatch.setattr(
            "omniplc.plc.ab.ab.random.randrange", lambda low, high: _TO_ID
        )
        client = AAllenBradleyEthIpClient(
            "127.0.0.1", 44818, connected_messaging=True
        )
        assert client.connected_messaging is True
        sync_client = client._sync
        assert isinstance(sync_client, AllenBradleyEthIpClient)
        sync_client._originator_serial = 42
        scripted = ScriptedTransport(
            _session_chunks()
            + _forward_open_chunks(codec_cip.CIP_SERVICE_LARGE_FORWARD_OPEN)
            + _connected_reply_chunks(
                _atomic_payload(0xC4, b"\x07\x00\x00\x00"),
                codec_cip.CIP_SERVICE_READ_TAG,
                1,
            )
        )
        monkeypatch.setattr(sync_client, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_int("MyDint") == (True, 7)
        await client.close()

    asyncio.run(scenario())
