# -*- coding: utf-8 -*-
"""omniplc 手动联机测试公共运行器(各协议脚本 tools/manual_<driver>.py 共用)。

配合外部 PLC 模拟软件(或直接接真机)人工验证。**无参数直接运行**:
目标 127.0.0.1 + 协议默认端口 + 内置精细点位,一次只测一个协议::

    uv run python tools/test_modbus_tcp.py
    uv run python tools/test_siemens_s7.py --ip 192.168.0.1 --debug

可选 JSON 配置文件覆盖(自定义 IP/端口/点位;--ip/--port 亦可覆盖配置)::

    uv run python tools/test_modbus_tcp.py 我的配置.json

每个脚本只跑 driver(及 frame,若给定)匹配的连接,流程::

    连接 → 逐点位 随机值写→读回比对 × 3 轮(只读点仅读) → 关闭连接
    结果逐条打印,同时汇总追加到 logs/manual_test.log,任一失败退出码 1

配置文件结构(tools/manual_test.json 为全协议示例):
顶层 {"debug", "seed", "rounds", "connections"};每个连接:
name / driver / ip / port / params(驱动特定参数)/ serial(串口驱动)/
connect_only(只测联通)/ raw(opentcp)/ snapshot(mtconnect)/
points:[{name, address, type, access, length, encoding}]。
type:bool/short/ushort/int/uint/long/ulong/float/double/string;
access 缺省 "rw","r" 为只读点(输入区/扫码内容/数采项)。
"""
import argparse
import json
import random
import string
import struct
import sys
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
LOG_PATH = ROOT / "logs" / "manual_test.log"
DEFAULT_CONFIG = ROOT / "tools" / "manual_test.json"
FRAME_DEFAULTS = {"melsec_mc_tcp": "3E", "melsec_mc_udp": "3E", "melsec_mc_serial": "3C"}
"""MC 各走线的缺省帧型(连接 params.frame 缺省时按此参与帧筛选)。"""
DEFAULT_IP = "127.0.0.1"

