"""海康机器人 ID 系列智能读码器客户端(MvCodeReaderSDK ctypes 封装)。

依据:MvCodeReaderSDK V2.0.0(随 IDMVS Development 开发包分发,
``Modules/MvCodeReaderSDK/``)+《MvCodeReader SDK (C or C++) Developer
Guide》V1.5.3(2024/01/10,``Doc/``)。指南印刷页码引用(下称「指南」):

- 调用流程(指南 §2 印刷页 9-11):EnumDevices → CreateHandle →
  OpenDevice →(参数读写)→ StartGrabbing → GetOneFrameTimeoutEx2 →
  StopGrabbing → CloseDevice → DestroyHandle
- ``MV_CODEREADER_GetOneFrameTimeoutEx2``(指南 §3.3.3 印刷页 27-28):
  帧信息含条码 + 面单 + **条码质量**;ID5000 系列支持质量评分,不支持
  评分的设备质量字段为 0;取回的pData指针指向 SDK 内部缓存,无需释放
- ``MV_CODEREADER_SetCommandValue``(指南 §3.5.12 印刷页 44):软触发经
  ``TriggerSoftware`` 命令(GenICam 命令节点)
- ``MV_CODEREADER_EnumIDDevices``(指南 §3.1.4 印刷页 13):私有协议枚举
  ID 系列设备;结构体定义见指南 §4.1(印刷页 51-79)与头文件
  ``MvCodeReaderParams.h``(就近注释行号)、错误码 ``MvCodeReaderErrorDefine.h``

**无官方 Python 绑定**(SDK 提供 C/C++/C#/Java),本模块以 ctypes 按头文件
直接绑定;动态库须现场安装(Runtime 安装包或指明 SDK 目录),核心零依赖。
结构体为 MSVC 自然对齐,与头文件逐字段对应。**结构体布局以 V2.0.0 头文件
为权威依据**(本地归档 ``docs/protocol/hikrobot/MvCodeReaderParams.h``,
与其他厂商资料同口径不入库)——指南 V1.5.3 的 IMAGE_OUT_INFO_EX2 布局
落后于 V2.0.0 头文件(头文件新增 UnparsedAgvInfo union 并把 nReserved
25→23,见审查 1001 P1-2),差异以头文件为准;测试含 sizeof 解析对拍守卫。

触发前置(读码器侧):TriggerMode=On + TriggerSource=Software(可经
:meth:`set_enum_value` 设置或 IDMVS 配置),采集进行中(StartGrabbing)。
"""
from __future__ import annotations

import ctypes
import os
import platform
import struct as _struct
from typing import NamedTuple, Optional, Tuple

from ..core.base_client import BaseClient
from ..core.errors import (
    DeviceError,
    ErrorCategory,
    OmniPLCInternalError,
    TransportClosedError,
    TransportTimeoutError,
)
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue
from ..transport import BaseTransport

__all__ = [
    "HikrobotIdSdkClient",
    "HikrobotSdkCode",
    "HikrobotSdkFrame",
    "HikrobotSdkQuality",
]

# --------------------------------------------------------------------------
# 头文件常量(MvCodeReaderParams.h / MvCodeReaderErrorDefine.h)
# --------------------------------------------------------------------------
_MV_CODEREADER_OK = 0x00000000
"""成功(MvCodeReaderErrorDefine.h 行 6)。"""
_MV_CODEREADER_E_NODATA = 0x80020006
"""无数据:帧等待超时未取到图(MvCodeReaderErrorDefine.h 行 15)。"""
_MV_CODEREADER_GIGE_DEVICE = 0x00000001
"""传输层类型:GigE 设备(MvCodeReaderParams.h 行 51 区)。"""
_MV_CODEREADER_USB_DEVICE = 0x00000004
"""传输层类型:USB3.0 设备。"""
_MV_CODEREADER_MAX_DEVICE_NUM = 256
"""枚举最大设备数(MvCodeReaderParams.h 行 45)。"""
_MV_CODEREADER_MAX_BCR_CODE_LEN_EX = 4096
"""扩展条码字符最大长度(MvCodeReaderParams.h 行 61)。"""
_MAX_CODEREADER_BCR_COUNT_EX = 300
"""一次最多输出条码个数(扩展,MvCodeReaderParams.h 行 68)。"""
_TRIGGER_MODE_OFF = 0
"""TriggerMode 枚举:触发关闭(MvCodeReaderParams.h 行 845)。"""
_TRIGGER_MODE_ON = 1
"""TriggerMode 枚举:触发打开(MvCodeReaderParams.h 行 846)。"""
_TRIGGER_SOURCE_SOFTWARE = 7
"""TriggerSource 枚举:软触发(MvCodeReaderParams.h 行 859)。"""

# 条码类型 → 名称(MV_CODEREADER_CODE_TYPE 枚举,MvCodeReaderParams.h 行 868-906;
# 中文名以头文件注释为准,控制台回显乱码不改其值)
_SDK_CODE_TYPE_NAMES = {
    0: "NONE",
    1: "DataMatrix",
    2: "QR",
    140: "MicroQR",
    8: "EAN8",
    9: "UPCE",
    12: "UPCA",
    13: "EAN13",
    14: "ISBN13",
    20: "Codabar",
    25: "ITF25",
    26: "Matrix25",
    27: "ITF14",
    30: "MSI",
    31: "Code11",
    32: "Industrial25",
    33: "ChinaPost",
    36: "Pharmacode",
    37: "PharmacodeTwoTrack",
    39: "Code39",
    93: "Code93",
    128: "Code128",
    131: "PDF417",
    132: "AZTEC",
    133: "ECC140",
    134: "MicroPDF417",
    145: "HANXIN",
    150: "MAXICODE",
    1000: "NoRead1D",
    1001: "NoRead2D",
    1002: "NoReadSD",
}
"""有码无读标记类型(1000/1001/1002)代表对应区无读出,判定为未读到码。"""
_NOREAD_CODE_TYPES = frozenset({1000, 1001, 1002})


def _frame_is_noread(frame: "HikrobotSdkFrame") -> bool:
    """整帧无有效条码判定(内部方法):无码,或全部为 NoRead 标记类型。

    读码器开启 NoRead 输出时,未读到码的 ROI 会输出 NoRead 标记条码
    (类型 1000/1001/1002,指南附录 C.1)——全帧皆标记 = 没读到码,
    不得当作成功读码返回。
    """
    return (not frame.codes) or all(
        code.code_type in _NOREAD_CODE_TYPES for code in frame.codes
    )


# --------------------------------------------------------------------------
# ctypes 结构体(逐字段对应 MvCodeReaderParams.h,MSVC 自然对齐)
# --------------------------------------------------------------------------
class _MV_CODEREADER_POINT_I(ctypes.Structure):
    """条码角点坐标(MvCodeReaderParams.h 行 437-441)。"""
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
    ]


