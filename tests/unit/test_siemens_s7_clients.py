"""西门子 S7 自研栈测试:codec 黄金帧(参考字节锁)+ 会话全流程(假 TCP)。

黄金帧依据:docs/protocol/siemens/s7comm/README.md(python-snap7 3.2.0
帧面事实);会话流程用 monkeypatch 的假 TCP(size 感知应答)驱动完整
TCP → COTP → S7 协商 → 事务序列,不依赖 snap7。
"""
from __future__ import annotations

import struct
from typing import Any, List

import pytest

from omniplc import SiemensS7Client
from omniplc.plc.siemens import codec
from omniplc.plc.siemens.address import parse_s7_address
from omniplc.plc.siemens.client import _S7Session


# ----------------------------------------------------------------------
# codec 纯函数:黄金帧(参考实现字节锁定)
# ----------------------------------------------------------------------

def test_tpkt_golden() -> None:
    """TPKT:版本 3 + 保留 0 + 总长 u16 大端(含头)。"""
    assert codec.build_tpkt(b"\x02\xf0\x80\x01") == bytes.fromhex("03 00 00 08 02 f0 80 01".replace(" ", ""))


def test_cotp_cr_golden() -> None:
    """COTP CR:TSAP 编码 rack/slot(rack 0 / slot 1 → 远端 0x0101)。

    参数 TLV 各含 code+len 两字节头:Calling(4)+ Called(4)+ PDU Size(3)
    = 11 字节,PDU 长度字节 = 6 + 11 = 0x11(与 python-snap7
    `_build_cotp_cr` 的 `total = 6 + len(parameters)` 一致)。
    """
    frame = codec.build_cotp_cr(0x0101)
    expected = bytes.fromhex(
        "11 e0 00 00 00 01 00 c1 02 01 00 c2 02 01 01 c0 01 0a".replace(" ", "")
    )
    assert frame == expected


def test_remote_tsap_rack_slot_encoding() -> None:
    """远端 TSAP = (PG<<8) | (rack<<5) | slot:0/2 → 0x0102、1/0 → 0x0120。"""
    assert (codec.CONNECTION_TYPE_PG << 8) | (0 << 5) | 2 == 0x0102
    assert (codec.CONNECTION_TYPE_PG << 8) | (1 << 5) | 0 == 0x0120


def test_setup_comm_golden() -> None:
    """协商请求:功能 0xF0 + AMQ 1/1 + PDU 480(参数 8 字节)。"""
    frame = codec.build_setup_comm(480, 1)
    expected = bytes.fromhex(
        "32 01 00 00 00 01 00 08 00 00 f0 00 00 01 00 01 01 e0".replace(" ", "")
    )
    assert frame == expected
    assert codec.parse_setup_comm(
        bytes.fromhex(
            "32 03 00 00 00 01 00 08 00 00 00 00 f0 00 00 01 00 01 01 e0".replace(" ", "")
        ),
        1,
    ) == 480


def test_address_spec_word_golden() -> None:
    """地址规范:DB1.DBB0 起 4 字节(BYTE,bit 地址 = 0×8)。"""
    spec = codec.build_address_spec(codec.AREA_DB, 1, 0, codec.WORD_LEN_BYTE, 4)
    assert spec == bytes.fromhex("12 0a 10 02 00 04 00 01 84 00 00 00".replace(" ", ""))


def test_address_spec_bit_address() -> None:
    """M10.2 位地址编码:bit 地址 = (10 << 3) | 2 = 0x52。"""
    spec = codec.build_address_spec(codec.AREA_MK, 0, (10 << 3) | 2, codec.WORD_LEN_BIT, 1)
    assert spec[9:12] == b"\x00\x00\x52"
    assert spec[6:8] == b"\x00\x00"  # 非 DB 区 DB 号为 0


def test_read_request_golden() -> None:
    """读请求:功能 0x04 + 单 Item,DB1.DBB0 起 4 字节。"""
    frame = codec.build_read(codec.AREA_DB, 1, 0, codec.WORD_LEN_BYTE, 4, 7)
    expected = bytes.fromhex(
        "32 01 00 00 00 07 00 0e 00 00 04 01 12 0a 10 02 00 04 00 01 84 00 00 00".replace(" ", "")
    )
    assert frame == expected


