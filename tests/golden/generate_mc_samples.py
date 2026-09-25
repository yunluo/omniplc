"""生成三菱 MC 黄金报文样本(tests/golden/mc_*.json)。

独立最小实现(不复用 omniplc 代码)计算标准帧;帧布局对照
SLMP 参考规范(SH-080956)。

用法::

    uv run python tests/golden/generate_mc_samples.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent


def words_le(values: List[int]) -> bytes:
    """字序列 → 小端字节(独立实现)。"""
    return b"".join(value.to_bytes(2, "little") for value in values)


def qna_request(
    frame: str,
    serial: int,
    network: int,
    pc: int,
    timer: int,
    is_write: bool,
    is_bit: bool,
    number: int,
    code: int,
    points: int,
    payload: bytes = b"",
) -> bytes:
    """3E/4E 请求帧(独立实现)。"""
    command = b"\x01\x14" if is_write else b"\x01\x04"
    subcommand = (1 if is_bit else 0).to_bytes(2, "little")
    core = (
        command
        + subcommand
        + number.to_bytes(3, "little")
        + bytes([code])
        + points.to_bytes(2, "little")
        + payload
    )
    body = (
        bytes([network, pc])
        + b"\xff\x03\x00"
        + (2 + len(core)).to_bytes(2, "little")
        + timer.to_bytes(2, "little")
        + core
    )
    if frame == "4E":
        return b"\x54\x00" + serial.to_bytes(2, "little") + b"\x00\x00" + body
    return b"\x50\x00" + body


def qna_response(frame: str, serial: int, end_code: int, data: bytes = b"") -> bytes:
    """3E/4E 响应帧(独立实现)。"""
    if frame == "4E":
        head = b"\xd4\x00" + serial.to_bytes(2, "little") + b"\x00\x00" + b"\x00\xff\xff\x03\x00"
    else:
        head = b"\xd0\x00" + b"\x00\xff\xff\x03\x00"
    return head + (2 + len(data)).to_bytes(2, "little") + end_code.to_bytes(2, "little") + data


def one_e_request(
    pc: int, timer: int, subtitle: int, number: int, code: int, points: int, payload: bytes = b""
) -> bytes:
    """1E 请求帧(独立实现)。"""
    return (
        bytes([subtitle, pc])
        + timer.to_bytes(2, "little")
        + number.to_bytes(2, "little")
        + b"\x00\x00"
        + code.to_bytes(2, "little")
        + points.to_bytes(2, "little")
        + payload
    )


def one_e_response(subtitle: int, end_code: int, data: bytes = b"") -> bytes:
    """1E 响应帧(独立实现)。"""
    return bytes([subtitle + 0x80, end_code]) + data


def main() -> None:
    """生成全部 MC 样本文件。"""
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

    add(
        "mc_3e_batch_read_001",
        "3E 成批读:D100 起 2 字,值 20/10",
        qna_request("3E", 0, 0, 0xFF, 10, False, False, 100, 0xA8, 2),
        qna_response("3E", 0, 0, words_le([20, 10])),
        {"values": [20, 10]},
        {"frame": "3E", "network": 0, "station": 255, "monitoring_timer": 10},
    )
    add(
        "mc_3e_bit_read_001",
        "3E 成批读(位):M10 起 3 点,ON/OFF/ON",
        qna_request("3E", 0, 0, 0xFF, 10, False, True, 10, 0x90, 3),
        qna_response("3E", 0, 0, b"\x10\x10"),
        {"values": [1, 0, 1]},
        {"frame": "3E", "network": 0, "station": 255, "monitoring_timer": 10},
    )
    add(
        "mc_3e_batch_write_001",
        "3E 成批写:D100 起 2 字,值 10/258",
        qna_request("3E", 0, 0, 0xFF, 10, True, False, 100, 0xA8, 2, words_le([10, 258])),
        qna_response("3E", 0, 0),
        {"echo": True},
        {"frame": "3E", "network": 0, "station": 255, "monitoring_timer": 10},
    )
    add(
        "mc_4e_batch_read_001",
        "4E 成批读:D100 起 2 字,序列号 1",
        qna_request("4E", 1, 0, 0xFF, 10, False, False, 100, 0xA8, 2),
        qna_response("4E", 1, 0, words_le([20, 10])),
        {"values": [20, 10]},
        {"frame": "4E", "network": 0, "station": 255, "monitoring_timer": 10, "serial": 1},
    )
    add(
        "mc_3e_error_001",
        "3E 读返回结束代码 0xC059",
        qna_request("3E", 0, 0, 0xFF, 10, False, False, 100, 0xA8, 2),
        qna_response("3E", 0, 0xC059),
        {"error_code": 0xC059},
        {"frame": "3E", "network": 0, "station": 255, "monitoring_timer": 10},
    )
    add(
        "mc_1e_batch_read_001",
        "1E 成批读(字):D100 起 2 字,值 20/10",
        one_e_request(0x00, 10, 0x01, 100, 0x4420, 2),
        one_e_response(0x01, 0x00, words_le([20, 10])),
        {"values": [20, 10]},
        {"frame": "1E", "station": 0, "monitoring_timer": 10},
    )
    add(
        "mc_1e_batch_write_001",
        "1E 成批写(字):D100 1 字,值 1234",
        one_e_request(0x00, 10, 0x03, 100, 0x4420, 1, words_le([1234])),
        one_e_response(0x03, 0x00),
        {"echo": True},
        {"frame": "1E", "station": 0, "monitoring_timer": 10},
    )

    for sample in samples:
        filename = sample.pop("file")
        path = HERE / "{}.json".format(filename)
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print("written:", path)


if __name__ == "__main__":
    main()
