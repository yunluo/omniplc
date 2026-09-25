"""西门子 S7 客户端(封装 python-snap7,ISO-on-TCP 102)。

S7comm 是完整私有协议栈(TPKT/COTP/S7 PDU、机架/槽位路由、
S7-1200/1500 的 PUT-GET 授权与优化块限制),**不自研**,封装成熟库
`python-snap7`,依赖按解释器版本二选一(``s7`` extra 环境标记自动生效,
核心库本体仍为 3.7.9+):

- Python 3.7~3.9 → **1.3**:C 库封装末版线,wheel 捆绑 64 位原生库,
  32 位 Python 需自备 32 位 snap7.dll 并经 ``dll_path`` 指定
- Python 3.10+ → **3.x**:3.0 起纯 Python 实现,不再需要原生 DLL

两线 API 有差异,均在边界处适配:错误类 1.x/2.x 抛 RuntimeError、
3.x 抛 ``S7Error`` 谱系(见 ``_SNAP7_ERRORS``);area 参数 1.x/2.x 要求
``Areas`` 枚举成员、3.x 收裸 int(统一经 :func:`_snap7_area` 转换);
构造参数 1.x/2.x 真实加载原生库、3.x 忽略 ``lib_location``。

类继承::

    BaseClient
    └── SiemensS7Client   S7 会话(默认 rack 0 / slot 1 / 端口 102;适配见 _S7Session)

地址语法见 :mod:`omniplc.plc.siemens.address`(DB/I/Q/M,尺寸由显式
DataType 决定,大端序)。S7-1200/1500 侧需勾选"允许来自远程对象的
PUT/GET 通信访问",且 DB 须为**非优化块**(绝对寻址)。

错误边界:snap7 抛错无统一类型区分,以 ``Cli_GetConnected``
连接态判别——在线 → DeviceError(PLC 拒绝/地址错,不断线),断连 →
OSError(惰性重连);连接建立失败 → OSError。

v1 范围:单点读写(位读改写)+ S7 String;多变量组包(read_multi)、
块操作、SZL 系统状态留后续版本。
"""
from __future__ import annotations

import struct
from typing import Any, NoReturn, Optional, Tuple

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import S7_DEFAULT_PORT, S7_DEFAULT_RACK, S7_DEFAULT_SLOT, S7_RACK_MAX, S7_SLOT_MAX
from ...core.debug import log_op
from ...core.errors import DeviceError, TransportClosedError
from ...core.validation import require_bool, require_float, require_int
from ...types import DataType, PrimitiveValue
from ...transport.base import BaseTransport
from .address import area_code, parse_s7_address

_SIZES = {
    DataType.BOOL: 1,
    DataType.SHORT: 2,
    DataType.USHORT: 2,
    DataType.INT: 4,
    DataType.UINT: 4,
    DataType.LONG: 8,
    DataType.ULONG: 8,
    DataType.FLOAT: 4,
    DataType.DOUBLE: 8,
}
"""数值 DataType → 字节数(S7 大端序)。"""

_INT_FORMATS = {
    DataType.SHORT: ">h",
    DataType.USHORT: ">H",
    DataType.INT: ">i",
    DataType.UINT: ">I",
    DataType.LONG: ">q",
    DataType.ULONG: ">Q",
}
"""整数 DataType → struct 大端格式(含符号语义)。"""

_SNAP7_ERRORS: Tuple[Any, ...] = (RuntimeError,)
"""snap7 错误类元组(会话边界捕获):1.x/2.x 抛 RuntimeError;3.x 纯
Python 抛 ``S7Error`` 谱系(基类挂在 snap7.client 命名空间,由
:func:`_new_client` 探测并入表)。"""

_AREAS_ENUM: Any = False
"""snap7 ``Areas`` 枚举类缓存:False = 未探测,None = 探测失败(裸 int
透传),否则为枚举类。1.x 在 ``snap7.types``、2.x/3.x 在 ``snap7.type``。"""


