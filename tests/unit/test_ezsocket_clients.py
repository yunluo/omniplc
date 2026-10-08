"""三菱 CNC EZSocket(GIOP)客户端测试:黄金帧 + 脚本化传输全链路。

覆盖:

- GIOP 请求帧编码黄金样本(mochaGetData/AlarmMsg/PrgBlock 三操作)
- 响应解析黄金样本(数据头/类型改写/T_FLOATBIN/T_STR/T_DLONG)
- request_id 回显校验(omniplc 加严点,C 库不校验)
- 三菱错误码分流:链路码(0x80A00101)拆连 / 业务码 DeviceError 不断线
- 坏帧(魔数/类型/帧长超限/不完整)→ ProtocolFrameError 断连
- 状态拼合(read_run_state 三段逻辑)/报警/程序块解码
- 构造期校验(machine/axis/system_no/count/rows/alarm_type)
- 探活、通用地址读写拒绝、aio 镜像
"""

from __future__ import annotations

import struct

import pytest

from omniplc import (
    EzDeviceStatus,
    EzFeedSpeedType,
    EzPositionType,
    EzRunMode,
    EzRunState,
    EzRunStatus,
    EzSocketMachine,
    MitsubishiEzSocketClient,
)
from omniplc.cnc.ezsocket import (
    build_alarm_request,
    build_get_data_request,
    build_prog_block_request,
    decode_alarms,
    decode_get_data,
    decode_prog_block,
    parse_giop_response,
)
from omniplc.core.errors import ProtocolFrameError
from scripted import ScriptedTransport

_RID = 0x1234


def _chunks(frame: bytes) -> list:
    """把响应帧切成 [GIOP 头 12 字节, 余量] 两段(匹配按长收包契约)。"""
    return [frame[:12], frame[12:]]


def _client(monkeypatch: pytest.MonkeyPatch, chunks: list) -> MitsubishiEzSocketClient:
    """建已连接客户端:固定 request_id + 挂脚本化传输。"""
    client = MitsubishiEzSocketClient("127.0.0.1", 683)
    client._request_id = _RID
    scripted = ScriptedTransport(chunks)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    return client


def _giop_response(
    body: bytes, msg_type: int = 0x01, request_id: int = _RID, is_error: int = 0
) -> bytes:
    """按 GIOP 响应格式构造一帧(头 + 响应体头 + body)。"""
    head = struct.pack("<3I", 0, request_id, is_error)
    data_length = len(head) + len(body)
    return (
        b"GIOP"
        + struct.pack("<HBB", 1, 1, msg_type)
        + struct.pack("<I", data_length)
        + head
        + body
    )


def _get_data_response(data_type: int, data: bytes, **kwargs: int) -> bytes:
    """构造 mochaGetData 成功响应(数据头 + 数据);kwargs 透传 _giop_response。"""
    return _giop_response(struct.pack("<3I", 0, data_type, len(data)) + data, **kwargs)


# ----------------------------------------------------------------------
# 帧编码黄金样本
# ----------------------------------------------------------------------


def test_build_get_data_request_golden() -> None:
    """mochaGetData 请求黄金帧:读系统数(2/1,system=0,axis=0,T_CHAR)。

    布局对照 C 库 m70_ezsocket_private.h get_data_pack(L166~181):
    GIOP 头(12)+ 请求头(24)+ op[16]+ principal + 6×u32 参数
    (params 首个 u32 即 principal,共 28 字节)。
    """
    frame = build_get_data_request(_RID, 2, 1, 0, 0, 0x01)
    assert frame == (
        b"GIOP"
        + b"\x01\x00"  # version 1.0(小端)
        + b"\x01"  # byte_order = little
        + b"\x00"  # msg_type = Request
        + b"\x44\x00\x00\x00"  # data_length = 68
        + b"\x00\x00\x00\x00"  # service_context
        + b"\x34\x12\x00\x00"  # request_id
        + b"\x01"  # response_expected
        + b"\x00\x00\x00"  # reserved
        + b"\x04\x00\x00\x00"  # object_key_length
        + b"\x01\x00\x00\x00"  # object_key
        + b"\x0d\x00\x00\x00"  # operation_length = 13(mochaGetData + NUL)
        + b"mochaGetData\x00\x00\x00\x00"  # op[16]
        + struct.pack("<7I", 0, 2, 1, 0, 0, 0, 1)  # principal + 参数区
    )
    assert len(frame) == 80


