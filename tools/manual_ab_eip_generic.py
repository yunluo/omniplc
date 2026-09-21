# -*- coding: utf-8 -*-
"""罗克韦尔 AB EtherNet/IP 通用 CIP 服务手动联机测试。

走 :class:`AllenBradleyEthIpClient` 的 5 个通用入口:

- ``list_identity()`` —— ENIP ListIdentity 单播(无需 CIP 会话也能应答)
- ``get_plc_info()`` —— GetAttributesAll on Identity Object
- ``get_attribute_all(class, instance)`` —— 通用对象裸属性
- ``get_attribute_list(class, instance, attrs)`` —— 指定属性号列表
- ``generic_message(service, class, instance, body)`` —— 低阶入口

无参数直接跑(每次只测这一个协议):目标 127.0.0.1:44818。
    uv run python tools/manual_ab_eip_generic.py [--ip IP] [--port 端口] [--debug]

手动调试脚本(不进 ``manual_common.py`` 默认套件),用于:
- 现场对端 CIP 设备发现 / 元信息读取
- 非 Logix 对象(Connection Manager、Assembly、Parameter 等)的探测
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from omniplc.plc.ab import AllenBradleyEthIpClient
from omniplc.plc.ab.codec_cip import CIP_CLASS_IDENTITY, CIP_INSTANCE_IDENTITY


def _log(msg: str) -> None:
    """写一行到 stdout(手动调试不需要汇总文件)。"""
    print(msg, flush=True)


def probe(ip: str, port: int, slot: int) -> int:
    """连接 → 调 5 个通用入口 → 关闭 → 返回退出码。"""
    client = AllenBradleyEthIpClient(ip, port, slot=slot)
    failed = 0

    if not client.connect():
        _log("连接失败:{}".format(client.last_error))
        return 1

    try:
        _log("==== 通用 CIP 服务手动测试({}:{}, slot={}) ====".format(ip, port, slot))

        # 1. ListIdentity
        _log("[1/5] list_identity() …")
        ok, info = client.list_identity()
        if ok and info is not None:
            _log("  vendor={vendor:#06x} product_code={product_code:#06x} "
                 "revision={revision} serial=0x{serial:08X} state=0x{state:02X}".format(**info))
            _log("  product_name={!r}".format(info["product_name"]))
        else:
            _log("  [FAIL] {}".format(client.last_error))
            failed += 1

        # 2. GetAttributesAll on Identity Object
        _log("[2/5] get_plc_info() (GetAttributesAll on Identity Object) …")
        ok, info = client.get_plc_info()
        if ok and info is not None:
            _log("  vendor={vendor:#06x} product_type={product_type:#06x} "
                 "product_code={product_code:#06x} revision={revision}".format(**info))
            _log("  serial=0x{:08X} product_name={!r}".format(
                info["serial"], info["product_name"]
            ))
        else:
            _log("  [FAIL] {}".format(client.last_error))
            failed += 1

        # 3. GetAttributeAll on Identity Object(原始字节)
        _log("[3/5] get_attribute_all(0x01, 0x01) …")
        ok, payload = client.get_attribute_all(CIP_CLASS_IDENTITY, CIP_INSTANCE_IDENTITY)
        if ok and payload is not None:
            _log("  裸数据 {} 字节".format(len(payload)))
        else:
            _log("  [FAIL] {}".format(client.last_error))
            failed += 1

        # 4. GetAttributeList on Identity Object(7 字段解码)
        _log("[4/5] get_attribute_list(0x01, 0x01, (1, 2, 3, 4, 5, 6, 7)) …")
        ok, decoded = client.get_attribute_list(
            CIP_CLASS_IDENTITY, CIP_INSTANCE_IDENTITY, (1, 2, 3, 4, 5, 6, 7)
        )
        if ok and decoded is not None:
            for attr_id, value in decoded:
                _log("  属性 {} = {!r}".format(attr_id, value))
        else:
            _log("  [FAIL] {}".format(client.last_error))
            failed += 1

        # 5. generic_message 低阶入口(GetAttributesAll 不带路径)
        _log("[5/5] generic_message(0x01, 0x01, 0x01, b\"\") …")
        ok, payload = client.generic_message(
            0x01, CIP_CLASS_IDENTITY, CIP_INSTANCE_IDENTITY, b""
        )
        if ok and payload is not None:
            _log("  返回数据 {} 字节".format(len(payload)))
        else:
            _log("  [FAIL] {}".format(client.last_error))
            failed += 1

    finally:
        client.disconnect()
        _log("连接已关闭")

    _log("==== 总结 ====")
    if failed:
        _log("失败 {} / 5".format(failed))
        return 1
    _log("全部通过")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", default="127.0.0.1", help="目标 IP(默认 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=44818, help="EtherNet/IP 端口(默认 44818)"
    )
    parser.add_argument(
        "--slot", type=int, default=0, help="CPU 槽号(默认 0:内置口)"
    )
    parser.add_argument("--debug", action="store_true", help="打开 BaseClient 调试日志")
    args = parser.parse_args(argv)
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    return probe(args.ip, args.port, args.slot)


if __name__ == "__main__":
    sys.exit(main())
