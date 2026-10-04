# 各协议用法示例

按品牌分节的用法速查。**所有客户端共享的通用 API 面**(读写原语/批量读写/
点位表/监视器/超时重试/错误处理/全局开关/异步层)见下一节,各协议节只讲
该协议的特有能力。构造参数、地址语法与能力细节以各客户端 docstring 与
[architecture.md](architecture.md) 为准;失败分类与现场排障见
[troubleshooting.md](troubleshooting.md)。

安装可选依赖:`pip install 'omniplc[serial|mx|opcua]'`(对应小节标注)。

## 通用 API 面(所有客户端共享)

### 读写原语

读返回 `(是否成功, 值)`、写返回 `bool`,失败**不抛异常**(原因在
`last_error`);参数非法抛 `ValueError`。

```python
ok, b = client.read_bool("M100")          # 位
ok, i = client.read_ushort("D100")        # 16 位:short / ushort
ok, i = client.read_int("D200")           # 32 位:int / uint;另有 long / ulong(64 位)
ok, f = client.read_float("D200")         # 32 位浮点;另有 double(64 位)
ok, s = client.read_string("D300", length=20, encoding="gbk")   # 字符串(按协议定长定编码)
ok = client.write_ushort("D100", 1234)
ok = client.write_string("D300", "PV=1234")
```

### 超时 / 重试 / 退避 / 心跳(运行期属性,赋值即生效)

```python
client.connect_timeout = 5.0      # 建连超时(秒)
client.receive_timeout = 3.0      # 单事务收包超时;TCP 修改立即下发 socket
client.retries = 1                # 读重试次数(默认 0)
client.write_retries = 0          # 写重试(默认 0——超时重试对非幂等写有双写风险)
client.reconnect_backoff = True   # 连接失败指数退避门控(默认开,0.5s→30s 封顶)
client.next_connect_in            # 门控剩余秒数(None = 未激活)
client.heartbeat_interval = 30.0  # 应用层心跳周期(仅支持探活的驱动生效;0 = 关)
```

### 失败原因与告警分级

```python
ok, value = client.read_float("hr100")
if not ok:
    print(client.last_error)             # 人类可读原因(omniplc.set_lang("en") 切英文)
    print(client.last_error_category)    # TRANSPORT / PROTOCOL / DEVICE / TIMEOUT / UNKNOWN
    print(client.last_error_code)        # PLC 原始错误码(语言无关,接上位告警分级)
```

分类语义与现场排障动线见 [troubleshooting.md](troubleshooting.md)。

### 连接健康统计

```python
s = client.stats     # ClientStats:connect_count/disconnect_count/transactions/
                     # error_count/device_error_count/heartbeat_ok/heartbeat_fail/
                     # last_rtt/last_success_at/...
ok = client.ping()   # 手动探活一次(仅支持探活命令的驱动)
```

### 点位表(按名读写 + 工程量缩放)

```python
from omniplc import Tag, TagTable

table = TagTable([
    Tag(tag_id="furnace_temp", address="hr100", data_type="float",
        scale=0.1, remark="炉温"),        # 读值 = 原始 × scale + offset
    Tag(tag_id="pump_running", address="c0", data_type="bool", remark="泵运行"),
])
client.bind_tags(table)
ok, value = client.read_tag("furnace_temp")    # 按点位标识读写,自动套缩放
ok = client.write_tag("pump_running", True)
```

表构造后严格只读(不提供 add);`Monitor` 直接收 `TagTable`。

### 监视器(周期轮询 + 本地快照 + 变更事件)

```python
mon = client.create_monitor(
    {"炉温": ("hr0", "float"), "压力": ("hr2", "ushort")},
    interval=1.0,
    on_change=lambda ev: print(ev.tag_id, ev.old, "→", ev.new, ev.quality),
    on_disconnect=lambda: print("采集失败期开始"),
    deadband=0.5,                        # 全点统一死区;或 {"炉温": 0.5} 逐点指定
)
mon.start()
snap = mon.get("炉温")     # PointSnapshot(quality, value, updated_at)——纯本地不发报文
mon.get_all()              # 全部点位快照
mon.stats                  # cycle_count / consecutive_fails / slow_cycles /
                           # skipped_ticks(退避期跳拍)/ change_events / ...
mon.stop()
```

