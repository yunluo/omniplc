"""FANUC FOCAS 客户端(fwlib32.dll ctypes 封装,数采只读)。

依据:`fwlib32.h`(16043 行,FOCAS「CNC/PMC Data Window Library」官方
头文件,归档 ``docs/protocol/fanuc/``,行号引用均指该文件)——连接
API 15117~15128 行(Ethernet 段)、EW_ 错误码 104~147 行、尺寸宏
44~80 行、结构体 2850~2942/457~481 行(逐字段行号见各定义)。交叉:
同仓库 ``examples/ctypes/main.py``(连接闭环,端口 8193/句柄
``c_ushort`` 口径一致)、cnblogs snail1502/p/18068966(C++ 闭环旁证)。
物料出处为社区打包仓库 社区打包参考实现快照(内部留存)(非 FANUC 官方渠道,与官方
开发包一致性待核对——已登记档案 §0)。

- 走线:TCP/IP 8193(机床内嵌以太网口,「系统 → 内嵌 → 公共」配置);
  HSSB 不实现。**Ethernet 通用 DLL 为系列宏全不定义构建**——结构体取
  `#else` 默认布局(ODBSYS addinfo 版、MAX_AXIS=32/MAX_SPINDLE=8),
  系列限定构建(HSSB/专用 DLL)不支持;结构体区 ``#pragma pack(4)``
  → ctypes 全部 ``_pack_ = 4``。
- 调用约定:Windows ``WINAPI`` = ``__stdcall``(头文件 32~42 行,
  非 Windows 分支清空 WINAPI)→ ``ctypes.WinDLL``;**仅支持 Windows**
  (Linux ``.so`` 留后续,加载期显式报不支持)。
- 范围(数采只读,与 MTConnect 同域):连接生命周期(``cnc_allclibhndl3``
  /``cnc_freelibhndl``/``cnc_settimeout``/``cnc_rdcncid``)+
  ``cnc_sysinfo``(系统信息)/``cnc_rddynamic2``(实时状态:报警号/
  程序号/序列号/进给/主轴/四组坐标)/``cnc_statinfo2``(CNC 状态:
  模式/运行/急停/报警位,同时是探活命令)。写族一律不做;
  ``cnc_rdprgnum``/``cnc_rdaxisdata``/PMC/参数族留后续版本。
- 结构体形态:ODBDY 的 alarm/prgnum/prgmnum 是 short 而 ODBDY2 全
  long(头文件 431~481 行两布局)——只绑 ODBDY2 防混用错位;ODBST
  有三种系列布局(2884~2925 行)——只绑无条件编译的 ODBST2(2928~
  2942 行)规避系列差异。
"""

from __future__ import annotations

import ctypes
import os
import platform
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from ..core.base_client import BaseClient, validate_endpoint
from ..core.debug import log_op
from ..core.errors import (
    DeviceError,
    OmniPLCInternalError,
    TransportClosedError,
)
from ..core.i18n import _
from ..core.types import DataType, PrimitiveValue
from ..transport import BaseTransport

__all__ = [
    "FanucFocasClient",
    "FocasCncId",
    "FocasDynamic",
    "FocasStatus",
    "FocasSysInfo",
]

# --------------------------------------------------------------------------
# 头文件常量(fwlib32.h;不入 core/constants,海康 SDK 同款口径)
# --------------------------------------------------------------------------

EW_OK: int = 0
EW_BUSY: int = -1
EW_RESET: int = -2
EW_MMCSYS: int = -3
EW_PARITY: int = -4
EW_SYSTEM: int = -5
EW_UNEXP: int = -6
EW_VERSION: int = -7
EW_HANDLE: int = -8
EW_HSSB: int = -9
EW_SYSTEM2: int = -10
EW_BUS: int = -11
EW_ITHIGHT: int = -12
EW_ITLOW: int = -13
EW_INIERR: int = -14
EW_NODLL: int = -15
EW_SOCKET: int = -16
EW_PROTOCOL: int = -17
EW_FUNC: int = 1
EW_LENGTH: int = 2
EW_NUMBER: int = 3
EW_ATTRIB: int = 4
EW_DATA: int = 5
EW_NOOPT: int = 6
EW_PROT: int = 7
EW_OVRFLOW: int = 8
EW_PARAM: int = 9
EW_BUFFER: int = 10
EW_PATH: int = 11
EW_MODE: int = 12
EW_REJECT: int = 13
EW_DTSRVR: int = 14
EW_ALARM: int = 15
EW_STOP: int = 16
EW_PASSWD: int = 17
EW_PMC: int = 18
EW_PMCHANDLE: int = 19
EW_RD_OVWSTP: int = 20
EW_RD_RSTFIN: int = 21
"""FOCAS 数据窗口函数返回码(fwlib32.h 104~147 行)。

负值 = 库/通信层故障,正值 = CNC/功能/数据层错误(宏表归纳;别名
EW_NOPMC=EW_FUNC、EW_RANGE=EW_NUMBER、EW_TYPE=EW_ATTRIB 不重复定义)。
"""

