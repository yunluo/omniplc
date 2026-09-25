"""生成 Modbus 黄金报文样本(tests/golden/modbus_*.json)。

使用**独立的最小实现**(CRC16 与组帧单独编写,不复用 omniplc 代码)
计算标准示例报文,避免"实现生成测试"的同源偏差;也可向本目录手工
补充真机/专用模拟器工具的抓包样本(保持 JSON 结构即可)。

用法::

    uv run python tests/golden/generate_modbus_samples.py
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent


def crc16(data: bytes) -> int:
    """独立实现的 Modbus CRC-16(初始 0xFFFF,反射多项式 0xA001)。"""
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
    return value


def rtu(station: int, pdu: bytes) -> str:
    """站号 + PDU + CRC16(低字节在前)→ 十六进制串。"""
    body = bytes([station]) + pdu
    return (body + crc16(body).to_bytes(2, "little")).hex()


def mbap(transaction_id: int, station: int, pdu: bytes) -> str:
    """MBAP 封帧 → 十六进制串。"""
    return (struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, station) + pdu).hex()


def device_id_pdu(
    code: int,
    object_id: int,
    conformity: int,
    objects: List[tuple],
    more_follows: int = 0x00,
    next_object_id: int = 0x00,
) -> bytes:
    """读设备标识响应 PDU(FC 43/14,规范 §6.21 结构,独立实现)。

    响应 = 功能码(1) + MEI 0x0E(1) + 读取码(1) + 符合级别(1)
    + MoreFollows(1) + 下一对象号(1) + 对象数(1)
    + 对象数 × [对象号(1) + 长度(1) + 值(N)]。
    """
    body = bytearray(
        [
            0x2B,
            0x0E,
            code,
            conformity,
            0xFF if more_follows else 0x00,
            next_object_id,
            len(objects),
        ]
    )
    for oid, raw in objects:
        body.append(oid)
        body.append(len(raw))
        body += raw
    return bytes(body)


def device_id_request(code: int, object_id: int) -> bytes:
    """读设备标识请求 PDU(FC 43/14,独立实现)。"""
    return bytes([0x2B, 0x0E, code, object_id])


def main() -> None:
    """生成全部 Modbus 样本文件。"""
    samples: List[Dict[str, Any]] = []

    def add(
        filename: str,
        name: str,
        request: str,
        response: str,
        expect: Dict[str, Any],
        setup: Dict[str, Any],
    ) -> None:
        sample: Dict[str, Any] = {
            "file": filename,
            "name": name,
            "request_hex": request,
            "response_hex": response,
            "expect": expect,
            "setup": setup,
        }
        samples.append(sample)

    coil_bits_8 = [(0xCD >> index) & 1 for index in range(8)]
    coil_bits_10 = coil_bits_8 + [(0x01 >> index) & 1 for index in range(2)]

    # ---- TCP / UDP 共用 MBAP(单元号 1,事务号 1) ------------------------
    add(
        "modbus_tcp_read_holding_001",
        "读保持寄存器 2 个,值 20/10",
        mbap(1, 1, struct.pack(">BHH", 3, 0, 2)),
        mbap(1, 1, bytes([3, 4]) + struct.pack(">HH", 20, 10)),
        {"values": [20, 10]},
        {"station": 1, "transaction_id": 1},
    )
    add(
        "modbus_tcp_read_input_001",
        "读输入寄存器 1 个,值 65518",
        mbap(1, 1, struct.pack(">BHH", 4, 10, 1)),
        mbap(1, 1, bytes([4, 2]) + struct.pack(">H", 65518)),
        {"values": [65518]},
        {"station": 1, "transaction_id": 1},
    )
    add(
        "modbus_tcp_write_single_register_001",
        "写单寄存器:地址 5,值 3",
        mbap(1, 1, struct.pack(">BHH", 6, 5, 3)),
        mbap(1, 1, struct.pack(">BHH", 6, 5, 3)),
        {"echo": True},
        {"station": 1, "transaction_id": 1},
    )
    add(
        "modbus_tcp_write_single_coil_001",
        "写单线圈:地址 0,置位(0xFF00)",
        mbap(1, 1, struct.pack(">BHH", 5, 0, 0xFF00)),
        mbap(1, 1, struct.pack(">BHH", 5, 0, 0xFF00)),
        {"echo": True},
        {"station": 1, "transaction_id": 1},
    )
    add(
        "modbus_tcp_write_multi_registers_001",
        "写多寄存器:地址 0,2 个,值 10/258",
        mbap(1, 1, struct.pack(">BHHB", 16, 0, 2, 4) + struct.pack(">HH", 10, 258)),
        mbap(1, 1, struct.pack(">BHH", 16, 0, 2)),
        {"echo": True},
        {"station": 1, "transaction_id": 1},
    )
    add(
        "modbus_tcp_exception_001",
        "读保持寄存器返回异常码 02(地址越界)",
        mbap(1, 1, struct.pack(">BHH", 3, 0, 2)),
        mbap(1, 1, bytes([0x83, 0x02])),
        {"error_code": 2},
        {"station": 1, "transaction_id": 1},
    )

    # ---- RTU(站号 1) ---------------------------------------------------
    add(
        "modbus_rtu_read_holding_001",
        "读保持寄存器 2 个,值 20/10(经典抓包样本)",
        rtu(1, struct.pack(">BHH", 3, 0, 2)),
        rtu(1, bytes([3, 4]) + struct.pack(">HH", 20, 10)),
        {"values": [20, 10]},
        {"station": 1},
    )
    add(
        "modbus_rtu_read_coils_001",
        "读线圈 8 个,状态字节 0xCD(LSB 在前)",
        rtu(1, struct.pack(">BHH", 1, 0, 8)),
        rtu(1, bytes([1, 1, 0xCD])),
        {"values": coil_bits_8},
        {"station": 1},
    )
    add(
        "modbus_rtu_write_single_coil_001",
        "写单线圈:地址 0,置位(0xFF00)",
        rtu(1, struct.pack(">BHH", 5, 0, 0xFF00)),
        rtu(1, struct.pack(">BHH", 5, 0, 0xFF00)),
        {"echo": True},
        {"station": 1},
    )
    add(
        "modbus_rtu_write_multi_coils_001",
        "写多线圈:地址 0,10 个,字节 0xCD 0x01(Modbus 手册示例)",
        rtu(1, struct.pack(">BHHB", 15, 0, 10, 2) + bytes([0xCD, 0x01])),
        rtu(1, struct.pack(">BHH", 15, 0, 10)),
        {"values": coil_bits_10},
        {"station": 1},
    )
    add(
        "modbus_rtu_exception_001",
        "读保持寄存器返回异常码 02(地址越界)",
        rtu(1, struct.pack(">BHH", 3, 0x2710, 2)),
        rtu(1, bytes([0x83, 0x02])),
        {"error_code": 2},
        {"station": 1},
    )

    # ---- FC 23 读写多寄存器(规范 §6.17 示例:读 6 个 @3、写 3 个 @14) ----
    rw_request = struct.pack(">BHHHHB", 0x17, 3, 6, 14, 3, 6) + struct.pack(
        ">HHH", 0x00FF, 0x00FF, 0x00FF
    )
    rw_response = bytes([0x17, 12]) + struct.pack(
        ">HHHHHH", 0x00FE, 0x0ACD, 0x0001, 0x0003, 0x000D, 0x00FF
    )
    add(
        "modbus_tcp_read_write_multi_001",
        "读写多寄存器:读 6 个 @3、写 3 个 @14(规范 §6.17 示例)",
        mbap(1, 1, rw_request),
        mbap(1, 1, rw_response),
        {"values": [0x00FE, 0x0ACD, 0x0001, 0x0003, 0x000D, 0x00FF]},
        {
            "station": 1,
            "transaction_id": 1,
            "read_address": 3,
            "read_count": 6,
            "write_address": 14,
            "write_values": [0x00FF, 0x00FF, 0x00FF],
        },
    )
    add(
        "modbus_rtu_read_write_multi_001",
        "读写多寄存器(RTU 走线):读 6 个 @3、写 3 个 @14",
        rtu(1, rw_request),
        rtu(1, rw_response),
        {"values": [0x00FE, 0x0ACD, 0x0001, 0x0003, 0x000D, 0x00FF]},
        {
            "station": 1,
            "read_address": 3,
            "read_count": 6,
            "write_address": 14,
            "write_values": [0x00FF, 0x00FF, 0x00FF],
        },
    )
    add(
        "modbus_tcp_read_write_multi_exception_001",
        "读写多寄存器返回异常码 02(写地址越界)",
        mbap(1, 1, rw_request),
        mbap(1, 1, bytes([0x97, 0x02])),
        {"error_code": 2},
        {"station": 1, "transaction_id": 1},
    )

    # ---- FC 43/14 读设备标识(规范 §6.21 结构:基本标识三对象) ----------
    # 注:规范正文示例的打印长度与文本不完全自洽(PDF 排版所致),
    # 此处按规范**结构**用自洽内容生成(长度由 len() 计算)
    basic_objects = [
        (0x00, b"OmniPLC Industries"),
        (0x01, b"MDL-2024"),
        (0x02, b"V2.11"),
    ]
    basic_response = device_id_pdu(0x01, 0x00, 0x01, basic_objects)
    add(
        "modbus_tcp_device_id_001",
        "读设备标识:基本标识单页三对象(VendorName/ProductCode/MajorMinorRevision)",
        mbap(1, 1, device_id_request(0x01, 0x00)),
        mbap(1, 1, basic_response),
        {
            "conformity_level": 0x01,
            "more_follows": False,
            "next_object_id": 0x00,
            "objects": [
                [0x00, b"OmniPLC Industries".hex()],
                [0x01, b"MDL-2024".hex()],
                [0x02, b"V2.11".hex()],
            ],
        },
        {"station": 1, "transaction_id": 1, "object_id": 0x00},
    )
    add(
        "modbus_rtu_device_id_001",
        "读设备标识(RTU 走线):基本标识单页三对象",
        rtu(1, device_id_request(0x01, 0x00)),
        rtu(1, basic_response),
        {
            "conformity_level": 0x01,
            "more_follows": False,
            "next_object_id": 0x00,
            "objects": [
                [0x00, b"OmniPLC Industries".hex()],
                [0x01, b"MDL-2024".hex()],
                [0x02, b"V2.11".hex()],
            ],
        },
        {"station": 1, "object_id": 0x00},
    )
    add(
        "modbus_tcp_device_id_more_001",
        "读设备标识:首页带 MoreFollows=FF(后续对象从 0x02 起)",
        mbap(1, 1, device_id_request(0x01, 0x00)),
        mbap(
            1,
            1,
            device_id_pdu(
                0x01,
                0x00,
                0x01,
                [(0x00, b"OmniPLC Industries"), (0x01, b"MDL-2024")],
                more_follows=0xFF,
                next_object_id=0x02,
            ),
        ),
        {
            "conformity_level": 0x01,
            "more_follows": True,
            "next_object_id": 0x02,
            "objects": [
                [0x00, b"OmniPLC Industries".hex()],
                [0x01, b"MDL-2024".hex()],
            ],
        },
        {"station": 1, "transaction_id": 1, "object_id": 0x00},
    )
    add(
        "modbus_tcp_device_id_exception_001",
        "读设备标识返回异常码 01(设备不支持该访问码)",
        mbap(1, 1, device_id_request(0x01, 0x00)),
        mbap(1, 1, bytes([0xAB, 0x01])),
        {"error_code": 1},
        {"station": 1, "transaction_id": 1},
    )

    for sample in samples:
        filename = sample.pop("file")
        path = HERE / "{}.json".format(filename)
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print("written:", path)


if __name__ == "__main__":
    main()