class _MV_CODEREADER_CODE_INFO(ctypes.Structure):
    """条码质量(MvCodeReaderParams.h 行 513-561):等级 0~4,越高越好。"""
    _fields_ = [
        ("nOverQuality", ctypes.c_int),
        ("nDeCode", ctypes.c_int),
        ("nSCGrade", ctypes.c_int),
        ("nModGrade", ctypes.c_int),
        ("nFPDGrade", ctypes.c_int),
        ("nANGrade", ctypes.c_int),
        ("nGNGrade", ctypes.c_int),
        ("nUECGrade", ctypes.c_int),
        ("nPGHGrade", ctypes.c_int),
        ("nPGVGrade", ctypes.c_int),
        ("fSCScore", ctypes.c_float),
        ("fModScore", ctypes.c_float),
        ("fFPDScore", ctypes.c_float),
        ("fAnScore", ctypes.c_float),
        ("fGNScore", ctypes.c_float),
        ("fUECScore", ctypes.c_float),
        ("fPGHScore", ctypes.c_float),
        ("fPGVScore", ctypes.c_float),
        ("nRMGrade", ctypes.c_int),
        ("fRMScore", ctypes.c_float),
        ("n1DEdgeGrade", ctypes.c_int),
        ("n1DMinRGrade", ctypes.c_int),
        ("n1DMinEGrade", ctypes.c_int),
        ("n1DDcdGrade", ctypes.c_int),
        ("n1DDefGrade", ctypes.c_int),
        ("n1DQZGrade", ctypes.c_int),
        ("f1DEdgeScore", ctypes.c_float),
        ("f1DMinRScore", ctypes.c_float),
        ("f1DMinEScore", ctypes.c_float),
        ("f1DDcdScore", ctypes.c_float),
        ("f1DDefScore", ctypes.c_float),
        ("f1DQZScore", ctypes.c_float),
        ("nReserved", ctypes.c_int * 18),
    ]


class _MV_CODEREADER_BCR_INFO_EX2(ctypes.Structure):
    """单条码信息(扩展字符 + 质量,MvCodeReaderParams.h 行 606-635)。"""
    _fields_ = [
        ("nID", ctypes.c_uint),
        ("chCode", ctypes.c_char * _MV_CODEREADER_MAX_BCR_CODE_LEN_EX),
        ("nLen", ctypes.c_uint),
        ("nBarType", ctypes.c_uint),
        ("pt", _MV_CODEREADER_POINT_I * 4),
        ("stCodeQuality", _MV_CODEREADER_CODE_INFO),
        ("nAngle", ctypes.c_int),
        ("nMainPackageId", ctypes.c_uint),
        ("nSubPackageId", ctypes.c_uint),
        ("sAppearCount", ctypes.c_ushort),
        ("sPPM", ctypes.c_ushort),
        ("sAlgoCost", ctypes.c_ushort),
        ("sSharpness", ctypes.c_ushort),
        ("bIsGetQuality", ctypes.c_bool),
        ("nIDRScore", ctypes.c_uint),
        ("n1DIsGetQuality", ctypes.c_uint),
        ("nTotalProcCost", ctypes.c_uint),
        ("nTriggerTimeTvHigh", ctypes.c_uint),
        ("nTriggerTimeTvLow", ctypes.c_uint),
        ("nTriggerTimeUtvHigh", ctypes.c_uint),
        ("nTriggerTimeUtvLow", ctypes.c_uint),
        ("sPollingIndex", ctypes.c_ushort),
        ("sRoiIndex", ctypes.c_ushort),
        ("nLightSourceBitMap", ctypes.c_uint),
        ("nReserved", ctypes.c_int * 57),
    ]


class _MV_CODEREADER_RESULT_BCR_EX2(ctypes.Structure):
    """条码结果列表(仅作为指针目标类型,由 SDK 内部存储填充,
    MvCodeReaderParams.h 行 638-645)。"""
    _fields_ = [
        ("nCodeNum", ctypes.c_uint),
        ("stBcrInfoEx2", _MV_CODEREADER_BCR_INFO_EX2 * _MAX_CODEREADER_BCR_COUNT_EX),
        ("nNoReadNum", ctypes.c_ushort),
        ("nRes", ctypes.c_ushort),
        ("nReserved", ctypes.c_uint * 7),
    ]


class _MV_CODEREADER_GIGE_DEVICE_INFO(ctypes.Structure):
    """GigE 设备信息(MvCodeReaderParams.h 行 126-149)。"""
    _fields_ = [
        ("nIpCfgOption", ctypes.c_uint),
        ("nIpCfgCurrent", ctypes.c_uint),
        ("nCurrentIp", ctypes.c_uint),
        ("nCurrentSubNetMask", ctypes.c_uint),
        ("nDefultGateWay", ctypes.c_uint),
        ("chManufacturerName", ctypes.c_ubyte * 32),
        ("chModelName", ctypes.c_ubyte * 32),
        ("chDeviceVersion", ctypes.c_ubyte * 32),
        ("chManufacturerSpecificInfo", ctypes.c_ubyte * 48),
        ("chSerialNumber", ctypes.c_ubyte * 16),
        ("chUserDefinedName", ctypes.c_ubyte * 16),
        ("nNetExport", ctypes.c_uint),
        ("nCurUserIP", ctypes.c_uint),
        ("nAreaLogo", ctypes.c_uint),
        ("chSafeMajorVer", ctypes.c_ubyte),
        ("chSafeMinorVer", ctypes.c_ubyte),
        ("chActive", ctypes.c_ubyte),
        ("chLock", ctypes.c_ubyte),
        ("nLockTime", ctypes.c_ushort),
        ("chSafeAction", ctypes.c_ubyte),
        ("chReserved", ctypes.c_ubyte),
    ]


class _MV_CODEREADER_USB3_DEVICE_INFO(ctypes.Structure):
    """U3V 设备信息(MvCodeReaderParams.h 行 152-171)。"""
    _fields_ = [
        ("CrtlInEndPoint", ctypes.c_ubyte),
        ("CrtlOutEndPoint", ctypes.c_ubyte),
        ("StreamEndPoint", ctypes.c_ubyte),
        ("EventEndPoint", ctypes.c_ubyte),
        ("idVendor", ctypes.c_ushort),
        ("idProduct", ctypes.c_ushort),
        ("nDeviceNumber", ctypes.c_uint),
        ("chDeviceGUID", ctypes.c_ubyte * 64),
        ("chVendorName", ctypes.c_ubyte * 64),
        ("chModelName", ctypes.c_ubyte * 64),
        ("chFamilyName", ctypes.c_ubyte * 64),
        ("chDeviceVersion", ctypes.c_ubyte * 64),
        ("chManufacturerName", ctypes.c_ubyte * 64),
        ("chSerialNumber", ctypes.c_ubyte * 64),
        ("chUserDefinedName", ctypes.c_ubyte * 64),
        ("nbcdUSB", ctypes.c_uint),
        ("nReserved", ctypes.c_uint * 3),
    ]


class _SpecialInfoUnion(ctypes.Union):
    _fields_ = [
        ("stGigEInfo", _MV_CODEREADER_GIGE_DEVICE_INFO),
        ("stUsb3VInfo", _MV_CODEREADER_USB3_DEVICE_INFO),
    ]


class _MV_CODEREADER_DEVICE_INFO(ctypes.Structure):
    """设备信息(MvCodeReaderParams.h 行 174-194)。"""
    _fields_ = [
        ("nMajorVer", ctypes.c_ushort),
        ("nMinorVer", ctypes.c_ushort),
        ("nMacAddrHigh", ctypes.c_uint),
        ("nMacAddrLow", ctypes.c_uint),
        ("nTLayerType", ctypes.c_uint),
        ("nDeviceType", ctypes.c_uint),
        ("bSelectDevice", ctypes.c_bool),
        ("nWebHostIp", ctypes.c_uint),
        ("nReserved", ctypes.c_uint * 1),
        ("SpecialInfo", _SpecialInfoUnion),
    ]


