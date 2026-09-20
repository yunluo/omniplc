"""AB EtherNet/IP(CIP)编解码黄金向量测试:ENIP 封装 + UC Send + 标签服务。

字面字节向量按 CIP/EtherNet/IP 规范与 pylogix/cm_ethernetip/aphyt
三份参考实现交叉核证手工推得(见 architecture.md §8.1)。
"""
from __future__ import annotations

import struct

import pytest

from omniplc.plc.ab import codec_cip
from omniplc.plc.ab.address import parse_ab_tag
from omniplc.core.errors import DeviceError, ProtocolFrameError
from omniplc.types import DataType

_SESSION = 0x12345678
_MYDINT_PATH = bytes.fromhex("91064d7944696e74")  # 91 06 "MyDint"


def test_parse_ab_tag() -> None:
    """标签名解析:成员路径/数组下标/位号/程序作用域。"""
    parsed = parse_ab_tag("MyUdt.Member[3].Other")
    assert parsed.members == ("MyUdt", "Member", "Other")
    assert parsed.indices == ((), (3,), ())
    assert parsed.bit is None
    assert parsed.base == "MyUdt.Member.Other"
    parsed = parse_ab_tag("Tag[1].2")
    assert parsed.members == ("Tag",) and parsed.indices == ((1,),) and parsed.bit == 2
    parsed = parse_ab_tag("Matrix[1,2]")
    assert parsed.indices == ((1, 2),)
    parsed = parse_ab_tag("Program:Prog1.Tag")
    assert parsed.members == ("Program:Prog1", "Tag")


def test_parse_ab_tag_invalid() -> None:
    """非法标签名:空名/非法字符/非法段/下标越界。"""
    with pytest.raises(ValueError):
        parse_ab_tag("")
    with pytest.raises(ValueError):
        parse_ab_tag("Tag#1")
    with pytest.raises(ValueError):
        parse_ab_tag("Tag..x")
    with pytest.raises(ValueError):
        parse_ab_tag("Tag[1,].x")
    with pytest.raises(ValueError):
        parse_ab_tag("Tag.3x")
    with pytest.raises(ValueError):
        parse_ab_tag("Tag[4294967296]")


def test_register_session_golden() -> None:
    """RegisterSession 请求字面字节(命令 0x0065 + 协议版本 1)。"""
    assert codec_cip.build_register_session() == bytes.fromhex(
        "65000400" "0000000000000000000000000000000000000000" "01000000"
    )


def test_parse_register_session() -> None:
    """应答解析取会话句柄;封装状态非 0/命令不符按坏帧。"""
    reply = struct.pack("<HHIIQIHH", 0x65, 4, _SESSION, 0, 0, 0, 1, 0)
    assert codec_cip.parse_register_session(reply) == _SESSION
    bad_status = struct.pack("<HHIIQIHH", 0x65, 4, 0, 0x64, 0, 0, 1, 0)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_register_session(bad_status)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_register_session(struct.pack("<HHIIQIHH", 0x66, 4, 0, 0, 0, 0, 1, 0))


def test_unregister_session_golden() -> None:
    """UnregisterSession 请求:命令 0x0066 + 空载荷。"""
    frame = codec_cip.build_unregister_session(_SESSION)
    assert frame[:2] == b"\x66\x00"
    assert len(frame) == 24
    assert struct.unpack_from("<I", frame, 4)[0] == _SESSION


def test_uc_send_read_frame_golden() -> None:
    """读 MyDint 完整 RRData 帧字面字节(session 0x12345678,槽 0)。"""
    request = codec_cip.build_tag_read(_MYDINT_PATH, 1)
    frame = codec_cip.build_rr_data(_SESSION, codec_cip.build_uc_send(request, 0))
    assert frame == bytes.fromhex(
        "6f002a0078563412" "00000000000000000000000000000000"  # ENIP 头
        "000000000000020000000000b2001a00"  # CPF 前缀
        "5202200624010aff0c00"  # UC Send 头
        "4c0491064d7944696e740100"  # Tag Read 服务
        "02000100"  # 路由段(背板 + 槽 0)
    )


def test_uc_send_padding_and_route() -> None:
    """奇数长度请求补零对齐,槽号进路由段末字节。"""
    frame = codec_cip.build_uc_send(b"\x01\x02\x03", 2)
    assert len(frame) == 10 + 3 + 1 + 4
    assert frame[10:13] == b"\x01\x02\x03"
    assert frame[13] == 0x00
    assert frame[14:] == bytes.fromhex("02000102")


def test_symbol_path_segments() -> None:
    """路径构造:符号段偶对齐 + 元素段按大小选码 + 末级下标置零。"""
    path = codec_cip.build_symbol_path(("M",), ((5, 70000),))
    assert path == bytes.fromhex("91014d00" "2805" "2a70110100")
    path = codec_cip.build_symbol_path(("Bits",), ((12,),), zero_last_index=True)
    assert path == bytes.fromhex("910442697473" "2800")


def test_tag_write_frames() -> None:
    """写帧:原子类型域(码 + 0x00 + 点数)与 STRING 类型域(A0 + 02 + 模板)。"""
    request = codec_cip.build_tag_write(_MYDINT_PATH, 0xC4, b"\x39\x05\x00\x00")
    assert request == bytes.fromhex("4d04" "91064d7944696e74" "c4000100" "39050000")
    request = codec_cip.build_string_write(_MYDINT_PATH, b"\x02\x00\x00\x00AB")
    assert request[10:12] == bytes.fromhex("a002")
    assert request[12:14] == struct.pack("<H", 0x0FCE)
    assert request[14:16] == b"\x01\x00"


