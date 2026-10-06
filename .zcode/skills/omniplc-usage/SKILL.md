---
name: omniplc-usage
description: omniplc 多品牌 PLC/CNC/读码器统一通信库的使用向导。当用户要用 Python 连接 PLC(Modbus、三菱 MC/MX、欧姆龙 FINS/CIP、西门子 S7、AB EtherNet/IP、基恩士 KV、汇川、松下、丰田 TOYOPUC)、CNC(FANUC FOCAS、三菱 EZSocket、MTConnect)、读码器(海康、基恩士 SR),写数采/监控代码,做批量读、周期监视、异步采集,或排查通信错误(超时/坏帧/错误码)时使用本技能。
---

# omniplc 使用向导

omniplc 是多品牌多协议工业设备统一通信库(Python ≥3.7.9,核心零第三方依赖):
18 族协议、32 个同步客户端,统一 API 读写 PLC / CNC / 智能读码器。
**写代码前先读 `docs/examples.md` 对应协议节**——本技能是导航,细节在文档。

## 安装

```bash
pip install omniplc                 # 核心(绝大多数客户端零依赖)
pip install "omniplc[serial]"       # + 串口走线(RTU/MC 串口/MEWTOCOL 串口)
pip install "omniplc[opcua]"        # + OPC-UA
pip install "omniplc[mx]"           # + 三菱 MX Component(Windows COM)
pip install "omniplc[all]"          # 全部可选依赖
```

CNC 特例:FANUC FOCAS 需现场 `fwlib32.dll`(Windows,`sdk_dir`/`dll_path` 参数指路);
三菱 EZSocket 与 MTConnect 零依赖直连。

## 30 秒上手(最典型场景:TCP 采集)

```python
from omniplc import ModbusTcpClient

client = ModbusTcpClient("192.168.0.10", 502, 1)   # (IP, 端口, 从站号)
client.connect()
try:
    ok, value = client.read_ushort("hr100")          # 读:返回 (是否成功, 值)
    if ok:
        print(value)
    assert client.write_ushort("hr100", 42)          # 写:返回 bool
finally:
    client.disconnect()
```

## 通用 API 契约(所有客户端一致)

- **不抛自定义异常**:读失败 `(False, None)`、写失败 `False`;参数错误
  (坏地址/非法类型)同步抛 `ValueError`——那是调用方编码错误;
- **错误三件套**:`last_error`(人读原因,`set_lang("en")` 可切英文)/
  `last_error_category`(TRANSPORT/TIMEOUT/DEVICE/PROTOCOL/UNKNOWN,语言无关,
  供程序化告警分级)/`last_error_code`(PLC 原始码,查对应协议手册);
- **上下文管理**:`with client:` 等价 connect+disconnect;
- **自动可靠性**:惰性重连 + 指数退避门控、心跳保活(默认 30s)、
  写默认不重试(防双写);失败分类与自动行为速查见
  `docs/troubleshooting.md` §一。

## 客户端速查(品牌 → 类 → 默认端口)

| 设备 | 客户端 | 默认端口 |
|---|---|---|
| Modbus TCP / RTU | `ModbusTcpClient` / `ModbusRtuClient` | 502 / 串口 |
| 三菱 MC 3E/4E | `MelsecMcTcpClient` | 2000 |
| 三菱 MC 串口 | `MelsecMcSerialClient` | 串口(C24) |
| 三菱 MX Component | `MelsecMxClient` | 逻辑站号 |
| 欧姆龙 FINS | `OmronFinsTcpClient` | 9600 |
| 欧姆龙 NJ/NX、罗克韦尔 AB | `OmronCipClient` / `AllenBradleyEthIpClient` | 44818 |
| 基恩士 KV Host Link / MC 兼容 | `KeyenceHostLinkTcpClient` / `KeyenceMcTcpClient` | 8000 / 5000 |
| 基恩士 SR 扫码枪 | `KeyenceSrClient` | 9004 |
| 汇川(记号直写) | `InovanceTcpClient` / `InovanceMcTcpClient` | 502 / 2000 |
| 松下 MC 兼容 / MEWTOCOL | `PanasonicMcTcpClient` / `PanasonicMewtocolTcpClient` | 2000 / 1024 |
| 丰田 TOYOPUC | `ToyopucTcpClient` | 1025 |
| 西门子 S7 全系 | `SiemensS7Client(model=S7Model.S7_1200)` | 102 |
| OPC-UA | `OpcUaClient` | 4840 |
| MTConnect Agent | `MTConnectClient` | 5000 |
| FANUC CNC | `FanucFocasClient` | 8193(Windows) |
| 三菱 CNC | `MitsubishiEzSocketClient` | 683 |
| 海康读码器 | `HikrobotIdModbusClient` / `HikrobotIdTcpClient` / `HikrobotIdSdkClient` / `HikrobotIdSerialClient` | 读码器侧配置 |

完整表(含 UDP 走线)见 `docs/examples.md`「各走线默认端口对照」。

## 高频能力

- **批量读**(少往返):`read_many(地址列表, 类型)` 同类型合并 /
  `read_batch([(地址, 类型), ...])` 混类型自动分组 / `read_range(起始, 数量, 类型)`
  连续区段单事务——七协议原生单事务,其余自动分组;
- **周期监视**:`client.create_monitor(点位表, interval, on_change)` 后台线程
  周期采集 + 本地快照(`monitor.get(tag_id)`,**检查 `quality` 别拿 STALE 当新值**)
  + 变更回调 + 点位级死区 `deadband` 抑制浮点抖动;
- **点位表缩放**:`TagTable`/`Tag`(scale/offset)工程量换算;
- **字符串读写**:`read_string/write_string`(各协议编码/布局差异见 examples);
- **异步两层**:aio 包装层 `omniplc.aio`(类名前加 `A`,全协议镜像,选它不会错);
  原生层 `omniplc.native`(`Async` 前缀,五协议:Modbus TCP/MC/FINS/汇川/S7,
  真中断与无锁属性)。选型详见 `docs/async.md`。

## 排障入口

1. `client.last_error` / `last_error_category` / `last_error_code` 三件套;
2. `docs/troubleshooting.md`「按现象排查」九节(连不上/时好时坏/超时/坏帧/旧值…);
3. 报文级:`omniplc.set_debug(True)` 实时打印,或 `set_frame_recorder(True)`
   黑匣子常驻(生产推荐),`recorded_frames()` 取故障现场;
4. 已知固件差异:`docs/firmware-notes.md`。

## 深入文档

| 要什么 | 看 |
|---|---|
| 某协议全部 API 与地址语法 | `docs/examples.md` 对应协议节 |
| 能力矩阵(谁支持什么) | `docs/protocol-features.md` |
| 异步选型与边界 | `docs/async.md` |
| 架构/机制设计 | `docs/architecture.md` |
| 现场排障 | `docs/troubleshooting.md` |
| 真机核证状态 | `docs/real-machine-checklist.md` |

## 参与本项目开发

协作纪律与门禁见根目录 `AGENTS.md`(硬纪律摘要)与 `CONTRIBUTING.md`(全文):
协议实现必须有官方文档页码级出处;阶段提交(实现/测试/文档分开),
`git add` 只加显式文件;门禁五件套(pytest 3.7.9 全量 / ruff format / ruff /
mypy / ty)提交前全绿;全中文(注释/文档/提交消息)。