DEFAULT_CONNECTIONS = {
    # (driver, frame 或 None) → 内置默认连接(params/serial/points);
    # 不带 port 键即用协议默认端口(build_client 兜底),串口用 serial.port。
    ("modbus_tcp", None): {
        "params": {"station": 1, "word_order": "ABCD"},
        "points": [
            {'tag_id': 'c0_bool', 'address': 'c0', 'type': 'bool', 'remark': '线圈'},
            {'tag_id': 'c1_bool', 'address': 'c1', 'type': 'bool', 'remark': '线圈1'},
            {'tag_id': 'hr0_short', 'address': 'hr0', 'type': 'short', 'remark': '保持寄存器-短整数'},
            {'tag_id': 'hr2_ushort', 'address': 'hr2', 'type': 'ushort', 'remark': '保持寄存器-无符号短'},
            {'tag_id': 'hr4_int', 'address': 'hr4', 'type': 'int', 'remark': '保持寄存器-整数'},
            {'tag_id': 'hr6_uint', 'address': 'hr6', 'type': 'uint', 'remark': '保持寄存器-无符号整'},
            {'tag_id': 'hr8_long', 'address': 'hr8', 'type': 'long', 'remark': '保持寄存器-长整数'},
            {'tag_id': 'hr12_float', 'address': 'hr12', 'type': 'float', 'remark': '保持寄存器-浮点'},
            {'tag_id': 'hr16_double', 'address': 'hr16', 'type': 'double', 'remark': '保持寄存器-双精度'},
            {'tag_id': 'hr20_3_bool', 'address': 'hr20.3', 'type': 'bool', 'remark': '寄存器位'},
            {'tag_id': 'ir0_ushort', 'address': 'ir0', 'type': 'ushort', 'remark': '输入寄存器(只读)', 'access': 'r'},
            {'tag_id': 'di0_bool', 'address': 'di0', 'type': 'bool', 'remark': '离散输入(只读)', 'access': 'r'},
        ],
    },
    ("modbus_rtu", None): {
        "params": {"station": 1},
        "serial": {"port": "COM3", "baud_rate": 9600, "data_bits": 8,
                   "stop_bits": 1, "parity": "N"},
        "points": [
            {'tag_id': 'c0_bool', 'address': 'c0', 'type': 'bool', 'remark': '线圈'},
            {'tag_id': 'hr0_short', 'address': 'hr0', 'type': 'short', 'remark': '保持寄存器-短整数'},
            {'tag_id': 'hr4_float', 'address': 'hr4', 'type': 'float', 'remark': '保持寄存器-浮点'},
        ],
    },
    ("melsec_mc_tcp", "3E"): {
        "params": {"frame": "3E", "network_number": 0, "pc_number": 255},
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_ushort', 'address': 'D102', 'type': 'ushort', 'remark': 'D102 无符号短'},
            {'tag_id': 'd104_int', 'address': 'D104', 'type': 'int', 'remark': 'D104 整数'},
            {'tag_id': 'd106_uint', 'address': 'D106', 'type': 'uint', 'remark': 'D106 无符号整'},
            {'tag_id': 'd108_float', 'address': 'D108', 'type': 'float', 'remark': 'D108 浮点'},
            {'tag_id': 'd110_double', 'address': 'D110', 'type': 'double', 'remark': 'D110 双精度'},
            {'tag_id': 'd120_long', 'address': 'D120', 'type': 'long', 'remark': 'D120 长整数'},
            {'tag_id': 'd130_3_bool', 'address': 'D130.3', 'type': 'bool', 'remark': 'D130.3 字软元件位访问'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
            {'tag_id': 'm10_bool', 'address': 'M10', 'type': 'bool', 'remark': 'M10 位'},
            {'tag_id': 'y40_bool', 'address': 'Y40', 'type': 'bool', 'remark': 'Y40 输出位'},
            {'tag_id': 'b0_bool', 'address': 'B0', 'type': 'bool', 'remark': 'B0 工作位(十六进制编号)'},
            {'tag_id': 'w100_ushort', 'address': 'W100', 'type': 'ushort', 'remark': 'W100 链接寄存器(十六进制编号)'},
            {'tag_id': 'x1f_bool', 'address': 'X1F', 'type': 'bool', 'remark': 'X1F 输入位(只读)', 'access': 'r'},
            {'tag_id': 'd300_str', 'address': 'D300', 'type': 'string', 'remark': 'D300 字符串', 'length': 16},
        ],
    },
    ("melsec_mc_tcp", "4E"): {
        "params": {"frame": "4E"},
        "points": [
            {'tag_id': 'd400_short', 'address': 'D400', 'type': 'short', 'remark': 'D400 短整数'},
            {'tag_id': 'd402_float', 'address': 'D402', 'type': 'float', 'remark': 'D402 浮点'},
            {'tag_id': 'd404_int', 'address': 'D404', 'type': 'int', 'remark': 'D404 整数'},
            {'tag_id': 'd406_double', 'address': 'D406', 'type': 'double', 'remark': 'D406 双精度'},
            {'tag_id': 'm20_bool', 'address': 'M20', 'type': 'bool', 'remark': 'M20 位'},
            {'tag_id': 'm30_bool', 'address': 'M30', 'type': 'bool', 'remark': 'M30 位'},
            {'tag_id': 'y50_bool', 'address': 'Y50', 'type': 'bool', 'remark': 'Y50 输出位'},
            {'tag_id': 'd420_5_bool', 'address': 'D420.5', 'type': 'bool', 'remark': 'D420.5 字软元件位访问'},
        ],
    },
    ("melsec_mc_tcp", "1E"): {
        "params": {"frame": "1E"},
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_ushort', 'address': 'D102', 'type': 'ushort', 'remark': 'D102 无符号短'},
            {'tag_id': 'd104_int', 'address': 'D104', 'type': 'int', 'remark': 'D104 整数'},
            {'tag_id': 'd106_float', 'address': 'D106', 'type': 'float', 'remark': 'D106 浮点'},
            {'tag_id': 'd108_double', 'address': 'D108', 'type': 'double', 'remark': 'D108 双精度'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
            {'tag_id': 'm1_bool', 'address': 'M1', 'type': 'bool', 'remark': 'M1 位'},
            {'tag_id': 's0_bool', 'address': 'S0', 'type': 'bool', 'remark': 'S0 步进位'},
            {"name": "X20 输入位(八进制编号,只读)", "address": "X20",
             "type": "bool", "access": "r"},
        ],
    },
    ("melsec_mc_udp", "3E"): {
        "params": {"frame": "3E"},
        "points": [
            {'tag_id': 'd500_short', 'address': 'D500', 'type': 'short', 'remark': 'D500 短整数'},
            {'tag_id': 'd502_float', 'address': 'D502', 'type': 'float', 'remark': 'D502 浮点'},
            {'tag_id': 'd504_int', 'address': 'D504', 'type': 'int', 'remark': 'D504 整数'},
            {'tag_id': 'm40_bool', 'address': 'M40', 'type': 'bool', 'remark': 'M40 位'},
            {'tag_id': 'm50_bool', 'address': 'M50', 'type': 'bool', 'remark': 'M50 位'},
            {'tag_id': 'y60_bool', 'address': 'Y60', 'type': 'bool', 'remark': 'Y60 输出位'},
        ],
    },
    ("melsec_mc_udp", "4E"): {
        "params": {"frame": "4E"},
        "points": [
            {'tag_id': 'd600_short', 'address': 'D600', 'type': 'short', 'remark': 'D600 短整数'},
            {'tag_id': 'd602_float', 'address': 'D602', 'type': 'float', 'remark': 'D602 浮点'},
            {'tag_id': 'm60_bool', 'address': 'M60', 'type': 'bool', 'remark': 'M60 位'},
            {'tag_id': 'm70_bool', 'address': 'M70', 'type': 'bool', 'remark': 'M70 位'},
        ],
    },
    ("melsec_mc_udp", "1E"): {
        "params": {"frame": "1E"},
        "points": [
            {'tag_id': 'd700_short', 'address': 'D700', 'type': 'short', 'remark': 'D700 短整数'},
            {'tag_id': 'd702_float', 'address': 'D702', 'type': 'float', 'remark': 'D702 浮点'},
            {'tag_id': 'm2_bool', 'address': 'M2', 'type': 'bool', 'remark': 'M2 位'},
            {'tag_id': 'm3_bool', 'address': 'M3', 'type': 'bool', 'remark': 'M3 位'},
        ],
    },
    ("melsec_mc_serial", "3C"): {
        "params": {"frame": "3C", "station_number": 0},
        "serial": {"port": "COM4", "baud_rate": 9600, "data_bits": 8,
                   "stop_bits": 1, "parity": "N"},
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_float', 'address': 'D102', 'type': 'float', 'remark': 'D102 浮点'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
            {'tag_id': 'm10_bool', 'address': 'M10', 'type': 'bool', 'remark': 'M10 位'},
        ],
    },
    ("melsec_mc_serial", "4C"): {
        "params": {"frame": "4C", "station_number": 0},
        "serial": {"port": "COM4", "baud_rate": 9600, "data_bits": 8,
                   "stop_bits": 1, "parity": "N"},
        "points": [
            {'tag_id': 'd200_short', 'address': 'D200', 'type': 'short', 'remark': 'D200 短整数'},
            {'tag_id': 'd202_float', 'address': 'D202', 'type': 'float', 'remark': 'D202 浮点'},
            {'tag_id': 'd204_int', 'address': 'D204', 'type': 'int', 'remark': 'D204 整数'},
            {'tag_id': 'm20_bool', 'address': 'M20', 'type': 'bool', 'remark': 'M20 位'},
            {'tag_id': 'm30_bool', 'address': 'M30', 'type': 'bool', 'remark': 'M30 位'},
            {'tag_id': 'y0_bool', 'address': 'Y0', 'type': 'bool', 'remark': 'Y0 输出位'},
        ],
    },
    ("melsec_mx", None): {
        "params": {"logical_station_number": 0},
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_float', 'address': 'D102', 'type': 'float', 'remark': 'D102 浮点'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
        ],
    },
    ("omron_fins_tcp", None): {
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_ushort', 'address': 'D102', 'type': 'ushort', 'remark': 'D102 无符号短'},
            {'tag_id': 'd104_float', 'address': 'D104', 'type': 'float', 'remark': 'D104 浮点'},
            {'tag_id': 'd110_double', 'address': 'D110', 'type': 'double', 'remark': 'D110 双精度'},
            {'tag_id': 'cio0_5_bool', 'address': 'CIO0.5', 'type': 'bool', 'remark': 'CIO0.5 位'},
            {'tag_id': 'w10_3_bool', 'address': 'W10.3', 'type': 'bool', 'remark': 'W10.3 位'},
            {'tag_id': 'd200_3_bool', 'address': 'D200.3', 'type': 'bool', 'remark': 'D200.3 字位访问'},
            {'tag_id': 'e0_100_ushort', 'address': 'E0_100', 'type': 'ushort', 'remark': 'EM区 bank0(只读)', 'access': 'r'},
            {'tag_id': 'a0_short', 'address': 'A0', 'type': 'short', 'remark': '辅助区A(只读)', 'access': 'r'},
        ],
    },
    ("omron_fins_udp", None): {
        "points": [
            {'tag_id': 'd300_short', 'address': 'D300', 'type': 'short', 'remark': 'D300 短整数'},
            {'tag_id': 'd302_float', 'address': 'D302', 'type': 'float', 'remark': 'D302 浮点'},
            {'tag_id': 'cio10_0_bool', 'address': 'CIO10.0', 'type': 'bool', 'remark': 'CIO10.0 位'},
        ],
    },
    ("omron_cip", None): {
        "points": [
            {'tag_id': 'testbool_bool', 'address': 'TestBool', 'type': 'bool', 'remark': 'BOOL 变量'},
            {'tag_id': 'testint_short', 'address': 'TestInt', 'type': 'short', 'remark': 'INT 变量'},
            {'tag_id': 'testdint_int', 'address': 'TestDint', 'type': 'int', 'remark': 'DINT 变量'},
            {'tag_id': 'testreal_float', 'address': 'TestReal', 'type': 'float', 'remark': 'REAL 变量'},
        ],
    },
    ("keyence_hostlink_tcp", None): {
        "points": [
            {'tag_id': 'dm100_short', 'address': 'DM100', 'type': 'short', 'remark': 'DM100 短整数'},
            {'tag_id': 'dm102_ushort', 'address': 'DM102', 'type': 'ushort', 'remark': 'DM102 无符号短'},
            {'tag_id': 'dm104_float', 'address': 'DM104', 'type': 'float', 'remark': 'DM104 浮点'},
            {'tag_id': 'dm110_double', 'address': 'DM110', 'type': 'double', 'remark': 'DM110 双精度'},
            {'tag_id': 'm100_bool', 'address': 'M100', 'type': 'bool', 'remark': 'M100 扩展继电器'},
            {'tag_id': 'b1f_bool', 'address': 'B1F', 'type': 'bool', 'remark': 'B1F 工作位'},
            {'tag_id': 'dm100_5_bool', 'address': 'DM100.5', 'type': 'bool', 'remark': 'DM100.5 字位访问'},
            {'tag_id': 'r000_bool', 'address': 'R000', 'type': 'bool', 'remark': 'R000 中继电器'},
        ],
    },
    ("keyence_hostlink_udp", None): {
        "points": [
            {'tag_id': 'dm200_short', 'address': 'DM200', 'type': 'short', 'remark': 'DM200 短整数'},
            {'tag_id': 'dm202_float', 'address': 'DM202', 'type': 'float', 'remark': 'DM202 浮点'},
        ],
    },
    ("keyence_mc_tcp", None): {
        "points": [
            {'tag_id': 'dm100_short', 'address': 'DM100', 'type': 'short', 'remark': 'DM100 短整数'},
            {'tag_id': 'dm102_float', 'address': 'DM102', 'type': 'float', 'remark': 'DM102 浮点'},
            {'tag_id': 'r0_bool', 'address': 'R0', 'type': 'bool', 'remark': 'R0 继电器位'},
            {'tag_id': 'b0_bool', 'address': 'B0', 'type': 'bool', 'remark': 'B0 工作位'},
        ],
    },
    ("keyence_mc_udp", None): {
        "points": [
            {'tag_id': 'dm300_short', 'address': 'DM300', 'type': 'short', 'remark': 'DM300 短整数'},
            {'tag_id': 'r10_bool', 'address': 'R10', 'type': 'bool', 'remark': 'R10 继电器位'},
        ],
    },
    ("keyence_sr", None): {
        "params": {"scan_dwell": 1.0, "scan_timeout": 3.0},
    },
    ("inovance_tcp", None): {
        "points": [
            {'tag_id': 'd0_short', 'address': 'D0', 'type': 'short', 'remark': 'D0 短整数'},
            {'tag_id': 'd10_int', 'address': 'D10', 'type': 'int', 'remark': 'D10 整数'},
            {'tag_id': 'd20_float', 'address': 'D20', 'type': 'float', 'remark': 'D20 浮点'},
            {'tag_id': 'd30_double', 'address': 'D30', 'type': 'double', 'remark': 'D30 双精度'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
            {'tag_id': 'm1_bool', 'address': 'M1', 'type': 'bool', 'remark': 'M1 位'},
            {'tag_id': 'y0_bool', 'address': 'Y0', 'type': 'bool', 'remark': 'Y0 输出位'},
            {'tag_id': 'x0_bool', 'address': 'X0', 'type': 'bool', 'remark': 'X0 输入位(只读)', 'access': 'r'},
        ],
    },
    ("inovance_rtu", None): {
        "params": {"station": 1},
        "serial": {"port": "COM5", "baud_rate": 9600, "data_bits": 8,
                   "stop_bits": 2, "parity": "N"},
        "points": [
            {'tag_id': 'd0_short', 'address': 'D0', 'type': 'short', 'remark': 'D0 短整数'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
        ],
    },
    ("inovance_mc_tcp", None): {
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_float', 'address': 'D102', 'type': 'float', 'remark': 'D102 浮点'},
            {'tag_id': 'm0_bool', 'address': 'M0', 'type': 'bool', 'remark': 'M0 位'},
        ],
    },
    ("panasonic_mc_tcp", None): {
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_float', 'address': 'D102', 'type': 'float', 'remark': 'D102 浮点'},
            {'tag_id': 'r0_bool', 'address': 'R0', 'type': 'bool', 'remark': 'R0 继电器位'},
            {'tag_id': 'y0_bool', 'address': 'Y0', 'type': 'bool', 'remark': 'Y0 输出位'},
        ],
    },
    ("panasonic_mewtocol_tcp", None): {
        "points": [
            {'tag_id': 'd100_short', 'address': 'D100', 'type': 'short', 'remark': 'D100 短整数'},
            {'tag_id': 'd102_float', 'address': 'D102', 'type': 'float', 'remark': 'D102 浮点'},
            {'tag_id': 'r0_0_bool', 'address': 'R0.0', 'type': 'bool', 'remark': 'R0.0 接点位(点号形式)'},
            {'tag_id': 'r1_5_bool', 'address': 'R1.5', 'type': 'bool', 'remark': 'R1.5 接点位(点号形式)'},
        ],
    },
    ("panasonic_mewtocol_udp", None): {
        "points": [
            {'tag_id': 'd200_short', 'address': 'D200', 'type': 'short', 'remark': 'D200 短整数'},
            {'tag_id': 'r10_bool', 'address': 'R10', 'type': 'bool', 'remark': 'R10 接点位'},
        ],
    },
    ("toyopuc_tcp", None): {
        "points": [
            {'tag_id': 'd0100_short', 'address': 'D0100', 'type': 'short', 'remark': 'D0100 短整数(十六进制编号)'},
            {'tag_id': 'd0200_float', 'address': 'D0200', 'type': 'float', 'remark': 'D0200 浮点'},
            {'tag_id': 'm0100_bool', 'address': 'M0100', 'type': 'bool', 'remark': 'M0100 位'},
            {'tag_id': 'x0100_bool', 'address': 'X0100', 'type': 'bool', 'remark': 'X0100 位'},
        ],
    },
    ("toyopuc_udp", None): {
        "points": [
            {'tag_id': 'd0300_short', 'address': 'D0300', 'type': 'short', 'remark': 'D0300 短整数'},
            {'tag_id': 'm0200_bool', 'address': 'M0200', 'type': 'bool', 'remark': 'M0200 位'},
        ],
    },
    ("ab_eip", None): {
        "params": {"slot": 0, "connected_messaging": False},
        "points": [
            {'tag_id': 'mybool_bool', 'address': 'MyBool', 'type': 'bool', 'remark': 'BOOL 标签'},
            {'tag_id': 'myint_short', 'address': 'MyInt', 'type': 'short', 'remark': 'INT 标签'},
            {'tag_id': 'mydint_int', 'address': 'MyDint', 'type': 'int', 'remark': 'DINT 标签'},
            {'tag_id': 'myreal_float', 'address': 'MyReal', 'type': 'float', 'remark': 'REAL 标签'},
            {'tag_id': 'mylong_long', 'address': 'MyLong', 'type': 'long', 'remark': 'LINT 标签'},
            {'tag_id': 'mystring_str', 'address': 'MyString', 'type': 'string', 'remark': 'STRING 标签', 'length': 40},
        ],
    },
    ("beckhoff_ads", None): {
        "params": {"ads_port": 851, "net_id": ""},
        "points": [
            {'tag_id': 'main_bswitch_bool', 'address': 'MAIN.bSwitch', 'type': 'bool', 'remark': 'MAIN 布尔'},
            {'tag_id': 'main_nvalue_short', 'address': 'MAIN.nValue', 'type': 'short', 'remark': 'MAIN 短整数'},
            {'tag_id': 'main_fvalue_float', 'address': 'MAIN.fValue', 'type': 'float', 'remark': 'MAIN 浮点'},
            {'tag_id': 'gvl_ncounter_int', 'address': 'GVL.nCounter', 'type': 'int', 'remark': 'GVL 整数'},
        ],
    },
    ("siemens_s7", None): {
        "params": {"rack": 0, "slot": 1},
        "points": [
            {'tag_id': 'db1_dbx0_3_bool', 'address': 'DB1.DBX0.3', 'type': 'bool', 'remark': 'DB1.DBX0.3 位'},
            {'tag_id': 'm10_2_bool', 'address': 'M10.2', 'type': 'bool', 'remark': 'M10.2 位'},
            {'tag_id': 'q0_1_bool', 'address': 'Q0.1', 'type': 'bool', 'remark': 'Q0.1 输出位'},
            {'tag_id': 'i0_0_bool', 'address': 'I0.0', 'type': 'bool', 'remark': 'I0.0 输入位(只读)', 'access': 'r'},
            {'tag_id': 'db1_dbw2_short', 'address': 'DB1.DBW2', 'type': 'short', 'remark': 'DB1.DBW2 短整数'},
            {'tag_id': 'mw20_ushort', 'address': 'MW20', 'type': 'ushort', 'remark': 'MW20 无符号短'},
            {'tag_id': 'db1_dbd4_float', 'address': 'DB1.DBD4', 'type': 'float', 'remark': 'DB1.DBD4 浮点'},
            {'tag_id': 'db1_dbd10_double', 'address': 'DB1.DBD10', 'type': 'double', 'remark': 'DB1.DBD10 双精度'},
            {'tag_id': 'db1_dbs30_str', 'address': 'DB1.DBS30', 'type': 'string', 'remark': 'DB1.DBS30 字符串', 'length': 32},
        ],
    },
    ("opcua", None): {
        "points": [
            {'tag_id': 'ns_2_s_demo_bool_bool', 'address': 'ns=2;s=Demo.Bool', 'type': 'bool', 'remark': '布尔变量'},
            {'tag_id': 'ns_2_s_demo_short_short', 'address': 'ns=2;s=Demo.Short', 'type': 'short', 'remark': '短整数变量'},
            {'tag_id': 'ns_2_s_demo_int_int', 'address': 'ns=2;s=Demo.Int', 'type': 'int', 'remark': '整数变量'},
            {'tag_id': 'ns_2_s_demo_float_float', 'address': 'ns=2;s=Demo.Float', 'type': 'float', 'remark': '浮点变量'},
            {'tag_id': 'ns_2_s_demo_double_double', 'address': 'ns=2;s=Demo.Double', 'type': 'double', 'remark': '双精度变量'},
            {'tag_id': 'ns_2_s_demo_string_str', 'address': 'ns=2;s=Demo.String', 'type': 'string', 'remark': '字符串变量', 'length': 32},
        ],
    },
    ("opentcp", None): {
        "params": {"delimiter": "\r\n", "encoding": "utf-8"},
        "raw": {"send_text": "PING", "expect_contains": "PONG"},
    },
    ("mtconnect", None): {
        "snapshot": True,
        "points": [
            {'tag_id': 'xact_float', 'address': 'Xact', 'type': 'float', 'remark': 'X 轴实际位置', 'access': 'r'},
            {'tag_id': 'sov_float', 'address': 'Sov', 'type': 'float', 'remark': '主轴转速', 'access': 'r'},
            {'tag_id': 'program_str', 'address': 'program', 'type': 'string', 'remark': '程序号', 'length': 32, 'access': 'r'},
        ],
    },
}

