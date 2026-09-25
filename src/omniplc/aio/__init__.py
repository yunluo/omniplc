"""异步客户端:同步核心的 A 前缀镜像。

命名规则:**异步类名 = 同步类名前加 A**,如 ``AModbusTcpClient``、
``AOmronFinsUdpClient``;方法签名与同步版同名同型,返回可 await。

实现方式:组合对应同步实例,所有协议调用经**单线程**
:class:`~concurrent.futures.ThreadPoolExecutor` 串行执行——协议
编解码只有一份代码,顺序与同步版的线程安全语义一致。

**这不是原生 asyncio 协议栈**(预期管理,详见 README「异步」节):

- 每个 ``await`` 把同步调用投递到该客户端自己的单工作线程;I/O 期间事件
  循环不被阻塞,但**同一客户端仍串行**(并发收益来自跨设备重叠等待);
- 同步属性(``connected``/``last_error*``/``stats``/``receive_timeout``
  读写)直读同步实例,不发报文也不切线程;
- **无 asyncio 原生取消**:``asyncio.wait_for`` 超时只是放弃等待,已提交的
  写事务仍会在工作线程里跑完(写动作不可回滚);
- :meth:`ABaseClient.close` 关闸后**排空**已提交任务(不锯断在途事务),
  再释放线程。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type, TypeVar, Union

from ..core.base_client import BaseClient, ClientStats
from ..core.errors import ErrorCategory
from ..cnc import MTConnectClient
from ..core.constants import (
    AB_EIP_DEFAULT_PORT,
    AB_EIP_DEFAULT_SLOT,
    ADS_DEFAULT_ADS_PORT,
    DEFAULT_STRING_ENCODING,
    FINS_DEFAULT_PORT,
    INOVANCE_MC_DEFAULT_PORT,
    INOVANCE_SERIAL_DEFAULT_STOP_BITS,
    KEYENCE_MC_DEFAULT_PORT,
    KV_DEFAULT_PORT,
    MC_1C_DEFAULT_MESSAGE_WAIT,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MC_DEFAULT_PORT,
    MC_SERIAL_DEFAULT_MODULE_IO,
    MC_SERIAL_DEFAULT_MODULE_STATION,
    MC_SERIAL_DEFAULT_NETWORK_NUMBER,
    MC_SERIAL_DEFAULT_PC_NUMBER,
    MC_SERIAL_DEFAULT_SELF_STATION,
    MC_SERIAL_DEFAULT_STATION,
    MEWTOCOL_DEFAULT_PORT,
    MEWTOCOL_DEFAULT_STATION,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    MX_DEFAULT_LOGICAL_STATION,
    OPCUA_DEFAULT_PORT,
    OPCUA_DEFAULT_SAMPLING_INTERVAL_MS,
    OPEN_TCP_DEFAULT_DELIMITER,
    OPEN_TCP_DEFAULT_ENCODING,
    OPEN_TCP_DEFAULT_PORT,
    OPEN_TCP_MAX_FRAME,
    MTCONNECT_DEFAULT_PORT,
    S7_DEFAULT_PORT,
    S7_DEFAULT_RACK,
    S7_DEFAULT_SLOT,
    PANASONIC_MC_DEFAULT_PORT,
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
from ..opentcp import OpenTcpClient
from ..plc.ab import AllenBradleyEthIpClient
from ..plc.beckhoff import BeckhoffAdsClient
from ..plc.inovance import InovanceMcTcpClient, InovanceRtuClient, InovanceTcpClient
from ..plc.siemens import SiemensS7Client
from ..opcua import OpcUaClient, OpcUaSubscription
from ..plc.panasonic import (
    PanasonicMcTcpClient,
    PanasonicMewtocolTcpClient,
    PanasonicMewtocolUdpClient,
)
from ..plc.keyence import (
    KeyenceHostLinkTcpClient,
    KeyenceHostLinkUdpClient,
    KeyenceMcTcpClient,
    KeyenceMcUdpClient,
)
from ..plc.melsec import (
    MelsecMcSerialClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
)
from ..plc.toyopuc import ToyopucTcpClient, ToyopucUdpClient
from ..scanner import KeyenceSrClient
from ..plc.omron import OmronCipClient, OmronFinsTcpClient, OmronFinsUdpClient
from ..tag import Tag, TagTable
from ..types import DataType, McFrame, PrimitiveValue, SerialParity

_T = TypeVar("_T")
_A = TypeVar("_A", bound="ABaseClient")
_SyncT = TypeVar("_SyncT", bound="BaseClient")

# 本包公开面仅异步基类与 A* 客户端:同步客户端、端口等常量、内部助手
# (如 _coerce_word_order)只是实现依赖,不进 __all__,避免
# ``from omniplc.aio import *`` 把同步类与内部名倒入使用者命名空间。
__all__ = [
    # ---- 客户端基类 ----
    "ABaseClient",
    # ---- Modbus 客户端 ----
    "AModbusBaseClient",
    "AModbusTcpClient",
    "AModbusRtuClient",
    # ---- 三菱 MC 客户端 ----
    "AMelsecMcTcpClient",
    "AMelsecMcUdpClient",
    "AMelsecMcSerialClient",
    "AMelsecMxClient",
    # ---- 基恩士 KV Host Link / MC 兼容客户端 ----
    "AKeyenceHostLinkTcpClient",
    "AKeyenceHostLinkUdpClient",
    "AKeyenceMcTcpClient",
    "AKeyenceMcUdpClient",
    # ---- 基恩士 SR 扫码枪 ----
    "AKeyenceSrClient",
    # ---- 欧姆龙 FINS / CIP 客户端 ----
    "AOmronFinsTcpClient",
    "AOmronFinsUdpClient",
    "AOmronCipClient",
    # ---- 汇川 H3U/H5U 客户端 ----
    "AInovanceTcpClient",
    "AInovanceRtuClient",
    "AInovanceMcTcpClient",
    # ---- 松下 FP 系列客户端 ----
    "APanasonicMcTcpClient",
    "APanasonicMewtocolTcpClient",
    "APanasonicMewtocolUdpClient",
    # ---- 丰田 TOYOPUC 客户端 ----
    "AToyopucTcpClient",
    "AToyopucUdpClient",
    # ---- 罗克韦尔 AB EtherNet/IP 客户端 ----
    "AAllenBradleyEthIpClient",
    # ---- 倍福 TwinCAT ADS 客户端 ----
    "ABeckhoffAdsClient",
    # ---- 西门子 S7 客户端 ----
    "ASiemensS7Client",
    # ---- 通用自定义 TCP 客户端 ----
    "AOpenTcpClient",
    # ---- OPC-UA 客户端 ----
    "AOpcUaClient",
    # ---- CNC 机床数采客户端 ----
    "AMTConnectClient",
]


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
        """由具体异步子类调用,传入已配置好的同步实例。

        :param sync_client: 已配置好的同步客户端实例(协议驱动类见各 A* 子类)
        """
        self._sync = sync_client
        self._executor: Optional[ThreadPoolExecutor] = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="omniplc-aio"
        )

    # ------------------------------------------------------------------
    # 执行机制
    # ------------------------------------------------------------------

    def _typed(self, expected: Type[_SyncT]) -> _SyncT:
        """取同步实例并断言其驱动类型(内部方法,驱动专属转发的公共守卫)。

        各 A* 客户端把 ``self._sync`` 收窄为具体驱动类型后转发驱动特有
        方法/属性;实例类型由构造保证,断言失败即内部组装错误。
        """
        sync = self._sync
        if not isinstance(sync, expected):
            raise TypeError(f"内部错误:sync 实例不是 {expected.__name__}")
        return sync

    async def _run(self, operation: Callable[[], _T]) -> _T:
        """把同步操作投递到单线程 executor 执行(内部方法)。"""
        self._ensure_open()
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
    def last_error_category(self) -> Optional[ErrorCategory]:
        """最近一次失败的分类(转发同步实例)。"""
        return self._sync.last_error_category

    @property
    def last_error_code(self) -> Optional[int]:
        """最近一次失败的原始错误码(转发同步实例)。"""
        return self._sync.last_error_code

    @property
    def stats(self) -> ClientStats:
        """连接健康统计快照(转发同步实例,字段说明见同步版)。"""
        return self._sync.stats

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

    @property
    def reconnect_backoff(self) -> bool:
        """连接失败后的指数退避门控(转发同步实例)。"""
        return self._sync.reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, enabled: bool) -> None:
        self._sync.reconnect_backoff = enabled

    @property
    def next_connect_in(self) -> Optional[float]:
        """距下次允许连接的剩余秒数;None = 可立即连接(转发同步实例)。"""
        return self._sync.next_connect_in

    # ------------------------------------------------------------------
    # 通用与类型化读写(签名与同步版一致)
    # ------------------------------------------------------------------

    async def read(
        self, address: str, data_type: Union[DataType, str]
    ) -> Tuple[bool, Optional[PrimitiveValue]]:
        """按数据类型读取一个点。"""
        return await self._run(lambda: self._sync.read(address, data_type))

    async def write(
        self, address: str, data_type: Union[DataType, str], value: PrimitiveValue
    ) -> bool:
        """按数据类型写入一个点。"""
        return await self._run(lambda: self._sync.write(address, data_type, value))

    async def read_many(
        self, addresses: Sequence[str], data_type: Union[DataType, str]
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读取,逐点独立容错。"""
        return await self._run(lambda: self._sync.read_many(addresses, data_type))

    async def write_many(
        self, items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]]
    ) -> List[bool]:
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

    def _ensure_open(self) -> None:
        """已关闭客户端不可再执行协议操作(内部方法)。"""
        if self._executor is None:
            raise RuntimeError("客户端已关闭,无法再执行协议操作")

    async def close(self) -> None:
        """断开连接并释放单工作线程(幂等;关闭后客户端不可复用)。

        关闭顺序与语义:

        1. **立刻关闸**:``self._executor`` 先置 ``None``,此后任何协议调用
           (含 :meth:`disconnect` / :meth:`close`)抛 ``RuntimeError``——
           不再向工作线程排队,避免任务在关闭返回之后才执行(关闭后仍写 PLC)。
        2. **排空**:已提交的任务照常跑完(单线程池 FIFO,各任务自带事务超时
           上界),最后排队的是同步侧 ``disconnect``(释放 socket/会话);
           在途事务**不会被锯断**。
        3. **释放线程**:``shutdown(wait=True)`` 在事件循环的默认执行器里等待,
           不阻塞事件循环;返回时保证没有任务会在关闭之后落线。

        与 :meth:`disconnect` 的区别:disconnect 只断同步侧、客户端仍可用;
        close 是彻底收尾(不可复用)。
        """
        if self._executor is None:
            return
        executor = self._executor
        self._executor = None  # 关闸:新调用立即 RuntimeError
        loop = asyncio.get_running_loop()
        try:
            # 绕过闸门直接投递:断开排在所有已提交任务之后执行
            await loop.run_in_executor(executor, self._sync.disconnect)
        except Exception:
            pass  # 尽力断开,失败不阻断释放
        finally:
            try:
                # wait=True 等线程排空;放在默认执行器里等 → 不阻塞事件循环
                await loop.run_in_executor(None, executor.shutdown)
            except CancelledError:
                # close 自身被取消:退化为同步等待,线程不悬在关闭之后
                # (CancelledError 即 asyncio.CancelledError,3.7~3.12 同一类)
                executor.shutdown(wait=True)
                raise

    async def __aenter__(self: _A) -> _A:
        """进入 async with 时自动连接,失败抛 ConnectionError。"""
        if not await self.connect():
            raise ConnectionError(f"连接失败:{self._sync.last_error}")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_val: Optional[BaseException] = None,
        exc_tb: Optional[TracebackType] = None,
    ) -> None:
        await self.close()