class _MV_CODEREADER_DEVICE_INFO_LIST(ctypes.Structure):
    """设备信息列表(MvCodeReaderParams.h 行 197-202):SDK 填充内部存储指针。"""
    _fields_ = [
        ("nDeviceNum", ctypes.c_uint),
        ("pDeviceInfo", ctypes.POINTER(_MV_CODEREADER_DEVICE_INFO) * _MV_CODEREADER_MAX_DEVICE_NUM),
    ]


class _UnparsedBcrListUnion(ctypes.Union):
    _fields_ = [
        ("pstCodeListEx2", ctypes.POINTER(_MV_CODEREADER_RESULT_BCR_EX2)),
        ("nAligning", ctypes.c_int64),
    ]


class _UnparsedOcrListUnion(ctypes.Union):
    """OCR 信息指针/对齐占位(MvCodeReaderParams.h 行 804-808;本库不解引用)。"""

    _fields_ = [
        ("pstOcrList", ctypes.c_void_p),
        ("nAligning", ctypes.c_int64),
    ]


class _UnparsedAgvInfoUnion(ctypes.Union):
    """AGV 读码头指针/对齐占位(MvCodeReaderParams.h 行 814-818;本库不解引用)。"""

    _fields_ = [
        ("pstAgvInfo", ctypes.c_void_p),
        ("nAligning", ctypes.c_int64),
    ]


class _MV_CODEREADER_IMAGE_OUT_INFO_EX2(ctypes.Structure):
    """帧输出信息(MvCodeReaderParams.h 行 751-823):图像 + 条码(扩展/质量)。

    逐字段对应 V2.0.0 头文件原文:UnparsedBcrList 之后依次为
    UnparsedOcrList(8B)→ nWholeFlag/nRes → UnparsedAgvInfo(8B)→
    nReserved[23]——两个 union 占位缺一不可(SDK 按其编译期尺寸整体写
    调用方缓冲,漏一个即 8 字节越界写,合计 16B;外层 pstCodeListEx 头文件
    类型为 ``RESULT_BCR_EX*`` 非 EX2,本库不解引用故按 void* 占位)。
    """
    _fields_ = [
        ("nWidth", ctypes.c_ushort),
        ("nHeight", ctypes.c_ushort),
        ("enPixelType", ctypes.c_uint),
        ("nTriggerIndex", ctypes.c_uint),
        ("nFrameNum", ctypes.c_uint),
        ("nFrameLen", ctypes.c_uint),
        ("nTimeStampHigh", ctypes.c_uint),
        ("nTimeStampLow", ctypes.c_uint),
        ("bFlaseTrigger", ctypes.c_uint),
        ("nFocusScore", ctypes.c_uint),
        ("bIsGetCode", ctypes.c_bool),
        ("pstCodeListEx", ctypes.c_void_p),
        ("pstWaybillList", ctypes.c_void_p),
        ("nEventID", ctypes.c_uint),
        ("nChannelID", ctypes.c_uint),
        ("nImageCost", ctypes.c_uint),
        ("UnparsedBcrList", _UnparsedBcrListUnion),
        ("UnparsedOcrList", _UnparsedOcrListUnion),
        ("nWholeFlag", ctypes.c_ushort),
        ("nRes", ctypes.c_ushort),
        ("UnparsedAgvInfo", _UnparsedAgvInfoUnion),
        ("nReserved", ctypes.c_uint * 23),
    ]


class _MV_CODEREADER_INTVALUE_EX(ctypes.Structure):
    """整型参数值(MvCodeReaderParams.h 行 401-408)。"""
    _fields_ = [
        ("nCurValue", ctypes.c_int64),
        ("nMax", ctypes.c_int64),
        ("nMin", ctypes.c_int64),
        ("nInc", ctypes.c_int64),
        ("nReserved", ctypes.c_uint * 16),
    ]


class _MV_CODEREADER_ENUMVALUE(ctypes.Structure):
    """枚举参数值(MvCodeReaderParams.h 行 382-388)。"""
    _fields_ = [
        ("nCurValue", ctypes.c_uint),
        ("nSupportedNum", ctypes.c_uint),
        ("nSupportValue", ctypes.c_uint * 64),
        ("nReserved", ctypes.c_uint * 4),
    ]


class _MV_CODEREADER_FLOATVALUE(ctypes.Structure):
    """浮点参数值(MvCodeReaderParams.h 行 411-417)。"""
    _fields_ = [
        ("fCurValue", ctypes.c_float),
        ("fMax", ctypes.c_float),
        ("fMin", ctypes.c_float),
        ("nReserved", ctypes.c_uint * 4),
    ]


class _MV_CODEREADER_STRINGVALUE(ctypes.Structure):
    """字符串参数值(MvCodeReaderParams.h 行 420-425)。"""
    _fields_ = [
        ("chCurValue", ctypes.c_char * 256),
        ("nMaxLength", ctypes.c_int64),
        ("nReserved", ctypes.c_uint * 2),
    ]


# --------------------------------------------------------------------------
# 结果对象(Python 侧结构化元数据)
# --------------------------------------------------------------------------
class HikrobotSdkQuality(NamedTuple):
    """条码质量(等级 0~4 越高越好;ID5000 系列支持,其余设备全 0)。"""

    over_quality: int
    """总体质量评分(1D/2D 共用)。"""
    decode: int
    """译码评分。"""
    contrast_grade: int
    """Symbol Contrast 对比度评分(1D/2D 共用,指南 §4.1.5 印刷页 55-56)。"""
    modulation_grade: int
    """模块均匀性评分(1D/2D 共用,指南 §4.1.5 印刷页 55-56)。"""
    fpd_grade: int
    """fixed_pattern_damage 评分(2D)。"""
    axial_grade: int
    """axial_nonuniformity 码轴规整性评分(2D)。"""
    grid_grade: int
    """grid_nonuniformity 评分(2D)。"""
    uec_grade: int
    """unused_error_correction 未使用纠错评分(2D)。"""
    print_growth_h_grade: int
    """打印伸缩(水平)评分(2D)。"""
    print_growth_v_grade: int
    """打印伸缩(垂直)评分(2D)。"""
    contrast_score: float
    reflectance_margin_grade: int
    reflectance_margin_score: float
    edge_grade: int
    """边缘确定度评分(1D)。"""
    min_reflectance_grade: int
    """最小反射率评分(1D)。"""
    min_edge_contrast_grade: int
    """最小边缘对比度评分(1D)。"""
    decodability_grade: int
    """可译码性评分(1D)。"""
    defects_grade: int
    """缺陷评分(1D)。"""
    quiet_zone_grade: int
    """静区评分(1D)。"""
    edge_score: float
    min_reflectance_score: float
    min_edge_contrast_score: float
    decodability_score: float
    defects_score: float
    quiet_zone_score: float