def test_build_alarm_request_golden() -> None:
    """mochaGetCurrentAlarmMsgFirst 请求黄金帧(op[32],length=29)。"""
    frame = build_alarm_request(_RID, 1, 10, 0x000)
    assert frame[32:36] == b"\x1d\x00\x00\x00"
    assert frame[36:68] == b"mochaGetCurrentAlarmMsgFirst\x00\x00\x00\x00"
    assert frame[68:84] == struct.pack("<4I", 0, 1, 10, 0)
    assert len(frame) == 12 + 24 + 32 + 16


def test_build_prog_block_request_golden() -> None:
    """mochaGetCurrentPrgBlockFirst 请求黄金帧(op[32],length=29)。"""
    frame = build_prog_block_request(_RID, 1, 10)
    assert frame[36:68] == b"mochaGetCurrentPrgBlockFirst\x00\x00\x00\x00"
    assert frame[68:80] == struct.pack("<3I", 0, 1, 10)


# ----------------------------------------------------------------------
# 响应解析
# ----------------------------------------------------------------------


def test_parse_response_and_decode_char() -> None:
    """成功响应:T_CHAR 数据(系统数 = 5),回显校验通过。"""
    raw = _get_data_response(0x01, b"\x05")
    payload, error_code = parse_giop_response(raw, _RID)
    assert error_code is None
    assert decode_get_data(payload, 0x01) == 5


def test_parse_response_request_id_mismatch() -> None:
    """回显不符(omniplc 加严,C 库不校验)→ ProtocolFrameError。"""
    raw = _get_data_response(0x01, b"\x05", request_id=0x9999)
    with pytest.raises(ProtocolFrameError):
        parse_giop_response(raw, _RID)


def test_parse_response_bad_magic() -> None:
    """魔数非法 → ProtocolFrameError。"""
    raw = _get_data_response(0x01, b"\x05")
    raw = b"XPOP" + raw[4:]
    with pytest.raises(ProtocolFrameError):
        parse_giop_response(raw, _RID)


def test_parse_response_bad_msg_type() -> None:
    """msg_type 非 Reply → ProtocolFrameError。"""
    raw = _get_data_response(0x01, b"\x05", msg_type=0x00)
    with pytest.raises(ProtocolFrameError):
        parse_giop_response(raw, _RID)


def test_parse_response_truncated() -> None:
    """声明长度超出实际字节 → ProtocolFrameError。"""
    raw = _get_data_response(0x01, b"\x05")
    with pytest.raises(ProtocolFrameError):
        parse_giop_response(raw[:-1], _RID)


def test_decode_type_rewrite() -> None:
    """响应头 data_type 改写:请求 T_CHAR 回 T_SHORT,按响应解码。"""
    payload = struct.pack("<3I", 0, 0x02, 2) + struct.pack("<h", -3)
    assert decode_get_data(payload, 0x01) == -3


def test_decode_floatbin() -> None:
    """T_FLOATBIN:16 字节(位段数×2 + option + float64),取 data。"""
    payload = struct.pack("<3I", 0, 0x06, 16) + struct.pack("<hhId", 5, 3, 0, 123.456)
    assert decode_get_data(payload, 0x06) == pytest.approx(123.456)


def test_decode_dlong_full_width() -> None:
    """T_DLONG 按 i64 全宽解码(omniplc 精确解码,C 库低 32 位读差异)。"""
    payload = struct.pack("<3I", 0, 0x04, 8) + struct.pack("<q", 3000)
    assert decode_get_data(payload, 0x04) == 3000


def test_decode_str() -> None:
    """T_STR:u32 长度 + ASCII。"""
    payload = struct.pack("<3I", 0, 0x10, 7) + struct.pack("<I", 3) + b"2.0A"
    assert decode_get_data(payload, 0x10) == "2.0"


