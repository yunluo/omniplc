"""西门子 S7 客户端(封装 python-snap7 1.3,ISO-on-TCP 102)。

S7comm 是完整私有协议栈(TPKT/COTP/S7 PDU、机架/槽位路由、
S7-1200/1500 的 PUT-GET 授权与优化块限制),**不自研**,封装成熟库
`python-snap7`(**1.3** 为最后支持 Python 3.7 的版本线;Windows/Linux
wheel 捆绑 64 位 snap7 原生库,32 位 Python 需自备 32 位 snap7.dll 并经
``dll_path`` 指定)。

类继承::

    BaseClient
    └── SiemensS7Client   S7 会话(默认 rack 0 / slot 1 / 端口 102;适配见 _S7Session)

地址语法见 :mod:`omniplc.plc.siemens.address`(DB/I/Q/M,尺寸由显式
DataType 决定,大端序)。S7-1200/1500 侧需勾选"允许来自远程对象的
PUT/GET 通信访问",且 DB 须为**非优化块**(绝对寻址)。

错误边界:snap7 抛 RuntimeError 无类型区分,以 ``Cli_GetConnected``
连接态判别——在线 → DeviceError(PLC 拒绝/地址错,不断线),断连 →
OSError(惰性重连);连接建立失败 → OSError。

v1 范围:单点读写(位读改写)+ S7 String;多变量组包(read_multi)、
块操作、SZL 系统状态留后续版本。
"""
from __future__ import annotations

import struct
from typing import Any, Optional

from ... import convert
from ...core.base_client import BaseClient, validate_endpoint
from ...core.constants import S7_DEFAULT_PORT, S7_DEFAULT_RACK, S7_DEFAULT_SLOT
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


def _new_client(dll_path: str) -> Any:
    """创建 snap7 Client(模块级,单测以假对象替换;内部函数)。

    :raises OSError: python-snap7 未安装或 snap7 原生库加载失败
    """
    try:
        import snap7.client
    except Exception as exc:
        raise OSError(
            "python-snap7 加载失败(pip install omniplc[s7]):{}".format(exc)
        ) from exc
    try:
        return snap7.client.Client(dll_path or None)
    except (OSError, RuntimeError) as exc:
        raise OSError(
            "snap7 原生库加载失败:{}(64 位 Python 可用捆绑 DLL;"
            "32 位 Python 需自备 32 位 snap7.dll,经 dll_path 参数指定)".format(exc)
        ) from exc


class _S7Session(BaseTransport):
    """S7 会话适配器:snap7 Client 适配为传输对象外形(私有)。

    供 :class:`BaseClient` 的连接状态机直接管理——``connect`` 加载
    snap7 库并连 CPU,``close`` 断开并销毁;无字节流收发,区域读写经
    :meth:`read_area` / :meth:`write_area` 完成,RuntimeError 在此边界
    按连接态翻译(在线→DeviceError 不断线,断连→OSError 惰性重连)。
    """

    def __init__(
        self,
        ip_address: str,
        rack: int,
        slot: int,
        port: int,
        dll_path: str,
    ) -> None:
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
        except RuntimeError as exc:
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
            data = self._require_client().read_area(area, db_number, start, size)
        except RuntimeError as exc:
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
            self._require_client().write_area(area, db_number, start, bytearray(data))
        except RuntimeError as exc:
            self._raise_link_aware(exc)
        log_op(
            self._debug_label,
            "write area=0x%02X db=%d start=%d %dB",
            area,
            db_number,
            start,
            len(data),
        )

    def _raise_link_aware(self, exc: RuntimeError) -> None:
        """按 snap7 连接态翻译 RuntimeError(内部方法)。

        在线 → :class:`DeviceError`(PLC 侧拒绝,不断线);
        断连 → :class:`OSError`(惰性重连)。
        """
        if self._is_connected():
            raise DeviceError("S7 错误:{}".format(exc), 0)
        raise OSError("S7 连接已断:{}".format(exc))

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
    """西门子 S7 客户端(封装 python-snap7 1.3,rack/slot 路由)。

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
        rack: int = S7_DEFAULT_RACK,
        slot: int = S7_DEFAULT_SLOT,
        port: int = S7_DEFAULT_PORT,
        dll_path: str = "",
    ) -> None:
        """初始化 S7 客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param rack: 机架号,S7_DEFAULT_RACK(0)
        :param slot: 槽位号,1200/1500 常用 1;300/400 的 CPU 常在 2
        :param port: ISO-on-TCP 端口,标准 102
        :param dll_path: snap7 原生库路径显式覆盖(32 位 Python 需自备
            32 位 snap7.dll;留空用 python-snap7 捆绑库,仅限 64 位)
        :raises ValueError: 参数非法
        """
        validate_endpoint(ip_address, port)
        super().__init__(ip_address, int(port))
        if not 0 <= int(rack) <= 7:
            raise ValueError("机架号必须在 0~7 之间,收到:{}".format(rack))
        if not 0 <= int(slot) <= 31:
            raise ValueError("槽位号必须在 0~31 之间,收到:{}".format(slot))
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
            raise ValueError("S7 不支持的数据类型:{}".format(data_type))
        parsed = parse_s7_address(address)
        if data_type is DataType.BOOL:
            if parsed.bit is None:
                raise ValueError(
                    "S7 按位读取需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)".format(address)
                )
        elif parsed.bit is not None:
            raise ValueError(
                "S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)".format(address)
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
                    "S7 按位写入需要位地址:{!r}(示例:M10.2 / DB1.DBX0.3)".format(address)
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
                "S7 位地址只能按 BOOL 读写:{!r}(数值请用字节起点地址)".format(address)
            )
        session.write_area(area_code(parsed.area), parsed.db_number, parsed.byte_index, data)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        """读 S7 String(头 2 字节 = 声明长/实际长,正文按声明长)。"""
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError("S7 字符串地址不带位号:{!r}".format(address))
        size = length + 2
        data = self._session().read_area(
            area_code(parsed.area), parsed.db_number, parsed.byte_index, size
        )
        if len(data) < 2:
            raise DeviceError("S7 String 响应过短:{}".format(len(data)), 0)
        actual = data[1]
        if actual <= 0 or actual > length:
            return ""
        return convert.decode_string(data[2:2 + actual], encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        """写 S7 String(声明长/实际长均按本次编码长度,建议 ≤ PLC 侧声明长)。"""
        parsed = parse_s7_address(address)
        if parsed.bit is not None:
            raise ValueError("S7 字符串地址不带位号:{!r}".format(address))
        encoded = convert.encode_string(value, len(value.encode(encoding)), encoding)
        header = bytes([len(encoded), len(encoded)])
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
            raise ValueError("S7 写入值超出类型范围:{}".format(value)) from exc