质量三态:`INITIAL`(从未成功)/ `GOOD`(最新值)/ `STALE`(失败期保留的
旧值)——**消费方必须检查 quality**,拿 STALE 旧值当最新值是采集系统常见
自伤。STRING 不支持(批量读不收变长);建议监视器独占客户端实例。

`deadband`(值变化死区,默认 0 = 关):数值点新值与**该点上次报告值**的
绝对差小于死区视为未变——专治浮点传感器抖动逐周期误发 `on_change`;
与上一拍比较的是"上次报告值"而非"上一拍快照",缓慢漂移累计越过死区
照样报(不会永远压在死区内漏报)。死区内快照照常刷新,仅压事件;
首拍/质量跨界/bool/NaN 跳变不受压制。

### 批量读三入口 / 批量写两入口

```python
pairs = client.read_many(["hr0", "hr2", "hr4"], "float")   # 逐点独立容错 [(ok, 值)]
ok, values = client.read_range("hr0", 100, "float")        # 连续区段单事务(整批容错)
ok, values = client.read_batch([("D100", "short"), ("M10", "bool")])  # 协议原生合并

ok = client.write_many(["M0", "M1", "M2"], [True, False, True])       # 逐点写
ok = client.write_batch([("Q0", "bool", True), ("hr10", "ushort", 7)])  # 协议原生合并写
```

合并形态对照见下文「批量读取」「批量写」两节。

### 全局开关(进程级,所有客户端生效)

```python
import omniplc

omniplc.set_debug(True)                    # 收发报文十六进制实时输出(走 logging)
omniplc.set_frame_recorder(True)           # 报文黑匣子:只存不打印,常驻最近 1000 帧
for rec in omniplc.recorded_frames():      # 故障后取现场(墙钟时间升序)
    print(rec.at, rec.direction, rec.label, rec.data.hex())
omniplc.set_lang("en")                     # 报错文案切英文(默认中文)
```

### 异步两层(包装层 A* / 原生层 Async*)

```python
# 包装层:同步类名前加 A,方法全是协程(内部单工作线程驱动同步实例)
import asyncio
from omniplc.aio import AModbusTcpClient

async def main() -> None:
    client = AModbusTcpClient("192.168.0.10", 502, 1)
    assert await client.connect() is True
    ok, value = await client.read_float("hr100")
    await client.close()

asyncio.run(main())

# 原生层(omniplc.native,Async* 前缀):原生 asyncio 协议栈,零第三方依赖
from omniplc.native import AsyncModbusTcpClient

async def main() -> None:
    async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = await client.read_float("hr100")

asyncio.run(main())
```

包装层 30 客户端全镜像(`omniplc.aio`);原生层已覆盖 Modbus TCP、
MC 3E(TCP/UDP)、FINS(TCP/UDP)、汇川两走线(`omniplc.native`,不进
根包 `__all__`);两层差异与取舍见 [async.md](async.md)。


## Modbus(TCP / RTU)

```python
from omniplc import ModbusTcpClient, DataType

client = ModbusTcpClient(ip_address="192.168.0.10", port=502, station=1)
client.receive_timeout = 3.0        # 超时走属性,不进构造函数
client.connect()

ok, value = client.read_float("hr100")     # 读返回 (是否成功, 值)
ok = client.write_float("hr100", 3.14)     # 写返回 bool

# 掩码写(FC22):设备侧原子位修改,替代读-改-写两段事务
ok = client.write_mask_register("hr100", and_mask=0xFFFE, or_mask=0x0001)
ok = client.write_mask_register("hr100", 0xFFFE, 0x0001, byte_order="little")  # 少数施耐德机型按本机字序

# 读写多寄存器(FC23):单事务「先写后读」,读到的是写入生效后的值
ok, values = client.read_write_registers("hr200", 2, "hr100", [0x0001, 0x0002])

# FIFO 队列(FC24):返回先进先出的寄存器值列表(单次 ≤31 个)
ok, fifo = client.read_fifo_queue("hr1000")

# 设备标识(FC43/14):厂商名/产品代码/版本号等;More Follows 自动翻页
ok, info = client.read_device_id()
ok, raw = client.read_device_object(0x02)   # 单个对象(个体访问),返回原始字节

# 诊断/事件(FC07/08/11/12/17)
ok, status = client.read_exception_status()          # FC07:1 字节异常状态
ok, errors = client.diagnostics(0x000C)             # FC08:返回 2 字节数据域
ok, count = client.get_comm_event_counter()         # FC11
ok, log = client.get_comm_event_log()               # FC12:状态/事件/报文计数 + events
ok, sid = client.report_server_id()                 # FC17:(从站 ID, 运行指示, 附加数据)

# 文件记录(FC20/21):子请求 (文件号, 起始记录号, 记录长/值)
ok, records = client.read_file_record([(4, 1, 2), (3, 9, 2)])
ok = client.write_file_record([(4, 1, [0x0001, 0x0002])])
```