from omniplc import (  # noqa: E402
    AllenBradleyEthIpClient,
    BeckhoffAdsClient,
    InovanceMcTcpClient,
    InovanceRtuClient,
    InovanceTcpClient,
    KeyenceHostLinkTcpClient,
    KeyenceHostLinkUdpClient,
    KeyenceMcTcpClient,
    KeyenceMcUdpClient,
    KeyenceSrClient,
    ModbusRtuClient,
    ModbusTcpClient,
    MelsecMcSerialClient,
    MelsecMcTcpClient,
    MelsecMcUdpClient,
    MelsecMxClient,
    MTConnectClient,
    OmronCipClient,
    OmronFinsTcpClient,
    OmronFinsUdpClient,
    OpenTcpClient,
    OpcUaClient,
    PanasonicMcTcpClient,
    PanasonicMewtocolTcpClient,
    PanasonicMewtocolUdpClient,
    SiemensS7Client,
    ToyopucTcpClient,
    ToyopucUdpClient,
    set_debug,
)
from omniplc.modbus.modbus import _coerce_word_order  # noqa: E402

INT_RANGES = {
    "short": (-30000, 30000),
    "ushort": (0, 60000),
    "int": (-(2 ** 30), 2 ** 30),
    "uint": (0, 2 ** 31 - 1),
    "long": (-(2 ** 62), 2 ** 62),
    "ulong": (0, 2 ** 63 - 1),
}
STRING_TYPES = ("bool", "short", "ushort", "int", "uint", "long", "ulong",
                "float", "double", "string")

