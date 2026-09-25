"""欧姆龙 NJ/NX CIP 客户端测试:直发 RRData、空路由 Forward Open、错误契约。

走线差异按 CIP/EtherNet/IP 规范交叉核证:unconnected 直发不包 UC Send
(0xB2 项直接承载服务请求/应答),connected 连接路径只剩消息路由对象
(20 02 24 01)。继承面(类型发现/位访问/惰性重连)由 AB 测试覆盖,
此处聚焦 NJ/NX 差异点与异步镜像。
"""
from __future__ import annotations

import asyncio
import struct
from typing import List

import pytest

from omniplc import OmronCipClient
from omniplc.aio import AOmronCipClient
from omniplc.core.constants import AB_EIP_DEFAULT_PORT, AB_EIP_ORIGINATOR_VENDOR_ID
from omniplc.plc.ab import AllenBradleyEthIpClient, codec_cip
from omniplc.transport import TcpTransport
from scripted import ScriptedTransport

_SESSION = 0x12345678
_TO_ID = 0x11112222
_OT_ID = 0xAABBCCDD


def _session_chunks() -> List[bytes]:
    """RegisterSession 应答按 recv 尺寸分片(24 头 + 4 载荷)。"""
    reply = struct.pack("<HHIIQIHH", 0x65, 4, _SESSION, 0, 0, 0, 1, 0)
    return [reply[:24], reply[24:]]


def _direct_reply_chunks(
    payload: bytes = b"",
    service: int = codec_cip.CIP_SERVICE_READ_TAG,
    cip_status: int = 0,
    enip_status: int = 0,
) -> List[bytes]:
    """直发应答分片(0xB2 项直接承载服务应答,无 0xD2 外层)。"""
    cip = bytes((service | 0x80, 0, cip_status, 0)) + payload
    frame = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, enip_status, 0, 0)
    frame += struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(cip))
    frame += cip
    return [frame[:24], frame[24:]]


def _atomic_payload(cip_type: int, data: bytes) -> bytes:
    """原子类型读应答数据域:类型码 + 0x00 + 值。"""
    return bytes((cip_type, 0)) + data


def _mount(
    monkeypatch: pytest.MonkeyPatch, client: OmronCipClient, scripted: ScriptedTransport
) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_defaults_and_transport() -> None:
    """默认端口 44818、继承 AB 客户端、默认 unconnected 直发。"""
    client = OmronCipClient()
    assert client._ip_address == "192.168.0.10"
    assert client._port == AB_EIP_DEFAULT_PORT == 44818
    assert isinstance(client, AllenBradleyEthIpClient)
    assert client.connected_messaging is False
    assert client._route_path() == b""
    assert isinstance(client._create_transport(), TcpTransport)


def test_read_dint_direct_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """DINT 读:直发帧逐字节等于 RRData + 裸 Tag Read(无 UC Send 包裹)。"""
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks() + _direct_reply_chunks(_atomic_payload(0xC4, b"\x39\x05\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("TestVar") == (True, 1337)
    assert client._known_types["TestVar"] == 0xC4
    sent = bytes(scripted.sent)
    assert sent[:28] == codec_cip.build_register_session()
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("TestVar",), ((),)), 1
    )
    assert sent[28:] == codec_cip.build_rr_data(_SESSION, tag_read)


