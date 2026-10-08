"""汇川 H3U/H5U 客户端——继承 Modbus 实现,只换地址映射。

汇川小型 PLC(H3U/H3S/H5U/Easy 系列)的 TCP 与串口通信本质是
标准 Modbus:网口 Modbus TCP(默认端口 502,从站默认开启),
串口 Modbus RTU(缺省 9600-8N2)。帧收发完全复用
:mod:`omniplc.plc.modbus`,本模块只把汇川软元件地址换算为
Modbus 线圈/保持寄存器地址,见 :mod:`.address`。

地址语法::

    D100      数据寄存器(字,十进制)
    R100      保持寄存器(H5U,基址 0x3000)
    M10 / B10 / S10        位软元件(线圈区)
    SM10 / SD10            特殊软元件(H3U)
    T10 / C10  位 = 接点,字 = 当前值(C 字:C0~C199 为 16 位;
               C200~C255 为 32 位计数器,仅 32 位类型 INT/UINT/FLOAT)
    X17 / Y17  输入/输出(八进制编号;H5U 到 X/Y1777,H3U 到 X/Y377)
    D100.3    字软元件位访问(读-改-写)
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple, Union

from ...core.constants import (
    INOVANCE_SERIAL_DEFAULT_STOP_BITS,
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
)
from ..modbus.modbus import ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from .address import (
    check_counter_word_type,
    parse_inovance_address,
    to_modbus_address,
    translate_batch_address,
)
from ...core.types import (
    ByteOrder,
    DataType,
    PrimitiveValue,
    SerialParity,
)


class _InovanceBase(ModbusBaseClient):
    """汇川客户端公共基类:地址翻译后委托 Modbus 公共逻辑(私有)。

    位软元件映射到线圈区,字软元件映射到保持寄存器区,
    功能码选择/字序/事务/重连全部由 Modbus 实现承担。
    32 位计数器(C200~C255)的 32 位类型门控在翻译收口处完成
    (``C205`` → ``hr63242``,双寄存器展开由 Modbus 层按类型自动完成)。

    **记号约定**:单点读写仅认**汇川软元件记号**(``D100``/``X17``/``C205``;
    ``hr100``/``40001`` 等 Modbus 记号单点均拒,防两类编号体系静默混淆),
    批量/区间读写支持**双记号**——批量路径按
    :func:`~omniplc.plc.inovance.address.translate_batch_address`
    「Modbus 记号优先」裁决:本库 Modbus 记号形态原样放行(存量批量行为
    零变化),非 Modbus 记号才按汇川换算表翻译。唯 **``C`` 记号歧义**
    (Modbus 线圈 vs 汇川计数器)批量按 Modbus 线圈裁决——汇川计数器
    (含 C32)的批量访问请用单点(单点 ``C10`` 恒为汇川计数器,与批量
    语义分叉已在两处 docstring 披露)。
    """

    def _translate(self, address: str, data_type: DataType) -> str:
        """地址翻译 + 32 位计数器类型门控(内部方法)。

        :raises ValueError: 地址非法 / 以非 32 位类型访问 C200~C255
        """
        if data_type is DataType.BOOL:
            return to_modbus_address(address, True)
        parsed = parse_inovance_address(address)
        check_counter_word_type(parsed, data_type)
        return to_modbus_address(parsed, False)

    # ------------------------------------------------------------------
    # 批量方法覆写:双记号翻译后委托 Modbus 基类(读/写/掩码/FC23/区间读)
    # ------------------------------------------------------------------

    def read_many(
        self,
        addresses: Sequence[str],
        data_type: Union[DataType, str],
    ) -> List[Tuple[bool, Optional[PrimitiveValue]]]:
        """批量读(双记号):汇川记号翻译后走 Modbus 合并读。"""
        data_type_enum = DataType.coerce(data_type)
        return super().read_many(
            [translate_batch_address(addr, data_type_enum) for addr in addresses],
            data_type_enum,
        )

    def read_range(
        self,
        address: str,
        count: int,
        data_type: Union[DataType, str],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """区间读(双记号):起始地址翻译后走 Modbus 区间读。

        BOOL 仅位软元件(翻译为线圈区,``M100`` → ``c100``);字软元件
        BOOL 区间读语义未定义,由 Modbus 层拒绝(同 Modbus 记号口径)。
        """
        data_type_enum = DataType.coerce(data_type)
        return super().read_range(
            translate_batch_address(address, data_type_enum),
            count,
            data_type_enum,
        )

    def read_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str]]],
    ) -> Tuple[bool, Optional[List[PrimitiveValue]]]:
        """混类型批量读(双记号):逐条目翻译后走 Modbus 合并读。"""
        return super().read_batch(
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype)
                for addr, dtype in items
            ]
        )

    def write_many(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> List[bool]:
        """批量写(双记号):逐条目翻译后走 Modbus 合并写。"""
        return super().write_many(
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype, value)
                for addr, dtype, value in items
            ]
        )

    def write_batch(
        self,
        items: Sequence[Tuple[str, Union[DataType, str], PrimitiveValue]],
    ) -> Tuple[bool, Optional[List[bool]]]:
        """混类型批量写(双记号):逐条目翻译后走 Modbus 批量写。"""
        return super().write_batch(
            [
                (translate_batch_address(addr, DataType.coerce(dtype)), dtype, value)
                for addr, dtype, value in items
            ]
        )

    def write_mask_register(
        self,
        address: str,
        and_mask: int,
        or_mask: int,
        byte_order: Union[ByteOrder, str] = "big",
    ) -> bool:
        """掩码写(FC 22,双记号):汇川字记号翻译为保持寄存器。"""
        return super().write_mask_register(
            translate_batch_address(address, DataType.USHORT),
            and_mask,
            or_mask,
            byte_order,
        )

    def read_write_registers(
        self,
        read_address: str,
        read_count: int,
        write_address: str,
        values: Sequence[int],
    ) -> Tuple[bool, Optional[List[int]]]:
        """「先写后读」多寄存器(FC 23,双记号):两地址分别翻译。"""
        return super().read_write_registers(
            translate_batch_address(read_address, DataType.USHORT),
            read_count,
            translate_batch_address(write_address, DataType.USHORT),
            values,
        )

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        return ModbusBaseClient._read(
            self, self._translate(address, data_type), data_type
        )

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        ModbusBaseClient._write(
            self, self._translate(address, data_type), data_type, value
        )

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        return ModbusBaseClient._read_string(
            self, self._translate(address, DataType.STRING), length, encoding
        )

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        return ModbusBaseClient._write_string(
            self, self._translate(address, DataType.STRING), value, encoding
        )


class InovanceTcpClient(_InovanceBase, ModbusTcpClient):
    """汇川 H3U/H5U Modbus TCP 客户端(端口 502)。

    :example: ``client = InovanceTcpClient("192.168.1.88", 502, 1)``
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


