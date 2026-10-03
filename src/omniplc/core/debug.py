"""全局报文调试开关与报文黑匣子:一行开启,所有协议客户端输出/留存报文。

用法::

    import omniplc

    omniplc.set_debug(True)   # 之后所有客户端的收发报文都输出
    ...
    omniplc.set_debug(False)  # 关闭(默认关闭)

输出走 :mod:`logging`(记录器名 ``omniplc.debug``,DEBUG 级),便于接入
应用既有日志体系;开启时若应用尚未配置任何日志处理器,则自动向
``omniplc.debug`` 挂一个 stderr 处理器,保证开箱即用。

**报文黑匣子**(独立于 :func:`set_debug`,默认关闭)::

    omniplc.set_frame_recorder(True)        # 常驻留存最近 1000 帧
    ...
    for rec in omniplc.recorded_frames():   # 故障后取现场
        print(rec.at, rec.direction, rec.label, rec.data.hex())

与实时打印的区别:黑匣子**只存不打印**——生产环境不便开
:func:`set_debug`(刷屏/性能)时,常驻一个小容量环形缓冲,故障发生后
取最近报文做现场比对。挂点与实时日志同在传输层收发口
(:func:`log_frame`),走线型协议全量覆盖;会话型(OPC-UA / MX)无字节
流,不进黑匣子。

两类协议的输出口径:

- 走线型(Modbus/MC/FINS/Host Link/TOYOPUC/EtherNet/IP/CIP/SR/通用 TCP,
  TCP/UDP/串口):在传输层统一挂钩,``send``/``recv`` 的原始字节即
  请求/响应报文(TCP 应答分多段到达时按段输出)
- 会话型(OPC-UA / MX Component):无字节流,输出操作级日志
  (读写了哪个节点/变量、按什么类型、返回什么值)
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from typing import List, NamedTuple, Optional

from .constants import (
    DEBUG_MAX_DUMP_BYTES,
    FRAME_RECORDER_DEFAULT_CAPACITY,
    FRAME_RECORDER_MAX_CAPACITY,
)
from .i18n import _

LOGGER_NAME = "omniplc.debug"
"""报文日志使用的记录器名(接管输出时按此配置)。"""

SEND_MARK = "→ 发送"
RECV_MARK = "← 接收"

_logger = logging.getLogger(LOGGER_NAME)
_enabled = False

# 报文黑匣子:deque(maxlen) 的 append 是原子操作(GIL 下无撕裂),
# 记录路径不加锁;锁只保护 set_frame_recorder 的重建/关闭与快照拷贝——
# 替换/关闭瞬间另一线程可能仍持旧 deque 引用 append,丢失/残留一条属
# 观测级竞态(与 Monitor stats 无锁快照同口径)。
_recorder: Optional[deque] = None
_recorder_lock = threading.Lock()


class FrameRecord(NamedTuple):
    """单条黑匣子报文记录(:func:`recorded_frames` 的元素,不可变)。

    :ivar at: 记录时刻(墙钟秒,``time.time()``——现场排障按对表时间查)
    :ivar direction: 方向标记(常量 ``SEND_MARK``/``RECV_MARK``)
    :ivar label: 走线标识,如 ``tcp://192.168.0.10:502``
    :ivar data: 原始报文字节(展示用 :func:`format_hex` 转储)
    """

    at: float
    direction: str
    label: str
    data: bytes


def set_frame_recorder(
    enabled: bool, capacity: int = FRAME_RECORDER_DEFAULT_CAPACITY
) -> None:
    """开启/关闭报文黑匣子(进程级开关,对所有客户端实例生效)。

    开启后走线报文(传输层 ``send``/``recv``)按序留存最近 ``capacity``
    帧,**只存不打印**;关闭即丢弃已留存内容并释放缓冲。

    - 同容量重复开启**不清空**历史(幂等);变更容量才重建缓冲;
      只清空不关停用 :func:`clear_recorded_frames`;
    - 与 :func:`set_debug` 相互独立,可单独开启(黑匣子常驻、日志关闭
      是生产环境的推荐组合);
    - 记录路径无锁(:class:`~collections.deque` append 原子),替换/关闭
      瞬间的极端竞态最多丢/串一条,观测级。

    :param enabled: True = 开始留存;False = 关闭并清空
    :param capacity: 环形容量(帧数),1~100000
    :raises ValueError: capacity 非整数或越界
    """
    global _recorder
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise ValueError(_("capacity 必须为整数,收到:{!r}").format(capacity))
    if not 1 <= capacity <= FRAME_RECORDER_MAX_CAPACITY:
        raise ValueError(
            _("capacity 必须在 1~{} 之间,收到:{}").format(
                FRAME_RECORDER_MAX_CAPACITY, capacity
            )
        )
    with _recorder_lock:
        if enabled:
            if _recorder is None or _recorder.maxlen != capacity:
                _recorder = deque(maxlen=capacity)
        else:
            _recorder = None


def frame_recorder_enabled() -> bool:
    """当前是否已开启报文黑匣子(内部与测试使用)。"""
    return _recorder is not None


def recorded_frames() -> List[FrameRecord]:
    """取黑匣子留存的最近报文(快照拷贝,先旧后新)。

    未开启时返回空列表;返回后黑匣子继续滚动,不影响已取快照。

    :return: :class:`FrameRecord` 列表(墙钟时间升序)
    """
    with _recorder_lock:
        if _recorder is None:
            return []
        return list(_recorder)


def clear_recorded_frames() -> None:
    """清空黑匣子已留存内容(不改变开关状态)。"""
    with _recorder_lock:
        if _recorder is not None:
            _recorder.clear()


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

    黑匣子开启时同步留存(append 原子无锁),与实时日志相互独立——
    日志关、黑匣子开时只留存不输出。

    :param label: 走线标识,如 ``tcp://192.168.0.10:502``
    :param direction: 方向标记(常量 ``SEND_MARK``/``RECV_MARK``)
    :param data: 报文字节
    """
    recorder = _recorder
    if recorder is not None:
        recorder.append(FrameRecord(time.time(), direction, label, data))
    if not _enabled:
        return
    _logger.debug("%s %s %dB: %s", label, direction, len(data), format_hex(data))


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


