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
    """REAL 写:传输尺寸 0x07,数据长 = 字节数(4);地址 = 字节×8(review-1008 P3 修正脚手架错帧)。"""
    frame = codec.build_write(codec.AREA_DB, 1, 6 * 8, codec.WORD_LEN_REAL, b"\x42\xc9\x00\x00", 1)
    assert frame[-6:-4] == b"\x00\x04"  # 数据长 u16 = 4(REAL 按字节数)
    assert frame[-4:] == b"\x42\xc9\x00\x00"
    assert frame[21:24] == b"\x00\x00\x30"  # 位地址 48 = 0x30


def test_write_request_multi_byte_golden() -> None:
    """多字节写:地址规范 count = 数据长 // 元素宽(review-1008 P0-1,曾恒 1)。

    DB1.DBD6 写 4 字节:参数区 count=0x0004、数据段长 0x0020 位 +
    4 字节数据,与 python-snap7 3.2.0 `build_write_request` 逐字节一致
    (count 与数据段长度必须自洽,否则真机按条目返回码拒绝)。
    """
    frame = codec.build_write(
        codec.AREA_DB, 1, 6 * 8, codec.WORD_LEN_BYTE, b"\x01\x02\x03\x04", 9
    )
    expected = bytes.fromhex(
        "32 01 00 00 00 09 00 0e 00 08 05 01 12 0a 10 02 00 04 00 01 84 00 00 30"
        " 00 04 00 20 01 02 03 04".replace(" ", "")
    )
    assert frame == expected


def test_write_request_rejects_ungolled_data() -> None:
    """写数据长非元素宽整数倍:入参期 ValueError(参考 L233-234 同款)。"""
    with pytest.raises(ValueError):
        codec.build_write(codec.AREA_DB, 1, 0, codec.WORD_LEN_WORD, b"\x01\x02\x03", 1)


def test_build_address_spec_bit_overflow() -> None:
    """位地址超 3 字节字段:ValueError(旧实现 pack 截断静默别名,review-1008 P1)。"""
    with pytest.raises(ValueError):
        codec.build_address_spec(codec.AREA_DB, 1, 0x1000000, codec.WORD_LEN_BYTE, 1)
    # 边界:0xFFFFFF(字节起点 2097151 ×8)恰好占满 3 字节,合法
    spec = codec.build_address_spec(codec.AREA_DB, 1, 0xFFFFFF, codec.WORD_LEN_BYTE, 1)
    assert spec[9:12] == b"\xff\xff\xff"


def test_byte_index_limit_address() -> None:
    """字节起点上限 = 2^21−1(线上 3 字节是位地址,字节×8 恰占满)。"""
    parsed = parse_s7_address("DB1.DBB2097151")
    assert parsed.byte_index == 2097151
    with pytest.raises(ValueError):
        parse_s7_address("DB1.DBB2097152")


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


def test_szl_response_echo_mismatch() -> None:
    """SZL 应答 ID/Index 回显校验(review-1008 P3:旧实现不比对)。"""
    pdu = _szl_ack(1, b"\x08\x00")
    assert codec.parse_szl_response(pdu, 1, 0x0424, 0x0000) == b"\x08\x00"
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_szl_response(pdu, 1, szl_id=0x0425)
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_szl_response(pdu, 1, szl_index=1)


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
    """条目返回码非 0xFF(地址非法 0x05):DeviceError 携带返回码不断线。"""
    from omniplc.core.errors import DeviceError

    pdu = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 04 00 00 04 01 05 00 00 00".replace(" ", "")
    )
    with pytest.raises(DeviceError) as exc_info:
        codec.parse_read_response(pdu, 1, 1)
    assert exc_info.value.code == 0x05


def test_parse_read_response_length_mismatch() -> None:
    """线上声明长度与请求期望不符:S7ProtocolError(review-1008 P2)。

    旧实现按期望覆盖线上推导,短回时把填充/下一项头切进数据
    (先产出污染数据才失败)——改为交叉校验直接坏帧。
    """
    pdu = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 05 00 00 04 01 ff 04 00 08 11".replace(" ", "")
    )
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_read_response(pdu, 1, 1, [4])