_log_fh = None


def log(msg):
    """输出一行:控制台 + 汇总日志文件(logs/manual_test.log 追加)。"""
    print(msg)
    if _log_fh is not None:
        _log_fh.write(msg + "\n")
        _log_fh.flush()


def _p(conn, key, default):
    """取连接的 params 子项(缺省回退)。"""
    return conn.get("params", {}).get(key, default)


def build_client(conn):
    """按 driver 构造同步客户端(参数缺省与各驱动默认一致)。"""
    d = conn["driver"]
    ip = conn.get("ip", "127.0.0.1")
    port = conn.get("port")
    if d == "modbus_tcp":
        c = ModbusTcpClient(ip, port or 502, _p(conn, "station", 1))
        if _p(conn, "word_order", ""):
            c.word_order = _coerce_word_order(_p(conn, "word_order", "ABCD"))
        return c
    if d == "modbus_rtu":
        return ModbusRtuClient(_p(conn, "station", 1))
    if d == "melsec_mc_tcp":
        return MelsecMcTcpClient(ip, port or 2000, _p(conn, "frame", "3E"),
                                 _p(conn, "network_number", 0),
                                 _p(conn, "pc_number", 255))
    if d == "melsec_mc_udp":
        return MelsecMcUdpClient(ip, port or 2000, _p(conn, "frame", "3E"),
                                 _p(conn, "network_number", 0),
                                 _p(conn, "pc_number", 255))
    if d == "melsec_mc_serial":
        return MelsecMcSerialClient(_p(conn, "frame", "3C"),
                                    _p(conn, "station_number", 0),
                                    _p(conn, "network_number", 0),
                                    _p(conn, "pc_number", 255),
                                    _p(conn, "self_station_number", 0),
                                    _p(conn, "module_io", 0x03FF),
                                    _p(conn, "module_station", 0))
    if d == "melsec_mx":
        return MelsecMxClient(_p(conn, "logical_station_number", 0))
    if d == "omron_fins_tcp":
        return OmronFinsTcpClient(ip, port or 9600, _p(conn, "local_node", 0))
    if d == "omron_fins_udp":
        return OmronFinsUdpClient(ip, port or 9600)
    if d == "omron_cip":
        return OmronCipClient(ip, port or 44818,
                              _p(conn, "connected_messaging", False))
    if d == "keyence_hostlink_tcp":
        return KeyenceHostLinkTcpClient(ip, port or 8000)
    if d == "keyence_hostlink_udp":
        return KeyenceHostLinkUdpClient(ip, port or 8000)
    if d == "keyence_mc_tcp":
        return KeyenceMcTcpClient(ip, port or 5000,
                                  _p(conn, "network_number", 0),
                                  _p(conn, "pc_number", 255))
    if d == "keyence_mc_udp":
        return KeyenceMcUdpClient(ip, port or 5000,
                                  _p(conn, "network_number", 0),
                                  _p(conn, "pc_number", 255))
    if d == "keyence_sr":
        return KeyenceSrClient(ip, port or 9004, _p(conn, "scan_dwell", 1.0))
    if d == "inovance_tcp":
        return InovanceTcpClient(ip, port or 502, _p(conn, "station", 1))
    if d == "inovance_rtu":
        return InovanceRtuClient(_p(conn, "station", 1))
    if d == "inovance_mc_tcp":
        return InovanceMcTcpClient(ip, port or 2000,
                                   _p(conn, "network_number", 0),
                                   _p(conn, "pc_number", 255))
    if d == "panasonic_mc_tcp":
        return PanasonicMcTcpClient(ip, port or 2000,
                                    _p(conn, "network_number", 0),
                                    _p(conn, "pc_number", 255))
    if d == "panasonic_mewtocol_tcp":
        return PanasonicMewtocolTcpClient(ip, port or 1024,
                                          _p(conn, "station", 1))
    if d == "panasonic_mewtocol_udp":
        return PanasonicMewtocolUdpClient(ip, port or 1024,
                                          _p(conn, "station", 1))
    if d == "toyopuc_tcp":
        return ToyopucTcpClient(ip, port or 1025)
    if d == "toyopuc_udp":
        return ToyopucUdpClient(ip, port or 1025)
    if d == "ab_eip":
        return AllenBradleyEthIpClient(ip, port or 44818,
                                       _p(conn, "slot", 0),
                                       _p(conn, "connected_messaging", False))
    if d == "beckhoff_ads":
        return BeckhoffAdsClient(ip, _p(conn, "ads_port", 851),
                                 _p(conn, "net_id", ""))
    if d == "siemens_s7":
        return SiemensS7Client(ip, _p(conn, "rack", 0), _p(conn, "slot", 1),
                               port or 102, _p(conn, "dll_path", ""))
    if d == "opcua":
        return OpcUaClient(ip, port or 4840, _p(conn, "path", ""),
                           _p(conn, "endpoint", ""))
    if d == "opentcp":
        return OpenTcpClient(ip, port or 9000, _p(conn, "delimiter", "\r\n"),
                             _p(conn, "encoding", "utf-8"),
                             _p(conn, "append_delimiter", True),
                             _p(conn, "strip_delimiter", True),
                             _p(conn, "max_frame", 4096))
    if d == "mtconnect":
        return MTConnectClient(ip, port or 5000)
    raise ValueError("未知 driver:{!r}".format(d))