class HikrobotSdkCode(NamedTuple):
    """单条码解码结果(MV_CODEREADER_BCR_INFO_EX2 解析)。"""

    content: str
    """条码内容(按 :attr:`encoding` 解码,截断于首个 \\x00)。"""
    code_type: int
    """条码类型枚举原始值(MV_CODEREADER_CODE_TYPE)。"""
    code_type_name: str
    """条码类型名称(见模块 ``_SDK_CODE_TYPE_NAMES``;1000~1002 为有码无读标记)。"""
    length: int
    """条码字符长度。"""
    points: Tuple[Tuple[int, int], ...]
    """条码四角点坐标(图像像素系)。"""
    angle_deg: float
    """条码角度(度;原始值 10 倍 0~3600,1D 顺时针/2D 逆时针)。"""
    quality: HikrobotSdkQuality
    id: int
    """条码 ID。"""
    appear_count: int
    """条码被识别的次数。"""
    ppm: float
    """PPM(原始值 10 倍)。"""
    algo_cost_ms: int
    """算法耗时(毫秒)。"""
    sharpness: float
    """图像清晰度(原始值 10 倍)。"""
    idr_score: int
    """读码评分。"""
    total_proc_cost_ms: int
    """从触发开始到输出的时间统计(毫秒)。"""
    trigger_time_seconds: int
    """触发开始时间(秒,64 位由高/低 32 位拼合;现场以读码器时间为准)。"""
    trigger_time_microseconds: int
    """触发开始时间微秒分量(64 位拼合)。"""
    polling_index: int
    """库编号。"""
    roi_index: int
    """ROI 编号。"""


class HikrobotSdkFrame(NamedTuple):
    """一帧取回结果(MV_CODEREADER_IMAGE_OUT_INFO_EX2 解析)。"""

    codes: Tuple[HikrobotSdkCode, ...]
    width: int
    height: int
    pixel_type: int
    frame_num: int
    trigger_index: int
    frame_len: int
    timestamp_high: int
    timestamp_low: int
    false_trigger: int
    """是否误触发。"""
    focus_score: int
    """聚焦得分。"""
    image_cost_ms: int
    """帧图像在相机内部的处理耗时(毫秒)。"""
    image: Optional[bytes]
    """图像原始数据(:attr:`with_image` 为 ``True`` 时才有,否则 ``None``)。"""


# --------------------------------------------------------------------------
# 动态库加载
# --------------------------------------------------------------------------
def _resolve_dll(sdk_dir: Optional[str], dll_path: Optional[str]) -> str:
    """按解释器位数解析动态库路径(内部方法)。

    ``dll_path`` 显式指定优先;否则 ``sdk_dir`` 下找 ``win32|win64``
    子目录,再退 ``sdk_dir`` 本身(指到 SDK 根的两种层级都可用)。
    """
    if dll_path:
        return dll_path
    if not sdk_dir:
        raise ValueError(_("必须提供 sdk_dir 或 dll_path 之一"))
    if platform.system() == "Windows":
        bit_dir = "win64" if _struct.calcsize("P") == 8 else "win32"
        name = "MvCodeReaderCtrl.dll"
    else:
        bit_dir = "linux64" if _struct.calcsize("P") == 8 else "linux32"
        name = "libMvCodeReaderCtrl.so"
    for candidate in (
        os.path.join(sdk_dir, bit_dir, name),
        os.path.join(sdk_dir, name),
    ):
        if os.path.isfile(candidate):
            return candidate
    raise OmniPLCInternalError(
        _("SDK 动态库未找到:{}(请确认 sdk_dir 指向含 {} 的目录)").format(
            name, bit_dir
        )
    )


def _load_sdk(sdk_dir: Optional[str], dll_path: Optional[str]) -> ctypes.CDLL:
    """加载 MvCodeReaderCtrl 动态库(模块级工厂,测试替换点)。

    依赖库(GenApi 等)与主库同目录,加载前把该目录前置到 PATH
    (Windows);Linux 下追加 LD_LIBRARY_PATH 需在进程启动前设置,此处
    仅尝试直接加载。
    """
    path = _resolve_dll(sdk_dir, dll_path)
    directory = os.path.dirname(os.path.abspath(path))
    if platform.system() == "Windows":
        env_path = os.environ.get("PATH", "")
        if directory.lower() not in env_path.lower():
            os.environ["PATH"] = directory + os.pathsep + env_path
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(directory)
        return ctypes.WinDLL(path)
    return ctypes.CDLL(path)


class _SdkFunctions:
    """按头文件签名的 ctypes 函数表(内部类,win32/win64 均 __stdcall)。"""

    def __init__(self, dll: ctypes.CDLL) -> None:
        dll.MV_CODEREADER_GetSDKVersion.restype = ctypes.c_uint
        dll.MV_CODEREADER_GetSDKVersion.argtypes = []
        dll.MV_CODEREADER_EnumDevices.restype = ctypes.c_int
        dll.MV_CODEREADER_EnumDevices.argtypes = [
            ctypes.POINTER(_MV_CODEREADER_DEVICE_INFO_LIST),
            ctypes.c_uint,
        ]
        dll.MV_CODEREADER_EnumIDDevices.restype = ctypes.c_int
        dll.MV_CODEREADER_EnumIDDevices.argtypes = [
            ctypes.POINTER(_MV_CODEREADER_DEVICE_INFO_LIST)
        ]
        dll.MV_CODEREADER_CreateHandle.restype = ctypes.c_int
        dll.MV_CODEREADER_CreateHandle.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_MV_CODEREADER_DEVICE_INFO),
        ]
        dll.MV_CODEREADER_DestroyHandle.restype = ctypes.c_int
        dll.MV_CODEREADER_DestroyHandle.argtypes = [ctypes.c_void_p]
        dll.MV_CODEREADER_OpenDevice.restype = ctypes.c_int
        dll.MV_CODEREADER_OpenDevice.argtypes = [ctypes.c_void_p]
        dll.MV_CODEREADER_CloseDevice.restype = ctypes.c_int
        dll.MV_CODEREADER_CloseDevice.argtypes = [ctypes.c_void_p]
        dll.MV_CODEREADER_StartGrabbing.restype = ctypes.c_int
        dll.MV_CODEREADER_StartGrabbing.argtypes = [ctypes.c_void_p]
        dll.MV_CODEREADER_StopGrabbing.restype = ctypes.c_int
        dll.MV_CODEREADER_StopGrabbing.argtypes = [ctypes.c_void_p]
        dll.MV_CODEREADER_GetOneFrameTimeoutEx2.restype = ctypes.c_int
        dll.MV_CODEREADER_GetOneFrameTimeoutEx2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(_MV_CODEREADER_IMAGE_OUT_INFO_EX2),
            ctypes.c_uint,
        ]
        dll.MV_CODEREADER_GetIntValue.restype = ctypes.c_int
        dll.MV_CODEREADER_GetIntValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(_MV_CODEREADER_INTVALUE_EX),
        ]
        dll.MV_CODEREADER_SetIntValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetIntValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int64,
        ]
        dll.MV_CODEREADER_GetEnumValue.restype = ctypes.c_int
        dll.MV_CODEREADER_GetEnumValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(_MV_CODEREADER_ENUMVALUE),
        ]
        dll.MV_CODEREADER_SetEnumValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetEnumValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        dll.MV_CODEREADER_GetBoolValue.restype = ctypes.c_int
        dll.MV_CODEREADER_GetBoolValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_bool),
        ]
        dll.MV_CODEREADER_SetBoolValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetBoolValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_bool,
        ]
        dll.MV_CODEREADER_GetFloatValue.restype = ctypes.c_int
        dll.MV_CODEREADER_GetFloatValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(_MV_CODEREADER_FLOATVALUE),
        ]
        dll.MV_CODEREADER_SetFloatValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetFloatValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_float,
        ]
        dll.MV_CODEREADER_GetStringValue.restype = ctypes.c_int
        dll.MV_CODEREADER_GetStringValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(_MV_CODEREADER_STRINGVALUE),
        ]
        dll.MV_CODEREADER_SetStringValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetStringValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        dll.MV_CODEREADER_SetCommandValue.restype = ctypes.c_int
        dll.MV_CODEREADER_SetCommandValue.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.GetSDKVersion = dll.MV_CODEREADER_GetSDKVersion
        self.EnumDevices = dll.MV_CODEREADER_EnumDevices
        self.EnumIDDevices = dll.MV_CODEREADER_EnumIDDevices
        self.CreateHandle = dll.MV_CODEREADER_CreateHandle
        self.DestroyHandle = dll.MV_CODEREADER_DestroyHandle
        self.OpenDevice = dll.MV_CODEREADER_OpenDevice
        self.CloseDevice = dll.MV_CODEREADER_CloseDevice
        self.StartGrabbing = dll.MV_CODEREADER_StartGrabbing
        self.StopGrabbing = dll.MV_CODEREADER_StopGrabbing
        self.GetOneFrameTimeoutEx2 = dll.MV_CODEREADER_GetOneFrameTimeoutEx2
        self.GetIntValue = dll.MV_CODEREADER_GetIntValue
        self.SetIntValue = dll.MV_CODEREADER_SetIntValue
        self.GetEnumValue = dll.MV_CODEREADER_GetEnumValue
        self.SetEnumValue = dll.MV_CODEREADER_SetEnumValue
        self.GetBoolValue = dll.MV_CODEREADER_GetBoolValue
        self.SetBoolValue = dll.MV_CODEREADER_SetBoolValue
        self.GetFloatValue = dll.MV_CODEREADER_GetFloatValue
        self.SetFloatValue = dll.MV_CODEREADER_SetFloatValue
        self.GetStringValue = dll.MV_CODEREADER_GetStringValue
        self.SetStringValue = dll.MV_CODEREADER_SetStringValue
        self.SetCommandValue = dll.MV_CODEREADER_SetCommandValue


