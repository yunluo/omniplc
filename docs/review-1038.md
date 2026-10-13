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
| **`drain()` 调用契约** | **7 处**调用点:5 处有 `datagram` 守卫 + 2 处在 UDP-only 类内;与 7 个 UDP 实例化点一一对应 | — |
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
| 8 | `drain()` 调用面 | 全库检索 **7 处**(订正) | 5 处有守卫 + 2 处 UDP-only 类内直调;7 处调用 ↔ 7 个 UDP 实例化点,零缺口 ✔ |
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
| `drain()` 调用契约 | 全库 **7 处**(订正) | ✔ 5 处 `datagram` 守卫 + 2 处 UDP-only 类内;与 7 个 UDP 实例化点一一对应 |
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

## 六、并入:传输层深切片(子代理实测,2026-10-11)

> 下表由**派发的传输层子代理**完成(回环 socket + 进程内假 pyserial,27 例测试面盘点);
> 与 §一~§五 **无冲突**,仅**新增**结论与**一处对全库记录的订正**。

### 7.1 ★ Q1 `recv_some` 基类默认退化在**流式串口**上语义错位

- **现象**:`BaseTransport.recv_some` 默认 `return self.recv(max_bytes)`;
  **`TcpTransport` 覆写为真实流式读,`SerialTransport` 未覆写**;
  而串口 `recv` 是"**读满恰好 size;部分字节超时即关串口** + 抛 `TransportClosedError`"
  ⇒ `recv_some(n)` 退化为"要么凑满 n 字节,要么**关串口断线**";
- **最小复现**:`recv_some(4098)`(唯一调用点 `hostlink.py:134`,`KV_MAX_LINE=4096` + 2)
  → `TransportClosedError: …已收 11/4098 字节(帧截断…),已关闭串口`;
  端到端把 `SerialTransport` 挂 `KeyenceHostLinkTcpClient` →
  `read -> (False, None)`、`串口已关: True` ⇒ 每事务"超时→关串口→重连→再超时",**永久失败**;
- **可达性**:库内**暂无串口 KV Host Link 驱动**(`hostlink.py` 只有 TCP/UDP)⇒
  **当前在册路径不可达**,故定 **P2**(契约缺口;若按"仅看可达"可降 P3);
- **注意**:`serial.py:1` 模块头**自称**"RTU/**Host Link** 等走线" ⇒ 属**模块自我宣称的用法被契约挡死**;
- **修法**:`SerialTransport.recv_some` 用 `port.in_waiting` 探量 + `read(min(n or 1, max_bytes))`,
  空则抛 `TransportTimeoutError`(0 字节不断线口径);或基类默认改抛 `NotImplementedError`
  并写明"**默认实现仅对数据报走线有效,流式实现必须覆写**"(native 侧未来串口会同样中招,须两侧同改)。

### 7.2 Q3 补:`SerialConfig.validate()` 的**其他字段同类问题确实存在**(回答我此前的提问)

| 输入 | `validate()` | 实际后果 |
| --- | --- | --- |
| `baud_rate=nan`/`inf` | **通过** | 永久失败,`last_error` 是 **CPython 文案** |
| `baud_rate="9600"` | **TypeError** | **`read()` 抛 `builtins.TypeError`(穿出公开 API)** |
| `baud_rate=True` | **通过** | pyserial 以 **1 波特**打开 |
| `baud_rate=9600.7` | **通过** | pyserial **静默取整** 9600 |
| **`data_bits=6.5`** | **通过** | `_DATA_BITS_MAP[6.5]` → **`KeyError: 6.5`** ⇒ `last_error="连接 -:- 失败:6.5"`(**零信息**) |
| `data_bits="8"` | **TypeError** | 同 baud 字符串 |
| `data_bits` 4/9/nan、`stop_bits` nan/inf/3、空 `port_name` | ✅ 正确 `ValueError` | — |
| `parity` 非 N/E/O | ✅ 正确 `ValueError` | — |

- **根因链**:`configure_serial()` 只构造 `SerialConfig`,校验在 `SerialTransport.__init__`;
  而 `_create_transport()` **显式在 try 之外** ⇒ **`validate()` 的 `TypeError` 直接穿出 `read()`**;
  `KeyError` 更靠后被转成"连接失败:6.5";
- **修法**:库内**已有** `require_int`(先例 `base_client.py:121`)却未用于此 ⇒
  用 `require_int` + `math.isfinite` 收口 `baud_rate`/`data_bits`(明确拒 bool/float),
  或至少 `data_bits in _DATA_BITS_MAP` 消 `KeyError`、非数值统一 `ValueError`。

### 7.3 其余 P3(9 条,择要)