EW_TEXT: Dict[int, str] = {
    EW_OK: "成功",
    EW_BUSY: "忙(EW_BUSY)",
    EW_RESET: "复位或停止中(EW_RESET)",
    EW_MMCSYS: "MMC 系统安装错误(EW_MMCSYS)",
    EW_PARITY: "共享 RAM 奇偶错误(EW_PARITY)",
    EW_SYSTEM: "系统错误(EW_SYSTEM)",
    EW_UNEXP: "异常错误(EW_UNEXP)",
    EW_VERSION: "CNC/PMC 版本不匹配(EW_VERSION)",
    EW_HANDLE: "库句柄错误(EW_HANDLE)",
    EW_HSSB: "HSSB 通信错误(EW_HSSB)",
    EW_SYSTEM2: "系统错误(EW_SYSTEM2)",
    EW_BUS: "总线错误(EW_BUS)",
    EW_ITHIGHT: "智能终端高温告警(EW_ITHIGHT)",
    EW_ITLOW: "智能终端低温告警(EW_ITLOW)",
    EW_INIERR: "API 库初始化文件错误(EW_INIERR)",
    EW_NODLL: "DLL 不存在(EW_NODLL)",
    EW_SOCKET: "Socket 错误(EW_SOCKET)",
    EW_PROTOCOL: "协议错误(EW_PROTOCOL)",
    EW_FUNC: "命令准备错误(EW_FUNC)",
    EW_LENGTH: "数据块长度错误(EW_LENGTH)",
    EW_NUMBER: "数据编号错误(EW_NUMBER)",
    EW_ATTRIB: "数据属性错误(EW_ATTRIB)",
    EW_DATA: "数据错误(EW_DATA)",
    EW_NOOPT: "无选件(EW_NOOPT)",
    EW_PROT: "写保护(EW_PROT)",
    EW_OVRFLOW: "内存溢出(EW_OVRFLOW)",
    EW_PARAM: "CNC 参数不正确(EW_PARAM)",
    EW_BUFFER: "缓冲错误(EW_BUFFER)",
    EW_PATH: "路径错误(EW_PATH)",
    EW_MODE: "CNC 模式错误(EW_MODE)",
    EW_REJECT: "执行被拒绝(EW_REJECT)",
    EW_DTSRVR: "数据服务器错误(EW_DTSRVR)",
    EW_ALARM: "发生报警(EW_ALARM)",
    EW_STOP: "CNC 未运行(EW_STOP)",
    EW_PASSWD: "保护数据错误(EW_PASSWD)",
    EW_PMC: "PMC 侧错误(EW_PMC)",
    EW_PMCHANDLE: "PMC 句柄错误(EW_PMCHANDLE)",
    EW_RD_OVWSTP: "程序读取中覆盖停止(EW_RD_OVWSTP)",
    EW_RD_RSTFIN: "程序读取中复位中断(EW_RD_RSTFIN)",
}
"""EW 返回码 → 文本(fwlib32.h 104~147 行注释意译;i18n 取词)。"""

MAX_AXIS: int = 32
MAX_SPINDLE: int = 8
"""尺寸宏(fwlib32.h 48~54 行:系列宏全不定义时 #else 分支)——
Ethernet 通用 DLL 的构建值;F22_TYPE* 系列限定构建(48/72/96)不支持。"""

ALL_AXES: int = -1
ALL_SPINDLES: int = -1
"""轴/主轴组选择宏(fwlib32.h 79~80 行)。"""

FOCAS_DEFAULT_PORT: int = 8193
"""FOCAS 以太网默认端口(机床内嵌口;档案 §1,示例/旁证双源一致)。"""

_EW_LINK_ERRORS = frozenset(
    {
        EW_SOCKET,
        EW_PROTOCOL,
        EW_HSSB,
        EW_BUS,
        EW_SYSTEM2,
        EW_HANDLE,
        EW_UNEXP,
        EW_PARITY,
        EW_INIERR,
    }
)
"""链路/会话层错误码段:触发拆连(OSError → 基类惰性重连)。

EW_HANDLE(句柄失效)与 EW_UNEXP(异常)按链路处置——句柄已废,
重连重建是唯一恢复路径;EW_NODLL/EW_MMCSYS 归环境错误单独报。"""