def test_read_bool_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """标量 BOOL 读:类型发现(C1)+ 直读,两个事务。"""
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(_atomic_payload(0xC1, b"\x00"))
        + _direct_reply_chunks(_atomic_payload(0xC1, b"\x01"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("RunFlag") == (True, True)


def test_write_bool_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """标量 BOOL 写:类型发现 + 携带类型码的直写帧(0x4D)。"""
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(_atomic_payload(0xC1, b"\x00"))
        + _direct_reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("RunFlag", True) is True
    write_request = codec_cip.build_tag_write(
        codec_cip.build_symbol_path(("RunFlag",), ((),)), 0xC1, b"\x01"
    )
    assert bytes(scripted.sent).endswith(codec_cip.build_rr_data(_SESSION, write_request))


def test_cip_status_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """CIP 状态 0x08:DeviceError 不断线,下一事务不重新注册。"""
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(b"", cip_status=0x08)
        + _direct_reply_chunks(_atomic_payload(0xC4, b"\x07\x00\x00\x00"))
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("TestVar") == (False, None)
    assert "服务不支持" in (client.last_error or "")
    assert client.connected is True
    assert client.read_int("TestVar") == (True, 7)
    assert bytes(scripted.sent).count(codec_cip.build_register_session()) == 1


def test_string_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """NJ/NX STRING 读:len(u32)+字符,继承结构体解析路径。"""
    client = OmronCipClient("127.0.0.1", 44818)
    payload = bytes([0xA0, 0x00, 0x34, 0x12]) + struct.pack("<I", 5) + b"HELLO"
    scripted = ScriptedTransport(_session_chunks() + _direct_reply_chunks(payload))
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_string("MyString") == (True, "HELLO")


def test_string_write_carries_template_and_declared_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NJ/NX STRING 写:先读模板号与声明尺寸,写入类型域回带模板号。"""
    client = OmronCipClient("127.0.0.1", 44818)
    # 结构体 = 类型域(4)+ len(u32)(4)+ 字符区 10 → 声明可写 10 字符
    payload = (
        bytes([0xA0, 0x00, 0x34, 0x12]) + struct.pack("<I", 5) + b"HELLO" + b"\x00" * 5
    )
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(payload)
        + _direct_reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_string("MyString", "HI") is True
    data = struct.pack("<I", 2) + b"HI" + b"\x00" * 8
    write_request = codec_cip.build_string_write(
        codec_cip.build_symbol_path(("MyString",), ((),)), data, template_id=0x1234
    )
    assert bytes(scripted.sent).endswith(codec_cip.build_rr_data(_SESSION, write_request))


def test_string_write_rejects_overflow_of_declared_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入值超 NJ STRING 声明尺寸:ValueError 且不发写帧。"""
    client = OmronCipClient("127.0.0.1", 44818)
    payload = bytes([0xA0, 0x00, 0x34, 0x12]) + struct.pack("<I", 2) + b"AB"
    scripted = ScriptedTransport(_session_chunks() + _direct_reply_chunks(payload))
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.write_string("MyString", "TOOLONG")
    read_request = codec_cip.build_rr_data(
        _SESSION,
        codec_cip.build_tag_read(codec_cip.build_symbol_path(("MyString",), ((),)), 1),
    )
    assert bytes(scripted.sent).endswith(read_request)  # 只发了先读,无写帧出线


# ----------------------------------------------------------------------
# BOOL 数组:按元素访问,实际类型由自描述应答决定
# ----------------------------------------------------------------------


def test_bool_array_element_direct_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """类型发现回存储字类型时,元素直读应答 BOOL 按本体取值(NJ 口径)。"""
    client = OmronCipClient("127.0.0.1", 44818)
    dword = codec_cip.CIP_TYPE_DWORD
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(_atomic_payload(dword, b"\x00\x00\x00\x00"))  # 探 Bits[0]
        + _direct_reply_chunks(_atomic_payload(0xC1, b"\x01"))               # Bits[5]→BOOL
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("Bits[5]") == (True, True)


def test_bool_array_element_direct_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """BOOL 数组元素写:先按元素以 BOOL 类型直写(0x4D 携带 C1)。"""
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(
            _atomic_payload(codec_cip.CIP_TYPE_DWORD, b"\x00\x00\x00\x00")
        )
        + _direct_reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("Bits[5]", True) is True
    write_request = codec_cip.build_tag_write(
        codec_cip.build_symbol_path(("Bits",), ((5,),)), 0xC1, b"\x01"
    )
    assert bytes(scripted.sent).endswith(codec_cip.build_rr_data(_SESSION, write_request))


def test_bool_array_element_dword_reply_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """元素直读应答存储字类型(DWORD)时,按 Logix 下标//32 打包口径回退。"""
    client = OmronCipClient("127.0.0.1", 44818)
    dword = codec_cip.CIP_TYPE_DWORD
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(_atomic_payload(dword, b"\x00\x00\x00\x00"))  # 探 →DWORD
        + _direct_reply_chunks(_atomic_payload(dword, b"\x00\x00\x00\x00"))  # Bits[5]→DWORD
        + _direct_reply_chunks(_atomic_payload(dword, b"\x20\x00\x00\x00"))  # Bits[0] bit5=1
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("Bits[5]") == (True, True)


# ----------------------------------------------------------------------
# connected 消息:连接路径只剩消息路由对象(20 02 24 01)
# ----------------------------------------------------------------------


def _rr_data_frame(cip: bytes) -> bytes:
    """裸 CIP 应答的 RRData 帧(测试脚手架)。"""
    frame = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, 0, 0, 0)
    frame += struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(cip))
    return frame + cip


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
    frame = struct.pack("<HHIIQI", 0x70, 22 + len(cip), _SESSION, 0, 0, 0)
    frame += (
        struct.pack("<IHH", 0, 0, 2)
        + struct.pack("<HHI", 0xA1, 4, _TO_ID)
        + struct.pack("<HHH", 0xB1, len(cip) + 2, sequence)
    )
    frame += cip
    return [frame[:24], frame[24:]]