_SDK_LINK_ERROR_RANGES = (
    (0x80020200, 0x800202FF),
    (0x80020300, 0x800203FF),
    (0x80020500, 0x800205FF),
)
"""链路类错误码段(指南附录 D 印刷页 108-119):命中即断链,按 OSError
抛出触发基类惰性重连——GigE 状态 / USB 状态 / 网络组件三类。"""


def _check_rc(rc: int, action: str) -> None:
    """SDK 返回码校验(内部方法):非 OK 按错误类别分流抛出。

    - **链路类**(GigE 状态 0x800202xx / USB 状态 0x800203xx / 网络组件
      0x800205xx,指南附录 D 印刷页 108-119 明确为连接状态错误)→
      :class:`OSError`——触发基类惰性重连,网线拔掉/掉电后不再永驻
      "已连接"态;
    - 其余(语义类/参数类/超时)→ :class:`DeviceError` 不断线。

    ``code`` 统一按无符号呈现(restype ``c_int`` 下 0x80020006 呈负数,
    与 0x 文本不一致)。
    """
    code = rc & 0xFFFFFFFF
    if code == _MV_CODEREADER_OK:
        return
    if any(low <= code <= high for low, high in _SDK_LINK_ERROR_RANGES):
        raise OSError(_("SDK 链路类错误({}):0x{:08X}").format(action, code))
    raise DeviceError(
        _("SDK 调用失败({}):0x{:08X}").format(action, code), code
    )


def _ip_to_uint(ip: str) -> int:
    """点分 IPv4 → 大端 uint(设备 nCurrentIp 字序,头文件示例按字节从高到低拆)。"""
    parts = ip.split(".")
    if len(parts) != 4:
        raise ValueError(_("非法 IPv4 地址:{!r}").format(ip))
    value = 0
    for part in parts:
        try:
            octet = int(part)
        except ValueError as exc:
            raise ValueError(_("非法 IPv4 地址:{!r}").format(ip)) from exc
        if not 0 <= octet <= 255:
            raise ValueError(_("非法 IPv4 地址:{!r}").format(ip))
        value = (value << 8) | octet
    return value


