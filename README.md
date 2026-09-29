# Omniplc
![](docs/assets/omniplc_banner.png)

面向多品牌、多协议 PLC 的 Python 统一通信库:三菱 / 欧姆龙 / 基恩士 / 汇川 / 松下 / 丰田 / 罗克韦尔(AB)/ 倍福(TwinCAT)/ 西门子(S7)/ SR 扫码枪 / OPC-UA / CNC(MTConnect),共 16 族协议、29 个同步客户端。**一次编写,同一套 API。**

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
uv add 'omniplc[ads]'     # 倍福 TwinCAT ADS(pyads + TcAdsDll 运行库)
uv add 'omniplc[s7]'      # 西门子 S7(python-snap7,按解释器自动二选一)
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
    ok, values = client.read_many(["hr0", "hr2"], DataType.FLOAT)  # 逐点容错批量
```

- **读返回 `(是否成功, 值)`,写返回 `bool`,不抛自定义异常**;失败原因在
  `client.last_error`。参数非法抛 `ValueError`。
- 程序化分类用 `client.last_error_category`(`TRANSPORT`/`PROTOCOL`/`DEVICE`/
  `TIMEOUT`/`UNKNOWN`)与 `client.last_error_code`(PLC 原始错误码),语言无关。
- `set_lang("en")` 可把报错文案切英文(默认中文);`set_debug(True)` 输出全部
  协议的收发报文十六进制转储。两者均为进程级开关。
- 连接失败自动进入指数退避门控(默认开,`reconnect_backoff = False` 关闭);
  `client.stats` 提供连接健康统计(`ClientStats` TypedDict)。

各协议全部 29 个客户端的用法示例(构造参数 / 地址语法 / 协议特有能力)见
**[docs/examples.md](docs/examples.md)**。

#### 客户端一览

| 协议 | TCP | UDP | 串口 | 其他 |
|---|---|---|---|---|
| Modbus(FC01~24/43 全功能码) | ✅ 502 | — | ✅ RTU(广播写) | — |
| 三菱 MC 3E/4E/1E | ✅ 2000 | ✅ 2000 | ✅ 1C/3C/4C(C24) | ✅ MX Component(Windows) |
| 欧姆龙 FINS | ✅ 9600 | ✅ 9600 | — | — |
| 欧姆龙 NJ/NX CIP | ✅ 44818 | — | — | — |
| 罗克韦尔 AB EtherNet/IP(Logix) | ✅ 44818 | — | — | — |
| 倍福 TwinCAT ADS | ✅ 851 | — | — | — |
| 基恩士 KV Host Link | ✅ 8000 | ✅ 8000 | — | — |
| 基恩士 KV MC 兼容(SLMP 3E) | ✅ 5000 | ✅ 5000 | — | — |
| 汇川 H3U/H5U(Modbus 映射) | ✅ 502 | — | ✅ 9600-8N2 | — |
| 汇川 MC 兼容(Easy/H5U) | ✅ 2000 | — | — | — |
| 松下 FP0H/FP7 MC 兼容 | ✅ 2000 | — | — | — |
| 松下 MEWTOCOL | ✅ 1024 | ✅ 1024 | — | — |
| 基恩士 SR 扫码枪 | ✅ 9004 | — | — | — |
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
