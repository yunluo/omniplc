"""海康机器人 ID 系列读码器 TCP 命令客户端测试:假 socket 双通道全链路。

替换 :func:`omniplc.scanner.hikrobot_tcp._open_connection` 模块工厂(同
MTConnect ``_new_connection`` 惯例),命令/结果两条 TCP 连接各挂
size 感知假 socket,按《工业读码器通信指令操作手册》V1.0.3 §1.3/§3 与
用户手册的推送口径验证:

- scan() 全流程:``<Exec,TriSoft>`` 命令应答 OK → 结果推送静默成帧 → 解码
- NoRead 文本判定 / 结果等待超时(合并入 (False, None),SR 同契约)
- 命令面:errno 应答(-4 设备忙)/ invalid 拒绝 / 回显不符坏帧断线
- 低阶 command() 透传:Get 参数文本返回、Set/Exec 参数校验
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from omniplc import HikrobotIdTcpClient
from omniplc.scanner import hikrobot_tcp

import scripted
from scripted import ChunkSocket

_CMD_PORT = 9989
_RESULT_PORT = 9988


class _FakeSocket(ChunkSocket):
    """size 感知假 socket + 发送记录(命令字节断言用)。"""

    def __init__(self, chunks: List[bytes]) -> None:
        super().__init__(chunks)
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    command_chunks: List[bytes],
    result_chunks: Optional[List[bytes]],
    **kwargs: object,
) -> Tuple[HikrobotIdTcpClient, _FakeSocket, Optional[_FakeSocket]]:
    """注入双通道假 socket 并建立客户端(connect 已完成)。"""
    cmd_sock = _FakeSocket(command_chunks)
    result_sock = _FakeSocket(result_chunks) if result_chunks is not None else None

    def fake_open(host: str, port: int, timeout: float) -> _FakeSocket:
        # connect() 先开命令通道再开结果通道,按顺序路由
        if result_sock is not None and port == _RESULT_PORT:
            return result_sock
        return cmd_sock

    monkeypatch.setattr(hikrobot_tcp, "_open_connection", fake_open)
    # 结果通道未给脚本时,客户端也不配结果端口(result_port=None 口径)
    client_result_port = _RESULT_PORT if result_chunks is not None else None
    client = HikrobotIdTcpClient("127.0.0.1", _CMD_PORT, client_result_port, **kwargs)  # type: ignore[arg-type]
    assert client.connect() is True
    return client, cmd_sock, result_sock


def test_scan_success_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """scan() 全流程:TriSoft 命令应答 OK → 结果推送成帧解码。"""
    client, cmd_sock, result_sock = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        [b"ABC123\r\n"],
    )
    assert client.scan(timeout=2.0) == (True, "ABC123")
    assert bytes(cmd_sock.sent) == b"<Exec,TriSoft>"
    assert result_sock is not None and bytes(result_sock.sent) == b""


def test_scan_multicode_and_metadata_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多码/元数据模板原文透传:两行码 + 类型/评分占位符输出原样返回。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        [b"A1\r\nB2 <code_type>DataMatrix <code_eval_score>0.98\r\n"],
    )
    ok, text = client.scan(timeout=2.0)
    assert ok is True
    assert text == "A1\r\nB2 <code_type>DataMatrix <code_eval_score>0.98"


def test_scan_noread(monkeypatch: pytest.MonkeyPatch) -> None:
    """NoRead 输出(手册「输出无读」默认文本):返回 (False, None) 记无读出。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        [b"NoRead\r\n"],
    )
    assert client.scan(timeout=2.0) == (False, None)
    assert client.last_error is not None and "无读出" in client.last_error


def test_scan_result_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """结果通道静默:超时合并入 (False, None),last_error 可辨(不断线)。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        [],
    )
    assert client.scan(timeout=0.05) == (False, None)
    assert client.last_error is not None and "超时" in client.last_error
    assert client.connected is True


def test_scan_without_result_port_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """result_port=None(仅命令控制):scan() 参数期快速失败。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        None,
    )
    with pytest.raises(ValueError, match="result_port"):
        client.scan(timeout=2.0)


def test_trigger_success_and_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    """trigger():OK 应答成功;errno 应答按错误码表记 last_error_code。"""
    client, cmd_sock, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>", b"<Exec,TriSoft,-4>"],
        None,
    )
    assert client.trigger() is True
    assert client.trigger() is False
    assert client.last_error_code == -4
    assert client.last_error is not None and "设备忙" in client.last_error
    assert bytes(cmd_sock.sent) == b"<Exec,TriSoft><Exec,TriSoft>"


