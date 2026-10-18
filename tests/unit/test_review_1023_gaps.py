"""review-1023 测试缺口收口(§9.1:FC02 全链路 / 翻页三守卫 / native
station·word_order / 写位 1968 切片 / FC20 子请求边界)+ 黄金样本再生成门禁
(§9.3:手改 JSON 无测试能发现的盲区)。
"""

from __future__ import annotations

import asyncio
import runpy
from pathlib import Path
from typing import List, Optional, Tuple

import pytest
from scripted import ScriptedTransport as _ScriptedTransport

from omniplc import ModbusRtuClient, ModbusTcpClient
from omniplc.core.errors import ErrorCategory
from omniplc.core.types import DataType
from omniplc.native import AsyncModbusTcpClient
from omniplc.native.transport import AsyncBaseTransport
from omniplc.plc.modbus import codec

# FC02 读 1 个离散输入、字节计数 1、值 0b00001010 的标准响应 PDU
_RESPONSE_ONE_DISCRETE = bytes([2, 1, 0x0B])


# ----------------------------------------------------------------------
# §9.1-1:FC02(离散输入)全链路成功读——此前任何层级零用例
# ----------------------------------------------------------------------


def test_tcp_read_discrete_input_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP:di 位读全链路(FC02 请求帧 + 位提取);FC02 与 FC01 只差功能码,
    正是"复制粘贴改错"最易漏测的形态。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = _mbap(1, 1, _RESPONSE_ONE_DISCRETE)
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.read_bool("di3") == (True, True)  # 单点读 offset=3,响应 bit0
    assert bytes(scripted.sent) == _mbap(1, 1, codec.build_read_pdu(2, 3, 1))


def test_rtu_read_discrete_input_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RTU:di 位读走 CRC 帧,增量收包按响应长度精确收满。"""
    client = ModbusRtuClient(station=1)
    frame = _rtu(1, _RESPONSE_ONE_DISCRETE)
    scripted = _ScriptedTransport([frame[:2], frame[2:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    assert client.connect() is True
    assert client.read_bool("di3") == (True, True)  # 单点读 offset=3,响应 bit0
    assert bytes(scripted.sent) == _rtu(1, codec.build_read_pdu(2, 3, 1))


def test_native_read_discrete_input_roundtrip() -> None:
    """native:di 位读(响应按 MBAP 分片喂给 recv)。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    frame = _mbap(1, 1, _RESPONSE_ONE_DISCRETE)
    transport = _NativeScripted([frame[:7], frame[7:]])
    client._create_transport = lambda: transport  # type: ignore[method-assign]

    async def scenario() -> Tuple[bool, Optional[bool]]:
        await client.connect()
        try:
            return await client.read_bool("di3")
        finally:
            await client.close()

    ok, value = asyncio.run(scenario())
    assert ok is True and value is True
    assert bytes(transport.sent) == _mbap(1, 1, codec.build_read_pdu(2, 3, 1))


# ----------------------------------------------------------------------
# §9.1-2:设备标识翻页三守卫(防恶意/故障设备把客户端钉死)
# ----------------------------------------------------------------------


def _device_id_pdu(
    more_follows: int, next_object_id: int, object_id: int = 0x00
) -> bytes:
    """单对象 FC43 响应 PDU(basic 流式;对象 0 = "V",长度 1)。"""
    return (
        bytes([0x2B, 0x0E, 0x01, 0x01, more_follows, next_object_id, 1, object_id, 1])
        + b"V"
    )


def test_device_id_paging_rejects_reserved_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫一:More-follows 恒 0xFF 且 next_object_id 落保留区(0x07~0x7F)
    → DeviceError(设备侧坏指针),不以 ValueError 逃出。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = _mbap(1, 1, _device_id_pdu(0xFF, 0x10))
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, _text = client.read_device_id("basic")
    assert ok is False
    assert client.last_error_category is ErrorCategory.DEVICE
    assert "翻页指针非法" in (client.last_error or "")


