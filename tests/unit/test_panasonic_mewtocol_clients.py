"""松下 MEWTOCOL 客户端测试:ASCII 帧 + BCC,脚本化传输全链路。

覆盖:BCC 金样本(MEWTOCOL 参考向量 %01#RCSX0000→1D)、RCS/WCS 单接点、
RD/WD 数据区、低字在前+字内高字节在前的多字编解码、读-改-写、
错误响应(!帧)不断线、BCC/站号错误按坏帧断开、UDP 整包、异步镜像。
"""
from __future__ import annotations

import asyncio

import pytest

from omniplc import PanasonicMewtocolTcpClient, PanasonicMewtocolUdpClient
from omniplc.aio import APanasonicMewtocolTcpClient
from omniplc.core.constants import (
    MEWTOCOL_DEFAULT_PORT,
    MEWTOCOL_STATION_DIRECT,
)
from omniplc.core.debug import format_hex
from omniplc.plc.panasonic import codec_mewtocol
from scripted import ScriptedTransport

_STATION = "01"


def _resp(head: str, data: str = "", station: str = _STATION) -> bytes:
    """构造正常响应帧:%HH$CC + 数据 + BCC + CR(测试脚手架)。"""
    body = "%{}${}{}".format(station, head, data)
    return (body + codec_mewtocol.bcc(body) + "\r").encode("ascii")


def _err(code: str) -> bytes:
    """构造错误响应帧:%HH!CC + BCC + CR(测试脚手架)。"""
    body = "%{}!{}".format(_STATION, code)
    return (body + codec_mewtocol.bcc(body) + "\r").encode("ascii")


def _mount(monkeypatch: pytest.MonkeyPatch, client, scripted: ScriptedTransport) -> None:
    """挂载脚本传输(走正常 connect 流程)。"""
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)


def test_bcc_golden_vector() -> None:
    """BCC 金样本(MEWTOCOL 参考向量):%01#RCSX0000 → 1D。"""
    assert codec_mewtocol.bcc("%01#RCSX0000") == "1D"
    request = codec_mewtocol.build_read_contact("01", "X", 0, 0)
    assert request == b"%01#RCSX00001D\r"


def test_station_formatting() -> None:
    """站号文本:01~99 两位十进制、0xEE 直连、越界拒绝。"""
    assert codec_mewtocol.station_text(1) == "01"
    assert codec_mewtocol.station_text(99) == "99"
    assert codec_mewtocol.station_text(MEWTOCOL_STATION_DIRECT) == "EE"
    with pytest.raises(ValueError):
        codec_mewtocol.station_text(0)
    with pytest.raises(ValueError):
        codec_mewtocol.station_text(100)


