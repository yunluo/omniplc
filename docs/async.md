# 异步使用指南(两套:包装层 `omniplc.aio` / 原生层 `omniplc.native`)

异步客户端是**多设备并发**的手段,不是单连接提速(单设备逐笔轮询用同步即可):
协议事务在单连接内本就是"一问一答"串行,收益来自把多台设备的等待重叠——
10 台设备并发采集约等于顺序轮询的 1/10 耗时。

## 选哪套

|          | `omniplc.aio`(**包装层**,类名前加 `A`)                                     | `omniplc.native`(**原生层**,类名前加 `Async`)                                                                                 |
| -------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 实现     | 同步 I/O + 单线程 `ThreadPoolExecutor` 包装                                | 原生 `asyncio` 协议栈(零第三方依赖)                                                                                           |
| 覆盖面   | **全部**协议(自动镜像的过渡层)                                             | Modbus TCP / 三菱 MC(1E·3E·4E,TCP/UDP)/ 汇川(H3U/H5U Modbus TCP + MC 兼容 3E)/ 欧姆龙 FINS(TCP/UDP)/ 西门子 S7(自研栈,会话型) |
| 属性读取 | 抢事务锁,最长阻塞一个 `receive_timeout`                                    | 直接读字段、不阻塞事件循环                                                                                                    |
| 取消     | `wait_for` 超时只放弃等待,已提交事务照跑完                                 | **真中断**:按"是否已发出请求"决定是否拆连                                                                                     |
| 适用     | 原生层尚未覆盖的协议(AB / OPC-UA / 基恩士 / 松下 / TOYOPUC / MTConnect 等) | 五协议的新代码,尤其是需要原生取消与严格超时的场景                                                                             |

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
from omniplc import S7Model
from omniplc.native import AsyncModbusTcpClient, AsyncSiemensS7Client

async def main():
    # 支持 async with:失败抛 ConnectionError,退出自动断开
    async with AsyncModbusTcpClient("192.168.0.10", 502, 1) as client:
        ok, value = await client.read_float("hr100")
        print(client.stats["transactions"])     # 属性同步读取,不阻塞事件循环

    # 西门子 S7(原生层唯一会话型;型号进构造参数,同同步侧):
    async with AsyncSiemensS7Client("192.168.0.1", model=S7Model.S7_1200) as s7:
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

## 线程安全边界与推荐用法(review-1043 丁-7)

同步客户端**实例方法可跨线程调用**(同一实例的并发调用被事务锁串行化,
保证正确性而非并行吞吐)。但"能用"不等于"随便用",以下边界是现场
踩过/推演过的(细则散见各类 docstring,这里给齐):

| 边界 | 语义 | 推荐用法 |
| --- | --- | --- |
| **同一实例多线程** | 正确但串行;`last_error*`/`stats` 是**实例级共享态**,多线程互踩是设计行为(看最后一次) | 一个实例一个"车主线程"(采集线程或控制线程);需要真并发就多开实例 |
| **`disconnect()` 是终态** | 2026-10 起(review-1043 丙-2):disconnect 后**在途/后续事务不再惰性重连**,直接 `RuntimeError`;只有显式 `connect()` 才解除 | "停止采集"由车主线程调 `disconnect()`;控制线程要恢复须显式 `connect()` |
| **MX(COM)不许跨线程** | `MelsecMxClient` 的 `connect()` 在调用线程 `CoInitialize`+建 COM 对象(STA 套间);换线程发 `ReadDeviceBlock` 可能 `RPC_E_WRONG_THREAD` 或错值 | MX 实例从建到用(finally `close()`)都在**同一线程**内 |
| **开关切换无事务锁** | `read_only`/`write_whitelist`/`bind_tags`(以及 aio 的超时 setter)的写入**不抢事务锁**:在途事务按旧开关走完 | 切换开关选在无在途事务的时机(如车主线程的两轮采集之间) |
| **进程级全局开关** | `set_debug`/`set_frame_recorder`/`set_lang` 与 `OMNIPLC_READ_ONLY` 影响**本进程所有实例**(不只是你手上的这个) | 启动期一次性设置;运行期别当"按客户端的开关"用 |

**回调线程两处特殊点**:

- **Monitor 回调**(`on_change`/`on_disconnect`)跑在监视器自己的线程上;
  回调异常独立计数(`monitor.stats["callback_errors"]`),不误判断连。回调内
  **不要**调 `create_monitor`/`disconnect`(与 `disconnect()` 的联动停摆互等);
- **OPC-UA 订阅回调**跑在 asyncua 的事件循环线程上:不得在回调内再调本
  客户端的读写/订阅/退订(业务线程持锁等 loop 应答时双向等待,最长到
  asyncua 内部 120s 超时,该设备期间完全停摆;review-1043 丁-3)——需要
  联动操作请把请求投给业务线程/队列。
