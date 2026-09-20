"""汇川 H3U/H5U 客户端测试:地址映射按手册逐项核对,TCP/RTU 金帧全链路。

映射表来源:H3U 手册 9.4.3(19010394)、H5U&Easy 手册 9.5.1(19011157)。
"""
from __future__ import annotations

import asyncio

import pytest

from omniplc import InovanceRtuClient, InovanceTcpClient, convert
from omniplc.aio import AInovanceRtuClient, AInovanceTcpClient
from omniplc.modbus import codec
from omniplc.plc.inovance.address import parse_inovance_address, to_modbus_address
from omniplc.types import WordOrder
from scripted import ScriptedTransport

_RESPONSE_ONE_REGISTER = bytes([3, 2, 0x00, 0x14])
_RESPONSE_ONE_COIL = bytes([1, 1, 1])


def _mount(monkeypatch: pytest.MonkeyPatch, client: object, scripted: ScriptedTransport) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


# ---------------------------------------------------------------- 地址映射


def test_parse_word_devices() -> None:
    """字软元件:D/SD/R/T/C → 保持寄存器偏移按手册基址。"""
    assert to_modbus_address("D100") == "hr100"
    assert to_modbus_address("SD10") == "hr9226"        # 0x2400 + 10
    assert to_modbus_address("R100") == "hr12388"       # 0x3000 + 100
    assert to_modbus_address("T5") == "hr61445"         # 0xF000 + 5(当前值)
    assert to_modbus_address("C150") == "hr62614"       # 0xF400 + 150(当前值)
    assert to_modbus_address("D100.3") == "hr100.3"


def test_parse_bit_devices() -> None:
    """位软元件(位访问):M/SM/S/T/C/X/Y/B → 线圈偏移按手册基址。"""
    assert to_modbus_address("M10", is_bool=True) == "c10"
    assert to_modbus_address("M8100", is_bool=True) == "c8100"   # M8000+ 从 0x1F40 连续
    assert to_modbus_address("SM10", is_bool=True) == "c9226"    # 0x2400 + 10
    assert to_modbus_address("S10", is_bool=True) == "c57354"    # 0xE000 + 10
    assert to_modbus_address("T5", is_bool=True) == "c61445"     # 0xF000 + 5(接点)
    assert to_modbus_address("C250", is_bool=True) == "c62714"   # 0xF400 + 250(接点)
    assert to_modbus_address("B10", is_bool=True) == "c12298"    # 0x3000 + 10


def test_octal_addressing_x_y() -> None:
    """X/Y 八进制编号:X17 = 15(十进制)、Y377 = 255。"""
    assert parse_inovance_address("X10").number == 8    # 八进制 10 = 8
    assert to_modbus_address("X10", is_bool=True) == "c63496"   # 0xF800 + 8
    assert to_modbus_address("X17", is_bool=True) == "c63503"   # 0xF800 + 15
    assert to_modbus_address("Y377", is_bool=True) == "c64767"  # 0xFC00 + 255


def test_invalid_addresses() -> None:
    """非法地址:越界、八进制含 8/9、位软元件带位号、C200+ 字访问、未知软元件。"""
    with pytest.raises(ValueError):
        to_modbus_address("X38", is_bool=True)      # 八进制不能有 8
    with pytest.raises(ValueError):
        to_modbus_address("M8512", is_bool=True)    # M 上限 8511
    with pytest.raises(ValueError):
        to_modbus_address("D8512")                  # D 上限 8511
    with pytest.raises(ValueError):
        to_modbus_address("S4096", is_bool=True)    # S 上限 4095
    with pytest.raises(ValueError):
        to_modbus_address("C200")                   # C200+ 为 32 位计数器,字访问不支持
    with pytest.raises(ValueError):
        to_modbus_address("M10.1", is_bool=True)    # 位软元件不支持位号后缀
    with pytest.raises(ValueError):
        to_modbus_address("D100.16")                # 位号 0~15
    with pytest.raises(ValueError):
        to_modbus_address("QX100")                  # AM 系列记号不在 H3U/H5U 范围
    with pytest.raises(ValueError):
        to_modbus_address("M10")                    # 纯位软元件不支持字访问
    with pytest.raises(ValueError):
        to_modbus_address("")


# ---------------------------------------------------------------- TCP 金帧


def test_tcp_read_d100(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 读 D100:等价读保持寄存器偏移 100,值正确返回。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_REGISTER)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    assert client.connect() is True
    assert client.read_ushort("D100") == (True, 20)
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, codec.build_read_pdu(3, 100, 1))


def test_tcp_read_m10_coil(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 读 M10:走线圈功能码 FC01,偏移为 M 基址 + 编号。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_COIL)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M10") == (True, True)
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, codec.build_read_pdu(1, 10, 1))


def test_tcp_write_bool_y(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 写 Y10:八进制换算后走 FC05 写线圈。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    pdu = codec.build_write_single_pdu(5, 0xFC00 + 8, 0xFF00)
    frame = codec.build_mbap(1, 1, pdu)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("Y10", True) is True
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, pdu)


def test_tcp_write_float_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 写 float 到 D200:FC16 写保持寄存器,寄存器序与 Modbus 路径一致。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    registers = list(convert.float32_to_registers(3.14, WordOrder.ABCD))
    pdu = codec.build_write_multi_pdu(16, 200, registers)
    frame = codec.build_mbap(1, 1, pdu)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_float("D200", 3.14) is True
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, pdu)


def test_tcp_defaults() -> None:
    """默认端口 502、站号 1;默认 IP 为 Easy 出厂段。"""
    client = InovanceTcpClient()
    assert client._ip_address == "192.168.1.88"
    assert client._port == 502
    assert client.station == 1


# ---------------------------------------------------------------- RTU


def test_rtu_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """RTU:配置串口后按汇川映射读 R100,CRC 帧与偏移正确。"""
    client = InovanceRtuClient(station=2)
    client.configure_serial("COM3")
    frame = codec.build_rtu_frame(2, _RESPONSE_ONE_REGISTER)
    scripted = ScriptedTransport([frame[:2], frame[2:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("R100") == (True, 20)
    assert bytes(scripted.sent) == codec.build_rtu_frame(2, codec.build_read_pdu(3, 12388, 1))


def test_rtu_configure_serial_defaults() -> None:
    """汇川串口缺省 9600-8N2:停止位默认 2。"""
    client = InovanceRtuClient(station=1)
    client.configure_serial("COM3")
    assert client._serial_config is not None
    assert client._serial_config.baud_rate == 9600
    assert client._serial_config.data_bits == 8
    assert client._serial_config.stop_bits == 2


# ---------------------------------------------------------------- 异步镜像


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 单工作线程往返;RTU configure_serial 对称暴露。"""

    async def scenario() -> None:
        tcp = AInovanceTcpClient("127.0.0.1", 502, 1)
        frame = codec.build_mbap(1, 1, _RESPONSE_ONE_REGISTER)
        scripted = ScriptedTransport([frame[:7], frame[7:]])
        monkeypatch.setattr(tcp._sync, "_create_transport", lambda: scripted)
        assert await tcp.connect() is True
        assert await tcp.read_ushort("D100") == (True, 20)
        await tcp.close()

        rtu = AInovanceRtuClient(station=1)
        rtu.configure_serial("COM3")
        sync = rtu._sync
        if not isinstance(sync, InovanceRtuClient):
            raise TypeError("内部错误:sync 实例不是 InovanceRtuClient")
        assert sync._serial_config is not None
        assert sync._serial_config.stop_bits == 2

    asyncio.run(scenario())
