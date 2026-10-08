"""UDP 传输实现。"""

from __future__ import annotations

import socket
import sys
from typing import Optional

from .base import BaseTransport
from ..core.debug import RECV_MARK, SEND_MARK, log_frame, log_op, log_warning
from ..core.errors import DeviceError, TransportClosedError, TransportTimeoutError
from ..core.i18n import _

# Windows ``recv`` 对超长 UDP 报文抛 WSAEMSGSIZE(errno 10040);库捕获后
# 与 POSIX 截断分支统一处理(见 :meth:`UdpTransport.recv`)。诊断码取负值,
# 避开真实协议错误码空间(基类据此不计入 device_error_count)。
_WSAEMSGSIZE_ERRNO = 10040

# UDP 截断探测手段按平台:POSIX ``recv_into`` + ``MSG_TRUNC`` 暴露真实
# 报文字节数(POSIX ``recv`` 本身静默截断);Windows 下 ``recv`` 直接抛
# ``WSAEMSGSIZE``,无需 ``MSG_TRUNC``(该组合在 Windows 上抛
# WSAEOPNOTSUPP, WinError 10045)。
_SUPPORTS_MSG_TRUNC = sys.platform != "win32" and hasattr(socket, "MSG_TRUNC")

# 排空循环的数据报个数上限:陈旧帧防护的保险丝。正常现场缓冲里只有
# 上一轮迟到的个别响应;对端若以线速持续灌包(异常形态),无上限循环
# 会长时间占用事务锁,到量即停并记 WARNING。
_UDP_DRAIN_MAX_DATAGRAMS = 64