# --------------------------------------------------------------------------
# ctypes 结构体(全部 _pack_ = 4,对应头文件 #pragma pack(push,4))
# --------------------------------------------------------------------------


class _ODBSYS(ctypes.Structure):
    """ODBSYS(cnc_sysinfo;fwlib32.h 2850~2858 行默认分支,18 字节)。"""

    _pack_ = 4
    _fields_ = [
        ("addinfo", ctypes.c_short),  # additional information
        ("max_axis", ctypes.c_short),  # maximum axis number
        ("cnc_type", ctypes.c_char * 2),  # cnc type <ascii>
        ("mt_type", ctypes.c_char * 2),  # M/T/TT <ascii>
        ("series", ctypes.c_char * 4),  # series NO. <ascii>
        ("version", ctypes.c_char * 4),  # version NO. <ascii>
        ("axes", ctypes.c_char * 2),  # axis number <ascii>
    ]


class _ODBDY2Axes(ctypes.Structure):
    """ODBDY2.pos 的 faxis 分支(4 组 × MAX_AXIS 轴坐标,fwlib32.h 467~473 行)。"""

    _pack_ = 4
    _fields_ = [
        ("absolute", ctypes.c_long * MAX_AXIS),
        ("machine", ctypes.c_long * MAX_AXIS),
        ("relative", ctypes.c_long * MAX_AXIS),
        ("distance", ctypes.c_long * MAX_AXIS),
    ]


class _ODBDY2OneAxis(ctypes.Structure):
    """ODBDY2.pos 的 oaxis 分支(单轴形态,fwlib32.h 474~479 行)。"""

    _pack_ = 4
    _fields_ = [
        ("absolute", ctypes.c_long),
        ("machine", ctypes.c_long),
        ("relative", ctypes.c_long),
        ("distance", ctypes.c_long),
    ]


class _ODBDY2Pos(ctypes.Union):
    """ODBDY2.pos union(fwlib32.h 467~480 行)。"""

    _pack_ = 4
    _fields_ = [
        ("faxis", _ODBDY2Axes),
        ("oaxis", _ODBDY2OneAxis),
    ]


class _ODBDY2(ctypes.Structure):
    """ODBDY2(cnc_rddynamic2;fwlib32.h 457~481 行)。

    与 ODBDY(431~455 行)前部字段类型不同(alarm/prgnum/prgmnum
    short vs long)——只绑 ODBDY2 防混用错位。``pos`` union:轴=0 时
    DLL 只填 ``oaxis``("In case of 1 axis");轴=ALL_AXES(-1) 填
    ``faxis``。
    """

    _pack_ = 4
    _fields_ = [
        ("dummy", ctypes.c_short),
        ("axis", ctypes.c_short),  # axis number
        ("alarm", ctypes.c_long),  # alarm status
        ("prgnum", ctypes.c_long),  # current program number
        ("prgmnum", ctypes.c_long),  # main program number
        ("seqnum", ctypes.c_long),  # current sequence number
        ("actf", ctypes.c_long),  # actual feedrate
        ("acts", ctypes.c_long),  # actual spindle speed
        ("pos", _ODBDY2Pos),
    ]


class _ODBST2(ctypes.Structure):
    """ODBST2(cnc_statinfo2;fwlib32.h 2928~2942 行,无条件编译,13 short)。

    ODBST(2884~2925 行)有三种系列布局——直接用 ODBST2 规避系列差异。
    """

    _pack_ = 4
    _fields_ = [
        ("hdck", ctypes.c_short),  # handle retrace status
        ("tmmode", ctypes.c_short),  # T/M mode
        ("aut", ctypes.c_short),  # selected automatic mode
        ("run", ctypes.c_short),  # running status
        ("motion", ctypes.c_short),  # axis, dwell status
        ("mstb", ctypes.c_short),  # m, s, t, b status
        ("emergency", ctypes.c_short),  # emergency stop status
        ("alarm", ctypes.c_short),  # alarm status
        ("edit", ctypes.c_short),  # editing status
        ("warning", ctypes.c_short),  # warning status
        ("o3dchk", ctypes.c_short),  # interference check status
        ("ext_opt", ctypes.c_short),  # optional parameter extra status
        ("restart", ctypes.c_short),  # restart status
    ]


# 头文件常量独立于结构体(ODBCNCID 即 4×unsigned long,无 typedef 名)
_CNC_ID_COUNT = 4


