"""海康机器人 ID 系列读码器 SDK 客户端测试:假函数表 + 真 ctypes 结构体。

替换 :func:`omniplc.scanner.hikrobot_sdk._load_sdk`(模块工厂,同
MTConnect ``_new_connection`` 惯例),函数表按《MvCodeReader SDK (C or
C++) Developer Guide》V1.5.3 的流程语义在 Python 侧模拟,结构体使用
模块内**真实 ctypes 定义**填充——解析路径端到端验证:

- connect:私有协议 + GigE 枚举按 IP 匹配 → CreateHandle/OpenDevice/
  StartGrabbing 调用序列(指南 §2 印刷页 10-11)
- scan:TriggerSoftware → GetOneFrameTimeoutEx2 → EX2 结构体解析
  (内容/码制/四点坐标/角度 10 倍/质量评分/时间戳拼合)
- 未读到码(NODATA/无条码)→ (False, None) 不断线
- 参数访问面:Get/Set Int/Enum/Bool/Float/String + SetCommandValue
"""
from __future__ import annotations

import ctypes
from typing import List, Optional

import pytest

from omniplc import HikrobotIdSdkClient
from omniplc.scanner import hikrobot_sdk
from omniplc.scanner.hikrobot_sdk import (
    _MV_CODEREADER_DEVICE_INFO,
    _MV_CODEREADER_E_NODATA,
    _MV_CODEREADER_GIGE_DEVICE,
    _MV_CODEREADER_IMAGE_OUT_INFO_EX2,
    _MV_CODEREADER_RESULT_BCR_EX2,
    _ip_to_uint,
)

_IP = "192.168.1.100"


