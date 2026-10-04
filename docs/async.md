# 异步使用指南(两套:包装层 `omniplc.aio` / 原生层 `omniplc.native`)

异步客户端是**多设备并发**的手段,不是单连接提速(单设备逐笔轮询用同步即可):
协议事务在单连接内本就是"一问一答"串行,收益来自把多台设备的等待重叠——
10 台设备并发采集约等于顺序轮询的 1/10 耗时。

## 选哪套

| | `omniplc.aio`(**包装层**,类名前加 `A`) | `omniplc.native`(**原生层**,类名前加 `Async`) |
|---|---|---|
| 实现 | 同步 I/O + 单线程 `ThreadPoolExecutor` 包装 | 原生 `asyncio` 协议栈(零第三方依赖) |
| 覆盖面 | **全部**协议(自动镜像的过渡层) | Modbus TCP / 三菱 MC(1E·3E·4E,TCP/UDP)/ 汇川(H3U/H5U Modbus TCP + MC 兼容 3E)/ 欧姆龙 FINS(TCP/UDP)/ 西门子 S7(自研栈,会话型) |
| 属性读取 | 抢事务锁,最长阻塞一个 `receive_timeout` | 直接读字段、不阻塞事件循环 |
| 取消 | `wait_for` 超时只放弃等待,已提交事务照跑完 | **真中断**:按"是否已发出请求"决定是否拆连 |
| 适用 | 原生层尚未覆盖的协议(AB / OPC-UA / 基恩士 / 松下 / TOYOPUC / MTConnect 等) | 五协议的新代码,尤其是需要原生取消与严格超时的场景 |

两套可混用(互不影响);原生层能力面与同步层同面(批量/扩展功能码详见
[architecture.md](architecture.md) §12)。

## 包装层示例

```python
import asyncio
from omniplc.aio import AMelsecMcTcpClient, AOmronFinsTcpClient, AAllenBradleyEthIpClient

async def main():
    clients = [
        AMelsecMcTcpClient("192.168.0.11", 2000),
        AOmronFinsTcpClient("192.168.0.12", 9600),
        AAllenBradleyEthIpClient("192.168.0.13", 44818),
    ]
    for client in clients:
        await client.connect()
    # 三台设备的同时刻采集:总耗时 ≈ 最慢一台的往返,而非三者之和
    d1, d2, d3 = await asyncio.gather(
        clients[0].read_float("D100"),
        clients[1].read_float("D100"),
        clients[2].read_float("MyReal"),
    )
    for client in clients:
        await client.disconnect()

asyncio.run(main())
```

**包装层边界**(它不是原生 asyncio 协议栈,每个 `await` 把同步调用投递到
该客户端自己的单工作线程):

- **同一客户端仍是串行的**:调用按 FIFO 排队(与同步侧同一把事务锁);并发
  收益只来自跨设备重叠等待,单设备并发吞吐请多开实例(连接池留 v1.x)。
- **事件循环不被 I/O 阻塞,但同步属性是直读**:`connected` / `last_error*` /
  `stats` 等读写不发报文、不切线程,读到的可能不是原子快照。
- **没有原生取消**:`wait_for` 超时只放弃等待,已提交的**写事务仍会跑完**;
  `close()` 则会**排空**已提交任务再释放线程(关闭后不会再有帧落线)。

## 原生层示例

```python
import asyncio
from omniplc import S7Cpu
from omniplc.native import AsyncModbusTcpClient, AsyncSiemensS7Client

async def main():
    # 支持 async with:失败抛 ConnectionError,退出自动断开
    async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = await client.read_float("hr100")
        print(client.stats["transactions"])     # 属性同步读取,不阻塞事件循环

    # 西门子 S7(原生层唯一会话型;型号进构造参数,同同步侧):
    async with AsyncSiemensS7Client("192.168.0.1", model=S7Cpu.S7_1200) as s7:
        ok, cpu = await s7.get_cpu_state()      # SZL 0x0424(Run/Stop/...)
        ok, temp = await s7.read_float("DB1.DBD6")

asyncio.run(main())
```

- **同一客户端串行、多客户端真并发**:同一实例排在一把 `asyncio.Lock` 后。
- **取消是真中断**:请求已发出 → 保守拆连重同步(下次事务惰性重连);
  仅排队未发出 → 保持连接。
- **超时口径与同步层一致**:TCP 读超时拆连;UDP 接收超时不拆连、发送超时拆连。
- **一个实例绑定一个事件循环**(锁按首次使用时的循环惰性创建);跨循环/跨线程
  共享同一实例不支持。串口走线与其余协议仍走包装层或同步客户端。