def _snap7_area(area: int) -> Any:
    """协议区码 int → snap7 ``Areas`` 枚举成员(内部函数)。

    1.x 的 ``read_area`` 对 area 做枚举成员校验(裸 int 抛 ValueError)、
    ``write_area`` 直接取 ``area.value``(裸 int 抛 AttributeError),
    2.x 校验更严;3.x 虽收裸 int,统一转枚举全兼容。探测不到枚举时
    (假 Client 单测环境)原样返回 int。

    :param area: 协议区码(0x81 PE / 0x82 PA / 0x83 MK / 0x84 DB)
    """
    global _AREAS_ENUM
    if _AREAS_ENUM is False:
        _AREAS_ENUM = None
        for module_name in ("snap7.type", "snap7.types"):
            try:
                module = __import__(module_name, fromlist=["Areas"])
            except ImportError:
                continue
            areas = getattr(module, "Areas", None)
            if areas is not None:
                _AREAS_ENUM = areas
                break
    if _AREAS_ENUM is not None:
        try:
            return _AREAS_ENUM(area)
        except ValueError:
            pass
    return area


def _new_client(dll_path: str) -> Any:
    """创建 snap7 Client(模块级,单测以假对象替换;内部函数)。

    :param dll_path: 原生库路径(仅 1.x/2.x 生效;3.x 纯 Python 忽略)
    :raises OSError: python-snap7 未安装或 snap7 原生库加载失败
    """
    global _SNAP7_ERRORS
    try:
        import snap7.client
    except Exception as exc:
        raise OSError(
            f"python-snap7 加载失败(pip install omniplc[s7]):{exc}"
        ) from exc
    error_base = getattr(snap7.client, "S7Error", None)
    if error_base is not None:
        _SNAP7_ERRORS = (RuntimeError, error_base)
    try:
        return snap7.client.Client(dll_path or None)
    except (OSError, RuntimeError) as exc:
        raise OSError(
            "snap7 原生库加载失败:{}(3.7~3.9 用 python-snap7 1.3:64 位"
            " Python 可用捆绑 DLL,32 位需自备 32 位 snap7.dll 经 dll_path"
            " 指定;3.10+ 为纯 Python 实现无需 DLL)".format(exc)
        ) from exc


class _S7Session(BaseTransport):
    """S7 会话适配器:snap7 Client 适配为传输对象外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 加载
    snap7 库并连 CPU,``close`` 断开并销毁;无字节流收发,区域读写经
    :meth:`read_area` / :meth:`write_area` 完成,snap7 错误(1.x/2.x
    RuntimeError、3.x S7Error 谱系)在此边界按连接态翻译
    (在线→DeviceError 不断线,断连→OSError 惰性重连)。
    """

    def __init__(
        self,
        ip_address: str,
        rack: int,
        slot: int,
        port: int,
        dll_path: str,
    ) -> None:
        """S7 会话适配器。

        :param ip_address: PLC 的 IP 或主机名
        :param rack: 机架号
        :param slot: 槽位号
        :param port: ISO-on-TCP 端口,标准 102
        :param dll_path: snap7 原生库路径(1.x/2.x 生效,留空用捆绑库)
        """
        super().__init__()
        self._ip_address = ip_address
        self._rack = rack
        self._slot = slot
        self._port = port
        self._dll_path = dll_path
        self._client: Optional[Any] = None
        self._debug_label = "s7://{}:{}(机架{}槽位{})".format(
            ip_address, port, rack, slot
        )

    def connect(self) -> None:
        """加载 snap7 库并连接 CPU(每次连接新建 Client)。

        :raises OSError: 库加载失败或连接失败(拒绝/超时/路由参数不符)
        """
        client = _new_client(self._dll_path)
        try:
            client.connect(self._ip_address, self._rack, self._slot, self._port)
        except _SNAP7_ERRORS as exc:
            raise OSError(
                "S7 连接失败:{}(检查 IP/机架/槽位,1200/1500 需开启"
                " PUT-GET 访问授权)".format(exc)
            ) from exc
        self._client = client
        log_op(self._debug_label, "会话已建立")

    def close(self) -> None:
        """断开连接并销毁 Client,幂等。"""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.destroy()
        except Exception:
            pass
        log_op(self._debug_label, "会话已断开")

    def send(self, data: bytes) -> None:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("S7 走会话通道,无字节流收发")

    def recv(self, size: int) -> bytes:
        """S7 为会话型协议,无字节流收发(不调用)。"""
        raise TransportClosedError("S7 走会话通道,无字节流收发")

    def read_area(self, area: int, db_number: int, start: int, size: int) -> bytes:
        """读一块区域字节(会话调用,异常在此翻译)。"""
        try:
            data = self._require_client().read_area(
                _snap7_area(area), db_number, start, size
            )
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc)
        log_op(
            self._debug_label,
            "read area=0x%02X db=%d start=%d size=%d → %dB",
            area,
            db_number,
            start,
            size,
            len(data),
        )
        return bytes(data)

    def write_area(self, area: int, db_number: int, start: int, data: bytes) -> None:
        """写一块区域字节(会话调用,异常在此翻译)。"""
        try:
            self._require_client().write_area(
                _snap7_area(area), db_number, start, bytearray(data)
            )
        except _SNAP7_ERRORS as exc:
            self._raise_link_aware(exc)
        log_op(
            self._debug_label,
            "write area=0x%02X db=%d start=%d %dB",
            area,
            db_number,
            start,
            len(data),
        )

    def _raise_link_aware(self, exc: BaseException) -> NoReturn:
        """按 snap7 连接态翻译错误(内部方法,恒抛出)。

        在线 → :class:`DeviceError`(PLC 侧拒绝,不断线);
        断连 → :class:`OSError`(惰性重连)。
        """
        if self._is_connected():
            raise DeviceError(f"S7 错误:{exc}", 0)
        raise OSError(f"S7 连接已断:{exc}")

    def _is_connected(self) -> bool:
        """取 snap7 本地连接态标志(不产生网络流量;异常视为断连)。"""
        try:
            return bool(self._require_client().get_connected())
        except Exception:
            return False

    def _require_client(self) -> Any:
        """取当前 snap7 Client,未建立则抛出(内部方法)。"""
        if self._client is None:
            raise TransportClosedError("S7 会话未建立")
        return self._client


