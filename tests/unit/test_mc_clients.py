"""MC 客户端帧收发测试:脚本化传输验证 TCP/UDP × 3E/4E/1E 全链路。

覆盖:组帧逐字节断言、按长收包、序列号校验、结束码 DeviceError 不断线、
坏帧断线重连、寄存器位"读-改-写"、多块批量读(read_batch/read_many 覆写)。
"""
from __future__ import annotations

import asyncio
import struct

import pytest

from omniplc import MelsecMcSerialClient, MelsecMcTcpClient, MelsecMcUdpClient
from omniplc.aio import AMelsecMcTcpClient
from omniplc.core.constants import MC_DEFAULT_MONITOR_TIMER, MC_DEFAULT_PC_NUMBER
from omniplc.plc.melsec import codec_a, codec_qna
from omniplc.plc.melsec.address import parse_mc_address
from omniplc.plc.melsec.melsec import _MC_DEVICE_CODES_FX5U_XY
from omniplc.core.types import DataType
from scripted import ScriptedTransport, mount_real_tcp


def _qna_read_response(values: list, frame: str = "3E", serial: int = 0, end_code: int = 0) -> bytes:
    """构造 QnA 读响应(测试脚手架)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    if frame == "4E":
        head = b"\xd4\x00" + serial.to_bytes(2, "little") + b"\x00\x00" + b"\x00\xff\xff\x03\x00"
    else:
        head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + end_code.to_bytes(2, "little") + data


def _qna_write_response(frame: str = "3E", serial: int = 0) -> bytes:
    """构造 QnA 写响应(测试脚手架)。"""
    return _qna_read_response([], frame=frame, serial=serial)


def _one_e_read_response(values: list) -> bytes:
    """构造 1E 字读响应(测试脚手架)。"""
    data = b"".join(value.to_bytes(2, "little") for value in values)
    return bytes([0x81, 0x00]) + data


def _one_e_write_response() -> bytes:
    """构造 1E 字写响应(测试脚手架)。"""
    return bytes([0x83, 0x00])


def _one_e_bit_write_response() -> bytes:
    """构造 1E 位写响应(测试脚手架;副头部 = 位写 0x02 + 0x80)。"""
    return bytes([0x82, 0x00])


def _mount(monkeypatch: pytest.MonkeyPatch, client: object, scripted: ScriptedTransport) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_tcp_3e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:请求组帧正确,响应解析出字数据。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([20])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_4e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 4E:13 字节头分段收包,序列号回包校验。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="4E")
    frame = _qna_read_response([20], frame="4E", serial=1)
    scripted = ScriptedTransport([frame[:13], frame[13:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(scripted.sent) == codec_qna.build_request(
        "4E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_1e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:2 字节头 + 按点数收数据。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    frame = _one_e_read_response([20])
    scripted = ScriptedTransport([frame[:2], frame[2:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(scripted.sent) == codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def test_tcp_3e_read_float_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:float32 小端字序往返(回归:解码不得再做字节交换)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    raw = struct.pack("<f", 3.14)
    words = list(struct.unpack("<HH", raw))
    frame = _qna_read_response(words)
    _mount(monkeypatch, client, ScriptedTransport([frame[:9], frame[9:]]))
    client.connect()
    ok, value = client.read_float("D100")
    assert ok is True and value is not None and abs(value - 3.14) < 1e-6


def test_tcp_3e_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:字写请求与回包校验。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    response = _qna_write_response()
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"),
        1, False, True, [300],
    )


def test_tcp_1e_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:写响应仅 2 字节(副头部+结束码)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    scripted = ScriptedTransport([_one_e_write_response()])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_short("D100", 300) is True
    assert bytes(scripted.sent) == codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, True, [300]
    )


def test_tcp_1e_bit_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:位写响应副头部为 0x82(回归:不得按字写的 0x83 校验)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    scripted = ScriptedTransport([_one_e_bit_write_response()])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("M0", True) is True
    assert bytes(scripted.sent) == codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M0"), 1, True, True, [1]
    )


def test_tcp_1e_bit_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 1E:位读响应副头部 0x80 + 半字节位数据(1 点收 1 字节)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    frame = bytes([0x80, 0x00, 0x10])  # M0 = ON(高半字节)
    scripted = ScriptedTransport([frame[:2], frame[2:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M0") == (True, True)
    assert bytes(scripted.sent) == codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M0"), 1, True, False
    )


def test_tcp_3e_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:结束代码非 0 → DeviceError,不断线。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([0], end_code=0xC059)
    _mount(monkeypatch, client, ScriptedTransport([frame[:9], frame[9:]]))
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "结束代码 0xC059" in client.last_error


def test_tcp_bad_subheader_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:响应副头部非法按坏帧处理,标记断开。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    bad = b"\x00\x00" + _qna_read_response([20])[2:]
    _mount(monkeypatch, client, ScriptedTransport([bad[:9], bad[9:]]))
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "副头部" in client.last_error


def test_tcp_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E:字软元件位写 = 读(FC0104)→改位→写(0114)两段事务。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    read_frame = _qna_read_response([0x0004])
    write_frame = _qna_write_response()
    scripted = ScriptedTransport(
        [read_frame[:9], read_frame[9:], write_frame[:9], write_frame[9:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    expected = codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    ) + codec_qna.build_request(
        "3E", 2, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"),
        1, False, True, [0x000C],
    )
    assert bytes(scripted.sent) == expected


def test_udp_3e_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP 3E:一问一答一数据报,整包解析。"""
    client = MelsecMcUdpClient("127.0.0.1", 2000)
    frame = _qna_read_response([20])
    scripted = ScriptedTransport([frame], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 1, False, False
    )


