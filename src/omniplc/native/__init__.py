"""原生 asyncio 客户端(零第三方依赖,Python 3.7 起)。

与 :mod:`omniplc.aio` 的区别(两套异步并存,按场景选):

==================  ==================================  ==================================
维度                :mod:`omniplc.aio`(线程池包装)     `omniplc.native`(本包,原生)
==================  ==================================  ==================================
实现                同步 I/O + 单线程 ``ThreadPoolExecutor``  原生 ``asyncio`` 协议栈
覆盖面              **全部**协议(自动镜像)                 首批见下(逐批补齐)
属性读取            抢事务锁,最长阻塞 ``receive_timeout``  直接读字段,不阻塞事件循环
取消                ``wait_for`` 只放弃等待,事务照跑完    真中断(取消按"是否已发请求"决定拆连)
批量/扩展方法        与同步层同面                          首批不含(见下)
==================  ==================================  ==================================

**首批覆盖**:Modbus TCP、三菱 MC 1E/3E(TCP/UDP)、欧姆龙 FINS(TCP/UDP),
能力面为单点读/写 + 类型化方法 + 字符串 + 点位表;批量
(``read_many``/``read_batch``)与各家扩展方法(FC 22/23/43、MC 0406 批量读、
FINS 0104 多存储区读等)留后续批次,按需从 :mod:`omniplc.aio` 或同步客户端过渡。

**实例绑定一个事件循环**:客户端在哪个循环里用就在哪个循环里建(事务锁按
首次使用时的循环惰性创建,模块级构造再 ``asyncio.run`` 也可正常工作);
跨循环/跨线程共享同一实例不支持。

:example::

    import asyncio
    from omniplc.native import AsyncModbusTcpClient

    async def main() -> None:
        async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
            ok, value = await client.read_float("hr0")

    asyncio.run(main())
"""
from __future__ import annotations

from .base import AsyncBaseClient
from .modbus import AsyncModbusTcpClient
from .transport import AsyncBaseTransport, AsyncTcpTransport, AsyncUdpTransport

# 本包公开面:**不进** ``omniplc.__all__``——根包的镜像守卫测试要求
# ``omniplc.__all__`` 里每个 ``*Client`` 都有 ``omniplc.aio.A<名字>`` 镜像,
# 原生客户端命名是 ``Async<名字>`` 且分属不同实现层,故在此单独导出。
__all__ = [
    # ---- 客户端基类 ----
    "AsyncBaseClient",
    # ---- Modbus 客户端 ----
    "AsyncModbusTcpClient",
    # ---- 传输层 ----
    "AsyncBaseTransport",
    "AsyncTcpTransport",
    "AsyncUdpTransport",
]
