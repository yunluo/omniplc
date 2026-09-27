"""原生 asyncio 客户端(零第三方依赖,Python 3.7 起)。

与 :mod:`omniplc.aio` 的区别(两套异步并存,按场景选):

==================  ==================================  ==================================
维度                :mod:`omniplc.aio`(线程池包装)     `omniplc.native`(本包,原生)
==================  ==================================  ==================================
实现                同步 I/O + 单线程 ``ThreadPoolExecutor``  原生 ``asyncio`` 协议栈
覆盖面              **全部**协议(自动镜像)                 三协议(Modbus TCP / MC / FINS)
能力面              与同步层同面                          三协议与同步层同面(含批量与扩展)
属性读取            抢事务锁,最长阻塞 ``receive_timeout``  直接读字段,不阻塞事件循环
取消                ``wait_for`` 只放弃等待,事务照跑完    真中断(取消按"是否已发请求"决定拆连)
==================  ==================================  ==================================

**覆盖与能力面**:Modbus TCP、三菱 MC(以太网 1E/3E/4E,TCP/UDP)、欧姆龙
FINS(TCP/UDP);能力面与同步层逐项对齐——单点读写、类型化方法、字符串、点位表、
批量(``read_many``/``write_many``/``read_batch``/``write_batch``)、扩展命令
(MC 0406/0403/1402/0101、FINS 0104、Modbus FC 07/08/11/12/17/20/21/22/23/24 与
FC 43 设备标识),守卫测试 ``tests/unit/test_native_surface.py`` 逐协议锁定
"同步有、原生也必须有的公开面"。**尚未进入本包**的是串口走线(Modbus RTU /
MC 1C·3C·4C)与其余协议(AB / S7 / ADS / OPC-UA / 基恩士 / 松下 / TOYOPUC /
MTConnect / 通用 TCP),需要时用 :mod:`omniplc.aio` 或同步客户端过渡。

**实例绑定一个事件循环**:客户端在哪个循环里用就在哪个循环里建(事务锁按
首次使用时的循环惰性创建,模块级构造再 ``asyncio.run`` 也可正常工作);
跨循环/跨线程共享同一实例不支持。

:example::

    import asyncio
    from omniplc.native import AsyncModbusTcpClient

    async def main() -> None:
        async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
            ok, values = await client.read_batch([("hr0", "ushort"), ("hr1", "float")])
            print(ok, values)

    asyncio.run(main())
"""
from __future__ import annotations

from .base import AsyncBaseClient
from .melsec import AsyncMelsecMcTcpClient, AsyncMelsecMcUdpClient
from .modbus import AsyncModbusTcpClient
from .omron import AsyncOmronFinsTcpClient, AsyncOmronFinsUdpClient
from .transport import AsyncBaseTransport, AsyncTcpTransport, AsyncUdpTransport

# 本包公开面:**不进** ``omniplc.__all__``——根包的镜像守卫测试要求
# ``omniplc.__all__`` 里每个 ``*Client`` 都有 ``omniplc.aio.A<名字>`` 镜像,
# 原生客户端命名是 ``Async<名字>`` 且分属不同实现层,故在此单独导出。
__all__ = [
    # ---- 客户端基类 ----
    "AsyncBaseClient",
    # ---- Modbus 客户端 ----
    "AsyncModbusTcpClient",
    # ---- 三菱 MC 客户端(1E/3E,TCP + UDP)----
    "AsyncMelsecMcTcpClient",
    "AsyncMelsecMcUdpClient",
    # ---- 欧姆龙 FINS 客户端(TCP + UDP)----
    "AsyncOmronFinsTcpClient",
    "AsyncOmronFinsUdpClient",
    # ---- 传输层 ----
    "AsyncBaseTransport",
    "AsyncTcpTransport",
    "AsyncUdpTransport",
]