def _qna_random_read_response(words: list, bits: list) -> bytes:
    """构造多块批量读响应(测试脚手架):字块逐字小端 + 位块逐点 16 位字。"""
    data = b"".join(value.to_bytes(2, "little") for value in words)
    data += b"".join(value.to_bytes(2, "little") for value in bits)
    head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data


def test_tcp_3e_read_batch_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E read_batch:混类型单事务,字块/位块分节,值与 items 顺序对应。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_random_read_response([0xFFFE, 0x0000, 0x3F80, 0x0008], [0x0001, 0x0000])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_batch(
        [
            ("D100", DataType.SHORT),
            ("D102", DataType.FLOAT),
            ("D110.3", DataType.BOOL),
            ("M10", DataType.BOOL),
            ("X20", DataType.BOOL),
        ]
    )
    assert ok is True and values == [-2, 1.0, True, True, False]
    assert bytes(scripted.sent) == codec_qna.build_random_read(
        "3E",
        1,
        0,
        0xFF,
        MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, 100, 1), (0xA8, 102, 2), (0xA8, 110, 1)],
        [(0x90, 10, 1), (0x9C, 0x20, 1)],
    )


def test_tcp_3e_read_many_single_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E read_many:覆写为 0406 单事务(协议原生批量合并)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_random_read_response([7, 9], [])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_many(["D0", "D2"], "short") == [(True, 7), (True, 9)]
    assert bytes(scripted.sent) == codec_qna.build_random_read(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, [(0xA8, 0, 1), (0xA8, 2, 1)], []
    )


def test_read_batch_frame_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """1E 帧:read_batch 拒绝;read_many 回退基类逐点独立事务。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.read_batch([("D0", "short")])
    frame_a = _one_e_read_response([5])
    frame_b = _one_e_read_response([6])
    fallback = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    _mount(
        monkeypatch,
        fallback,
        ScriptedTransport(
            [frame_a[:2], frame_a[2:], frame_b[:2], frame_b[2:]]
        ),
    )
    fallback.connect()
    assert fallback.read_many(["D0", "D1"], "short") == [(True, 5), (True, 6)]


def test_read_batch_empty_rejected() -> None:
    """read_batch 空列表:参数错误直接抛出。"""
    with pytest.raises(ValueError):
        MelsecMcTcpClient("127.0.0.1", 2000).read_batch([])


# ----------------------------------------------------------------------
# 随机读/写(0403/1402)与 CPU 型号(0101)全链路
# ----------------------------------------------------------------------


def test_tcp_3e_random_read_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E random_read:乱序不连续软元件单事务读取(字 + 双字)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    # 响应数据布局:字数据(D100=5, M0 位字=1)在前,双字数据(D500=0x12345678)在后
    data = (
        (0x0005).to_bytes(2, "little")                      # D100 字
        + (0x0001).to_bytes(2, "little")                    # M0 位字
        + (0x12345678).to_bytes(4, "little")                # D500 双字
    )
    frame = b"\xd0\x00" + b"\x00\xff\xff\x03\x00" + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.random_read(
        [("D100", DataType.SHORT), ("M0", DataType.BOOL)],
        [("D500", DataType.UINT)],
    )
    assert ok is True and values == [5, True, 0x12345678]
    assert bytes(scripted.sent) == codec_qna.build_random_read_devices(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, 100), (0x90, 0)],
        [(0xA8, 500)],
    )


def test_tcp_3e_random_read_frame_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """1E 帧 random_read:直接拒绝(0403 仅 QnA 兼容帧)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.random_read([("D0", "short")])