def test_get_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_acquisition():``<Get,Acq>`` 参数值转整型。"""
    client, cmd_sock, _result = _make_client(
        monkeypatch,
        [b"<Get,Acq,1>"],
        None,
    )
    assert client.get_acquisition() == (True, 1)
    assert bytes(cmd_sock.sent) == b"<Get,Acq>"


def test_set_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_acquisition():``<Set,Acq,1>`` 写命令与 OK 应答。"""
    client, cmd_sock, _result = _make_client(
        monkeypatch,
        [b"<Set,Acq,OK>"],
        None,
    )
    assert client.set_acquisition(True) is True
    assert bytes(cmd_sock.sent) == b"<Set,Acq,1>"


def test_command_get_string_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """低阶 command():Get 字符串参数原样返回(触发文本等)。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Get,TriTcpStart,start>"],
        None,
    )
    assert client.command("Get", "TriTcpStart") == (True, "start")


def test_command_rejects_bad_combination() -> None:
    """低阶 command():Set 缺参 / Get 带参 / 类型非法,参数期拒绝。"""
    client = HikrobotIdTcpClient("127.0.0.1", _CMD_PORT)
    with pytest.raises(ValueError, match="Set"):
        client.command("Set", "RunMode")
    with pytest.raises(ValueError, match="Get"):
        client.command("Get", "RunMode", "1")
    with pytest.raises(ValueError, match="cmd_type"):
        client.command("Call", "RunMode")


def test_command_echo_mismatch_marks_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应答回显不符(错配/迟到帧):按坏帧处理,断线待惰性重连。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Set,AcqX,OK>"],
        None,
    )
    assert client.set_acquisition(True) is False
    assert client.connected is False
    assert client.last_error is not None and "回显不符" in client.last_error


def test_command_invalid_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """设备应答 invalid(指令不合法):DeviceError 记录,链路不断线。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,invalid>"],
        None,
    )
    assert client.trigger() is False
    assert client.last_error is not None and "invalid" in client.last_error
    assert client.connected is True


def test_read_result_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_result():不发送任何命令,被动收推送(IO 硬触发场景)。"""
    client, cmd_sock, _result = _make_client(
        monkeypatch,
        [],
        [b"IO-trig-code\r\n"],
    )
    assert client.read_result(timeout=2.0) == (True, "IO-trig-code")
    assert bytes(cmd_sock.sent) == b""


def test_split_reply_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """应答跨 TCP 分片:逐字节读到 ``>`` 拼帧正确。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,Tri", b"Soft,OK>"],
        None,
    )
    assert client.trigger() is True


def test_constructor_ports_must_differ() -> None:
    """构造期校验:结果端口与命令端口不得相同(手册要求)。"""
    with pytest.raises(ValueError, match="相同"):
        HikrobotIdTcpClient("127.0.0.1", 9989, 9989)


def test_constructor_noread_text_and_intervals() -> None:
    """构造期校验:noread_text 非空、settle_interval/max_frame 正值。"""
    with pytest.raises(ValueError, match="noread_text"):
        HikrobotIdTcpClient("127.0.0.1", _CMD_PORT, noread_text="")
    with pytest.raises(ValueError, match="settle_interval"):
        HikrobotIdTcpClient("127.0.0.1", _CMD_PORT, settle_interval=0)
    with pytest.raises(ValueError, match="max_frame"):
        HikrobotIdTcpClient("127.0.0.1", _CMD_PORT, max_frame=-1)


def test_custom_noread_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """现场改过「输出无读」文本时,noread_text 同步判定。"""
    client, _cmd, _result = _make_client(
        monkeypatch,
        [b"<Exec,TriSoft,OK>"],
        [b"NOREAD!\r\n"],
        noread_text="NOREAD!",
    )
    assert client.scan(timeout=2.0) == (False, None)


def test_scripted_module_used_for_chunks() -> None:
    """占位断言:确认测试脚本架依赖 scripted.ChunkSocket 语义(空池即超时)。"""
    assert scripted.ChunkSocket is ChunkSocket


def test_ping_get_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    """ping():``<Get,Acq>`` 零副作用查询,应答即命令通道探活成功。"""
    client, cmd_sock, _result = _make_client(
        monkeypatch,
        [b"<Get,Acq,1>"],
        None,
    )
    assert client.ping() is True
    assert bytes(cmd_sock.sent) == b"<Get,Acq>"
