"""MX Component 客户端测试:假 COM 对象验证全链路。

COM 层(comtypes)以模块级助手函数隔离,测试替换为内存版假控件:

- 地址 → 控件软元件名的还原(D100/M10/D100.5)
- 单点(GetDevice/SetDevice)与块(ReadDeviceBlock/WriteDeviceBlock)调用参数
- 16 位符号、32/64 位小端字序、浮点、字符串编解码
- Open 出错代码 → 连接失败;(GetDevice 非零码)→ 标记断开 + 惰性重连
"""

from __future__ import annotations

import asyncio
import ctypes
import re
import weakref
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
from omniplc.core.errors import DeviceError, OmniPLCInternalError
from omniplc.core.types import DataType
from omniplc.plc.melsec import mx as mx_module
from omniplc.plc.melsec.melsec import _encode_32, _encode_64

_TEXT_RE = re.compile(r"^([A-Za-z]+)(\d+)(?:\.(\d+))?$")

_REAL_GET_ERROR_MESSAGE = mx_module._com_get_error_message
"""真 GetErrorMessage 助手(夹具会替换为假件;需要走真实现的用例显式换回)。"""

_SYS_ALLOC_STRING = ctypes.oledll.oleaut32.SysAllocString
_SYS_ALLOC_STRING.restype = ctypes.c_void_p  # BSTR 指针;缺省 c_int 在 64 位截断


def _set_bstr(buffer: Any, text: str) -> None:
    """按真控件同款写 BSTR 出参:``SysAllocString`` 出真 BSTR 指针再写入。

    ``_com_get_cpu_type``/``_com_get_error_message`` 读后 ``SysFreeString``
    释放——桩若用 ``.value =`` 写入(ctypes 内部缓冲,非 BSTR 布局),
    释放即错释放;与真控件同构才能验全生命周期。
    """
    pointer = _SYS_ALLOC_STRING(text)
    ctypes.memmove(
        ctypes.addressof(buffer),
        ctypes.byref(ctypes.c_void_p(pointer)),
        ctypes.sizeof(ctypes.c_void_p),
    )


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
        return [self.get(f"{prefix}{base + i}") for i in range(count)]

    def random_read(self, texts: list) -> list:
        return [self.get(text) & 0xFFFF for text in texts]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeActUtlType:  # noqa: C901
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
            com.put(f"{match.group(1)}{int(match.group(2)) + i}", word)

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
            "year": y,
            "month": mo,
            "day": d,
            "day_of_week": dow,
            "hour": h,
            "minute": mi,
            "second": s,
        }

    def fake_error_message(com: FakeActUtlType, code: int) -> str:
        com.calls.append(("GetErrorMessage", code))
        return f"出错信息文本 0x{code & 0xFFFFFFFF:08X}"

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
        "year": 2026,
        "month": 9,
        "day": 23,
        "day_of_week": 2,
        "hour": 8,
        "minute": 5,
        "second": 30,
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
        fake.put(f"D{100 + i}", word)
    client = _client()
    client.connect()
    ok, value = client.read_float("D100")
    assert ok is True
    assert value is not None and abs(value - 3.14) < 1e-6
    assert ("ReadDeviceBlock", "D100", 2) in [tuple(call) for call in fake.calls]


def test_read_double_block(fake: FakeActUtlType) -> None:
    for i, word in enumerate(_encode_64(123.456, DataType.DOUBLE)):
        fake.put(f"D{0 + i}", word)
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