class _FakeFn:
    """可挂 restype/argtypes 的假函数包装(替代 ctypes 函数指针)。"""

    def __init__(self, fn) -> None:
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeSdk:
    """假 SDK:Python 侧模拟指南流程语义,操作真实 ctypes 结构体。"""

    def __init__(self) -> None:
        self.devices: List[_MV_CODEREADER_DEVICE_INFO] = []
        self.enum_id_rc = 0
        self.enum_gige_rc = 0
        self.create_rc = 0
        self.open_rc = 0
        self.start_rc = 0
        self.frame_rc = 0
        self.frame: Optional[_MV_CODEREADER_IMAGE_OUT_INFO_EX2] = None
        self.bcr_list: Optional[_MV_CODEREADER_RESULT_BCR_EX2] = None
        self.image_data = b"IMGDATA"
        self.param_rc = 0
        self.calls: List[str] = []
        self.enum_values = {}
        self.int_values = {}
        self.bool_values = {}
        self.float_values = {}
        self.string_values = {}
        self._held = []  # 句柄/设备指针保活

    def add_gige_device(self, ip: str) -> None:
        info = _MV_CODEREADER_DEVICE_INFO()
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        info.nTLayerType = _MV_CODEREADER_GIGE_DEVICE
        info.SpecialInfo.stGigEInfo.nCurrentIp = _ip_to_uint(ip)
        self.devices.append(info)

    def _fill_list(self, plist) -> None:
        # 指针实例解引用出结构体本体
        device_list = plist[0] if hasattr(plist, "__getitem__") else plist
        for index, dev in enumerate(self.devices):
            device_list.pDeviceInfo[index] = ctypes.pointer(dev)
        device_list.nDeviceNum = len(self.devices)

    # 函数表 ------------------------------------------------------------
    def MV_CODEREADER_GetSDKVersion(self):
        return _FakeFn(lambda: 0x02000000)

    def MV_CODEREADER_EnumDevices(self):
        def fn(device_list, layer):
            self.calls.append("EnumDevices")
            if self.enum_gige_rc:
                return self.enum_gige_rc
            self._fill_list(device_list)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_EnumIDDevices(self):
        def fn(device_list):
            self.calls.append("EnumIDDevices")
            if self.enum_id_rc:
                return self.enum_id_rc
            self._fill_list(device_list)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_CreateHandle(self):
        def fn(handle, dev_info):
            self.calls.append("CreateHandle")
            if self.create_rc:
                return self.create_rc
            handle[0] = 0xDEAD
            self._held.append(dev_info)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_DestroyHandle(self):
        def fn(handle):
            self.calls.append("DestroyHandle")
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_OpenDevice(self):
        def fn(handle):
            self.calls.append("OpenDevice")
            return self.open_rc

        return _FakeFn(fn)

    def MV_CODEREADER_CloseDevice(self):
        def fn(handle):
            self.calls.append("CloseDevice")
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_StartGrabbing(self):
        def fn(handle):
            self.calls.append("StartGrabbing")
            return self.start_rc

        return _FakeFn(fn)

    def MV_CODEREADER_StopGrabbing(self):
        def fn(handle):
            self.calls.append("StopGrabbing")
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_GetOneFrameTimeoutEx2(self):
        def fn(handle, data_ptr, info, timeout_ms):
            self.calls.append("GetOneFrameTimeoutEx2")
            if self.frame_rc:
                return self.frame_rc
            if self.frame is not None:
                ctypes.memmove(info, ctypes.byref(self.frame), ctypes.sizeof(self.frame))
                # 图像长度与缓冲一致,防 string_at 越界(真机由 SDK 保证)
                info[0].nFrameLen = len(self.image_data)
            if self.image_data:
                buf = ctypes.create_string_buffer(self.image_data)
                _HELD.append(buf)
                data_ptr[0] = ctypes.cast(buf, ctypes.c_void_p).value
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_GetIntValue(self):
        def fn(handle, key, value):
            self.calls.append("GetInt:{}".format(key.decode()))
            if self.param_rc:
                return self.param_rc
            value[0].nCurValue = self.int_values.get(key.decode(), 0)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_SetIntValue(self):
        def fn(handle, key, value):
            self.calls.append("SetInt:{}={}".format(key.decode(), value))
            return self.param_rc

        return _FakeFn(fn)

    def MV_CODEREADER_GetEnumValue(self):
        def fn(handle, key, value):
            self.calls.append("GetEnum:{}".format(key.decode()))
            if self.param_rc:
                return self.param_rc
            value[0].nCurValue = self.enum_values.get(key.decode(), 0)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_SetEnumValue(self):
        def fn(handle, key, value):
            self.calls.append("SetEnum:{}={}".format(key.decode(), value))
            return self.param_rc

        return _FakeFn(fn)

    def MV_CODEREADER_GetBoolValue(self):
        def fn(handle, key, value):
            self.calls.append("GetBool:{}".format(key.decode()))
            if self.param_rc:
                return self.param_rc
            value[0] = self.bool_values.get(key.decode(), False)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_SetBoolValue(self):
        def fn(handle, key, value):
            self.calls.append("SetBool:{}={}".format(key.decode(), value))
            return self.param_rc

        return _FakeFn(fn)

    def MV_CODEREADER_GetFloatValue(self):
        def fn(handle, key, value):
            self.calls.append("GetFloat:{}".format(key.decode()))
            if self.param_rc:
                return self.param_rc
            value[0].fCurValue = self.float_values.get(key.decode(), 0.0)
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_SetFloatValue(self):
        def fn(handle, key, value):
            self.calls.append("SetFloat:{}={}".format(key.decode(), value))
            return self.param_rc

        return _FakeFn(fn)

    def MV_CODEREADER_GetStringValue(self):
        def fn(handle, key, value):
            self.calls.append("GetString:{}".format(key.decode()))
            if self.param_rc:
                return self.param_rc
            text = self.string_values.get(key.decode(), "")
            value[0].chCurValue = text.encode("utf-8")
            return 0

        return _FakeFn(fn)

    def MV_CODEREADER_SetStringValue(self):
        def fn(handle, key, value):
            self.calls.append(
                "SetString:{}={}".format(key.decode(), value.decode())
            )
            return self.param_rc

        return _FakeFn(fn)

    def MV_CODEREADER_SetCommandValue(self):
        def fn(handle, key):
            self.calls.append("Command:{}".format(key.decode()))
            return 0

        return _FakeFn(fn)


_HELD: List[object] = []  # 防 GC:指针目标(RESULT_BCR_EX2/图像缓冲)需与信息同生命周期