RTU(串口,需 pyserial):`ModbusRtuClient(station=1)` + `configure_serial("COM3",
baud_rate=9600)`;`inter_frame_delay` 帧间静默(默认 0)与广播后
`broadcast_turnaround`(默认 200ms)可配。

## 三菱 MC(3E/4E/1E 以太网 / 1C/3C/4C 串口 / MX Component)

```python
from omniplc import MelsecMcTcpClient, McFrame

# frame=FRAME_3E/FRAME_4E(QnA 兼容)或 FRAME_1E(A 兼容)
mc = MelsecMcTcpClient(ip_address="192.168.3.39", port=2000, frame=McFrame.FRAME_3E)
mc.connect()
ok, value = mc.read_ushort("D100")
ok = mc.write_bool("M100", True)
ok, values = mc.read_batch([("D100", "short"), ("M100", "bool")])  # 0406 多块批量读,单事务

# 串口帧(C24 模块,需 pyserial):1C=A 兼容 ASCII 格式 4 / 3C=QnA ASCII / 4C=二进制
from omniplc import MelsecMcSerialClient
mc_sio = MelsecMcSerialClient(frame=McFrame.FRAME_4C)
mc_sio.configure_serial("COM3", baud_rate=9600)   # 串口参数须与 C24「传送设定」一致
ok, value = mc_sio.read_ushort("D100")

# MX Component(Windows,需 omniplc[mx]):逻辑站号在通信设置实用程序中配置
from omniplc import MelsecMxClient
mx = MelsecMxClient(logical_station_number=1)
ok, values = mx.read_batch([("M10", "bool"), ("D100", "short"), ("D200", "int")])
```

扩展:`random_read(word_items, double_word_items)` / `random_write(...)`——
0403/1402 乱序不连续软元件单事务,按「字软元件 / 双字软元件」两组传;
`get_cpu_type()`(0101,CPU 型号 + 模型代码,即探活命令);
`read_range("D0", 100, "short")` 连续区段单事务;字符串
`read_string` / `write_string`。

MX Component 另有(Windows,`omniplc[mx]`):`get_clock()` / `set_clock(...)`
PLC 时钟读写、`get_error_message(code)` 错误码取文本、
`write_batch` 按 16 位类型合并写。

## 欧姆龙 FINS / NJ·NX CIP

```python
import datetime

from omniplc import OmronFinsUdpClient

# TCP 自动做节点分配握手,UDP 无握手
fins = OmronFinsUdpClient(ip_address="192.168.250.1", port=9600)
ok, value = fins.read_ushort("D100")
ok = fins.write_bool("CIO0.5", True)
ok, values = fins.read_batch([("D100", "short"), ("CIO0.5", "bool")])  # 0104 多存储区读
ok, status = fins.read_cpu_unit_status()   # 0101 CPU 单元状态:运行模式/故障状态等字典

# PLC 时钟(W342 §5-3-19/20):读返回 FinsClock(年=右两位,星期 0=周日);
# 写收 FinsClock 或 datetime(星期自动换算;需 PLC 侧访问权、未开网络写保护)
ok, clock = fins.read_clock()
print(clock.year, clock.month, clock.day, clock.day_of_week)  # 26 10 3 6
ok = fins.write_clock(clock)                                   # 原样回写保持星期
ok = fins.write_clock(datetime.datetime.now())                 # 对时到当前时刻

# NJ/NX CIP(44818):Sysmac 变量自描述,地址即变量名(TestVar / MyArray[5] / Motor[2].Speed)
from omniplc import OmronCipClient
nj = OmronCipClient(ip_address="192.168.0.10", connected_messaging=True)  # 连接型可选
ok, value = nj.read_int("TestVar")
# NJ 不支持 Logix 符号点位枚举:nj.list_tags() 显式拒绝(ValueError,不发包)
```

## 罗克韦尔 AB EtherNet/IP(Logix 标签)