def test_parse_read_response_u16_count_guard() -> None:
    """读/写元素数超 u16:入参期 ValueError(防裸 struct.error 穿透)。"""
    with pytest.raises(ValueError):
        codec.build_read(codec.AREA_DB, 1, 0, codec.WORD_LEN_BYTE, 65536, 1)
    with pytest.raises(ValueError):
        codec.build_write(
            codec.AREA_DB, 1, 0, codec.WORD_LEN_BYTE, b"\x00" * 65536, 1
        )


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
    """读应答:单项/多项(自动加奇数填充;参数区条目数按实际项数)。

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
    # 参数区 = 功能码 0x04 + 实际条目数(review-1008 P2:真机回显请求数,
    # 曾硬编码 02 掩盖「应答项数与请求不符」异常形态)
    return header + b"\x04" + bytes([len(blobs)]) + data


def _write_ack(sequence: int, count: int = 1) -> bytes:
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, sequence, 2, count, 0, 0)
    return header + b"\x05\x01" + b"\xff" * count


def _szl_ack(sequence: int, entries: bytes) -> bytes:
    """SZL 应答(参考桩真机形态):USERDATA 应答参数 12 字节 + 传输尺寸 0x09。

    参数区布局(python-snap7 3.2.0 `_parse_userdata_response_params`
    L1663-1678 / server 桩 L2193-2208):[3]=0x08 响应长、[4]=0x12、
    [5]=0x84(响应位 0x8|SZL 组 0x4)、[6]=0x01 子功能、[10:12]=参数级
    错误码(0)——review-1008 P2:曾按请求形态 8 字节伪造且传输尺寸 0x04。
    """
    data = b"\xff\x09" + struct.pack(">H", len(entries) + 8) + b"\x04\x24\x00\x00\x00\x02\x00\x01" + entries
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0, sequence, 12, len(data))
    param = struct.pack(">BBBBBBBBBBBB", 0x00, 0x01, 0x12, 0x08, 0x12, 0x84, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00)
    return header + param + data


def _szl_cpu_record(bzu_id: int) -> bytes:
    """0x0424 标准记录 20 字节:bereig(2)+ae(1)+bzu_id(1)+res(4)+anlinfo(4)+time(8)。

    bzu_id 在记录区 [3](snap7 C `opGetPlcStatus` L2038 读 opData[7],
    opData = AddLen/AddCount + 记录区,即记录区 [3])——review-1008 P1
    之前 fixture 按「记录首字节=状态」自造帧,固化错误偏移。
    """
    return b"\x00\x00\x00" + bytes([bzu_id]) + b"\x00" * 16


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
    """CPU 状态 SZL:bzu_id(记录区[3])0x08 → Run / 0x04 → Stop;ping 同命令。"""
    client, fake = s7
    _connect_ready(client, fake)
    assert client.ping_supported is True
    fake.queue(_tpkt(_dt(_szl_ack(2, _szl_cpu_record(0x08)))))
    ok, state = client.get_cpu_state()
    assert (ok, state) == (True, "S7CpuStatusRun")
    fake.queue(_tpkt(_dt(_szl_ack(3, _szl_cpu_record(0x04)))))
    ok, state = client.get_cpu_state()
    assert (ok, state) == (True, "S7CpuStatusStop")
    fake.queue(_tpkt(_dt(_szl_ack(4, _szl_cpu_record(0x08)))))
    assert client.ping() is True


def test_get_cpu_state_unknown_status(s7: Any) -> None:
    """bzu_id 非已知值:Unknown 兜底(不照搬 C 的「未知一律 STOP」)。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_szl_ack(2, _szl_cpu_record(0x77)))))
    ok, state = client.get_cpu_state()
    assert (ok, state) == (True, "S7CpuStatusUnknown")


def test_szl_param_level_error(s7: Any) -> None:
    """USERDATA 参数级错误码(0x8104):DeviceError(与数据段返回码双通道)。"""
    client, fake = s7
    _connect_ready(client, fake)
    data = b"\xff\x09" + struct.pack(">H", 8) + b"\x04\x24\x00\x00\x00\x02\x00\x01"
    header = struct.pack(">BBHHHH", 0x32, 0x07, 0, 2, 12, len(data))
    param = struct.pack(">BBBBBBBBBBBB", 0x00, 0x01, 0x12, 0x08, 0x12, 0x84, 0x01, 0x00, 0x00, 0x00, 0x81, 0x04)
    fake.queue(_tpkt(_dt(header + param + data)))
    ok, state = client.get_cpu_state()
    assert ok is False
    assert client.last_error is not None and "参数级错误码 0x8104" in client.last_error


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


