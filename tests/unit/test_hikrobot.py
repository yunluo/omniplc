"""海康机器人 ID 系列读码器客户端测试:脚本化传输走完整触发握手。

注入按脚本应答的假传输(ModbusTcpClient 同款手法,本类继承
ModbusTcpClient 故同样 monkeypatch ``_create_transport``),按《海康机器人
智能读码器工业协议操作手册》V1.0.4 §3.5/§3.6 的寄存器模型与时序验证:

- scan() 全握手控制字序列:使能(0x0001) → 触发(0x0003) → 回落触发位
  (0x0001) → 应答(0x0005),与手册「写 3 和 5 交替」测试口径一致
- 结果区解码:首字长度 + ASCII 数据、字节交换、NoRead 透传、NG 正常返回
- 异常路径:General Fault / 读码超时 / Ack 未消费各自可区分
- 构造期校验:result_words 范围(§3.5 印刷页 41)

超时类用例以假时钟(每次 monotonic 调用步进)驱动,不依赖真实耗时,
脚本耗尽(抛 ConnectionError)先于判定到达即说明轮询越界,属测试错误。
"""
from __future__ import annotations

from typing import List, Tuple

import pytest

from omniplc import HikrobotIdModbusClient, HikrobotStatus
from omniplc.core.errors import DeviceError, TransportTimeoutError
from omniplc.modbus import codec
from scripted import ScriptedTransport as _ScriptedTransport

import omniplc.scanner.hikrobot as hikrobot_module

_STATION = 0
_RESULT_WORDS = 100


class _FakeClock:
    """假时钟:每次调用步进固定秒数,超时用例确定性驱动。"""

    def __init__(self, step: float = 0.3) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


def _chunks(frames: List[bytes]) -> List[bytes]:
    """MBAP 应答帧序列 → recv 分片序列(帧头 7 字节 + 帧体逐帧交错)。"""
    chunks: List[bytes] = []
    for frame in frames:
        chunks.append(frame[:7])
        chunks.append(frame[7:])
    return chunks


def _mbap(tid: int, pdu: bytes) -> bytes:
    """按站号 0 封 MBAP 应答帧。"""
    return codec.build_mbap(tid, _STATION, pdu)


def _fc03_response(tid: int, words: List[int]) -> bytes:
    """构造 FC03 应答:字节计数 + 大端寄存器序列。"""
    pdu = bytes([3, 2 * len(words)]) + b"".join(w.to_bytes(2, "big") for w in words)
    return _mbap(tid, pdu)


def _fc06_response(tid: int, offset: int, value: int) -> bytes:
    """构造 FC06 应答(请求回显)。"""
    return _mbap(tid, codec.build_write_single_pdu(6, offset, value))


def _block_response(tid: int, status: int, payload: List[int]) -> bytes:
    """构造「状态字 + 结果区」合并读应答(REG1..REG101,共 101 字)。"""
    words = [status] + payload + [0] * (_RESULT_WORDS - len(payload))
    return _fc03_response(tid, words)


def _make_client(
    monkeypatch: pytest.MonkeyPatch, frames: List[bytes]
) -> Tuple[HikrobotIdModbusClient, _ScriptedTransport]:
    """注入脚本化传输并建立读码器客户端(站号 0,结果区 100 字)。"""
    scripted = _ScriptedTransport(_chunks(frames))
    client = HikrobotIdModbusClient("127.0.0.1", 502, _STATION, _RESULT_WORDS)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    return client, scripted


