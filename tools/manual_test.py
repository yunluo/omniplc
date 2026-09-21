# -*- coding: utf-8 -*-
"""omniplc 手动联机测试脚本:JSON 配置驱动,全协议联通 + 随机值写读回验。

用途:配合外部 PLC 模拟软件(或直接接真机)做人工验证——
先启动模拟器,再运行本脚本;每个连接依次执行:

    连接 → 逐点位 写入随机值 → 读回比对(配置 access="r" 的点只读)
    (扫码枪只触发扫码;MTConnect 只读;OpenTcp 按配置收发原始帧)

结果逐条打印并汇总,任一失败退出码为 1,便于人工排查协议配置。

用法(uv 或本机任意 py3.7+):
    uv run python tools/manual_test.py                     # 默认读 tools/manual_test.json
    uv run python tools/manual_test.py 我的配置.json       # 自选配置文件
    uv run python tools/manual_test.py --only 三菱,西门子   # 按名称/驱动筛选(子串,逗号分隔)
    uv run python tools/manual_test.py --list              # 只列出连接与点位,不连设备
    uv run python tools/manual_test.py --debug             # 打开报文级调试(等价配置 debug:true)
    uv run python tools/manual_test.py --seed 42           # 固定随机种子(复现同一组随机值)

配置文件结构见 tools/manual_test.json(带全部协议的示例模板):
顶层 {"debug": bool, "seed": int, "connections": [...]};
每个连接:name / driver / ip / port / params(驱动特定参数) /
serial(串口驱动:port,baud_rate,data_bits,stop_bits,parity)/
connect_only(只测联通)/ points:[{name, address, type, access, length}]。

type 取值:bool/short/ushort/int/uint/long/ulong/float/double/string;
access 缺省 "rw"(写随机值读回比对),"r" 为只读点(如输入区、扫码内容)。
"""
import argparse
import json
import random
import string
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

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
    raise ValueError("未知 driver:{!r}(见 tools/manual_test.json 的 driver 列表)".format(d))


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


def test_points(client, conn, rng):
    """逐点位随机值写读回验,返回 (通过数, 总数)。"""
    passed = 0
    total = 0
    for point in conn.get("points", []):
        total += 1
        name = point.get("name", "")
        address = point["address"]
        dtype = point["type"]
        tag = "{!r:<18} {:<7}".format(address, dtype) + (" 只读" if point.get("access") == "r" else "")
        try:
            if dtype not in STRING_TYPES:
                print("  [跳过] {} {}:未知类型".format(tag, name))
                continue
            if point.get("access") == "r":
                ok, value = try_read(client, point)
                if ok:
                    passed += 1
                    print("  [OK]   {} {} 读={}".format(tag, name, show(value)))
                else:
                    print("  [FAIL] {} {} 读失败:{}".format(tag, name, client.last_error))
                continue
            expect = gen_random(dtype, point, rng)
            if not try_write(client, point, expect):
                print("  [FAIL] {} {} 写失败:{}".format(tag, name, client.last_error))
                continue
            ok, actual = try_read(client, point)
            if ok and values_equal(dtype, expect, actual):
                passed += 1
                print("  [OK]   {} {} 写={} 读={}".format(tag, name, show(expect), show(actual)))
            else:
                detail = "读失败:{}".format(client.last_error) if not ok else \
                    "写={} 读={}".format(show(expect), show(actual))
                print("  [FAIL] {} {} 读回不符:{}".format(tag, name, detail))
        except ValueError as exc:
            print("  [FAIL] {} {} 参数错误:{}".format(tag, name, exc))
        except Exception as exc:  # 兜底:单点异常不中断整个连接的测试
            print("  [FAIL] {} {} 异常:{}:{}".format(tag, name, type(exc).__name__, exc))
    return passed, total


def test_raw(client, conn):
    """OpenTcp 原始收发(配置 raw:{send_text, expect_contains}),返回 (通过, 总数)。"""
    raw = conn.get("raw")
    if not raw:
        return 0, 0
    send = raw.get("send_text", "")
    ok, reply = client.transact_text(send)
    expect = raw.get("expect_contains")
    if not ok:
        print("  [FAIL] 原始收发 {!r} 失败:{}".format(send, client.last_error))
        return 0, 1
    if expect is not None and expect not in (reply or ""):
        print("  [FAIL] 原始收发 {!r} 应答 {!r} 不含 {!r}".format(send, reply, expect))
        return 0, 1
    print("  [OK]   原始收发 {!r} → {!r}".format(send, reply))
    return 1, 1