def _connected_client() -> OmronCipClient:
    """connected 模式客户端,T->O 连接 ID 固定便于断言。"""
    client = OmronCipClient("127.0.0.1", 44818, connected_messaging=True)
    client._originator_serial = 42
    return client


def test_connected_forward_open_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 读:Forward Open 连接路径无背板段,SendUnitData 往返。"""
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
    assert client.read_int("TestVar") == (True, 1337)
    sent = bytes(scripted.sent)
    register = codec_cip.build_register_session()
    forward_open_request = codec_cip.build_forward_open(
        True,
        codec_cip.CONNECTION_SIZE_LARGE,
        client._connection_serial,
        _TO_ID,
        AB_EIP_ORIGINATOR_VENDOR_ID,
        42,
        b"",
    )
    # 连接路径 = 字数 2 + 仅消息路由对象(无 01 00 背板段)
    assert forward_open_request.endswith(bytes.fromhex("02" "20022401"))
    forward_open = codec_cip.build_rr_data(_SESSION, forward_open_request)
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("TestVar",), ((),)), 1
    )
    unit_data = codec_cip.build_send_unit_data(_SESSION, _OT_ID, 1, tag_read)
    assert sent == register + forward_open + unit_data


def test_connected_fallback_to_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large 被拒(状态 0x01)回落普通 Forward Open,回落路径同样无背板段。"""
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
    assert client.read_int("TestVar") == (True, 6)
    sent = bytes(scripted.sent)
    tag_read = codec_cip.build_tag_read(
        codec_cip.build_symbol_path(("TestVar",), ((),)), 1
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
            b"",
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
            b"",
        ),
    )
    unit_data = codec_cip.build_send_unit_data(_SESSION, _OT_ID, 1, tag_read)
    assert sent == (
        codec_cip.build_register_session() + large_open + normal_open + unit_data
    )
    # AB 的"背板+槽号+消息路由"路径不应出现在任何帧里
    assert bytes.fromhex("010020022401") not in sent


def test_connected_disconnect_sends_forward_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """connected 断开:Forward Close(空路由)+ UnregisterSession。"""
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
            b"",
        ),
    )
    assert sent.endswith(forward_close + codec_cip.build_unregister_session(_SESSION))


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:单工作线程完成会话注册 + 直发读写往返。"""

    async def scenario() -> None:
        client = AOmronCipClient("127.0.0.1", 44818)
        assert client.connected_messaging is False
        sync = client._sync
        assert isinstance(sync, OmronCipClient)
        scripted = ScriptedTransport(
            _session_chunks()
            + _direct_reply_chunks(_atomic_payload(0xC4, b"\x05\x00\x00\x00"))
            + _direct_reply_chunks(service=codec_cip.CIP_SERVICE_WRITE_TAG)
        )
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_int("TestVar") == (True, 5)
        assert await client.write_int("TestVar", 6) is True
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 通用 CIP 服务入口:NJ/NX 通过继承复用
# ----------------------------------------------------------------------

def test_get_plc_info_works_on_nj_direct_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NJ/NX 直发模式下 get_plc_info 走裸 RRData(不经 UC-Send),解析 7 字段。

    验证 :class:`OmronCipClient` 继承自 :class:`AllenBradleyEthIpClient` 的
    通用 CIP 服务入口,无需 override 即可在 NJ 直发路径上工作。
    """
    identity = struct.pack(
        "<HHHBBH",
        0x0001, 0x000E, 0x1234, 30, 11, 0x0001
    ) + struct.pack("<I", 0x00C0FFEE) + bytes((5,)) + b"NJ-NX"
    client = OmronCipClient("127.0.0.1", 44818)
    scripted = ScriptedTransport(
        _session_chunks()
        + _direct_reply_chunks(identity, service=codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL)
    )
    _mount(monkeypatch, client, scripted)
    ok, info = client.get_plc_info()
    assert ok is True
    assert info is not None
    assert info["vendor"] == 0x0001
    assert info["revision"] == (30, 11)
    assert info["serial"] == 0x00C0FFEE
    assert info["product_name"] == "NJ-NX"
    # 验证请求帧:裸 RRData(无 UC-Send 包裹)+ GetAttributesAll 请求体
    sent = bytes(scripted.sent)
    expected_req = codec_cip.build_rr_data(
        _SESSION, codec_cip.build_get_attributes_all(0x01, 0x01)
    )
    assert expected_req in sent
