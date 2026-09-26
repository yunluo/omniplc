"""故障注入回环服务:按连接编排半帧/慢速/错长度/断管等产线级网络异常。

产线网络会丢包、分片、截断、慢速;脚本化假传输(oracle 全应答)全绿并不代表
现场可用。本模块起真 TCP 服务,每个连接交给 ``behavior(sock)`` 注入故障,驱动
走真建链 / 真收包路径,验证失败分类、断线重连与"不被对端拖死"。

用法::

    def behavior(conn):
        recv_request(conn)
        conn.sendall(b"PAR")   # 半帧后关闭
    with chaos_server(behavior) as port:
        client = OpenTcpClient("127.0.0.1", port, delimiter="\\r\\n")
        ...
"""
from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, List

Behavior = Callable[[socket.socket], None]


@contextmanager
def chaos_server(behavior: Behavior) -> Iterator[int]:
    """启动回环 TCP 服务,yield 端口;每个连接交由 ``behavior(sock)`` 注入故障。

    ``behavior`` 抛出的 ``OSError`` 被吞(连接已断属预期);无论是否异常,
    处理后连接都会被关闭。服务在退出上下文时关闭并回收工作线程。
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: List[threading.Thread] = []

    def handle(conn: socket.socket) -> None:
        try:
            behavior(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=handle, args=(conn,), daemon=True)
            worker.start()
            workers.append(worker)

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        stop.set()
        try:
            listener.close()
        except OSError:
            pass
        thread.join(timeout=5)
        for worker in workers:
            worker.join(timeout=5)


def recv_request(conn: socket.socket, size: int = 4096) -> bytes:
    """读一次请求(故障注入前对齐驱动状态;对端已关时返回 b"")。"""
    return conn.recv(size)


def drip(conn: socket.socket, payload: bytes, delay: float, chunk: int = 1) -> None:
    """把 ``payload`` 按 ``chunk`` 字节分片、每片间隔 ``delay`` 秒发出(慢速对端)。"""
    for index in range(0, len(payload), chunk):
        conn.sendall(payload[index:index + chunk])
        time.sleep(delay)
