"""客户端帧收发测试:脚本化传输(无网络)验证 TCP/UDP/RTU 全链路。

注入按脚本应答的假传输,验证:

- 请求字节与协议帧格式逐字节一致(MBAP 事务号/站号、RTU CRC)
- 响应正确解析出值
- 坏帧(事务号/站号/CRC 错)按"传输故障"处理:标记断开,触发惰性重连
- PLC 异常码(DeviceError)按"链路正常"处理:不断线、不重试
- 寄存器位写入的"读-改-写"两段事务
"""
from __future__ import annotations

from typing import List

import pytest

from omniplc import ModbusRtuClient, ModbusTcpClient, ModbusUdpClient
from omniplc.core.errors import DeviceError
from omniplc.modbus import codec
from omniplc.transport.base import BaseTransport

# FC03 读 1 个寄存器、字节计数 2、值 20 的标准响应 PDU
_RESPONSE_ONE_REGISTER = bytes([3, 2, 0x00, 0x14])


class _ScriptedTransport(BaseTransport):
    """按脚本应答的假传输:send 记录请求,recv 按序返回预置分片。

    TCP 用法:把完整应答帧按 recv 尺寸切成多个分片(如 ``frame[:7]``);
    UDP 用法:单个分片即整个数据报。
    """

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent = bytearray()

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, size: int) -> bytes:
        if not self._chunks:
            raise ConnectionError("脚本分片已耗尽")
        return self._chunks.pop(0)


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
    """TCP:事务号不匹配按坏帧处理,标记断开等待惰性重连。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(99, 1, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "事务号" in client.last_error


def test_tcp_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP:PLC 异常码记录 last_error,不断线(链路是好的)。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, bytes([0x83, 0x02]))
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "异常码 0x02" in client.last_error


def test_udp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:一请求一数据报,整包校验。"""
    client = ModbusUdpClient("127.0.0.1", 502, 2)
    frame = codec.build_mbap(1, 2, _RESPONSE_ONE_REGISTER)
    scripted = _ScriptedTransport([frame])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (True, 20)
    assert bytes(scripted.sent) == codec.build_mbap(1, 2, codec.build_read_pdu(3, 0, 1))


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
    """RTU:CRC 校验失败按坏帧处理,标记断开。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    corrupted = bytearray(codec.build_rtu_frame(1, _RESPONSE_ONE_REGISTER))
    corrupted[-1] ^= 0xFF
    scripted = _ScriptedTransport([bytes(corrupted[:2]), bytes(corrupted[2:])])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "CRC" in client.last_error


def test_rtu_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:异常响应(功能码|0x80)正确解析为 DeviceError,不断线。"""
    client = ModbusRtuClient(station=1)
    client.configure_serial("COM3")
    frame = codec.build_rtu_frame(1, bytes([0x83, 0x02]))
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.read_ushort("hr0") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "异常码 0x02" in client.last_error


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


def test_device_error_instance_carries_code() -> None:
    """DeviceError.code 携带原始异常码(供上层程序化判断)。"""
    with pytest.raises(DeviceError) as exc_info:
        codec.check_response_exception(bytes([0x84, 0x03]), 4)
    assert exc_info.value.code == 3
