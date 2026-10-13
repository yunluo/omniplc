# review-1038 传输层专项审查报告(只差不做)

> 2026-10-11 · 基线:master `a4dc17c`(review-1033 补审计后;工作区另有维护者未提交批)
> 范围:`transport/`(base 145 + tcp 172 + udp 237 + serial 226 + `__init__` 17 = **797 行**);
> 测试 `test_transport.py`(545 行);旁证 `native/transport.py`(652 行)。
> **本轮只检查与差距分析,不落地任何修改**。
>
> **性质**:传输层是**横切层** —— 全部 34 个客户端都经它,任何缺陷影响面都是全库。
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019~1037 同款)。
> **版本说明**:本文件为**主会话亲验版**;§六 列出仍待展开的切片(会话/并发语义细则)。

## 〇、执行摘要

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| **`recv` 恰好 size 语义** | **真实 socketpair 实测通过**(分 3 次投递仍读满恰好 10 字节) | — |
| **绝对 deadline** | 实测涓流拖延受 `receive_timeout` 约束(0.4s 预算 → **0.41s** 抛) | — |
| **对端关闭** | 实测抛 `TransportClosedError`(文案明确) | — |
| **`finally` 恢复超时** | 受控三场景**全部恢复**;我先前一次 `None` 观察**已撤回**(§二) | — |
| **UDP 截断** | 跨平台同口径(POSIX `MSG_TRUNC` / Windows `WSAEMSGSIZE` → `DeviceError(-10040)`) | — |
| **`drain()` 调用契约** | 8 处调用点,**7 处有 `datagram` 守卫**(第 8 处是 asyncio 的 `writer.drain()`,同名不同物) | — |
| **串口参数校验** | `baud_rate` 的 `<= 0` 让 **NaN/inf 穿透**(实测;不泄漏 FD) | **P3**(§三) |
| `base.py` 契约 vs 三实现 | `send`/`close`/`connect`/`recv`/`recv_some`/`drain` **逐条符合** | — |

---

## 一、审查范围与实证清单

| # | 实证项 | 方法 | 结果 |
| --- | --- | --- | --- |
| 1 | `recv(size)` 读满恰好 size | **真实 `socketpair`**,分 3 次投递写 | 返回 `b"1234567890"`(长度 10)✔ |
| 2 | 对端关闭 | `socketpair` 对端 `close()` | `TransportClosedError: TCP 连接已被对端关闭` ✔ |
| 3 | 涓流绝对 deadline | 只投 1 字节后不给,`receive_timeout=0.4` | **0.41s** 抛 `socket.timeout` ✔ |
| 4 | `finally` 恢复 socket 超时 | 受控三场景(就绪一轮/延迟两轮/延迟一轮) | **全部恢复为 0.5** ✔ |
| 5 | TCP 配置失败收 FD | 读 `tcp.py` | `except BaseException` + `sock.close()`(review-1023 修法)✔ |
| 6 | UDP 配置失败收 FD | 读 `udp.py` | 同款 ✔ |
| 7 | 两个超时 setter | 读 `base.py` | 均含 `not math.isfinite(value) or value <= 0` ✔ |
| 8 | `drain()` 调用面 | 全库检索 8 处 | 7 处有 `transport.datagram` 守卫 ✔ |
| 9 | UDP `drain()` 实现 | 读 `udp.py` | 非阻塞 + `finally` 恢复 + 平台差异 + 上限截断告警 ✔ |
| 10 | UDP 截断 | 读 `udp.py` | `MSG_TRUNC`(POSIX)/`WSAEMSGSIZE`(Windows)双路同口径 ✔ |
| 11 | **串口 `baud_rate` NaN/inf** | 实测 `SerialConfig.validate()` | **穿透**(`nan <= 0` 恒 False)❌ |
| 12 | FINS identity 回显 | **实跑** | SID/ICF/命令码三项不符全部拒绝;20 组短帧无 `IndexError` 逃逸 ✔ |

---

## 二、虚警留痕:TCP `recv` 的 `finally` 恢复超时**未失效**(已撤回)

**首轮观察(错误)**:在一段非受控脚本里,我观察到"正常读完一次后
`socket.gettimeout()` 变成 `None`(阻塞)",并据此推断
"**紧接着的 `sendall` 没有超时** —— 与 `finally` 注释所声明的意图不一致"。

**复核(受控对照,三场景)**:

```
数据已就绪,recv(2) 一轮完成   -> 读完   gettimeout()=0.5
数据延迟到达,recv(2) 需两轮   -> 读完   gettimeout()=0.5
数据延迟到达,recv(4) 需一轮   -> 读完   gettimeout()=0.5
```

⇒ **`finally` 恢复是有效的**,三种路径都恢复为配置值;
先前那次 `None` 是**我脚本自身的干扰**(在同一进程里复用/改写了变量与 socket 对象,
并把"未连接/已关闭"状态误当成"读完后的状态")。

**结论:该疑点撤回,不是缺陷**。记入本节以免下轮重复投入 ——
`tcp.py` 的 `finally: sock.settimeout(self._receive_timeout)` 与注释所述意图**一致**。

> **方法学注**:这条虚警的成因与 review-1033 §六(`S7_MODEL_PRESETS` 被误当码表)、
> review-1033 §七(我两次把"构造失败/参数个数错"误当成"契约违反")**同型** ——
> **测量工具本身的错误会伪装成被测对象的缺陷**。故本报告对每条结论都标注了**受控性**
> (单变量、可复现的命令),而把未受控的观察一律降级为"待复核"。

---

## 三、P3:串口参数校验的 NaN/inf 穿透(亲验)

### 3.1 现象

`SerialConfig.validate()` 用 `if self.baud_rate <= 0` 判非法 —— **`nan <= 0` 恒为 False**,
故 NaN 与 inf **穿透校验**:

