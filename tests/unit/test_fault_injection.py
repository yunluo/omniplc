"""故障注入测试:真回环服务注入半帧/慢速/错长度/断管 + 断线惰性重连。

对应 `docs/review.md` §4.2:脚本化假传输(全应答)全绿不等于产线可用;
本组用例走真建链 / 真收包,验证失败干净(不挂死、正确分类、该拆连就拆连、
能自动重连),覆盖驱动走线层(TCP)的现场级异常。
"""
from __future__ import annotations

import socket
import struct
import time

from omniplc import MelsecMcTcpClient, ModbusTcpClient, OpenTcpClient
from omniplc.core.errors import ErrorCategory

from chaos import chaos_server, drip, recv_request


# ----------------------------------------------------------------------
# OpenTcp(通用分隔符成帧):半帧 / 慢速 / 静默
# ----------------------------------------------------------------------


def test_opentcp_partial_frame_then_close() -> None:
    """对端发半帧后关闭:失败并标记断线(不误判为完整帧)。"""

    def behavior(conn: socket.socket) -> None:
        conn.sendall(b"PAR")  # 无分隔符即截断;receive() 不发请求
        conn.close()

    with chaos_server(behavior) as port:
        client = OpenTcpClient("127.0.0.1", port, delimiter="\r\n")
        client.receive_timeout = 1.0
        client.connect()
        ok, raw = client.receive()
        assert ok is False and raw is None
        assert client.connected is False  # 断管 → 拆连


def test_opentcp_drip_fed_frame_assembles() -> None:
    """慢速对端逐字节滴帧:跨分片拼接仍能成帧(整事务预算内)。"""

    def behavior(conn: socket.socket) -> None:
        recv_request(conn)
        drip(conn, b"HELLO\r\n", delay=0.02, chunk=1)

    with chaos_server(behavior) as port:
        client = OpenTcpClient("127.0.0.1", port, delimiter="\r\n")
        client.receive_timeout = 3.0
        client.connect()
        ok, text = client.transact_text("HI")
        assert ok is True and text == "HELLO"


def test_opentcp_stall_times_out_without_hanging() -> None:
    """对端静默:按 receive_timeout 失败,不被拖死;超时不断线(OpenTcp 口径)。"""

    def behavior(conn: socket.socket) -> None:
        time.sleep(1.0)  # 静默不回

    with chaos_server(behavior) as port:
        client = OpenTcpClient("127.0.0.1", port, delimiter="\r\n")
        client.receive_timeout = 0.3
        client.connect()
        started = time.monotonic()
        ok, raw = client.receive()
        elapsed = time.monotonic() - started
        assert ok is False and raw is None
        assert elapsed < 0.8  # 远小于对端 1.0s 静默
        assert client.last_error_category is ErrorCategory.DEVICE  # 超时归 DeviceError
        assert client.connected is True  # 链路完好,不断线


# ----------------------------------------------------------------------
# Modbus TCP:错长度头 / 断管后惰性重连
# ----------------------------------------------------------------------


def _mbap_read_response(request: bytes, value: int) -> bytes:
    """回显请求事务号/站号,构造 FC03 单寄存器读应答(测试脚手架)。"""
    unit = request[6:7]
    pdu = b"\x03\x02" + value.to_bytes(2, "big")
    return request[0:2] + b"\x00\x00" + (1 + len(pdu)).to_bytes(2, "big") + unit + pdu


def test_modbus_wrong_length_header_does_not_hang() -> None:
    """MBAP 声明正文但一直不来:按超时失败,不无限等。"""

    def behavior(conn: socket.socket) -> None:
        recv_request(conn)
        conn.sendall(struct.pack(">HHHB", 1, 0, 100, 1))  # length=100 合法但无正文
        time.sleep(2.0)

    with chaos_server(behavior) as port:
        client = ModbusTcpClient("127.0.0.1", port, station=1)
        client.receive_timeout = 0.3
        client.connect()
        started = time.monotonic()
        ok, value = client.read_ushort("hr0")
        elapsed = time.monotonic() - started
        assert ok is False and value is None
        assert elapsed < 1.5


def test_modbus_drop_then_lazy_reconnect() -> None:
    """首连被断(读到 EOF)→ 标记断开;下一次读惰性重连并成功。"""
    connections = {"n": 0}

    def behavior(conn: socket.socket) -> None:
        request = recv_request(conn)
        connections["n"] += 1
        if connections["n"] == 1:
            return  # 直接关(context 收尾 close):本轮无应答
        conn.sendall(_mbap_read_response(request, 0x1234))

    with chaos_server(behavior) as port:
        client = ModbusTcpClient("127.0.0.1", port, station=1)
        client.receive_timeout = 1.0
        client.connect()
        assert client.read_ushort("hr0") == (False, None)
        assert client.connected is False
        assert client.read_ushort("hr0") == (True, 0x1234)  # 惰性重连
        assert client.connected is True
        assert connections["n"] == 2


# ----------------------------------------------------------------------
# 三菱 MC 3E:半帧头后关闭
# ----------------------------------------------------------------------


def test_melsec_partial_header_close_disconnects() -> None:
    """3E 只发 4 字节头即关闭:失败并拆连(不把残头当帧)。"""

    def behavior(conn: socket.socket) -> None:
        recv_request(conn)
        conn.sendall(b"\xd0\x00\x00\xff")  # 3E 响应头应 9 字节
        conn.close()

    with chaos_server(behavior) as port:
        client = MelsecMcTcpClient("127.0.0.1", port)
        client.receive_timeout = 1.0
        client.connect()
        assert client.read_ushort("D100") == (False, None)
        assert client.connected is False