def test_device_id_paging_rejects_non_convergent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫二:next_object_id 停在与本页起始相同的对象号 → 翻页不收敛。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    frame = _mbap(1, 1, _device_id_pdu(0xFF, 0x00))
    scripted = _ScriptedTransport([frame[:7], frame[7:]])
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, _text = client.read_device_id("basic")
    assert ok is False
    assert client.last_error_category is ErrorCategory.PROTOCOL
    assert "翻页不收敛" in (client.last_error or "")


def test_device_id_paging_caps_at_max_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守卫三:More-follows 恒 0xFF 且对象号逐页递增(0x80 起,不落保留区)
    → 耗尽 MODBUS_DEVICE_ID_MAX_PAGES(8)页后拒绝,循环必有上界。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    chunks: List[bytes] = []
    for page in range(8):
        # 第 page 页从对象 0x80+page 起,More-follows 恒 0xFF、下一页 +1
        pdu = _device_id_pdu(0xFF, 0x80 + page + 1, 0x80 + page)
        frame = _mbap(page + 1, 1, pdu)
        chunks.extend([frame[:7], frame[7:]])
    scripted = _ScriptedTransport(chunks)
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    ok, _text = client.read_device_id("basic")
    assert ok is False
    assert client.last_error_category is ErrorCategory.PROTOCOL
    assert "翻页次数超过上限" in (client.last_error or "")


# ----------------------------------------------------------------------
# §9.1-3:native station / word_order 行为(此前零用例;方法集守卫
# 只锁"方法存在",锁不住行为——review-1001/1002 修过的漂移类型)
# ----------------------------------------------------------------------


def test_native_station_bounds() -> None:
    """native station:TCP Unit ID 0~255(0xFF 路由字段合法),300 拒。"""
    assert AsyncModbusTcpClient("127.0.0.1", 502, 255).station == 255
    with pytest.raises(ValueError, match="Unit ID"):
        AsyncModbusTcpClient("127.0.0.1", 502, 300)


def test_native_word_order_cdab_applies_to_decoding() -> None:
    """native word_order=CDAB:整字反转作用于 32 位解码(与同步层四字序
    口径一致:ABCD=恒等 / DCBA=整体反转 / CDAB=整字反转 / BADC=字内交换;
    规范序 0x01020304 的 ABCD 寄存器 [0x0102, 0x0304] → CDAB 线上
    [0x0304, 0x0102])。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 1)
    client.word_order = "CDAB"
    response = _mbap(1, 1, bytes([0x03, 0x04, 0x03, 0x04, 0x01, 0x02]))
    transport = _NativeScripted([response[:7], response[7:]])
    client._create_transport = lambda: transport  # type: ignore[method-assign]

    async def scenario() -> Tuple[bool, Optional[int]]:
        await client.connect()
        try:
            return await client.read("hr0", DataType.UINT)
        finally:
            await client.close()

    ok, value = asyncio.run(scenario())
    assert ok is True and value == 0x01020304
    # 请求帧:MBAP(tid=1, station=1) + FC03 读 2 字;station 落在第 7 字节
    assert bytes(transport.sent) == _mbap(1, 1, codec.build_read_pdu(3, 0, 2))
    assert bytes(transport.sent)[6] == 1


def test_native_station_carried_into_request_frame() -> None:
    """native station=255:请求帧 Unit ID 字段携带 255(路由字段语义)。"""
    client = AsyncModbusTcpClient("127.0.0.1", 502, 255)
    response = _mbap(1, 255, bytes([0x03, 0x02, 0x00, 0x14]))
    transport = _NativeScripted([response[:7], response[7:]])
    client._create_transport = lambda: transport  # type: ignore[method-assign]

    async def scenario() -> Tuple[bool, Optional[int]]:
        await client.connect()
        try:
            return await client.read_ushort("hr0")
        finally:
            await client.close()

    ok, value = asyncio.run(scenario())
    assert ok is True and value == 20
    assert bytes(transport.sent) == _mbap(1, 255, codec.build_read_pdu(3, 0, 1))
    assert bytes(transport.sent)[6] == 255


# ----------------------------------------------------------------------
# §9.1-5:写位 1968 上限切片(写位分支是 review-1019 优化同款逻辑,
# 读侧/写字侧均有用例,唯独写位没有)
# ----------------------------------------------------------------------


def test_tcp_write_bits_1968_top_and_1969_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_many 连续位:1968 位(FC15 写位上限)一笔;1969 位切两笔
    (1968 + 1),各自独立事务、逐笔回显校验。"""
    client = ModbusTcpClient("127.0.0.1", 502, 1)
    items = [(f"c{i}", "bool", True) for i in range(1969)]
    pdu_top = codec.build_write_multi_pdu(15, 0, [1] * 1968)
    pdu_tail = codec.build_write_multi_pdu(15, 1968, [1])
    scripted = _ScriptedTransport(
        [
            _mbap(1, 1, pdu_top[:5])[:7],
            _mbap(1, 1, pdu_top[:5])[7:],
            _mbap(2, 1, pdu_tail[:5])[:7],
            _mbap(2, 1, pdu_tail[:5])[7:],
        ]
    )
    monkeypatch.setattr(client, "_create_transport", lambda: scripted)
    client.connect()
    results = client.write_many(items)
    assert all(results)
    assert bytes(scripted.sent) == _mbap(1, 1, pdu_top) + _mbap(2, 1, pdu_tail)