def test_decode_get_data_insufficient_bytes_rejected() -> None:
    """数据段字节不足按类型解码 → ProtocolFrameError 收口(review-1020 P2-1)。

    旧实现 IndexError/struct.error 直抛,逃逸 _execute 契约且不拆连
    (decode_prog_block 同型防护先例)。
    """
    # T_CHAR 声明 0 字节:data[0] 曾 IndexError
    with pytest.raises(ProtocolFrameError):
        decode_get_data(struct.pack("<3I", 0, 0x01, 0), 0x01)
    # T_DLONG 声明 2 字节:曾 struct.error
    with pytest.raises(ProtocolFrameError):
        decode_get_data(struct.pack("<3I", 0, 0x04, 2) + b"\x01\x00", 0x04)
    # T_STR 不足 4 字节长度域
    with pytest.raises(ProtocolFrameError):
        decode_get_data(struct.pack("<3I", 0, 0x10, 2) + b"\x01\x00", 0x10)
    # T_FLOATBIN 不足 16 字节
    with pytest.raises(ProtocolFrameError):
        decode_get_data(struct.pack("<3I", 0, 0x06, 8) + b"\x00" * 8, 0x06)


def test_decode_alarms_two_entries() -> None:
    """报警数组:两条定长 264 字节结构逐条解码。"""
    entry = struct.pack("<2i", 101, 3) + b"Y01" + b"\x00" * 253
    assert len(entry) == 264
    alarms = decode_alarms(entry * 2)
    assert len(alarms) == 2
    assert alarms[0].no == 101 and alarms[0].text == "Y01"
    assert alarms[1].no == 101


def test_decode_prog_block() -> None:
    """程序块首条:块号/行号/文本。"""
    block = struct.pack("<4i", 10, 5, 0, 4) + b"MAIN" + b"\x00" * 508
    assert len(block) == 528
    parsed = decode_prog_block(block)
    assert parsed.block == 10 and parsed.row == 5 and parsed.text == "MAIN"


# ----------------------------------------------------------------------
# 全链路(脚本化传输)
# ----------------------------------------------------------------------


def test_read_system_count_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读系统数全链路:请求帧逐字节 + 响应值;探活同命令。"""
    client = _client(
        monkeypatch,
        _chunks(_get_data_response(0x01, b"\x03"))
        + _chunks(_get_data_response(0x01, b"\x03")),
    )
    ok, count = client.read_system_count()
    assert ok is True and count == 3
    transport = client._transport
    assert transport.sent == build_get_data_request(_RID, 2, 1, 0, 0, 0x01)
    assert client._ping_probe() == "system_count=3"


def test_device_error_keeps_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务错误码(大区分番号非法 0x80040191)→ DeviceError 不断线。

    错误体布局(照录 C 库,登记待真机核):desc_len(4B)+
    mel_error_code(char[3] + u32 code + u32,11B)。
    """
    body = struct.pack("<I", 0)  # desc_len = 0
    body += b"\x00\x00\x00" + struct.pack("<2I", 0x80040191, 0)
    client = _client(monkeypatch, _chunks(_giop_response(body, is_error=1)))
    ok, _ = client.read_system_count()
    assert ok is False
    assert client.connected is True
    assert client.last_error is not None and "0x80040191" in client.last_error
    assert client.last_error_code == 0x80040191


def test_link_error_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """链路错误码(通信回线未开 0x80A00101)→ OSError 语义拆连。"""
    body = struct.pack("<I", 0)
    body += b"\x00\x00\x00" + struct.pack("<2I", 0x80A00101, 0)
    client = _client(monkeypatch, _chunks(_giop_response(body, is_error=2)))
    ok, _ = client.read_nc_version()
    assert ok is False
    assert client.connected is False


def test_frame_length_over_limit_fast_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """帧长域超限在 recv 余量前快失败,按坏帧断线。"""
    evil = b"GIOP" + struct.pack("<HBB", 1, 1, 1) + struct.pack("<I", 0xFFFFFF)
    client = _client(monkeypatch, [evil[:12]])
    ok, _ = client.read_system_count()
    assert ok is False
    assert client.connected is False
    assert client.last_error is not None and "超限" in client.last_error