def test_read_bool_rcs(monkeypatch: pytest.MonkeyPatch) -> None:
    """RCS 读单接点:R1F(字 1 位 F)→ RCSR001F,响应 1 → True。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([_resp("RC", "1")[:4], _resp("RC", "1")[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_bool("R1F") == (True, True)
    assert bytes(scripted.sent) == codec_mewtocol.build_read_contact("01", "R", 1, 15)
    body = "%01#RCSR001F"
    assert bytes(scripted.sent) == body.encode("ascii") + \
        codec_mewtocol.bcc(body).encode("ascii") + b"\r"


def test_read_ushort_rd(monkeypatch: pytest.MonkeyPatch) -> None:
    """RD 读字:D100 → RDD0010000100,响应 2710 → 10000(高字节在前)。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([_resp("RD", "2710")[:4], _resp("RD", "2710")[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 10000)
    assert bytes(scripted.sent) == b"%01#RDD0010000100" + \
        codec_mewtocol.bcc("%01#RDD0010000100").encode("ascii") + b"\r"


def test_read_multi_word_low_word_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """int32/float 低字在前:int32 = 0x00020001 → 字序 0001 0002;float 1.0 → 0000 3F80。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    int_frame = _resp("RD", "00010002")
    float_frame = _resp("RD", "00003F80")
    scripted = ScriptedTransport(
        [int_frame[:4], int_frame[4:], float_frame[:4], float_frame[4:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_int("D0") == (True, 131073)  # 0x00020001
    assert client.read_float("D0") == (True, 1.0)
    sent = bytes(scripted.sent)
    body = "%01#RDD0000000001"  # 两事务同为 RD D00000~D00001(int32/float 读 2 字)
    frame = (body + codec_mewtocol.bcc(body) + "\r").encode("ascii")
    assert sent[:len(frame)] == frame
    assert sent[len(frame):].startswith(frame)


def test_write_words_wd(monkeypatch: pytest.MonkeyPatch) -> None:
    """WD 写字:write_ushort("D0", 100) → WDD0000000000 0064。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([_resp("WD")[:4], _resp("WD")[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_ushort("D0", 100) is True
    expected_body = "%01#WDD00000000000064"
    assert bytes(scripted.sent) == expected_body.encode("ascii") + \
        codec_mewtocol.bcc(expected_body).encode("ascii") + b"\r"


def test_write_bool_wcs(monkeypatch: pytest.MonkeyPatch) -> None:
    """WCS 写单接点:write_bool("Y0.3", True) → WCSY00031。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([_resp("WC")[:4], _resp("WC")[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("Y0.3", True) is True
    expected_body = "%01#WCSY00031"
    assert bytes(scripted.sent) == expected_body.encode("ascii") + \
        codec_mewtocol.bcc(expected_body).encode("ascii") + b"\r"


def test_word_bit_write_read_modify_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """D100.3 位写:先 RD 读字再 WD 写回(置 bit3),链路内两事务。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    read_frame = _resp("RD", "0004")
    write_frame = _resp("WD")
    scripted = ScriptedTransport([read_frame[:4], read_frame[4:], write_frame[:4], write_frame[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.write_bool("D100.3", True) is True
    sent = bytes(scripted.sent)
    read_body = "%01#RDD0010000100"
    write_body = "%01#WDD0010000100000C"
    assert sent.startswith(read_body.encode("ascii") +
                          codec_mewtocol.bcc(read_body).encode("ascii") + b"\r")
    assert sent.endswith(write_body.encode("ascii") +
                         codec_mewtocol.bcc(write_body).encode("ascii") + b"\r")


def test_l_area_dual_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """L 双语境:字访问按 LT(RDL0001000010),位访问按链接继电器(RCSL0010)。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    word_frame = _resp("RD", "0005")
    bit_frame = _resp("RC", "0")
    scripted = ScriptedTransport(
        [word_frame[:4], word_frame[4:], bit_frame[:4], bit_frame[4:]]
    )
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("L10") == (True, 5)
    assert client.read_bool("L10") == (True, False)
    sent = bytes(scripted.sent)
    assert b"RDL0001000010" in sent
    assert b"RCSL0010" in sent


def test_timer_counter_word_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """T/C 字访问拒绝并提示 S/K 区:参数错误抛 ValueError。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([])
    _mount(monkeypatch, client, scripted)
    client.connect()
    with pytest.raises(ValueError) as exc_info:
        client.read_ushort("T0")
    assert "S 区" in str(exc_info.value)
    assert bytes(scripted.sent) == b""


def test_error_response_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误响应(!帧):记录 last_error,不断线(链路正常)。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    scripted = ScriptedTransport([_err("21")[:4], _err("21")[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D99999") == (False, None)
    assert client.connected is True
    assert client.last_error is not None and "21" in client.last_error and "NACK" in client.last_error


def test_bcc_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """BCC 校验失败按坏帧处理:标记断开等待惰性重连;错误信息带收到的原始帧。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    frame = _resp("RD", "2710")
    corrupted = bytearray(frame)
    corrupted[-2] ^= 0x01
    corrupted = bytes(corrupted)
    scripted = ScriptedTransport([corrupted[:4], corrupted[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "BCC" in client.last_error
    # 现场排查需要原始字节:噪声误码 vs 收发错位靠帧内容区分
    assert format_hex(corrupted) in client.last_error


def test_station_echo_mismatch_marks_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """站号回显不符按坏帧处理。"""
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    body = "%02$RD2710"
    frame = (body + codec_mewtocol.bcc(body) + "\r").encode("ascii")
    scripted = ScriptedTransport([frame[:4], frame[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "站号" in client.last_error


def test_direct_station_reply_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """直连口径应答(自报 EE)放行:请求站 01、应答站 EE 正常解析。

    现场存在不论请求站号一律以直连站号 EE 应答的设备/模拟器
    (部分模拟器默认站号即 0xEE 且不校验应答站号)。
    """
    client = PanasonicMewtocolTcpClient("127.0.0.1", 1024)
    frame = _resp("RD", "2710", station="EE")
    scripted = ScriptedTransport([frame[:4], frame[4:]])
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D100") == (True, 10000)
    assert client.connected is True


def test_udp_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP:一问一答一数据报,整包接收后解析。"""
    client = PanasonicMewtocolUdpClient("127.0.0.1", 1024, station=2)
    scripted = ScriptedTransport([_resp("RD", "0064", station="02")], datagram=True)
    _mount(monkeypatch, client, scripted)
    client.connect()
    assert client.read_ushort("D0") == (True, 100)
    assert bytes(scripted.sent).startswith(b"%02#RDD0000000000")
    assert client.station == 2


def test_defaults() -> None:
    """默认 IP/端口 1024/站号 1。"""
    client = PanasonicMewtocolTcpClient()
    assert client._ip_address == "192.168.0.10"
    assert client._port == MEWTOCOL_DEFAULT_PORT == 1024
    assert client.station == 1


def test_async_mirror_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """异步镜像:TCP 客户端单工作线程往返。"""

    async def scenario() -> None:
        client = APanasonicMewtocolTcpClient("127.0.0.1", 1024)
        sync = client._sync
        if not isinstance(sync, PanasonicMewtocolTcpClient):
            raise TypeError("内部错误:sync 实例不是 PanasonicMewtocolTcpClient")
        assert sync.station == 1
        frame = _resp("RD", "2710")
        write_frame = _resp("WD")
        scripted = ScriptedTransport(
            [frame[:4], frame[4:], write_frame[:4], write_frame[4:]]
        )
        monkeypatch.setattr(sync, "_create_transport", lambda: scripted)
        assert await client.connect() is True
        assert await client.read_ushort("D100") == (True, 10000)
        assert await client.write_ushort("D100", 1) is True
        await client.close()

    asyncio.run(scenario())