def apply_serial(client, conn):
    """串口驱动按配置 configure_serial(缺省 9600-8N1,汇川 8N2)。"""
    s = conn.get("serial")
    if not s:
        return
    stop = s.get("stop_bits", 2.0 if conn["driver"] == "inovance_rtu" else 1.0)
    client.configure_serial(s.get("port", "COM3"),
                            s.get("baud_rate", 9600),
                            s.get("data_bits", 8),
                            stop,
                            s.get("parity", "N"))


def gen_random(dtype, point, rng):
    """按类型生成随机写入值(float 预先按 32 位量化,保证能精确读回)。"""
    if dtype == "bool":
        return rng.choice([True, False])
    if dtype in INT_RANGES:
        lo, hi = INT_RANGES[dtype]
        return rng.randint(lo, hi)
    if dtype == "float":
        return struct.unpack(">f", struct.pack(">f", round(rng.uniform(-1000, 1000), 3)))[0]
    if dtype == "double":
        return round(rng.uniform(-1000000, 1000000), 6)
    if dtype == "string":
        length = min(int(point.get("length", 32)), 64)
        # 定宽字段语义:写只补齐到偶数字节(偶数长不带 \x00 终止符),读按首个
        # \x00 截断——短写后窗口残留上轮字节是字段语义而非协议错。因此随机
        # 串只在两种确定性形态里取:整窗写入,或奇数长度(写路径自动带终止符)。
        if rng.random() < 0.5 or length <= 1:
            n = length
        else:
            n = rng.randint(0, (length - 1) // 2) * 2 + 1
        return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))
    raise ValueError("未知类型:{!r}".format(dtype))