def test_aio_two_clients_isolated_stations(
    fake: FakeActUtlType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aio 双 MX 客户端:各用独立控件实例与工作线程,内存互不串(盲点清缴)。

    ActUtlType 是 STA 控件——aio 层每实例单工作线程,COM 控件按逻辑站号
    各建一份;本用例以按站隔离的内存控件锁"双客户端并发采集互不串数据"。
    """
    import asyncio

    from omniplc.aio import AMelsecMxClient

    acts: dict = {}

    def factory(station: int) -> FakeActUtlType:
        if station not in acts:
            acts[station] = FakeActUtlType()
        return acts[station]

    monkeypatch.setattr(mx_module, "_new_com_object", factory)
    factory(1).put("D0", 11)
    factory(2).put("D0", 22)

    async def scenario() -> None:
        client_a = AMelsecMxClient(1)
        client_b = AMelsecMxClient(2)
        first, second = await asyncio.gather(
            client_a.read_ushort("D0"), client_b.read_ushort("D0")
        )
        assert first == (True, 11)
        assert second == (True, 22)
        await client_a.close()
        await client_b.close()

    asyncio.run(scenario())


def test_device_error_type() -> None:
    with pytest.raises(OmniPLCInternalError):
        mx_module._check_rc(0xC0500100, "GetDevice")


# ----------------------------------------------------------------------
# COM 助手真口径(comtypes 出参约定 + 原始 vtable 块读写;真机联测暴露的回归面)
# ----------------------------------------------------------------------


class ComtypesStyleActUtlType:
    """按 comtypes 真实挂载口径的假控件:

    - 块读写 + 4 个新方法都经 ``__com_*`` 原始 vtable 方法(类体内
      定义,Python 名字改写后的挂载键与 comtypes 一致保留
      ``__com_<方法名>`` 后缀);review-1024 §9.2-1 P1 后 GetDevice/
      GetCpuType/GetClockData/GetErrorMessage 全部走 raw 通道,高位 0
      出错码写入 ``lplRetCode`` 而**不抛** COMError 正是修复要验证的
      关键路径。
    """

    def __init__(self) -> None:
        self.written: Any = None
        self.written_random: Any = None
        self.read_code = 0
        self.get_device_code = 0  # lplRetCode for __com_GetDevice
        self.get_cpu_type_code = 0
        self.get_clock_data_code = 0
        self.get_error_message_code = 0

    def __com_ReadDeviceBlock(
        self, text: str, count: int, buffer: Any, retcode: Any
    ) -> int:
        for index, word in enumerate([1, 2]):
            buffer[index] = word
        retcode.value = self.read_code
        return 0

    def __com_ReadDeviceRandom(
        self, text: str, count: int, buffer: Any, retcode: Any
    ) -> int:
        for index, word in enumerate([3, 4]):
            buffer[index] = word
        retcode.value = 0
        return 0

    def __com_WriteDeviceBlock(
        self, text: str, count: int, data: Any, retcode: Any
    ) -> int:
        self.written = data
        retcode.value = 0
        return 0

    def __com_WriteDeviceRandom(
        self, text: str, count: int, data: Any, retcode: Any
    ) -> int:
        self.written_random = (text, data)
        retcode.value = 0
        return 0

    def __com_GetDevice(self, text: str, lplData: Any, lplRetCode: Any) -> int:
        # 高位 0 出错码路径(0x010A42A0 访问口令不符等):写入 lplRetCode
        # 而不抛 COMError——这是 review-1024 §9.2-1 P1 修复要锁定的盲区。
        # ctypes.byref(c_long) 在 Python 函数侧收 CArgObject 代理,
        # 经 _obj 取回原 ctypes 实例再 .value 写入。
        lplData._obj.value = 1234 if self.get_device_code == 0 else 0
        lplRetCode._obj.value = self.get_device_code
        return 0

    def __com_GetCpuType(self, szCpuName: Any, lCpuType: Any, lplRetCode: Any) -> int:
        _set_bstr(szCpuName._obj, "Q06HCPU")
        lCpuType._obj.value = 0x0333
        lplRetCode._obj.value = self.get_cpu_type_code
        return 0

    def __com_GetClockData(
        self,
        iYear: Any,
        iMonth: Any,
        iDay: Any,
        iDayOfWeek: Any,
        iHour: Any,
        iMinute: Any,
        iSecond: Any,
        lplRetCode: Any,
    ) -> int:
        iYear._obj.value = 2026
        iMonth._obj.value = 9
        iDay._obj.value = 23
        iDayOfWeek._obj.value = 3
        iHour._obj.value = 12
        iMinute._obj.value = 30
        iSecond._obj.value = 15
        lplRetCode._obj.value = self.get_clock_data_code
        return 0

    def __com_GetErrorMessage(
        self, lErrorCode: int, szErrorMessage: Any, lplRetCode: Any
    ) -> int:
        if lErrorCode == 0xBAD:
            raise comtypes.COMError(-2147024894, "未知出错代码", None)
        _set_bstr(szErrorMessage._obj, f"出错文本 0x{lErrorCode & 0xFFFFFFFF:08X}")
        lplRetCode._obj.value = self.get_error_message_code
        return 0


def test_com_helpers_comtypes_out_param_convention() -> None:
    """comtypes 口径:全部 8 个方法经 ``__com_*`` 原始 vtable 通道。

    review-1024 §9.2-1 P1:4 个原高层包装(GetDevice/GetCpuType/
    GetClockData/GetErrorMessage)改走 raw 通道,自备 ctypes 出参
    缓冲 + ``lplRetCode`` 显式校验,高层包装形态(单参调用收进返回
    值/byref 回退)已弃用——本测试验证新通道正确读写缓冲区与返回码。
    """
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
        "year": 2026,
        "month": 9,
        "day": 23,
        "day_of_week": 3,
        "hour": 12,
        "minute": 30,
        "second": 15,
    }
    assert mx_module._com_get_error_message(com, 0xC0500100) == ("出错文本 0xC0500100")
    # COMError 抛错路径仍走原始 vtable
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_error_message(com, 0xBAD)


def test_com_bstr_out_params_marshal_comtypes_bstr() -> None:
    """BSTR 出参(GetCpuType/GetErrorMessage)必须传 comtypes.BSTR 缓冲。

    真类型库(comtypes/gen ActUtlType dump)对 GetCpuType 的声明是
    ``(POINTER(BSTR), POINTER(c_int), POINTER(c_int))``;ctypes 指针
    形参严格查型,``byref(create_unicode_buffer(N))``(wchar 数组
    指针)必抛 ``ArgumentError: expected LP_BSTR instance instead of
    pointer to c_wchar_Array_N``——2026-10-28 现场 GetCpuType 必败的
    根因(用户见到的 "point array" 即报错原文里的 "pointer … Array")。
    旧桩「收什么都能写」验不出该缺陷,本测试以**真 argtypes 的
    ctypes 函数桩**锁定调用形态(整数缓冲不受此限:byref(c_long)
    对 POINTER(c_int) 同宽放行,故其余 raw 方法无恙)。
    """
    from comtypes import BSTR

    @ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        ctypes.POINTER(BSTR),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    )
    def raw_get_cpu_type(sz_cpu_name, lpl_cpu_code, lpl_retcode):
        _set_bstr(sz_cpu_name.contents, "Q06HCPU")
        lpl_cpu_code.contents.value = 0x0333
        lpl_retcode.contents.value = 0
        return 0

    class ComCpu:
        __com_GetCpuType = raw_get_cpu_type

    assert mx_module._com_get_cpu_type(ComCpu()) == ("Q06HCPU", 0x0333)

    @ctypes.WINFUNCTYPE(
        ctypes.HRESULT,
        ctypes.c_long,
        ctypes.POINTER(BSTR),
        ctypes.POINTER(ctypes.c_int),
    )
    def raw_get_error_message(l_error_code, sz_error_message, lpl_retcode):
        _set_bstr(sz_error_message.contents, "出错文本")
        lpl_retcode.contents.value = 0
        return 0

    class ComMsg:
        __com_GetErrorMessage = raw_get_error_message

    assert mx_module._com_get_error_message(ComMsg(), 0xC0500100) == "出错文本"


def test_com_helpers_raw_retcode_propagates_high_bit_zero_error() -> None:
    """review-1024 §9.2-1 P1 修复价值:raw 通道 lplRetCode 非 0 出错码
    写入而不抛 COMError 时,``_com_get_device`` 等应感知并抛内部异常。

    高位 0 错误码(如 0x010A42A0 访问口令不符)是 P1 修复的核心盲区:
    旧高层包装路径下 PLC 写入 ``lplRetCode`` 但**不抛** COMError,本
    函数会静默返回出参缓冲残留值——即静默错值。raw 通道显式校验
    ``lplRetCode`` 后,该路径被收口。
    """
    com = ComtypesStyleActUtlType()
    # 模拟"业务出错时 HRESULT=S_OK, lplRetCode 写入 0x010A42A0"
    com.get_device_code = 0x010A42A0
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_device(com, "D100")
    com.get_cpu_type_code = 0x01800000
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_cpu_type(com)
    com.get_clock_data_code = 0x0001FFFF
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_clock_data(com)
    com.get_error_message_code = 0xC0500100
    with pytest.raises(DeviceError):
        # get_error_message 走 DeviceError(链路完好,文本查询失败)
        mx_module._com_get_error_message(com, 0xC0500100)


def test_com_helpers_raw_error_code() -> None:
    """原始调用 lplRetCode 出参缓冲非 0 出错码 → 内部异常(标记断线,惰性重连)。"""
    com = ComtypesStyleActUtlType()
    com.read_code = 0xC0500100
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_read_words(com, "D100", 2)


def test_raw_com_method_rebinds_new_instance_after_reconnect() -> None:
    """重连新建 COM 实例后原始方法重新绑定到新实例(review-1019 P1-3)。

    记忆化只落**属性名**:若缓存绑定方法,重连后的新控件会拿到旧实例
    已释放的接口指针——两实例同型,方法必须各自绑定。
    """
    com_a = ComtypesStyleActUtlType()
    com_b = ComtypesStyleActUtlType()
    fn_a = mx_module._raw_com_method(com_a, "ReadDeviceBlock")
    fn_b = mx_module._raw_com_method(com_b, "ReadDeviceBlock")
    assert fn_a.__self__ is com_a
    assert fn_b.__self__ is com_b
    assert fn_a is not fn_b


def test_raw_com_attr_resolution_memoized_per_type() -> None:
    """属性名定位按 COM 类型记忆化:同类型重复解析返回同一结果(review-1019 P1-3)。"""
    com_a = ComtypesStyleActUtlType()
    name_first = mx_module._locate_raw_com_attr(type(com_a), "ReadDeviceBlock")
    name_again = mx_module._locate_raw_com_attr(type(com_a), "ReadDeviceBlock")
    assert name_again is name_first  # lru_cache 命中:同一 str 对象
    assert name_again.lower().endswith("__com_readdeviceblock")


def test_com_get_error_message_releases_reference_on_error() -> None:
    """GetErrorMessage 失败路径不扣留 ActSupportMsg 代理引用(配对收口)。

    review-1024 §9.2-1 P1 后 ``_com_get_error_message`` 走 raw 通道,
    不再有 TypeError 探测 + byref 回退形态——lplRetCode 非 0 直接
    转 :class:`DeviceError`(链路完好,文本查询失败)。本测试断言
    失败路径不扣留 STA 控件代理。
    """

    class RcFailMsg:
        def __com_GetErrorMessage(self, code: int, message: Any, retcode: Any) -> int:
            retcode._obj.value = 0xC0500100  # 模拟文本查询失败
            return 0

    obj = RcFailMsg()
    ref = weakref.ref(obj)
    box: list = []

    def call(target: Any) -> None:
        try:
            mx_module._com_get_error_message(target, 0xBAD)
        except DeviceError as exc:
            target = None  # 排除测试自身帧对代理的引用
            box.append(exc)

    call(obj)
    del obj  # 调用方用完即弃(排除本帧对代理的引用)
    assert box, "非 0 返回码应抛 DeviceError"
    assert ref() is None, "异常路径不得扣留控件代理引用"


def test_get_error_message_releases_support_msg_control(
    fake: FakeActUtlType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_error_message 用毕即时释放 ActSupportMsg 控件(创建点配对收口)。

    守卫:控件每次调用新建(刻意——STA 控件绑定创建线程,缓存跨线程复用
    不可用),创建点的 ``finally`` 置空引用,成功路径返回即释放。
    """
    refs: list = []

    class TrackedMsg:
        def __com_GetErrorMessage(self, code: int, message: Any, retcode: Any) -> int:
            _set_bstr(message._obj, f"出错文本 0x{code & 0xFFFFFFFF:08X}")
            retcode._obj.value = 0
            return 0

    def new_com(station: int) -> TrackedMsg:
        obj = TrackedMsg()
        refs.append(weakref.ref(obj))
        return obj

    monkeypatch.setattr(mx_module, "_new_support_msg_com", new_com)
    monkeypatch.setattr(mx_module, "_com_get_error_message", _REAL_GET_ERROR_MESSAGE)
    client = _client()
    assert client.connect() is True
    ok, text = client.get_error_message(0xC0500100)
    assert ok is True and text == "出错文本 0xC0500100"
    assert refs[0]() is None


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


def test_com_helpers_byref_fallback_removed() -> None:
    """review-1024 §9.3 测试强度结构性缺口(已修):旧 byref 回退形态
    测试已删除,因 P1-B 修复后 ``_com_get_cpu_type/_com_get_clock_data/
    _com_get_error_message`` 不再走「TypeError 探测 → byref VARIANT
    回退」路径——直接走 raw vtable 通道(``__com_*``)。

    旧测试断言「fixture 同时支持高层包装 + byref 回退」恰好绕开
    P1 盲区,因两种实现都能返回相同结果。新实现强制走 raw 通道,
    旧 fixture 类型无 ``__com_*`` 挂载即抛 OmniPLCInternalError,
    业务无法绕过。占位测试保留以明示该路径已被消除。
    """
    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_cpu_type(object())  # 任意无 __com_* 挂载对象


def test_com_helpers_tuple_result_defensive_removed() -> None:
    """review-1024 §9.3 高层包装回 (数据, 码) 元组形态(已废):
    P1-B 修复后 ``_com_get_device`` 不再依赖 comtypes 高层包装
    「(数据, 码) 元组」回执——直接走 raw 通道,数据自 ctypes
    LONG 缓冲取值。占位测试保留以明示该形态已废。
    """

    class TupleFake:
        def GetDevice(self, text: str):  # 高层包装回元组——已废路径
            return (1234, 0)

    with pytest.raises(OmniPLCInternalError):
        mx_module._com_get_device(TupleFake(), "D100")  # 无 __com_GetDevice 挂载


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
    assert client.read_batch(
        [
            ("M10", "bool"),
            ("D100", "short"),
            ("D102", "ushort"),
            ("D200.3", "bool"),
        ]
    ) == (True, [True, -2, 7, True])
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


def test_read_batch_wide_types(fake: FakeActUtlType) -> None:
    """32/64 位条目各走块读(地址原文透传无法安全拆相邻字),顺序保持。"""
    client = _client()
    for index, word in enumerate(_encode_32(-2, DataType.INT)):
        fake.put(f"D{100 + index}", word)
    for index, word in enumerate(_encode_32(1.5, DataType.FLOAT)):
        fake.put(f"D{110 + index}", word)
    for index, word in enumerate(_encode_64(-2, DataType.LONG)):
        fake.put(f"D{120 + index}", word)
    ok, values = client.read_batch(
        [
            ("M10", "bool"),
            ("D100", "int"),
            ("D110", "float"),
            ("D120", "long"),
        ]
    )
    assert ok is True and values is not None
    assert values[0] is False  # M10 未置位(16 位随机读路径)
    assert values[1] == -2  # INT 32 位块读
    assert values[2] == pytest.approx(1.5)
    assert values[3] == -2  # LONG 64 位块读
    kinds = [call[0] for call in fake.calls]
    assert kinds.count("ReadDeviceRandom") == 1  # 16 位条目合并一笔
    assert kinds.count("ReadDeviceBlock") == 3  # 宽类型各一笔块读


def test_read_batch_rejects(fake: FakeActUtlType) -> None:
    """read_batch 拒绝路径:空列表、不支持类型(字符串)、条数超限。"""
    client = _client()
    with pytest.raises(ValueError):
        client.read_batch([])
    with pytest.raises(ValueError):
        client.read_batch([("D100", "string")])
    with pytest.raises(ValueError):
        client.read_batch([(f"D{index}", "short") for index in range(961)])


# ----------------------------------------------------------------------
# 连续批量读 read_range(ReadDeviceBlock 单事务)
# ----------------------------------------------------------------------


def test_read_range_shorts_block(fake: FakeActUtlType) -> None:
    """read_range:D100 起 3 个 SHORT = ReadDeviceBlock 3 字单事务。"""
    client = _client()
    fake.put("D100", 10)
    fake.put("D101", 20)
    fake.put("D102", 30)
    ok, values = client.read_range("D100", 3, "short")
    assert ok is True
    assert values == [10, 20, 30]
    kinds = [call[0] for call in fake.calls if call[0] == "ReadDeviceBlock"]
    assert kinds == ["ReadDeviceBlock"]  # 单事务


def test_read_range_ints_two_words_each(fake: FakeActUtlType) -> None:
    """read_range:D0 起 2 个 INT = ReadDeviceBlock 4 字,按 2 字/元素小端解码。"""
    client = _client()
    for index, word in enumerate(_encode_32(-2, DataType.INT)):
        fake.put(f"D{index}", word)
    for index, word in enumerate(_encode_32(300, DataType.UINT)):
        fake.put(f"D{2 + index}", word)
    ok, values = client.read_range("D0", 2, "uint")
    assert ok is True
    assert values == [4294967294, 300]
    block_calls = [call for call in fake.calls if call[0] == "ReadDeviceBlock"]
    assert block_calls == [("ReadDeviceBlock", "D0", 4)]


def test_read_range_rejects(fake: FakeActUtlType) -> None:
    """read_range 拒绝路径:count/STRING/位软元件 BOOL 连续读。"""
    client = _client()
    with pytest.raises(ValueError):
        client.read_range("D100", 0, "short")
    with pytest.raises(ValueError):
        client.read_range("D100", True, "short")
    with pytest.raises(ValueError):
        client.read_range("D100", 2, "string")
    with pytest.raises(ValueError):
        client.read_range("M0", 4, "bool")  # MX 位块读语义不一致,拒绝
    with pytest.raises(ValueError):
        client.read_range("D0", 961, "short")  # 961 字 > 960 上限


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
        client.write_batch([(f"D{index}", 0) for index in range(961)])
    assert fake.calls[-1][0] != "WriteDeviceRandom"  # 拒绝路径不发请求
    # 整数值写位软元件合法(1/0)
    assert client.write_batch([("M20", 1)]) is True
    assert fake.get("M20") == 1


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


def test_support_msg_progid_matches_manual() -> None:
    """ActSupportMsg 控件 ProgID 依手册 §1.2.1(ActSupportMsg.dll → ActSupportMsg.ActSupportMsg)。"""
    from omniplc.core.constants import MX_SUPPORT_MSG_PROG_ID

    assert MX_SUPPORT_MSG_PROG_ID == "ActSupportMsg.ActSupportMsg"


def test_support_msg_create_failure_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    """ActSupportMsg 控件创建失败 → 翻译为 OmniPLCInternalError(不逃出公开 API)。"""
    import comtypes.client

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("控件未注册")

    monkeypatch.setattr(comtypes.client, "CreateObject", boom)
    with pytest.raises(OmniPLCInternalError):
        mx_module._new_support_msg_com(1)


def test_get_error_message_creation_failure_returns_false(
    fake: FakeActUtlType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """控件创建失败时 get_error_message 返回 (False, None)(公共 API 契约)。"""
    client = _client()

    def boom(station: int) -> Any:
        raise OmniPLCInternalError("ActSupportMsg 控件创建失败")

    monkeypatch.setattr(mx_module, "_new_support_msg_com", boom)
    assert client.get_error_message(0xC0500100) == (False, None)


# ----------------------------------------------------------------------
# P3:位软元件字访问限制 / 位号后缀 / 位值范围 / 软元件表
# ----------------------------------------------------------------------


def test_bit_device_table_excludes_invalid_st() -> None:
    """位软元件表不得含手册外的 "ST"(手册为 STS/STC;步进继电器为 S)。"""
    from omniplc.core.constants import MX_BIT_DEVICES

    assert "ST" not in MX_BIT_DEVICES
    assert "STS" in MX_BIT_DEVICES and "STC" in MX_BIT_DEVICES


def test_bit_device_table_includes_cc_link_and_long_timers(
    fake: FakeActUtlType,
) -> None:
    """review-1024 §9.2-3 P2:补 11 类位软元件(CC-Link + 长定时/计数
    继电器),按 MX Component 手册 §2.3 印刷 46-48 软元件表。

    旧表仅 18 项,真机访问 ``FX0`` / ``FY0`` / ``MF0`` / ``LTS0`` 等
    会落入"按字软元件处理",位/字门控(``_is_bit_device``)漏——
    ``read_ushort("FX0")`` 静默按字通道读 16 点位值,``write_int``
    按 16 点/字写位软元件,均静默错值。
    """
    from omniplc.core.constants import MX_BIT_DEVICES

    for device in (
        "FX",
        "FY",
        "MF",
        "SS",
        "SC",
        "LTS",
        "LTC",
        "LCS",
        "LCC",
        "LSS",
        "LSC",
    ):
        assert device in MX_BIT_DEVICES, (
            f"MX_BIT_DEVICES 缺 {device!r}(review-1024 §9.2-3 P2)"
        )


def test_check_com_call_accepts_s_false() -> None:
    """review-1024 §9.2-2 P2:HRESULT = S_FALSE(0x00000001)不再误判失败。

    手册 §7.3 印刷 529:「S_FALSE 正常结束——函数处理正常结束,
    但操作(至 PLC 的访问)失败」,真实码经 ``lplRetCode`` 传。
    旧判据 ``hresult != 0`` 把 S_FALSE 误抛 COM 失败 + 拆健康会话。
    """
    mx_module._check_com_call(0x00000001, 0, "SFalseTest")  # S_FALSE
    mx_module._check_com_call(0, 0, "SOkTest")  # S_OK


def test_check_com_call_rejects_failed_hresult() -> None:
    """HRESULT 失败位(E_FAIL=0x80004005 等)→ 内部异常。

    HRESULT 是 32 位有符号整数,失败位在最高位;Python int 无 32 位
    上限,需先 ``& 0xFFFFFFFF`` 转无符号再按有符号比较。
    """
    with pytest.raises(OmniPLCInternalError):
        mx_module._check_com_call(0x80004005, 0, "EFailTest")  # E_FAIL
    with pytest.raises(OmniPLCInternalError):
        mx_module._check_com_call(0x80004002, 0, "ENoInterfaceTest")


def test_create_monitor_rejected_for_sta_conflict() -> None:
    """review-1024 §9.2-4 P2:MX 不支持 create_monitor(STA 假设冲突)。

    监视器自建线程驱动 ``self._client._execute``,但 MX 仅在 connect
    线程 :func:`_com_initialize` 一次——监视器线程既不 ``CoInitialize``
    也不封送,真机行为未核证。明示拒绝优于静默风险。
    """
    client = _client()
    with pytest.raises(NotImplementedError):
        client.create_monitor({})


def test_get_error_message_works_without_connect(
    fake: FakeActUtlType,
) -> None:
    """review-1024 §9.2-5 P2:get_error_message 不依赖 ActUtlType 链路
    (用独立 ActSupportMsg 控件 + 构造期固定 logical_station_number)——
    链路未连时仍可查码,现场"连不上"场景可立即拿到出错文本。
    """
    client = _client()  # 不 connect
    ok, text = client.get_error_message(0xC0500100)
    assert ok is True
    assert text == "出错信息文本 0xC0500100"


def test_bit_device_rejects_non_bool(fake: FakeActUtlType) -> None:
    """位软元件非 BOOL:GetDevice(位)与块读(16 点/字)口径不一,入参期拒绝。"""
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client.read_ushort("M10")
    with pytest.raises(ValueError):
        client.write_int("M10", 5)
    with pytest.raises(ValueError):
        client.read_batch([("M10", "int")])


def test_word_device_bit_suffix_rejects_non_bool(fake: FakeActUtlType) -> None:
    """字软元件位号后缀仅 BOOL 可用;非 BOOL 拒绝(与 MC/Modbus 一致)。"""
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client.read_short("D100.3")
    with pytest.raises(ValueError):
        client.write_int("D100.3", 5)


def test_write_batch_bit_device_value_must_be_0_or_1(fake: FakeActUtlType) -> None:
    """write_batch 对位软元件的整数值必须是 0/1(手册位写取值)。"""
    client = _client()
    client.connect()
    with pytest.raises(ValueError):
        client.write_batch([("M20", 2)])
    assert client.write_batch([("M20", 1)]) is True


def test_connect_comerror_releases_com_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open 抛 COMError 时必须配对释放 CoInitialize(第八轮 P2-16)。

    Open 在 try 外时 COMError 直接逃逸且 ``_com=None`` 使 close 短路,
    惰性重连每轮累积一个 CoInitialize 引用计数。
    """
    released: list = []

    class FakeCom:
        @staticmethod
        def Open() -> int:
            raise ValueError("模拟 COMError(经 _com_error 假冒)")

    monkeypatch.setattr(mx_module, "_com_error", lambda: ValueError)
    monkeypatch.setattr(mx_module, "_com_initialize", lambda: None)
    monkeypatch.setattr(mx_module, "_com_uninitialize", lambda: released.append(1))
    monkeypatch.setattr(mx_module, "_new_com_object", lambda station: FakeCom())
    client = MelsecMxClient(0)
    # 基类 connect 吞建连异常记 last_error(契约:连接失败不抛),断言 False + 释放
    assert client.connect() is False
    assert released, "COM 初始化计数未配对释放"


def test_connect_disconnect_cycles_balance_com_init(
    fake: FakeActUtlType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect/disconnect 多次循环 COM init 计数配平(盲点清缴)。

    每轮成功 connect 恰一次 ``_com_initialize``、disconnect 恰一次
    ``_com_uninitialize``——循环 N 轮后两侧计数相等(防重连场景泄漏
    CoInitialize 引用计数)。
    """
    inits: list = []
    releases: list = []
    monkeypatch.setattr(mx_module, "_com_initialize", lambda: inits.append(1))
    monkeypatch.setattr(mx_module, "_com_uninitialize", lambda: releases.append(1))
    client = MelsecMxClient(0)
    for _ in range(3):
        assert client.connect() is True
        assert client.disconnect() is True
    assert fake.calls.count(("Open",)) == 3
    assert fake.calls.count(("Close",)) == 3
    assert inits == releases == [1, 1, 1]
