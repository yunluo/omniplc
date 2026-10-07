"""汇川 H3U/H5U 客户端测试:地址映射按手册逐项核对,TCP/RTU 金帧全链路。

映射表来源:H3U 手册 9.4.3(19010394)、H5U&Easy 手册 9.5.1(19011157)。
"""

from __future__ import annotations

import asyncio

import pytest

from omniplc import InovanceRtuClient, InovanceTcpClient
from omniplc.aio import AInovanceRtuClient, AInovanceTcpClient
from omniplc.core import convert
from omniplc.plc.modbus import codec
from omniplc.plc.inovance.address import parse_inovance_address, to_modbus_address
from omniplc.core.types import WordOrder
from scripted import ScriptedTransport

_RESPONSE_ONE_REGISTER = bytes([3, 2, 0x00, 0x14])
_RESPONSE_TWO_REGISTERS = bytes([3, 4, 0x00, 0x0D, 0x00, 0x04])
_RESPONSE_ONE_COIL = bytes([1, 1, 1])


def _mount(
    monkeypatch: pytest.MonkeyPatch, client: object, scripted: ScriptedTransport
) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


# ---------------------------------------------------------------- 地址映射


def test_parse_word_devices() -> None:
    """字软元件:D/SD/R/T/C → 保持寄存器偏移按手册基址。"""
    assert to_modbus_address("D100") == "hr100"
    assert to_modbus_address("SD10") == "hr9226"  # 0x2400 + 10
    assert to_modbus_address("R100") == "hr12388"  # 0x3000 + 100
    assert to_modbus_address("T5") == "hr61445"  # 0xF000 + 5(当前值)
    assert to_modbus_address("C150") == "hr62614"  # 0xF400 + 150(当前值)
    assert to_modbus_address("D100.3") == "hr100.3"


def test_parse_bit_devices() -> None:
    """位软元件(位访问):M/SM/S/T/C/X/Y/B → 线圈偏移按手册基址。"""
    assert to_modbus_address("M10", is_bool=True) == "c10"
    assert to_modbus_address("M8100", is_bool=True) == "c8100"  # M8000+ 从 0x1F40 连续
    assert to_modbus_address("SM10", is_bool=True) == "c9226"  # 0x2400 + 10
    assert to_modbus_address("S10", is_bool=True) == "c57354"  # 0xE000 + 10
    assert to_modbus_address("T5", is_bool=True) == "c61445"  # 0xF000 + 5(接点)
    assert to_modbus_address("C250", is_bool=True) == "c62714"  # 0xF400 + 250(接点)
    assert to_modbus_address("B10", is_bool=True) == "c12298"  # 0x3000 + 10


def test_octal_addressing_x_y() -> None:
    """X/Y 八进制编号:X17 = 15(十进制)、Y377 = 255。"""
    assert parse_inovance_address("X10").number == 8  # 八进制 10 = 8
    assert to_modbus_address("X10", is_bool=True) == "c63496"  # 0xF800 + 8
    assert to_modbus_address("X17", is_bool=True) == "c63503"  # 0xF800 + 15
    assert to_modbus_address("Y377", is_bool=True) == "c64767"  # 0xFC00 + 255


def test_xy_extended_range_h5u() -> None:
    """X/Y 上限取 H5U 口径(X0~X1777 = 1024 点,H5U 9.5.1 印刷页 419)。

    H3U 表只到 X/Y377(256 点),超出部分在 H3U 上落地址空洞由 PLC 报异常,
    故按宽口径放行不影响 H3U 现场使用。
    """
    assert parse_inovance_address("X1000").number == 512  # 八进制 1000 = 512
    assert to_modbus_address("X1000", is_bool=True) == "c64000"  # 0xF800 + 512
    assert to_modbus_address("X1777", is_bool=True) == "c64511"  # 0xF800 + 1023
    assert to_modbus_address("Y1777", is_bool=True) == "c65535"  # 0xFC00 + 1023
    with pytest.raises(ValueError):
        to_modbus_address("Y2000", is_bool=True)  # 八进制 2000 = 1024 超上限


def test_c32_counter_word_mapping() -> None:
    """C200~C255 32 位计数器:0xF700 起、每只占两个 16 位寄存器(H3U 9.4.3 印刷页 575)。"""
    assert to_modbus_address("C199") == "hr62663"  # 0xF400 + 199(16 位当前值段)
    assert to_modbus_address("C200") == "hr63232"  # 0xF700 + 0
    assert to_modbus_address("C205") == "hr63242"  # 0xF700 + 10(手册算例 0xF70A)
    assert to_modbus_address("C255") == "hr63342"  # 0xF700 + 110
    with pytest.raises(ValueError):
        to_modbus_address("C256")  # 上限 C255
    with pytest.raises(ValueError):
        to_modbus_address("C205.3")  # 32 位计数器无位号定义
    assert to_modbus_address("C205", is_bool=True) == "c62669"  # 接点位仍走线圈