def log_warning(label: str, message: str, *args: object) -> None:
    """输出一条不受调试门控的 WARNING 日志(传输层异常事件使用)。

    与 :func:`log_op` 的区别:UDP 截断 / 帧异常等"链路收到坏数据但继续走
    流程"的事件必须**总能被看到**——调用方应用不一定开了
    :func:`set_debug`,但此类事件是排查 bug 的关键信号。走 WARNING 级别,
    走 :data:`LOGGER_NAME` 记录器(与 :func:`log_op` 共用,应用按 logger
    统一接管即可)。

    :param label: 走线标识,如 ``udp://192.168.0.10:9600``
    :param message: 描述模板(如 ``"UDP 数据报截断:实收 %dB,缓冲 %dB"``)
    :param args: 模板参数
    """
    if args:
        message = message % args
    _logger.warning("%s %s", label, message)


def format_hex(data: bytes) -> str:
    """十六进制转储(大写、空格分隔;超长截断并注明)。

    全库统一的报文十六进制展示口径:日志转储(:func:`log_frame`)与
    协议层错误信息(坏帧/校验失败时把**收到的原始数据**带进异常文本,
    便于现场比对抓包)共用。

    截断上限见 :data:`~omniplc.core.constants.DEBUG_MAX_DUMP_BYTES`;
    协议帧(Modbus 最长 260B、MC/FINS 均为 KB 级)远小于该上限,实际
    不会截断,仅防异常路径上传入超大缓冲刷屏。

    :param data: 待转储字节
    :return: 形如 ``"01 03 00 00 ..."`` 的文本(超长带「仅转储前 NB」尾注)
    """
    dumped = data[:DEBUG_MAX_DUMP_BYTES]
    text = " ".join(f"{byte:02X}" for byte in dumped)
    if len(data) > len(dumped):
        text += " …(仅转储前 {}B,共 {}B)".format(DEBUG_MAX_DUMP_BYTES, len(data))
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