```python
from omniplc import AllenBradleyEthIpClient
# 地址即标签名:MyDint / MyArray[5] / MyUdt.Member / MyDint.3(位)/ Program:prog.Tag
ab = AllenBradleyEthIpClient(ip_address="192.168.1.20", port=44818, slot=0)
ab.connect()
ok, value = ab.read_int("MyDint")
ok, values = ab.read_batch([("MyDint", "int"), ("MyReal", "float")])  # 0x0A 多服务包,≤32 条
ok, info = ab.get_plc_info()      # Identity Object
ok, tags = ab.list_tags()         # 0x55 符号枚举(自动分页):AbTagEntry(名称/类型/is_struct/维度)
ok, ident = ab.list_identity()    # EtherNet/IP 身份识别(设备发现,无须先 connect)
ok, raw = ab.get_attribute_all(0x01, 1)                   # Get Attribute All
ok, pairs = ab.get_attribute_list(0x04, 0x63, [1, 3])     # 批量读属性 → [(属性 ID, 值)]
ok, reply = ab.generic_message(0x0E, 0x073, 1, body=b"\x00")  # 任意服务/类/实例透传
ab_c = AllenBradleyEthIpClient(ip_address="192.168.1.20", connected_messaging=True,
                               rpi_us=100_000)  # connected 消息,大批量轮询吞吐更高
```

## 基恩士 KV(Host Link / MC 协议兼容)

```python
from omniplc import KeyenceHostLinkTcpClient
kv = KeyenceHostLinkTcpClient(ip_address="192.168.0.10", port=8000)
ok, value = kv.read_ushort("DM100")   # 地址:DM100 / R515 / W100
ok = kv.write_bool("R515", True)

# MC 协议兼容(SLMP 3E,5000):地址 R5(位)/ DM100 / B1F / W10 / ZR100
from omniplc import KeyenceMcTcpClient
kvmc = KeyenceMcTcpClient(ip_address="192.168.1.22", port=5000)
ok, value = kvmc.read_ushort("DM100")
```

> **KV 的 X/Y 等设备读法**(真机记录):该 KV 的 SLMP 兼容接受标准三菱记号,
> 请用三菱客户端 `MelsecMcTcpClient`/`MelsecMcUdpClient` 读;`KeyenceMc*` 仅收
> R/B/W/DM/ZR,传 X/Y 在组帧期即被拒。

## 汇川 H3U/H5U(Modbus 映射 / MC 兼容)

```python
from omniplc import InovanceTcpClient, InovanceMcTcpClient
# Modbus 映射:地址 D100 / R100 / M10 / SM10 / X17(八进制,H5U 到 X1777)/ D100.3
# T/C 位=接点、字=当前值;C200~C255 为 32 位计数器(read_uint/read_int/read_float)
h3u = InovanceTcpClient(ip_address="192.168.1.88", port=502, station=1)
ok, value = h3u.read_ushort("D100")

# MC 兼容(Easy/H5U 固件 V6.4.0.0+ 的「MC配置」):R 与 D 统一编址(R100=D8100),
# X/Y 八进制命名;端口无出厂默认,须与 AutoShop「MC配置」一致
imc = InovanceMcTcpClient(ip_address="192.168.1.88", port=2000)
ok, value = imc.read_ushort("R100")
```

## 松下 FP0H/FP7(MC 兼容 / MEWTOCOL)

```python
from omniplc import PanasonicMcTcpClient, PanasonicMewtocolTcpClient
# MC:位软元件按「字号+位号」(R1F=字1位F/R1.15),R9000+ 为 SM,D90000+ 为 SD
pmc = PanasonicMcTcpClient(ip_address="192.168.0.10", port=2000)
ok, value = pmc.read_ushort("D100")

# MEWTOCOL(1024):接点 X/Y/R/T/C/L + 数据 D/L/F/S/K(K=经过值,S=设定值)
mew = PanasonicMewtocolTcpClient(ip_address="192.168.0.10", port=1024, station=1)
ok = mew.write_bool("Y0.3", True)
```

## 基恩士 SR 扫码枪

```python
from omniplc import KeyenceSrClient
sr = KeyenceSrClient(ip_address="192.168.0.10", port=9004, scan_dwell=1.0)
sr.connect()
ok, code = sr.scan()        # LON → 窗口 → LOFF → 读应答
ok, code = sr.scan(bank=2)  # 指定库位号(读码器预设切换)
ok = sr.reset()             # 复位读码器
```

