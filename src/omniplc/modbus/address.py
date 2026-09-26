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

from ..core.constants import ADDRESS_CACHE_MAXSIZE, MODBUS_REGISTER_BIT_MAX

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
                f"区域 {self.area.value!r} 不可写(仅线圈/保持寄存器可写)"
            )

    @property
    def write_multi_function_code(self) -> int:
        """批量写入使用的功能码(仅线圈/保持寄存器可写)。"""
        try:
            return _FC_WRITE_MULTI[self.area]
        except KeyError:
            raise ValueError(
                f"区域 {self.area.value!r} 不可写(仅线圈/保持寄存器可写)"
            )


# 地址串 → 解析结果缓存(结果类型不可变):高频轮询同址免重复正则解析
@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)
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
        return _parse_modicon(text)

    match = _PREFIX_RE.match(text)
    if match is None:
        raise ValueError(f"无法解析 Modbus 地址:{address!r}(示例:hr0 / c0 / di10 / 40001)")
    area = ModbusArea(match.group(1))
    offset = int(match.group(2))
    bit: Optional[int] = None
    if match.group(3) is not None:
        bit = int(match.group(3))
        if area in (ModbusArea.COIL, ModbusArea.DISCRETE_INPUT):
            raise ValueError(f"区域 {area.value!r} 本身就是位地址,不支持位访问:{address!r}")
        if not 0 <= bit <= MODBUS_REGISTER_BIT_MAX:
            raise ValueError(
                f"寄存器位号必须在 0~{MODBUS_REGISTER_BIT_MAX} 之间,收到:{bit}"
            )
    return ModbusAddress(area=area, offset=offset, bit=bit)


def _parse_modicon(text: str) -> ModbusAddress:
    """解析 Modicon 1 基地址(内部函数)。

    按**数字位数**区分两套方案(数值法无法消歧,如 ``40001`` 既是 5 位
    保持寄存器、也是 6 位线圈 ``040001``):

    - **6 位**扩展方案:``000001~065536`` 线圈 / ``100001~165536`` 离散输入 /
      ``300001~365536`` 输入寄存器 / ``400001~465536`` 保持寄存器;
    - **5 位**传统方案:``00001~09999`` / ``10001~19999`` / ``30001~39999`` /
      ``40001~49999``;1~5 位数字均按此口径。

    偏移 = 编号 − 区段基址(1 基)。
    """
    number = int(text)
    if len(text) == 6:
        ranges = (
            (1, 65536, ModbusArea.COIL),
            (100001, 165536, ModbusArea.DISCRETE_INPUT),
            (300001, 365536, ModbusArea.INPUT_REGISTER),
            (400001, 465536, ModbusArea.HOLDING_REGISTER),
        )
        hint = "6 位 000001~065536/100001~165536/300001~365536/400001~465536"
    else:
        ranges = (
            (1, 9999, ModbusArea.COIL),
            (10001, 19999, ModbusArea.DISCRETE_INPUT),
            (30001, 39999, ModbusArea.INPUT_REGISTER),
            (40001, 49999, ModbusArea.HOLDING_REGISTER),
        )
        hint = "5 位 00001~09999/10001~19999/30001~39999/40001~49999"
    for start, end, area in ranges:
        if start <= number <= end:
            return ModbusAddress(area=area, offset=number - start)
    raise ValueError(f"Modicon 地址超出区段:{number},支持 {hint}")