def run_connection(index, count, conn, rng):
    """测一个连接,返回 (通过点数, 总点数, 连接是否建立)。"""
    name = conn.get("name", conn["driver"])
    target = conn.get("serial", {}).get("port") or "{}:{}".format(
        conn.get("ip", "127.0.0.1"), conn.get("port", "默认"))
    print("")
    print("==== [{}/{}] {}({},{})".format(index, count, name, conn["driver"], target))
    try:
        client = build_client(conn)
        apply_serial(client, conn)
    except Exception as exc:
        print("  [FAIL] 构造客户端失败:{}:{}".format(type(exc).__name__, exc))
        return 0, 0, False
    for key in ("connect_timeout", "receive_timeout", "retries", "write_retries"):
        if key in conn:
            setattr(client, key, conn[key])
    if not client.connect():
        print("  [FAIL] 连接失败:{}".format(client.last_error))
        return 0, 0, False
    print("  连接:OK")
    passed, total = 0, 0
    try:
        if conn.get("connect_only"):
            print("  (connect_only:仅测联通)")
        elif conn["driver"] == "keyence_sr":
            total = 1
            ok, code = client.scan(_p(conn, "bank"), _p(conn, "scan_timeout"))
            if ok:
                passed = 1
                print("  [OK]   扫码结果:{!r}".format(code))
            else:
                print("  [FAIL] 扫码失败(模拟器未回码也常见,人工确认):{}".format(client.last_error))
        elif conn["driver"] == "mtconnect":
            if conn.get("snapshot"):
                total += 1
                ok, items = client.snapshot()
                if ok:
                    passed += 1
                    print("  [OK]   snapshot:{} 个数据项".format(len(items or {})))
                else:
                    print("  [FAIL] snapshot 失败:{}".format(client.last_error))
            p2, t2 = test_points(client, conn, rng)
            passed, total = passed + p2, total + t2
        else:
            rp, rt = test_raw(client, conn)
            if rp < rt:
                print("  (原始收发失败,跳过点位)")
            else:
                pp, pt = test_points(client, conn, rng)
                passed, total = rp + pp, rt + pt
    finally:
        client.disconnect()
    print("  小计:{}/{} 通过".format(passed, total))
    return passed, total, True


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="omniplc 手动联机测试(联通 + 随机值写读回验)")
    parser.add_argument("config", nargs="?", default=str(Path(__file__).parent / "manual_test.json"),
                        help="JSON 配置文件(默认 tools/manual_test.json)")
    parser.add_argument("--only", default="", help="只测名称/驱动含关键字的连接(逗号分隔子串)")
    parser.add_argument("--list", action="store_true", help="只列出连接与点位,不连设备")
    parser.add_argument("--debug", action="store_true", help="强制打开报文调试")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        raise SystemExit("配置文件不存在:{},可从 tools/manual_test.json 复制修改".format(cfg_path))
    with open(str(cfg_path), "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    if args.debug or cfg.get("debug"):
        set_debug(True)
    rng = random.Random(args.seed if args.seed is not None else cfg.get("seed"))

    conns = cfg.get("connections", [])
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        conns = [c for c in conns
                 if any(k in c.get("name", "").lower() or k in c["driver"].lower()
                        for k in keys)]
    if args.list:
        for c in conns:
            target = c.get("serial", {}).get("port") or "{}:{}".format(
                c.get("ip", "-"), c.get("port", "默认"))
            print("{:<22} {:<22} {:<16} 点位 {}".format(
                c.get("name", "-"), c["driver"], target, len(c.get("points", []))))
        raise SystemExit(0)
    if not conns:
        raise SystemExit("没有可测的连接(检查 --only 关键字)")

    sum_passed = sum_total = 0
    failed_conns = []
    for i, conn in enumerate(conns, 1):
        p, t, linked = run_connection(i, len(conns), conn, rng)
        sum_passed += p
        sum_total += t
        if p < t or not linked:
            failed_conns.append(conn.get("name", conn["driver"]))

    print("")
    print("==== 总计:{}/{} 点通过{}".format(sum_passed, sum_total,
          "" if not failed_conns else ";失败连接:{}".format("、".join(failed_conns))))
    raise SystemExit(0 if not failed_conns else 1)


if __name__ == "__main__":
    main()