def test_tcp_3e_random_read_empty_rejected() -> None:
    """random_read 空列表:参数错误直接抛出。"""
    with pytest.raises(ValueError):
        MelsecMcTcpClient("127.0.0.1", 2000).random_read([])


def test_tcp_3e_random_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E random_write:乱序写(无响应数据,应答头即成功)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = b"\xd0\x00" + b"\x00\xff\xff\x03\x00" + (2).to_bytes(2, "little") + b"\x00\x00"
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.random_write([("D100", 0x1234)], [("D500", 0x89ABCDEF)]) is True
    assert bytes(scripted.sent) == codec_qna.build_random_write_devices(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        [(0xA8, 100, 0x1234)],
        [(0xA8, 500, 0x89ABCDEF)],
    )


def test_tcp_3e_random_write_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """random_write:双列表均空拒绝;负值拒绝(不触碰连接)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.random_write([])
    with pytest.raises(ValueError):
        client.random_write([("D100", -5)])
    assert client.connected is True


def test_tcp_3e_random_read_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """random_read 入参校验:字软元件位号后缀 / 位软元件带位号 / 32 位入门字列表
    均入参期拒绝(0403 字访问 1 字/点,SH-080008 §8.3)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.random_read([("D100.3", DataType.SHORT)])   # 字软元件位号后缀静默丢位(原 P0)
    with pytest.raises(ValueError):
        client.random_read([("M100.3", DataType.BOOL)])    # 位软元件不带位号
    with pytest.raises(ValueError):
        client.random_read([("D100", DataType.INT)])       # 32 位须走双字列表
    assert client.connected is True


def test_tcp_3e_random_write_rejects_bit_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """random_write 位号后缀入参期拒绝(1402 无位号字段)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.random_write([("D100.3", 1)])
    with pytest.raises(ValueError):
        client.random_write([], [("D500.1", 1)])
    assert client.connected is True


def test_tcp_3e_random_write_validates_end_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """random_write 结束码非 0 → False + last_error,不断线(原 P1:只发不收判)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([], frame="3E", end_code=0xC059)
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.random_write([("D100", 0x1234)]) is False
    assert client.last_error is not None and "结束代码 0xC059" in client.last_error
    assert client.connected is True


def test_tcp_3e_get_cpu_type_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 3E get_cpu_type:0101 请求 → (模型名, 模型代码)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    data = b"Q06HCPU".ljust(16) + bytes.fromhex("0b02")
    frame = b"\xd0\x00" + b"\x00\xff\xff\x03\x00" + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, info = client.get_cpu_type()
    assert ok is True and info == ("Q06HCPU", 0x020B)
    assert bytes(scripted.sent) == codec_qna.build_read_cpu_model("3E")