def _make_frame(
    *,
    is_get_code: bool = True,
    codes: Optional[List[dict]] = None,
) -> _MV_CODEREADER_IMAGE_OUT_INFO_EX2:
    """构造一帧 EX2 信息(真实 ctypes 结构体)。"""
    info = _MV_CODEREADER_IMAGE_OUT_INFO_EX2()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    info.nWidth = 1280
    info.nHeight = 960
    info.nFrameNum = 7
    info.nTriggerIndex = 3
    info.nFrameLen = 1234567
    info.bIsGetCode = is_get_code
    if codes:
        bcr = _MV_CODEREADER_RESULT_BCR_EX2()
        bcr.nCodeNum = len(codes)
        for index, spec in enumerate(codes):
            item = bcr.stBcrInfoEx2[index]
            item.chCode = spec["content"].encode("utf-8")
            item.nLen = len(spec["content"])
            item.nBarType = spec["bar_type"]
            for i in range(4):
                item.pt[i].x = spec["points"][i][0]
                item.pt[i].y = spec["points"][i][1]
            item.stCodeQuality.nOverQuality = spec.get("over_quality", 4)
            item.nAngle = int(spec["angle_deg"] * 10)
            item.sPPM = spec.get("ppm", 0)
            item.sAlgoCost = 33
            item.sSharpness = spec.get("sharpness", 0)
            item.nTriggerTimeTvHigh = 0
            item.nTriggerTimeTvLow = 1700000000
            item.nTriggerTimeUtvLow = 500000
        _HELD.append(bcr)
        info.UnparsedBcrList.pstCodeListEx2 = ctypes.pointer(bcr)
    return info


def _make_client(monkeypatch: pytest.MonkeyPatch, sdk: _FakeSdk, **kwargs):
    """注入假 SDK 并完成 connect。"""
    monkeypatch.setattr(
        hikrobot_sdk, "_load_sdk", lambda sdk_dir, dll_path: _FakeDll(sdk)
    )
    client = HikrobotIdSdkClient(_IP, sdk_dir="D:/fake", **kwargs)  # type: ignore[arg-type]
    assert client.connect() is True
    return client, sdk


class _FakeDll:
    def __init__(self, sdk: _FakeSdk) -> None:
        self._sdk = sdk
        for name in (
            "GetSDKVersion",
            "EnumDevices",
            "EnumIDDevices",
            "CreateHandle",
            "DestroyHandle",
            "OpenDevice",
            "CloseDevice",
            "StartGrabbing",
            "StopGrabbing",
            "GetOneFrameTimeoutEx2",
            "GetIntValue",
            "SetIntValue",
            "GetEnumValue",
            "SetEnumValue",
            "GetBoolValue",
            "SetBoolValue",
            "GetFloatValue",
            "SetFloatValue",
            "GetStringValue",
            "SetStringValue",
            "SetCommandValue",
        ):
            setattr(self, "MV_CODEREADER_" + name, getattr(sdk, "MV_CODEREADER_" + name)())



def test_connect_and_scan_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect 枚举匹配 + scan 全流程:调用序列与元数据解析逐字段断言。"""
    sdk = _FakeSdk()
    sdk.add_gige_device("192.168.1.1")
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(
        codes=[
            {
                "content": "HELLO-1",
                "bar_type": 1,
                "points": [(10, 20), (110, 20), (110, 60), (10, 60)],
                "angle_deg": 123.4,
                "over_quality": 4,
                "ppm": 30,
                "sharpness": 125,
            },
            {"content": "X<\\x01>Y", "bar_type": 128, "points": [(1, 2), (3, 4), (5, 6), (7, 8)], "angle_deg": 0},
        ]
    )
    client, sdk = _make_client(monkeypatch, sdk)
    # 流程序列(指南 §2 印刷页 10-11)
    assert "EnumIDDevices" in sdk.calls
    assert "CreateHandle" in sdk.calls
    assert "OpenDevice" in sdk.calls
    assert "StartGrabbing" in sdk.calls
    # scan:软触发 + 取帧
    ok, frame = client.scan(timeout=2.0)
    assert ok is True
    assert "Command:TriggerSoftware" in sdk.calls
    assert "GetOneFrameTimeoutEx2" in sdk.calls
    assert frame is not None
    assert frame.width == 1280 and frame.height == 960
    assert frame.frame_num == 7 and frame.trigger_index == 3
    assert len(frame.codes) == 2
    first = frame.codes[0]
    assert first.content == "HELLO-1"
    assert first.code_type == 1 and first.code_type_name == "DataMatrix"
    assert first.points == ((10, 20), (110, 20), (110, 60), (10, 60))
    assert first.angle_deg == 123.4
    assert first.quality.over_quality == 4
    assert first.ppm == 3.0 and first.sharpness == 12.5
    assert first.algo_cost_ms == 33
    assert first.trigger_time_seconds == 1700000000
    assert first.trigger_time_microseconds == 500000
    second = frame.codes[1]
    assert second.code_type == 128 and second.code_type_name == "Code128"


def test_connect_ip_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """枚举未命中目标 IP:connect False,last_error 可辨。"""
    sdk = _FakeSdk()
    sdk.add_gige_device("10.0.0.5")
    monkeypatch.setattr(
        hikrobot_sdk, "_load_sdk", lambda sdk_dir, dll_path: _FakeDll(sdk)
    )
    client = HikrobotIdSdkClient(_IP, sdk_dir="D:/fake")
    assert client.connect() is False
    assert client.last_error is not None and "枚举未找到" in client.last_error


def test_scan_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """帧无条码(bIsGetCode=True 但列表为空):(False, None) 记无读出。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(is_get_code=True, codes=None)
    client, _sdk = _make_client(monkeypatch, sdk)
    assert client.scan(timeout=2.0) == (False, None)
    assert client.last_error is not None and "无读出" in client.last_error