def test_write_request_golden() -> None:
    """写请求:MB10 写 0xAB——数据段传输尺寸 0x04,数据长 = **位数**(1×8)。

    头 data_len = 数据段 5 字节(4 字节项头 + 1 字节数据)。
    """
    frame = codec.build_write(codec.AREA_MK, 0, 10 * 8, codec.WORD_LEN_BYTE, b"\xab", 9)
    expected = bytes.fromhex(
        "32 01 00 00 00 09 00 0e 00 05 05 01 12 0a 10 02 00 01 00 00 83 00 00 50"
        " 00 04 00 08 ab".replace(" ", "")
    )
    assert frame == expected


def test_write_request_real_uses_byte_length() -> None:
    """REAL 写:传输尺寸 0x07,数据长 = 字节数(4)。"""
    frame = codec.build_write(codec.AREA_DB, 1, 6, codec.WORD_LEN_REAL, b"\x42\xc9\x00\x00", 1)
    assert frame[-6:-4] == b"\x00\x04"  # 数据长 u16 = 4(REAL 按字节数)
    assert frame[-4:] == b"\x42\xc9\x00\x00"


def test_multi_read_golden() -> None:
    """multi 读:2 项各按字节跨度编(BYTE,地址 ×8)。

    头布局 `>BBHHHH`:param_len 在偏移 6-7、data_len 在 8-9。
    """
    frame = codec.build_multi_read(
        [(codec.AREA_DB, 1, 0, 2), (codec.AREA_MK, 0, 10, 1)], 3
    )
    assert frame[:2] == b"\x32\x01"
    assert struct.unpack(">H", frame[6:8])[0] == 26  # 参数长 = 2 + 2×12
    assert struct.unpack(">H", frame[8:10])[0] == 0  # 读请求数据长 0
    assert frame[10:12] == b"\x04\x02"
    assert frame[12:24] == bytes.fromhex("12 0a 10 02 00 02 00 01 84 00 00 00".replace(" ", ""))
    assert frame[24:36] == bytes.fromhex("12 0a 10 02 00 01 00 00 83 00 00 50".replace(" ", ""))


def test_multi_read_rejects_over_max() -> None:
    """multi 超 20 项:入参期 ValueError(snap7 MAX_VARS 口径)。"""
    items = [(codec.AREA_DB, 1, 0, 2)] * 21
    with pytest.raises(ValueError):
        codec.build_multi_read(items, 1)


def test_szl_request_golden() -> None:
    """SZL 读请求:USERDATA(group 0x44 / 子功能 0x01)+ ID 0x0424 Index 0。"""
    frame = codec.build_read_szl(0x0424, 0x0000, 5)
    expected = bytes.fromhex(
        "32 07 00 00 00 05 00 08 00 08 00 01 12 04 11 44 01 00 0a 00 00 04 04 24 00 00".replace(" ", "")
    )
    assert frame == expected


def test_parse_read_response_golden() -> None:
    """读应答解析:单项 返回码 FF + 传输尺寸 04 + 位长 0x20 + 4 字节数据。"""
    pdu = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 08 00 00 04 01 ff 04 00 20 3f 80 00 00".replace(" ", "")
    )
    blobs = codec.parse_read_response(pdu, 1, 1)
    assert blobs == [b"\x3f\x80\x00\x00"]


def test_parse_read_response_odd_fill() -> None:
    """multi 奇数长项后 1 字节填充(非末项):步进正确。"""
    data = b"\xff\x04\x00\x08" + b"\x11" + b"\x00" + b"\xff\x04\x00\x08" + b"\x22"
    pdu = (
        struct.pack(">BBHHHHBB", 0x32, 0x03, 0, 2, 2, len(data), 0, 0) + b"\x04\x02" + data
    )
    blobs = codec.parse_read_response(pdu, 2, 2, [1, 1])
    assert blobs == [b"\x11", b"\x22"]


def test_parse_read_entry_error() -> None:
    """条目返回码非 0xFF(地址非法 0x05):DeviceError 不断线语义。"""
    from omniplc.core.errors import DeviceError

    pdu = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 04 00 00 04 01 05 00 00 00".replace(" ", "")
    )
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_read_response(pdu, 1, 1)
    assert exc_info.value.code == 0


def test_parse_response_sequence_mismatch() -> None:
    """序列号回显不符:S7ProtocolError(坏帧拆连)。"""
    pdu = bytes.fromhex(
        "32 03 00 00 00 09 00 02 00 0a 00 00 04 01 ff 04 00 00".replace(" ", "")
    )
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_read_response(pdu, 1, 1)


def test_parse_response_error_class() -> None:
    """error_class 非 0(协议错误):DeviceError 携带组合码。"""
    from omniplc.core.errors import DeviceError

    pdu = bytes.fromhex("32 03 00 00 00 01 00 00 00 00 81 00".replace(" ", ""))
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_s7_response(pdu, 1)
    assert exc_info.value.code == 0x8100