class SiemensS7Client(BaseClient):
    """西门子 S7 客户端(封装 python-snap7,rack/slot 路由)。

    :example::

        client = SiemensS7Client("192.168.0.1", rack=0, slot=1)
        client.connect()
        ok, value = client.read_float("DB1.DBD6")
        ok = client.write_bool("DB1.DBX0.3", True)
        ok, text = client.read_string("DB1.DBS20", length=32)
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.1",
        port: int = S7_DEFAULT_PORT,
        rack: int = S7_DEFAULT_RACK,
        slot: int = S7_DEFAULT_SLOT,
        dll_path: str = "",
    ) -> None:
        """初始化 S7 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param rack: 机架号,S7_DEFAULT_RACK(0)
        :param slot: 槽位号,1200/1500 常用 1;300/400 的 CPU 常在 2
        :param dll_path: snap7 原生库路径显式覆盖,仅 1.x/2.x(C 封装线)
            生效——32 位 Python 需自备 32 位 snap7.dll;3.x 纯 Python 实现
            忽略此参数;留空用捆绑库
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if not 0 <= int(rack) <= S7_RACK_MAX:
            raise ValueError(f"机架号必须在 0~{S7_RACK_MAX} 之间,收到:{rack}")
        if not 0 <= int(slot) <= S7_SLOT_MAX:
            raise ValueError(f"槽位号必须在 0~{S7_SLOT_MAX} 之间,收到:{slot}")
        self._rack = int(rack)
        self._slot = int(slot)
        self._dll_path = dll_path.strip()

    @property
    def rack(self) -> int:
        """机架号。"""
        return self._rack

    @property
    def slot(self) -> int:
        """槽位号。"""
        return self._slot

    # ------------------------------------------------------------------
    # 会话访问(仅事务锁内)
    # ------------------------------------------------------------------

    def _session(self) -> _S7Session:
        """取当前 S7 会话适配器(仅事务锁内调用,内部方法)。"""
        link = self._require_transport()
        if not isinstance(link, _S7Session):
            raise TransportClosedError("内部错误:传输对象不是 S7 会话")
        return link

    def _create_transport(self) -> BaseTransport:
        return _S7Session(
            self._ip_address, self._rack, self._slot, self._port, self._dll_path
        )

    # ------------------------------------------------------------------
    # 协议原语
    # ------------------------------------------------------------------

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """读数据项并按 DataType 尺寸收窄(大端序)。"""
        if data_type not in _SIZES:
            raise ValueError(f"S7 不支持的数据类型:{data_type}")
        parsed = parse_s7_address(address)
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    f"S7 按位读取需要位地址:{address!r}(示例:M10.2 / DB1.DBX0.3)"
                )
        elif parsed.bit is not None:
            raise ValueError(
                f"S7 位地址只能按 BOOL 读写:{address!r}(数值请用字节起点地址)"
            )
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, _SIZES[data_type]
        )
        if data_type is DataType.BOOL:
            # 前置校验已保证 bit 非空,or 0 仅供类型收窄
            return bool((data[0] >> (parsed.bit or 0)) & 1)
        if data_type is DataType.FLOAT:
            return struct.unpack(">f", data)[0]
        if data_type is DataType.DOUBLE:
            return struct.unpack(">d", data)[0]
        return int.from_bytes(data, "big", signed=data_type in (DataType.SHORT, DataType.INT, DataType.LONG))

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        """写数据项;位为锁内读-改-写,数值按大端编码。"""
        parsed = parse_s7_address(address)
        session = self._session()
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    f"S7 按位写入需要位地址:{address!r}(示例:M10.2 / DB1.DBX0.3)"
                )
            flag = require_bool(value)
            raw = session.read_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
            )
            byte = (raw[0] | (1 << parsed.bit)) if flag else (raw[0] & ~(1 << parsed.bit))
            session.write_area(
                area_code(parsed.area), parsed.db_number, parsed.byte_index, bytes([byte & 0xFF])
            )
            return
        if data_type is DataType.FLOAT:
            number = require_float(value)
            data = self._pack(">f", number)
        elif data_type is DataType.DOUBLE:
            number = require_float(value)
            data = self._pack(">d", number)
        else:
            number = require_int(value)
            data = self._pack(_INT_FORMATS[data_type], number)
        if parsed.bit is not None:
            raise ValueError(
                f"S7 位地址只能按 BOOL 读写:{address!r}(数值请用字节起点地址)"
            )
        session.write_area(area_code(parsed.area), parsed.db_number, parsed.byte_index, data)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读 S7 String(头 2 字节 = 声明长/实际长,正文按声明长)。

        实际长超出请求 ``length`` 时按 ``length`` 截断返回(不报错不丢帧)。
        """
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(f"S7 字符串地址不带位号:{address!r}")
        size = length + 2
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, size
        )
        if len(data) < 2:
            raise DeviceError("S7 String 响应过短:{}".format(len(data)), 0)
        actual = data[1]
        if actual <= 0:
            return ""
        if actual > length:
            # PLC 侧实际长 > 请求 length:截断返回,避免静默丢成空串
            actual = length
        return convert.decode_string(data[2:2 + actual], encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写 S7 String(声明长字节保留 PLC 侧现值,仅覆盖实际长字节)。

        先读 1 字节取 PLC 侧声明长(STRING[x] 的 x),写入值超声明长时
        拒绝(防溢出污染相邻变量);声明长字节读得 0(未初始化区)时
        按本次编码长度落盘(与旧版行为兼容)。
        """
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError(f"S7 字符串地址不带位号:{address!r}")
        encoded = convert.encode_string(value, len(value.encode(encoding)), encoding)
        head = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, 1
        )
        declared_max = head[0] if head else 0
        if declared_max == 0:
            # 未初始化区(声明长为 0 非法):退回旧口径,声明长=实际长
            declared_max = len(encoded)
        if len(encoded) > declared_max:
            raise ValueError(
                "S7 String 写入值超出 PLC 侧声明长:{} > {} 字符({!r})".format(
                    len(encoded), declared_max, address
                )
            )
        header = bytes([declared_max, len(encoded)])
        self._session().write_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, header + encoded
        )
        return value

    @staticmethod
    def _pack(fmt: str, value: PrimitiveValue) -> bytes:
        """大端打包,越界 struct 报错统一转 ValueError(内部方法)。"""
        try:
            return struct.pack(fmt, value)
        except (struct.error, OverflowError) as exc:
            raise ValueError(f"S7 写入值超出类型范围:{value}") from exc