class _HikrobotSdkSession(BaseTransport):
    """SDK 会话(加载动态库 + 枚举匹配 + 句柄生命周期,内部传输适配)。

    SDK 是会话型通道(无字节流 send/recv),同 MX/ADS 适配形态。
    """

    def __init__(
        self,
        ip_address: str,
        sdk_dir: Optional[str],
        dll_path: Optional[str],
        with_image: bool = False,
    ) -> None:
        super().__init__()
        self._ip_address = ip_address
        self._sdk_dir = sdk_dir
        self._dll_path = dll_path
        self._with_image = bool(with_image)
        self._functions: Optional[_SdkFunctions] = None
        self._handle: Optional[int] = None
        self._grabbing = False

    def connect(self) -> None:
        """加载动态库并按 IP 匹配设备:枚举(私有协议 + GigE/USB)→ 建句柄
        → 开设备 → 起流(指南 §2 流程印刷页 10-11)。

        :raises DeviceError: 枚举未命中目标 IP
        :raises OmniPLCInternalError: 动态库加载失败
        """
        self.close()
        try:
            dll = _load_sdk(self._sdk_dir, self._dll_path)
        except OSError as exc:
            raise OmniPLCInternalError(
                _("SDK 动态库加载失败:{}(请检查位数与依赖库是否齐全)").format(exc)
            ) from exc
        self._functions = _SdkFunctions(dll)
        target = _ip_to_uint(self._ip_address)
        dev_info = self._find_device_by_ip(target)
        handle = ctypes.c_void_p()
        _check_rc(
            self._functions.CreateHandle(ctypes.pointer(handle), ctypes.pointer(dev_info)),
            _("创建句柄"),
        )
        self._handle = handle.value
        try:
            _check_rc(self._functions.OpenDevice(self._handle), _("打开设备"))
            _check_rc(self._functions.StartGrabbing(self._handle), _("开始取流"))
        except DeviceError:
            self.close()
            raise
        self._grabbing = True

    def _find_device_by_ip(self, target: int) -> _MV_CODEREADER_DEVICE_INFO:
        """在私有协议与 GigE/USB 枚举结果中按 IP 匹配设备(内部方法)。"""
        functions = self._functions
        assert functions is not None  # connect 先加载函数表,再进入枚举
        for layer in (None, _MV_CODEREADER_GIGE_DEVICE | _MV_CODEREADER_USB_DEVICE):
            device_list = _MV_CODEREADER_DEVICE_INFO_LIST()
            if layer is None:
                rc = functions.EnumIDDevices(ctypes.pointer(device_list))
            else:
                rc = functions.EnumDevices(ctypes.pointer(device_list), layer)
            if rc != _MV_CODEREADER_OK:
                continue
            for index in range(device_list.nDeviceNum):
                pointer = device_list.pDeviceInfo[index]
                if not pointer:
                    # 官方样例显式判 NULL(ctypes 对 NULL 指针 .contents 会抛
                    # ValueError 被 connect 吞成误导文案),跳过空槽位
                    continue
                info = pointer.contents
                if (
                    info.nTLayerType == _MV_CODEREADER_GIGE_DEVICE
                    and info.SpecialInfo.stGigEInfo.nCurrentIp == target
                ):
                    return info
        raise DeviceError(
            _("枚举未找到目标设备 {}(私有协议与 GigE/USB 枚举均未命中,请核对 IP 与网口)").format(
                self._ip_address
            ),
            0,
        )

    def close(self) -> None:
        """停流 → 关设备 → 销毁句柄;重复调用幂等(动态库句柄为空即跳过)。"""
        functions = self._functions
        handle = self._handle
        if functions is not None and handle is not None:
            if self._grabbing:
                try:
                    functions.StopGrabbing(handle)
                except DeviceError:
                    pass
                self._grabbing = False
            try:
                functions.CloseDevice(handle)
            except DeviceError:
                pass
            try:
                functions.DestroyHandle(handle)
            except DeviceError:
                pass
        self._handle = None
        self._functions = None

    def send(self, data: bytes) -> None:
        """SDK 会话无字节流(内部方法):不接受基类发送原语。"""
        raise TransportClosedError(_("SDK 会话不支持字节流发送"))

    def recv(self, size: int) -> bytes:
        """SDK 会话无字节流(内部方法):不接受基类接收原语。"""
        raise TransportClosedError(_("SDK 会话不支持字节流接收"))

    # ------------------------------------------------------------------
    # SDK 调用面(事务锁内由客户端调用)
    # ------------------------------------------------------------------

    @property
    def with_image(self) -> bool:
        """是否随帧复制图像数据(客户端构造参数透传)。"""
        return self._with_image

    def _require_functions(self) -> _SdkFunctions:
        """取函数表(内部方法):未连接时拒绝。"""
        if self._functions is None or self._handle is None:
            raise TransportClosedError(_("SDK 会话未连接,请先调用 connect()"))
        return self._functions

    def trigger_software(self) -> int:
        """执行软触发命令 TriggerSoftware(内部方法;返回 SDK 原始码)。

        前置:TriggerMode=On(MvCodeReaderParams.h 行 843-847),否则设备按
        GenICam 语义返回错误码。
        """
        return self._require_functions().SetCommandValue(
            self._handle, b"TriggerSoftware"
        )

    def get_one_frame_ex2(
        self, data_ptr: object, info: object, timeout_ms: int
    ) -> int:
        """取一帧(Ex2,含条码质量;内部方法;返回 SDK 原始码)。

        超时无帧返回 ``MV_CODEREADER_E_NODATA``(指南 §3.3.3 印刷页 27-28)。
        """
        return self._require_functions().GetOneFrameTimeoutEx2(
            self._handle, data_ptr, info, timeout_ms
        )

    def set_enum_value(self, key: str, value: int) -> None:
        """设置枚举参数(内部方法;指南 §3.5.4 印刷页 39)。"""
        _check_rc(
            self._require_functions().SetEnumValue(
                self._handle, key.encode("ascii"), value
            ),
            _("设置枚举参数 {}").format(key),
        )

    def set_command_value(self, key: str) -> None:
        """执行命令型参数(内部方法;指南 §3.5.12 印刷页 44)。"""
        _check_rc(
            self._require_functions().SetCommandValue(
                self._handle, key.encode("ascii")
            ),
            _("执行命令 {}").format(key),
        )

    def get_int_value(self, key: str) -> Optional[int]:
        """读取整型参数(内部方法;指南 §3.5.1 印刷页 37)。"""
        value = _MV_CODEREADER_INTVALUE_EX()
        _check_rc(
            self._require_functions().GetIntValue(
                self._handle, key.encode("ascii"), ctypes.pointer(value)
            ),
            _("读取整型参数 {}").format(key),
        )
        return int(value.nCurValue)

    def set_int_value(self, key: str, value: int) -> None:
        """设置整型参数(内部方法;指南 §3.5.2 印刷页 38)。"""
        _check_rc(
            self._require_functions().SetIntValue(
                self._handle, key.encode("ascii"), value
            ),
            _("设置整型参数 {}").format(key),
        )

    def get_bool_value(self, key: str) -> Optional[bool]:
        """读取布尔参数(内部方法)。"""
        value = ctypes.c_bool(False)
        _check_rc(
            self._require_functions().GetBoolValue(
                self._handle, key.encode("ascii"), ctypes.pointer(value)
            ),
            _("读取布尔参数 {}").format(key),
        )
        return bool(value.value)

    def set_bool_value(self, key: str, value: bool) -> None:
        """设置布尔参数(内部方法)。"""
        _check_rc(
            self._require_functions().SetBoolValue(
                self._handle, key.encode("ascii"), value
            ),
            _("设置布尔参数 {}").format(key),
        )

    def get_float_value(self, key: str) -> Optional[float]:
        """读取浮点参数(内部方法)。"""
        value = _MV_CODEREADER_FLOATVALUE()
        _check_rc(
            self._require_functions().GetFloatValue(
                self._handle, key.encode("ascii"), ctypes.pointer(value)
            ),
            _("读取浮点参数 {}").format(key),
        )
        return float(value.fCurValue)

    def set_float_value(self, key: str, value: float) -> None:
        """设置浮点参数(内部方法)。"""
        _check_rc(
            self._require_functions().SetFloatValue(
                self._handle, key.encode("ascii"), value
            ),
            _("设置浮点参数 {}").format(key),
        )

    def get_string_value(self, key: str) -> Optional[str]:
        """读取字符串参数(内部方法)。"""
        value = _MV_CODEREADER_STRINGVALUE()
        _check_rc(
            self._require_functions().GetStringValue(
                self._handle, key.encode("ascii"), ctypes.pointer(value)
            ),
            _("读取字符串参数 {}").format(key),
        )
        return bytes(value.chCurValue).split(b"\x00", 1)[0].decode("utf-8", "replace")

    def set_string_value(self, key: str, value: str) -> None:
        """设置字符串参数(内部方法)。"""
        _check_rc(
            self._require_functions().SetStringValue(
                self._handle, key.encode("ascii"), value.encode("utf-8")
            ),
            _("设置字符串参数 {}").format(key),
        )