## 海康机器人 ID 智能读码器(Modbus 模式)

```python
from omniplc import HikrobotIdModbusClient
# 读码器 = Modbus TCP 从站(IDMVS 通信配置选 Modbus、工作模式=服务端);
# 本类继承 ModbusTcpClient,实例本身也是 Modbus 主站
reader = HikrobotIdModbusClient("192.168.0.10", 502, station=0)
reader.connect()
ok, code = reader.scan()          # 完整握手;OK→(True, 条码文本),NG→(False, None)
status = reader.read_status()     # Trigger Ready / Acquiring / Decoding / OK / NG / Fault
reader.clear_error()              # General Fault 清除
# 结果串内容 = 读码器 IDMVS「数据处理」配置的输出(质量/码制等可配置并入)
```

## 海康机器人 ID 读码器(TCP 命令协议)

```python
from omniplc import HikrobotIdTcpClient
# 双通道:命令通道(IDMVS「通信命令控制」设端口)+ 结果通道(「通信配置>TCP服务器」设端口)
reader = HikrobotIdTcpClient("192.168.0.10", command_port=9989, result_port=9988)
reader.connect()
reader.set_acquisition(True)      # <Set,Acq,1> 开始采集(或 IDMVS 工具栏)
ok, text = reader.scan()          # <Exec,TriSoft> → 等结果推送;NoRead→(False, None)
ok, n = reader.get_acquisition()  # <Get,Acq>
ok = reader.command("Set", "1DNum", "5")   # 手册 §3 指令列表全量透传
# 结果文本 = 读码器「输出格式化」模板原文(<code_type>/<code_quality> 等占位符随文输出)
```

## 海康机器人 ID 智能读码器(MvCodeReaderSDK)

```python
from omniplc import HikrobotIdSdkClient
# 需 MvCodeReaderSDK 运行库(IDMVS Development 开发包);按解释器位数自动选 win32/win64
reader = HikrobotIdSdkClient("192.168.1.100", sdk_dir=r"D:\MvCodeReaderSDK\SDK")
reader.connect()                       # 枚举匹配 IP → 建句柄 → 开设备 → 起流
reader.set_enum_value("TriggerMode", 1)     # 触发模式开
reader.set_enum_value("TriggerSource", 7)   # 触发源 = 软触发
ok, frame = reader.scan(timeout=5.0)   # TriggerSoftware → 取帧(全量元数据)
if ok:
    for code in frame.codes:           # 多码遍历
        print(code.content, code.code_type_name, code.angle_deg,
              code.quality.over_quality, code.points)
ok, n = reader.get_int_value("Width")  # GenICam 参数访问
```

## 海康机器人 ID 读码器(串口)

```python
from omniplc import HikrobotIdSerialClient
# 读码器 RS-232:触发源=串口触发(start/stop)+「串口通讯协议」使能+换行符使能
reader = HikrobotIdSerialClient()             # 触发/停止文本默认 start/stop,可配
reader.configure_serial("COM3", 115200)       # 与读码器串口配置一致
reader.connect()
ok, code = reader.scan()                      # start → 结果行 → stop
# 多码编排:reader.trigger() → read_result()×N → reader.stop()
```

## 丰田 TOYOPUC 计算机链接

```python
from omniplc import ToyopucTcpClient
toyopuc = ToyopucTcpClient(ip_address="192.168.0.10", port=1025)
ok, value = toyopuc.read_ushort("D0100")   # 编号为十六进制
ok = toyopuc.write_bool("M0201", True)
```

## OPC-UA(需 omniplc[opcua])

```python
from omniplc import OpcUaClient
opc = OpcUaClient("192.168.0.10", 4840,
                  auto_resubscribe=True)   # 断线自动重订(默认关)
ok, value = opc.read_float("ns=2;s=Device.Temperature")
ok = opc.write_ushort("ns=2;s=Device.Speed", 1200)

# 推模式:订阅数据变化 / 事件;浏览地址树
def on_change(value, node_id, ts): ...
ok, sub = opc.subscribe_data_change("ns=2;s=Device.Temperature", on_change,
                                    sampling_interval_ms=1000,
                                    deadband_value=0.5)  # 死区可选(DataChangeFilter)
ok, sub = opc.subscribe_event("i=2253", on_event)         # 事件订阅(EventFilter 可透传)
ok, tree = opc.browse("Root", recursive=True)             # {node_id: {browse_name, ...}}
```