# --------------------------------------------------------------------------
# 动态库加载与函数表
# --------------------------------------------------------------------------


def _resolve_dll(sdk_dir: Optional[str], dll_path: Optional[str]) -> str:
    """解析 fwlib32 动态库路径(内部方法)。

    ``dll_path`` 显式指定优先;否则 ``sdk_dir`` 下按解释器位数找
    ``win64|win32`` 子目录再退根目录。fwlib32 官方以 32 位形态分发
    (本地 fwlib 物料全套为 32 位),64 位 Python 需 FANUC 提供的
    64 位 fwlib32.dll(x64 上 stdcall == cdecl,WinDLL 同样正确)。
    """
    if dll_path:
        return dll_path
    if not sdk_dir:
        raise ValueError(_("必须提供 sdk_dir 或 dll_path 之一"))
    bit_dir = "win64" if _pointer_size() == 8 else "win32"
    name = "Fwlib32.dll"
    # 物料常见形态:文件名大小写混排(Fwlib32.dll / fwlib32.dll),两级兜底
    candidates = [
        os.path.join(sdk_dir, bit_dir, name),
        os.path.join(sdk_dir, name),
        os.path.join(sdk_dir, bit_dir, "fwlib32.dll"),
        os.path.join(sdk_dir, "fwlib32.dll"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise OmniPLCInternalError(
        _("SDK 动态库未找到:{}(请确认 sdk_dir 指向含 {} 的目录)").format(name, bit_dir)
    )


def _pointer_size() -> int:
    """解释器指针宽度(内部函数;测试替换点)。"""
    import struct as _struct

    return _struct.calcsize("P")


def _load_fwlib(sdk_dir: Optional[str], dll_path: Optional[str]) -> ctypes.CDLL:
    """加载 fwlib32 动态库(模块级工厂,测试替换点;Windows 专用)。

    Windows 侧 ``WINAPI`` = ``__stdcall``(fwlib32.h 32~42 行)→
    ``ctypes.WinDLL``;Linux ``.so`` 留后续(档案 §1),加载期显式报
    不支持。依赖 DLL(fwlibe1.dll 等)与主库同目录,加载前前置 PATH
    与 add_dll_directory(海康 SDK 同款)。
    """
    if platform.system() != "Windows":
        raise OmniPLCInternalError(
            _("FOCAS 封装首批仅支持 Windows(fwlib32.dll);Linux 侧 .so 留后续版本")
        )
    path = _resolve_dll(sdk_dir, dll_path)
    directory = os.path.dirname(os.path.abspath(path))
    env_path = os.environ.get("PATH", "")
    if directory.lower() not in env_path.lower():
        os.environ["PATH"] = directory + os.pathsep + env_path
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(directory)
    return ctypes.WinDLL(path)


class _FwlibFunctions:
    """按头文件签名的 ctypes 函数表(内部类;全部 __stdcall)。

    绑定面 = 连接生命周期 5 函数 + 首批只读 3 函数(fwlib32.h 行号
    就近标注);restype 统一 c_short(FOCAS 返回 short)。
    """

    def __init__(self, dll: ctypes.CDLL) -> None:
        # allocate library handle 3(15122 行;Ethernet)
        dll.cnc_allclibhndl3.restype = ctypes.c_short
        dll.cnc_allclibhndl3.argtypes = [
            ctypes.c_char_p,
            ctypes.c_ushort,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_ushort),
        ]
        # free library handle(12239 行)
        dll.cnc_freelibhndl.restype = ctypes.c_short
        dll.cnc_freelibhndl.argtypes = [ctypes.c_ushort]
        # set timeout for socket(15128 行)
        dll.cnc_settimeout.restype = ctypes.c_short
        dll.cnc_settimeout.argtypes = [ctypes.c_ushort, ctypes.c_long]
        # read cnc id(11288 行)
        dll.cnc_rdcncid.restype = ctypes.c_short
        dll.cnc_rdcncid.argtypes = [
            ctypes.c_ushort,
            ctypes.POINTER(ctypes.c_ulong * _CNC_ID_COUNT),
        ]
        # read CNC system information(12140 行)
        dll.cnc_sysinfo.restype = ctypes.c_short
        dll.cnc_sysinfo.argtypes = [ctypes.c_ushort, ctypes.POINTER(_ODBSYS)]
        # read all dynamic data 2(10574 行)
        dll.cnc_rddynamic2.restype = ctypes.c_short
        dll.cnc_rddynamic2.argtypes = [
            ctypes.c_ushort,
            ctypes.c_short,
            ctypes.c_short,
            ctypes.POINTER(_ODBDY2),
        ]
        # read CNC status information 2(12146 行)
        dll.cnc_statinfo2.restype = ctypes.c_short
        dll.cnc_statinfo2.argtypes = [ctypes.c_ushort, ctypes.POINTER(_ODBST2)]
        # 逐函数挂为自身属性(海康 _SdkFunctions 同款;会话经属性调用)
        self.cnc_allclibhndl3 = dll.cnc_allclibhndl3
        self.cnc_freelibhndl = dll.cnc_freelibhndl
        self.cnc_settimeout = dll.cnc_settimeout
        self.cnc_rdcncid = dll.cnc_rdcncid
        self.cnc_sysinfo = dll.cnc_sysinfo
        self.cnc_rddynamic2 = dll.cnc_rddynamic2
        self.cnc_statinfo2 = dll.cnc_statinfo2


# --------------------------------------------------------------------------
# 返回码处置
# --------------------------------------------------------------------------


def _ew_text(code: int) -> str:
    """EW 返回码 → 文本(未知码给带符号十进制;内部函数)。"""
    if code in EW_TEXT:
        return _(EW_TEXT[code])
    return _("未知返回码 {}").format(code)


def _check_rc(rc: int, action: str) -> None:
    """FOCAS 返回码分流(内部方法):链路段拆连、环境错误单独报、
    其余 DeviceError 不断线(海康 _check_rc 同款三分)。

    :param rc: FOCAS 返回值(0 = EW_OK;short 经 c_short 已带符号)
    :param action: 操作名(进错误文案)
    """
    if rc == EW_OK:
        return
    if rc == EW_NODLL:
        # DLL 运行库中途缺失属环境问题,不是单次事务失败
        raise OmniPLCInternalError(
            _("{}失败:{}(请确认 fwlib32.dll 及依赖库可被加载)").format(
                action, _ew_text(rc)
            )
        )
    if rc == EW_MMCSYS:
        raise OmniPLCInternalError(
            _("{}失败:{}(请确认运行环境)").format(action, _ew_text(rc))
        )
    if rc in _EW_LINK_ERRORS:
        # OSError → 基类 _execute 拆连惰性重连
        raise OSError(_("{}失败:{}(链路/会话故障,将重连)").format(action, _ew_text(rc)))
    raise DeviceError(
        _("{}失败:{}").format(action, _ew_text(rc)),
        rc,
    )


# --------------------------------------------------------------------------
# 会话适配
# --------------------------------------------------------------------------


class _FocasSession(BaseTransport):
    """FOCAS 会话(加载动态库 + 库句柄生命周期,内部传输适配)。

    FOCAS 为会话型通道(无字节流 send/recv,同海康 SDK/MX 形态)。
    """

    def __init__(
        self,
        ip_address: str,
        port: int,
        sdk_dir: Optional[str],
        dll_path: Optional[str],
    ) -> None:
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._sdk_dir = sdk_dir
        self._dll_path = dll_path
        self._functions: Optional[_FwlibFunctions] = None
        self._handle: Optional[int] = None
        self._cnc_id: Optional[str] = None
        self._debug_label = "focas://{}:{}".format(ip_address, port)

    @property
    def cnc_id(self) -> Optional[str]:
        """连接时读得的 CNC ID(8 位十六进制 ×4 连字符连接;未连接为 None)。"""
        return self._cnc_id

    def connect(self) -> None:
        """加载动态库 → 分配库句柄(allclibhndl3)→ 读 CNC ID 确认连通。

        :raises OmniPLCInternalError: 动态库加载失败或平台不支持
        :raises OSError: 连接类错误(基类惰性重连)
        :raises DeviceError: CNC 侧拒绝
        """
        self.close()
        try:
            dll = _load_fwlib(self._sdk_dir, self._dll_path)
        except OSError as exc:
            raise OmniPLCInternalError(
                _("SDK 动态库加载失败:{}(请检查位数与依赖库是否齐全)").format(exc)
            ) from exc
        self._functions = _FwlibFunctions(dll)
        handle = ctypes.c_ushort(0)
        _check_rc(
            self._functions.cnc_allclibhndl3(
                self._ip_address.encode("ascii"),
                self._port,
                max(1, int(self._connect_timeout)),
                ctypes.pointer(handle),
            ),
            _("分配库句柄"),
        )
        self._handle = handle.value
        try:
            ids = (ctypes.c_ulong * _CNC_ID_COUNT)()
            _check_rc(
                self._functions.cnc_rdcncid(self._handle, ctypes.pointer(ids)),
                _("读取 CNC ID"),
            )
            self._cnc_id = "-".join(
                "{:08x}".format(ids[i]) for i in range(_CNC_ID_COUNT)
            )
        except (DeviceError, OSError):
            self.close()
            raise
        log_op(self._debug_label, "会话已建立(cnc_id={})", self._cnc_id)

    def apply_receive_timeout(self) -> None:
        """把 receive_timeout 下发为 socket 超时(cnc_settimeout,15128 行;
        内部方法,每连接属性可随时改)。"""
        functions = self._functions
        handle = self._handle
        if functions is None or handle is None:
            return
        _check_rc(
            functions.cnc_settimeout(handle, max(1, int(self._receive_timeout))),
            _("下发 socket 超时"),
        )

    def close(self) -> None:
        """释放库句柄;重复调用幂等(海康同款逐段吞错)。"""
        functions = self._functions
        handle = self._handle
        if functions is not None and handle is not None:
            try:
                functions.cnc_freelibhndl(handle)
            except (DeviceError, OSError):
                pass
        self._handle = None
        self._functions = None
        self._cnc_id = None

    def send(self, data: bytes) -> None:
        """FOCAS 会话无字节流(内部方法)。"""
        raise TransportClosedError(_("FOCAS 会话不支持字节流发送"))

    def recv(self, size: int) -> bytes:
        """FOCAS 会话无字节流(内部方法)。"""
        raise TransportClosedError(_("FOCAS 会话不支持字节流接收"))

    # ------------------------------------------------------------------
    # 数据读取原语(仅事务锁内;供客户端 _execute 调用)
    # ------------------------------------------------------------------

    def _require_functions(self) -> _FwlibFunctions:
        """取函数表,未建立则抛出(内部方法)。"""
        if self._functions is None or self._handle is None:
            raise TransportClosedError(_("FOCAS 会话未建立"))
        return self._functions

    def read_sysinfo_raw(self) -> FocasSysInfo:
        """cnc_sysinfo 原语(内部方法):系统信息。"""
        functions = self._require_functions()
        info = _ODBSYS()
        _check_rc(
            functions.cnc_sysinfo(self._handle, ctypes.pointer(info)),
            _("读取 CNC 系统信息"),
        )
        result = FocasSysInfo(
            addinfo=info.addinfo,
            max_axis=info.max_axis,
            cnc_type=_c_string(info.cnc_type),
            mt_type=_c_string(info.mt_type),
            series=_c_string(info.series),
            version=_c_string(info.version),
            axes=_c_string(info.axes),
        )
        log_op(
            self._debug_label,
            "sysinfo → %s %s.%s axes=%s",
            result.cnc_type,
            result.series,
            result.version,
            result.axes,
        )
        return result

    def read_dynamic_raw(self, axis: int) -> FocasDynamic:
        """cnc_rddynamic2 原语(内部方法):实时状态。

        :param axis: ``ALL_AXES``(-1)= 坐标按轴列表;具体轴号 = 标量
        """
        functions = self._require_functions()
        dyn = _ODBDY2()
        _check_rc(
            functions.cnc_rddynamic2(self._handle, axis, 0, ctypes.pointer(dyn)),
            _("读取 CNC 实时状态"),
        )
        if axis == ALL_AXES:
            # 响应 axis 字段 = 机床实际轴数(0~MAX_AXIS);越界口径按满轴收窄
            count = min(dyn.axis, MAX_AXIS) if 0 < dyn.axis <= MAX_AXIS else MAX_AXIS
            absolute = [dyn.pos.faxis.absolute[i] for i in range(count)]
            machine = [dyn.pos.faxis.machine[i] for i in range(count)]
            relative = [dyn.pos.faxis.relative[i] for i in range(count)]
            distance = [dyn.pos.faxis.distance[i] for i in range(count)]
        else:
            absolute = dyn.pos.oaxis.absolute
            machine = dyn.pos.oaxis.machine
            relative = dyn.pos.oaxis.relative
            distance = dyn.pos.oaxis.distance
        result = FocasDynamic(
            alarm=dyn.alarm,
            prgnum=dyn.prgnum,
            prgmnum=dyn.prgmnum,
            seqnum=dyn.seqnum,
            actf=dyn.actf,
            acts=dyn.acts,
            absolute=absolute,
            machine=machine,
            relative=relative,
            distance=distance,
        )
        log_op(
            self._debug_label,
            "dynamic axis=%s → alarm=%s prg=%s actf=%s acts=%s",
            axis,
            result.alarm,
            result.prgnum,
            result.actf,
            result.acts,
        )
        return result

    def read_status_raw(self) -> FocasStatus:
        """cnc_statinfo2 原语(内部方法):CNC 状态位(探活探测命令)。"""
        functions = self._require_functions()
        status = _ODBST2()
        _check_rc(
            functions.cnc_statinfo2(self._handle, ctypes.pointer(status)),
            _("读取 CNC 状态"),
        )
        result = FocasStatus(
            hdck=status.hdck,
            tmmode=status.tmmode,
            aut=status.aut,
            run=status.run,
            motion=status.motion,
            mstb=status.mstb,
            emergency=status.emergency,
            alarm=status.alarm,
            edit=status.edit,
            warning=status.warning,
            o3dchk=status.o3dchk,
            ext_opt=status.ext_opt,
            restart=status.restart,
        )
        log_op(
            self._debug_label,
            "status → run=%s emergency=%s alarm=%s",
            result.run,
            result.emergency,
            result.alarm,
        )
        return result


# --------------------------------------------------------------------------
# 结果类型
# --------------------------------------------------------------------------


def _c_string(raw: bytes) -> str:
    """FOCAS 定长 ASCII 字段 → str(去尾部填充;内部函数)。"""
    return raw.split(b"\x00")[0].decode("ascii", "replace").strip()


class FocasSysInfo(NamedTuple):
    """CNC 系统信息(:meth:`FanucFocasClient.read_sysinfo`;ODBSYS)。"""

    addinfo: int
    """附加信息(系列细分类)。"""
    max_axis: int
    """最大轴数。"""
    cnc_type: str
    """CNC 类型(ASCII 两字符,如 "15"/"16"/"30")。"""
    mt_type: str
    """机床类型(T/M/TT)。"""
    series: str
    """系列号(ASCII 四字符)。"""
    version: str
    """版本号(ASCII 四字符)。"""
    axes: str
    """轴数(ASCII 两字符)。"""


class FocasDynamic(NamedTuple):
    """CNC 实时状态(:meth:`FanucFocasClient.read_dynamic`;ODBDY2)。

    坐标四组:轴=ALL_AXES 时为逐轴列表(长度 = 机床轴数,由响应 ``axis``
    字段给出;超出实际轴数的槽位为 0);轴=单轴号时为标量。
    """

    alarm: int
    """报警状态(报警号;0 = 无报警)。"""
    prgnum: int
    """当前程序号。"""
    prgmnum: int
    """主程序号。"""
    seqnum: int
    """当前序列号(N 号)。"""
    actf: int
    """实际进给速度。"""
    acts: int
    """实际主轴转速。"""
    absolute: Union[List[int], int]
    """绝对坐标。"""
    machine: Union[List[int], int]
    """机械坐标。"""
    relative: Union[List[int], int]
    """相对坐标。"""
    distance: Union[List[int], int]
    """剩余移动量(distance to go)。"""


class FocasStatus(NamedTuple):
    """CNC 状态位(:meth:`FanucFocasClient.read_status`;ODBST2)。"""

    hdck: int
    """手轮回退状态。"""
    tmmode: int
    """T/M 模式。"""
    aut: int
    """自动运行模式选择。"""
    run: int
    """运行状态。"""
    motion: int
    """轴移动/暂停状态。"""
    mstb: int
    """M/S/T/B 辅助功能状态。"""
    emergency: int
    """急停状态。"""
    alarm: int
    """报警状态。"""
    edit: int
    """编辑状态。"""
    warning: int
    """警告状态。"""
    o3dchk: int
    """干涉检查状态。"""
    ext_opt: int
    """选件参数扩展状态。"""
    restart: int
    """再启动状态。"""


class FocasCncId(NamedTuple):
    """CNC ID(cnc_rdcncid;4×32 位无符号)。"""

    parts: List[int]
    """4 个 32 位 ID 段(十进制)。"""
    text: str
    """"%08x" ×4 连字符连接形态(示例口径,如 "00000001-…")。"""


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class FanucFocasClient(BaseClient):
    """FANUC CNC 数采客户端(FOCAS fwlib32.dll ctypes 封装,只读)。

    :example::

        client = FanucFocasClient("192.168.1.10", sdk_dir=r"C:\\fwlib")
        client.connect()
        ok, info = client.read_sysinfo()     # 系列/版本/轴数
        ok, dyn = client.read_dynamic()      # 实时状态:报警/程序/进给/主轴/坐标
        if ok:
            print(dyn.acts, dyn.absolute)    # 主轴转速 / 绝对坐标
        ok, status = client.read_status()    # 模式/运行/急停/报警位
        client.close()

    依赖:FANUC FOCAS 运行库(``Fwlib32.dll`` 及 ``fwlibe1.dll`` 等依赖,
    随 FOCAS Development 包或随机资料分发,须现场安装);``sdk_dir`` 指向
    含动态库的目录,或 ``dll_path`` 显式指定。机床侧需启用内嵌以太网口
    (端口 8193)。**首批仅支持 Windows**。
    """

    _has_ping = True
    """支持探活(:meth:`read_status` 走 cnc_statinfo2,零副作用)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = FOCAS_DEFAULT_PORT,
        *,
        sdk_dir: Optional[str] = None,
        dll_path: Optional[str] = None,
    ) -> None:
        """初始化 FOCAS 客户端。

        :param ip_address: CNC 的 IP(内嵌以太网口)
        :param port: FOCAS 端口,标准 8193
        :param sdk_dir: fwlib32 动态库目录(含 ``Fwlib32.dll``/``fwlib32.dll``)
        :param dll_path: 动态库显式路径(优先于 ``sdk_dir``)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if sdk_dir is None and dll_path is None:
            raise ValueError(_("必须提供 sdk_dir 或 dll_path 之一"))
        self._sdk_dir = sdk_dir
        self._dll_path = dll_path

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _FocasSession:
        """取当前 FOCAS 会话(仅事务锁内调用,内部方法)。"""
        transport = self._require_transport()
        if not isinstance(transport, _FocasSession):
            raise TransportClosedError(_("FOCAS 会话未建立"))
        return transport

    def _create_transport(self) -> BaseTransport:
        return _FocasSession(
            self._ip_address, self._port, self._sdk_dir, self._dll_path
        )

    def connect(self) -> bool:
        """建立 FOCAS 会话(加载库 → 分配句柄 → 读 CNC ID)。"""
        ok = super().connect()
        if ok:
            transport = self._transport
            if isinstance(transport, _FocasSession):
                try:
                    transport.apply_receive_timeout()
                except (DeviceError, OSError):
                    pass  # 超时下发尽力而为,失败不阻断建连
        return ok

    @property
    def cnc_id(self) -> Optional[str]:
        """连接时读得的 CNC ID(未连接为 None;转发会话)。"""
        transport = self._transport
        if isinstance(transport, _FocasSession):
            return transport.cnc_id
        return None

    def close(self) -> bool:
        """断开会话(:meth:`disconnect` 别名,海康 SDK 客户端同款)。"""
        return self.disconnect()

    def _ping_probe(self) -> str:
        """探活探测命令:CNC 状态读(cnc_statinfo2,零副作用;内部方法)。"""
        return str(self._session().read_status_raw())

    # ------------------------------------------------------------------
    # 基类抽象方法:FOCAS 为结构化数采面(无通用地址读写原语)
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """FOCAS 无通用地址读写(内部方法):引导到结构化数采 API。"""
        raise DeviceError(
            _(
                "FOCAS 为结构化数采面,无通用地址读写;请用 read_sysinfo/read_dynamic/read_status"
            ),
            0,
        )

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """FOCAS 首批只读(内部方法):数采口径不提供写入。"""
        raise DeviceError(_("FOCAS 数采客户端为只读,不支持写入"), 0)

    # ------------------------------------------------------------------
    # 只读数采 API
    # ------------------------------------------------------------------

    def read_sysinfo(self) -> Tuple[bool, Optional[FocasSysInfo]]:
        """读 CNC 系统信息(cnc_sysinfo;类型/系列/版本/轴数)。"""
        return self._execute(self._session().read_sysinfo_raw)

    def read_dynamic(self, axis: int = ALL_AXES) -> Tuple[bool, Optional[FocasDynamic]]:
        """读 CNC 实时状态(cnc_rddynamic2;报警/程序/进给/主轴/坐标)。

        :param axis: ``ALL_AXES``(-1,缺省)= 坐标四组按轴返回列表;
            具体轴号(0 起)= 坐标为标量
        :return: ``(是否成功, :class:`FocasDynamic`)``
        :raises ValueError: ``axis`` 非(整数轴号 | ALL_AXES)
        """
        if not isinstance(axis, int) or isinstance(axis, bool):
            raise ValueError(_("axis 必须是整数轴号或 ALL_AXES,收到:{!r}").format(axis))
        return self._execute(lambda: self._session().read_dynamic_raw(axis))

    def read_status(self) -> Tuple[bool, Optional[FocasStatus]]:
        """读 CNC 状态位(cnc_statinfo2;模式/运行/急停/报警等 13 位)。"""
        return self._execute(self._session().read_status_raw)