def test_scan_success_full_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """scan() 全握手:控制字序列 0x0001→0x0003→0x0001→0x0005,结果解码正确。"""
    # 时序:使能写(tid1) → Ready 轮询(tid2, Ready=1) → 触发写(tid3) →
    # 状态+结果合并读(tid4: Ack|OK, "ABC123") → 回落触发位写(tid5) →
    # 应答写(tid6) → Ack 消费轮询(tid7, 全 0)
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x0002 | 0x0100, [6, 0x4142, 0x4331, 0x3233]),  # "ABC123"
        _fc06_response(5, 0, 0x0001),
        _fc06_response(6, 0, 0x0005),
        _fc03_response(7, [0x0000]),
    ]
    client, scripted = _make_client(monkeypatch, frames)
    assert client.scan(timeout=2.0, poll_interval=0.001) == (True, "ABC123")
    # 控制字写序列与手册 §3.6 一致:使能 / 触发 / 回落 / 应答
    assert bytes(scripted.sent) == b"".join([
        codec.build_mbap(1, _STATION, codec.build_write_single_pdu(6, 0, 0x0001)),
        codec.build_mbap(2, _STATION, codec.build_read_pdu(3, 1, 1)),
        codec.build_mbap(3, _STATION, codec.build_write_single_pdu(6, 0, 0x0003)),
        codec.build_mbap(4, _STATION, codec.build_read_pdu(3, 1, _RESULT_WORDS + 1)),
        codec.build_mbap(5, _STATION, codec.build_write_single_pdu(6, 0, 0x0001)),
        codec.build_mbap(6, _STATION, codec.build_write_single_pdu(6, 0, 0x0005)),
        codec.build_mbap(7, _STATION, codec.build_read_pdu(3, 1, 1)),
    ])


def test_scan_ng_returns_false_without_result_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Results NG(未读到码):scan 正常返回 (False, None),握手仍闭环。"""
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x0002 | 0x0200, []),  # Ack|NG,结果区已清零
        _fc06_response(5, 0, 0x0001),
        _fc06_response(6, 0, 0x0005),
        _fc03_response(7, [0x0000]),
    ]
    client, _scripted = _make_client(monkeypatch, frames)
    assert client.scan(timeout=2.0, poll_interval=0.001) == (False, None)


def test_scan_noread_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """读码器 NoRead 使能时未读到码反馈 OK + "NoRead" 字符,原样透传。"""
    payload = b"NoRead"
    words: List[int] = [6]
    words += [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x0100, words),
        _fc06_response(5, 0, 0x0005),
        _fc03_response(6, [0x0000]),
    ]
    client, _scripted = _make_client(monkeypatch, frames)
    assert client.scan(timeout=2.0, poll_interval=0.001) == (True, "NoRead")


def test_scan_general_fault_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """General Fault(设备内部异常):抛 DeviceError,可区分于 NG。"""
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x8000, []),
    ]
    client, _scripted = _make_client(monkeypatch, frames)
    with pytest.raises(DeviceError, match="General Fault"):
        client.scan(timeout=2.0, poll_interval=0.001)


def test_scan_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时未出结果:抛 TransportTimeoutError(区别于 NG 的正常未读到码)。"""
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
    ] + [_block_response(4 + i, 0x0008, []) for i in range(8)]  # 持续 Decoding(整块读应答)
    scripted = _ScriptedTransport(_chunks(frames))
    client = HikrobotIdModbusClient("127.0.0.1", 502, _STATION, _RESULT_WORDS)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    # 假时钟每次 monotonic 步进 0.3s,timeout=1.0 → 若干次轮询内必到期;
    # 若脚本先耗尽(8 个应答用尽前未判定)则 ConnectionError 暴露测试错误
    monkeypatch.setattr(hikrobot_module.time, "monotonic", _FakeClock(step=0.3))
    with pytest.raises(TransportTimeoutError, match="读码超时"):
        client.scan(timeout=1.0, poll_interval=0.001)