def values_equal(dtype, expect, actual):
    """读回比对:浮点带容差(跨协议栈浮点转发),其余精确。"""
    if dtype in ("float", "double"):
        try:
            return abs(expect - actual) <= 1e-5 * max(1.0, abs(expect))
        except TypeError:
            return False
    return expect == actual


def try_read(client, point):
    """读一个点(字符串走专用方法带长度/编码)。"""
    dtype = point["type"]
    if dtype == "string":
        return client.read_string(point["address"],
                                  int(point.get("length", 32)),
                                  point.get("encoding", "ascii"))
    return client.read(point["address"], dtype)


def try_write(client, point, value):
    """写一个点(字符串走专用方法)。"""
    if point["type"] == "string":
        return client.write_string(point["address"], value,
                                   point.get("encoding", "ascii"))
    return client.write(point["address"], point["type"], value)


def show(value):
    """结果值打印形式。"""
    return "None" if value is None else repr(value)


def test_point_rounds(client, point, rounds, rng):
    """一个点位连测 rounds 轮(读写点每轮新随机值),返回 (通过轮数, 总轮数)。

    全过打一行汇总(附各轮写入值);失败在当轮立即打出失败明细。
    """
    dtype = point["type"]
    tag_id = point.get("tag_id", "")
    remark = point.get("remark", "")
    label = "{}({})".format(remark, tag_id) if remark and tag_id else (remark or tag_id)
    tag = "{!r:<16} {:<7}".format(point["address"], dtype) + \
        (" 只读" if point.get("access") == "r" else "")
    if label:
        tag += " " + label
    if dtype not in STRING_TYPES:
        log("  [跳过] {} {}:未知类型".format(tag, label))
        return 0, rounds
    written = []
    for r in range(1, rounds + 1):
        tail = "第{}/{}轮 ".format(r, rounds) if rounds > 1 else ""
        try:
            if point.get("access") == "r":
                ok, value = try_read(client, point)
                if not ok:
                    log("  [FAIL] {} {}读失败:{}".format(tag, tail, client.last_error))
                    return r - 1, rounds
                written.append(show(value))
                continue
            expect = gen_random(dtype, point, rng)
            if not try_write(client, point, expect):
                log("  [FAIL] {} {}写失败:{}".format(tag, tail, client.last_error))
                return r - 1, rounds
            ok, actual = try_read(client, point)
            if not ok:
                log("  [FAIL] {} {}读失败:{}".format(tag, tail, client.last_error))
                return r - 1, rounds
            if not values_equal(dtype, expect, actual):
                log("  [FAIL] {} {}读回不符:写={} 读={}".format(
                    tag, tail, show(expect), show(actual)))
                return r - 1, rounds
            written.append(show(expect))
        except ValueError as exc:
            log("  [FAIL] {} {}参数错误:{}".format(tag, tail, exc))
            return r - 1, rounds
        except Exception as exc:  # 兜底:单点异常不中断整个连接的测试
            log("  [FAIL] {} {}异常:{}:{}".format(tag, tail, type(exc).__name__, exc))
            return r - 1, rounds
    detail = "(值: {})".format(", ".join(written)) if written else ""
    log("  [OK]   {} {}/{} 轮通过{}".format(tag, rounds, rounds, detail))
    return rounds, rounds


