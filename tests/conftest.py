"""pytest 全局 fixture:本机回环 echo 服务(TCP/UDP),供传输层测试。"""
from __future__ import annotations

import socket
import socketserver
import threading
from typing import Iterator

import pytest


class _TcpEchoHandler(socketserver.BaseRequestHandler):
    """把收到的字节原样发回(粘包不做处理,测试只验证字节往返)。"""

    def handle(self) -> None:
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                self.request.sendall(data)
        except OSError:
            pass


@pytest.fixture
def tcp_echo_port() -> Iterator[int]:
    """启动一个 TCP echo 服务,yield 其端口。"""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TcpEchoHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def udp_echo_port() -> Iterator[int]:
    """启动一个 UDP echo 服务,yield 其端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.1)
    stop_event = threading.Event()

    def echo_loop() -> None:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            sock.sendto(data, addr)

    thread = threading.Thread(target=echo_loop, daemon=True)
    thread.start()
    try:
        yield int(sock.getsockname()[1])
    finally:
        stop_event.set()
        thread.join(timeout=5)
        sock.close()