def test_scan_ack_not_consumed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Results Ack 未被设备消费(OK/NG 不清零):握手未闭环,抛 DeviceError。"""
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x0100, [2, 0x4142]),
        _fc06_response(5, 0, 0x0005),
    ] + [_fc03_response(6 + i, [0x0100]) for i in range(8)]  # OK 挂住不清零
    scripted = _ScriptedTransport(_chunks(frames))
    client = HikrobotIdModbusClient("127.0.0.1", 502, _STATION, _RESULT_WORDS)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    monkeypatch.setattr(hikrobot_module.time, "monotonic", _FakeClock(step=0.3))
    with pytest.raises(DeviceError, match="Results Ack"):
        client.scan(timeout=1.0, poll_interval=0.001)


def test_scan_byte_swap_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """读码器「结果字节交换」开启时寄存器内高低字节对调,按 byte_swap 解码。"""
    # "AB" 大端 0x4142;交换后线上为 0x4241
    frames = [
        _fc06_response(1, 0, 0x0001),
        _fc03_response(2, [0x0001]),
        _fc06_response(3, 0, 0x0003),
        _block_response(4, 0x0100, [2, 0x4241]),
        _fc06_response(5, 0, 0x0005),
        _fc03_response(6, [0x0000]),
    ]
    scripted = _ScriptedTransport(_chunks(frames))
    client = HikrobotIdModbusClient(
        "127.0.0.1", 502, _STATION, _RESULT_WORDS, byte_swap=True
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    assert client.scan(timeout=2.0, poll_interval=0.001) == (True, "AB")


def test_read_status_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_status():状态字各位正确展开,原始值保留。"""
    client, _scripted = _make_client(
        monkeypatch, [_fc03_response(1, [0x0002 | 0x0100 | 0x0008])]  # Ack + Decoding + OK
    )
    status = client.read_status()
    assert isinstance(status, HikrobotStatus)
    assert status.trigger_ready is False
    assert status.trigger_ack is True
    assert status.decoding is True
    assert status.results_ok is True
    assert status.results_ng is False
    assert status.general_fault is False
    assert status.raw == 0x010A


def test_clear_error_pulses_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_error():置位 bit15 → 轮询故障清零 → 复位控制字。"""
    frames = [
        _fc06_response(1, 0, 0x8001),  # Enable + Clear Error
        _fc03_response(2, [0x8000]),   # 故障仍在
        _fc03_response(3, [0x0000]),   # 故障已清
        _fc06_response(4, 0, 0x0001),  # 复位控制字
    ]
    client, _scripted = _make_client(monkeypatch, frames)
    assert client.clear_error(timeout=1.0, poll_interval=0.001) is True


def test_clear_error_timeout_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_error():超时故障未清零,返回 False 而非抛异常。"""
    frames = [
        _fc06_response(1, 0, 0x8001),
    ] + [_fc03_response(2 + i, [0x8000]) for i in range(8)]
    scripted = _ScriptedTransport(_chunks(frames))
    client = HikrobotIdModbusClient("127.0.0.1", 502, _STATION, _RESULT_WORDS)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    monkeypatch.setattr(hikrobot_module.time, "monotonic", _FakeClock(step=0.3))
    assert client.clear_error(timeout=1.0, poll_interval=0.001) is False


def test_constructor_result_words_range() -> None:
    """构造期校验:结果区大小 4~500(§3.5 印刷页 41)。"""
    with pytest.raises(ValueError, match="result_words"):
        HikrobotIdModbusClient("127.0.0.1", result_words=3)
    with pytest.raises(ValueError, match="result_words"):
        HikrobotIdModbusClient("127.0.0.1", result_words=501)


def test_constructor_defaults_station_zero() -> None:
    """构造期缺省:站号默认 0(读码器默认 255 或 0,本库按规范取 0)。"""
    client = HikrobotIdModbusClient("127.0.0.1")
    assert client.station == 0
    assert client._ip_address == "127.0.0.1"  # noqa: SLF001 私有属性直读(构造缺省断言)


def test_ping_reads_status_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """ping():FC03 读 REG1 状态字,应答即探活成功(不触发扫描握手)。"""
    client, scripted = _make_client(monkeypatch, [_fc03_response(1, [0x0001])])
    assert client.ping() is True
    assert bytes(scripted.sent) == codec.build_mbap(
        1, _STATION, codec.build_read_pdu(3, 1, 1)
    )