def test_tcp_read_uint_c32_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """读 32 位计数器 C200:FC03 读 2 个寄存器(0xF700 双寄存器展开)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_TWO_REGISTERS)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_uint("C200") == (True, 0x000D0004)
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_pdu(3, 63232, 2)
    )


def test_tcp_write_uint_c32_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """写 32 位计数器 C255 走 FC16 双寄存器(手册注明 32 位寄存器不支持 FC06)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    registers = list(convert.uint32_to_registers(0x00010000, WordOrder.ABCD))
    pdu = codec.build_write_multi_pdu(16, 63342, registers)
    frame = codec.build_mbap(1, 1, pdu[:5])
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_uint("C255", 0x00010000) is True
    sent = bytes(scripted.sent)
    assert sent == codec.build_mbap(1, 1, pdu)
    assert sent[7] == 16  # FC16(非 FC06)


def test_c32_counter_type_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """类型门控:16 位/字符串访问 32 位计数器入参期拒绝,零字节发送。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError):
        client.read_ushort("C205")
    with pytest.raises(ValueError):
        client.write_ushort("C205", 1)
    with pytest.raises(ValueError):
        client.read_string("C205", 2)
    assert bytes(scripted.sent) == b""


def test_invalid_addresses() -> None:
    """非法地址:越界、八进制含 8/9、位软元件带位号、未知软元件。"""
    with pytest.raises(ValueError):
        to_modbus_address("X38", is_bool=True)  # 八进制不能有 8
    with pytest.raises(ValueError):
        to_modbus_address("M8512", is_bool=True)  # M 上限 8511
    with pytest.raises(ValueError):
        to_modbus_address("D8512")  # D 上限 8511
    with pytest.raises(ValueError):
        to_modbus_address("S4096", is_bool=True)  # S 上限 4095
    with pytest.raises(ValueError):
        to_modbus_address("C256", is_bool=True)  # C 接点上限 C255
    with pytest.raises(ValueError):
        to_modbus_address("M10.1", is_bool=True)  # 位软元件不支持位号后缀
    with pytest.raises(ValueError):
        to_modbus_address("D100.16")  # 位号 0~15
    with pytest.raises(ValueError):
        to_modbus_address("QX100")  # AM 系列记号不在 H3U/H5U 范围
    with pytest.raises(ValueError):
        to_modbus_address("M10")  # 纯位软元件不支持字访问
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
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_pdu(3, 100, 1)
    )


def test_tcp_read_m10_coil(monkeypatch: pytest.MonkeyPatch) -> None:
    """TCP 读 M10:走线圈功能码 FC01,偏移为 M 基址 + 编号。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_COIL)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("M10") == (True, True)
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_pdu(1, 10, 1)
    )


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
    # 规范 §6.12(印刷页 30):FC16 正常响应 = 请求前 5 字节回显(FC+地址+数量),
    # 不含数据域
    frame = codec.build_mbap(1, 1, pdu[:5])
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


# ------------------------------------------------------ 批量双记号(根治批)

_FC16_ECHO = 5  # FC16 正常响应 = 请求前 5 字节回显(规范 §6.12)
_FC15_ECHO = 5  # FC15 同口径


def test_tcp_read_many_d_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量读汇川记号:连续 D 地址翻译后合并为一笔 FC03(帧逐字节断言)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    response = bytes([3, 4, 0x00, 0x14, 0x00, 0x1E])
    frame = codec.build_mbap(1, 1, response)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    values = client.read_many(["D7021", "D7022"], "ushort")
    assert [v for _, v in values] == [20, 30]
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_pdu(3, 7021, 2)
    )


