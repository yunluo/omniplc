# -*- coding: utf-8 -*-
"""omniplc 手动联机测试公共运行器(各协议脚本 tools/test_<driver>.py 共用)。

配合外部 PLC 模拟软件(或直接接真机)人工验证:先启动模拟器,再运行对应
协议脚本,例如::

    uv run python tools/test_modbus_tcp.py
    uv run python tools/test_siemens_s7.py --debug

每个脚本只跑配置里 driver 匹配的连接,流程::

    连接 → 逐点位 随机值写→读回比对 × 3 轮(只读点仅读) → 关闭连接
    结果逐条打印,同时汇总追加到 logs/manual_test.log,任一失败退出码 1

配置文件(tools/manual_test.json)顶层 {"debug", "seed", "rounds", "connections"};
每个连接:name / driver / ip / port / params / serial / connect_only /
raw(opentcp)/ points:[{name, address, type, access, length, encoding}]。
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
LOG_PATH = ROOT / "logs" / "manual_test.log"
DEFAULT_CONFIG = ROOT / "tools" / "manual_test.json"
FRAME_DEFAULTS = {"melsec_mc_tcp": "3E", "melsec_mc_udp": "3E", "melsec_mc_serial": "3C"}
"""MC 各走线的缺省帧型(连接 params.frame 缺省时按此参与帧筛选)。"""

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
        length = int(point.get("length", 32))
        n = rng.randint(1, max(1, min(length, 32)))
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
    name = point.get("name", "")
    tag = "{!r:<16} {:<7}".format(point["address"], dtype) + \
        (" 只读" if point.get("access") == "r" else "")
    if dtype not in STRING_TYPES:
        log("  [跳过] {} {}:未知类型".format(tag, name))
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
    """协议脚本入口:只跑配置里 driver(及 frame,若给定)匹配的连接。"""
    global _log_fh
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    label = driver if frame is None else "{}({} 帧)".format(driver, frame)
    parser = argparse.ArgumentParser(
        description="omniplc 手动联机测试:driver={}(联通 + 随机值写读 3 轮 + 日志汇总)".format(label))
    parser.add_argument("config", nargs="?", default=str(DEFAULT_CONFIG),
                        help="JSON 配置文件(默认 tools/manual_test.json)")
    parser.add_argument("--debug", action="store_true", help="强制打开报文调试")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子")
    parser.add_argument("--rounds", type=int, default=None, help="每点位读写轮数(默认 3)")
    args = parser.parse_args()

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

    rounds = args.rounds or int(cfg.get("rounds", 3))
    if args.debug or cfg.get("debug"):
        set_debug(True)
    rng = random.Random(args.seed if args.seed is not None else cfg.get("seed"))

    LOG_PATH.parent.mkdir(exist_ok=True)
    _log_fh = open(str(LOG_PATH), "a", encoding="utf-8")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log("======== {} driver={} 配置={} 轮数={} ".format(stamp, driver, cfg_path.name, rounds))
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