class InovanceRtuClient(_InovanceBase, ModbusRtuClient):
    """汇川 H3U/H5U Modbus RTU 客户端(串口,需要 pyserial)。

    :example::

        client = InovanceRtuClient(station=1)
        client.configure_serial("COM3")
        client.connect()
    """

    def configure_serial(
        self,
        port_name: str,
        baud_rate: int = SERIAL_DEFAULT_BAUD_RATE,
        data_bits: int = SERIAL_DEFAULT_DATA_BITS,
        stop_bits: float = INOVANCE_SERIAL_DEFAULT_STOP_BITS,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数(参数以 PLC 侧 D8110/D8120 配置为准)。

        H5U&Easy 手册 9.5.1(印刷页 418)从站默认 9600-8N2(停止位默认 2);
        H3U 手册的 Modbus-RTU 示例为 9600-8N1(D8120=H081),两机型默认并不
        一致——本库默认按 H5U 口径给 2,接 H3U 时按现场配置显式传参。

        :param port_name: 串口名,如 ``"COM3"``
        :param baud_rate: 波特率,默认 9600
        :param data_bits: 数据位,Modbus RTU 为 8
        :param stop_bits: 停止位,默认 2(H5U 口径)
        :param parity: 校验位,缺省无校验
        :raises ValueError: 参数非法
        """
        ModbusRtuClient.configure_serial(
            self, port_name, baud_rate, data_bits, stop_bits, parity
        )