```
SerialConfig(port_name="COM3", baud_rate=float("nan")).validate()   -> 通过
SerialTransport(SerialConfig(port_name="COM_NOPE", baud_rate=nan))  -> 构造成功
  t.connect() -> ValueError: cannot convert float NaN to integer   ← CPython 文案,非库内 i18n
```

### 3.2 论证

- **可达性**:`baud_rate` 是**公共参数**(公开 dataclass 字段),
  且 `SerialTransport.__init__` 会调 `config.validate()` ⇒ 用户可从公共 API 直达;
- **函数内不一致**:同函数里 `data_bits`(`not 5 <= x <= 8`)与 `stop_bits`(`not in (1,1.5,2)`)
  **都能拒绝 NaN**,唯独 `baud_rate` 的 `<= 0` 不能;
- **同型已修先例**:review-1023 P1 即"两个超时 setter 的 `<= 0` 让 NaN/inf 穿透",
  该处已由 `ac7af9d` 用 `math.isfinite` 收口(`transport/base.py` 现在是标准写法);
- **不泄漏 FD**(与 review-1023 P1 的关键差别):异常发生在 `port.baudrate = nan`
  (pyserial 内部转换阶段,在 `port.open()` **之前**),实测 `_serial` 保持 `None`。

### 3.3 定级与修法

- **定级 P3**:①违反 `validate()` 的「`:raises ValueError:` 参数非法」契约(抛 CPython 文案,
  现场不可读);②同形态若将来出现在"先 open 后配置"的顺序里就会变成 FD 泄漏;
- **修法(一行,与 base.py 同口径)**:
  `if not math.isfinite(self.baud_rate) or self.baud_rate <= 0:`
  —— 并可顺带要求 `int` 类型(`1.5` 亦应拒)。

---

## 四、经核无问题(亲验)

| 项 | 核法 | 结论 |
| --- | --- | --- |
| `recv(size)` 读满恰好 size | **真实 socketpair**,分 3 次投递 | ✔ 与类文档"TCP 实现:阻塞读取**恰好** `size` 字节"一致 |
| 绝对 deadline | 实测 0.4s 预算在 0.41s 收口 | ✔ 对端涓流不能无限拖住 |
| 对端关闭 | 实测 | ✔ `TransportClosedError`,文案明确 |
| `finally` 恢复超时 | 受控三场景 | ✔(§二 虚警已撤回) |
| `sendall` 发送 | 读 `tcp.py` | ✔ 未连接时先 `_require_socket()` |
| `recv_some` | 读 `tcp.py` | ✔ 单次 `socket.recv`,阻塞上限与属性同步 |
| TCP/UDP 配置失败收 FD | 读两文件 | ✔ 均 `except BaseException` + `close()` |
| 两个超时 setter | 读 `base.py` | ✔ `math.isfinite` 守卫齐备 |
| UDP 截断跨平台同口径 | 读 `udp.py` | ✔ `MSG_TRUNC` / `WSAEMSGSIZE` 双路 + 平台注记 |
| UDP `drain()` | 读 `udp.py` | ✔ 非阻塞 + `finally` 恢复 + 上限截断告警 + 平台差异 |
| `drain()` 调用契约 | 全库 8 处 | ✔ 7 处 `datagram` 守卫;第 8 处是 `writer.drain()`(同名不同物) |
| **`drain()` 不可替代性** | **实测**(见 review-1033 §二) | ✔ MC 3E 不校验命令码 ⇒ 无 `drain` 则静默接受陈旧值 |
| 基类 `recv_some` 默认退化 | 读 `base.py` | ✔ 文档已说明"默认退化为 `recv`";TCP 覆写为真实流式读 |

---

## 五、与 native 层的一致性(仅口径层)

| 维度 | 同步三实现 | `native/transport.py` | 判定 |
| --- | --- | --- | --- |
| `recv` 语义 | 读满恰好 size | 同名同义(await) | ✔ |
| 超时语义 | 绝对 deadline + `socket.timeout` | `_await_with_timeout` | ✔ 语义对齐 |
| `drain` 语义 | 仅数据报;UDP 覆写 | 同(见 native 各驱动调用点) | ✔ |
| 截断处理 | `DeviceError(-10040)` 跨平台 | 同口径 | ✔ |
| 取消 | 无(同步无取消概念) | `pending` 标志 + 保守拆连 | ⚠ **S7 侧未落地**(review-1034 §4.2 P2-2) |

---

## 六、待补切片(覆盖边界)

> 本报告覆盖**契约、资源、边界与实测语义**;以下**尚未展开**(原派发子代理仍在运行,
> 若其后回报有新增结论,将在并入时追加):
> 1. `serial.py` 的 `recv` 双语义("0 字节超时不断线 / **部分字节超时关串口重同步**")
>    是否在真实串口路径成立(本轮只读了实现与文档,未接真实串口);
> 2. `_PARITY_MAP`/`_DATA_BITS_MAP`/`_STOP_BITS_MAP` 与 pyserial 常量的逐项对应(未逐项对);
> 3. `_UDP_DRAIN_MAX_DATAGRAMS` 上限的语义与调优依据;
> 4. `test_transport.py`(545 行)的覆盖矩阵与断言强度(未逐用例读);
> 5. native 传输层的并发/取消细则。
> **覆盖边界声明**:§一~§五 的结论**独立成立**,不依赖上述切片。

---

> **登记与排期**:本报告不落地任何修改。§三 属**行为变更**(校验收紧),须补测试并
> 按 CONTRIBUTING §六披露;§二 的虚警留痕无需任何动作。
> **真机核验**:本报告结论为**真实 socket 实测 + 库内静态核**,**未接真实串口/PLC**。
