"""生成欧姆龙 FINS 黄金报文样本(tests/golden/fins_*.json)。

独立最小实现(不复用 omniplc 代码)计算标准帧;帧布局按欧姆龙
FINS 协议规范生成。

用法::

    uv run python tests/golden/generate_fins_samples.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent


def words_be(values: List[int]) -> bytes:
    """字序列 → 大端字节(独立实现)。"""
    return b"".join(value.to_bytes(2, "big") for value in values)


def fins_header(icf: int, dna: int, da1: int, da2: int, sna: int, sa1: int, sa2: int, sid: int) -> bytes:
    """FINS 10 字节帧头(独立实现)。"""
    return bytes([icf, 0x00, 0x02, dna, da1, da2, sna, sa1, sa2, sid])


def area_read_fins(dst: List[int], src: List[int], sid: int, area_code: int, number: int, bit: int, count: int) -> bytes:
    """Area Read 0101 FINS 帧(独立实现)。"""
    return (
        fins_header(0x80, dst[0], dst[1], dst[2], src[0], src[1], src[2], sid)
        + b"\x01\x01"
        + bytes([area_code])
        + number.to_bytes(2, "big")
        + bytes([bit])
        + count.to_bytes(2, "big")
    )


def area_write_fins(
    dst: List[int], src: List[int], sid: int, area_code: int, number: int, bit: int, data: bytes
) -> bytes:
    """Area Write 0102 FINS 帧(独立实现)。"""
    return (
        fins_header(0x80, dst[0], dst[1], dst[2], src[0], src[1], src[2], sid)
        + b"\x01\x02"
        + bytes([area_code])
        + number.to_bytes(2, "big")
        + bytes([bit])
        + (len(data) // 2 if area_code & 0x80 else len(data)).to_bytes(2, "big")
        + data
    )


def area_read_response(dst: List[int], src: List[int], sid: int, end_code: int, data: bytes) -> bytes:
    """Area Read 响应 FINS 帧(独立实现)。"""
    return (
        fins_header(0xC0, src[0], src[1], src[2], dst[0], dst[1], dst[2], sid)
        + b"\x01\x01"
        + end_code.to_bytes(2, "big")
        + data
    )


def area_write_response(dst: List[int], src: List[int], sid: int, end_code: int) -> bytes:
    """Area Write 响应 FINS 帧(独立实现)。"""
    return (
        fins_header(0xC0, src[0], src[1], src[2], dst[0], dst[1], dst[2], sid)
        + b"\x01\x02"
        + end_code.to_bytes(2, "big")
    )


def tcp_wrap(fins_frame: bytes, command: int) -> bytes:
    """FINS/TCP 封帧(独立实现)。"""
    body = command.to_bytes(4, "big") + (0).to_bytes(4, "big") + fins_frame
    return b"FINS" + len(body).to_bytes(4, "big") + body


def handshake_request(local_node: int) -> bytes:
    """节点分配握手请求(独立实现,总长 20)。"""
    return (
        b"FINS"
        + (12).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00"
        + bytes([local_node])
    )


def handshake_response(local_node: int, plc_node: int) -> bytes:
    """节点分配握手响应(独立实现,总长 24)。"""
    return (
        b"FINS"
        + (16).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + b"\x00\x00\x00"
        + bytes([local_node])
        + b"\x00\x00\x00"
        + bytes([plc_node])
    )


def main() -> None:
    """生成全部 FINS 样本文件。"""
    samples: List[Dict[str, Any]] = []

    def add(
        filename: str,
        name: str,
        request: bytes,
        response: bytes,
        expect: Dict[str, Any],
        setup: Dict[str, Any],
    ) -> None:
        samples.append(
            {
                "file": filename,
                "name": name,
                "request_hex": request.hex(),
                "response_hex": response.hex(),
                "expect": expect,
                "setup": setup,
            }
        )

    dst = [0, 5, 0]
    src = [0, 10, 0]
    add(
        "fins_udp_area_read_001",
        "UDP 区域读:D100 起 2 字,值 20/10",
        area_read_fins(dst, src, 1, 0x82, 100, 0, 2),
        area_read_response(dst, src, 1, 0, words_be([20, 10])),
        {"values": [20, 10]},
        {"transport": "udp", "destination": dst, "source": src, "sid": 1},
    )
    add(
        "fins_tcp_area_read_001",
        "TCP 区域读:D100 起 2 字(含 FINS/TCP 头)",
        tcp_wrap(area_read_fins(dst, src, 1, 0x82, 100, 0, 2), 2),
        tcp_wrap(area_read_response(dst, src, 1, 0, words_be([20, 10])), 2),
        {"values": [20, 10]},
        {"transport": "tcp", "destination": dst, "source": src, "sid": 1},
    )
    add(
        "fins_udp_area_write_001",
        "UDP 区域写:W10 起 2 字,值 0x1234/0x5678",
        area_write_fins(dst, src, 2, 0xB1, 10, 0, words_be([0x1234, 0x5678])),
        area_write_response(dst, src, 2, 0),
        {"echo": True},
        {"transport": "udp", "destination": dst, "source": src, "sid": 2},
    )
    add(
        "fins_udp_error_001",
        "UDP 区域读返回结束码 0x0001",
        area_read_fins(dst, src, 1, 0x82, 100, 0, 2),
        area_read_response(dst, src, 1, 1, b""),
        {"error_code": 1},
        {"transport": "udp", "destination": dst, "source": src, "sid": 1},
    )
    add(
        "fins_tcp_handshake_001",
        "FINS/TCP 节点分配握手(本地 0 → 分配 11,PLC 节点 5)",
        handshake_request(0),
        handshake_response(11, 5),
        {"local_node": 11, "plc_node": 5},
        {"transport": "tcp"},
    )

    for sample in samples:
        filename = sample.pop("file")
        path = HERE / "{}.json".format(filename)
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print("written:", path)


if __name__ == "__main__":
    main()