def test_raw_rounds(client, conn, rounds):
    """OpenTcp 原始收发 × rounds 轮,返回 (通过轮数, 总轮数)。"""
    raw = conn.get("raw")
    if not raw:
        return 0, 0
    send = raw.get("send_text", "")
    expect = raw.get("expect_contains")
    for r in range(1, rounds + 1):
        tail = "第{}/{}轮 ".format(r, rounds) if rounds > 1 else ""
        ok, reply = client.transact_text(send)
        if not ok:
            log("  [FAIL] 原始收发{}{!r} 失败:{}".format(tail, send, client.last_error))
            return r - 1, rounds
        if expect is not None and expect not in (reply or ""):
            log("  [FAIL] 原始收发{}{!r} 应答 {!r} 不含 {!r}".format(
                tail, send, reply, expect))
            return r - 1, rounds
    log("  [OK]   原始收发 {!r} → {!r}({}/{} 轮)".format(send, reply, rounds, rounds))
    return rounds, rounds


def run_connection(conn, rounds, rng):
    """连接 → 测点位/特例 × rounds 轮 → 关闭连接,返回 (通过, 总数, 是否连上)。"""
    name = conn.get("name", conn["driver"])
    target = conn.get("serial", {}).get("port") or "{}:{}".format(
        conn.get("ip", "127.0.0.1"), conn.get("port", "默认"))
    log("")
    log("==== {}({},{})".format(name, conn["driver"], target))
    try:
        client = build_client(conn)
        apply_serial(client, conn)
    except Exception as exc:
        log("  [FAIL] 构造客户端失败:{}:{}".format(type(exc).__name__, exc))
        return 0, 0, False
    for key in ("connect_timeout", "receive_timeout", "retries", "write_retries"):
        if key in conn:
            setattr(client, key, conn[key])
    try:
        if not client.connect():
            log("  [FAIL] 连接失败:{}".format(client.last_error))
            return 0, 0, False
        log("  连接:OK")
        passed = total = 0
        if conn.get("connect_only"):
            log("  (connect_only:仅测联通)")
        elif conn["driver"] == "keyence_sr":
            total = rounds
            ok_count = 0
            for r in range(1, rounds + 1):
                ok, code = client.scan(_p(conn, "bank"), _p(conn, "scan_timeout"))
                if ok:
                    ok_count += 1
                    log("  [OK]   第{}/{}轮 扫码:{!r}".format(r, rounds, code))
                else:
                    log("  [FAIL] 第{}/{}轮 扫码失败(模拟器未回码也常见,人工确认):{}".format(
                        r, rounds, client.last_error))
            passed = ok_count
        elif conn["driver"] == "mtconnect":
            if conn.get("snapshot"):
                total += rounds
                ok_count = 0
                for r in range(1, rounds + 1):
                    ok, items = client.snapshot()
                    if ok:
                        ok_count += 1
                    else:
                        log("  [FAIL] 第{}/{}轮 snapshot 失败:{}".format(
                            r, rounds, client.last_error))
                if ok_count == rounds:
                    log("  [OK]   snapshot {} 个数据项({}/{} 轮)".format(
                        len(items or {}), rounds, rounds))
                passed += ok_count
            p, t = test_points_rounds(client, conn, rounds, rng)
            passed, total = passed + p, total + t
        else:
            rp, rt = test_raw_rounds(client, conn, rounds)
            if rp < rt:
                log("  (原始收发失败,跳过点位)")
            else:
                p, t = test_points_rounds(client, conn, rounds, rng)
                passed, total = rp + p, rt + t
        log("  小计:{}/{} 通过".format(passed, total))
        return passed, total, True
    finally:
        client.disconnect()
        log("  连接已关闭")


