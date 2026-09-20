"""异步客户端:同步核心的 A 前缀镜像。

命名规则:**异步类名 = 同步类名前加 A**,如 ``AModbusTcpClient``、
``AOmronFinsUdpClient``;方法签名与同步版同名同型,返回可 await。

实现方式:组合对应同步实例,所有协议调用经**单线程**
:class:`~concurrent.futures.ThreadPoolExecutor` 串行执行——协议
编解码只有一份代码,顺序与同步版的线程安全语义一致。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import Callable, List, Optional, Sequence, Tuple, Type, TypeVar, Union

from ..core.base_client import BaseClient
from ..core.constants import (
    DEFAULT_STRING_ENCODING,
    FINS_DEFAULT_PORT,
    KEYENCE_MC_DEFAULT_PORT,
    KV_DEFAULT_PORT,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MX_DEFAULT_LOGICAL_STATION,
    OPCUA_DEFAULT_PORT,
    READ_STRING_DEFAULT_LENGTH,
    SR_DEFAULT_PORT,
    SR_DEFAULT_SCAN_DWELL,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
    SERIAL_DEFAULT_STOP_BITS,
    TOYOPUC_DEFAULT_PORT,
)
from ..modbus import ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from ..modbus.modbus import _coerce_word_order
from ..opcua import OpcUaClient
from ..plc.keyence import (
    KeyenceHostLinkTcpClient,
    KeyenceHostLinkUdpClient,
    KeyenceMcTcpClient,
)
from ..plc.melsec import MelsecMcTcpClient, MelsecMcUdpClient, MelsecMxClient
from ..plc.toyopuc import ToyopucTcpClient, ToyopucUdpClient
from ..scanner import KeyenceSrClient
from ..plc.omron import OmronFinsTcpClient, OmronFinsUdpClient
from ..tag import Tag, TagTable
from ..types import McFrame, PrimitiveValue, SerialParity

_T = TypeVar("_T")
_A = TypeVar("_A", bound="ABaseClient")


class ABaseClient:
    """异步客户端基类。

    不要直接实例化,使用具体协议类,如::

        client = AModbusTcpClient("192.168.0.10", 502, 1)
        await client.connect()
        ok, value = await client.read_float("hr0")
        await client.close()

    线程安全:所有协议调用在同一个工作线程中串行执行,天然保序;
    多个协程共用同一客户端是安全的。
    """

    def __init__(self, sync_client: BaseClient) -> None:
        """由具体异步子类调用,传入已配置好的同步实例。"""
        self._sync = sync_client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="omniplc-aio")

    # ------------------------------------------------------------------
    # 执行机制
    # ------------------------------------------------------------------

    async def _run(self, operation: Callable[[], _T]) -> _T:
        """把同步操作投递到单线程 executor 执行(内部方法)。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, operation)

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """建立连接(语义同同步版 :meth:`BaseClient.connect`)。"""
        return await self._run(self._sync.connect)

    async def disconnect(self) -> bool:
        """断开连接。"""
        return await self._run(self._sync.disconnect)

    @property
    def connected(self) -> bool:
        """当前是否已连接(快照,不发报文)。"""
        return self._sync.connected

    @property
    def last_error(self) -> Optional[str]:
        """最近一次失败的错误描述。"""
        return self._sync.last_error

    @property
    def receive_timeout(self) -> float:
        """单次收发超时(秒)。"""
        return self._sync.receive_timeout

    @receive_timeout.setter
    def receive_timeout(self, seconds: float) -> None:
        self._sync.receive_timeout = seconds

    @property
    def connect_timeout(self) -> float:
        """连接超时(秒)。"""
        return self._sync.connect_timeout

    @connect_timeout.setter
    def connect_timeout(self, seconds: float) -> None:
        self._sync.connect_timeout = seconds

    @property
    def retries(self) -> int:
        """读重试次数。"""
        return self._sync.retries

    @retries.setter
    def retries(self, count: int) -> None:
        self._sync.retries = count

    @property
    def write_retries(self) -> int:
        """写重试次数(默认 0,防重复写入)。"""
        return self._sync.write_retries

    @write_retries.setter
    def write_retries(self, count: int) -> None:
        self._sync.write_retries = count

    # ------------------------------------------------------------------
    # 通用与类型化读写(签名与同步版一致)
    # ------------------------------------------------------------------

    async def read(self, address: str, data_type: str) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按数据类型读取一个点。"""
        return await self._run(lambda: self._sync.read(address, data_type))

    async def write(self, address: str, data_type: str, value: PrimitiveValue) -> bool:
        """按数据类型写入一个点。"""
        return await self._run(lambda: self._sync.write(address, data_type, value))

    async def read_many(
        self, addresses: Sequence[str], data_type: str
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取,逐点独立容错。"""
        return await self._run(lambda: self._sync.read_many(addresses, data_type))

    async def write_many(self, items: Sequence[Tuple[str, str, PrimitiveValue]]) -> List[bool]:
        """批量写入,逐点独立容错。"""
        return await self._run(lambda: self._sync.write_many(items))

    async def read_bool(self, address: str) -> Tuple[bool, Optional[bool]]:
        """读取布尔量(位)。"""
        return await self._run(lambda: self._sync.read_bool(address))

    async def read_short(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位有符号整数。"""
        return await self._run(lambda: self._sync.read_short(address))

    async def read_ushort(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 16 位无符号整数。"""
        return await self._run(lambda: self._sync.read_ushort(address))

    async def read_int(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位有符号整数。"""
        return await self._run(lambda: self._sync.read_int(address))

    async def read_uint(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 32 位无符号整数。"""
        return await self._run(lambda: self._sync.read_uint(address))

    async def read_long(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位有符号整数。"""
        return await self._run(lambda: self._sync.read_long(address))

    async def read_ulong(self, address: str) -> Tuple[bool, Optional[int]]:
        """读取 64 位无符号整数。"""
        return await self._run(lambda: self._sync.read_ulong(address))

    async def read_float(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 32 位浮点数。"""
        return await self._run(lambda: self._sync.read_float(address))

    async def read_double(self, address: str) -> Tuple[bool, Optional[float]]:
        """读取 64 位浮点数。"""
        return await self._run(lambda: self._sync.read_double(address))

    async def read_string(
        self,
        address: str,
        length: int = READ_STRING_DEFAULT_LENGTH,
        encoding: str = DEFAULT_STRING_ENCODING,
    ) -> Tuple[bool, Optional[str]]:
        """读取字符串。"""
        return await self._run(lambda: self._sync.read_string(address, length, encoding))

    async def write_bool(self, address: str, value: bool) -> bool:
        """写入布尔量(位)。"""
        return await self._run(lambda: self._sync.write_bool(address, value))

    async def write_short(self, address: str, value: int) -> bool:
        """写入 16 位有符号整数。"""
        return await self._run(lambda: self._sync.write_short(address, value))

    async def write_ushort(self, address: str, value: int) -> bool:
        """写入 16 位无符号整数。"""
        return await self._run(lambda: self._sync.write_ushort(address, value))

    async def write_int(self, address: str, value: int) -> bool:
        """写入 32 位有符号整数。"""
        return await self._run(lambda: self._sync.write_int(address, value))

    async def write_uint(self, address: str, value: int) -> bool:
        """写入 32 位无符号整数。"""
        return await self._run(lambda: self._sync.write_uint(address, value))

    async def write_long(self, address: str, value: int) -> bool:
        """写入 64 位有符号整数。"""
        return await self._run(lambda: self._sync.write_long(address, value))

    async def write_ulong(self, address: str, value: int) -> bool:
        """写入 64 位无符号整数。"""
        return await self._run(lambda: self._sync.write_ulong(address, value))

    async def write_float(self, address: str, value: float) -> bool:
        """写入 32 位浮点数。"""
        return await self._run(lambda: self._sync.write_float(address, value))

    async def write_double(self, address: str, value: float) -> bool:
        """写入 64 位浮点数。"""
        return await self._run(lambda: self._sync.write_double(address, value))

    async def write_string(
        self, address: str, value: str, encoding: str = DEFAULT_STRING_ENCODING
    ) -> bool:
        """写入字符串。"""
        return await self._run(lambda: self._sync.write_string(address, value, encoding))

    # ------------------------------------------------------------------
    # 点位表
    # ------------------------------------------------------------------

    def bind_tags(self, table: TagTable) -> None:
        """绑定点位表(与同步版共用)。"""
        self._sync.bind_tags(table)

    async def read_tag(self, tag: Union[str, Tag]) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按点位读取(自动应用缩放)。"""
        return await self._run(lambda: self._sync.read_tag(tag))

    async def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool:
        """按点位写入(自动逆缩放)。"""
        return await self._run(lambda: self._sync.write_tag(tag, value))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """释放 executor 线程(不断开连接,需要时先 await disconnect())。"""
        self._executor.shutdown(wait=False)

    async def __aenter__(self: _A) -> _A:
        """进入 async with 时自动连接,失败抛 ConnectionError。"""
        if not await self.connect():
            raise ConnectionError("连接失败:{}".format(self._sync.last_error))
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        await self.disconnect()
        self._executor.shutdown(wait=False)


class AModbusBaseClient(ABaseClient):
    """Modbus 异步基类(供走线子类继承)。"""

    @property
    def station(self) -> int:
        """Modbus 站号。"""
        return self._modbus.station

    @station.setter
    def station(self, value: int) -> None:
        self._modbus.station = value

    @property
    def word_order(self) -> str:
        """多寄存器字序(ABCD/CDAB/BADC/DCBA)。"""
        return self._modbus.word_order.value

    @word_order.setter
    def word_order(self, value: str) -> None:
        self._modbus.word_order = _coerce_word_order(value)

    async def write_mask_register(self, address: str, and_mask: int, or_mask: int) -> bool:
        """掩码写保持寄存器(FC22,语义同同步版)。"""
        return await self._run(
            lambda: self._modbus.write_mask_register(address, and_mask, or_mask)
        )

    @property
    def _modbus(self) -> ModbusBaseClient:
        """取 Modbus 同步实例(内部属性)。"""
        sync = self._sync
        if not isinstance(sync, ModbusBaseClient):
            raise TypeError("内部错误:sync 实例不是 ModbusBaseClient")
        return sync


class AModbusTcpClient(AModbusBaseClient):
    """Modbus TCP 异步客户端。"""

    def __init__(self, ip_address: str = "127.0.0.1", port: int = MODBUS_DEFAULT_PORT, station: int = MODBUS_DEFAULT_STATION) -> None:
        """参数同 :class:`omniplc.modbus.ModbusTcpClient`。"""
        super().__init__(ModbusTcpClient(ip_address, port, station))


class AModbusRtuClient(AModbusBaseClient):
    """Modbus RTU 异步客户端。"""

    def __init__(self, station: int = MODBUS_DEFAULT_STATION) -> None:
        """参数同 :class:`omniplc.modbus.ModbusRtuClient`。

        串口参数需在 connect 前配置::

            client = AModbusRtuClient(1)
            client.configure_serial("COM3", 9600)
        """
        super().__init__(ModbusRtuClient(station))

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(转发到同步实例,推荐 :class:`~omniplc.types.SerialParity` 枚举)。"""
        sync = self._sync
        if not isinstance(sync, ModbusRtuClient):
            raise TypeError("内部错误:sync 实例不是 ModbusRtuClient")
        sync.configure_serial(port_name, baud_rate, data_bits, stop_bits, parity)


class AMelsecMcTcpClient(ABaseClient):
    """三菱 MC 异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """参数同 :class:`omniplc.plc.melsec.MelsecMcTcpClient`。"""
        super().__init__(MelsecMcTcpClient(ip_address, port, frame, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        sync = self._sync
        if isinstance(sync, MelsecMcTcpClient):
            return sync.frame
        raise TypeError("内部错误")


class AMelsecMcUdpClient(ABaseClient):
    """三菱 MC 异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.3.39",
        port: int = MC_DEFAULT_PORT,
        frame: Union[McFrame, str] = McFrame.FRAME_3E,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """参数同 :class:`omniplc.plc.melsec.MelsecMcUdpClient`。"""
        super().__init__(MelsecMcUdpClient(ip_address, port, frame, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        sync = self._sync
        if isinstance(sync, MelsecMcUdpClient):
            return sync.frame
        raise TypeError("内部错误")


class AKeyenceMcTcpClient(ABaseClient):
    """基恩士 KV MC 协议兼容(SLMP)异步客户端(TCP,3E 帧)。"""

    def __init__(
        self,
        ip_address: str = "192.168.1.22",
        port: int = KEYENCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """参数同 :class:`omniplc.plc.keyence.KeyenceMcTcpClient`。"""
        super().__init__(KeyenceMcTcpClient(ip_address, port, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型,恒为 :attr:`McFrame.FRAME_3E`。"""
        sync = self._sync
        if isinstance(sync, KeyenceMcTcpClient):
            return sync.frame
        raise TypeError("内部错误")


class AMelsecMxClient(ABaseClient):
    """三菱 MX Component 异步客户端(Windows,COM)。

    所有 COM 调用在单工作线程中串行执行,天然满足 ActUtlType 的
    STA 线程模型。
    """

    def __init__(self, logical_station_number: int = MX_DEFAULT_LOGICAL_STATION) -> None:
        """参数同 :class:`omniplc.plc.melsec.MelsecMxClient`。"""
        super().__init__(MelsecMxClient(logical_station_number))

    @property
    def logical_station_number(self) -> int:
        """逻辑站号(转发同步实例)。"""
        sync = self._sync
        if not isinstance(sync, MelsecMxClient):
            raise TypeError("内部错误:sync 实例不是 MelsecMxClient")
        return sync.logical_station_number


class AKeyenceHostLinkTcpClient(ABaseClient):
    """基恩士 KV Host Link 异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """参数同 :class:`omniplc.plc.keyence.KeyenceHostLinkTcpClient`。"""
        super().__init__(KeyenceHostLinkTcpClient(ip_address, port))


class AKeyenceHostLinkUdpClient(ABaseClient):
    """基恩士 KV Host Link 异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """参数同 :class:`omniplc.plc.keyence.KeyenceHostLinkUdpClient`。"""
        super().__init__(KeyenceHostLinkUdpClient(ip_address, port))


class AKeyenceSrClient(ABaseClient):
    """基恩士 SR 扫码枪异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = SR_DEFAULT_PORT,
        scan_dwell: float = SR_DEFAULT_SCAN_DWELL,
    ) -> None:
        """参数同 :class:`omniplc.scanner.KeyenceSrClient`。"""
        super().__init__(KeyenceSrClient(ip_address, port, scan_dwell))

    def _scanner(self) -> KeyenceSrClient:
        """取扫码枪同步实例(内部属性)。"""
        sync = self._sync
        if not isinstance(sync, KeyenceSrClient):
            raise TypeError("内部错误:sync 实例不是 KeyenceSrClient")
        return sync

    async def scan(
        self, bank: Optional[int] = None, timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """触发一次扫码(语义同同步版 :meth:`KeyenceSrClient.scan`)。"""
        return await self._run(lambda: self._scanner().scan(bank, timeout))

    async def reset(self) -> bool:
        """清缓冲并复位扫码枪。"""
        return await self._run(self._scanner().reset)

    @property
    def scan_dwell(self) -> float:
        """扫码窗口时长(秒)。"""
        return self._scanner().scan_dwell

    @scan_dwell.setter
    def scan_dwell(self, seconds: float) -> None:
        self._scanner().scan_dwell = seconds


class AToyopucTcpClient(ABaseClient):
    """丰田 TOYOPUC 计算机链接异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """参数同 :class:`omniplc.plc.toyopuc.ToyopucTcpClient`。"""
        super().__init__(ToyopucTcpClient(ip_address, port))


class AToyopucUdpClient(ABaseClient):
    """丰田 TOYOPUC 计算机链接异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """参数同 :class:`omniplc.plc.toyopuc.ToyopucUdpClient`。"""
        super().__init__(ToyopucUdpClient(ip_address, port))


class AOpcUaClient(ABaseClient):
    """OPC-UA 异步客户端(封装 asyncua,opc.tcp 会话)。

    与其他 A 前缀镜像一致,所有调用经单线程 executor 串行执行;
    asyncua.sync 内部亦有独立事件循环线程,双层串行保序。
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = OPCUA_DEFAULT_PORT,
        path: str = "",
        endpoint: str = "",
    ) -> None:
        """参数同 :class:`omniplc.opcua.OpcUaClient`。"""
        super().__init__(OpcUaClient(ip_address, port, path, endpoint))

    @property
    def endpoint(self) -> str:
        """opc.tcp 端点 URL(转发同步实例)。"""
        sync = self._sync
        if not isinstance(sync, OpcUaClient):
            raise TypeError("内部错误:sync 实例不是 OpcUaClient")
        return sync.endpoint


class AOmronFinsTcpClient(ABaseClient):
    """欧姆龙 FINS/TCP 异步客户端。"""

    def __init__(
        self,
        ip_address: str = "192.168.250.1",
        port: int = FINS_DEFAULT_PORT,
        local_node: int = 0,
    ) -> None:
        """参数同 :class:`omniplc.plc.omron.OmronFinsTcpClient`。"""
        super().__init__(OmronFinsTcpClient(ip_address, port, local_node))

    @property
    def local_node(self) -> int:
        """本地节点号(自动分配时在连接后可用,转发同步实例)。"""
        sync = self._sync
        if not isinstance(sync, OmronFinsTcpClient):
            raise TypeError("内部错误:sync 实例不是 OmronFinsTcpClient")
        return sync.local_node


class AOmronFinsUdpClient(ABaseClient):
    """欧姆龙 FINS/UDP 异步客户端。"""

    def __init__(self, ip_address: str = "192.168.250.1", port: int = FINS_DEFAULT_PORT) -> None:
        """参数同 :class:`omniplc.plc.omron.OmronFinsUdpClient`。"""
        super().__init__(OmronFinsUdpClient(ip_address, port))
