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
from omniplc.core.errors import TransportTimeoutError
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
    client = _make_client(monkeypatch, [b"A1 <code_type>DM <code_quality>4\n"])
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


# 「半行残留 → 断线」已由 test_scan_timeout_half_line_then_residue_disconnects
# 以真机超时口径覆盖(原 ScriptedTransport 分片耗尽版走 ConnectionError 拆连
# 路径,drain 分支测不到,与本文件旧错配同源,review-1003/1004 P1-3 随批删除)


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
    """configure_serial 落位:库缺省 115200(出厂默认 9600,手册 §4.6.2 印刷页 48)。"""
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


# ----------------------------------------------------------------------
# 审查 1001 修复:超时收尾顺序 / read_result 不 drain / ASCII 构造期校验
# ----------------------------------------------------------------------


def test_scan_timeout_silent_recv_stops_then_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完全静默超时(真串口 0 字节口径 = TransportTimeoutError):停止文本
    先发、drain 后行,链路不断线——审查 1001 P1-4 + review-1002 返工:
    原用例直接注入 socket.timeout,绕过了 _read_line 的
    TransportTimeoutError→socket.timeout 转换路径(修复前同样通过,假绿);
    改注入契约异常类型后,修复前该异常从 _read_line 逃逸、scan 直接抛。"""
    client = HikrobotIdSerialClient()
    client.configure_serial("COM3", 115200)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    events = []
    orig_send = scripted.send

    def send_log(data: bytes) -> None:
        events.append("send:" + data.decode("ascii"))
        orig_send(data)

    def silent_recv(size: int) -> bytes:
        events.append("recv")
        raise TransportTimeoutError("串口接收超时(0.05s)", 0)

    monkeypatch.setattr(scripted, "send", send_log)
    monkeypatch.setattr(scripted, "recv", silent_recv)
    assert client.scan(timeout=0.05) == (False, None)
    # 收尾完整且有序:start → 首拍超时 → stop 先发 → drain 后读(P2-4 顺序)
    assert bytes(scripted.sent) == b"startstop"
    assert events[0] == "send:start"
    assert events[1] == "recv"  # 首拍即静默超时
    assert events[2] == "send:stop"
    assert events[3] == "recv"  # stop 之后才进入 drain 读
    # 统一意图消息(review-1002 P3:drain 的 TransportTimeoutError 修复前
    # 从 _drain_line 逃逸,把这里的消息覆盖成传输层文本"串口接收超时")
    assert "读码结果等待超时" in (client.last_error or "")
    assert client.connected is True


def test_scan_timeout_half_line_then_residue_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半行后静默超时:drain 读不尽 → 断线重同步(防旧半行拼行)。

    注入真串口 0 字节超时口径 ``TransportTimeoutError``(review-1003/1004
    P1-3:原用 ScriptedTransport 分片耗尽的 ConnectionError 模拟,走的是
    OSError 拆连路径,「超时 → 停窗 → drain → 残留读不尽 → 拆连」真机
    分支完全没被测到)。断言 ``connected is False`` 本身正确——部分字节
    截断必断线重同步(review-1001 P2-4 三分语义),错的是模拟类型。
    """
    client = HikrobotIdSerialClient()
    client.configure_serial("COM3", 115200)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()

    timeout_exc = TransportTimeoutError("串口接收超时(0.1s)", 0)
    # 逐字节时序:_read_line/_drain_line 都是 recv(1) 逐字节收包
    # ①读行:A、B(半行,无行尾)→ 超时;②drain:C(读到字节未达行尾)→ 超时
    phases = [b"A", b"B", timeout_exc, b"C", timeout_exc]

    def phased_recv(size: int) -> bytes:
        item = phases.pop(0)
        if isinstance(item, bytes):
            return item
        raise item

    monkeypatch.setattr(scripted, "recv", phased_recv)
    assert client.scan(timeout=0.1) == (False, None)
    assert client.connected is False  # drain 读不尽 → 断线
    assert "读码结果等待超时" in (client.last_error or "")


def test_slow_result_arriving_during_drain_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """慢结果在 drain 期间到达:整行读完 → 读净不拆连、不污染下一轮。

    review-1001 P2-4 用户点名场景(review-1003 待办 2):窗内只到半行,
    静默超时触发停窗 + drain;此时慢结果的余下部分(含行尾)到达——
    drain 读完行尾即止,无残渣、链路完好,下一轮扫描不受残留影响。
    """
    client = HikrobotIdSerialClient()
    client.configure_serial("COM3", 115200)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()

    timeout_exc = TransportTimeoutError("串口接收超时(0.1s)", 0)
    # ①读行:A、B(半行)→ 超时;②drain:C、D、CR——慢结果余下部分到齐(读净)
    # ③下一轮 scan:完整一行 XYCR 正常应答
    phases = [b"A", b"B", timeout_exc, b"C", b"D", b"\r", b"X", b"Y", b"\r"]

    def phased_recv(size: int) -> bytes:
        item = phases.pop(0)
        if isinstance(item, bytes):
            return item
        raise item

    monkeypatch.setattr(scripted, "recv", phased_recv)
    assert client.scan(timeout=0.1) == (False, None)
    assert client.connected is True  # drain 读净 → 不断线
    assert "读码结果等待超时" in (client.last_error or "")
    # 残留已被 drain 消费:下一轮从流头读完整行,无半行拼接
    assert client.scan(timeout=0.1) == (True, "XY")


def test_read_result_timeout_keeps_window_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_result 超时:不 drain 不断线(窗口由调用方编排,迟到真结果
    留给下一次 read_result)——审查 1001 P2-4 + review-1002 返工:
    改注入契约异常类型 TransportTimeoutError(真串口 0 字节口径),
    修复前该异常从 _read_line/_read_result_once 逃逸、直接抛给调用方。"""
    client = HikrobotIdSerialClient()
    client.configure_serial("COM3", 115200)
    scripted = _ScriptedTransport([])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()

    def silent_recv(size: int) -> bytes:
        raise TransportTimeoutError("串口接收超时(0.05s)", 0)

    monkeypatch.setattr(scripted, "recv", silent_recv)
    assert client.read_result(timeout=0.05) == (False, None)
    # _read_line 统一转 socket.timeout → _read_result_once 转
    # TransportTimeoutError(0 字节超时口径,基类按"链路无残渣"不断线)
    assert client.connected is True  # 不断线
    assert bytes(scripted.sent) == b""  # 未发 stop(窗口由调用方 stop() 收)
    # 统一后的用户可见消息(修复前原始异常文本"串口接收超时"逃逸,假绿点)
    assert "读码结果等待超时" in (client.last_error or "")


def test_constructor_rejects_non_ascii_text() -> None:
    """触发/停止文本非 ASCII 构造期拒绝(事务期 encode 不再逃逸)。"""
    with pytest.raises(ValueError, match="ASCII"):
        HikrobotIdSerialClient(trigger_text="中文触发")