def test_read_run_state_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """状态拼合:MEM + 自动运转 1 + 状态 AUT → RUN。"""
    client = _client(
        monkeypatch,
        _chunks(_get_data_response(0x02, struct.pack("<h", 0)))  # mode=MEM
        + _chunks(_get_data_response(0x04, struct.pack("<q", 1)))  # auto=1
        + _chunks(_get_data_response(0x02, struct.pack("<h", 3))),  # status=AUT
    )
    ok, state = client.read_run_state()
    assert ok is True
    assert isinstance(state, EzRunState)
    assert state.device_status == EzDeviceStatus.RUN
    assert state.mode == EzRunMode.MEM
    assert state.run_status == EzRunStatus.AUTO
    assert state.auto_run is True


def test_read_run_state_emergency(monkeypatch: pytest.MonkeyPatch) -> None:
    """状态拼合:急停(状态码 1)→ STOP 优先。"""
    client = _client(
        monkeypatch,
        _chunks(_get_data_response(0x02, struct.pack("<h", 0)))
        + _chunks(_get_data_response(0x04, struct.pack("<q", 1)))
        + _chunks(_get_data_response(0x02, struct.pack("<h", 1))),
    )
    ok, state = client.read_run_state()
    assert ok is True and state is not None
    assert state.device_status == EzDeviceStatus.STOP
    assert state.run_status == EzRunStatus.EMERGENCY


def test_read_axis_position_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读轴位置:机械坐标(37/2),axis=1 → 位标志 0x01。"""
    data = struct.pack("<hhId", 5, 3, 0, -12.5)
    client = _client(monkeypatch, _chunks(_get_data_response(0x06, data)))
    ok, pos = client.read_axis_position(
        system_no=1, axis=1, position=EzPositionType.MACHINE
    )
    assert ok is True and pos == pytest.approx(-12.5)
    assert client._transport.sent == build_get_data_request(_RID, 37, 2, 1, 0x01, 0x06)


def test_read_feed_speed_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """进给速度分派:FC→33/1、FA/FM/FS→42/值+1、FE→42/4(review-1020 P1-2)。

    FE 枚举值 4 不能套「值+1」:C 库 case FE: 42,4,旧实现发 42/5。
    """
    for feed, section, sub in (
        (EzFeedSpeedType.FC, 33, 1),
        (EzFeedSpeedType.FA, 42, 1),
        (EzFeedSpeedType.FM, 42, 2),
        (EzFeedSpeedType.FS, 42, 3),
        (EzFeedSpeedType.FE, 42, 4),
    ):
        data = struct.pack("<hhId", 5, 3, 0, 1.5)
        client = _client(monkeypatch, _chunks(_get_data_response(0x06, data)))
        ok, speed = client.read_feed_speed(system_no=1, feed=feed)
        assert ok is True and speed == pytest.approx(1.5)
        assert client._transport.sent == build_get_data_request(
            _RID, section, sub, 1, 0, 0x06
        )
        client.disconnect()


def test_read_all_axis_positions_uses_axis_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全轴位置:先读轴数(2/2),再逐轴(位标志 0x01/0x02)。"""
    client = _client(
        monkeypatch,
        _chunks(_get_data_response(0x01, b"\x02"))  # 轴数 2
        + _chunks(_get_data_response(0x06, struct.pack("<hhId", 5, 3, 0, 1.0)))
        + _chunks(_get_data_response(0x06, struct.pack("<hhId", 5, 3, 0, 2.0))),
    )
    ok, positions = client.read_all_axis_positions()
    assert ok is True and positions == [1.0, 2.0]
    sent = client._transport.sent
    assert build_get_data_request(_RID, 2, 2, 0, 0, 0x01) in sent
    assert build_get_data_request(_RID, 37, 2, 1, 0x01, 0x06) in sent
    assert build_get_data_request(_RID, 37, 2, 1, 0x02, 0x06) in sent


def test_read_nc_version_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """读 NC 版本:T_STR 响应。"""
    client = _client(
        monkeypatch,
        _chunks(_get_data_response(0x10, struct.pack("<I", 7) + b"#0104[1A]")),
    )
    ok, version = client.read_nc_version()
    assert ok is True and version == "#0104[1"


