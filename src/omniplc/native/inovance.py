"""原生 asyncio 汇川客户端(H3U/H5U Modbus TCP + MC 协议兼容 3E TCP)。

:class:`AsyncInovanceTcpClient` / :class:`AsyncInovanceMcTcpClient` 是同步
:class:`~omniplc.plc.inovance.InovanceTcpClient` /
:class:`~omniplc.plc.inovance.mc.InovanceMcTcpClient` 的原生异步孪生,**只重写
薄层**——与同步侧同款继承结构:

- TCP 版继承 :class:`~omniplc.native.AsyncModbusTcpClient`,覆写四个单点
  钩子(``_read``/``_write``/``_read_string``/``_write_string``)做汇川记号
  → Modbus 地址翻译后委托 Modbus 原语(与同步 ``_InovanceBase._translate``
  同一换算收口,复用 :mod:`omniplc.plc.inovance.address` 纯函数);
  批量与诊断方法直承 Modbus 原生实现,按 **Modbus 记号**(``hr100``/``c10``)
  解析——汇川记号明确拒绝,不静默错址(记号边界与同步侧同口径)。
- MC 版继承 :class:`~omniplc.native.AsyncMelsecMcTcpClient`,覆写
  ``_device_info``(换汇川码表)/``_translate_address``(R→D 统一编址、
  X/Y 八进制→帧内十六进制)/``_build_frame`` 三个纯编解码钩子,收发与解析
  全走三菱原生实现;``_has_ping`` 显式置 False(H5U 手册 16.4 命令支持面
  无 0101,与同步侧同口径)。

RTU 串口走线需要串口传输层,**不在本层**(与 MC 1C/3C/4C 同批口径)。

:example::

    from omniplc.native import AsyncInovanceTcpClient

    async with AsyncInovanceTcpClient("192.168.1.88", 502, 1) as client:
        ok, value = await client.read_float("D100")
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple, Union

from .modbus import AsyncModbusTcpClient
from .melsec import AsyncMelsecMcTcpClient
from ..core.constants import (
    INOVANCE_MC_DEFAULT_PORT,
    INOVANCE_MC_DEVICE_CODES,
    MC_DEFAULT_MONITOR_TIMER,
    MC_DEFAULT_NETWORK_NUMBER,
    MC_DEFAULT_PC_NUMBER,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
)
from ..core.types import ByteOrder, DataType, McFrame, PrimitiveValue
from ..plc.inovance.address import (
    check_counter_word_type,
    parse_inovance_address,
    to_modbus_address,
    translate_batch_address,
)
from ..plc.inovance.mc import _to_melsec_address
from ..plc.melsec import codec_qna
from ..plc.melsec.address import McAddress


class AsyncInovanceTcpClient(AsyncModbusTcpClient):
    """汇川 H3U/H5U Modbus TCP 客户端(原生异步,端口 502)。

    单点与批量/区间读写都支持**双记号**(与同步
    :class:`~omniplc.plc.inovance.InovanceTcpClient` 同收口):汇川软元件
    记号(``D100``/``X17``/``C205``)与本库 Modbus 记号(``hr100``/
    ``c10``);批量按「Modbus 记号优先」翻译(:func:`~omniplc.plc.inovance.address.translate_batch_address`),
    **``C`` 记号歧义批量按 Modbus 线圈裁决**,汇川计数器批量请用单点。

    :example: ``client = AsyncInovanceTcpClient("192.168.1.88", 502, 1)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.88",
        port: int = MODBUS_DEFAULT_PORT,
        station: int = MODBUS_DEFAULT_STATION,
    ) -> None:
        """初始化汇川 TCP 客户端。

        :param ip_address: PLC 的 IP 或主机名(Modbus TCP 从站默认开启,
            示例默认取 Easy 系列出厂 IP,实际以 AutoShop 以太网配置为准)
        :param port: 端口,默认 502(汇川从站服务默认开启且多数机型不可改)
        :param station: 从站站号(Unit ID),默认 1
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, station)

    def _translate(self, address: str, data_type: DataType) -> str:
        """地址翻译 + 32 位计数器类型门控(内部方法,与同步侧同收口)。

        :raises ValueError: 地址非法 / 以非 32 位类型访问 C200~C255
        """
        if data_type is DataType.BOOL:
            return to_modbus_address(address, True)
        parsed = parse_inovance_address(address)
        check_counter_word_type(parsed, data_type)
        return to_modbus_address(parsed, False)

    async def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        """汇川记号翻译后委托 Modbus 读原语(内部方法)。"""
        return await AsyncModbusTcpClient._read(
            self, self._translate(address, data_type), data_type
        )

    async def _write(
        self, address: str, data_type: DataType, value: PrimitiveValue
    ) -> None:
        """汇川记号翻译后委托 Modbus 写原语(内部方法)。"""
        await AsyncModbusTcpClient._write(
            self, self._translate(address, data_type), data_type, value
        )

    async def _read_string(
        self, address: str, length: int, encoding: str
    ) -> PrimitiveValue:
        """汇川记号翻译后委托 Modbus 字符串读原语(内部方法)。"""
        return await AsyncModbusTcpClient._read_string(
            self, self._translate(address, DataType.STRING), length, encoding
        )

    async def _write_string(
        self, address: str, value: str, encoding: str
    ) -> PrimitiveValue:
        """汇川记号翻译后委托 Modbus 字符串写原语(内部方法)。"""
        return await AsyncModbusTcpClient._write_string(
            self, self._translate(address, DataType.STRING), value, encoding
        )

    # ------------------------------------------------------------------
    # 批量方法覆写:双记号翻译后委托 Modbus 原生批量(与同步侧同收口)
    # ------------------------------------------------------------------

    async def read_many(
        self,
        addresses: Sequence[str],
        data_type: Union[DataType, str],
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读(双记号):汇川记号翻译后走 Modbus 合并读。"""
        data_type_enum = DataType.coerce(data_type)
        return await AsyncModbusTcpClient.read_many(
            self,
            [translate_batch_address(addr, data_type_enum) for addr in addresses],
            data_type_enum,
        )

    async def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """区间读(双记号):起始地址翻译后走 Modbus 区间读(与同步侧同收口)。"""
        data_type_enum = DataType.coerce(data_type)
        return await AsyncModbusTcpClient.read_range(
            self,
            translate_batch_address(address, data_type_enum),
            count,
            data_type_enum,
        )

    async def read_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str]]],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """混类型批量读(双记号):逐条目翻译后走 Modbus 合并读。"""
        return await AsyncModbusTcpClient.read_batch(
            self,
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype)
                for addr, dtype in items
            ],
        )

    async def write_many(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> List[bool]:
        """批量写(双记号):逐条目翻译后走 Modbus 合并写。"""
        return await AsyncModbusTcpClient.write_many(
            self,
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype, value)
                for addr, dtype, value in items
            ],
        )

    async def write_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> Tuple[bool, Optional[List[bool]]]:
        """混类型批量写(双记号):逐条目翻译后走 Modbus 批量写。"""
        return await AsyncModbusTcpClient.write_batch(
            self,
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype, value)
                for addr, dtype, value in items
            ],
        )

    async def write_mask_register(
        self,
        address: str,
        and_mask: int,
        or_mask: int,
        byte_order: Union[ByteOrder, str] = "big",
    ) -> bool:
        """掩码写(FC 22,双记号):汇川字记号翻译为保持寄存器。"""
        return await AsyncModbusTcpClient.write_mask_register(
            self,
            translate_batch_address(address, DataType.USHORT),
            and_mask,
            or_mask,
            byte_order,
        )

    async def read_write_registers(
        self,
        read_address: str,
        read_count: int,
        write_address: str,
        values: Sequence[int],
    ) -> Tuple[bool, Optional[List[int]]]:
        """「先写后读」多寄存器(FC 23,双记号):两地址分别翻译。"""
        return await AsyncModbusTcpClient.read_write_registers(
            self,
            translate_batch_address(read_address, DataType.USHORT),
            read_count,
            translate_batch_address(write_address, DataType.USHORT),
            values,
        )


