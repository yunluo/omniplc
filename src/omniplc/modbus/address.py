"""Modbus 地址解析。

支持的地址语法(不区分大小写):

==========================  ==========================================
语法                        含义
==========================  ==========================================
``c0``                      线圈 Coil(FC 01/05/0F)
``di0``                     离散输入 Discrete Input(FC 02)
``hr0``                     保持寄存器 Holding Register(FC 03/06/10)
``ir0``                     输入寄存器 Input Register(FC 04)
``hr0.3``                   保持寄存器 0 的第 3 位(bit0 = 最低位)
``40001``                   Modicon 风格保持寄存器(1 基,内部转 0 基)
``00001``                   Modicon 风格线圈
``10001``                   Modicon 风格离散输入
``30001``                   Modicon 风格输入寄存器
==========================  ==========================================

注意:本库地址为**协议地址(0 基)**;Modicon 风格自动减 1 转换。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from enum import Enum
from typing import Optional

from ..core.constants import MODBUS_REGISTER_BIT_MAX

# 前缀语法:c0 / di10 / hr100.3 / ir5
_PREFIX_RE = re.compile(r"^(c|di|hr|ir)(\d+)(?:\.(\d+))?$")


class ModbusArea(Enum):
    """Modbus 数据区域。"""

    COIL = "c"
    DISCRETE_INPUT = "di"
    HOLDING_REGISTER = "hr"
    INPUT_REGISTER = "ir"


# 各区域读写功能码(键为区域枚举)
_FC_READ = {
    ModbusArea.COIL: 1,
    ModbusArea.DISCRETE_INPUT: 2,
    ModbusArea.HOLDING_REGISTER: 3,
    ModbusArea.INPUT_REGISTER: 4,
}
_FC_WRITE_SINGLE = {ModbusArea.COIL: 5, ModbusArea.HOLDING_REGISTER: 6}
_FC_WRITE_MULTI = {ModbusArea.COIL: 15, ModbusArea.HOLDING_REGISTER: 16}


@dataclass(frozen=True)
class ModbusAddress:
    """解析后的 Modbus 地址(不可变值对象)。

    :ivar area: 数据区域(:class:`ModbusArea` 枚举)
    :ivar offset: 0 基协议地址
    :ivar bit: 位号(0~15,仅寄存器位访问时有值)
    """

    area: ModbusArea
    offset: int
    bit: Optional[int] = None

    @property
    def read_function_code(self) -> int:
        """读取该区域使用的功能码。"""
        return _FC_READ[self.area]

    @property
    def write_single_function_code(self) -> int:
        """单点写入使用的功能码(仅线圈/保持寄存器可写)。"""
        try:
            return _FC_WRITE_SINGLE[self.area]
        except KeyError:
            raise ValueError(
                "区域 {!r} 不可写(仅线圈/保持寄存器可写)".format(self.area.value)
            )

    @property
    def write_multi_function_code(self) -> int:
        """批量写入使用的功能码(仅线圈/保持寄存器可写)。"""
        try:
            return _FC_WRITE_MULTI[self.area]
        except KeyError:
            raise ValueError(
                "区域 {!r} 不可写(仅线圈/保持寄存器可写)".format(self.area.value)
            )


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=4096)
def parse_address(address: str) -> ModbusAddress:
    """解析 Modbus 地址字符串。

    :param address: 地址,如 ``"hr0"``、``"C7"``、``"40001"``
    :return: :class:`ModbusAddress`
    :raises ValueError: 语法错误、位号越界或区域不可写
    """
    if not address or not address.strip():
        raise ValueError("Modbus 地址不能为空")
    text = address.strip().lower()

    if text.isdigit():
        return _parse_modicon(int(text))

    match = _PREFIX_RE.match(text)
    if match is None:
        raise ValueError("无法解析 Modbus 地址:{!r}(示例:hr0 / c0 / di10 / 40001)".format(address))
    area = ModbusArea(match.group(1))
    offset = int(match.group(2))
    bit: Optional[int] = None
    if match.group(3) is not None:
        bit = int(match.group(3))
        if area in (ModbusArea.COIL, ModbusArea.DISCRETE_INPUT):
            raise ValueError("区域 {!r} 本身就是位地址,不支持位访问:{!r}".format(area.value, address))
        if not 0 <= bit <= MODBUS_REGISTER_BIT_MAX:
            raise ValueError(
                "寄存器位号必须在 0~{} 之间,收到:{}".format(MODBUS_REGISTER_BIT_MAX, bit)
            )
    return ModbusAddress(area=area, offset=offset, bit=bit)
def _parse_modicon(number: int) -> ModbusAddress:
    """解析 Modicon 1 基地址(内部函数)。"""
    ranges = (
        (1, 9999, ModbusArea.COIL),
        (10001, 19999, ModbusArea.DISCRETE_INPUT),
        (30001, 39999, ModbusArea.INPUT_REGISTER),
        (40001, 49999, ModbusArea.HOLDING_REGISTER),
    )
    for start, end, area in ranges:
        if start <= number <= end:
            return ModbusAddress(area=area, offset=number - start)
    raise ValueError(
        "Modicon 地址超出区段:{},支持 00001~09999/10001~19999/30001~49999".format(number)
    )
