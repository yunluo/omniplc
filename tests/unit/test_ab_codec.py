"""AB EtherNet/IP(CIP)编解码黄金向量测试:ENIP 封装 + UC Send + 标签服务。

字面字节向量按 CIP/EtherNet/IP 规范推得(见 architecture.md §8.1)。
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
        "000000000100020000000000b2001a00"  # CPF 前缀(超时 1 秒)
        "5202200624010af00c00"  # UC Send 头
        "4c0491064d7944696e740100"  # Tag Read 服务
        "01000100"  # 路由段(path_size=1 字 + 保留 + 背板 + 槽 0)
    )


def test_uc_send_padding_and_route() -> None:
    """奇数长度请求补零对齐,槽号进路由段末字节。"""
    frame = codec_cip.build_uc_send(b"\x01\x02\x03", 2)
    assert len(frame) == 10 + 3 + 1 + 4
    assert frame[10:13] == b"\x01\x02\x03"
    assert frame[13] == 0x00
    assert frame[14:] == bytes.fromhex("01000102")


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


def test_parse_service_reply_tolerates_0x66_reply() -> None:
    """0x66 应答头宽容(偏差模拟器):规范 CPF 体与连接式 CPF 体都取数据项。"""
    payload = bytes.fromhex("c400" "39050000")
    reply = _wrapped_reply(payload)
    deviant = struct.pack(
        "<HHIIQI", 0x66, len(reply) - 24, _SESSION, 0, 0, 0
    ) + reply[24:]
    assert codec_cip.parse_service_reply(
        deviant, codec_cip.CIP_SERVICE_READ_TAG
    ) == payload

    embedded = bytes((codec_cip.CIP_SERVICE_READ_TAG | 0x80, 0, 0, 0)) + payload
    cip = bytes((0xD2, 0, 0, 0)) + embedded
    header = struct.pack("<HHIIQI", 0x66, 22 + len(cip), _SESSION, 0, 0, 0)
    prefix = (
        struct.pack("<I", 0)
        + struct.pack("<H", 0)
        + struct.pack("<H", 2)
        + struct.pack("<HHI", 0xA1, 4, 0x11223344)
        + struct.pack("<HH", 0xB1, len(cip) + 2)
        + struct.pack("<H", 7)
    )
    connected = header + prefix + cip
    assert codec_cip.parse_service_reply(
        connected, codec_cip.CIP_SERVICE_READ_TAG
    ) == payload

    wrong = struct.pack(
        "<HHIIQI", 0x65, len(reply) - 24, _SESSION, 0, 0, 0
    ) + reply[24:]
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_service_reply(wrong, codec_cip.CIP_SERVICE_READ_TAG)


def test_parse_service_reply_bare_body_without_d2() -> None:
    """偏差模拟器(个别服务端)剥掉 0xD2 路由信封:应答体即内嵌服务应答。"""
    payload = bytes.fromhex("c400" "39050000")
    cip = bytes((codec_cip.CIP_SERVICE_READ_TAG | 0x80, 0, 0, 0)) + payload
    header = struct.pack("<HHIIQI", 0x66, 22 + len(cip), _SESSION, 0, 0, 0)
    prefix = (
        struct.pack("<I", 0)
        + struct.pack("<H", 0)
        + struct.pack("<H", 2)
        + struct.pack("<HHI", 0xA1, 4, 0x11223344)
        + struct.pack("<HH", 0xB1, len(cip) + 2)
        + struct.pack("<H", 7)
    )
    frame = header + prefix + cip
    assert codec_cip.parse_service_reply(frame, codec_cip.CIP_SERVICE_READ_TAG) == payload


def test_parse_direct_service_reply() -> None:
    """直发应答(NJ/NX,无 0xD2 外层):一层服务头 + 数据;错误路径同契约。"""
    payload = bytes.fromhex("c400" "05000000")
    cip = bytes((codec_cip.CIP_SERVICE_READ_TAG | 0x80, 0, 0, 0)) + payload
    header = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, 0, 0, 0)
    prefix = struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(cip))
    reply = header + prefix + cip
    assert codec_cip.parse_direct_service_reply(
        reply, codec_cip.CIP_SERVICE_READ_TAG
    ) == payload
    bad_cip = (
        bytes((codec_cip.CIP_SERVICE_READ_TAG | 0x80, 0, 0x16, 0)) + payload
    )
    bad_reply = (
        struct.pack("<HHIIQI", 0x6F, 16 + len(bad_cip), _SESSION, 0, 0, 0)
        + struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(bad_cip))
        + bad_cip
    )
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_direct_service_reply(
            bad_reply, codec_cip.CIP_SERVICE_READ_TAG
        )
    assert exc_info.value.code == 0x16
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_direct_service_reply(
            reply, codec_cip.CIP_SERVICE_WRITE_TAG
        )


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
    assert codec_cip.encode_value(DataType.SHORT, -5) == b"\xfb\xff"
    assert codec_cip.encode_value(DataType.UINT, 4294967295) == b"\xff\xff\xff\xff"
    with pytest.raises(ValueError):
        codec_cip.encode_value(DataType.SHORT, 32768)
    with pytest.raises(ValueError):
        codec_cip.encode_value(DataType.ULONG, -1)
    with pytest.raises(ValueError):
        codec_cip.data_type_code(DataType.STRING)


# ----------------------------------------------------------------------
# connected 消息(Forward Open/Close + SendUnitData)
# ----------------------------------------------------------------------

def _rr_data_reply(cip: bytes) -> bytes:
    """裸 CIP 应答的 RRData 包装(测试脚手架)。"""
    header = struct.pack("<HHIIQI", 0x6F, 16 + len(cip), _SESSION, 0, 0, 0)
    prefix = struct.pack("<IHHHHHH", 0, 0, 2, 0, 0, 0xB2, len(cip))
    return header + prefix + cip


def test_forward_open_golden() -> None:
    """普通 Forward Open 字面字节(0x54,参数域 16 位,背板槽 0 路径)。"""
    request = codec_cip.build_forward_open(
        False, 504, 0x1234, 0x5678, 0x1337, 42, b"\x01\x00"
    )
    assert request == bytes.fromhex(
        "5402200624010a0e"  # 服务 + CM 路径 + 优先级/超时
        "00000000" "78560000"  # O->T CID(0=目标分配)+ T->O CID
        "3412" "3713" "2a000000"  # 连接序列号 + 厂商号 + 发起方序列号
        "03000000"  # 超时乘数 + 3 保留
        "a0860100" "f843"  # O->T RPI(100ms) + 参数(P2P+固定+504)
        "a0860100" "f843"  # T->O RPI(100ms) + 参数
        "a3" "03" "010020022401"  # 传输触发 + 路径字数 + 背板/槽/消息路由
    )


def test_forward_open_empty_route() -> None:
    """空路由段(NJ/NX 内置口):路径只剩消息路由对象,字数 2。"""
    request = codec_cip.build_forward_open(
        False, 504, 0x1234, 0x5678, 0x1337, 42, b""
    )
    assert request.endswith(bytes.fromhex("02" "20022401"))
    with pytest.raises(ValueError):
        codec_cip.build_forward_open(False, 504, 0, 0, 0, 0, b"\x01")


def test_forward_open_large_format() -> None:
    """Large Forward Open:服务 0x5B、参数域 32 位(0x4200<<16 + 尺寸)。"""
    request = codec_cip.build_forward_open(
        True, 4002, 0x1234, 0x5678, 0x1337, 42, b"\x01\x00"
    )
    assert request[0] == 0x5B
    large_params = struct.pack("<I", (0x4200 << 16) + 4002)
    assert large_params == bytes.fromhex("a20f0042")
    body = request[8:]
    assert body[24:28] == large_params
    assert body[32:36] == large_params


def test_forward_open_rpi_param() -> None:
    """RPI 可配:O->T/T->O 两处都写入给定值;非正拒绝。"""
    rpi = struct.pack("<I", 500_000)
    request = codec_cip.build_forward_open(
        False, 504, 1, 2, 0x1337, 42, b"\x01\x00", rpi_us=500_000
    )
    assert request.count(rpi) == 2
    with pytest.raises(ValueError):
        codec_cip.build_forward_open(False, 504, 1, 2, 0x1337, 42, b"", rpi_us=0)


def test_connection_reset_status_set() -> None:
    """连接失效状态集:0x01/0x07 触发重连,其余(如 0x05)不触发。"""
    assert codec_cip.is_connection_reset_status(0x01)
    assert codec_cip.is_connection_reset_status(0x07)
    assert not codec_cip.is_connection_reset_status(0x05)


def test_forward_open_reply() -> None:
    """应答解析:成功取 O->T 连接 ID;状态非 0 原样返回供回落判断。"""
    reply = _rr_data_reply(
        bytes((0xD4, 0, 0, 0)) + struct.pack("<II", 0xAABBCCDD, 0x5678)
    )
    assert codec_cip.parse_forward_open_reply(reply, 0x54) == (0, 0xAABBCCDD)
    reply = _rr_data_reply(bytes((0xDB, 0, 0x01, 0)) + b"\x00\x00")
    assert codec_cip.parse_forward_open_reply(reply, 0x5B) == (0x01, 0)
    reply = _rr_data_reply(bytes((0xD4, 0, 0, 0)) + b"\x00" * 8)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_forward_open_reply(reply, 0x5B)


def test_forward_close_golden_and_reply() -> None:
    """Forward Close 字面字节与应答状态解析。"""
    request = codec_cip.build_forward_close(0x1234, 0x1337, 42, b"\x01\x00")
    assert request == bytes.fromhex(
        "4e0220062401" "0a0e"
        "3412" "3713" "2a000000"
        "03" "00" "010020022401"
    )
    reply = _rr_data_reply(bytes((0xCE, 0, 0, 0)))
    assert codec_cip.parse_forward_close_reply(reply) == 0
    reply = _rr_data_reply(bytes((0xCC, 0, 0, 0)))
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_forward_close_reply(reply)
    with pytest.raises(ValueError):
        codec_cip.build_forward_close(0, 0, 0, b"\x01")


def test_send_unit_data_golden() -> None:
    """SendUnitData 字面字节(0xA1 地址项 + 0xB1 数据项 + 序列号)。"""
    request = codec_cip.build_tag_read(_MYDINT_PATH, 1)
    frame = codec_cip.build_send_unit_data(_SESSION, 0xAABBCCDD, 1, request)
    assert frame == bytes.fromhex(
        "70002200" "78563412" "00000000000000000000000000000000"  # ENIP 头(len=22+12)
        "0000000001000200"  # interface + timeout(1 秒) + 项数
        "a1000400" "ddccbbaa"  # 连接地址项:O->T 连接 ID
        "b1000e000100"  # 连接数据项:len=12+2、序列号 1
        "4c0491064d7944696e740100"  # Tag Read
    )


def test_parse_send_unit_data_reply() -> None:
    """应答解析:校验 T->O ID/序列号/服务回显/状态。"""
    payload = bytes.fromhex("c40010040000")
    cip = bytes((0xCC, 0, 0, 0)) + payload
    header = struct.pack("<HHIIQI", 0x70, 22 + len(cip), _SESSION, 0, 0, 0)
    prefix = (
        struct.pack("<IHH", 0, 0, 2)
        + struct.pack("<HHI", 0xA1, 4, 0x5678)
        + struct.pack("<HHH", 0xB1, len(cip) + 2, 7)
    )
    reply = header + prefix + cip
    assert codec_cip.parse_send_unit_data_reply(reply, 0x4C, 0x5678, 7) == payload
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_send_unit_data_reply(reply, 0x4C, 0x1111, 7)
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_send_unit_data_reply(reply, 0x4C, 0x5678, 8)
    bad_cip = bytes((0xCC, 0, 0x16, 0))
    bad_header = struct.pack("<HHIIQI", 0x70, 22 + len(bad_cip), _SESSION, 0, 0, 0)
    bad_prefix = (
        struct.pack("<IHH", 0, 0, 2)
        + struct.pack("<HHI", 0xA1, 4, 0x5678)
        + struct.pack("<HHH", 0xB1, len(bad_cip) + 2, 7)
    )
    bad = bad_header + bad_prefix + bad_cip
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_send_unit_data_reply(bad, 0x4C, 0x5678, 7)
    assert exc_info.value.code == 0x16


# ----------------------------------------------------------------------
# 通用 CIP 服务 + 扩展码诊断
# ----------------------------------------------------------------------

def test_build_get_attributes_all_identity_object() -> None:
    """GetAttributesAll on Identity Object (class=0x01 instance=0x01) 字节布局。

    期望:0x01 (服务) + 0x02 (路径字数) + 0x20 0x01 (class 8 位段) +
    0x24 0x01 (instance 8 位段) = 6 字节。
    """
    frame = codec_cip.build_get_attributes_all(0x01, 0x01)
    assert frame == bytes.fromhex("010220012401")


def test_build_get_attribute_list_two_attributes() -> None:
    """GetAttributeList 请求布局:属性数(2 字节)+ 属性号列表(每 2 字节)。

    Identity Object 属性 1 (vendor) + 6 (serial) = 0100 0600,前面拼
    class-instance 路径 ``01 02 20 01 24 01`` 与服务头 0x03 + 0x02 字数。
    """
    frame = codec_cip.build_get_attribute_list(0x01, 0x01, (1, 6))
    assert frame == bytes.fromhex("030220012401020001000600")


def test_parse_list_identity_reply_full_fields() -> None:
    """ListIdentity ENIP 应答:校验 ENIP 头 + 22 字节 socket 前置 + 7 字段解码。

    vendor=0x1234, product_type=0x000E(PLC), product_code=0x5678,
    revision=(30, 11), status=0x0001, serial=0x89ABCDEF,
    product_name="1769-L23E"(8 字节), state=0xFF。
    """
    socket_prefix = b"\x00\x00"  # 2 字节兼容前缀(部分实现带 interface handle / version)
    identity_body = struct.pack(
        "<HHHBBH",
        0x1234,  # vendor
        0x000E,  # product_type
        0x5678,  # product_code
        30, 11,  # revision major/minor
        0x0001,  # status
    ) + struct.pack("<I", 0x89ABCDEF) + bytes((8,)) + b"1769-L23" + bytes((0xFF,))
    payload = socket_prefix + identity_body
    header = struct.pack(
        "<HHIIQI",
        codec_cip.EIP_COMMAND_LIST_IDENTITY,
        len(payload),
        _SESSION,
        0,
        0,
        0,
    )
    reply = header + payload
    info = codec_cip.parse_list_identity_reply(reply)
    assert info["vendor"] == 0x1234
    assert info["product_type"] == 0x000E
    assert info["product_code"] == 0x5678
    assert info["revision"] == (30, 11)
    assert info["status"] == 0x0001
    assert info["serial"] == 0x89ABCDEF
    assert info["product_name"] == "1769-L23"
    assert info["state"] == 0xFF


def test_parse_module_identity_payload() -> None:
    """GetAttributesAll 裸数据(7 字段 Identity Object)解码。"""
    payload = struct.pack(
        "<HHHBBH",
        0x0001,  # vendor = Rockwell
        0x000E,  # product_type = PLC
        0x1234,  # product_code
        24, 6,  # revision major/minor
        0x0001,  # status
    ) + struct.pack("<I", 0xDEADBEEF) + bytes((5,)) + b"PLC-A"
    info = codec_cip.parse_module_identity_payload(payload)
    assert info["vendor"] == 0x0001
    assert info["product_type"] == 0x000E
    assert info["revision"] == (24, 6)
    assert info["serial"] == 0xDEADBEEF
    assert info["product_name"] == "PLC-A"


def test_extended_status_attached_to_device_error() -> None:
    """cip_status=0x04 + 扩展 0x0001:DeviceError 消息末尾拼扩展码文本。

    布局:服务回显(0x01|0x80) + 保留(0) + 通用状态(0x04) +
    size_of_additional=2(单位 16 位字) + 扩展码 LE u16 0x0001 +
    数据域(空)。cip 总长 8 字节(4 头 + 4 扩展码),_parse_service_payload
    抛 DeviceError 之前把扩展码文本拼到 message。
    """
    cip_with_ext = (
        bytes((codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL | 0x80, 0x00, 0x04, 0x02))
        + struct.pack("<H", 0x0001)
        + b"\x00\x00"  # 数据域(空但需 padding,使总长 ≥ 4 + ext_bytes)
    )
    # 手动构造完整 ENIP 帧,确保 length 域匹配实际长度
    uc_send = bytes((0xD2, 0x00, 0x00, 0x00))
    cip_payload = uc_send + cip_with_ext
    cpf_prefix = (
        struct.pack("<IHHHHHH", 0, 0, 2, codec_cip._CPF_ITEM_NULL_ADDRESS, 0,
                    codec_cip._CPF_ITEM_UNCONNECTED_DATA, len(cip_payload))
    )
    body = cpf_prefix + cip_payload
    header = struct.pack(
        "<HHIIQI", codec_cip.EIP_COMMAND_SEND_RR_DATA, len(body), _SESSION, 0, 0, 0
    )
    reply = header + body
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL)
    assert exc_info.value.code == 0x04
    assert "路径段错误" in str(exc_info.value)
    assert "实例不足" in str(exc_info.value)


def test_extended_status_unknown_omitted() -> None:
    """cip_status=0x04 + 扩展 0x9999:扩展码未命中,消息不含扩展文本。

    DeviceError.message 只含通用状态文本;code 仍为 0x04。
    """
    cip = (
        bytes((codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL | 0x80, 0x00, 0x04, 0x02))
        + struct.pack("<H", 0x9999)
    )
    uc_send = bytes((0xD2, 0x00, 0x00, 0x00))
    cip_payload = uc_send + cip
    cpf_prefix = (
        struct.pack("<IHHHHHH", 0, 0, 2, codec_cip._CPF_ITEM_NULL_ADDRESS, 0,
                    codec_cip._CPF_ITEM_UNCONNECTED_DATA, len(cip_payload))
    )
    body = cpf_prefix + cip_payload
    header = struct.pack(
        "<HHIIQI", codec_cip.EIP_COMMAND_SEND_RR_DATA, len(body), _SESSION, 0, 0, 0
    )
    reply = header + body
    with pytest.raises(DeviceError) as exc_info:
        codec_cip.parse_service_reply(reply, codec_cip.CIP_SERVICE_GET_ATTRIBUTES_ALL)
    assert exc_info.value.code == 0x04
    assert "路径段错误" in str(exc_info.value)
    # 扩展码 0x9999 不在表中,不应出现 "—" 分隔的扩展文本
    assert "—" not in str(exc_info.value)


def test_parse_service_reply_tolerates_zero_echo_write_reply() -> None:
    """个别服务端写应答回显省略(首字节 0x00):放行;非零错回显仍拒。

    写应答 CIP 体 = 00 00 00 00(回显省略 + 保留 0 + 状态 0 + 附加长 0)。
    """
    def _reply_with_embedded(embedded: bytes) -> bytes:
        uc_send = bytes((0xD2, 0x00, 0x00, 0x00))
        cip_payload = uc_send + embedded
        cpf_prefix = struct.pack(
            "<IHHHHHH", 0, 0, 2, codec_cip._CPF_ITEM_NULL_ADDRESS, 0,
            codec_cip._CPF_ITEM_UNCONNECTED_DATA, len(cip_payload)
        )
        body = cpf_prefix + cip_payload
        header = struct.pack(
            "<HHIIQI", codec_cip.EIP_COMMAND_SEND_RR_DATA, len(body), _SESSION, 0, 0, 0
        )
        return header + body

    zero_echo = bytes((0x00, 0x00, 0x00, 0x00))
    assert codec_cip.parse_service_reply(_reply_with_embedded(zero_echo), 0x4D) == b""
    wrong_echo = bytes((0xCB, 0x00, 0x00, 0x00))  # 期望 0xCC,实际 0xCB
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_service_reply(_reply_with_embedded(wrong_echo), 0x4D)


def test_build_multiple_service_packet() -> None:
    """多服务包:0x0A + 消息路由器路径 + 条数/偏移(自条数域起算)/补齐。"""
    packet = codec_cip.build_multiple_service_packet([
        bytes.fromhex("4c01020304"),  # 5 字节(奇)
        bytes.fromhex("4c0105060708"),  # 6 字节(偶)
    ])
    # 条数 0200;偏移:第 1 条 6(2+2*2),第 2 条 6+5+1(补齐)=0x0C
    assert packet == bytes.fromhex(
        "0a02" "20022401" "0200" "0600" "0c00" "4c01020304" "00" "4c0105060708"
    )


def test_build_multiple_service_packet_validation() -> None:
    """多服务包构造校验:空请求/条数超限。"""
    with pytest.raises(ValueError):
        codec_cip.build_multiple_service_packet([])
    with pytest.raises(ValueError):
        codec_cip.build_multiple_service_packet(
            [b"\x4c\x01\x01"] * 33
        )


def test_parse_multiple_service_payload() -> None:
    """多服务包应答:按偏移切段,逐条校验回显与状态后返回数据域。"""
    seg1 = bytes((0xCC, 0x00, 0x00, 0x00)) + b"\x01\x02\x03\x04"
    seg2 = bytes((0xCC, 0x00, 0x00, 0x00)) + b"\x05\x06"
    payload = (
        struct.pack("<H", 2)
        + struct.pack("<H", 6)
        + struct.pack("<H", 6 + len(seg1))
        + seg1
        + seg2
    )
    assert codec_cip.parse_multiple_service_payload(payload, [0x4C, 0x4C]) == [
        b"\x01\x02\x03\x04",
        b"\x05\x06",
    ]


def test_parse_multiple_service_payload_errors() -> None:
    """多服务包应答错误路径:条数不符/回显不符/内嵌状态非 0。"""
    seg = bytes((0xCC, 0x00, 0x00, 0x00)) + b"\x01"
    payload = struct.pack("<H", 2) + struct.pack("<H", 6) + seg * 2
    with pytest.raises(ValueError):
        codec_cip.parse_multiple_service_payload(payload, [0x4C])
    bad_echo = bytes((0xCD, 0x00, 0x00, 0x00)) + b"\x01"
    bad_payload = struct.pack("<H", 1) + struct.pack("<H", 4) + bad_echo
    with pytest.raises(ProtocolFrameError):
        codec_cip.parse_multiple_service_payload(bad_payload, [0x4C])
    status_seg = bytes((0xCC, 0x00, 0x05, 0x00))
    status_payload = struct.pack("<H", 1) + struct.pack("<H", 4) + status_seg
    with pytest.raises(DeviceError):
        codec_cip.parse_multiple_service_payload(status_payload, [0x4C])