def test_read_batch_rejects_bit_suffix_on_word_type() -> None:
    """read_batch 字软元件非 BOOL 带位号 → 拒绝(与单点 read 口径一致)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    with pytest.raises(ValueError):
        client.read_batch([("D100.3", "short")])


# ----------------------------------------------------------------------
# 连续批量读 read_range(0401 成批读,单事务)
# ----------------------------------------------------------------------


def test_tcp_3e_read_range_words_0401(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range:D100 起 3 个 SHORT = 0401 成批读 3 字单事务(§8.2)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([10, 20, 30])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_range("D100", 3, "short")
    assert ok is True
    assert values == [10, 20, 30]
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 3, False, False
    )


def test_tcp_3e_read_range_ints_two_words_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range:D0 起 2 个 INT = 0401 读 4 字,按 2 字/元素小端切片解码。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([0x0001, 0xF4240 - 0x10000, 0x0000, 0x0000]) if False else \
        _qna_read_response([100, 0, 200, 0])
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_range("D0", 2, "uint")
    assert ok is True
    assert values == [100, 200]
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D0"), 4, False, False
    )


def test_tcp_3e_read_range_bit_device_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range:M0 起 10 个 BOOL = 0401 位单位成批读 10 点。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    request = codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("M0"), 10, True, False
    )
    # 位读响应:半字节打包,每字节 2 点(高半字节在前,SH-080008 §8.2)
    bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 0]
    data = bytes(
        (bits[i] << 4) | bits[i + 1] for i in range(0, len(bits), 2)
    )
    response = b"\xd0\x00" + b"\x00\xff\xff\x03\x00" + (2 + len(data)).to_bytes(
        2, "little"
    ) + (0).to_bytes(2, "little") + data
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_range("M0", 10, "bool")
    assert ok is True
    assert values == [True, False, True, True, False, False, True, False, True, False]
    assert bytes(scripted.sent) == request


def test_tcp_3e_read_range_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range 入参校验:count/类型/位软元件字访问门控/超限。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    with pytest.raises(ValueError):
        client.read_range("D100", 0, "short")
    with pytest.raises(ValueError):
        client.read_range("D100", True, "short")
    with pytest.raises(ValueError):
        client.read_range("D100.3", 2, "bool")  # 字软元件位号不支持 range
    with pytest.raises(ValueError):
        client.read_range("M0", 2, "short")  # 位软元件字访问被门控
    with pytest.raises(ValueError):
        client.read_range("D100", 901, "short")  # 超点数上限
    with pytest.raises(ValueError):
        client.read_range("D100", 2, "string")


def test_read_range_frame_specific_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range 帧型上限分流(review-1002 P2):1E 255、1C BR 256/WR 64,
    超限在入参期拒绝而非 codec 锁内 ValueError 穿透 _execute。"""
    one_e = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    with pytest.raises(ValueError, match="255"):
        one_e.read_range("D100", 256, "short")  # 1E 字读上限 255
    with pytest.raises(ValueError, match="255"):
        one_e.read_range("M0", 256, "bool")  # 1E 位读同上限
    serial_1c = MelsecMcSerialClient(frame="1C")
    with pytest.raises(ValueError, match="256"):
        serial_1c.read_range("M0", 257, "bool")  # 1C BR 上限 256
    with pytest.raises(ValueError, match="64"):
        serial_1c.read_range("D100", 65, "short")  # 1C WR 上限 64 字
    # 边界值放行:挂无应答脚本传输,仅证入口不拒(整批 (False, None))
    boundary = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    _mount(monkeypatch, boundary, ScriptedTransport([]))
    boundary.connect()
    assert boundary.read_range("D100", 255, "short") == (False, None)