def test_return_code_text() -> None:
    """返回码文本:0xFF 成功、0x05 地址非法、未知码十六进制。"""
    assert codec.return_code_text(0xFF) == "成功"
    assert codec.return_code_text(0x05) == "地址非法"
    assert "0x77" in codec.return_code_text(0x77)


# ----------------------------------------------------------------------
# 会话层:假 TCP(size 感知)驱动完整连接与事务
# ----------------------------------------------------------------------

class _FakeS7Tcp:
    """size 感知假 TCP:按 recv 尺寸从字节池切分;记录全部发送。"""

    def __init__(self, ip_address: str, port: int) -> None:
        self._pool = bytearray()
        self.sent = bytearray()
        self.receive_timeout = 5.0
        self.closed = False

    def queue(self, frames: bytes) -> None:
        self._pool.extend(frames)

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self._pool:
            raise ConnectionError("应答字节池已耗尽")
        chunk = bytes(self._pool[:size])
        del self._pool[:size]
        return chunk


def _negotiate_ack(sequence: int, pdu_size: int = 480) -> bytes:
    """协商应答(ACK_DATA,PDU 尺寸可调)。"""
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, sequence, 8, 0, 0, 0)
    return header + struct.pack(">BBHHH", 0xF0, 0x00, 1, 1, pdu_size)


def _read_ack(
    sequence: int, blobs: List[bytes], wire_bits: "List[int] | None" = None
) -> bytes:
    """读应答:单项/多项(自动加奇数填充)。

    :param wire_bits: 逐项 bit_length 字段(默认 = len(blob)×8;PLC 实际
        应答与请求数一致,wstring 等部分读取场景需显式给请求字节数×8)
    """
    data = b""
    for index, blob in enumerate(blobs):
        bits = wire_bits[index] if wire_bits else len(blob) * 8
        data += b"\xff\x04" + struct.pack(">H", bits) + blob
        if index < len(blobs) - 1 and len(blob) % 2:
            data += b"\x00"
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, sequence, 2, len(data), 0, 0)
    return header + b"\x04\x02" + data


def _write_ack(sequence: int, count: int = 1) -> bytes:
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, sequence, 2, count, 0, 0)
    return header + b"\x05\x01" + b"\xff" * count


def _szl_ack(sequence: int, entries: bytes) -> bytes:
    data = b"\xff\x04" + struct.pack(">H", len(entries) + 8) + b"\x04\x24\x00\x00\x00\x02\x00\x01" + entries
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0, sequence, 8, len(data))
    param = struct.pack(">BBBBBBBB", 0x00, 0x01, 0x12, 0x04, 0x12, 0x44, 0x01, 0x00)
    return header + param + data


def _tpkt(payload: bytes) -> bytes:
    """TPKT 封帧(测试 helper)。"""
    return struct.pack(">BBH", 3, 0, len(payload) + 4) + payload


def _dt(pdu: bytes) -> bytes:
    """COTP DT 包裹(02 F0 80 + S7 PDU,测试 helper)。"""
    return b"\x02\xf0\x80" + pdu


def _cotp_cc() -> bytes:
    """最小 COTP CC(7 字节头:len 6 + 0xD0 + 引用 + 类别)。"""
    return b"\x06\xd0\x00\x0a\x00\x01\x00"