def test_connect_timeout_flows_to_tcp(s7: Any) -> None:
    """connect_timeout:建会话时下发到底层 TCP 传输(review-1008 P1,曾漏)。"""
    client, fake = s7
    client.connect_timeout = 2.0
    _connect_ready(client, fake)
    assert fake.connect_timeout == 2.0


def test_close_dr_carries_dst_ref(s7: Any) -> None:
    """COTP DR 的 dst_ref = CC 应答回显值(_cotp_cc 固定 0x000A)。"""
    client, fake = s7
    _connect_ready(client, fake)
    assert client.disconnect() is True
    # DR 帧:LI 6 + 0x80 + dst_ref 0x000A + src_ref 0x0001 + class 0
    assert b"\x06\x80\x00\x0a\x00\x01\x00\x00" in fake.sent


def test_read_area_auto_split(s7: Any) -> None:
    """读跨 PDU 自动分片(pdu=20 → 片容量 2):5 字节 = 2+2+1 三事务。

    与 python-snap7 read_area 自动分片行为对齐(review-1008 P2:旧实现
    单事务硬发,超 PDU 被 PLC 拒绝)。
    """
    client, fake = s7
    _connect_ready(client, fake, pdu_size=20)
    session = client._transport
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x11\x22"]))))
    fake.queue(_tpkt(_dt(_read_ack(3, [b"\x33\x44"]))))
    fake.queue(_tpkt(_dt(_read_ack(4, [b"\x55"]))))
    data = session.read_area(codec.AREA_DB, 1, 0, 5)
    assert data == b"\x11\x22\x33\x44\x55"
    assert fake.sent.count(b"\x04\x01") == 3  # 三片各发一次单 Item 读
    # 第三片:count=1、字节起点 4(位地址 0x20)
    assert bytes.fromhex("0401120a10020001000184000020") in fake.sent


def test_write_area_auto_split(s7: Any) -> None:
    """写跨 PDU 自动分片(pdu=60 → 片容量 25):30 字节 = 25+5 两事务。"""
    client, fake = s7
    _connect_ready(client, fake, pdu_size=60)
    session = client._transport
    fake.queue(_tpkt(_dt(_write_ack(2))))
    fake.queue(_tpkt(_dt(_write_ack(3))))
    session.write_area(codec.AREA_DB, 1, 0, bytes(range(30)))
    assert fake.sent.count(b"\x05\x01") == 2
    # 第一片:count=25(0x0019);第二片:count=5、字节起点 25(位地址 0xC8)
    assert bytes.fromhex("0501120a10020019000184000000") in fake.sent
    assert bytes.fromhex("0501120a100200050001840000c8") in fake.sent


def test_get_cpu_state_empty_records(s7: Any) -> None:
    """SZL 应答零记录:拆连重同步(公开面 (False, None)),不再裸 IndexError。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_szl_ack(2, b""))))
    ok, state = client.get_cpu_state()
    assert ok is False
    assert client.last_error is not None and "无记录" in client.last_error


def test_connect_refused(s7: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 拒连:connect() False + last_error(review-1008 C 路 P1 覆盖)。"""
    client, _fake = s7
    monkeypatch.setattr(
        "omniplc.plc.siemens.client.TcpTransport",
        lambda ip, port: _RaiseOnConnect(),
    )
    assert client.connect() is False
    assert client.connected is False
    assert client.last_error is not None


class _RaiseOnConnect:
    """connect() 即抛的假传输(拒连/超时形态)。"""

    def __init__(self) -> None:
        self.closed = False
        self.receive_timeout: Any = None

    def connect(self) -> None:
        raise ConnectionRefusedError("connection refused")

    def close(self) -> None:
        self.closed = True

    def send(self, data: bytes) -> None:
        raise ConnectionRefusedError("connection refused")

    def recv(self, size: int) -> bytes:
        raise ConnectionRefusedError("connection refused")