订阅句柄 `OpcUaSubscription.unsubscribe()` 幂等(退订 = 同时移除重订意图);
`disconnect()` 释放服务端订阅资源但保留意图。开启 `auto_resubscribe` 后,
重连成功(显式 `connect()` 或事务惰性重连)按订阅意图自动重建(best-effort,
重建产生**新**句柄,旧句柄失效;结果以 `active_subscriptions` 为准)。
默认 SelectClauses 聚合 BaseEventType 全部属性(含 SourceNode/Time);
自定义字段可传 `event_filter`。

## CNC 机床数采 MTConnect(标准库实现,零第三方依赖)

```python
from omniplc import MTConnectClient
cnc = MTConnectClient("192.168.0.10", 5000)   # Agent 默认 5000;数据项 id 即地址
ok, speed = cnc.read_float("Sspeed")   # 主轴转速(文本值自动转 float)
ok, items = cnc.snapshot()             # 全量当前值快照;先看机器实际提供哪些数据项
ok, alarms = cnc.read_conditions()     # 条件项(Fault/Warning/Normal)
ok, device = cnc.probe()               # 设备信息(name/uuid 等)
ok, devs = cnc.probe_all()             # 多设备 Agent 的全部设备信息
ok, page = cnc.read_sample(from_sequence=100)   # /sample 历史样本(序列号续传)
ok, assets = cnc.read_assets()         # 资产(刀具等)
```

## 西门子 S7(自研 S7comm 栈,零第三方依赖)

```python
from omniplc import SiemensS7Client
# S7-1200/1500 需勾选「允许来自远程对象的 PUT/GET 通信访问」,DB 须为非优化块
# v0.53 起为自研 S7comm 协议栈(核心零依赖,无需安装任何扩展)
s7 = SiemensS7Client("192.168.0.1", rack=0, slot=1)  # 300/400 的 CPU 常在槽位 2
ok, temp = s7.read_float("DB1.DBD6")   # DB 双字起点,REAL
ok = s7.write_bool("DB1.DBX0.3", True) # DB 位(非原子读-改-写;多写者请 write 整字节)
ok, current = s7.read_ushort("MW10")   # Merker 字
ok, text = s7.read_string("DB1.DBS20", length=32)   # S7 String(头 2 字节声明/实际长)
ok, wtext = s7.read_wstring("DB1.DBW40", length=32) # S7 WString(UTF-16,中文/日文)
ok = s7.write_wstring("DB1.DBW60", "中文")
ok, state = s7.get_cpu_state()                      # CPU 状态(Run/Stop/...)
```

## 批量读取(默认逐点 / 协议原生单事务)

`read_many(地址列表, 数据类型)` 默认**逐点独立容错**;`read_batch([(地址, 类型), …])`
是协议级批量入口,支持的驱动覆写为**单事务整批语义**(任一地址非法或设备拒绝则
整批失败,原因在 `last_error`;要逐点容错请逐点 `read`):

| 驱动 | 单事务形态 |
|---|---|
| 三菱 MC 3E/4E(KV/汇川/松下 MC 兼容子类同享) | 0406 多块批量读(混软元件,总块数 ≤120) |
| 欧姆龙 FINS | 0104 多存储区读(以太网上限 167 条) |
| AB / NJ·NX CIP | 0x0A 多服务包(≤32 条且 ≤480B,超限自动拆多笔按序执行) |
| OPC-UA | UA Read 原生多节点(asyncua 单请求) |
| MX Component | 16 位类型合并 ReadDeviceRandom;32/64 位类型各走一笔块读 |
| Modbus | 按 (区域,类型) 分组、组内连续地址合并为单条 FC(K 笔,典型 1 笔) |

## 批量写(默认逐点 / 协议原生合并)

`write_many(地址列表, 值列表)` 逐点写(基类契约;Modbus 覆写保留
`List[bool]` 返回);`write_batch([(地址, 类型, 值), …])` 是协议级合并写:

| 驱动 | 合并形态 |
|---|---|
| Modbus(TCP/RTU 及其子类) | 按 (区域,类型) 分组、组内连续合并:位走 FC15 多线圈、寄存器走 FC16 多寄存器(上限 1968 位 / 123 字);寄存器位写(读-改-写)不参与合并,排在 FC16 之前保序 |
| 三菱 MX Component | `write_batch` 16 位同型合并块写;其余逐点 |
| 其余驱动 | 逐点(协议无合并写面) |