# ----------------------------------------------------------------------
# §9.1-4:FC20 子请求条数边界(35 = 0xF5 // 7,与 byte count 上限同源)
# ----------------------------------------------------------------------


def test_fc20_subrequest_count_boundary() -> None:
    """FC20:35 条子请求(byte count 顶格 0xF5)合法,36 条拒绝。"""
    requests = [(1, index, 1) for index in range(35)]
    pdu = codec.build_read_file_record_pdu(requests)
    assert pdu[1] == 0xF5  # 35 × 7 = 245 字节顶格
    with pytest.raises(ValueError, match="子请求条数"):
        codec.build_read_file_record_pdu(requests + [(1, 35, 1)])


# ----------------------------------------------------------------------
# §9.3:黄金样本再生成门禁(生成器独立实现重跑,逐字节比对)
# ----------------------------------------------------------------------


def test_modbus_golden_samples_reproducible() -> None:
    """重跑独立实现生成器,modbus_*.json 必须逐字节复现——手工改 JSON
    (README 曾鼓励)而实现未变时,本门禁红。"""
    golden = Path(__file__).resolve().parents[1] / "golden"
    names = sorted(p.name for p in golden.glob("modbus_*.json"))
    assert len(names) >= 15, f"黄金样本异常:{names}"
    before = {name: (golden / name).read_bytes() for name in names}
    try:
        runpy.run_path(str(golden / "generate_modbus_samples.py"), run_name="__main__")
        for name in names:
            assert (golden / name).read_bytes() == before[name], name
    finally:
        # 测试不留副作用:比对完成后恢复原字节
        for name, data in before.items():
            (golden / name).write_bytes(data)


# ----------------------------------------------------------------------
# 本文件自用助手(帧合成)
# ----------------------------------------------------------------------


def _mbap(transaction_id: int, station: int, pdu: bytes) -> bytes:
    return codec.build_mbap(transaction_id, station, pdu)


def _rtu(station: int, pdu: bytes) -> bytes:
    return codec.build_rtu_frame(station, pdu)


class _NativeScripted(AsyncBaseTransport):
    """最小原生桩:按 recv 调用粒度出队分片(同 test_native_core 形态)。"""

    def __init__(self, chunks: List[bytes]) -> None:
        self._queue = list(chunks)
        self.sent = bytearray()

    async def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def send(self, data: bytes) -> None:
        self.sent.extend(data)

    async def recv(self, size: int) -> bytes:
        chunk = self._queue.pop(0)
        assert len(chunk) == size, f"分片长度与 recv 期望不符:{len(chunk)} vs {size}"
        return chunk