def test_scan_nodata_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """取帧超时(NODATA):(False, None),链路不断线。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame_rc = _MV_CODEREADER_E_NODATA
    client, _sdk = _make_client(monkeypatch, sdk)
    assert client.scan(timeout=0.2) == (False, None)
    assert client.last_error is not None and "NODATA" in client.last_error
    assert client.connected is True


def test_read_frame_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_frame():不发送 TriggerSoftware,直接取帧。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(
        codes=[{"content": "IO-TRIG", "bar_type": 2, "points": [(0, 0)] * 4, "angle_deg": 0}]
    )
    client, sdk = _make_client(monkeypatch, sdk)
    ok, frame = client.read_frame(timeout=2.0)
    assert ok is True
    assert frame is not None and frame.codes[0].content == "IO-TRIG"
    assert "Command:TriggerSoftware" not in sdk.calls


def test_with_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """with_image=True:随帧复制图像原始字节。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(
        codes=[{"content": "ABC", "bar_type": 1, "points": [(0, 0)] * 4, "angle_deg": 0}]
    )
    client, _sdk = _make_client(monkeypatch, sdk, with_image=True)
    ok, frame = client.scan(timeout=2.0)
    assert ok is True
    assert frame is not None and frame.image == b"IMGDATA"


def test_param_accessors(monkeypatch: pytest.MonkeyPatch) -> None:
    """参数访问面:Get/Set Int/Enum/Bool/Float/String 与命令。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.int_values = {"Width": 1280}
    sdk.enum_values = {"TriggerSource": 7}
    sdk.bool_values = {"ReverseX": True}
    sdk.float_values = {"ExposureTime": 2500.5}
    sdk.string_values = {"DeviceUserID": "ID3000-01"}
    client, sdk = _make_client(monkeypatch, sdk)
    assert client.get_int_value("Width") == (True, 1280)
    assert client.set_int_value("Width", 640) is True
    assert client.get_bool_value("ReverseX") == (True, True)
    assert client.set_bool_value("ReverseX", False) is True
    assert client.get_float_value("ExposureTime") == (True, 2500.5)
    assert client.set_float_value("ExposureTime", 1000.0) is True
    assert client.get_string_value("DeviceUserID") == (True, "ID3000-01")
    assert client.set_string_value("DeviceUserID", "NEW") is True
    assert client.set_enum_value("TriggerSource", 7) is True
    assert client.set_command_value("TriggerSoftware") is True
    assert "SetEnum:TriggerSource=7" in sdk.calls
    assert "Command:TriggerSoftware" in sdk.calls


def test_sdk_not_loaded_raises_value_error() -> None:
    """构造期校验:sdk_dir 与 dll_path 都不给 → ValueError。"""
    with pytest.raises(ValueError, match="sdk_dir"):
        HikrobotIdSdkClient(_IP)


def test_dll_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """动态库加载失败:connect False,last_error 带原因。"""
    _FakeSdk()

    def broken_load(sdk_dir, dll_path):
        raise OSError("不是有效的 Win32 应用程序")

    monkeypatch.setattr(hikrobot_sdk, "_load_sdk", broken_load)
    client = HikrobotIdSdkClient(_IP, sdk_dir="D:/fake")
    assert client.connect() is False
    assert client.last_error is not None and "加载失败" in client.last_error


def test_ip_to_uint() -> None:
    """IPv4 → 设备 nCurrentIp 字序(大端拼合)。"""
    assert _ip_to_uint("192.168.1.100") == (192 << 24) | (168 << 16) | (1 << 8) | 100
    with pytest.raises(ValueError, match="IPv4"):
        _ip_to_uint("1.2.3")


# ----------------------------------------------------------------------
# 审查 1001 修复:NoRead 判定 / 结构体布局守卫 / 断链分流 / close 别名
# ----------------------------------------------------------------------


def test_ex2_struct_size_matches_header() -> None:
    """EX2 结构体 sizeof 对拍守卫:与 V2.0.0 头文件布局逐字段一致。

    头文件(MvCodeReaderParams.h L751-823)在 UnparsedBcrList 之后还有
    UnparsedOcrList(8B)与 UnparsedAgvInfo(8B)两个 union——漏一个即
    8 字节越界写(SDK 按其编译期尺寸整体写调用方缓冲)。win64 期望
    200:2+2(宽高)+4×8(至 bIsGetCode 前)+1+pad3(bool 后指针对齐)
    +8×2(双指针)+4×3(事件/通道/耗时)+pad4(union 8 对齐)+8×3
    (三 union)+2+2(标志/保留)+pad4(union 对齐)+4×23(nReserved)
    +尾垫 4。win32 期望 192:指针 4B,三 union 仍 8B/8 对齐,同构求和
    后尾垫 4。32 位门禁环境只执行 win32 分支,win64 期望由 64 位解释器
    (CI 3.12 腿)核证。
    """
    import sys

    size = ctypes.sizeof(_MV_CODEREADER_IMAGE_OUT_INFO_EX2)
    expected = 192 if sys.maxsize < 2 ** 32 else 200
    assert size == expected, (
        "IMAGE_OUT_INFO_EX2 布局与 V2.0.0 头文件不符(实际 {} 期望 {}):"
        "请对照 MvCodeReaderParams.h L751-823 逐字段核对".format(size, expected)
    )
    # 两个占位 union 必须在位(缺任何一个都会使 sizeof 偏小 8)
    fields = [name for name, _type in _MV_CODEREADER_IMAGE_OUT_INFO_EX2._fields_]
    assert "UnparsedOcrList" in fields
    assert "UnparsedAgvInfo" in fields


def test_scan_noread_marker_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """全帧均为 NoRead 标记类型(1000/1001/1002):(False, None) 不当成功。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(
        codes=[
            {"content": "NoRead1D", "bar_type": 1000, "points": [(0, 0)] * 4, "angle_deg": 0},
            {"content": "NoRead2D", "bar_type": 1001, "points": [(0, 0)] * 4, "angle_deg": 0},
        ]
    )
    client, _sdk = _make_client(monkeypatch, sdk)
    assert client.scan(timeout=2.0) == (False, None)
    assert client.last_error is not None and "无读出" in client.last_error
    assert client.connected is True


def test_scan_mixed_noread_and_real_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混合帧(部分 ROI NoRead、部分真码):按读到码成功返回。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame = _make_frame(
        codes=[
            {"content": "NoRead2D", "bar_type": 1001, "points": [(0, 0)] * 4, "angle_deg": 0},
            {"content": "ABC123", "bar_type": 2, "points": [(0, 0)] * 4, "angle_deg": 0},
        ]
    )
    client, _sdk = _make_client(monkeypatch, sdk)
    ok, frame = client.scan(timeout=2.0)
    assert ok is True
    assert frame is not None and len(frame.codes) == 2


def test_link_error_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """链路类错误码(0x800202xx GigE 状态):按 OSError 拆连触发惰性重连。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    sdk.frame_rc = 0x80020212  # 0x800202xx 段(指南附录 D:连接状态错误)
    client, _sdk = _make_client(monkeypatch, sdk)
    assert client.scan(timeout=2.0) == (False, None)
    assert client.connected is False


def test_close_alias_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() 别名:docstring 示例调用形态可用,等同 disconnect。"""
    sdk = _FakeSdk()
    sdk.add_gige_device(_IP)
    client, _sdk = _make_client(monkeypatch, sdk)
    client.close()
    assert client.connected is False
