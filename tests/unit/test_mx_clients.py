"""MX Component 客户端测试:假 COM 对象验证全链路。

COM 层(comtypes)以模块级助手函数隔离,测试替换为内存版假控件:

- 地址 → 控件软元件名的还原(D100/M10/D100.5)
- 单点(GetDevice/SetDevice)与块(ReadDeviceBlock/WriteDeviceBlock)调用参数
- 16 位符号、32/64 位小端字序、浮点、字符串编解码
- Open 出错代码 → 连接失败;(GetDevice 非零码)→ 标记断开 + 惰性重连
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

try:
    import comtypes
except ImportError:
    # 非 Windows 平台:comtypes 安装成功但导入期即抛(COM 仅限 Windows),
    # 模块级跳过,避免 pytest 收集期中断整轮门禁(ubuntu CI)
    pytest.skip(
        "comtypes(MX Component)仅限 Windows 平台",
        allow_module_level=True,
    )

from omniplc import MelsecMxClient
from omniplc.aio import AMelsecMxClient
from omniplc.core.errors import OmniPLCInternalError
from omniplc.plc.melsec import mx as mx_module
from omniplc.plc.melsec.melsec import _encode_32, _encode_64
from omniplc.types import DataType

_TEXT_RE = re.compile(r"^([A-Za-z]+)(\d+)(?:\.(\d+))?$")


class FakeActUtlType:
    """内存版 ActUtlType:按软元件名存值,可注入指定方法的出错码。"""

    def __init__(self) -> None:
        self.logical_station_number = -1
        self.memory: dict = {}
        self.calls: list = []
        self.open_code = 0
        self.codes: dict = {}

    def Open(self) -> int:
        self.calls.append(("Open",))
        return self.open_code

    def Close(self) -> int:
        self.calls.append(("Close",))
        return 0

    def _code(self, method: str, text: str) -> int:
        return self.codes.get((method, text), 0)

    def get(self, text: str) -> int:
        return int(self.memory.get(text, 0))

    def put(self, text: str, value: int) -> None:
        self.memory[text] = int(value) & 0xFFFF

    def block(self, text: str, count: int) -> list:
        match = _TEXT_RE.match(text)
        assert match is not None, "块访问需要十进制字软元件:" + text
        base = int(match.group(2))
        prefix = match.group(1)
        return [self.get("{}{}".format(prefix, base + i)) for i in range(count)]

    def random_read(self, texts: list) -> list:
        return [self.get(text) & 0xFFFF for text in texts]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeActUtlType:
    """替换 COM 交互层为内存版假控件。"""
    act = FakeActUtlType()

    def capture_new(station: int) -> FakeActUtlType:
        act.logical_station_number = station
        return act

    def fake_get(com: FakeActUtlType, text: str) -> int:
        com.calls.append(("GetDevice", text))
        mx_module._check_rc(com._code("GetDevice", text), "GetDevice")
        return com.get(text) & 0xFFFF

    def fake_set(com: FakeActUtlType, text: str, value: int) -> None:
        com.calls.append(("SetDevice", text, value))
        mx_module._check_rc(com._code("SetDevice", text), "SetDevice")
        com.put(text, value)

    def fake_read(com: FakeActUtlType, text: str, count: int) -> list:
        com.calls.append(("ReadDeviceBlock", text, count))
        mx_module._check_rc(com._code("ReadDeviceBlock", text), "ReadDeviceBlock")
        return [word & 0xFFFF for word in com.block(text, count)]

    def fake_write(com: FakeActUtlType, text: str, words: list) -> None:
        com.calls.append(("WriteDeviceBlock", text, len(words)))
        mx_module._check_rc(com._code("WriteDeviceBlock", text), "WriteDeviceBlock")
        for i, word in enumerate(words):
            match = _TEXT_RE.match(text)
            assert match is not None
            com.put("{}{}".format(match.group(1), int(match.group(2)) + i), word)

    def fake_random(com: FakeActUtlType, text: str, count: int) -> list:
        com.calls.append(("ReadDeviceRandom", text, count))
        mx_module._check_rc(com._code("ReadDeviceRandom", text), "ReadDeviceRandom")
        texts = text.split("\n")
        assert len(texts) == count, "随机读条数与换行分隔的软元件列表不符"
        return com.random_read(texts)

    def fake_write_random(
        com: FakeActUtlType, text: str, count: int, words: list
    ) -> None:
        com.calls.append(("WriteDeviceRandom", text, count))
        texts = text.split("\n")
        assert len(texts) == count, "随机写条数与换行分隔的软元件列表不符"
        for device, word in zip(texts, words):
            com.put(device, word)

    def fake_cpu_type(com: FakeActUtlType) -> tuple:
        com.calls.append(("GetCpuType",))
        return com.cpu_type

    def fake_get_clock(com: FakeActUtlType) -> dict:
        com.calls.append(("GetClockData",))
        return dict(com.clock)

    def fake_set_clock(
        com: FakeActUtlType, y: int, mo: int, d: int, dow: int, h: int, mi: int, s: int
    ) -> None:
        com.calls.append(("SetClockData", y, mo, d, dow, h, mi, s))
        com.clock = {
            "year": y, "month": mo, "day": d, "day_of_week": dow,
            "hour": h, "minute": mi, "second": s,
        }

    def fake_error_message(com: FakeActUtlType, code: int) -> str:
        com.calls.append(("GetErrorMessage", code))
        return "出错信息文本 0x{:08X}".format(code & 0xFFFFFFFF)

    monkeypatch.setattr(mx_module, "_com_initialize", lambda: None)
    monkeypatch.setattr(mx_module, "_new_com_object", capture_new)
    monkeypatch.setattr(mx_module, "_com_get_device", fake_get)
    monkeypatch.setattr(mx_module, "_com_set_device", fake_set)
    monkeypatch.setattr(mx_module, "_com_read_words", fake_read)
    monkeypatch.setattr(mx_module, "_com_write_words", fake_write)
    monkeypatch.setattr(mx_module, "_com_read_random", fake_random)
    monkeypatch.setattr(mx_module, "_com_write_random", fake_write_random)
    monkeypatch.setattr(mx_module, "_com_get_cpu_type", fake_cpu_type)
    monkeypatch.setattr(mx_module, "_com_get_clock_data", fake_get_clock)
    monkeypatch.setattr(mx_module, "_com_set_clock_data", fake_set_clock)
    monkeypatch.setattr(mx_module, "_com_get_error_message", fake_error_message)
    monkeypatch.setattr(mx_module, "_new_support_msg_com", lambda station: act)
    act.cpu_type = ("Q06HCPU", 0x0333)
    act.clock = {
        "year": 2026, "month": 9, "day": 23, "day_of_week": 2,
        "hour": 8, "minute": 5, "second": 30,
    }
    return act


def _client(station: int = 1) -> MelsecMxClient:
    return MelsecMxClient(logical_station_number=station)


# ----------------------------------------------------------------------
# 连接生命周期
# ----------------------------------------------------------------------

def test_open_close_cycle(fake: FakeActUtlType) -> None:
    client = _client(7)
    assert client.connect() is True
    assert fake.logical_station_number == 7
    assert fake.calls == [("Open",)]
    assert client.disconnect() is True
    assert fake.calls[-1] == ("Close",)
    assert client.connected is False


def test_open_failure_records_code(fake: FakeActUtlType) -> None:
    fake.open_code = 0xC0500100
    client = _client(1)
    assert client.connect() is False
    assert client.last_error is not None
    assert "返回码 0xC0500100" in client.last_error
    assert client.connected is False


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        _client(-1)
    with pytest.raises(ValueError):
        _client(1024)


def test_logical_station_property() -> None:
    assert _client(9).logical_station_number == 9


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------

def test_read_ushort_single_point(fake: FakeActUtlType) -> None:
    fake.put("D100", 20)
    client = _client()
    client.connect()
    assert client.read_ushort("D100") == (True, 20)
    assert ("GetDevice", "D100") in [tuple(call) for call in fake.calls]
    assert client.last_error is None


def test_read_short_negative(fake: FakeActUtlType) -> None:
    fake.put("D100", 0xFFFB)
    client = _client()
    client.connect()
    assert client.read_short("D100") == (True, -5)


def test_read_float_block(fake: FakeActUtlType) -> None:
    for i, word in enumerate(_encode_32(3.14, DataType.FLOAT)):
        fake.put("D{}".format(100 + i), word)
    client = _client()
    client.connect()
    ok, value = client.read_float("D100")
    assert ok is True
    assert value is not None and abs(value - 3.14) < 1e-6
    assert ("ReadDeviceBlock", "D100", 2) in [tuple(call) for call in fake.calls]


def test_read_double_block(fake: FakeActUtlType) -> None:
    for i, word in enumerate(_encode_64(123.456, DataType.DOUBLE)):
        fake.put("D{}".format(0 + i), word)
    client = _client()
    client.connect()
    ok, value = client.read_double("D0")
    assert ok is True
    assert value is not None and abs(value - 123.456) < 1e-9


def test_read_bool_bit_device(fake: FakeActUtlType) -> None:
    fake.put("M10", 1)
    client = _client()
    client.connect()
    assert client.read_bool("M10") == (True, True)
    fake.put("M10", 0)
    assert client.read_bool("M10") == (True, False)


def test_read_bool_word_bit(fake: FakeActUtlType) -> None:
    fake.put("D100", 0x0020)
    client = _client()
    client.connect()
    assert client.read_bool("D100.5") == (True, True)
    assert client.read_bool("D100.4") == (True, False)


def test_read_string(fake: FakeActUtlType) -> None:
    fake.put("D200", 0x4241)  # "AB" 小端
    fake.put("D201", 0x0043)  # "C\0"
    client = _client()
    client.connect()
    assert client.read_string("D200", 4) == (True, "ABC")


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------

def test_write_bool_bit_device(fake: FakeActUtlType) -> None:
    client = _client()
    client.connect()
    assert client.write_bool("M10", True) is True
    assert ("SetDevice", "M10", 1) in [tuple(call) for call in fake.calls]
    assert client.write_bool("M10", False) is True
    assert fake.get("M10") == 0


def test_write_bool_word_bit_read_modify_write(fake: FakeActUtlType) -> None:
    fake.put("D100", 0x0004)
    client = _client()
    client.connect()
    assert client.write_bool("D100.5", True) is True
    assert fake.get("D100") == 0x0024
    assert client.write_bool("D100.5", False) is True
    assert fake.get("D100") == 0x0004


def test_write_int_block(fake: FakeActUtlType) -> None:
    client = _client()
    client.connect()
    assert client.write_int("D100", -2) is True
    assert fake.get("D100") == 0xFFFE
    assert fake.get("D101") == 0xFFFF
    assert ("WriteDeviceBlock", "D100", 2) in [tuple(call) for call in fake.calls]


def test_write_string(fake: FakeActUtlType) -> None:
    client = _client()
    client.connect()
    assert client.write_string("D200", "ABC") is True
    assert fake.get("D200") == 0x4241
    assert fake.get("D201") == 0x0043


# ----------------------------------------------------------------------
# 错误处理
# ----------------------------------------------------------------------

def test_device_error_marks_disconnected_then_lazy_reconnect(
    fake: FakeActUtlType,
) -> None:
    fake.codes[("GetDevice", "D100")] = 0xC0500100
    client = _client()
    client.connect()
    assert client.read_ushort("D100") == (False, None)
    assert client.connected is False
    assert client.last_error is not None and "返回码 0xC0500100" in client.last_error
    # 链路恢复:清除出错码后下一次操作惰性重连成功
    fake.codes.clear()
    assert client.read_ushort("D100") == (True, 0)
    assert client.connected is True
    assert fake.calls.count(("Open",)) == 2


def test_block_size_cap(fake: FakeActUtlType) -> None:
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client._read_words("D0", 961)
    with pytest.raises(ValueError):
        client._write_words("D0", [0] * 961)


def test_bit_device_with_bit_suffix_rejected(fake: FakeActUtlType) -> None:
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client.read_bool("M10.3")


# ----------------------------------------------------------------------
# 异步镜像
# ----------------------------------------------------------------------

def test_async_mirror_roundtrip(fake: FakeActUtlType) -> None:
    fake.put("D100", 1234)

    async def scenario() -> None:
        client = AMelsecMxClient(3)
        assert client.logical_station_number == 3
        assert await client.connect() is True
        assert await client.read_ushort("D100") == (True, 1234)
        assert await client.write_batch([("M11", True), ("D105", 33)]) is True
        assert await client.get_cpu_type() == (True, ("Q06HCPU", 0x0333))
        ok, clock = await client.get_clock()
        assert ok is True and clock is not None and clock["year"] == 2026
        assert await client.set_clock(2026, 9, 23, 1, 2, 3) is True
        ok, text = await client.get_error_message(0xC0500100)
        assert ok is True and text is not None
        await client.close()

    asyncio.run(scenario())
    assert fake.logical_station_number == 3


def test_device_error_type() -> None:
    with pytest.raises(OmniPLCInternalError):
        mx_module._check_rc(0xC0500100, "GetDevice")


# ----------------------------------------------------------------------
# COM 助手真口径(comtypes 出参约定 + 原始 vtable 块读写;真机联测暴露的回归面)
# ----------------------------------------------------------------------


class ComtypesStyleActUtlType:
    """按 comtypes 真实挂载口径的假控件:

    - GetDevice 出参被高层包装收进返回值:单参调用即回数据
    - 块读写经 ``__com_*`` 原始 vtable 方法(类体内定义,Python 名字
      改写后的挂载键与 comtypes 一致保留 ``__com_<方法名>`` 后缀)
    """

    def __init__(self) -> None:
        self.written: Any = None
        self.written_random: Any = None
        self.read_code = 0

    def GetDevice(self, text: str) -> int:
        if text == "BAD":
            raise comtypes.COMError(-2147024894, "软元件不存在", None)
        return 1234

    def __com_ReadDeviceBlock(self, text: str, count: int, buffer: Any, retcode: Any) -> int:
        for index, word in enumerate([1, 2]):
            buffer[index] = word
        retcode.value = self.read_code
        return 0

    def __com_ReadDeviceRandom(self, text: str, count: int, buffer: Any, retcode: Any) -> int:
        for index, word in enumerate([3, 4]):
            buffer[index] = word
        retcode.value = 0
        return 0

    def __com_WriteDeviceBlock(self, text: str, count: int, data: Any, retcode: Any) -> int:
        self.written = data
        retcode.value = 0
        return 0

    def __com_WriteDeviceRandom(self, text: str, count: int, data: Any, retcode: Any) -> int:
        self.written_random = (text, data)
        retcode.value = 0
        return 0

    def GetCpuType(self) -> tuple:
        return ("Q06HCPU", 0x0333)

    def GetClockData(self) -> tuple:
        return (2026, 9, 23, 3, 12, 30, 15)

    def GetErrorMessage(self, code: int) -> str:
        if code == 0xBAD:
            raise comtypes.COMError(-2147024894, "未知出错代码", None)
        return "出错文本"


def test_com_helpers_comtypes_out_param_convention() -> None:
    """comtypes 口径:GetDevice 单参取出参;块读写走原始方法传自备缓冲。"""
    com = ComtypesStyleActUtlType()
    assert mx_module._com_get_device(com, "D100") == 1234
    assert mx_module._com_read_words(com, "D100", 2) == [1, 2]
    assert mx_module._com_read_random(com, "D0\nD2", 2) == [3, 4]
    mx_module._com_write_words(com, "D100", [5, 6])
    assert list(com.written) == [5, 6]
    assert mx_module._com_write_random(com, "M10\nD100", 2, [1, 0xFFFE]) is None
    assert com.written_random[0] == "M10\nD100"
    assert list(com.written_random[1]) == [1, 0xFFFE]
    assert mx_module._com_get_cpu_type(com) == ("Q06HCPU", 0x0333)
    assert mx_module._com_get_clock_data(com) == {
        "year": 2026, "month": 9, "day": 23, "day_of_week": 3,
        "hour": 12, "minute": 30, "second": 15,
    }
    assert mx_module._com_get_error_message(com, 0xC0500100) == "出错文本"
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_device(com, "BAD")  # COMError → 内部异常(断线)
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_error_message(com, 0xBAD)


def test_com_helpers_raw_error_code() -> None:
    """原始调用 lplRetCode 出参缓冲非 0 出错码 → 内部异常(标记断线,惰性重连)。"""
    com = ComtypesStyleActUtlType()
    com.read_code = 0xC0500100
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_read_words(com, "D100", 2)


def test_com_helpers_raw_hresult_error() -> None:
    """COM 层 HRESULT 非 0 → 内部异常(COM 调用失败,非 PLC 出错码)。"""

    class HresultFail:
        def __com_ReadDeviceBlock(self, text, count, buffer, retcode):
            retcode.value = 0
            return 0x80004005  # E_FAIL

    with pytest.raises(OmniPLCInternalError):
        mx_module._com_read_words(HresultFail(), "D100", 2)


def test_com_helpers_raw_method_missing() -> None:
    """未按类型库绑定(无 __com_* 挂载)→ 明确内部异常。"""
    with pytest.raises(OmniPLCInternalError):
        mx_module._raw_com_method(FakeActUtlType(), "ReadDeviceRandom")


def test_com_helpers_byref_fallback() -> None:
    """包装保留出参的版本(TypeError)→ 退回 byref VARIANT 形态。"""

    class ByrefStyle:
        def GetCpuType(self, *args):
            if not args:
                raise TypeError("call takes exactly 3 arguments (2 given)")
            # byref() 传入的是 CArgObject,经 _obj 取回原 VARIANT
            args[0]._obj.value = "Q06HCPU"
            args[1]._obj.value = 0x0333
            return 0

        def GetClockData(self, *args):
            if not args:
                raise TypeError("call takes exactly 8 arguments (1 given)")
            for arg, value in zip(args, [2026, 9, 23, 3, 12, 30, 15]):
                arg._obj.value = value
            return 0

        def GetErrorMessage(self, *args):
            if len(args) == 1:
                raise TypeError("call takes exactly 3 arguments (2 given)")
            args[1]._obj.value = "文本"
            return 0

    com = ByrefStyle()
    assert mx_module._com_get_cpu_type(com) == ("Q06HCPU", 0x0333)
    clock = mx_module._com_get_clock_data(com)
    assert clock["year"] == 2026 and clock["day_of_week"] == 3
    assert mx_module._com_get_error_message(com, 0xC0500100) == "文本"


def test_com_helpers_tuple_result_defensive() -> None:
    """含出参方法回 (数据, 码) 元组时取首个业务值;写返回码在元组中仍可取。"""

    class TupleFake:
        def GetDevice(self, text: str):
            return (1234, 0)

        def __com_WriteDeviceBlock(self, text: str, count: int, data: Any, retcode: Any):
            return (data, 0)

    com = TupleFake()
    assert mx_module._com_get_device(com, "D100") == 1234
    mx_module._com_write_words(com, "D100", [7])


# ----------------------------------------------------------------------
# 批量读取(ReadDeviceRandom 随机读)
# ----------------------------------------------------------------------

def test_read_batch_mixed(fake: FakeActUtlType) -> None:
    """随机批量读:位软元件/字软元件位/16 位类型混读,单事务。"""
    client = _client()
    fake.put("M10", 1)
    fake.put("D100", 0xFFFE)  # short -2
    fake.put("D102", 7)
    fake.put("D200", 0x0008)
    assert client.read_batch([
        ("M10", "bool"),
        ("D100", "short"),
        ("D102", "ushort"),
        ("D200.3", "bool"),
    ]) == (True, [True, -2, 7, True])
    kinds = [call[0] for call in fake.calls if call[0] == "ReadDeviceRandom"]
    assert kinds == ["ReadDeviceRandom"]  # 单事务
    random_calls = [call for call in fake.calls if call[0] == "ReadDeviceRandom"]
    assert random_calls[0][1] == "M10\nD100\nD102\nD200"


def test_read_many_random_single_transaction(fake: FakeActUtlType) -> None:
    """read_many:覆写为 ReadDeviceRandom 单事务(整批容错)。"""
    client = _client()
    fake.put("D0", 5)
    fake.put("D2", 300)
    assert client.read_many(["D0", "D2"], "ushort") == [(True, 5), (True, 300)]
    assert [call[0] for call in fake.calls].count("ReadDeviceRandom") == 1


def test_read_batch_rejects(fake: FakeActUtlType) -> None:
    """read_batch 拒绝路径:空列表、32 位类型(无法安全拆字)、条数超限。"""
    client = _client()
    with pytest.raises(ValueError):
        client.read_batch([])
    with pytest.raises(ValueError):
        client.read_batch([("D100", "int")])
    with pytest.raises(ValueError):
        client.read_batch([("D{}".format(index), "short") for index in range(961)])


# ----------------------------------------------------------------------
# 批量写入(WriteDeviceRandom 随机写)/ CPU 型号 / 时钟 / 出错文本
# ----------------------------------------------------------------------


def test_write_batch_mixed(fake: FakeActUtlType) -> None:
    """随机批量写:位软元件 bool + 字软元件 int 混写,单事务。"""
    client = _client()
    client.connect()
    assert client.write_batch([("M10", True), ("D100", -2), ("D102", 7)]) is True
    assert fake.get("M10") == 1
    assert fake.get("D100") == 0xFFFE  # -2 补码
    assert fake.get("D102") == 7
    random_calls = [call for call in fake.calls if call[0] == "WriteDeviceRandom"]
    assert len(random_calls) == 1  # 单事务
    assert random_calls[0][1] == "M10\nD100\nD102"


def test_write_batch_rejects(fake: FakeActUtlType) -> None:
    """write_batch 拒绝路径:空列表/字软元件位号/字软元件 bool/越界/类型/超限。"""
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client.write_batch([])
    with pytest.raises(ValueError):
        client.write_batch([("D200.3", True)])  # 字软元件位号(RMW 语义)
    with pytest.raises(ValueError):
        client.write_batch([("D100", True)])  # 字软元件不支持 bool
    with pytest.raises(ValueError):
        client.write_batch([("D100", 70000)])  # 超出 16 位范围
    with pytest.raises(ValueError):
        client.write_batch([("D100", 3.14)])  # 浮点不支持
    with pytest.raises(ValueError):
        client.write_batch([("D{}".format(index), 0) for index in range(961)])
    assert fake.calls[-1][0] != "WriteDeviceRandom"  # 拒绝路径不发请求
    # 整数值写位软元件合法(1/0)
    assert client.write_batch([("M20", 1)]) is True
    assert fake.get("M20") == 1


def test_get_cpu_type(fake: FakeActUtlType) -> None:
    """GetCpuType:返回 (型号字符串, 型号代码)。"""
    client = _client()
    client.connect()
    assert client.get_cpu_type() == (True, ("Q06HCPU", 0x0333))


def test_get_set_clock(fake: FakeActUtlType) -> None:
    """时钟读写:GetClockData 返回七字段字典;SetClockData 字段逐一透传。"""
    client = _client()
    client.connect()
    ok, clock = client.get_clock()
    assert ok is True and clock is not None
    assert clock["year"] == 2026 and clock["second"] == 30
    assert client.set_clock(2026, 9, 23, 12, 30, 15, day_of_week=3) is True
    set_calls = [call for call in fake.calls if call[0] == "SetClockData"]
    assert set_calls == [("SetClockData", 2026, 9, 23, 3, 12, 30, 15)]


def test_get_error_message(fake: FakeActUtlType) -> None:
    """GetErrorMessage:出错代码转官方文本(经 ActSupportMsg)。"""
    client = _client()
    client.connect()
    ok, text = client.get_error_message(0xC0500100)
    assert ok is True and text is not None and "0xC0500100" in text