**写重试双写警示**:写失败重试有双写风险(超时只证明响应未到达,写可能
已被执行),`write_retries` 默认 0——非幂等写(计数/脉冲/步进)保持默认,
详见 [troubleshooting.md](troubleshooting.md)。

## 各走线默认端口对照

| 客户端 | 协议 | 默认端口 |
|---|---|---|
| `ModbusTcpClient` | Modbus TCP | 502 |
| `ModbusRtuClient` | Modbus RTU | 串口(`configure_serial`) |
| `MelsecMcTcpClient` / `UdpClient` | 三菱 MC 3E/4E | 2000 |
| `MelsecMcSerialClient` | 三菱 MC 串口(1C/3C/4C) | 串口(C24 传送设定) |
| `MelsecMxClient` | MX Component | 逻辑站号(通信设置实用程序) |
| `OmronFinsTcpClient` / `UdpClient` | 欧姆龙 FINS | 9600 |
| `OmronCipClient` / `AllenBradleyEthIpClient` | EtherNet/IP | 44818 |
| `KeyenceHostLinkTcpClient` / `UdpClient` | KV Host Link | 8000 |
| `KeyenceMcTcpClient` / `UdpClient` | KV MC 兼容(SLMP 3E) | 5000 |
| `KeyenceSrClient` | SR 扫码枪 | 9004 |
| `InovanceTcpClient` | 汇川 Modbus 映射 | 502 |
| `InovanceRtuClient` | 汇川 Modbus RTU | 串口(默认 8N2/9600) |
| `InovanceMcTcpClient` | 汇川 MC 兼容(3E) | 无出厂默认,须与 AutoShop「MC配置」一致 |
| `PanasonicMcTcpClient` | 松下 MC 兼容(3E) | 2000 |
| `PanasonicMewtocolTcpClient` / `UdpClient` | MEWTOCOL | 1024 |
| `ToyopucTcpClient` / `UdpClient` | TOYOPUC | 1025 |
| `OpcUaClient` | OPC-UA | 4840 |
| `MTConnectClient` | MTConnect Agent | 5000 |
| 海康读码器四客户端 | Modbus / TCP 命令 / SDK / 串口 | 读码器侧配置(无出厂统一口) |

> **`S` 跨协议语义提示**:MEWTOCOL 的 `S10` 是**定时器设定值区**(SV,
> 字),而三菱系 MC(含 MC 兼容子类)的 `S10` 是**步进继电器**(位)——
> 同名不同物,跨协议移植地址时勿直接照抄。

## 采集 → MQTT 上行(可选,需 paho-mqtt)

「读 PLC → 发消息队列」是平台化数采的常见形态(工业网关的核心场景)。
omniplc 只负责采集,MQTT 客户端用任意库(下例 [paho-mqtt](https://pypi.org/project/paho-mqtt/)):
`pip install paho-mqtt`。本库核心保持零第三方依赖,MQTT 永远不进依赖树。

```python
import json
import time

import paho.mqtt.client as mqtt

from omniplc import ModbusTcpClient, Tag, TagTable

POINT_TABLE = TagTable([
    Tag(tag_id="furnace_temp", address="hr100", data_type="float", remark="炉温"),
    Tag(tag_id="pump_running", address="c0",    data_type="bool",   remark="泵运行"),
])

mq = mqtt.Client(client_id="omniplc-collector")
mq.connect("broker.local", 1883)            # 生产环境请加 TLS/凭据(由 broker 侧决定)
mq.loop_start()

with ModbusTcpClient("192.168.0.10", 502, 1) as client:
    client.bind_tags(POINT_TABLE)
    while True:
        payload = {}
        for tag_id in POINT_TABLE:                   # TagTable 是只读 Mapping
            ok, value = client.read_tag(tag_id)
            payload[tag_id] = value if ok else None  # 失败不中断整轮上报
        mq.publish("shop1/line1/plc1", json.dumps(payload), qos=0)
        time.sleep(1.0)                              # 采集周期
```

要点:失败点上报 `None` 而不是中断整轮(平台侧看得到"断点"才能告警);
高频采集请改用 `create_monitor`(内置轮询 + 快照 + 变更事件)驱动上报,
避免手工循环里轮询间隔漂移。上报通道的鉴权/TLS 属平台侧配置,与本库无关。
