"""全局报文调试开关:一行开启,所有协议客户端输出请求/响应报文。

用法::

    import omniplc

    omniplc.set_debug(True)   # 之后所有客户端的收发报文都输出
    ...
    omniplc.set_debug(False)  # 关闭(默认关闭)

输出走 :mod:`logging`(记录器名 ``omniplc.debug``,DEBUG 级),便于接入
应用既有日志体系;开启时若应用尚未配置任何日志处理器,则自动向
``omniplc.debug`` 挂一个 stderr 处理器,保证开箱即用。

两类协议的输出口径:

- 走线型(Modbus/MC/FINS/Host Link/TOYOPUC/EtherNet/IP/CIP/SR/通用 TCP,
  TCP/UDP/串口):在传输层统一挂钩,``send``/``recv`` 的原始字节即
  请求/响应报文(TCP 应答分多段到达时按段输出)
- 会话型(OPC-UA / ADS / MX Component):无字节流,输出操作级日志
  (读写了哪个节点/变量、按什么类型、返回什么值)
"""
from __future__ import annotations

import logging
import sys

LOGGER_NAME = "omniplc.debug"
"""报文日志使用的记录器名(接管输出时按此配置)。"""

SEND_MARK = "→ 发送"
RECV_MARK = "← 接收"

_MAX_DUMP_BYTES = 4096
"""单条日志最多转储的字节数(超出截断,防大块读写刷屏)。"""

_logger = logging.getLogger(LOGGER_NAME)
_enabled = False


def set_debug(enabled: bool) -> None:
    """开启/关闭全局报文调试输出(进程级开关,对所有客户端实例生效)。

    同步与异步客户端共用同一开关(异步镜像在单工作线程内驱动同步
    实例,日志自然汇聚到同一条流)。

    :param enabled: True = 输出请求/响应报文;False = 关闭(默认)
    """
    global _enabled
    _enabled = bool(enabled)
    if _enabled:
        _attach_default_handler(_logger, logging.getLogger())
        _logger.setLevel(logging.DEBUG)
    else:
        _logger.setLevel(logging.NOTSET)


def debug_enabled() -> bool:
    """当前是否已开启报文调试(内部与测试使用)。"""
    return _enabled


def log_frame(label: str, direction: str, data: bytes) -> None:
    """输出一条走线报文:方向 + 长度 + 十六进制转储(内部使用)。

    :param label: 走线标识,如 ``tcp://192.168.0.10:502``
    :param direction: 方向标记(常量 ``SEND_MARK``/``RECV_MARK``)
    :param data: 报文字节
    """
    if not _enabled:
        return
    _logger.debug("%s %s %dB: %s", label, direction, len(data), _format_hex(data))


def log_op(label: str, message: str, *args: object) -> None:
    """输出一条会话型操作/连接事件日志(内部使用)。

    ``message`` 为 %-风格模板,``args`` 仅在调试开启后才格式化——
    关闭时调用方零格式化成本(与 :func:`log_frame` 口径一致)。

    :param label: 走线/会话标识,如 ``opc.tcp://192.168.0.10:4840``
    :param message: 操作描述模板(如 ``"读 %s → %r"``)
    :param args: 模板参数(可省略;省略时 ``message`` 原样输出)
    """
    if not _enabled:
        return
    if args:
        message = message % args
    _logger.debug("%s %s", label, message)


def _format_hex(data: bytes) -> str:
    """十六进制转储(大写、空格分隔;超长截断并注明,内部函数)。"""
    dumped = data[:_MAX_DUMP_BYTES]
    text = " ".join("{:02X}".format(byte) for byte in dumped)
    if len(data) > len(dumped):
        text += " …(仅转储前 {}B,共 {}B)".format(_MAX_DUMP_BYTES, len(data))
    return text


def _attach_default_handler(logger: logging.Logger, root: logging.Logger) -> None:
    """应用未配置任何日志时挂一个 stderr 处理器,保证开箱即用(内部函数)。

    本记录器或根记录器已有处理器时不挂(交由应用日志体系接管,
    记录沿 propagate 到根);重复调用不会重复挂。
    """
    if logger.handlers or root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
