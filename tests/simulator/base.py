"""协议模拟器基类:接口约定(实现将在下一阶段提供)。

模拟器仅用于测试:

- 单元/集成测试在 CI 与无硬件环境跑通"连接→读写→断线重连"全链路
- 支持注入异常:返回错误码、延迟响应、超时、断开连接

约定:每个模拟器实现 :class:`BaseSimulator`,在线程中监听回环端口,
测试用例通过 fixture 启动/停止,不依赖真机。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSimulator(ABC):
    """协议模拟器抽象基类。

    子类::

        class ModbusSimulator(BaseSimulator):
            def listen(self): ...      # 绑定 127.0.0.1:0,返回实际端口
            def serve_forever(self): ...
            def shutdown(self): ...

    属性:``host`` 恒为 ``"127.0.0.1"``;``port`` 在 :meth:`listen` 后可用。
    """

    def __init__(self) -> None:
        self.host: str = "127.0.0.1"
        self.port: int = 0

    @abstractmethod
    def listen(self) -> int:
        """绑定回环随机端口,返回实际端口号。"""

    @abstractmethod
    def serve_forever(self) -> None:
        """在调用方线程中服务(通常由测试启动后台线程运行)。"""

    @abstractmethod
    def shutdown(self) -> None:
        """停止服务并释放端口,重复调用保持幂等。"""


def not_implemented_yet() -> None:
    """占位:模拟器实现将在 Modbus 驱动阶段一并交付。"""
    raise NotImplementedError("协议模拟器将在下一阶段实现")