class HikrobotIdSdkClient(BaseClient):
    """海康机器人 ID 系列智能读码器客户端(MvCodeReaderSDK ctypes 封装)。

    :example::

        client = HikrobotIdSdkClient("192.168.1.100", sdk_dir=r"D:\\MvCodeReaderSDK\\SDK")
        client.connect()                       # 枚举匹配 IP → 建句柄 → 开设备 → 起流
        ok, frame = client.scan(timeout=5.0)   # 软触发 + 取一帧(含全量元数据)
        if ok:
            for code in frame.codes:
                print(code.content, code.code_type_name, code.angle_deg,
                      code.quality.over_quality, code.points)
        ok = client.set_enum_value("TriggerSource", 7)   # TriggerSource = Software
        client.close()

    依赖:MvCodeReaderSDK 运行库(动态库随 IDMVS Development 开发包分发);
    ``sdk_dir`` 指向含 ``win32|win64/MvCodeReaderCtrl.dll`` 的目录,或
    ``dll_path`` 显式指定动态库路径。按解释器位数自动选择子目录。
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        *,
        sdk_dir: Optional[str] = None,
        dll_path: Optional[str] = None,
        with_image: bool = False,
        encoding: str = "utf-8",
        encoding_errors: str = "strict",
    ) -> None:
        """初始化 SDK 客户端。

        :param ip_address: 读码器 IP(经枚举匹配;私有协议 + GigE/USB 双路枚举)
        :param sdk_dir: MvCodeReaderSDK 动态库目录(含 ``win32|win64`` 子目录
            或直接含 ``MvCodeReaderCtrl.dll``)
        :param dll_path: 动态库显式路径(优先于 ``sdk_dir``)
        :param with_image: :meth:`scan`/:meth:`read_frame` 是否随帧返回图像
            原始数据(默认 ``False``;开启后每帧复制 ``nFrameLen`` 字节)
        :param encoding: 条码内容解码编码,默认 utf-8(QR/DataMatrix 可携带
            GB2312/任意二进制内容,按现场码制选择)
        :param encoding_errors: 解码失败策略,默认 ``strict``
        :raises ValueError: 参数非法
        """
        if not ip_address or not ip_address.strip():
            raise ValueError(_("ip_address 不能为空"))
        super().__init__()
        self._ip_address = ip_address
        if sdk_dir is None and dll_path is None:
            raise ValueError(_("必须提供 sdk_dir 或 dll_path 之一"))
        self._sdk_dir = sdk_dir
        self._dll_path = dll_path
        self._with_image = bool(with_image)
        try:
            "".encode(encoding)
        except LookupError as exc:
            raise ValueError(_("encoding 非法:{!r}").format(encoding)) from exc
        self._encoding = encoding
        self._encoding_errors = encoding_errors

    # ------------------------------------------------------------------
    # 读码 API
    # ------------------------------------------------------------------

    def scan(self, timeout: float = 10.0) -> Tuple[bool, Optional[HikrobotSdkFrame]]:
        """软触发一次读码并取回一帧结果(TriggerSoftware → Ex2 取帧)。

        触发是**动作型**操作,按写语义走事务模板(重试用 :attr:`write_retries`,
        默认 0,避免重复触发)。前置:TriggerMode=On 且 TriggerSource=Software
        (:meth:`set_enum_value` 或读码器侧配置),采集进行中。

        :param timeout: 等待帧的总超时(秒)
        :return: ``(是否读到条码, 帧)``——帧含全部条码及其质量/坐标/角度等
            元数据;未读到码(:attr:`HikrobotSdkFrame.codes` 为空或全部为
            NoRead 标记类型)/取帧超时/命令出错返回 ``(False, None)``,原因
            记入 :attr:`last_error`
        :raises ValueError: 参数非法

        注意 SDK 按先进先出返回帧:上一触发超时后**迟到的旧帧**会在下一轮
        取回(单触发语义);如需严格配对,调用方比对
        :attr:`HikrobotSdkFrame.trigger_index` / ``frame_num``。
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        ok, frame = self._execute(
            lambda: self._scan_once(float(timeout)), is_write=True
        )
        if not ok or frame is None:
            return False, None
        if _frame_is_noread(frame):
            with self._lock:
                self._set_error(
                    _("读码器无读出(NoRead 标记帧)"), ErrorCategory.DEVICE, None
                )
            return False, None
        return True, frame

    def read_frame(
        self, timeout: float = 10.0
    ) -> Tuple[bool, Optional[HikrobotSdkFrame]]:
        """被动取一帧(不触发;连续模式/外部触发场景)。

        :param timeout: 等待帧的总超时(秒)
        :return: 语义同 :meth:`scan`
        :raises ValueError: 参数非法
        """
        if timeout <= 0:
            raise ValueError(_("timeout 必须大于 0,收到:{}").format(timeout))
        ok, frame = self._execute(
            lambda: self._get_frame(float(timeout)), is_write=False
        )
        if not ok or frame is None:
            return False, None
        if _frame_is_noread(frame):
            with self._lock:
                self._set_error(
                    _("读码器无读出(NoRead 标记帧)"), ErrorCategory.DEVICE, None
                )
            return False, None
        return True, frame

    def close(self) -> None:
        """断开会话(:meth:`disconnect` 别名,与 TCP/串口读码器客户端命名一致)。"""
        self.disconnect()

    # ------------------------------------------------------------------
    # 参数访问(GenICam 节点名以 IDMVS 属性树为准,指南 §3.5 印刷页 37-45)
    # ------------------------------------------------------------------

    def set_enum_value(self, key: str, value: int) -> bool:
        """设置枚举参数(如 ``TriggerMode``=1 / ``TriggerSource``=7 软触发)。"""
        ok, _unused = self._execute(
            lambda: self._session().set_enum_value(key, int(value)), is_write=True
        )
        return ok

    def set_command_value(self, key: str) -> bool:
        """执行命令型参数(如 ``TriggerSoftware`` 软触发,指南 §3.5.12 印刷页 44)。"""
        ok, _unused = self._execute(
            lambda: self._session().set_command_value(key), is_write=True
        )
        return ok

    def get_int_value(self, key: str) -> Tuple[bool, Optional[int]]:
        """读取整型参数(指南 §3.5.1 印刷页 37)。"""
        ok, value = self._execute(lambda: self._session().get_int_value(key))
        if not ok or value is None:
            return False, None
        return True, int(value)

    def set_int_value(self, key: str, value: int) -> bool:
        """设置整型参数(指南 §3.5.2 印刷页 38)。"""
        ok, _unused = self._execute(
            lambda: self._session().set_int_value(key, int(value)), is_write=True
        )
        return ok

    def get_bool_value(self, key: str) -> Tuple[bool, Optional[bool]]:
        """读取布尔参数。"""
        ok, value = self._execute(lambda: self._session().get_bool_value(key))
        if not ok or value is None:
            return False, None
        return True, bool(value)

    def set_bool_value(self, key: str, value: bool) -> bool:
        """设置布尔参数。"""
        ok, _unused = self._execute(
            lambda: self._session().set_bool_value(key, bool(value)), is_write=True
        )
        return ok

    def get_float_value(self, key: str) -> Tuple[bool, Optional[float]]:
        """读取浮点参数。"""
        ok, value = self._execute(lambda: self._session().get_float_value(key))
        if not ok or value is None:
            return False, None
        return True, float(value)

    def set_float_value(self, key: str, value: float) -> bool:
        """设置浮点参数。"""
        ok, _unused = self._execute(
            lambda: self._session().set_float_value(key, float(value)), is_write=True
        )
        return ok

    def get_string_value(self, key: str) -> Tuple[bool, Optional[str]]:
        """读取字符串参数。"""
        return self._execute(lambda: self._session().get_string_value(key))

    def set_string_value(self, key: str, value: str) -> bool:
        """设置字符串参数。"""
        ok, _unused = self._execute(
            lambda: self._session().set_string_value(key, str(value)), is_write=True
        )
        return ok

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _session(self) -> _HikrobotSdkSession:
        """取 SDK 会话(内部方法)。"""
        session = self._require_transport()
        if not isinstance(session, _HikrobotSdkSession):
            raise OmniPLCInternalError(
                _("传输对象不是 SDK 会话:{!r}").format(type(session).__name__)
            )
        return session

    def _scan_once(self, timeout: float) -> HikrobotSdkFrame:
        """软触发 + 取一帧(内部方法,须事务内调用)。"""
        _check_rc(
            self._session().trigger_software(), _("软触发")
        )
        return self._get_frame(timeout)

    def _get_frame(self, timeout: float) -> HikrobotSdkFrame:
        """取一帧并解析(内部方法,须事务内调用)。

        ``GetOneFrameTimeoutEx2`` 超时返回 ``MV_CODEREADER_E_NODATA``
        (指南 §3.3.3):链路正常未出帧,按 ``(False, None)`` 处理不断线。
        """
        session = self._session()
        info = _MV_CODEREADER_IMAGE_OUT_INFO_EX2()
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        data_ptr = ctypes.c_void_p()
        rc = session.get_one_frame_ex2(
            ctypes.pointer(data_ptr), ctypes.pointer(info), int(timeout * 1000)
        )
        if rc == _MV_CODEREADER_E_NODATA or (rc & 0xFFFFFFFF) == _MV_CODEREADER_E_NODATA:
            # 超时无帧:与 TCP/串口超时同口径——TransportTimeoutError(code=0)
            # 不计 device_error_count、不断线;比较同时覆盖 c_int restype 的
            # 负数形态(真 DLL)与测试桩的正数形态
            raise TransportTimeoutError(
                _("取帧超时({}s),读码器未输出帧(NODATA)").format(timeout), 0
            )
        _check_rc(rc, _("取帧"))
        image: Optional[bytes] = None
        if session.with_image and info.nFrameLen > 0 and data_ptr.value:
            image = ctypes.string_at(data_ptr, info.nFrameLen)
        return _parse_frame(info, image, self._encoding, self._encoding_errors)

    def _create_transport(self) -> BaseTransport:
        return _HikrobotSdkSession(
            self._ip_address, self._sdk_dir, self._dll_path, self._with_image
        )

    # ------------------------------------------------------------------
    # 读码器不支持 PLC 数据读写(基类抽象原语必须实现)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读码器为图像设备,不支持 PLC 点位读取(内部方法)。

        参数访问请用 :meth:`get_int_value` 等入口,解码结果走 :meth:`scan`。
        """
        raise DeviceError(_("读码器不支持 PLC 点位读取,请使用 scan()"), 0)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """读码器为图像设备,不支持 PLC 点位写入(内部方法),语义同 :meth:`_read`。"""
        raise DeviceError(_("读码器不支持 PLC 点位写入"), 0)


def _parse_frame(
    info: _MV_CODEREADER_IMAGE_OUT_INFO_EX2,
    image: Optional[bytes],
    encoding: str,
    encoding_errors: str,
) -> HikrobotSdkFrame:
    """SDK 帧信息 → 结构化结果(内部方法)。

    条码列表取 ``UnparsedBcrList.pstCodeListEx2``(指南样例 C.1 印刷页 91:
    建议以扩展字符条码信息解析条码)。
    """
    codes: list = []
    bcr_list = info.UnparsedBcrList.pstCodeListEx2
    if bcr_list:
        result = bcr_list.contents
        for index in range(result.nCodeNum):
            item = result.stBcrInfoEx2[index]
            raw = bytes(item.chCode)
            content = raw.split(b"\x00", 1)[0].decode(encoding, errors=encoding_errors)
            quality = HikrobotSdkQuality(
                over_quality=item.stCodeQuality.nOverQuality,
                decode=item.stCodeQuality.nDeCode,
                contrast_grade=item.stCodeQuality.nSCGrade,
                modulation_grade=item.stCodeQuality.nModGrade,
                fpd_grade=item.stCodeQuality.nFPDGrade,
                axial_grade=item.stCodeQuality.nANGrade,
                grid_grade=item.stCodeQuality.nGNGrade,
                uec_grade=item.stCodeQuality.nUECGrade,
                print_growth_h_grade=item.stCodeQuality.nPGHGrade,
                print_growth_v_grade=item.stCodeQuality.nPGVGrade,
                contrast_score=item.stCodeQuality.fSCScore,
                reflectance_margin_grade=item.stCodeQuality.nRMGrade,
                reflectance_margin_score=item.stCodeQuality.fRMScore,
                edge_grade=item.stCodeQuality.n1DEdgeGrade,
                min_reflectance_grade=item.stCodeQuality.n1DMinRGrade,
                min_edge_contrast_grade=item.stCodeQuality.n1DMinEGrade,
                decodability_grade=item.stCodeQuality.n1DDcdGrade,
                defects_grade=item.stCodeQuality.n1DDefGrade,
                quiet_zone_grade=item.stCodeQuality.n1DQZGrade,
                edge_score=item.stCodeQuality.f1DEdgeScore,
                min_reflectance_score=item.stCodeQuality.f1DMinRScore,
                min_edge_contrast_score=item.stCodeQuality.f1DMinEScore,
                decodability_score=item.stCodeQuality.f1DDcdScore,
                defects_score=item.stCodeQuality.f1DDefScore,
                quiet_zone_score=item.stCodeQuality.f1DQZScore,
            )
            codes.append(
                HikrobotSdkCode(
                    content=content,
                    code_type=item.nBarType,
                    code_type_name=_SDK_CODE_TYPE_NAMES.get(
                        item.nBarType, "0x{:X}".format(item.nBarType)
                    ),
                    length=item.nLen,
                    points=tuple(
                        (item.pt[i].x, item.pt[i].y) for i in range(4)
                    ),
                    angle_deg=item.nAngle / 10.0,
                    quality=quality,
                    id=item.nID,
                    appear_count=item.sAppearCount,
                    ppm=item.sPPM / 10.0,
                    algo_cost_ms=item.sAlgoCost,
                    sharpness=item.sSharpness / 10.0,
                    idr_score=item.nIDRScore,
                    total_proc_cost_ms=item.nTotalProcCost,
                    trigger_time_seconds=(item.nTriggerTimeTvHigh << 32)
                    | item.nTriggerTimeTvLow,
                    trigger_time_microseconds=(item.nTriggerTimeUtvHigh << 32)
                    | item.nTriggerTimeUtvLow,
                    polling_index=item.sPollingIndex,
                    roi_index=item.sRoiIndex,
                )
            )
    return HikrobotSdkFrame(
        codes=tuple(codes),
        width=info.nWidth,
        height=info.nHeight,
        pixel_type=info.enPixelType,
        frame_num=info.nFrameNum,
        trigger_index=info.nTriggerIndex,
        frame_len=info.nFrameLen,
        timestamp_high=info.nTimeStampHigh,
        timestamp_low=info.nTimeStampLow,
        false_trigger=info.bFlaseTrigger,
        focus_score=info.nFocusScore,
        image_cost_ms=info.nImageCost,
        image=image,
    )
