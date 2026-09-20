"""汇川 H3U/H5U 客户端——继承 Modbus 实现,只换地址映射。

汇川小型 PLC(H3U/H3S/H5U/Easy 系列)的 TCP 与串口通信本质是
标准 Modbus:网口 Modbus TCP(默认端口 502,从站默认开启),
串口 Modbus RTU(缺省 9600-8N2)。帧收发完全复用
:mod:`omniplc.modbus`,本模块只把汇川软元件地址换算为
Modbus 线圈/保持寄存器地址,见 :mod:`.address`。

地址语法::

    D100      数据寄存器(字,十进制)
    R100      保持寄存器(H5U,基址 0x3000)
    M10 / B10 / S10        位软元件(线圈区)
    SM10 / SD10            特殊软元件(H3U)
    T10 / C10  位 = 接点,字 = 当前值(C 字仅 C0~C199)
    X17 / Y17  输入/输出(八进制编号)
    D100.3    字软元件位访问(读-改-写)
"""
from __future__ import annotations

from typing import Union

from .address import to_modbus_address
from ...core.constants import (
    MODBUS_DEFAULT_PORT,
    MODBUS_DEFAULT_STATION,
    SERIAL_DEFAULT_BAUD_RATE,
    SERIAL_DEFAULT_DATA_BITS,
    SERIAL_DEFAULT_PARITY,
)
from ...modbus.modbus import ModbusBaseClient, ModbusRtuClient, ModbusTcpClient
from ...types import DataType, PrimitiveValue, SerialParity


class _InovanceBase(ModbusBaseClient):
    """汇川客户端公共基类:地址翻译后委托 Modbus 公共逻辑(私有)。

    位软元件映射到线圈区,字软元件映射到保持寄存器区,
    功能码选择/字序/事务/重连全部由 Modbus 实现承担。
    """

    def _read(self, address: str, data_type: DataType) -> PrimitiveValue:
        is_bool = data_type is DataType.BOOL
        return ModbusBaseClient._read(self, to_modbus_address(address, is_bool), data_type)

    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None:
        is_bool = data_type is DataType.BOOL
        ModbusBaseClient._write(self, to_modbus_address(address, is_bool), data_type, value)

    def _read_string(self, address: str, length: int, encoding: str) -> PrimitiveValue:
        return ModbusBaseClient._read_string(self, to_modbus_address(address), length, encoding)

    def _write_string(self, address: str, value: str, encoding: str) -> PrimitiveValue:
        return ModbusBaseClient._write_string(self, to_modbus_address(address), value, encoding)


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
        stop_bits: float = 2,
        parity: Union[SerialParity, str] = SERIAL_DEFAULT_PARITY,
    ) -> None:
        """配置串口参数,汇川缺省 9600-8N2(停止位默认 2,参数同手册)。

        :param port_name: 串口名,如 ``"COM3"``
        :param baud_rate: 波特率,默认 9600
        :param data_bits: 数据位,Modbus RTU 为 8
        :param stop_bits: 停止位,汇川缺省 2
        :param parity: 校验位,汇川缺省无校验
        :raises ValueError: 参数非法
        """
        ModbusRtuClient.configure_serial(
            self, port_name, baud_rate, data_bits, stop_bits, parity
        )