def test_read_alarms_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读报警:请求帧(op[32])与两条解码。"""
    entry = struct.pack("<2i", 0, 0) + b"\x00" * 256  # 空条
    hit = struct.pack("<2i", 222, 2) + b"EM" + b"\x00" * 254
    client = _client(monkeypatch, _chunks(_giop_response(entry + hit)))
    ok, alarms = client.read_alarms(count=2)
    assert ok is True and len(alarms) == 2
    assert alarms[1].no == 222 and alarms[1].text == "EM"
    assert client._transport.sent == build_alarm_request(_RID, 1, 2, 0x000)


def test_read_is_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """报警中判定:文本长度 > 0 即报警。"""
    entry = struct.pack("<2i", 0, 0) + b"\x00" * 256
    client = _client(monkeypatch, _chunks(_giop_response(entry)))
    ok, is_alarm = client.read_is_alarm()
    assert ok is True and is_alarm is False


def test_read_program_block_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """读程序块:请求帧与首条解码。"""
    block = struct.pack("<4i", 7, 3, 0, 2) + b"O1" + b"\x00" * 510
    client = _client(monkeypatch, _chunks(_giop_response(block)))
    ok, parsed = client.read_program_block(rows=10)
    assert ok is True and parsed is not None
    assert parsed.block == 7 and parsed.row == 3 and parsed.text == "O1"
    assert client._transport.sent == build_prog_block_request(_RID, 1, 10)


# ----------------------------------------------------------------------
# 构造期校验与拒绝面
# ----------------------------------------------------------------------


def test_constructor_machine_validation() -> None:
    """machine 必须是枚举;合法枚举全接受。"""
    with pytest.raises(ValueError):
        MitsubishiEzSocketClient("127.0.0.1", 683, machine=6)
    client = MitsubishiEzSocketClient("127.0.0.1", 683)
    assert client.machine == EzSocketMachine.MELDAS700M


def test_parameter_validation_before_execute() -> None:
    """axis/system_no/count/rows/alarm_type 校验在事务前直接抛 ValueError。"""
    client = MitsubishiEzSocketClient("127.0.0.1", 683)
    with pytest.raises(ValueError):
        client.read_axis_position(axis=0)
    with pytest.raises(ValueError):
        client.read_axis_position(axis=9)
    with pytest.raises(ValueError):
        client.read_run_state(system_no=0)
    with pytest.raises(ValueError):
        client.read_alarms(count=11)
    with pytest.raises(ValueError):
        client.read_program_block(rows=0)
    with pytest.raises(ValueError):
        client.read_alarms(alarm_type=0)  # type: ignore[arg-type]


def test_generic_access_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """通用地址读写显式拒绝(结构化数采面;需已连接才会到达拒绝面)。"""
    client = _client(monkeypatch, [])
    assert client.read_float("D100") == (False, None)
    assert client.write_bool("M0", True) is False
    assert client.last_error is not None and "EZSocket" in client.last_error


def test_ping_probe_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """探活在未连接时走失败三件套(不抛出)。"""
    client = MitsubishiEzSocketClient("127.0.0.1", 683)
    assert client.ping() is False
    assert client.last_error is not None


# ----------------------------------------------------------------------
# aio 镜像
# ----------------------------------------------------------------------


def test_aio_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """aio 镜像:类级替换传输工厂,单工作线程完成建连 + 读数。"""
    import asyncio

    from omniplc.aio import AMitsubishiEzSocketClient

    chunks = _chunks(_get_data_response(0x01, b"\x04"))

    def _fake_transport(self: object) -> ScriptedTransport:
        return ScriptedTransport(chunks)

    monkeypatch.setattr(MitsubishiEzSocketClient, "_create_transport", _fake_transport)

    async def scenario() -> None:
        aio = AMitsubishiEzSocketClient("127.0.0.1", 683)
        aio._client()._request_id = _RID
        assert await aio.connect() is True
        assert aio.machine == EzSocketMachine.MELDAS700M
        ok, count = await aio.read_system_count()
        assert ok is True and count == 4
        ok, version = await aio.read_nc_version()
        assert ok is False  # 脚本分片已耗尽 → 失败三件套
        await aio.close()

    asyncio.run(scenario())