class AsyncInovanceMcTcpClient(AsyncMelsecMcTcpClient):
    """汇川 MC 协议兼容客户端(原生异步,TCP,3E 帧二进制)。

    与同步 :class:`~omniplc.plc.inovance.mc.InovanceMcTcpClient` 同口径:
    仅 3E 帧(``frame`` 属性恒为 :attr:`McFrame.FRAME_3E`);S 按 L 编码、
    R 统一编址(R n ≡ D(8000+n))、X/Y 八进制命名换算帧内十六进制;
    探活显式关闭(H5U 手册 16.4 命令支持面无 0101)。

    :example: ``client = AsyncInovanceMcTcpClient("192.168.1.88", 2000)``
    """

    def __init__(
        self,
        ip_address: str = "192.168.1.88",
        port: int = INOVANCE_MC_DEFAULT_PORT,
        network_number: int = MC_DEFAULT_NETWORK_NUMBER,
        pc_number: int = MC_DEFAULT_PC_NUMBER,
    ) -> None:
        """初始化汇川 MC 兼容客户端。

        :param ip_address: PLC 的 IP 或主机名(H5U/Easy 出厂默认 192.168.1.88)
        :param port: 端口,与 AutoShop"MC配置"中设置的端口号一致
            (手册未规定出厂默认;范围 1025~4999、5010~49151,
            不可用 502/9600/44818/2222/34980/12939/12940)
        :param network_number: 网络编号(按三菱 MC 默认 0)
        :param pc_number: PC 编号(默认 0xFF,与三菱 MC 客户端约定一致)
        :raises ValueError: 参数非法
        """
        super().__init__(ip_address, port, McFrame.FRAME_3E, network_number, pc_number)
        # H5U 手册 16.4 命令支持面仅披露 0401/1401/0403/1402,无 0101 CPU
        # 型号读——探活与自动心跳显式关闭(与同步 InovanceMcTcpClient 同口径)
        self._has_ping = False

    def _device_info(self, device: str) -> Tuple[int, bool, int]:
        """查汇川 MC 码表(R 视同 D,内部方法)。"""
        return codec_qna.device_info(
            "D" if device == "R" else device, INOVANCE_MC_DEVICE_CODES
        )

    def _translate_address(self, parsed: McAddress) -> McAddress:
        """汇川记号换算为三菱帧记号(批量读路径与 _build_frame 同源,内部方法)。"""
        return _to_melsec_address(parsed)

    def _build_frame(
        self,
        parsed: McAddress,
        points: int,
        is_bit: bool,
        is_write: bool,
        data: Optional[List[int]] = None,
    ) -> bytes:
        """构造 3E 请求帧,汇川记号先换算为三菱帧记号(内部方法)。"""
        return codec_qna.build_request(
            self._frame.value,
            self._next_serial(),
            self._network_number,
            self._pc_number,
            MC_DEFAULT_MONITOR_TIMER,
            _to_melsec_address(parsed),
            points,
            is_bit,
            is_write,
            data,
            INOVANCE_MC_DEVICE_CODES,
        )
