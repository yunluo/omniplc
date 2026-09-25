"""客户端帧收发测试:脚本化传输(无网络)验证 TCP/RTU 全链路。

注入按脚本应答的假传输,验证:

- 请求字节与协议帧格式逐字节一致(MBAP 事务号/站号、RTU CRC)
- 响应正确解析出值
- 坏帧(事务号/站号/CRC 错)按"传输故障"处理:标记断开,触发惰性重连
- PLC 异常码(DeviceError)按"链路正常"处理:不断线、不重试
- 寄存器位写入的"读-改-写"两段事务
"""
from __future__ import annotations

import struct

import pytest

from omniplc import ModbusRtuClient, ModbusTcpClient
from omniplc.core.debug import format_hex
from omniplc.core.errors import DeviceError, ErrorCategory
from omniplc.modbus import codec
from scripted import ScriptedTransport as _ScriptedTransport, mount_real_tcp

# FC03 读 1 个寄存器、字节计数 2、值 20 的标准响应 PDU
_RESPONSE_ONE_REGISTER = bytes([3, 2, 0x00, 0x14])


def test_tcp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:请求组帧正确,响应解析出寄存器值。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.read_ushort("hr0") == (True, 20)
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, codec.build_read_pdu(3, 0, 1))


def test_tcp_transaction_id_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:事务号不匹配按坏帧处理,标记断开等待惰性重连;错误信息带收到的原始帧。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(99, 1, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "事务号" in client.last_error
    assert format_hex(frame) in client.last_error  # 原始字节供现场比对抓包


def test_tcp_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:PLC 异常码记录 last_error,不断线(链路是好的)。

    异常码原样落 ``last_error_code``(2),分类 DEVICE,``device_error_count``
    计数——供上位系统程序化区分"PLC 拒绝"与"链路故障"。
    """
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, bytes([0x83, 0x02]))
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "异常码 0x02" in client.last_error
    assert client.last_error_code == 2
    assert client.last_error_category is ErrorCategory.DEVICE
    assert client.stats["device_error_count"] == 1


def test_tcp_exception_function_code_mismatch_marks_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP:异常响应功能码与请求不符(错配/迟到帧)按坏帧处理。

    请求 FC03、回了 FC05|0x80:这不是本次请求的设备错误,须分类
    PROTOCOL(断线 + 重试),``last_error_code`` 不得被别的请求的异常码污染。
    """
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    pdu = bytes([0x85, 0x02])
    frame = codec.build_mbap(1, 1, pdu)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error_category is ErrorCategory.PROTOCOL
    assert client.last_error_code is None
    assert client.last_error is not None and "异常响应功能码不符" in client.last_error
    assert format_hex(pdu) in client.last_error  # 原始字节供现场比对抓包


def test_rtu_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:站号+PDU+CRC16 组帧正确,分段接收后解析出值。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    frame = codec.build_rtu_frame(1, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (True, 20)
    assert bytes(scripted.sent) == codec.build_rtu_frame(1, codec.build_read_pdu(3, 0, 1))


def test_rtu_crc_failure_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:CRC 校验失败按坏帧处理,标记断开;错误信息带收到的原始帧(逐字节可比对)。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    corrupted = bytearray(codec.build_rtu_frame(1, _RESPONSE_ONE_REGISTER))
    corrupted[-1] ^= 0xFF
    corrupted = bytes(corrupted)
    scripted = _ScriptedTransport([corrupted[:2], corrupted[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "CRC" in client.last_error
    # 现场排查要有原始字节:噪声误码 vs 收发错位靠帧内容区分
    assert format_hex(corrupted) in client.last_error


def test_rtu_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:异常响应(功能码|0x80)正确解析为 DeviceError,不断线。

    异常码原样落 ``last_error_code``,分类 DEVICE(与 TCP 口径一致)。
    """
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    frame = codec.build_rtu_frame(1, bytes([0x83, 0x02]))
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "异常码 0x02" in client.last_error
    assert client.last_error_code == 2
    assert client.last_error_category is ErrorCategory.DEVICE
    assert client.stats["device_error_count"] == 1


def test_rtu_exception_function_code_mismatch_marks_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RTU:异常响应功能码与请求不符(半双工迟到帧)按坏帧处理。

    RTU 只校验站号,同站号前一条请求(如 FC05 写)的异常回包晚到时,
    必须按坏帧断线重连,而不是当作本次 FC03 读的设备错误落码。
    """
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    pdu = bytes([0x85, 0x02])
    frame = codec.build_rtu_frame(1, pdu)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error_category is ErrorCategory.PROTOCOL
    assert client.last_error_code is None
    assert client.last_error is not None and "异常响应功能码不符" in client.last_error
    assert format_hex(pdu) in client.last_error


def test_rtu_station_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:响应站号与请求不符按坏帧处理。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    frame = codec.build_rtu_frame(2, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "站号" in client.last_error


def test_rtu_register_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:寄存器位写入 = 读(FC03)→改位→写(FC06)两段事务。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    read_frame = codec.build_rtu_frame(1, bytes([3, 2, 0x00, 0x04]))  # 寄存器值 0x0004
    write_pdu = codec.build_write_single_pdu(6, 0, 0x0005)  # 置位后 0x0005
    write_frame = codec.build_rtu_frame(1, write_pdu)
    scripted = _ScriptedTransport(
        [read_frame[:2], read_frame[2:], write_frame[:2], write_frame[2:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_bool("hr0.0", True) is True
    expected = codec.build_rtu_frame(1, codec.build_read_pdu(3, 0, 1)) + write_frame
    assert bytes(scripted.sent) == expected


def test_rtu_broadcast_write_skips_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU 广播:站号 0 写操作发送后不等响应(设备不回包),不消耗接收脚本。"""
    client = ModbusRtuClient(station=0)
    client.configure_serial("COM3")
    scripted = _ScriptedTransport([])  # 无应答分片:若等待响应将超时失败
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_ushort("hr100", 1234) is True
    assert bytes(scripted.sent) == codec.build_rtu_frame(0, codec.build_write_single_pdu(6, 100, 1234))


def test_rtu_broadcast_read_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU 广播:站号 0 读操作直接拒绝(设备不回包,等待只会超时)。"""
    client = ModbusRtuClient(station=0)
    client.configure_serial("COM3")
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_ushort("hr0")
    with pytest.raises(ValueError):
        client.read_bool("c0")


def test_tcp_station_zero_waits_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:无广播语义,站号 0 写照常等待响应(Unit ID 为路由字段)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 0)
    request_pdu = codec.build_write_single_pdu(6, 100, 1234)
    frame = codec.build_mbap(1, 0, request_pdu)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_ushort("hr100", 1234) is True
    assert bytes(scripted.sent) == codec.build_mbap(1, 0, request_pdu)


def test_mask_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:FC22 掩码写请求/回显响应全链路。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    request_pdu = codec.build_mask_write_pdu(100, 0x00F0, 0x0005)
    frame = codec.build_mbap(1, 1, request_pdu)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_mask_register("hr100", 0x00F0, 0x0005) is True
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, request_pdu)


def test_mask_write_rtu_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:FC22 按长度推算收包(expected_response_length=7)。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    request_pdu = codec.build_mask_write_pdu(0, 0xFFFF, 0x0001)
    frame = codec.build_rtu_frame(1, request_pdu)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_mask_register("hr0", 0xFFFF, 0x0001) is True
    assert codec.expected_response_length(request_pdu) == 7


def test_mask_write_echo_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:FC22 响应回显不符按坏帧处理(掩码写必须原样回显)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    bad_echo = codec.build_mask_write_pdu(100, 0x00F0, 0x0006)  # OR 掩码不符
    scripted = _ScriptedTransport(
        [codec.build_mbap(1, 1, bad_echo)[:7], codec.build_mbap(1, 1, bad_echo)[7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.write_mask_register("hr100", 0x00F0, 0x0005) is False
    assert client.connected is False
    assert client.last_error is not None and "回显" in client.last_error


def test_mask_write_invalid_args() -> None:
    """掩码写:非 hr 区域/位号后缀/掩码越界 → ValueError。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError):
        client.write_mask_register("ir0", 0xFFFF, 0)  # 输入寄存器不可写
    with pytest.raises(ValueError):
        client.write_mask_register("hr0.3", 0xFFFF, 0)  # 不支持位号
    with pytest.raises(ValueError):
        client.write_mask_register("hr0", 0x10000, 0)  # and_mask 越界
    with pytest.raises(ValueError):
        client.write_mask_register("hr0", 0, -1)  # or_mask 越界


def test_mbap_length_field_overflow() -> None:
    """MBAP:长度域超出上限按坏帧拒绝(防止按长收包挂死)。"""
    from omniplc.core.errors import ProtocolFrameError
    from omniplc.core.constants import MODBUS_MBAP_LENGTH_MAX

    bad_header = bytes([0, 1, 0, 0]) + (MODBUS_MBAP_LENGTH_MAX + 1).to_bytes(2, "big") + b"\x01"
    with pytest.raises(ProtocolFrameError) as exc_info:
        codec.parse_mbap_header(bad_header)
    assert "上限" in str(exc_info.value)


def test_async_mask_write_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:掩码写(FC22)经单工作线程往返。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        sync = client._sync
        request_pdu = codec.build_mask_write_pdu(100, 0x00F0, 0x0005)
        frame = codec.build_mbap(1, 1, request_pdu)
        scripted = _ScriptedTransport([frame[:7], frame[7:]])
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.write_mask_register("hr100", 0x00F0, 0x0005) is True
        assert bytes(scripted.sent) == codec.build_mbap(1, 1, request_pdu)
        await client.close()

    asyncio.run(scenario())


def test_device_error_instance_carries_code() -> None:
    """DeviceError.code 携带原始异常码(供上层程序化判断)。"""
    with pytest.raises(DeviceError) as exc_info:
        codec.check_response_exception(bytes([0x84, 0x03]), 4)
    assert exc_info.value.code == 3


def test_tcp_read_real_transport_semantics() -> None:
    """真 TcpTransport 凑满循环:响应小片到达仍能完整收包。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_REGISTER)
    mount_real_tcp(client, [frame[:2], frame[2:5], frame[5:]])
    assert client.read_ushort("hr0") == (True, 20)


def test_station_frozen_after_construction() -> None:
    """站号构造期定:属性只读(双入口取消),构造传参生效。"""
    client = ModbusTcpClient("127.0.0.1", station=2)
    assert client.station == 2
    with pytest.raises(AttributeError):
        client.station = 5
    with pytest.raises(ValueError):
        ModbusTcpClient("127.0.0.1", station=248)


# ----------------------------------------------------------------------
# 批量读(按 FC+类型分组合并连续地址)
# ----------------------------------------------------------------------


def _mbap_response(transaction_id: int, station: int, response_pdu: bytes) -> bytes:
    """构造 MBAP 包裹的响应帧(测试夹具)。"""
    return codec.build_mbap(transaction_id, station, response_pdu)


def _fc03_response(values: list) -> bytes:
    """构造 FC 03/04 读 N 字的响应 PDU(测试夹具)。"""
    body = b"".join(int(v).to_bytes(2, "big") for v in values)
    return bytes([3, len(body)]) + body


def _fc01_response(bits: list) -> bytes:
    """构造 FC 01/02 读 N 位的响应 PDU(测试夹具)。

    按 Modbus 协议位序:LSB first,每字节低→高位对应低位→高位地址。
    """
    n = len(bits)
    byte_count = (n + 7) // 8
    out = bytearray(byte_count)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (index % 8)
    return bytes([1, byte_count]) + bytes(out)


def test_tcp_read_many_coalesces_short_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_many:3 个相邻 SHORT 合并为 1 笔 FC 03 读 3 字(事务数最少化)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # transaction_id 由 ModbusBaseClient 自增,首次为 1
    response = _mbap_response(1, 1, _fc03_response([100, 200, 300]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["hr10", "hr11", "hr12"], "short")
    assert results == [(True, 100), (True, 200), (True, 300)]
    # 验证只发了 1 笔 FC 03,起始 hr10 读 3 字
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, codec.build_read_pdu(3, 10, 3))


def test_tcp_read_many_non_contiguous_two_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_many:地址空洞强制拆 2 笔 FC 03(协议只能读连续地址)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 第一次 FC 03 读 hr100~102(3 字),第二次读 hr200~201(2 字)
    responses = [
        _mbap_response(1, 1, _fc03_response([10, 20, 30])),
        _mbap_response(2, 1, _fc03_response([40, 50])),
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["hr100", "hr101", "hr102", "hr200", "hr201"], "short")
    assert results == [(True, 10), (True, 20), (True, 30), (True, 40), (True, 50)]


def test_tcp_read_many_coil_bits_coalesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_many:位设备多个位连续相邻合并为 1 笔 FC 01,c0/c1/c2 → 读 3 位。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc01_response([1, 0, 1]))  # bits 0,1,2
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["c0", "c1", "c2"], "bool")
    assert results == [(True, True), (True, False), (True, True)]


def test_tcp_read_many_coil_bits_with_gap_two_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_many:位设备地址空洞强制拆 2 笔 FC 01(c0/c1/c2 + c5 留空洞 c3/c4)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    responses = [
        _mbap_response(1, 1, _fc01_response([1, 0, 1])),  # FC 01 c0~c2
        _mbap_response(2, 1, _fc01_response([1])),  # FC 01 c5
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["c0", "c1", "c2", "c5"], "bool")
    assert results == [(True, True), (True, False), (True, True), (True, True)]


def test_tcp_read_many_register_bits_same_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_many:寄存器多位同字合并 1 笔 FC 03 读 1 字,提两位。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 0x000A = bit3=1, bit1=0(实际 bit3=1,bit1=0;这里测试要的是 bit3=1,bit1=1 → 0x000A)
    # bit3 mask = 0x0008, bit1 mask = 0x0002 → 0x000A
    response = _mbap_response(1, 1, _fc03_response([0x000A]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["hr0.3", "hr0.1"], "bool")
    assert results == [(True, True), (True, True)]
    # 验证只发了 1 笔 FC 03,起始 hr0 读 1 字
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, codec.build_read_pdu(3, 0, 1))


def test_tcp_read_many_register_bits_two_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_many:寄存器位跨字合并 1 笔 FC 03 读 2 字。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc03_response([0x0008, 0x0080]))  # hr0.3=1, hr1.7=1
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["hr0.3", "hr1.7"], "bool")
    assert results == [(True, True), (True, True)]
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, codec.build_read_pdu(3, 0, 2))


def test_tcp_read_batch_different_widths_no_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_batch:不同宽度不合并(SHORT 1 字 + INT 2 字) → 2 笔 FC 03。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 第一笔 FC 03 读 hr100(1 字)=0x000A → SHORT 10
    # 第二笔 FC 03 读 hr101~102(2 字)=0x1234 0x5678 → INT(ABCD)=0x12345678=305419896
    responses = [
        _mbap_response(1, 1, _fc03_response([0x000A])),
        _mbap_response(2, 1, _fc03_response([0x1234, 0x5678])),
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, values = client.read_batch([("hr100", "short"), ("hr101", "int")])
    assert ok is True
    assert values == [10, 305419896]


def test_tcp_read_many_mixed_fc_areas(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_many:跨区(线圈 + 寄存器) → FC 01 + FC 03 各一笔。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    responses = [
        _mbap_response(1, 1, _fc01_response([1])),  # FC 01 读 c5=1
        _mbap_response(2, 1, _fc03_response([0x0001])),  # FC 03 读 hr100(bit 0)=1
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["c5", "hr100"], "bool")
    # 注意:c5 是线圈位访问(BOOL+COIL 走 FC 01),hr100 是寄存器位访问(BOOL+HR 走 FC 03)
    # 两者 area 不同 → 不同 FC
    assert results == [(True, True), (True, True)]


def test_tcp_read_many_preserves_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_many:打乱输入顺序,输出与输入顺序对应。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc03_response([10, 20, 30]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    # 输入:hr102, hr100, hr101(乱序)→ 输出应按输入顺序:[30, 10, 20]
    # FC 03 响应 [10, 20, 30] 对应 hr100/101/102 的字数据
    results = client.read_many(["hr102", "hr100", "hr101"], "short")
    assert results == [(True, 30), (True, 10), (True, 20)]


def test_tcp_read_many_failure_marks_all_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_many:任一笔 FC 失败 → 整批失败(全部 (False, None))。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 第二次响应给坏 PDU,模拟事务号不匹配
    bad_response = _mbap_response(99, 1, _fc03_response([10, 20, 30]))
    scripted = _ScriptedTransport(
        [
            _mbap_response(1, 1, _fc03_response([10, 20, 30]))[:7],
            _mbap_response(1, 1, _fc03_response([10, 20, 30]))[7:],
            bad_response[:7],
            bad_response[7:],
        ]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.read_many(["hr100", "hr101", "hr102", "hr200", "hr201"], "short")
    assert results == [(False, None)] * 5
    assert client.connected is False


def test_tcp_read_batch_empty_raises() -> None:
    """read_batch:空列表 → ValueError(对齐 MC/FINS 契约)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError):
        client.read_batch([])


def test_tcp_read_many_bad_address_raises_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_many:非法地址同步 ValueError,不进入事务锁(零字节发送)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    scripted = _ScriptedTransport([])  # 空响应,期望零调用
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_many(["hr10", "INVALID@"], "short")
    assert len(bytes(scripted.sent)) == 0  # 零字节发送(地址解析失败不入事务锁)


def test_tcp_read_batch_mixed_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_batch:混类型完整覆盖(线圈位 + 寄存器位 + SHORT + INT)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 4 笔 FC 事务(组数):
    #   1. FC 01 c0(1 位,线圈)
    #   2. FC 03 hr100(1 字,寄存器位)
    #   3. FC 03 hr110(1 字,SHORT)
    #   4. FC 03 hr120~hr121(2 字,INT)
    responses = [
        _mbap_response(1, 1, _fc01_response([1])),
        _mbap_response(2, 1, _fc03_response([0x0008])),  # bit3=1
        _mbap_response(3, 1, _fc03_response([100])),  # hr110=100
        _mbap_response(4, 1, _fc03_response([0x1234, 0x5678])),  # hr120~121=INT 0x12345678
    ]
    flat = []
    for resp in responses:
        flat += [resp[:7], resp[7:]]
    scripted = _ScriptedTransport(flat)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, values = client.read_batch(
        [("c0", "bool"), ("hr100.3", "bool"), ("hr110", "short"), ("hr120", "int")]
    )
    assert ok is True
    assert values == [True, True, 100, 305419896]


def test_async_read_many_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:read_many 经单工作线程驱动同步版(连续合并路径)。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _fc03_response([10, 20, 30]))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        results = await client.read_many(["hr0", "hr1", "hr2"], "short")
        assert results == [(True, 10), (True, 20), (True, 30)]
        await client.close()

    asyncio.run(scenario())


def test_async_read_batch_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:read_batch 经单工作线程驱动同步版。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _fc03_response([10, 20]))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, values = await client.read_batch([("hr0", "short"), ("hr1", "short")])
        assert ok is True
        assert values == [10, 20]
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 批量写(按 FC+类型分组合并连续地址)
# ----------------------------------------------------------------------


def _fc15_response(offset: int, count: int) -> bytes:
    """构造 FC 15 写多线圈的响应 PDU(测试夹具):回显 offset + count。"""
    return struct.pack(">BHH", 15, offset, count)


def _fc16_response(offset: int, count: int) -> bytes:
    """构造 FC 16 写多寄存器的响应 PDU(测试夹具):回显 offset + count。"""
    return struct.pack(">BHH", 16, offset, count)


def _fc03_read_response(values: list) -> bytes:
    """构造 FC 03 读 N 字的响应 PDU(寄存器位 RMW 用)。"""
    body = b"".join(int(v).to_bytes(2, "big") for v in values)
    return bytes([3, len(body)]) + body


def test_tcp_write_many_coalesces_short_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_many:3 邻接 SHORT 合 1 笔 FC 16 写 3 字。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc16_response(100, 3))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many(
        [("hr100", "short", 10), ("hr101", "short", 20), ("hr102", "short", 30)]
    )
    assert results == [True, True, True]
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, codec.build_write_multi_pdu(16, 100, [10, 20, 30]))


def test_tcp_write_many_non_contiguous_two_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_many:地址空洞强制拆 2 笔 FC 16(协议只能写连续地址)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    responses = [
        _mbap_response(1, 1, _fc16_response(100, 3)),  # FC 16 hr100~102
        _mbap_response(2, 1, _fc16_response(200, 2)),  # FC 16 hr200~201
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many(
        [
            ("hr100", "short", 10),
            ("hr101", "short", 20),
            ("hr102", "short", 30),
            ("hr200", "short", 40),
            ("hr201", "short", 50),
        ]
    )
    assert results == [True, True, True, True, True]


def test_tcp_write_many_coil_bits_coalesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_many:3 邻接线圈位合 1 笔 FC 15 写 3 位。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc15_response(0, 3))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many(
        [("c0", "bool", True), ("c1", "bool", False), ("c2", "bool", True)]
    )
    assert results == [True, True, True]
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, codec.build_write_multi_pdu(15, 0, [1, 0, 1]))


def test_tcp_write_many_different_widths_no_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_batch:不同宽度不合并(SHORT 1 字 + INT 2 字) → 2 笔 FC 16。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    responses = [
        _mbap_response(1, 1, _fc16_response(100, 1)),  # FC 16 hr100 (1 字)
        _mbap_response(2, 1, _fc16_response(101, 2)),  # FC 16 hr101~102 (2 字)
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, results = client.write_batch(
        [("hr100", "short", 10), ("hr101", "int", 0x12345678)]
    )
    assert ok is True
    assert results == [True, True]


def test_tcp_write_batch_mixed_fc_areas(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_batch:跨 FC(线圈 + 寄存器) → FC 15 + FC 16 各一笔。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    responses = [
        _mbap_response(1, 1, _fc15_response(0, 1)),  # FC 15 c0
        _mbap_response(2, 1, _fc16_response(100, 1)),  # FC 16 hr100
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, results = client.write_batch([("c0", "bool", True), ("hr100", "short", 10)])
    assert ok is True
    assert results == [True, True]


def test_tcp_write_many_register_bit_rmw(monkeypatch: pytest.MonkeyPatch) -> None:
    """write_many:寄存器位(``hr0.3``)走"读-改-写"两段事务(FC 03 + FC 06),不入合并。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # RMW 第一笔:FC 03 读 hr0 → 0x0000
    # RMW 第二笔:FC 06 写 hr0 → 0x0008(bit3=1)
    responses = [
        _mbap_response(1, 1, _fc03_read_response([0x0000])),
        _mbap_response(2, 1, bytes([6, 0, 0, 0, 8])),  # FC 06 echo offset=0 value=0x0008
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many([("hr0.3", "bool", True)])
    assert results == [True]


def test_tcp_write_many_register_bit_then_full_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_many:同一批次内 ``hr0.3`` RMW 先,``hr0=100`` FC 16 后 → hr0 最终值由 FC 16 决定(协议层竞态,与基类一致)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 顺序按入参:hr0.3 先 → hr0 后
    # 但实现按 offset 升序:RMW(hr0.3 offset=0) 先 → FC 16 hr0 后
    # RMW:FC 03 读 hr0 → 0x0000;FC 06 写 hr0 → 0x0008
    # FC 16:写 hr0 → 100 (0x0064)
    responses = [
        _mbap_response(1, 1, _fc03_read_response([0x0000])),  # RMW FC 03
        _mbap_response(2, 1, bytes([6, 0, 0, 0, 8])),  # RMW FC 06
        _mbap_response(3, 1, _fc16_response(0, 1)),  # FC 16 hr0
    ]
    scripted = _ScriptedTransport(
        [
            responses[0][:7], responses[0][7:],
            responses[1][:7], responses[1][7:],
            responses[2][:7], responses[2][7:],
        ]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many([("hr0.3", "bool", True), ("hr0", "short", 100)])
    assert results == [True, True]


def test_tcp_write_batch_failure_marks_all_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_batch:任一 FC 失败 → ``(False, None)``(整批失败)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    # 第二次响应给坏 PDU(功能码 0x90 是异常码 16 + 0x80),触发坏帧
    responses = [
        _mbap_response(1, 1, _fc16_response(100, 3)),  # OK
        _mbap_response(2, 1, b"\x90\x01"),  # FC 16 异常码 1
    ]
    scripted = _ScriptedTransport(
        [responses[0][:7], responses[0][7:], responses[1][:7], responses[1][7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, results = client.write_batch(
        [
            ("hr100", "short", 10),
            ("hr101", "short", 20),
            ("hr102", "short", 30),
            ("hr200", "short", 40),
            ("hr201", "short", 50),
        ]
    )
    assert ok is False
    assert results is None


def test_tcp_write_many_bad_address_raises_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_many:非法地址同步 ValueError,不进入事务锁(零字节发送)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.write_many([("hr10", "short", 100), ("INVALID@", "short", 200)])
    assert len(bytes(scripted.sent)) == 0


def test_tcp_write_batch_empty_raises() -> None:
    """write_batch:空列表 → ValueError。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError):
        client.write_batch([])


def test_async_write_many_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:write_many 经单工作线程驱动同步版(连续合并路径)。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _fc16_response(0, 3))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        results = await client.write_many(
            [("hr0", "short", 10), ("hr1", "short", 20), ("hr2", "short", 30)]
        )
        assert results == [True, True, True]
        await client.close()

    asyncio.run(scenario())


def test_async_write_batch_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:write_batch 经单工作线程驱动同步版。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _fc16_response(0, 2))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, results = await client.write_batch(
            [("hr0", "short", 10), ("hr1", "short", 20)]
        )
        assert ok is True
        assert results == [True, True]
        await client.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# FC 23 读写多寄存器(单事务"先写后读")
# ----------------------------------------------------------------------


def _fc23_response(values: list) -> bytes:
    """构造 FC 23 读响应 PDU(测试夹具):字节计数 = 2 × 读数量。"""
    body = b"".join(int(v).to_bytes(2, "big") for v in values)
    return bytes([0x17, len(body)]) + body


def test_tcp_read_write_registers_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC23:请求帧逐字节一致,响应解析出写入生效后的读值。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _fc23_response([0x00FE, 0x0ACD, 0x0001]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, values = client.read_write_registers("hr3", 3, "hr14", [0x00FF, 0x00FF])
    assert ok is True
    assert values == [0x00FE, 0x0ACD, 0x0001]
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_write_registers_pdu(3, 3, 14, [0x00FF, 0x00FF])
    )


def test_rtu_read_write_registers_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC23(RTU):按请求读数量推算收包长度,CRC 校验通过。"""
    client = ModbusRtuClient(1)
    pdu = _fc23_response([7, 9])
    frame = codec.build_rtu_frame(1, pdu)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, values = client.read_write_registers("hr0", 2, "hr10", [1])
    assert ok is True
    assert values == [7, 9]


def test_read_write_registers_rejects() -> None:
    """FC23 前置校验:仅保持寄存器、无位号后缀、数量与值非法、广播站号拒绝。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError):
        client.read_write_registers("ir0", 1, "hr0", [1])  # 输入寄存器不可写
    with pytest.raises(ValueError):
        client.read_write_registers("hr0.3", 1, "hr0", [1])  # 位号后缀
    with pytest.raises(ValueError):
        client.read_write_registers("hr0", 1, "hr0.3", [1])
    with pytest.raises(ValueError):
        client.read_write_registers("hr0", 0, "hr0", [1])  # 读数量 0
    with pytest.raises(ValueError):
        client.read_write_registers("hr0", 1, "hr0", [0] * 122)  # 写数量超 121
    broadcast = ModbusRtuClient(0)
    with pytest.raises(ValueError):
        broadcast.read_write_registers("hr0", 1, "hr0", [1])


# ----------------------------------------------------------------------
# FC 43/14 读设备标识
# ----------------------------------------------------------------------


def _device_id_response(objects: list, more_follows: int = 0, next_id: int = 0) -> bytes:
    """构造读设备标识响应 PDU(测试夹具,符合级别 0x81)。"""
    body = bytearray([0x2B, 0x0E, 0x01, 0x81, more_follows, next_id, len(objects)])
    for object_id, raw in objects:
        body += bytes([object_id, len(raw)]) + raw
    return bytes(body)


def _device_id_rtu_chunks(frame: bytes) -> list:
    """把 FC43 RTU 响应帧切成增量收包所需的 recv 分片(测试夹具)。

    与 ``_recv_device_id_tail`` 的读取节奏一一对应:帧头(2)+ 固定头(6)
    + 每个对象(2 + 长度)+ CRC(2)。
    """
    chunks = [frame[0:2], frame[2:8]]
    cursor = 8
    for _ in range(frame[7]):
        length = frame[cursor + 1]
        chunks.append(frame[cursor:cursor + 2])
        chunks.append(frame[cursor + 2:cursor + 2 + length])
        cursor += 2 + length
    chunks.append(frame[cursor:])
    return chunks


def test_tcp_read_device_id_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC43 基本标识:单页三对象映射为规范对象名,请求逐字节一致。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(
        1,
        1,
        _device_id_response(
            [(0x00, b"ACME"), (0x01, b"MDL-1"), (0x02, b"V2.11")]
        ),
    )
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, info = client.read_device_id()
    assert ok is True
    assert info == {
        "vendor_name": "ACME",
        "product_code": "MDL-1",
        "major_minor_revision": "V2.11",
    }
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_device_id_pdu(0x01, 0x00)
    )


def test_tcp_read_device_id_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC43 翻页:首页 MoreFollows=FF 自动续读,两页对象合并为一份结果。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    page_one = _mbap_response(
        1, 1, _device_id_response([(0x00, b"ACME")], more_follows=0xFF, next_id=0x01)
    )
    page_two = _mbap_response(
        2, 1, _device_id_response([(0x01, b"MDL-1"), (0x02, b"V2.11")])
    )
    scripted = _ScriptedTransport(
        [page_one[:7], page_one[7:], page_two[:7], page_two[7:]]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, info = client.read_device_id()
    assert ok is True
    assert info == {
        "vendor_name": "ACME",
        "product_code": "MDL-1",
        "major_minor_revision": "V2.11",
    }


def test_tcp_read_device_id_private_object_naming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FC43 厂商私有对象(0x80~0xFF)用 object_0xNN 兜底命名。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(
        1, 1, _device_id_response([(0x00, b"ACME"), (0x80, b"\xff\xfe")])
    )
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, info = client.read_device_id("extended")
    assert ok is True and info is not None
    assert info["vendor_name"] == "ACME"
    assert info["object_0x80"] == "\ufffd\ufffd"  # 非 ASCII 字节按替换字符解码,不失败


def test_rtu_read_device_id_incremental_recv(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC43(RTU):响应长度随对象变化,按对象头增量收包后 CRC 校验通过。"""
    client = ModbusRtuClient(1)
    frame = codec.build_rtu_frame(
        1,
        _device_id_response(
            [(0x00, b"ACME"), (0x01, b"MDL-1"), (0x02, b"V2.11")]
        ),
    )
    scripted = _ScriptedTransport(_device_id_rtu_chunks(frame))
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, info = client.read_device_id("basic")
    assert ok is True
    assert info == {
        "vendor_name": "ACME",
        "product_code": "MDL-1",
        "major_minor_revision": "V2.11",
    }


def test_read_device_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC43 个体访问(读取码 04):返回请求对象的原始字节。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, _device_id_response([(0x02, b"V2.11")]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, raw = client.read_device_object(0x02)
    assert ok is True and raw == b"V2.11"
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_device_id_pdu(0x04, 0x02)
    )


def test_read_device_id_rejects() -> None:
    """FC43 前置校验:层次名/编号非法、个体访问用错方法、对象号越界。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError):
        client.read_device_id("bogus")
    with pytest.raises(ValueError):
        client.read_device_id(4)  # 个体访问应走 read_device_object
    with pytest.raises(ValueError):
        client.read_device_id(9)
    with pytest.raises(ValueError):
        client.read_device_object(0x100)
    broadcast = ModbusRtuClient(0)
    with pytest.raises(ValueError):
        broadcast.read_device_id()


def test_read_device_id_device_error_keeps_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FC43 设备不支持(异常码 01):记 last_error 但不断线。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    response = _mbap_response(1, 1, bytes([0xAB, 0x01]))
    scripted = _ScriptedTransport([response[:7], response[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, info = client.read_device_id()
    assert ok is False and info is None
    assert client.connected is True
    assert client.last_error is not None and "ILLEGAL FUNCTION" in client.last_error


def test_read_address_span_over_space_via_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """客户端层越界:32 位读 hr65535 占 2 字越过地址空间顶端,组帧期拒绝且零字节发送。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_int("hr65535")
    assert len(bytes(scripted.sent)) == 0


def test_async_read_write_registers_aio_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步镜像:read_write_registers 经单工作线程驱动同步版。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _fc23_response([7, 9]))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, values = await client.read_write_registers("hr0", 2, "hr10", [1])
        assert ok is True and values == [7, 9]
        await client.close()

    asyncio.run(scenario())


def test_async_read_device_id_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:read_device_id 经单工作线程驱动同步版。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _device_id_response([(0x00, b"ACME")]))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, info = await client.read_device_id()
        assert ok is True and info == {"vendor_name": "ACME"}
        await client.close()

    asyncio.run(scenario())


def test_async_read_device_object_aio_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步镜像:read_device_object 经单工作线程驱动同步版。"""
    import asyncio

    from omniplc.aio import AModbusTcpClient

    async def scenario() -> None:
        client = AModbusTcpClient("127.0.0.1", 502, 1)
        response = _mbap_response(1, 1, _device_id_response([(0x01, b"MDL-1")]))
        scripted = _ScriptedTransport([response[:7], response[7:]])
        monkeypatch.setattr(client._sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        ok, raw = await client.read_device_object(0x01)
        assert ok is True and raw == b"MDL-1"
        await client.close()

    asyncio.run(scenario())