def test_connect_bad_cc_frame(s7: Any) -> None:
    """CC 帧类型非法:握手失败包装为 OSError(断连语义,基类重试)。"""
    client, fake = s7
    fake.queue(_tpkt(b"\x06\xe0\x00\x0a\x00\x01\x00"))  # 误发 CR 形态 0xE0
    assert client.connect() is False
    assert client.last_error is not None and "握手失败" in client.last_error
    assert client.connected is False


def test_parse_read_response_function_mismatch() -> None:
    """读应答功能码/条目数不符:S7ProtocolError(review-1008 P1,跨功能应答曾被误收)。"""
    # 功能码 0x05(写应答)喂读解析;条目数 3 与请求 1 不符
    bad_func = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 04 00 00 05 01 ff 00 00 00".replace(" ", "")
    )
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_read_response(bad_func, 1, 1)
    bad_count = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 04 00 00 04 03 ff 00 00 00".replace(" ", "")
    )
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_read_response(bad_count, 1, 1)


def test_parse_write_response_rejects_extra_bytes() -> None:
    """写应答数据段多字节:坏帧(review-1008 P2,参考 check_write_response 恰 1 字节)。"""
    ack = bytes.fromhex(
        "32 03 00 00 00 01 00 02 00 02 00 00 05 01 ff ff".replace(" ", "")
    )
    with pytest.raises(codec.S7ProtocolError):
        codec.parse_write_response(ack, 1, 1)


def test_read_range_float_multi(s7: Any) -> None:
    """read_range:FLOAT×2 连续读(单 Item 8 字节事务),切片解码正确(C 路 P1 覆盖)。"""
    client, fake = s7
    _connect_ready(client, fake)
    fake.queue(_tpkt(_dt(_read_ack(2, [b"\x42\xc9\x00\x00\x3f\x80\x00\x00"]))))
    ok, values = client.read_range("DB1.DBD0", 2, "FLOAT")
    assert ok is True
    assert values == [100.5, 1.0]


def test_read_range_rejects_string_and_bit(s7: Any) -> None:
    """read_range:STRING 与位地址入参期 ValueError(docstring :raises 口径)。"""
    client, _fake = s7
    with pytest.raises(ValueError):
        client.read_range("DB1.DBS0", 2, "STRING")
    with pytest.raises(ValueError):
        client.read_range("DB1.DBX0.3", 2, "SHORT")


def test_read_batch_over_max_items(s7: Any) -> None:
    """read_batch 超 20 条:入参期 ValueError(client 层上限,review-1008 C 路)。"""
    client, _fake = s7
    with pytest.raises(ValueError):
        client.read_batch([("DB1.DBB0", "SHORT")] * 21)


def test_read_batch_item_error_whole_batch(s7: Any) -> None:
    """multi 中一项返回码 0x05:整批 (False, None) 且连接保持(docstring 契约)。"""
    client, fake = s7
    _connect_ready(client, fake)
    # 两项各 2 字节(SHORT):项 1 正常、项 2 返回码 0x05(均偶长无填充)
    data = b"\xff\x04\x00\x10\x00\x11" + b"\x05\x04\x00\x10\x00\x00"
    header = struct.pack(">BBHHHHBB", 0x32, 0x03, 0, 2, 2, len(data), 0, 0)
    fake.queue(_tpkt(_dt(header + b"\x04\x02" + data)))
    results = client.read_batch([("DB1.DBB0", "SHORT"), ("DB1.DBB2", "SHORT")])
    assert results == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "S7 读条目 1" in client.last_error
    assert client.last_error_code == 0x05


def test_write_plc_reject_carries_code(s7: Any) -> None:
    """写应答条目 0x07(类型不一致):DeviceError 携带返回码。"""
    client, fake = s7
    _connect_ready(client, fake)
    ack = bytes.fromhex(
        "32 03 00 00 00 02 00 02 00 01 00 00 05 01 07".replace(" ", "")
    )
    fake.queue(_tpkt(_dt(ack)))
    assert client.write_float("DB1.DBD0", 1.5) is False
    assert client.last_error_code == 0x07
    # DB 区拒绝附优化块提示(review-1008 P2 行为回归恢复)
    assert "Optimized block access" in client.last_error


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
