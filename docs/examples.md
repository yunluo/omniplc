# 各协议用法示例

按品牌分节的用法速查。构造参数、地址语法与能力细节以各客户端 docstring 与
[architecture.md](architecture.md) 为准;通用约定(读写返回形状、错误处理、
超时/重试)见 [README](README.md)「错误处理」节。

安装可选依赖:`pip install 'omniplc[serial|mx|opcua|s7]'`(对应小节标注)。

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

扩展:`random_read`/`random_write`(0403/1402,乱序不连续软元件单事务)、
`get_cpu_type`(0101,CPU 型号 + 模型代码)。

## 欧姆龙 FINS / NJ·NX CIP

```python
from omniplc import OmronFinsUdpClient

# TCP 自动做节点分配握手,UDP 无握手
fins = OmronFinsUdpClient(ip_address="192.168.250.1", port=9600)
ok, value = fins.read_ushort("D100")
ok = fins.write_bool("CIO0.5", True)
ok, values = fins.read_batch([("D100", "short"), ("CIO0.5", "bool")])  # 0104 多存储区读

# NJ/NX CIP(44818):Sysmac 变量自描述,地址即变量名(TestVar / MyArray[5] / Motor[2].Speed)
from omniplc import OmronCipClient
nj = OmronCipClient(ip_address="192.168.0.10", connected_messaging=True)  # 连接型可选
ok, value = nj.read_int("TestVar")
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
opc = OpcUaClient("192.168.0.10", 4840)
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

订阅句柄 `OpcUaSubscription.unsubscribe()` 幂等;`disconnect()` 先退订再断开;
**断线不自动重订**,订阅重建留调用端。默认 SelectClauses 聚合 BaseEventType
全部属性(含 SourceNode/Time);自定义字段可传 `event_filter`。

## CNC 机床数采 MTConnect(标准库实现,零第三方依赖)

```python
from omniplc import MTConnectClient
cnc = MTConnectClient("192.168.0.10", 5000)   # Agent 默认 5000;数据项 id 即地址
ok, speed = cnc.read_float("Sspeed")   # 主轴转速(文本值自动转 float)
ok, items = cnc.snapshot()             # 全量当前值快照;先看机器实际提供哪些数据项
ok, alarms = cnc.read_conditions()     # 条件项(Fault/Warning/Normal)
ok, device = cnc.probe()               # 设备信息(name/uuid 等)
```

## 西门子 S7(需 omniplc[s7];封装 python-snap7)

```python
from omniplc import SiemensS7Client
# S7-1200/1500 需勾选「允许来自远程对象的 PUT/GET 通信访问」,DB 须为非优化块
# 依赖按解释器自动二选一:3.7~3.9 → 1.3(C 封装);3.10+ → 3.x(纯 Python)
s7 = SiemensS7Client("192.168.0.1", rack=0, slot=1)  # 300/400 的 CPU 常在槽位 2
ok, temp = s7.read_float("DB1.DBD6")   # DB 双字起点,REAL
ok = s7.write_bool("DB1.DBX0.3", True) # DB 位(非原子读-改-写;多写者请 write 整字节)
ok, current = s7.read_ushort("MW10")   # Merker 字
ok, text = s7.read_string("DB1.DBS20", length=32)   # S7 String(头 2 字节声明/实际长)
ok, wtext = s7.read_wstring("DB1.DBW40", length=32) # S7 WString(UTF-16,中文/日文)
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