def test_tcp_read_batch_mixed_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """混类型批量读双记号:D7021 翻译、hr300 原样,Modbus 记号行为零变化。

    合并组内按地址升序切块:hr300 先读、hr7021 后读;返回值按 items 原序
    回填(D7021=20、hr300=30)。
    """
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame_a = codec.build_mbap(1, 1, bytes([3, 2, 0x00, 0x1E]))  # hr300 = 30
    frame_b = codec.build_mbap(2, 1, bytes([3, 2, 0x00, 0x14]))  # hr7021 = 20
    scripted = ScriptedTransport([frame_a[:7], frame_a[7:], frame_b[:7], frame_b[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_batch([("D7021", "ushort"), ("hr300", "ushort")])
    assert ok is True and values == [20, 30]
    sent = bytes(scripted.sent)
    # 两笔 FC03:7021(翻译自 D7021)与 300(原样)
    assert codec.build_read_pdu(3, 7021, 1) in sent
    assert codec.build_read_pdu(3, 300, 1) in sent


def test_tcp_write_many_d_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量写汇川记号:连续 D 地址翻译后合并为一笔 FC16。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    pdu = codec.build_write_multi_pdu(16, 400, [11, 22])
    frame = codec.build_mbap(1, 1, pdu[:_FC16_ECHO])
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_many([("D400", "short", 11), ("D401", "short", 22)]) == [
        True,
        True,
    ]
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, pdu)


def test_tcp_write_batch_d_and_coil(monkeypatch: pytest.MonkeyPatch) -> None:
    """混类型批量写:D500 字走 FC16、M100 位走 FC15(两笔独立事务)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    word_pdu = codec.build_write_multi_pdu(16, 500, [5])
    bit_pdu = codec.build_write_multi_pdu(15, 100, [1])
    scripted = ScriptedTransport(
        [
            codec.build_mbap(1, 1, word_pdu[:_FC16_ECHO])[:7],
            codec.build_mbap(1, 1, word_pdu[:_FC16_ECHO])[7:],
            codec.build_mbap(2, 1, bit_pdu[:_FC15_ECHO])[:7],
            codec.build_mbap(2, 1, bit_pdu[:_FC15_ECHO])[7:],
        ]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, results = client.write_batch([("D500", "ushort", 5), ("M100", "bool", True)])
    assert ok is True and results == [True, True]
    sent = bytes(scripted.sent)
    assert word_pdu in sent
    assert bit_pdu in sent


def test_tcp_write_mask_register_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC22 掩码写汇川记号:D700 翻译为 hr700 后走原 Modbus 路径。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    pdu = codec.build_mask_write_pdu(700, 0x00FF, 0x0010, "big")
    frame = codec.build_mbap(1, 1, pdu)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_mask_register("D700", 0x00FF, 0x0010) is True
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, pdu)


def test_tcp_read_write_registers_d(monkeypatch: pytest.MonkeyPatch) -> None:
    """FC23 先写后读双记号:读 D800/写 D801 两地址分别翻译。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    pdu = codec.build_read_write_registers_pdu(800, 1, 801, [7])
    response = bytes([23, 2, 0x00, 0x09])
    frame = codec.build_mbap(1, 1, response)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    ok, values = client.read_write_registers("D800", 1, "D801", [7])
    assert ok is True and values == [9]
    assert bytes(scripted.sent) == codec.build_mbap(1, 1, pdu)


def test_batch_c_token_is_modbus_coil(monkeypatch: pytest.MonkeyPatch) -> None:
    """C 记号歧义按 Modbus 优先锁定:批量 ``C10`` = 线圈 c10(FC01),非汇川计数器。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    frame = codec.build_mbap(1, 1, _RESPONSE_ONE_COIL)
    scripted = ScriptedTransport([frame[:7], frame[7:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_many(["C10"], "bool") == [(True, True)]
    assert bytes(scripted.sent) == codec.build_mbap(
        1, 1, codec.build_read_pdu(1, 10, 1)
    )


def test_batch_c32_counter_requires_single_point() -> None:
    """汇川 C32 计数器批量被 Modbus 优先裁决为线圈记号 → 区域×类型错误。

    批量与单点对 ``C`` 记号的语义分叉(单点=汇川计数器、批量=Modbus 线圈)
    是双记号的有意取舍,汇川计数器批量访问请用单点(docstring 披露)。
    """
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError, match="寄存器区域"):
        client.read_many(["C205"], "int")


def test_batch_invalid_token_reports_inovance_error() -> None:
    """两类记号都不认的地址报汇川解析错误(回落链末端)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError, match="无法解析汇川地址"):
        client.read_many(["ZZ9"], "ushort")


def test_batch_modbus_span_error_stays_modbus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modbus 记号合法但跨度越界:仍报 Modbus 错误,不回落汇川(错误文案正确)。"""
    client = InovanceTcpClient("127.0.0.1", 502, 1)
    with pytest.raises(ValueError, match="地址空间"):
        client.read_many(["hr65535"], "int")


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
    assert bytes(scripted.sent) == codec.build_rtu_frame(
        2, codec.build_read_pdu(3, 12388, 1)
    )


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