class AModbusBaseClient(ABaseClient):
    """Modbus 异步基类(供走线子类继承)。"""

    @property
    def station(self) -> int:
        """Modbus 站号(构造期定,转发同步实例)。"""
        return self._modbus.station

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

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """混类型批量读(按 FC+类型分组合并连续地址,语义同同步版)。"""
        sync = self._typed(ModbusBaseClient)  # type: ignore[type-abstract]
        return await self._run(lambda: sync.read_batch(items))

    async def write_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> Tuple[bool, Optional[List[bool]]]:
        """混类型批量写(按 FC+类型分组合并连续地址,语义同同步版)。"""
        sync = self._typed(ModbusBaseClient)  # type: ignore[type-abstract]
        return await self._run(lambda: sync.write_batch(items))

    async def read_write_registers(
        self,
        read_address: str,
        read_count: int,
        write_address: str,
        values: Sequence[int],
    ) -> Tuple[bool, Optional[List[int]]]:
        """单事务「先写后读」多寄存器(FC23,语义同同步版)。"""
        sync = self._typed(ModbusBaseClient)  # type: ignore[type-abstract]
        return await self._run(
            lambda: sync.read_write_registers(
                read_address, read_count, write_address, values
            )
        )

    async def read_device_id(
        self, level: Union[str, int] = "basic"
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读设备标识(FC43/14 流式访问,语义同同步版)。"""
        sync = self._typed(ModbusBaseClient)  # type: ignore[type-abstract]
        return await self._run(lambda: sync.read_device_id(level))

    async def read_device_object(self, object_id: int) -> Tuple[bool, Optional[bytes]]:
        """读单个设备标识对象(FC43/14 个体访问,语义同同步版)。"""
        sync = self._typed(ModbusBaseClient)  # type: ignore[type-abstract]
        return await self._run(lambda: sync.read_device_object(object_id))

    @property
    def _modbus(self) -> ModbusBaseClient:
        """取 Modbus 同步实例(内部属性)。"""
        return self._typed(ModbusBaseClient)  # type: ignore[type-abstract]


class AModbusTcpClient(AModbusBaseClient):
    """Modbus TCP 异步客户端。"""

    def __init__(self, ip_address: str = "127.0.0.1", port: int = MODBUS_DEFAULT_PORT, station: int = MODBUS_DEFAULT_STATION) -> None:
        """初始化 Modbus TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,默认 502
        :param station: 站号(Unit ID),默认 1
        :raises ValueError: 参数非法
        """
        super().__init__(ModbusTcpClient(ip_address, port, station))


class AModbusRtuClient(AModbusBaseClient):
    """Modbus RTU 异步客户端。"""

    def __init__(self, station: int = MODBUS_DEFAULT_STATION) -> None:
        """初始化 Modbus RTU 异步客户端。

        :param station: 站号,默认 1
        :raises ValueError: 站号非法

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
        sync = self._typed(ModbusRtuClient)
        sync.configure_serial(port_name, baud_rate, data_bits, stop_bits, parity)


class AInovanceTcpClient(AModbusBaseClient):
    """汇川 H3U/H5U Modbus TCP 异步客户端。

    继承 AModbusBaseClient:station/word_order 属性与
    write_mask_register 原子掩码写与同步侧继承结构对称。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.88",
        port: int = MODBUS_DEFAULT_PORT,
        station: int = MODBUS_DEFAULT_STATION,
    ) -> None:
        """初始化汇川 TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名(Modbus TCP 从站默认开启,
            示例默认取 Easy 系列出厂 IP,实际以 AutoShop 以太网配置为准)
        :param port: 端口,默认 502(汇川从站服务默认开启且多数机型不可改)
        :param station: 从站站号(Unit ID),默认 1
        :raises ValueError: 参数非法
        """
        super().__init__(InovanceTcpClient(ip_address, port, station))


class AInovanceRtuClient(AModbusBaseClient):
    """汇川 H3U/H5U Modbus RTU 异步客户端(串口)。"""

    def __init__(self, station: int = MODBUS_DEFAULT_STATION) -> None:
        """初始化汇川 RTU 异步客户端。

        :param station: 站号,默认 1
        :raises ValueError: 站号非法
        """
        super().__init__(InovanceRtuClient(station))

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = INOVANCE_SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(汇川缺省 9600-8N2,转发到同步实例)。"""
        sync = self._typed(InovanceRtuClient)
        sync.configure_serial(port_name, baud_rate, data_bits, stop_bits, parity)


class APanasonicMewtocolTcpClient(ABaseClient):
    """松下 MEWTOCOL 异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MEWTOCOL_DEFAULT_PORT,
        station: int = MEWTOCOL_DEFAULT_STATION,
    ) -> None:
        """初始化 MEWTOCOL TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(以太网 MEWTOCOL 默认 1024,以模块设置为准)
        :param station: 站号(1~99,编程口直连场景 0xEE)
        :raises ValueError: 参数非法
        """
        super().__init__(PanasonicMewtocolTcpClient(ip_address, port, station))

    @property
    def station(self) -> int:
        """当前站号(转发同步实例)。"""
        sync = self._typed(PanasonicMewtocolTcpClient)
        return sync.station


class APanasonicMewtocolUdpClient(ABaseClient):
    """松下 MEWTOCOL 异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MEWTOCOL_DEFAULT_PORT,
        station: int = MEWTOCOL_DEFAULT_STATION,
    ) -> None:
        """初始化 MEWTOCOL UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(以太网 MEWTOCOL 默认 1024,以模块设置为准)
        :param station: 站号(1~99,编程口直连场景 0xEE)
        :raises ValueError: 参数非法
        """
        super().__init__(PanasonicMewtocolUdpClient(ip_address, port, station))

    @property
    def station(self) -> int:
        """当前站号(转发同步实例)。"""
        sync = self._typed(PanasonicMewtocolUdpClient)
        return sync.station


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
        """初始化 MC TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :raises ValueError: 参数非法
        """
        super().__init__(MelsecMcTcpClient(ip_address, port, frame, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._typed(MelsecMcTcpClient).frame

    @property
    def network_number(self) -> int:
        """当前网络编号(转发同步实例)。"""
        return self._typed(MelsecMcTcpClient).network_number

    @property
    def pc_number(self) -> int:
        """当前 PC 编号(转发同步实例)。"""
        return self._typed(MelsecMcTcpClient).pc_number

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多块批量读取(0406,单事务;语义同同步版)。"""
        sync = self._typed(MelsecMcTcpClient)
        return await self._run(lambda: sync.read_batch(items))


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
        """初始化 MC UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口(MELSEC 以太网模块常用 2000,调试器场景 6000)
        :param frame: 帧型,推荐 :class:`omniplc.types.McFrame` 枚举
            (``McFrame.FRAME_3E``/``FRAME_4E`` 为 QnA 兼容,
            ``FRAME_1E`` 为 A 兼容);也兼容 ``"3E"``/``"4E"``/``"1E"`` 字符串
        :param network_number: 网络编号(仅 3E/4E 使用)
        :param pc_number: PC 编号(仅 3E/4E 使用;1E 帧语义为站号)
        :raises ValueError: 参数非法
        """
        super().__init__(MelsecMcUdpClient(ip_address, port, frame, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._typed(MelsecMcUdpClient).frame

    @property
    def network_number(self) -> int:
        """当前网络编号(转发同步实例)。"""
        return self._typed(MelsecMcUdpClient).network_number

    @property
    def pc_number(self) -> int:
        """当前 PC 编号(转发同步实例)。"""
        return self._typed(MelsecMcUdpClient).pc_number

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多块批量读取(0406,单事务;语义同同步版)。"""
        sync = self._typed(MelsecMcUdpClient)
        return await self._run(lambda: sync.read_batch(items))


class AInovanceMcTcpClient(AMelsecMcTcpClient):
    """汇川 MC 协议兼容异步客户端(TCP,3E 帧)。

    继承 AMelsecMcTcpClient:frame 属性与 read_batch(0406 多块批量读)
    与同步侧继承结构对称,frame 按 MelsecMcTcpClient 收窄对汇川实例同样
    成立(InovanceMcTcpClient 是其子类)。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.88",
        port: int = INOVANCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化汇川 MC 兼容异步客户端。

        :param ip_address: PLC 的 IP 或主机名(H5U/Easy 出厂默认 192.168.1.88)
        :param port: 端口,与 AutoShop"MC配置"中设置的端口号一致
            (手册未规定出厂默认;范围 1025~4999、5010~49151,
            不可用 502/9600/44818/2222/34980/12939/12940)
        :param network_number: 网络编号(按三菱 MC 默认 0)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        ABaseClient.__init__(self, InovanceMcTcpClient(ip_address, port, network_number, pc_number))


class APanasonicMcTcpClient(AMelsecMcTcpClient):
    """松下 MC 协议兼容异步客户端(TCP,3E 帧)。

    继承 AMelsecMcTcpClient:frame 属性与 read_batch(0406 多块批量读)
    与同步侧继承结构对称。
    """

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = PANASONIC_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化松下 MC 兼容异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,与 PLC 以太网模块 MC 协议配置一致
            (手册未规定出厂默认;默认 2000 为三菱惯例占位)
        :param network_number: 网络编号(按三菱 MC 默认 0)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        ABaseClient.__init__(self, PanasonicMcTcpClient(ip_address, port, network_number, pc_number))


class AMelsecMcSerialClient(ABaseClient):
    """三菱 MC 异步客户端(串口,1C/3C/4C 帧,需 pyserial)。

    串口参数需在 connect 前配置::

        client = AMelsecMcSerialClient(frame=McFrame.FRAME_4C)
        client.configure_serial("COM3", 9600)
        await client.connect()
    """

    def __init__(
        self,
        frame: Union[McFrame, str] = McFrame.FRAME_3C,
        station_number: int = MC_SERIAL_DEFAULT_STATION,
        network_number: int = MC_SERIAL_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_SERIAL_DEFAULT_PC_NUMBER,
        self_station_number: int = MC_SERIAL_DEFAULT_SELF_STATION,
        module_io: int = MC_SERIAL_DEFAULT_MODULE_IO,
        module_station: int = MC_SERIAL_DEFAULT_MODULE_STATION,
        message_wait: int = MC_1C_DEFAULT_MESSAGE_WAIT,
    ) -> None:
        """初始化 MC 串口异步客户端。

        :param frame: 帧型,``McFrame.FRAME_1C``(A 兼容 ASCII 格式 4)、
            ``McFrame.FRAME_3C``(QnA 兼容 ASCII 格式 4)或
            ``McFrame.FRAME_4C``(QnA 扩展二进制格式 5);
            也兼容 ``"1C"``/``"3C"``/``"4C"`` 字符串
        :param station_number: 站号 0~31(0 = 连接站/主机站)
        :param network_number: 网络编号(0 = 本网络;仅 3C/4C 使用)
        :param pc_number: PC 编号(0~3 或 0xFF;0xFF = 连接站 CPU)
        :param self_station_number: 本站号(m:n 多点连接时外部设备自身站号;仅 3C/4C 使用)
        :param module_io: 请求目标模块 I/O 编号(4C 帧使用,CPU 直连 0x03FF)
        :param module_station: 请求目标模块局号(4C 帧使用,CPU 直连 0)
        :param message_wait: 消息等待(仅 1C 帧,0~15,单位 10ms)
        :raises ValueError: 参数非法
        """
        super().__init__(
            MelsecMcSerialClient(
                frame,
                station_number,
                network_number,
                pc_number,
                self_station_number,
                module_io,
                module_station,
                message_wait,
            )
        )

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(转发到同步实例,推荐 :class:`~omniplc.types.SerialParity` 枚举)。"""
        sync = self._typed(MelsecMcSerialClient)
        sync.configure_serial(port_name, baud_rate, data_bits, stop_bits, parity)

    @property
    def frame(self) -> McFrame:
        """当前帧型(:class:`omniplc.types.McFrame` 枚举)。"""
        return self._typed(MelsecMcSerialClient).frame

    @property
    def station_number(self) -> int:
        """当前站号(转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.station_number

    @property
    def pc_number(self) -> int:
        """当前 PC 编号(转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.pc_number

    @property
    def module_io(self) -> int:
        """请求目标模块 I/O 编号(仅 4C 帧,转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.module_io

    @property
    def network_number(self) -> int:
        """当前网络编号(转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.network_number

    @property
    def self_station_number(self) -> int:
        """本站号(m:n 多点连接时外部设备自身站号,转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.self_station_number

    @property
    def module_station(self) -> int:
        """请求目标模块局号(仅 4C 帧,转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.module_station

    @property
    def message_wait(self) -> int:
        """消息等待(仅 1C 帧,0~15,单位 10ms,转发同步实例)。"""
        sync = self._typed(MelsecMcSerialClient)
        return sync.message_wait

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多块批量读取(3C/4C 帧不支持,抛 ValueError;镜像契约一致性)。"""
        sync = self._typed(MelsecMcSerialClient)
        return await self._run(lambda: sync.read_batch(items))


class AKeyenceMcTcpClient(AMelsecMcTcpClient):
    """基恩士 KV MC 协议兼容(SLMP)异步客户端(TCP,3E 帧)。

    继承 AMelsecMcTcpClient:read_batch(0406 多块批量读)与同步侧
    继承结构对称(frame 按基类收窄对基恩士实例同样成立)。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.22",
        port: int = KEYENCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 KV MC 兼容异步客户端。

        :param ip_address: PLC 的 IP 或主机名(KV 以太网单元设置中配置)
        :param port: 端口(KV SLMP 兼容默认 5000,以单元设置为准)
        :param network_number: 网络编号(KV 通常按默认 0 应答)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        ABaseClient.__init__(self, KeyenceMcTcpClient(ip_address, port, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型,恒为 :attr:`McFrame.FRAME_3E`。"""
        return self._typed(KeyenceMcTcpClient).frame


class AKeyenceMcUdpClient(AMelsecMcUdpClient):
    """基恩士 KV MC 协议兼容(SLMP)异步客户端(UDP,3E 帧)。

    继承 AMelsecMcUdpClient:read_batch(0406 多块批量读)与同步侧
    继承结构对称(frame 按 MelsecMcUdpClient 收窄对基恩士实例同样成立)。
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.22",
        port: int = KEYENCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化 KV MC 兼容 UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名(KV 以太网单元设置中配置)
        :param port: 端口(KV SLMP 兼容默认 5000,以单元设置为准)
        :param network_number: 网络编号(KV 通常按默认 0 应答)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        ABaseClient.__init__(self, KeyenceMcUdpClient(ip_address, port, network_number, pc_number))

    @property
    def frame(self) -> McFrame:
        """当前帧型,恒为 :attr:`McFrame.FRAME_3E`。"""
        return self._typed(KeyenceMcUdpClient).frame


class AMelsecMxClient(ABaseClient):
    """三菱 MX Component 异步客户端(Windows,COM)。

    所有 COM 调用在单工作线程中串行执行,天然满足 ActUtlType 的
    STA 线程模型。
    """

    def __init__(self, logical_station_number: int = MX_DEFAULT_LOGICAL_STATION) -> None:
        """初始化 MX Component 异步客户端。

        :param logical_station_number: 通信设置实用程序中配置的逻辑站号(0~1023)
        :raises ValueError: 逻辑站号越界
        """
        super().__init__(MelsecMxClient(logical_station_number))

    @property
    def logical_station_number(self) -> int:
        """逻辑站号(转发同步实例)。"""
        sync = self._typed(MelsecMxClient)
        return sync.logical_station_number

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """随机批量读取(ReadDeviceRandom 单事务;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(lambda: sync.read_batch(items))

    async def write_batch(
        self, items: Sequence[Tuple[str, PrimitiveValue]]
    ) -> bool:
        """随机批量写入(WriteDeviceRandom 单事务;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(lambda: sync.write_batch(items))

    async def get_cpu_type(self) -> Tuple[bool, Optional[Tuple[str, int]]]:
        """读取 CPU 型号字符串与型号代码(GetCpuType;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(lambda: sync.get_cpu_type())

    async def get_clock(self) -> Tuple[bool, Optional[Dict[str, int]]]:
        """读取 PLC CPU 时钟(GetClockData;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(lambda: sync.get_clock())

    async def set_clock(
        self,
        year: int,
        month: int,
        day: int,
        hour: int = 0,
        minute: int = 0,
        second: int = 0,
        day_of_week: int = 0,
    ) -> bool:
        """写入 PLC CPU 时钟(SetClockData;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(
            lambda: sync.set_clock(
                year, month, day, hour, minute, second, day_of_week
            )
        )

    async def get_error_message(self, code: int) -> Tuple[bool, Optional[str]]:
        """出错代码转官方文本(GetErrorMessage;语义同同步版)。"""
        sync = self._typed(MelsecMxClient)
        return await self._run(lambda: sync.get_error_message(code))


class AKeyenceHostLinkTcpClient(ABaseClient):
    """基恩士 KV Host Link 异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """初始化 KV Host Link TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: Host Link 端口,默认 8000
        :raises ValueError: 参数非法
        """
        super().__init__(KeyenceHostLinkTcpClient(ip_address, port))


class AKeyenceHostLinkUdpClient(ABaseClient):
    """基恩士 KV Host Link 异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = KV_DEFAULT_PORT,
    ) -> None:
        """初始化 KV Host Link UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: Host Link 端口,默认 8000
        :raises ValueError: 参数非法
        """
        super().__init__(KeyenceHostLinkUdpClient(ip_address, port))


class AKeyenceSrClient(ABaseClient):
    """基恩士 SR 扫码枪异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = SR_DEFAULT_PORT,
        scan_dwell: float = SR_DEFAULT_SCAN_DWELL,
    ) -> None:
        """初始化 SR 扫码枪异步客户端。

        :param ip_address: 扫码枪 IP 或主机名
        :param port: TCP 端口,默认 9004
        :param scan_dwell: 扫码窗口时长(秒),LON 到 LOFF 的等待时间
        :raises ValueError: 参数非法
        """
        super().__init__(KeyenceSrClient(ip_address, port, scan_dwell))

    def _scanner(self) -> KeyenceSrClient:
        """取扫码枪同步实例(内部属性)。"""
        return self._typed(KeyenceSrClient)

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
        """扫码窗口时长(秒),构造期定(转发同步实例)。"""
        return self._scanner().scan_dwell


class AToyopucTcpClient(ABaseClient):
    """丰田 TOYOPUC 计算机链接异步客户端(TCP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """初始化 TOYOPUC TCP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 计算机链接端口,默认 1025
        :raises ValueError: 参数非法
        """
        super().__init__(ToyopucTcpClient(ip_address, port))


class AToyopucUdpClient(ABaseClient):
    """丰田 TOYOPUC 计算机链接异步客户端(UDP)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = TOYOPUC_DEFAULT_PORT,
    ) -> None:
        """初始化 TOYOPUC UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 计算机链接端口,默认 1025
        :raises ValueError: 参数非法
        """
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
        """初始化 OPC-UA 异步客户端。

        :param ip_address: 服务器 IP 或主机名
        :param port: 端口,标准默认 4840
        :param path: 端点 URL 路径(可空,如 ``"UA/Server"``)
        :param endpoint: 完整端点 URL 显式覆盖(以 ``opc.tcp://`` 开头;
            用于服务器发现返回的完整 URL,设置后忽略 ip/port/path)
        :raises ValueError: 参数非法
        """
        super().__init__(OpcUaClient(ip_address, port, path, endpoint))

    @property
    def endpoint(self) -> str:
        """opc.tcp 端点 URL(转发同步实例)。"""
        sync = self._typed(OpcUaClient)
        return sync.endpoint

    @property
    def active_subscriptions(self) -> Dict[int, OpcUaSubscription]:
        """活跃订阅快照(``subscription_id`` → :class:`OpcUaSubscription`);只读。"""
        sync = self._typed(OpcUaClient)
        return sync.active_subscriptions

    async def browse(
        self,
        node_text: str = "Root",
        *,
        recursive: bool = True,
        max_depth: Optional[int] = None,
    ) -> Tuple[bool, Optional[dict]]:
        """枚举节点树(语义同同步版 :meth:`OpcUaClient.browse`)。"""
        sync = self._typed(OpcUaClient)
        return await self._run(
            lambda: sync.browse(node_text, recursive=recursive, max_depth=max_depth)
        )

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多节点批量读取(UA Read 单请求;语义同同步版)。"""
        sync = self._typed(OpcUaClient)
        return await self._run(lambda: sync.read_batch(items))

    async def subscribe_data_change(
        self,
        node_text: str,
        on_change: Callable[[Any, str, Optional[float]], None],
        *,
        sampling_interval_ms: int = OPCUA_DEFAULT_SAMPLING_INTERVAL_MS,
    ) -> Tuple[bool, Optional[OpcUaSubscription]]:
        """订阅节点值变化(DataChange)。

        用户回调 ``on_change`` 是普通同步函数,但实际在 asyncua 内部线程触发;
        本方法捕获当前 aio loop 后,用 ``loop.call_soon_threadsafe`` 把
        调用调度到 aio loop 线程执行——回调内可安全做 asyncio 操作
        (queue.put_nowait、asyncio.Event.set 等)。

        :return: ``(成功, 订阅句柄)``;同步实例的句柄对象(同一引用)
        """
        sync = self._typed(OpcUaClient)
        loop = asyncio.get_running_loop()

        def _bridge(value: Any, node_id_str: str, ts: Optional[float]) -> None:
            try:
                loop.call_soon_threadsafe(on_change, value, node_id_str, ts)
            except RuntimeError:
                # loop 已关闭(典型:客户端 disconnect 后)
                pass

        return await self._run(
            lambda: sync.subscribe_data_change(
                node_text, _bridge, sampling_interval_ms=sampling_interval_ms
            )
        )

    async def subscribe_event(
        self,
        node_text: str,
        on_event: Callable[[dict, str, Optional[float]], None],
        *,
        event_filter: Optional[Any] = None,
    ) -> Tuple[bool, Optional[OpcUaSubscription]]:
        """订阅事件(Event);回调调度到 aio loop,语义同 :meth:`subscribe_data_change`。"""
        sync = self._typed(OpcUaClient)
        loop = asyncio.get_running_loop()

        def _bridge(fields: dict, node_id_str: str, ts: Optional[float]) -> None:
            try:
                loop.call_soon_threadsafe(on_event, fields, node_id_str, ts)
            except RuntimeError:
                pass

        return await self._run(
            lambda: sync.subscribe_event(node_text, _bridge, event_filter=event_filter)
        )


class _AFinsRoutingClient(ABaseClient):
    """FINS 路由参数只读镜像(TCP/UDP 共用,私有基类)。"""

    @property
    def _fins(self) -> Union[OmronFinsTcpClient, OmronFinsUdpClient]:
        """取 FINS 同步实例并断言驱动类型(内部属性)。"""
        sync = self._sync
        if not isinstance(sync, (OmronFinsTcpClient, OmronFinsUdpClient)):
            raise TypeError("内部错误:sync 实例不是 FINS 客户端")
        return sync

    @property
    def destination_network(self) -> int:
        """目标网络号(转发同步实例)。"""
        return self._fins.destination_network

    @property
    def destination_node(self) -> int:
        """目标节点号(自动模式为握手/推导后的最新值,转发同步实例)。"""
        return self._fins.destination_node

    @property
    def destination_unit(self) -> int:
        """目标单元号(转发同步实例)。"""
        return self._fins.destination_unit

    @property
    def source_network(self) -> int:
        """源网络号(转发同步实例)。"""
        return self._fins.source_network

    @property
    def source_node(self) -> int:
        """源节点号(自动模式为握手/推导后的最新值,转发同步实例)。"""
        return self._fins.source_node

    @property
    def source_unit(self) -> int:
        """源单元号(转发同步实例)。"""
        return self._fins.source_unit


class AOmronFinsTcpClient(_AFinsRoutingClient):
    """欧姆龙 FINS/TCP 异步客户端。"""

    def __init__(
        self,
        ip_address: str = "192.168.250.1",
        port: int = FINS_DEFAULT_PORT,
        local_node: int = 0,
    ) -> None:
        """初始化 FINS/TCP 异步客户端。

        :param ip_address: PLC 的 IP
        :param port: 端口,默认 9600
        :param local_node: 本地节点号;``None``/``0`` = 由 PLC 自动分配
            (握手时获取)
        :raises ValueError: 参数非法
        """
        super().__init__(OmronFinsTcpClient(ip_address, port, local_node))

    @property
    def local_node(self) -> int:
        """本地节点号(自动分配时在连接后可用,转发同步实例)。"""
        sync = self._typed(OmronFinsTcpClient)
        return sync.local_node

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多存储区批量读取(0104,单事务;语义同同步版)。"""
        sync = self._typed(OmronFinsTcpClient)
        return await self._run(lambda: sync.read_batch(items))


class AOmronFinsUdpClient(_AFinsRoutingClient):
    """欧姆龙 FINS/UDP 异步客户端。"""

    def __init__(self, ip_address: str = "192.168.250.1", port: int = FINS_DEFAULT_PORT) -> None:
        """初始化 FINS/UDP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,FINS 默认 9600
        :raises ValueError: 参数非法
        """
        super().__init__(OmronFinsUdpClient(ip_address, port))

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多存储区批量读取(0104,单事务;语义同同步版)。"""
        sync = self._typed(OmronFinsUdpClient)
        return await self._run(lambda: sync.read_batch(items))


class AOmronCipClient(ABaseClient):
    """欧姆龙 NJ/NX CIP 异步客户端(内置 EtherNet/IP,44818)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = AB_EIP_DEFAULT_PORT,
        connected_messaging: bool = False,
    ) -> None:
        """初始化欧姆龙 CIP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,EtherNet/IP 默认 44818
        :param connected_messaging: True 走 connected 消息(Forward Open +
            SendUnitData);默认 False 走 unconnected 直发
        :raises ValueError: 参数非法
        """
        super().__init__(OmronCipClient(ip_address, port, connected_messaging))

    @property
    def connected_messaging(self) -> bool:
        """是否走 connected 消息(转发同步实例)。"""
        sync = self._typed(OmronCipClient)
        return sync.connected_messaging

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多服务包批量读取(0x0A,单事务;语义同同步版)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(lambda: sync.read_batch(items))

    @property
    def slot(self) -> int:
        """CPU 槽号(转发同步实例;NJ 直发路径默认 0)。"""
        sync = self._typed(OmronCipClient)
        return sync.slot

    async def generic_message(
        self, service: int, class_id: int, instance: int, body: bytes = b""
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 CIP 服务(语义同同步版 :meth:`OmronCipClient.generic_message`)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(lambda: sync.generic_message(service, class_id, instance, body))

    async def list_identity(self) -> Tuple[bool, Optional[dict]]:
        """ENIP ListIdentity 单播(语义同同步版)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(sync.list_identity)

    async def get_plc_info(self) -> Tuple[bool, Optional[dict]]:
        """Identity Object GetAttributesAll(语义同同步版)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(sync.get_plc_info)

    async def get_attribute_all(
        self, class_id: int, instance: int
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 GetAttributesAll(语义同同步版)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(lambda: sync.get_attribute_all(class_id, instance))

    async def get_attribute_list(
        self, class_id: int, instance: int, attributes: Sequence[int]
    ) -> Tuple[bool, Optional[List[Tuple[int, object]]]]:
        """通用 GetAttributeList(语义同同步版)。"""
        sync = self._typed(OmronCipClient)
        return await self._run(
            lambda: sync.get_attribute_list(class_id, instance, attributes)
        )

    @property
    def connection_size(self) -> Optional[int]:
        """生效连接尺寸(connected 模式 Forward Open 后可用,转发同步实例)。"""
        sync = self._typed(OmronCipClient)
        return sync.connection_size


class AOpenTcpClient(ABaseClient):
    """通用自定义 TCP/IP 异步客户端(分隔符/定长成帧,收发行为可配)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = OPEN_TCP_DEFAULT_PORT,
        delimiter: Optional[Union[str, bytes]] = OPEN_TCP_DEFAULT_DELIMITER,
        encoding: str = OPEN_TCP_DEFAULT_ENCODING,
        append_delimiter: bool = True,
        strip_delimiter: bool = True,
        max_frame: int = OPEN_TCP_MAX_FRAME,
        frame_length: Optional[int] = None,
    ) -> None:
        """初始化通用 TCP 异步客户端。

        :param ip_address: 设备 IP 或主机名
        :param port: TCP 端口(自定义设备无统一标准,按现场配置)
        :param delimiter: 帧分隔符(bytes 或 str;str 按 UTF-8 编码);
            定长成帧时传 ``None``
        :param encoding: ``send_text``/``receive_text``/``transact_text``
            的字符编码,默认 UTF-8
        :param append_delimiter: ``send_text``/``transact_text`` 发送时
            自动补分隔符(定长成帧必须为 False)
        :param strip_delimiter: ``receive``/``transact*`` 返回帧时是否
            去掉末尾分隔符(仅分隔符成帧生效)
        :param max_frame: 帧内容字节上限(不含分隔符),超限判流内失步
        :param frame_length: 定长成帧的每帧字节数(≥1,不超过
            ``max_frame``);与 ``delimiter`` 互斥,二者必须提供其一
        :raises ValueError: 参数非法
        """
        super().__init__(
            OpenTcpClient(
                ip_address,
                port,
                delimiter,
                encoding,
                append_delimiter,
                strip_delimiter,
                max_frame,
                frame_length,
            )
        )

    def _client(self) -> OpenTcpClient:
        """取通用 TCP 同步实例(内部属性)。"""
        return self._typed(OpenTcpClient)

    async def send(self, data: bytes) -> bool:
        """原样发送字节(语义同同步版 :meth:`OpenTcpClient.send`)。"""
        return await self._run(lambda: self._client().send(data))

    async def send_text(self, text: str) -> bool:
        """编码发送文本(可自动补分隔符)。"""
        return await self._run(lambda: self._client().send_text(text))

    async def receive(
        self, timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[bytes]]:
        """按分隔符收一帧(bytes)。"""
        return await self._run(lambda: self._client().receive(timeout))

    async def receive_text(
        self, timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """收一帧并解码为文本。"""
        return await self._run(lambda: self._client().receive_text(timeout))

    async def transact(
        self, data: bytes, timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[bytes]]:
        """发送字节并收一帧应答。"""
        return await self._run(lambda: self._client().transact(data, timeout))

    async def transact_text(
        self, text: str, timeout: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """发送文本并收一帧应答解码为文本。"""
        return await self._run(lambda: self._client().transact_text(text, timeout))

    @property
    def delimiter(self) -> Optional[bytes]:
        """帧分隔符(bytes);定长成帧为 None(转发同步实例)。"""
        return self._client().delimiter

    @property
    def frame_length(self) -> Optional[int]:
        """定长成帧的每帧字节数;分隔符成帧为 None(转发同步实例)。"""
        return self._client().frame_length

    @property
    def encoding(self) -> str:
        """文本收发的字符编码(转发同步实例)。"""
        return self._client().encoding

    @property
    def append_delimiter(self) -> bool:
        """发送文本时是否自动补分隔符(转发同步实例)。"""
        return self._client().append_delimiter

    @property
    def strip_delimiter(self) -> bool:
        """收帧返回时是否去掉末尾分隔符(转发同步实例)。"""
        return self._client().strip_delimiter

    @property
    def max_frame(self) -> int:
        """帧内容字节上限(转发同步实例)。"""
        return self._client().max_frame


class AMTConnectClient(ABaseClient):
    """CNC MTConnect 异步客户端(HTTP/XML 只读数采)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        port: int = MTCONNECT_DEFAULT_PORT,
    ) -> None:
        """初始化 MTConnect 异步客户端。

        :param ip_address: Agent 所在 IP 或主机名(机床或工控机)
        :param port: Agent HTTP 端口,默认 5000
        :raises ValueError: 参数非法
        """
        super().__init__(MTConnectClient(ip_address, port))

    def _client(self) -> MTConnectClient:
        """取 MTConnect 同步实例(内部属性)。"""
        return self._typed(MTConnectClient)

    async def snapshot(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读取 /current 全量数据项快照(id/name → 文本值)。"""
        return await self._run(lambda: self._client().snapshot())

    async def read_conditions(self) -> Tuple[bool, Optional[List[Dict[str, str]]]]:
        """读取条件项(报警/警告/正常)当前列表。"""
        return await self._run(lambda: self._client().read_conditions())

    async def probe(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """读取 /probe 设备信息。"""
        return await self._run(lambda: self._client().probe())


class AAllenBradleyEthIpClient(ABaseClient):
    """罗克韦尔 AB EtherNet/IP 异步客户端(Logix 标签读写)。"""

    def __init__(
        self,
        ip_address: str = "192.168.1.20",
        port: int = AB_EIP_DEFAULT_PORT,
        slot: int = AB_EIP_DEFAULT_SLOT,
        connected_messaging: bool = False,
    ) -> None:
        """初始化 AB EtherNet/IP 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: 端口,EtherNet/IP 默认 44818
        :param slot: CPU 槽号(内置以太网口机型为 0;1756 背板按实际槽位)
        :param connected_messaging: True 走 connected 消息(Forward Open +
            SendUnitData);默认 False 走 unconnected 消息
        :raises ValueError: 参数非法
        """
        super().__init__(
            AllenBradleyEthIpClient(ip_address, port, slot, connected_messaging)
        )

    @property
    def slot(self) -> int:
        """CPU 槽号(转发同步实例)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return sync.slot

    async def read_batch(
        self, items: Sequence[Tuple[str, Union[DataType, str]]]
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """多服务包批量读取(0x0A,单事务;语义同同步版)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(lambda: sync.read_batch(items))

    async def generic_message(
        self, service: int, class_id: int, instance: int, body: bytes = b""
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 CIP 服务(语义同同步版 :meth:`AllenBradleyEthIpClient.generic_message`)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(lambda: sync.generic_message(service, class_id, instance, body))

    async def list_identity(self) -> Tuple[bool, Optional[dict]]:
        """ENIP ListIdentity 单播(语义同同步版)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(sync.list_identity)

    async def get_plc_info(self) -> Tuple[bool, Optional[dict]]:
        """Identity Object GetAttributesAll(语义同同步版)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(sync.get_plc_info)

    async def get_attribute_all(
        self, class_id: int, instance: int
    ) -> Tuple[bool, Optional[bytes]]:
        """通用 GetAttributesAll(语义同同步版)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(lambda: sync.get_attribute_all(class_id, instance))

    async def get_attribute_list(
        self, class_id: int, instance: int, attributes: Sequence[int]
    ) -> Tuple[bool, Optional[List[Tuple[int, object]]]]:
        """通用 GetAttributeList(语义同同步版)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return await self._run(
            lambda: sync.get_attribute_list(class_id, instance, attributes)
        )

    @property
    def connected_messaging(self) -> bool:
        """是否走 connected 消息(转发同步实例)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return sync.connected_messaging

    @property
    def connection_size(self) -> Optional[int]:
        """生效连接尺寸(connected 模式 Forward Open 后可用,转发同步实例)。"""
        sync = self._typed(AllenBradleyEthIpClient)
        return sync.connection_size


class ABeckhoffAdsClient(ABaseClient):
    """倍福 TwinCAT ADS 异步客户端(封装 pyads,变量名读写)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.10",
        ads_port: int = ADS_DEFAULT_ADS_PORT,
        net_id: str = "",
    ) -> None:
        """初始化 TwinCAT ADS 异步客户端。

        :param ip_address: PLC 的 IP 或主机名(构造默认 NetId 用)
        :param ads_port: 目标 AMS 端口,TC3 PLC 运行时 1 默认 851
        :param net_id: 目标 AMS NetId 显式覆盖(6 段 0~255 数字);
            默认由 ``ip_address`` 拼 ``.1.1`` 后缀组装
        :raises ValueError: 参数非法
        """
        super().__init__(BeckhoffAdsClient(ip_address, ads_port, net_id))

    @property
    def net_id(self) -> str:
        """目标 AMS NetId(转发同步实例)。"""
        sync = self._typed(BeckhoffAdsClient)
        return sync.net_id

    @property
    def ads_port(self) -> int:
        """目标 AMS 端口(转发同步实例)。"""
        sync = self._typed(BeckhoffAdsClient)
        return sync.ads_port


class ASiemensS7Client(ABaseClient):
    """西门子 S7 异步客户端(封装 python-snap7,DB/I/Q/M 绝对寻址)。"""

    def __init__(
        self,
        ip_address: str = "192.168.0.1",
        port: int = S7_DEFAULT_PORT,
        rack: int = S7_DEFAULT_RACK,
        slot: int = S7_DEFAULT_SLOT,
        dll_path: str = "",
    ) -> None:
        """初始化 S7 异步客户端。

        :param ip_address: PLC 的 IP 或主机名
        :param port: ISO-on-TCP 端口,标准 102
        :param rack: 机架号,S7_DEFAULT_RACK(0)
        :param slot: 槽位号,1200/1500 常用 1;300/400 的 CPU 常在 2
        :param dll_path: snap7 原生库路径显式覆盖,仅 1.x/2.x(C 封装线)
            生效——32 位 Python 需自备 32 位 snap7.dll;3.x 纯 Python 实现
            忽略此参数;留空用捆绑库
        :raises ValueError: 参数非法
        """
        super().__init__(SiemensS7Client(ip_address, port, rack, slot, dll_path))

    def _client(self) -> SiemensS7Client:
        """取 S7 同步实例(内部属性)。"""
        return self._typed(SiemensS7Client)

    @property
    def rack(self) -> int:
        """机架号(转发同步实例)。"""
        return self._client().rack

    @property
    def slot(self) -> int:
        """槽位号(转发同步实例)。"""
        return self._client().slot
