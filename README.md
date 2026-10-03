# Omniplc
![](docs/assets/omniplc_banner.png)

面向多品牌、多协议 PLC 的 Python 统一通信库:三菱 / 欧姆龙 / 基恩士 / 汇川 /
松下 / 丰田 / 罗克韦尔(AB)/ 倍福(TwinCAT)/ 西门子(S7)/ SR 扫码枪 /
海康读码器 / OPC-UA / CNC(MTConnect),共 17 族协议、31 个同步客户端。
**一次编写,同一套 API。**

- **Python 3.7.9+**,核心零第三方依赖;全量类型标注(PEP 484 + py.typed)
- 命名与使用习惯对齐各厂商 SDK,迁移成本极低
- 线程安全、惰性自动重连、指数退避门控、可配置超时/重试
- 全局报文调试(`set_debug`)+ 报错语言中英切换(`set_lang`)
- 同步 + 异步双轨(异步类 = 同步类名前加 `A`;另有原生 asyncio 层 `Async*`)

#### 安装

```bash
uv add omniplc            # 或 pip install omniplc
uv add 'omniplc[serial]'  # Modbus RTU / 三菱 MC 串口帧(pyserial)
uv add 'omniplc[mx]'      # 三菱 MX Component(Windows)
uv add 'omniplc[opcua]'   # OPC-UA(asyncua)
uv add 'omniplc[s7]'      # 西门子 S7(python-snap7,按解释器自动二选一)
uv add 'omniplc[all]'     # 全部可选扩展(serial/mx/opcua/s7,不含测试工具)
```

#### 快速上手

```python
from omniplc import ModbusTcpClient, DataType

with ModbusTcpClient(ip_address="192.168.0.10", port=502, station=1) as client:
    client.receive_timeout = 3.0                    # 超时走属性,不进构造函数
    ok, value = client.read_float("hr100")          # 读返回 (是否成功, 值)
    ok = client.write_float("hr100", 3.14)          # 写返回 bool
    if not ok:
        print(client.last_error)                    # 失败原因在这里
    results = client.read_many(["hr0", "hr2"], DataType.FLOAT)  # 批量读(整批容错)
    ok, values = client.read_range("hr0", 100, DataType.USHORT)    # 连续 100 点单事务
```

- **读返回 `(是否成功, 值)`,写返回 `bool`,不抛自定义异常**;失败原因在
  `client.last_error`。参数非法抛 `ValueError`。
- 批量入口两种:`read_many(地址列表, 类型)` 返回与地址等长的
  `[(是否成功, 值)]` 列表;`read_range(起始地址, 数量, 类型)` 返回
  `(是否成功, 值列表)`(整批容错)。
- 程序化分类用 `client.last_error_category`(`TRANSPORT`/`PROTOCOL`/`DEVICE`/
  `TIMEOUT`/`UNKNOWN`)与 `client.last_error_code`(PLC 原始错误码),语言无关。
- `set_lang("en")` 可把报错文案切英文(默认中文);`set_debug(True)` 输出全部
  协议的收发报文十六进制转储。两者均为进程级开关。
- 连接失败自动进入指数退避门控(默认开,`reconnect_backoff = False` 关闭);
  `client.stats` 提供连接健康统计(`ClientStats` TypedDict)。

各协议全部 31 个客户端的用法示例(构造参数 / 地址语法 / 协议特有能力)见
**[docs/examples.md](docs/examples.md)**。

#### 客户端一览

| 协议 | TCP | UDP | 串口 | 其他 |
|---|---|---|---|---|
| Modbus(FC01~24/43 全功能码) | ✅ 502 | — | ✅ RTU(广播写) | — |
| 三菱 MC 3E/4E/1E | ✅ 2000 | ✅ 2000 | ✅ 1C/3C/4C(C24) | ✅ MX Component(Windows) |
| 欧姆龙 FINS | ✅ 9600 | ✅ 9600 | — | — |
| 欧姆龙 NJ/NX CIP | ✅ 44818 | — | — | — |
| 罗克韦尔 AB EtherNet/IP(Logix) | ✅ 44818 | — | — | — |
| 基恩士 KV Host Link | ✅ 8000 | ✅ 8000 | — | — |
| 基恩士 KV MC 兼容(SLMP 3E) | ✅ 5000 | ✅ 5000 | — | — |
| 汇川 H3U/H5U(Modbus 映射) | ✅ 502 | — | ✅ 9600-8N2 | — |
| 汇川 MC 兼容(Easy/H5U) | ✅ 2000 | — | — | — |
| 松下 FP0H/FP7 MC 兼容 | ✅ 2000 | — | — | — |
| 松下 MEWTOCOL | ✅ 1024 | ✅ 1024 | — | — |
| 基恩士 SR 扫码枪 | ✅ 9004 | — | — | — |
| 海康机器人 ID 智能读码器(Modbus 模式) | ✅ 502 | — | — | — |
| 海康机器人 ID 读码器(TCP 命令协议) | ✅ 可配 | — | — | — |
| 海康机器人 ID 智能读码器(MvCodeReaderSDK) | ✅ GigE/USB | — | — | ✅ ctypes 封装(需 SDK 运行库) |
| 海康机器人 ID 读码器(串口) | — | — | ✅ RS-232 | — |
| 丰田 TOYOPUC 计算机链接 | ✅ 1025 | ✅ 1025 | — | — |
| OPC-UA | ✅ 4840 | — | — | — |
| CNC MTConnect | ✅ 5000 | — | — | — |
| 西门子 S7(DB/I/Q/M) | ✅ 102 | — | — | — |