def test_tcp_1e_read_range_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range:1E 帧走字单位成批读(副头部 0x01),与单点读同命令。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    frame = _one_e_read_response([10, 20, 30])
    scripted = ScriptedTransport([frame[:2], frame[2:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_range("D100", 3, "short")
    assert ok is True
    assert values == [10, 20, 30]
    assert bytes(scripted.sent) == codec_a.build_request(
        0xFF, MC_DEFAULT_MONITOR_TIMER, parse_mc_address("D100"), 3, False, False
    )


def test_tcp_3e_read_range_device_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_range:PLC 结束码非 0 → (False, None) 不断线。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([0] * 3, end_code=0xC059)
    _mount(monkeypatch, client, ScriptedTransport([frame[:9], frame[9:]]))
    client.connect()
    ok, values = client.read_range("D100", 3, "short")
    assert ok is False
    assert values is None
    assert client.connected is True


def test_async_mirror_read_batch() -> None:
    """异步镜像 read_batch:混类型批量读往返。"""

    async def scenario() -> None:
        client = AMelsecMcTcpClient("127.0.0.1", 2000)
        frame = _qna_random_read_response([7], [0x0001])
        scripted = ScriptedTransport([frame[:9], frame[9:]])
        scripted.receive_timeout = 5.0
        client._sync._transport = scripted
        client._sync._connected = True
        assert await client.read_batch([("D0", "short"), ("M0", "bool")]) == (
            True,
            [7, True],
        )
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 真 TcpTransport 读满语义回归(ScriptedTransport 忽略 size,会掩盖该语义)
# ----------------------------------------------------------------------


def test_tcp_1e_read_error_keeps_connection() -> None:
    """1E 读错误响应只有 2 字节头:按结束码分流,DeviceError 不断线。

    旧实现按成功响应长度继续收数据段,真帧更短 → 阻塞到超时误判断线。
    """
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    mount_real_tcp(client, [bytes([0x81, 0xC0])])  # 副头部 81 + 结束码 C0(软元件异常)
    ok, value = client.read_ushort("D100")
    assert ok is False and value is None
    assert client.connected is True
    assert client.last_error is not None and "0xC0" in client.last_error


def test_tcp_1e_end_code_5b_reads_one_extra_byte() -> None:
    """1E 结束码 5B 后跟 1 字节异常细分码(SH-080008 §18.2 印刷页 395 算例 5BH 10H)。

    回归(第八轮 P2-1):曾按 2 字节收扩展信息——TCP 部分已读超时拆连、
    UDP 整包路径把合法 5B 错误当坏帧拒(真实细分码 10H = PC 号错被吞)。
    """
    client = MelsecMcTcpClient("127.0.0.1", 2000, frame="1E")
    # 副头部 0x80|0x01=0x81 + 结束码 5B + 细分码 10H,共 3 字节
    mount_real_tcp(client, [bytes([0x81, 0x5B, 0x10])])
    ok, value = client.read_ushort("D100")
    assert ok is False and value is None
    assert client.connected is True
    assert client.last_error is not None and "0x5B" in client.last_error


def test_udp_1e_end_code_5b_not_rejected_as_bad_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """1E over UDP:5B 错误响应(3 字节)不得因长度 <4 被当坏帧拒。"""
    client = MelsecMcUdpClient("127.0.0.1", 2000, frame="1E")
    scripted = ScriptedTransport([bytes([0x81, 0x5B, 0x10])], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, value = client.read_ushort("D100")
    assert ok is False and value is None
    assert client.connected is True


def test_tcp_3e_read_real_transport_semantics() -> None:
    """真 TcpTransport 凑满循环:响应小片到达仍能完整收包。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_read_response([20])
    mount_real_tcp(client, [frame[:3], frame[3:7], frame[7:]])
    assert client.read_ushort("D100") == (True, 20)


def test_tcp_3e_response_content_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """3E:应答数据长超限在 recv 前快失败,按坏帧断线不阻塞。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    evil_head = b"\xd0\x00\x00\x00\xff\xff\x03\x00" + b"\xff\xff"  # 数据长 0xFFFF
    scripted = ScriptedTransport([evil_head])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "超限" in client.last_error


# ----------------------------------------------------------------------
# 位软元件位号后缀校验(MC 侧补齐,与 MX 同口径)+ FX5U 八进制
# ----------------------------------------------------------------------


def test_tcp_3e_bit_device_bit_suffix_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """位软元件带位号后缀(如 M10.5)直接拒绝,不静默丢位号错位读写。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_bool("M10.5")
    with pytest.raises(ValueError):
        client.write_bool("M10.5", True)
    with pytest.raises(ValueError):
        client.read_batch([("M10.5", "bool")])
    assert len(scripted.sent) == 0  # 参数错误不发报文


def test_tcp_3e_word_device_bit_suffix_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """字软元件位访问(D100.2)不受影响:读-改-写口径保持。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    read = _qna_read_response([0b0000_0000_0000_0100])
    write = _qna_write_response()
    scripted = ScriptedTransport([read[:9], read[9:], write[:9], write[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("D100.2", True) is True


def test_tcp_3e_bit_device_word_access_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """位软元件按字单位单点读受门控(第八轮 P2-14:原单点路径绕过门控)。

    ``read("M16", SHORT)`` 组出字单位读、编号非 16 对齐会被 PLC 拒或按
    16 点/字错读——与 read_batch 的 0406 字块同防线;``bit_device_word_
    access=True`` 放行(兼容子类口径);未知软元件的报错时机不变。
    """
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError, match="只支持 BOOL"):
        client.read("M16", "short")
    assert len(scripted.sent) == 0  # 参数错误不发报文
    # 字软元件读 BOOL(D100 无位号 → bit0)不受门控影响
    client.disconnect()
    read = _qna_read_response([0b0000_0000_0000_0001])
    scripted2 = ScriptedTransport([read[:9], read[9:]])
    _mount(monkeypatch, client, scripted2)
    client.connect()
    assert client.read_bool("D100") == (True, True)


def test_tcp_3e_fx5u_xy_octal(monkeypatch: pytest.MonkeyPatch) -> None:
    """xy_octal=True:iQ-F 口径,X/Y 编号按八进制换算组帧。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000, xy_octal=True)
    data = b"\x10"  # 1 点位读,bit0=1(高位在前)
    head = b"\xd0\x00\x00\xff\xff\x03\x00"
    frame = head + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("X17") == (True, True)  # 八进制 17 = 15 点
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("X17"), 1, True, False, None, _MC_DEVICE_CODES_FX5U_XY,
    )
    with pytest.raises(ValueError):
        client.read_bool("X19")  # 八进制无 8/9 数字


def test_tcp_3e_default_xy_hex_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认(非 xy_octal)仍按 Q/L/R 十六进制口径:X19 合法、按 0x19 组帧。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    data = b"\x10"
    head = b"\xd0\x00\x00\xff\xff\x03\x00"
    frame = head + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("X19") == (True, True)
    assert bytes(scripted.sent) == codec_qna.build_request(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER,
        parse_mc_address("X19"), 1, True, False, None,
    )


def test_tcp_3e_read_batch_rejects_word_type_on_bit_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """位软元件进 0406 字块直接拒绝(不发必然被拒/错读的请求)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_batch([("M10", "short")])
    with pytest.raises(ValueError):
        client.read_batch([("X0", "int")])
    assert len(scripted.sent) == 0  # 参数错误零字节发送


def test_tcp_3e_read_batch_merges_adjacent_bit_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相邻同软元件位请求合并为一个 0406 位块(1 点=16 位),按位位置解码。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_random_read_response([], [0x0005])  # M0=1 / M1=0 / M2=1(bit0/1/2)
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_batch(
        [("M0", DataType.BOOL), ("M1", DataType.BOOL), ("M2", DataType.BOOL)]
    )
    assert (ok, values) == (True, [True, False, True])
    # 3 个位请求合并为单个位块(点数为 1);未合并时会是 3 个块
    assert bytes(scripted.sent) == codec_qna.build_random_read(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, [], [(0x90, 0, 1)]
    )


def test_tcp_3e_read_batch_noncontiguous_bits_not_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非连续位请求各自成块(M0 / M2 不合并,两处各取 bit0)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    frame = _qna_random_read_response([], [0x0001, 0x0000])  # M0=1;M2=0
    scripted = ScriptedTransport([frame[:9], frame[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_batch([("M0", DataType.BOOL), ("M2", DataType.BOOL)])
    assert (ok, values) == (True, [True, False])
    assert bytes(scripted.sent) == codec_qna.build_random_read(
        "3E", 1, 0, 0xFF, MC_DEFAULT_MONITOR_TIMER, [], [(0x90, 0, 1), (0x90, 2, 1)]
    )


def test_string_rejects_bit_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """MC 字符串读写拒绝字软元件位号后缀(与 MX/Modbus 一致,不静默读整字)。"""
    client = MelsecMcTcpClient("127.0.0.1", 2000)
    _mount(monkeypatch, client, ScriptedTransport([]))
    client.connect()
    with pytest.raises(ValueError):
        client.read_string("D100.3", 4)
    with pytest.raises(ValueError):
        client.write_string("D100.3", "AB")


def _qna_cpu_model_response(name: bytes = b"Q02UCPU", code: int = 0x0263) -> bytes:
    """构造 0101 CPU 型号读响应(测试脚手架;名 16B ASCII + 代码 2B 小端)。"""
    data = name.ljust(16, b"\x20") + code.to_bytes(2, "little")
    head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + b"\x00\x00" + data


def test_ping_3e_uses_0101_and_1e_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """ping():3E 帧探测命令为 0101 CPU 型号读;1E 帧无探测命令恒 False。"""
    client = MelsecMcTcpClient("127.0.0.1", frame="3E")
    assert client.ping_supported is True
    response = _qna_cpu_model_response()
    scripted = ScriptedTransport([response[:9], response[9:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.ping() is True
    assert bytes(scripted.sent) == codec_qna.build_read_cpu_model(
        "3E", 1, 0, MC_DEFAULT_PC_NUMBER, MC_DEFAULT_MONITOR_TIMER
    )

    client_1e = MelsecMcTcpClient("127.0.0.1", frame="1E")
    assert client_1e.ping_supported is False
    assert client_1e.ping() is False
    assert client_1e._transport is None  # 兜底路径不发包、不建连
