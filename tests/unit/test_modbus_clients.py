"""客户端帧收发测试:脚本化传输(无网络)验证 TCP/RTU 全链路。

注入按脚本应答的假传输,验证:

- 请求字节与协议帧格式逐字节一致(MBAP 事务号/站号、RTU CRC)
- 响应正确解析出值
- 坏帧(事务号/站号/CRC 错)按"传输故障"处理:标记断开,触发惰性重连
- PLC 异常码(DeviceError)按"链路正常"处理:不断线、不重试
- 寄存器位写入的"读-改-写"两段事务
"""
from __future__ import annotations

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