#### 异步(两套)

| | `omniplc.aio`(类名前加 `A`) | `omniplc.native`(类名前加 `Async`) |
|---|---|---|
| 实现 | 同步 I/O + 单线程池包装 | 原生 asyncio 协议栈(零依赖) |
| 覆盖面 | 全部协议 | Modbus TCP / 三菱 MC(1E·3E·4E)/ FINS |
| 取消 | 超时只放弃等待,已提交事务照跑完 | 真中断(按是否已发出决定拆连) |

```python
import asyncio
from omniplc.aio import AMelsecMcTcpClient, AOmronFinsTcpClient

async def main():
    c1, c2 = AMelsecMcTcpClient("192.168.0.11", 2000), AOmronFinsTcpClient("192.168.0.12", 9600)
    await asyncio.gather(c1.connect(), c2.connect())
    v1, v2 = await asyncio.gather(c1.read_float("D100"), c2.read_float("D100"))
    await asyncio.gather(c1.disconnect(), c2.disconnect())

asyncio.run(main())
```

异步是**多设备并发**的手段(收益 = 跨设备重叠等待),不是单连接提速。选型
细节与边界见 **[docs/async.md](docs/async.md)**。

#### 点位表(可选)

```python
from omniplc import TagTable

client.bind_tags(TagTable.from_json("tags.json"))
# tags.json: [{"tag_id": "furnace_temp", "address": "D100", "data_type": "float",
#              "scale": 0.1, "remark": "炉温"}, ...]
ok, value = client.read_tag("furnace_temp")   # 点位标识 → 地址+类型,自动应用缩放
```

#### 监视器(可选,内置轮询采集)

客户端下建监视器,**默认不启动**;`start()` 后由后台线程按固定周期批量采集,
把"最新值 + 质量 + 时间戳"落成本地快照,消费方读快照不发报文、不阻塞:

```python
monitor = client.create_monitor(
    {"炉温": ("hr0", "float"), "压力": ("hr2", "ushort")},  # 或直接传 TagTable(自动应用缩放)
    interval=1.0,
    on_change=lambda ev: print(ev),      # MonitorEvent(tag_id, old, new, quality, updated_at)
    on_disconnect=lambda: print("采集失败期开始"),
)
client.connect()
monitor.start()
snap = monitor.get("炉温")               # PointSnapshot(质量, 值, 时间戳),纯本地
monitor.stats                            # cycle_count / fail_count / consecutive_fails / ...
monitor.stop()                           # client.disconnect() 也会联动停掉全部监视器(终态)
```

质量三态 `INITIAL / GOOD / STALE`:失败**保留旧值降质**,不用 None 冲掉;变更
事件在值变化或质量跨越 GOOD↔非GOOD 边界时触发(掉线恢复也通知,双 NaN 视为
未变);周期与业务共享同一本客户端账,建议监视器独占客户端实例。全部语义口径
见 `omniplc.core.monitor` 模块 docstring。

#### 心跳保活(可选)

```python
ok = client.ping()                        # 手动探活:执行零副作用探测命令,失败不抛
client.heartbeat_interval = 30            # 自动心跳间隔(秒),默认 30;0 = 关闭
stats = client.stats                      # heartbeat_ok / heartbeat_fail / last_heartbeat_at
```

连接建立后由守护线程按间隔自动探测,失败走与读写相同的事务口径(`last_error`
记录、传输失败拆连后下一 tick 自动重连自愈,退避门控防重连风暴);显式
`disconnect()` 停止。探测命令逐协议落位:Modbus FC08 回显、MC 0101 CPU 型号、
FINS 0601 状态读、AB Identity 读取、S7 `get_cpu_state`、
OPC-UA `i=2258` Server 时间、MTConnect `/probe`、海康读码器状态查询等——
逐协议依据与差异见 **[docs/protocol-features.md](docs/protocol-features.md)**
「心跳保活」节。

#### 文档

| 文档 | 内容 |
|---|---|
| [docs/examples.md](docs/examples.md) | 各协议用法示例(29 客户端 / 扩展功能码 / 批量读取) |
| [docs/async.md](docs/async.md) | 异步两套的选型、示例与边界 |
| [docs/architecture.md](docs/architecture.md) | 架构设计、类继承图、版本履历 |
| [docs/real-machine-checklist.md](docs/real-machine-checklist.md) | 真机联测记录与待核证项 |
| [docs/protocol-features.md](docs/protocol-features.md) | 协议功能实现矩阵(已实现/未实现逐协议对照) |
| [docs/protocol/README.md](docs/protocol/README.md) | 协议官方手册索引 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发约定、门禁、双版本测试 |
| [CHANGELOG.md](CHANGELOG.md) | 完整变更历史 |

#### 开发

```bash
uv sync --extra dev                 # 安装开发依赖
uv run python -m pytest tests -q    # 测试(无需真机)
uvx ruff check src tests            # 代码检查
uvx mypy src/omniplc                # 类型检查(python_version = 3.10)
uvx ty check src/omniplc            # ty 类型检查
```

本地双版本(3.7.9 + 3.12)测试跑法见 [CONTRIBUTING.md](CONTRIBUTING.md)「测试」节。

#### License

[MIT](LICENSE)
