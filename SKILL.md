---
name: omniplc
description: omniplc 多品牌 PLC/CNC/读码器统一通信库的使用指南。当用户的 Python 项目需要连接 PLC(Modbus、三菱 MC/MX、欧姆龙 FINS/CIP、西门子 S7、AB EtherNet/IP、基恩士 KV、汇川、松下、丰田 TOYOPUC)、CNC(FANUC FOCAS、三菱 EZSocket、MTConnect)、智能读码器(海康、基恩士 SR),编写数采/监控代码,使用批量读、周期监视、异步采集,或排查通信错误(超时/坏帧/错误码)时,按本指南编写与调试代码。
---

# omniplc 库使用指南

omniplc 是多品牌多协议工业设备统一通信库(Python ≥3.7.9,核心零第三方依赖):
18 族协议、32 个同步客户端,统一 API 读写 PLC / CNC / 智能读码器。
你在帮用户写**使用 omniplc 的代码**——写之前先确认设备品牌与走线,对照下表
选客户端;某协议的完整 API 与地址语法查 `docs/examples.md` 对应节,不要凭
其他库的经验猜地址写法(各协议差异极大)。

## 安装

```bash
pip install omniplc                 # 核心(绝大多数客户端零依赖)
pip install "omniplc[serial]"       # + 串口走线(RTU/MC 串口/MEWTOCOL 串口)
pip install "omniplc[opcua]"        # + OPC-UA
pip install "omniplc[mx]"           # + 三菱 MX Component(Windows COM)
pip install "omniplc[all]"          # 全部可选依赖
```

CNC 特例:FANUC FOCAS 需现场 `fwlib32.dll` 运行库(仅 Windows,构造时用
`sdk_dir` 或 `dll_path` 参数指路);三菱 EZSocket 与 MTConnect 零依赖直连。

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

## 通用 API 契约(所有客户端一致,写代码前必须知道)

- **不抛自定义异常**:读失败返回 `(False, None)`、写失败返回 `False`;
  参数错误(坏地址/非法类型)同步抛 `ValueError`——那是调用方编码错误,
  **不要捕获它**,让它暴露;
- **检查返回值,不要忽略 `False`**:失败原因在错误三件套——
  `client.last_error`(人读原因)/ `last_error_category`(TRANSPORT/TIMEOUT/
  DEVICE/PROTOCOL/UNKNOWN,语言无关,供告警分级)/ `last_error_code`
  (PLC 原始码,对照对应协议手册);
- **上下文管理**:`with client:` 等价 connect + disconnect;
- **自动可靠性已内置**:惰性重连 + 指数退避门控、心跳保活(默认 30 秒,
  `heartbeat_interval` 可调)、读重试按 `retries`、**写默认不重试**
  (防非幂等双写)——应用层不要重复实现这些;
- **写操作是危险动作**:写 PLC 可能影响生产,写前确认地址与值,
  库不提供"远程控制"类 API(运行/停止/强制)。

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

完整表(含 UDP 走线)见仓库 `docs/examples.md`「各走线默认端口对照」。

## 高频能力

- **批量读**(减少往返,采集系统必用):
  - `read_many(地址列表, 类型)`——同类型合并;
  - `read_batch([(地址, 类型), ...])`——混类型自动分组;
  - `read_range(起始地址, 数量, 类型)`——连续区段单事务;
  - `read_many_strict(地址列表, 类型)`——一致性快照(全部成功才交付);
  - 七协议有原生单事务覆写,其余自动分组,失败语义整批一致;
- **周期监视**:`client.create_monitor(点位表, interval, on_change)` 后台线程
  周期采集 + 本地快照(`monitor.get(tag_id)`,**消费方必须检查 `quality`,
  STALE 是失败期保留的旧值**)、变更回调、点位级死区 `deadband` 抑制浮点抖动;
- **点位表缩放**:`TagTable`/`Tag`(scale/offset)工程量换算;
- **写安全**(接产线先开,现场铁律):
  - `client.read_only = True`——一切写入口显式拒绝(只读观察期);
  - `client.write_whitelist = True`(配合 `bind_tags`)——表外地址拒绝;
  - `ok, readback = client.write_and_verify(地址, 类型, 值)`——写后回读校验
    (32 位浮点按位型比对);
- **等信号**:`ok, value = client.wait_value(地址, 类型, 谓词, timeout=…)`
  ——轮询等待条件成立(单次读失败不中断);
- **异步两层**:aio 包装层 `omniplc.aio`(类名前加 `A`,全协议镜像,拿不准选它);
  原生层 `omniplc.native`(`Async` 前缀,五协议:Modbus TCP/MC/FINS/汇川/S7,
  真中断与无锁属性读取)。选型详见仓库 `docs/async.md`。

## 排障入口(用户报告通信问题时按序查)

1. `last_error` / `last_error_category` / `last_error_code` 三件套定位;
2. 仓库 `docs/troubleshooting.md`「按现象排查」九节(连不上/时好时坏/
   超时/坏帧/读到旧值…)+「高频错误码现场话术」表;
3. 报文级证据:`omniplc.set_debug(True)` 实时打印,或 `set_frame_recorder(True)`
   黑匣子常驻(生产推荐),`export_recorded_frames("x.txt")` 一键导出,
   `client.incident_frames` 取拆连/设备错误时点自动快照;
4. 已知固件/设备差异:`docs/firmware-notes.md`。

## 深入文档(均在仓库 docs/ 下)

| 要什么 | 看 |
|---|---|
| 某协议全部 API 与地址语法 | `examples.md` 对应协议节 |
| 能力矩阵(谁支持什么) | `protocol-features.md` |
| 异步选型与边界 | `async.md` |
| 架构/机制设计 | `architecture.md` |
| 现场排障 | `troubleshooting.md` |
| 真机核证状态 | `real-machine-checklist.md` |

## 参与本库开发

协作纪律见根目录 `AGENTS.md`(硬纪律)与 `CONTRIBUTING.md`(全文):
协议实现必须有官方文档页码级出处;阶段提交(实现/测试/文档分开);
门禁五件套(pytest 3.7.9 全量 / ruff format / ruff / mypy / ty)提交前全绿;
全中文(注释/文档/提交消息)。