@pytest.fixture()
def s7(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造挂假 TCP 的客户端(未连接);返回 (client, fake_tcp)。"""
    fake: Any = _FakeS7Tcp("127.0.0.1", 102)
    monkeypatch.setattr("omniplc.plc.siemens.client.TcpTransport", lambda ip, port: fake)
    client = SiemensS7Client("127.0.0.1", rack=0, slot=1)
    return client, fake


def _connect_ready(client: Any, fake: Any, pdu_size: int = 480, next_seq: int = 1) -> None:
    """喂 CC + 协商应答并完成连接(后续事务序列号从 next_seq 起)。"""
    fake.queue(_tpkt(_cotp_cc()))
    fake.queue(_tpkt(_dt(_negotiate_ack(1, pdu_size))))
    assert client.connect() is True, client.last_error


def test_connect_handshake_and_pdu(s7: Any) -> None:
    """连接三步:CR 帧 + CC + 协商,PDU 以对端确认为准。"""
    client, fake = s7
    _connect_ready(client, fake, pdu_size=240)
    assert client.connected is True
    # CR 帧:COTP 类型 0xE0 + 远端 TSAP 0x0101(rack0 slot1)
    assert b"\xc2\x02\x01\x01" in fake.sent
    # 协商请求:功能 0xF0
    assert b"\xf0\x00\x00\x01\x00\x01\x01\xe0" in fake.sent
    assert client._transport.pdu_size == 240  # type: ignore[attr-defined]


def test_read_float_roundtrip(s7: Any) -> None:
    """DB1.DBD6 读 FLOAT:请求帧字节断言 + 大端解码。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00"]))))
    ok, value = client.read_float("DB1.DBD6")
    assert (ok, value) == (True, 100.5)
    # 读请求:功能 04 + 单 Item(BYTE,4 字节,DB1,区 0x84,地址 6×8=48)
    expected_request = bytes.fromhex(
        "32 01 00 00 00 02 00 0e 00 00 04 01 12 0a 10 02 00 04 00 01 84 00 00 30".replace(" ", "")
    )
    assert expected_request in fake.sent


def test_bit_write_rmw(s7: Any) -> None:
    """DB1.DBX0.3 写 True:RMW 两事务(读整字节 → 置位写回)。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x00"]))))
    fake.queue(_tpkt(_dt(_write_ack(3))))
    assert client.write_bool("DB1.DBX0.3", True) is True
    # 写帧:RMW 写整字节(地址 = 字节地址 0 ×8 = 0);数据 0x08(bit3 置位)
    expected_write = bytes.fromhex(
        "32 01 00 00 00 03 00 0e 00 05 05 01 12 0a 10 02 00 01 00 01 84 00 00 00"
        " 00 04 00 08 08".replace(" ", "")
    )
    assert expected_write in fake.sent


def test_string_roundtrip(s7: Any) -> None:
    """S7 String:读(声明长/实际长头)+ 写(保留声明长)。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x20\x05" + b"hello".ljust(30, b"\x00")]))))
    ok, text = client.read_string("DB1.DBS20", length=30)
    assert (ok, text) == (True, "hello")
    fake.queue(_tpkt(_dt(_read_ack(3, [b"\x20"]))))
    fake.queue(_tpkt(_dt(_write_ack(4))))
    assert client.write_string("DB1.DBS20", "world") is True
    # 写帧数据 = 声明长(0x20 保留)+ 实际长(5)+ world
    assert bytes([0x20, 5]) + b"world" in fake.sent


def test_wstring_roundtrip(s7: Any) -> None:
    """WString:UTF-16BE 读写(声明长 2 字节 + 实际长 2 字节)。"""
    client, fake = s7
    _connect_ready(client, fake)
    body = "温度".encode("utf-16-be")
    fake.queue(_tpkt(_dt(_read_ack(2, [(2).to_bytes(2, "big") + (2).to_bytes(2, "big") + body + b"\x00" * 12], [160]))))
    ok, text = client.read_wstring("DB1.DBW40", length=8)
    assert (ok, text) == (True, "温度")
    fake.queue(_tpkt(_dt(_read_ack(3, [(8).to_bytes(2, "big") + b"\x00\x00"], [16]))))
    fake.queue(_tpkt(_dt(_write_ack(4))))
    assert client.write_wstring("DB1.DBW40", "温度") is True


def test_read_batch_multi_with_fill(s7: Any) -> None:
    """read_batch 2 项(multi + 奇数填充):DB 字 + M 位。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x00\x05", b"\x08"]))))
    ok, values = client.read_batch([("DB1.DBW0", "ushort"), ("M0.3", "bool")])
    assert (ok, values) == (True, [5, True])
    assert b"\x00\x01\x84\x00\x00\x00" in fake.sent  # DB 项
    assert b"\x00\x00\x83\x00\x00\x00" in fake.sent  # M0 项(bit 地址 0)


def test_get_cpu_state_run_and_ping(s7: Any) -> None:
    """CPU 状态 SZL:0x08 → Run;ping 走同命令。"""
    client, fake = s7
    _connect_ready(client, fake)
    assert client.ping_supported is True
    fake.queue(_tpkt(_dt(_szl_ack(2, b"\x08\x00"))))
    ok, state = client.get_cpu_state()
    assert (ok, state) == (True, "S7CpuStatusRun")
    fake.queue(_tpkt(_dt(_szl_ack(3, b"\x04\x00"))))
    ok, state = client.get_cpu_state()
    assert (ok, state) == (True, "S7CpuStatusStop")
    fake.queue(_tpkt(_dt(_szl_ack(4, b"\x08\x00"))))
    assert client.ping() is True


def test_device_error_keeps_connection(s7: Any) -> None:
    """条目返回码 0x05(地址非法):DeviceError 不断线。"""
    client, fake = s7
    _connect_ready(client, fake)
    bad = bytes.fromhex(
        "32 03 00 00 00 02 00 02 00 04 00 00 04 01 05 00 00 00".replace(" ", "")
    )
    fake.queue(_tpkt(_dt(bad)))
    assert client.read_float("DB1.DBD6") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "S7 读条目" in client.last_error
    assert client.last_error_category.name == "DEVICE"


def test_link_down_lazy_reconnect(s7: Any) -> None:
    """半开(对端关闭):拆连,下次事务惰性重连并重走握手。"""
    client, fake = s7
    client.reconnect_backoff = False  # 重连即刻,不测退避
    _connect_ready(client, fake)
    # 字节池耗尽 → 假 TCP 抛 ConnectionError(∈ OSError)→ 拆连
    assert client.read_float("DB1.DBD6") == (False, None)
    assert client.connected is False
    # 恢复应答后自动重连(重走握手:协商 seq 重新从 1 起)
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00"]))))
    ok, value = client.read_float("DB1.DBD6")
    assert (ok, value) == (True, 100.5)


def test_receive_timeout_flows_to_tcp(s7: Any) -> None:
    """receive_timeout:连接时下发初值,运行期修改经基类 setter 即时传播。"""
    client, fake = s7
    client.receive_timeout = 2.5
    _connect_ready(client, fake)
    assert fake.receive_timeout == 2.5
    client.receive_timeout = 3.0
    # 基类 setter 即时传播到当前传输(热下发,与 TCP 走线同口径)
    assert fake.receive_timeout == 3.0


def test_constructor_validation() -> None:
    """构造校验:非法 IP/端口/机架/槽位;dll_path 已移除(TypeError)。"""
    with pytest.raises(ValueError):
        SiemensS7Client("", 102, 0, 1)
    with pytest.raises(ValueError):
        SiemensS7Client("192.168.0.1", 99999, 0, 1)
    with pytest.raises(ValueError):
        SiemensS7Client("192.168.0.1", 102, 8, 1)
    with pytest.raises(ValueError):
        SiemensS7Client("192.168.0.1", 102, 0, 32)
    # v0.53 破坏性变更:dll_path 随 python-snap7 退役移除
    with pytest.raises(TypeError):
        SiemensS7Client("192.168.0.1", 102, 0, 1, "")  # type: ignore[misc]


def test_signature_defaults() -> None:
    """构造默认:ip 192.168.0.1 / 102 / rack 0 / slot 1(冻结面)。"""
    client = SiemensS7Client()
    assert client._ip_address == "192.168.0.1"
    assert client._port == 102
    assert client.rack == 0
    assert client.slot == 1
    assert client.ping_supported is True


def test_address_parse_golden() -> None:
    """地址解析:DB 位/字节起点与 I/Q/M 记号(语义与旧封装一致)。"""
    parsed = parse_s7_address("DB1.DBX0.3")
    assert (parsed.area, parsed.db_number, parsed.byte_index, parsed.bit) == ("DB", 1, 0, 3)
    parsed = parse_s7_address("MW10")
    assert (parsed.area, parsed.byte_index, parsed.bit) == ("M", 10, None)
    parsed = parse_s7_address("I0.0")
    assert (parsed.area, parsed.byte_index, parsed.bit) == ("I", 0, 0)


def test_session_send_recv_rejects() -> None:
    """_S7Session 无字节流收发(send/recv 显式拒绝)。"""
    session = _S7Session("127.0.0.1", 0, 1, 102)
    from omniplc.core.errors import TransportClosedError

    with pytest.raises(TransportClosedError):
        session.send(b"\x01")
    with pytest.raises(TransportClosedError):
        session.recv(1)


def test_async_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """aio 镜像:单工作线程完成握手 + 读往返(构造签名去 dll_path)。"""
    import asyncio

    from omniplc.aio import ASiemensS7Client

    fake: Any = _FakeS7Tcp("127.0.0.1", 102)
    monkeypatch.setattr("omniplc.plc.siemens.client.TcpTransport", lambda ip, port: fake)

    async def scenario() -> None:
        client = ASiemensS7Client("127.0.0.1", 102, rack=0, slot=1)
        assert client.rack == 0 and client.slot == 1
        fake.queue(_tpkt(_cotp_cc()))
        fake.queue(_tpkt(_dt(_negotiate_ack(1))))
        assert await client.connect() is True
        fake.queue(_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00"]))))
        ok, value = await client.read_float("DB1.DBD6")
        assert (ok, value) == (True, 100.5)
        await client.close()

    asyncio.run(scenario())