def test_read_modify_write_frame() -> None:
    """0x4E 帧:掩码字节数 + OR + AND(小端)。"""
    request = codec_cip.build_read_modify_write(_MYDINT_PATH, 0xC4, 8, 0xFFFFFFF7)
    assert request == bytes.fromhex(
        "4e04" "91064d7944696e74" "0400" "08000000" "f7ffffff"
    )


def test_bit_masks() -> None:
    """位写掩码:置位 OR=该位/AND=全 1;清零 OR=0/AND=屏蔽该位。"""
    assert codec_cip.bit_masks(0xC4, 3, True) == (8, 0xFFFFFFFF)
    assert codec_cip.bit_masks(0xC4, 3, False) == (0, 0xFFFFFFF7)
    assert codec_cip.bit_masks(0xC5, 3, True) == (8, 0xFFFFFFFFFFFFFFFF)


def _wrapped_reply(
    payload: bytes = b"",
    service: int = codec_cip.CIP_SERVICE_READ_TAG,
    cip_status: int = 0,
    route_status: int = 0,
    enip_status: int = 0,
    data_type: int = 0xB2,
) -> bytes:
    """构造完整 SendRRData 应答(测试脚手架)。"""
    embedded = bytes((service | 0x80, 0, cip_status, 0)) + payload
    cip = bytes((0xD2, 0, route_status, 0)) + embedded
    header = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, enip_status, 0, 0)
    prefix = (
        struct.pack("<I", 0)
        + struct.pack("<H", 0)
        + struct.pack("<H", 2)
        + struct.pack("<HH", 0, 0)
        + struct.pack("<HH", data_type, len(cip))
    )
    return header + prefix + cip


def test_parse_service_reply_golden() -> None:
    """读应答解析黄金向量:剥 UC/内嵌两层头后剩类型域 + 数据。"""
    payload = bytes.fromhex("c400" "39050000")
    reply = _wrapped_reply(payload)
    assert struct.unpack_from("<H", reply, 2)[0] == 16 + 14
    data = codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)
    assert data == payload
    cip_type, values = codec_cip.parse_tag_read_payload(data)
    assert cip_type == 0xC4
    assert codec_cip.decode_values(values, cip_type, 1) == [1337]


def test_parse_service_reply_errors() -> None:
    """错误路径:CIP 状态 → DeviceError 不断线;坏帧 → ProtocolFrameError。"""
    reply = _wrapped_reply(b"", cip_status=0x08)
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)
    assert exc_info.value.code == 0x08
    reply = _wrapped_reply(b"", route_status=0x05)
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)
    assert exc_info.value.code == 0x05
    reply = _wrapped_reply(b"", service=0x4D)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)
    reply = _wrapped_reply(b"", enip_status=0x64)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)
    reply = _wrapped_reply(b"", data_type=0xB1)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_READ_TAG)


def test_parse_tag_read_payload_struct() -> None:
    """结构体应答:类型域 4 字节(0xA0 + 模板号),值域从 STRING 长度起。"""
    payload = bytes.fromhex("a000ce0f") + struct.pack("<I", 2) + b"AB"
    cip_type, data = codec_cip.parse_tag_read_payload(payload)
    assert cip_type == codec_cip.CIP_TYPE_STRUCT
    assert codec_cip.decode_string_payload(data, "utf-8") == "AB"


def test_decode_values_and_word() -> None:
    """原子解码:REAL/LINT/BOOL 与无符号位提取。"""
    assert codec_cip.decode_values(struct.pack("<f", 3.5), 0xCA, 1) == [3.5]
    assert codec_cip.decode_values(struct.pack("<q", -2), 0xC5, 1) == [-2]
    assert codec_cip.decode_values(b"\x01", 0xC1, 1) == [1]
    assert codec_cip.decode_word(b"\x00\x10\x00\x00", 0xD3) == 0x1000
    with pytest.raises(ProtocolFrameError):
        codec_cip.decode_values(b"\x01", 0xC4, 1)


def test_string_struct_codec() -> None:
    """STRING 结构体编码 88 字节布局,82 字符上限。"""
    encoded = codec_cip.encode_string_struct("AB", "ascii")
    assert len(encoded) == 88
    assert encoded[:6] == b"\x02\x00\x00\x00AB"
    assert set(encoded[6:]) == {0}
    with pytest.raises(ValueError):
        codec_cip.encode_string_struct("a" * 83, "ascii")
    with pytest.raises(ProtocolFrameError):
        codec_cip.decode_string_payload(b"\xff\x00\x00\x00AB", "ascii")


def test_encode_value_ranges() -> None:
    """写值编码:范围校验沿用库约定(越界 ValueError)。"""
    assert codec_cip.encode_value(DataType.SHORT, -5) == struct.pack("<h", -5)
    assert codec_cip.encode_value(DataType.UINT, 4294967295) == b"\xff\xff\xff\xff"
    with pytest.raises(ValueError):
        codec_cip.encode_value(DataType.SHORT, 32768)
    with pytest.raises(ValueError):
        codec_cip.encode_value(DataType.ULONG, -1)
    with pytest.raises(ValueError):
        codec_cip.data_type_code(DataType.STRING)