`connect()` **重复调用不清理旧通道**(实测旧 fd 仍活;失败后仍持旧 socket);
UDP 超长**四处文档写"截断"而实现抛 `DeviceError(-10040)`**(契约读者会被误导;native 须注明
**Proactor 形态例外**);`drain()` 上限判定用 `>=` ⇒ **缓冲恰好 64 个时报"仍在灌入"**(仅日志误判);
`drain()` 的 `WSAEMSGSIZE` 分支**不可达**(`recv(65535)` > AF_INET UDP 上限 65507 ⇒
**现有用例验证的是内核不可达状态**);`drain()` 未连接契约**不对称**(基类无 `:raises:`,UDP 抛、
TCP/串口返回 0);`connect_timeout` 对**已连接**传输无下发(文档称"立即生效"过强);
`_enable_keepalive` 的 **Windows `SIO_KEEPALIVE_VALS` 分支不可达**(本机两属性并存,
且 Linux 分支的 `setsockopt` 在 Windows **真实生效**;168-172 行死代码、零覆盖);
`recv_some(0)` **误报"对端关闭"**(`sock.recv(0)` → `b""`;sync 与 native **一致**,非口径分裂)。

### 7.4 ★ 唯一实质分裂 D1:UDP 超长在 **Selector vs Proactor** 下两种结果

```
[SelectorEventLoop] DeviceError code=-10040 | UDP 报文超过缓冲(响亮失败)
[ProactorEventLoop] 静默返回 len=100(未抛错,数据被截断)
```

**Proactor 是 3.8+ Windows 的默认事件循环** ⇒ 同机同协议两种结果。
**判定**:**同步侧更合理**(跨平台同口径 + 响亮失败);native 侧是 asyncio 能力边界
(3.7/3.8 的 `sock_recv_into` 不收 flags、IOCP 丢弃 `MSG_TRUNC`),库内**已披露**;
但实测确认"**截断帧被当完整帧上交、只靠协议层长度域兜底**"是**真实失效模式**
⇒ 建议:①能力表**显式写明该失效模式**;②要求协议层 `recv(size)` **留余量**;
③**补锁定用例**(现全库无测试覆盖该分裂)。

### 7.5 经核无问题(子代理实测,补充 §四)

**close 幂等 + 关闭后 send/recv/recv_some 全 `TransportClosedError`**;
**TCP `recv` deadline**(对端每 0.2s 挤 1 字节、size=10、timeout=1.0 → **1.00s** 抛);
**TCP 极大 size 不预分配**(`recv(10**8)`/`recv(10**9)` 进程 commit 增量 **0.0 MB**);
**UDP 超长无残留**;**UDP 收超时不断线**(同 socket 立刻可复用);
**`drain()` 真 socket 行为**(排 3 个、0.000s、`gettimeout()` 恢复);
**`drain()` 不会吞本轮应答**(只在事务锁内、发送之前调用);
**`send` 不静默短写**(TCP `sendall`/UDP 数据报原子/串口 `write_timeout>0` 时 pyserial 抛
`SerialTimeoutException`);**`receive_timeout` 即时下发三实现均实测生效**;
**串口 `recv` 双语义逐字实现**;**三张映射表与 pyserial 3.5 逐项对齐**;
**`_WSAEMSGSIZE_ERRNO` 真内核命中**;**UDP 对端未监听** → `ConnectionResetError(10054)` 按 `OSError` 拆连。

### 7.6 测试面缺口(补充 §六)

27 例、**无 skip/xfail**;TCP/UDP 往返用**真实回环 echo**(非全 mock)。**零用例**:
`TcpTransport.recv_some`(**0 例**)、TCP `recv` 的 deadline/涓流、对端帧中半关闭、
`send` 未连接/超时、`connect_timeout` 生效、`recv` 后 socket 超时恢复;
`UdpTransport.recv` 超时 → `TransportTimeoutError`(**0 例**)、`recv_some`、close 幂等、重复 connect、
**真内核 `WSAEMSGSIZE`** 与**真 socket `drain`**;
`SerialTransport.send`(**0 例**)、**`recv_some`(0 例 —— 即 Q1 陷阱)**、`drain`、
connect 失败路径、`baud_rate` getter、**与真 pyserial 常量映射**(测试自建同名同值 fake ⇒ 弱覆盖)。
**断言强度弱项**:`test_transport.py:350-351` 断言 `fake.blocking is False` 是**假 socket 模型**,
真 socket drain 后回到"阻塞+超时"。
**旁证**:`test_native_transport.py`(15 例)的 UDP 截断用假 socket 打 `WSAEMSGSIZE`,
与本机 Proactor 真实静默截断**不重合** ⇒ **D1 全库无测试锁定**。

---

## 七、待补切片(覆盖边界)

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

---
