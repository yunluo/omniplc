"""海康机器人 ID 系列读码器串口客户端测试:脚本化传输走完整触发时序。

注入按脚本应答的假传输(SR 同款手法),按《超小型智能读码器用户手册》
V1.1.2 §4.6.1/§4.6.2(串口触发文本 start/stop)与《工业读码器通信指令
操作手册》V1.0.3 印刷页 9(文本 1~31 字符、长度不可相等)验证:

- scan() 时序:发 start → 读结果行(CR/LF)→ 发 stop,发送字节序列断言
- NoRead 判定 / 结果超时(尽力停窗 + 残留行判定断线,SR 同口径)
- 多码编排:trigger() → read_result() → stop()
- 构造期校验:文本长度/等长拒绝/noread_text 非空;configure_serial 落位
"""
from __future__ import annotations

import pytest

from omniplc import HikrobotIdSerialClient
from scripted import ScriptedTransport as _ScriptedTransport


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list,
    **kwargs: object,
) -> HikrobotIdSerialClient:
    """注入脚本化传输并建立客户端(响应按字节切片,适配逐字节收行)。"""
    client = HikrobotIdSerialClient(**kwargs)  # type: ignore[arg-type]
    client.configure_serial("COM3", 115200)
    bytewise: list = []
    for chunk in chunks:
        bytewise.extend(bytes([b]) for b in chunk)
    scripted = _ScriptedTransport(bytewise)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    return client


def test_scan_success_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """scan() 全时序:start → 结果行 → stop,发送字节逐段断言。"""
    client = _make_client(monkeypatch, [b"ABC123\r\n"])
    assert client.scan(timeout=2.0) == (True, "ABC123")
    assert bytes(client._transport.sent) == b"startstop"  # noqa: SLF001 事务内发送序列


def test_scan_lf_only_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """结果行仅 LF 结尾同样成行;元数据模板文本原样透传。"""
    client = _make_client(
        monkeypatch, [b"A1 <code_type>DM <code_quality>4\n"]
    )
    ok, text = client.scan(timeout=2.0)
    assert ok is True
    assert text == "A1 <code_type>DM <code_quality>4"


def test_scan_noread(monkeypatch: pytest.MonkeyPatch) -> None:
    """NoRead 输出(手册默认文本):返回 (False, None) 记无读出。"""
    client = _make_client(monkeypatch, [b"NoRead\r\n"])
    assert client.scan(timeout=2.0) == (False, None)
    assert client.last_error is not None and "无读出" in client.last_error


def test_scan_timeout_stops_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """结果超时:仍发送 stop 收窗(防扫描窗常开),返回 (False, None) 不断线。"""
    client = HikrobotIdSerialClient()
    client.configure_serial("COM3", 115200)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()

    def silent_recv(size: int) -> bytes:
        raise __import__("socket").timeout("timed out")

    monkeypatch.setattr(scripted, "recv", silent_recv)
    assert client.scan(timeout=0.05) == (False, None)
    assert bytes(scripted.sent) == b"startstop"
    assert client.last_error is not None and "超时" in client.last_error
    assert client.connected is True


def test_scan_timeout_with_partial_line_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半行残留(SR 口径):读到了字节却未达行尾 → 判定断线重同步。"""
    # 结果只有半行且无更多数据:drain 读到字节但无行尾 → 留半行 → 断线
    client = _make_client(monkeypatch, [b"AB"])
    assert client.scan(timeout=0.05) == (False, None)
    assert client.connected is False


def test_read_result_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_result():不发送任何触发文本,被动收结果行。"""
    client = _make_client(monkeypatch, [b"IO-CODE\r\n"])
    assert client.read_result(timeout=2.0) == (True, "IO-CODE")
    assert bytes(client._transport.sent) == b""  # noqa: SLF001


def test_trigger_read_stop_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """多码编排:trigger() → read_result()×2 → stop()(发送仅 start/stop)。"""
    client = _make_client(monkeypatch, [b"CODE-1\r\n", b"CODE-2\r\n"])
    assert client.trigger() is True
    assert client.read_result(timeout=2.0) == (True, "CODE-1")
    assert client.read_result(timeout=2.0) == (True, "CODE-2")
    assert client.stop() is True
    assert bytes(client._transport.sent) == b"startstop"  # noqa: SLF001 发送端仅触发/停止文本


def test_custom_trigger_stop_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """现场改过触发/停止文本:构造参数同步,发送与长度等长校验生效。"""
    client = _make_client(
        monkeypatch,
        [b"OK-CODE\r\n"],
        trigger_text="TRIG",
        stop_text="HALT!",
    )
    assert client.trigger_text == "TRIG"
    assert client.stop_text == "HALT!"
    assert client.scan(timeout=2.0) == (True, "OK-CODE")
    assert bytes(client._transport.sent) == b"TRIGHALT!"  # noqa: SLF001


def test_constructor_text_validation() -> None:
    """构造期校验:文本 1~31、等长拒绝、noread_text 非空、编码合法。"""
    with pytest.raises(ValueError, match="trigger_text"):
        HikrobotIdSerialClient(trigger_text="")
    with pytest.raises(ValueError, match="trigger_text"):
        HikrobotIdSerialClient(trigger_text="x" * 32)
    with pytest.raises(ValueError, match="长度不可相等"):
        HikrobotIdSerialClient("begin", "start")  # 5 字符等长
    with pytest.raises(ValueError, match="noread_text"):
        HikrobotIdSerialClient(noread_text="")
    with pytest.raises(ValueError, match="encoding"):
        HikrobotIdSerialClient(encoding="not-a-codec")


def test_configure_serial_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """configure_serial 落位:默认 115200(手册未载出厂默认,以 IDMVS 为准)。"""
    client = HikrobotIdSerialClient()
    client.configure_serial("COM5")
    assert client._serial_config is not None  # noqa: SLF001
    assert client._serial_config.baud_rate == 115200  # noqa: SLF001
    assert client._serial_config.port_name == "COM5"  # noqa: SLF001
    # 未配置串口直接 scan:参数期快速失败
    client2 = HikrobotIdSerialClient()
    with pytest.raises(ValueError, match="configure_serial"):
        client2.scan(timeout=1.0)


def test_custom_noread_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """现场改过「输出无读」文本时,noread_text 同步判定。"""
    client = _make_client(monkeypatch, [b"NOREAD!\r\n"], noread_text="NOREAD!")
    assert client.scan(timeout=2.0) == (False, None)