class UdpTransport(BaseTransport):
    """UDP 传输:面向 Modbus UDP、MC over UDP、FINS/UDP。

    - 采用"已连接 UDP"语义:``connect()`` 固定对端,之后直接 send/recv
    - :meth:`recv` 返回**一条数据报**(最长 ``size`` 字节,超出截断),
      即一次收发对应一个协议帧
    - UDP 无连接概念,``connect()`` 只做本地套接字初始化,不会失败于对端
    - 接收超时抛 :class:`omniplc.core.errors.TransportTimeoutError`
      (DeviceError 子类):数据报整收无残留字节,按"链路完好不断线"
    - :attr:`receive_timeout` 修改后**立即作用于已连接 socket**
    """

    datagram: bool = True
    """一问一答一数据报:recv 整包,协议层按帧内长度字段校验。"""

    def __init__(self, ip_address: str, port: int) -> None:
        """初始化 UDP 传输。

        :param ip_address: 目标 IP 或主机名
        :param port: 目标端口
        """
        super().__init__()
        self._ip_address = ip_address
        self._port = port
        self._socket: Optional[socket.socket] = None
        self._debug_label = f"udp://{ip_address}:{port}"

    @BaseTransport.receive_timeout.setter  # type: ignore[attr-defined]
    def receive_timeout(self, seconds: float) -> None:
        """单次收发超时(秒);已连接时立即下发到 socket。"""
        BaseTransport.receive_timeout.fset(self, seconds)  # type: ignore[attr-defined]
        sock = self._socket
        if sock is not None:
            sock.settimeout(self._receive_timeout)

    def connect(self) -> None:
        """初始化 UDP 套接字并固定对端。

        :raises OSError: 地址解析失败或端口绑定错误
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self._connect_timeout)
            sock.connect((self._ip_address, self._port))
        except OSError:
            # connect 失败时清理局部套接字,避免惰性重连反复泄漏 FD
            sock.close()
            raise
        sock.settimeout(self._receive_timeout)
        self._socket = sock
        log_op(self._debug_label, "已连接")

    def close(self) -> None:
        """关闭 UDP 套接字,幂等。"""
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
            log_op(self._debug_label, "已断开")

    def send(self, data: bytes) -> None:
        """发送一条数据报到固定对端。

        :raises TransportClosedError: 未初始化
        :raises OSError: 发送失败或超时
        """
        sock = self._require_socket()
        log_frame(self._debug_label, SEND_MARK, data)
        sock.send(data)

    def recv(self, size: int) -> bytes:
        """接收一条数据报。

        UDP 报文截断**跨平台同口径**:库以 WARNING 日志输出一行带
        "缓冲 size / 实收字节"的诊断,随后抛
        :class:`DeviceError`(``code`` 为诊断性负码 ``-10040``,基类按
        ``DEVICE`` 分类且**不计入** ``device_error_count``——本地缓冲
        配置问题非 PLC 报错,也不触发重连):

        - **POSIX**(Linux/macOS):``recv_into`` + ``MSG_TRUNC`` 拿到真实
          报文字节数,超出缓冲即按上述口径处理(POSIX ``recv`` 本身会
          静默截断,``MSG_TRUNC`` 是探测真长的唯一手段;平台注记
          review-1020 P3-20⑤:``MSG_TRUNC`` 返回真长在 Linux 确证,
          macOS/BSD 待核——即便个别平台不回报真长,协议层长度校验仍会
          兜底拒坏帧,最坏情况退化为"静默截断被当完整帧",由帧级校验
          暴露而非本层)。
        - **Windows**:``recv`` 对超长报文抛 ``WSAEMSGSIZE``(errno
          10040),捕获后同口径处理。

        :param size: 缓冲上限(超出按截断故障处理)
        :raises TransportClosedError: 未初始化
        :raises TransportTimeoutError: 接收超时(不断线语义)
        :raises DeviceError: 报文超过缓冲时抛(code=-10040,链路正常)
        :raises OSError: 其他 OS 层错误
        """
        sock = self._require_socket()
        if _SUPPORTS_MSG_TRUNC:
            buffer = bytearray(size)
            try:
                datagram_size = sock.recv_into(buffer, size, socket.MSG_TRUNC)
            except socket.timeout as exc:
                raise TransportTimeoutError(
                    _("UDP 接收超时({}s)").format(self._receive_timeout), 0
                ) from exc
            if datagram_size > size:
                # 与 Windows 分支同口径:截断是故障,响亮抛错而非返回
                # 残缺帧(跨平台行为一致,调用方不必按平台分支处理)
                log_warning(
                    self._debug_label,
                    "UDP 数据报截断:实收 %dB,缓冲 %dB(超出 %dB 已丢,检查协议层 size 或对端报文)",
                    datagram_size,
                    size,
                    datagram_size - size,
                )
                raise DeviceError(
                    _("UDP 报文超过缓冲({}B,实收 {}B),链路正常(对端报文超长)").format(
                        size, datagram_size
                    ),
                    code=-_WSAEMSGSIZE_ERRNO,
                )
            frame = bytes(buffer[:datagram_size])
            log_frame(self._debug_label, RECV_MARK, frame)
            return frame
        # Windows:recv 对超长报文抛 WSAEMSGSIZE(无静默截断);捕获并
        # 转 DeviceError,这样基类 last_error_category = DEVICE(链路正常、
        # 对端报文超长,不断线),与真断线区分
        try:
            frame = sock.recv(size)
        except socket.timeout as exc:
            raise TransportTimeoutError(
                _("UDP 接收超时({}s)").format(self._receive_timeout), 0
            ) from exc
        except OSError as exc:
            if getattr(exc, "errno", None) == _WSAEMSGSIZE_ERRNO:
                log_warning(
                    self._debug_label,
                    "UDP 数据报超长(WinError 10040 WSAEMSGSIZE):缓冲 %dB,检查协议层 size 或对端报文",
                    size,
                )
                # 与 POSIX 截断分支同口径:诊断性负码(负值避开真实协议
                # 错误码空间),不计入 device_error_count——本地缓冲配置
                # 问题不应冒充"PLC 返回错误码"
                raise DeviceError(
                    _("UDP 报文超过缓冲({}B),链路正常(对端报文超长)").format(size),
                    code=-_WSAEMSGSIZE_ERRNO,
                ) from exc
            raise
        log_frame(self._debug_label, RECV_MARK, frame)
        return frame

    def drain(self) -> int:
        """排空接收缓冲中的陈旧数据报,返回排掉个数(陈旧帧防护)。

        非阻塞循环 ``recv`` 到缓冲见底(:class:`BlockingIOError`/超时即
        停);排掉的帧照常走 ``log_frame``(RECV_MARK)——黑匣子与调试
        日志里能看到"排掉了什么",现场排障时迟到帧不再是隐形因素。
        超长陈旧报文(POSIX ``MSG_TRUNC`` 探测 / Windows WSAEMSGSIZE)
        在排空语境下**照排不报**——丢弃正是本方法的目的,不能让陈旧帧
        的超长属性炸掉本轮事务。

        :raises TransportClosedError: 未初始化
        :raises OSError: 非"缓冲见底/报文超长"的 OS 层错误
        """
        sock = self._require_socket()
        drained = 0
        # 临时切非阻塞:recv 立即返回或抛 BlockingIOError,不引入等待;
        # 结束后恢复超时模式(与 __init__/connect 的 settimeout 口径一致)
        sock.setblocking(False)
        try:
            while drained < _UDP_DRAIN_MAX_DATAGRAMS:
                try:
                    stale = sock.recv(65535)
                except (BlockingIOError, socket.timeout):
                    break
                except OSError as exc:
                    if getattr(exc, "errno", None) == _WSAEMSGSIZE_ERRNO:
                        # Windows:超长陈旧报文,丢弃即目的,计入继续
                        drained += 1
                        log_warning(
                            self._debug_label,
                            "排空时丢弃超长陈旧数据报(缓冲 65535B,WinError 10040)",
                        )
                        continue
                    raise
                drained += 1
                log_frame(self._debug_label, RECV_MARK, stale)
        finally:
            sock.settimeout(self._receive_timeout)
        if drained >= _UDP_DRAIN_MAX_DATAGRAMS:
            log_warning(
                self._debug_label,
                "接收缓冲排空达上限(%d 个数据报仍在灌入),截断本次排空",
                _UDP_DRAIN_MAX_DATAGRAMS,
            )
        if drained:
            log_op(self._debug_label, "已排空 %d 个陈旧数据报", drained)
        return drained

    def _require_socket(self) -> socket.socket:
        """取当前 socket,未初始化则抛出。"""
        if self._socket is None:
            raise TransportClosedError(_("UDP 未初始化,请先调用 connect()"))
        return self._socket