def test_points_rounds(client, conn, rounds, rng):
    """逐点位 × rounds 轮,返回 (通过轮数合计, 总轮数)。"""
    passed = total = 0
    for point in conn.get("points", []):
        p, t = test_point_rounds(client, point, rounds, rng)
        passed += p
        total += t
    return passed, total


def _effective_frame(conn):
    """连接的生效帧型(缺省按走线默认;仅 MC 有帧概念)。"""
    return conn.get("params", {}).get("frame", FRAME_DEFAULTS.get(conn["driver"]))


def run(driver, frame=None):
    """协议脚本入口:一次只测一个协议。

    无配置文件 → 内置默认连接(127.0.0.1 + 协议默认端口 + 内置点位);
    传配置文件 → 按配置筛选(driver,及 frame 若给定),--ip/--port 覆盖配置值。
    """
    global _log_fh
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    label = driver if frame is None else "{}({} 帧)".format(driver, frame)
    parser = argparse.ArgumentParser(
        description="omniplc 手动联机测试:driver={}(联通 + 随机值写读 3 轮 + 日志汇总)".format(label))
    parser.add_argument("config", nargs="?", default=None,
                        help="JSON 配置文件(缺省用内置默认:127.0.0.1 + 协议默认端口 + 内置点位)")
    parser.add_argument("--ip", default=None,
                        help="目标 IP(缺省 127.0.0.1;配置模式下覆盖配置的 IP)")
    parser.add_argument("--port", type=int, default=None,
                        help="目标端口(缺省用协议默认端口;串口连接忽略)")
    parser.add_argument("--debug", action="store_true", help="强制打开报文调试")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子")
    parser.add_argument("--rounds", type=int, default=None, help="每点位读写轮数(默认 3)")
    args = parser.parse_args()

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_file():
            raise SystemExit("配置文件不存在:{},可从 tools/manual_test.json 复制修改".format(cfg_path))
        with open(str(cfg_path), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        conns = [c for c in cfg.get("connections", [])
                 if c.get("driver") == driver and (frame is None or _effective_frame(c) == frame)]
        if not conns:
            hint = "(driver={}, frame={})".format(driver, frame) if frame else "(driver={})".format(driver)
            raise SystemExit("配置里没有匹配的连接{}".format(hint))
        for conn in conns:
            if args.ip:
                conn["ip"] = args.ip
            if args.port:
                conn["port"] = args.port
    else:
        default = DEFAULT_CONNECTIONS.get((driver, frame))
        if default is None:
            raise SystemExit("没有 driver={!r} frame={!r} 的内置默认连接".format(driver, frame))
        conn = deepcopy(default)
        conn["name"] = label
        conn["driver"] = driver
        conn["ip"] = args.ip or DEFAULT_IP
        if args.port and "serial" not in conn:
            conn["port"] = args.port
        conns = [conn]

    rounds = args.rounds
    if rounds is None:
        rounds = int(cfg.get("rounds", 3)) if args.config else 3
    if args.debug or (args.config and cfg.get("debug")):
        set_debug(True)
    seed = cfg.get("seed") if args.config else None
    rng = random.Random(args.seed if args.seed is not None else seed)

    LOG_PATH.parent.mkdir(exist_ok=True)
    _log_fh = open(str(LOG_PATH), "a", encoding="utf-8")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    source = Path(args.config).name if args.config else "内置默认({}:{})".format(
        conns[0].get("ip", DEFAULT_IP), conns[0].get("port", "协议默认端口"))
    log("======== {} driver={} 配置={} 轮数={} ".format(stamp, driver, source, rounds))
    try:
        sum_passed = sum_total = 0
        failed = []
        for conn in conns:
            p, t, linked = run_connection(conn, rounds, rng)
            sum_passed += p
            sum_total += t
            if p < t or not linked:
                failed.append(conn.get("name", conn["driver"]))
    finally:
        _log_fh.close()
        _log_fh = None
    log("")
    log("==== 汇总:{}/{} 轮通过{}".format(
        sum_passed, sum_total,
        "" if not failed else ";失败连接:{}".format("、".join(failed))))
    log("日志文件:{}".format(LOG_PATH))
    raise SystemExit(0 if not failed else 1)
