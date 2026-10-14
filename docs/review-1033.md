# review-1033 横切层审查报告(native / aio)— 首版

> 2026-10-11 · 基线:master `98c88bf` 附近(review-1032 后;工作区另有维护者未提交批)
> 范围:`native/`(base 1470 + transport 652 + modbus 1182 + melsec 967 + omron 828
> + inovance 297 + siemens 966 + `__init__` 76 = **6438 行**)、
> `aio/__init__.py`(2825 行);测试 9 个 `test_native_*.py` 共 **约 6300 行**。
> **本轮只检查与差距分析,不落地任何修改**。
>
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019~1032 同款)。
> **版本说明**:本文件为**首版**;§四 列出的切片(会话/并发/取消语义、aio 线程池边界)
> 尚未展开,到位后并入。

## 〇、执行摘要

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| **覆盖矩阵** | native 9 客户端 / 5 协议族,与模块头声明**逐条一致**;`aio` 层 **34/34 客户端全部有 `A*` 镜像**,参数名仅两个**基类**差异(符合设计) | — |
| **★ UDP 陈旧帧防护** | 6 个 UDP 客户端**全部有 `drain()`**;实测证明"无防护则**静默接受陈旧值**" → 该防护不可替代 | — |
| **★ 边界对拍覆盖面** | 集中式边界对拍(`test_native_edge_parity.py`)**只绑定 3 族**(modbus/melsec/fins)—— `omron`/`inovance`/`siemens` **不在该机制内** | **P2**(§二) |
| 测试体量 | native 层 6438 行源码对 **约 6300 行**测试(≈1:1),密度健康 | — |
| 会话/并发/取消语义 | **待切片**(§四) | 待定 |

---

## 一、覆盖矩阵(亲验)

### 1.1 native 实现的族覆盖

| native 实现 | 行数 | 对应同步客户端 |
| --- | --- | --- |
| `native/modbus.py` | 1182 | `ModbusTcpClient` 等 |
| `native/melsec.py` | 967 | `MelsecMcTcpClient` / `MelsecMcUdpClient`(1E/3E/4E) |
| `native/siemens.py` | 966 | `SiemensS7Client` |
| `native/omron.py` | 828 | `OmronFinsTcpClient` / `OmronFinsUdpClient` |
| `native/inovance.py` | 297 | `InovanceTcpClient` / `InovanceMcTcpClient` |
| `native/base.py` + `transport.py` | 2122 | 基类与原生传输 |

**与模块头声明一致**:`native/__init__.py` 声明覆盖"Modbus TCP / 以太网 MC(1E/3E/4E,TCP+UDP)/
欧姆龙 FINS(TCP/UDP)/ 汇川(H3U/H5U Modbus + MC 兼容 3E)/ 西门子 S7" —— **五族,逐条对上** ✔

### 1.2 aio 层的镜像完整性(亲验,批量比对)

| 项 | 实测 |
| --- | --- |
| `omniplc.__all__` 里的同步客户端 | **34** |
| `aio` 中对应的 `A*` 镜像 | **34**(零缺失) ✔ |
| 构造参数名逐对比对 | **34 对比对完成**,仅 **2 处**不一致:`BaseClient`(`(ip, port)` vs `(sync_client)`)与 `ModbusBaseClient`(`()` vs `(sync_client)`)—— **两处都是基类**,符合"包装同步孪生"的设计 ✔ |

**结论**:aio 层的"参数名与语义与同步层相同"这一文档声明**成立**(仅基类因包装方式不同)。

---

## 二、★ 机理验证:UDP 陈旧帧防护的**不可替代性**(亲验)

### 2.1 覆盖矩阵(亲验)

| UDP 客户端 | `drain()` 防护 |
| --- | --- |
| `MelsecMcUdpClient` | ✅(实测调用 1 次) |
| `KeyenceMcUdpClient` | ✅(实测调用 1 次 —— **经继承基类 `_transact` 获得**,我一度疑为缺口,实测排除) |
| `KeyenceHostLinkUdpClient` | ✅(`hostlink.py` 内显式) |
| `OmronFinsUdpClient` | ✅(显式) |
| `PanasonicMewtocolUdpClient` | ✅(显式) |
| `ToyopucUdpClient` | ✅(显式) |

**结论**:6 个 UDP 客户端**全部有陈旧帧防护**,零缺口 ✔

**★ 更精确的口径(子代理深挖 + 主会话亲验,订正我此前的"8 处/7 处有守卫")**:
src 内 `transport.drain()` 共 **7 处**调用点 ——
**5 处有 `datagram` 守卫**(`melsec.py:979`、`toyopuc.py:107`、`hostlink.py:101`、
`mewtocol.py:336`、`native/melsec.py:884`)
\+ **2 处无守卫但在 UDP-only 类内**(`omron.py:847` ∈ `OmronFinsUdpClient._transact`、
`native/omron.py:826` ∈ `AsyncOmronFinsUdpClient._transact`;二者 `_create_transport`
**恒返回 UDP 传输**,故直调等价安全)。
且 **7 处调用与全库 7 个 `UdpTransport` 实例化点一一对应**
(`melsec.py:1067`、`omron.py:852`、`toyopuc.py:417`、`hostlink.py:412`、`mewtocol.py:410`、
`native/melsec.py:967`、`native/omron.py:799`)⇒ **零缺口,且覆盖面可证**;
流式走线(MC 串口 `_transact`、FINS/TCP 握手)有意不调,符合契约。
(我此前记作"8 处、7 处有守卫"—— 那是把**测试侧**的 `writer.drain()` 也计入了,已订正。)

### 2.2 ★ 但机理验证揭示了"为何这条防护不可替代"(亲验)

**实测**:向两个 MC UDP 客户端投喂一个**形状合法**的数据报(3E 头 + 长度 + 结束码 0 + 数据),
并令 `drain()` 为空操作以模拟"无防护":

```
Melsec MC UDP（drain 置空操作）   -> (True, True)   <- 陈旧值被接受
Keyence MC UDP（drain 置空操作）  -> (True, True)   <- 陈旧值被接受
```

**根因(亲验源码)**:`codec_qna.parse_response_head` 只校验
**子头部(`D0 00`)+ 长度域 + 结束码**;
**MC 3E 响应既不比对命令码也不比对子命令** —— 回显面只有 4E 的**序列号**
(`_locate_data` 对 `is_4e` 才校验 `serial == expected_serial`)。

⇒ 对 **MC 3E over UDP**,一个"上一事务的迟到响应"只要形状合法(长度与结束码对)就会被
**静默当成本轮应答**;`drain()` 是**唯一防线**,而非"锦上添花"。

**这解释了基类 `drain()` docstring 的分层口径**(「有身份回显校验的协议按坏帧报错拆连,
无事务号的协议则静默读到旧值」)—— 本实测为它补上了**可复现证据**:
MC 3E 属后者(无事务号 + 回显不覆盖命令),故必须靠排空。

**建议**:①在 `codec_qna.parse_response_head` 的 docstring 里点明
"**3E 响应不校验命令码/子命令**"这一事实(当前只在 `_locate_data` 的 4E 分支体现,
3E 侧无显式说明);②把本节实测补成回归用例(用假数据报 + 置空 `drain` 断言"会接受陈旧值",
以锁住"drain 必须存在"这一不变量)。

---

## 三、P2:集中式边界对拍**只覆盖 3/5 族**(亲验)

### 3.1 现状

`tests/unit/test_native_edge_parity.py`(183 行)的设计是**逐族 × 逐边界**参数化:
"同一边界入参在两层必须给出完全相同的可观察结果"(`assert sync_out == async_out`)。

**但绑定表只列了 3 族**(亲验源码):

```python
# test_native_edge_parity.py:120-124
for family, sync_name, async_name in (
    ("modbus",  "ModbusTcpClient",     "AsyncModbusTcpClient"),
    ("melsec",  "MelsecMcTcpClient",   "AsyncMelsecMcTcpClient"),
    ("fins",    "OmronFinsTcpClient",  "AsyncOmronFinsTcpClient"),
):
    for case_name, _desc, op in shared + per_family[family]:
```

| 参数 | 值 |
| --- | --- |
| 共享边界(`shared`) | **6 例**:`read_string` 长度 0、`write_string` 空串、`write_bool` 拒 int、`write_bool` 拒字符串、未知数据类型、`read` 传 `STRING` |
| `modbus` 专属 | 8 例 | 
| `melsec` 专属 | 12 例 |
| `fins` 专属 | 3 例(表格内) |
| **合计** | **31 例 × 2 层 = 62 次对拍**(执行 `_cases()` 实测;共享 7 + modbus 3 + melsec 4 + fins 1) |
| **未纳入该机制** | **`omron`(若指 CIP 侧)、`inovance`、`siemens`** |

### 3.2 量化缺口(亲验)

把共享的 6 个边界模式在**各驱动自己的 native 测试文件**里检索:

| 测试文件 | 命中共享边界模式 |
| --- | --- |
| `test_native_siemens.py`(458 行) | **0/6** |
| `test_native_inovance.py`(535 行) | **0/6** |
| `test_native_melsec.py`(1192 行) | 1/6 |
| `test_native_modbus.py`(1214 行) | 1/6 |
| `test_native_omron.py`(1089 行) | 1/6 |

→ **共享边界实际只在集中式文件里被覆盖**,而集中式文件只跑 3 族
⇒ `siemens`/`inovance` 的 native 实现的**共享边界行为未与同步层对拍**。

### 3.3 公平计入:各族自有对拍确实存在

`test_native_inovance.py:1` 与 `test_native_siemens.py:1` 的模块 docstring 均声明
"同步/异步**对拍**"(前者"帧逐字节一致"+ 地址翻译 + 记号边界;后者"帧逐字节一致"+ 会话专属行为),
且 `test_native_inovance.py` 有签名一致性检查(逐类比对 `__init__` 签名)。

⇒ **修正后的结论**:不是"没有对拍",而是
**"对拍机制存在两处(modbus/melsec/fins 的集中式边界对拍 + 各驱动自有的帧级对拍),
而 6 个共享边界只在前者、前者不含 siemens/inovance"**。

### 3.4 为何值得修(P2 依据)

- 这 6 个共享边界正是**契约面**的边界(`(False, None)` vs `ValueError` 的取舍、
  空串/零长/类型错的处理);
- 库的契约声明是"**两层参数名与语义相同**" —— 语义相同的最强证据就是**同输入同结果**;
- 而**跨层语义漂移**恰恰是这类双实现架构最典型的回归形态(同步层改了、
  原生层忘改,或反之);
- 成本极低:在 `test_native_edge_parity.py` 的绑定表加两行 + 补 `per_family` 条目即可。

**修法建议**:把绑定表扩到全部 5 族;`per_family` 补 `siemens`/`inovance` 的族专属边界
(如 S7 的地址语法错、汇川的 `B`/`W` 进制与 `read_range` 越界 —— 后者见 review-1029 的两条 P1)。

---

## 四、★ 跨族测试强度量化(主会话全库统计,review-1035 轮次补)

为把各台账反复出现的"**期望值由被测组帧器自产**"(自证循环)从印象变成数字,
主会话对 `tests/unit/*.py` 做了全库统计:

| 检索形态 | 全库命中 |
| --- | --- |
| `codec_*.build_*` 出现在断言/期望侧 | **152** |
| `codec_qna.build_*` 同上 | **54** |
| `bytes.fromhex(...)`(可作"独立字面量"的代理指标) | **131** |
| 手写 `b"\x..\x..\x.."` 字节字面量 | **0**(全库无人用这种写法) |

**逐文件分布(命中数;绝对值比占比更可读)**:

| 测试文件 | `build_*` 自产 | `fromhex` |
| --- | --- | --- |
| `test_modbus_clients.py` | **30** | 5 |
| `test_mc_clients.py` | **21**(另 `codec_qna` 19) | 1 |
| `test_ab_ethip_clients.py` | **16** | 1 |
| `test_inovance_clients.py` | **16** | 0 |
| `test_modbus_codec.py` | 15 | 14 |
| `test_mc_serial_clients.py` | 14 | 6 |
| `test_ab_codec.py` | 3 | **28** |
| `test_siemens_s7_clients.py` | **1** | **30** |
| `test_mc_codec.py` | 0(另 `codec_qna` **20**) | 21 |
| `test_toyopuc_clients.py` | 8 | 0 |
| `test_kv_clients.py` | 3 | 0 |
| 其余 14 个文件 | ≤5 | ≤10 |

### 两条用法

1. **`fromhex` 数高的文件,证据强度通常更高**(它是"期望帧被独立写出"的代理指标)。
   典型对比:**`test_siemens_s7_clients.py`(1 : 30)** vs
   **`test_modbus_clients.py`(30 : 5)** 与 **`test_inovance_clients.py`(16 : 0)**。
2. **两者都只是代理指标**(`fromhex` 可能用于构造**应答**而非期望**请求**;
   `build_*` 也可能只构造**中间片段**)。故本表**只作定位用** ——
   判定某文件是否"自证循环"仍须逐处读断言(各台账已各自判过)。

### ★ 该表的价值:它指向的族**恰好是缺陷被掩盖过的族**

| 族 | 自证程度 | 被掩盖的缺陷(出处) |
| --- | --- | --- |
| 汇川 MC | `build_*` 16 处、`fromhex` **0** | `B`/`W` 进制错(`B10`→16)**零用例**(review-1029 P1-1) |
| 松下 MC | `_expected()` 用**同表同函数** | 码表值断言在实现改错时**跟着一起绿**(review-1025 §8.3) |
| 三菱 KV MC | `_expected()` 同构 | 码表**从未真机验证**却被快照锁死(review-1028 §6.2) |
| AB | 应答侧用**同款 `struct.pack` 格式串** | 字段序自洽性错误测不出(review-1035 §五) |
| 欧姆龙 CIP | 事务断言 `endswith(codec 自产请求)` | BOOL 载荷少 1 字节测不出(review-1031 §二) |

⇒ **建议(零行为风险)**:对上述 5 处把期望帧改为**独立字面量或规范算例** ——
其中 **AB 与欧姆龙 CIP 已有现成算例可直接抄**
(Rockwell p.28-42 的 16 个抓包算例、W506 §7-3-2 的 EPATH 算例)。

---

## 五、★ 跨族常量依据覆盖率(主会话全库 AST 统计,review-1036 轮次补)

**方法**:对 `core/constants.py` 解析 AST,取**模块级带类型标注的赋值**(`NAME: int = ...`),
把紧随其后的字符串当作其 docstring;按"是否含 `依据/来源/手册编号/§/印刷页/指南/规范/档案/Vol/待核/待补`"
判定"有标注";只统计**协议面常量**(名字含 `MAX/MIN/LIMIT/PORT/DEFAULT/CODES/AREA/POINTS/COUNT/SIZE/TEXT/BASE/BIT`)。

**全库**:协议面常量 **218** 个,**有标注 54 个 = 25%**。

| 前缀(族) | 总数 | 有标注 | 覆盖率 |
| --- | --- | --- | --- |
| **MX** | 5 | 5 | **100%** |
| **HIKROBOT** | 13 | 11 | **85%** |
| **INOVANCE** | 6 | 5 | **83%** |
| MEWTOCOL | 5 | 2 | 40% |
| SR | 5 | 2 | 40% |
| FINS | 34 | 11 | 32% |
| KV | 8 | 2 | 25% |
| PANASONIC | 4 | 1 | 25% |
| MC | 39 | 8 | 21% |
| MODBUS | 31 | 6 | 19% |
| TOYOPUC | 9 | 1 | 11% |
| **AB** | 11 | **0** | **0%** |
| **S7** | 9 | **0** | **0%** |
| **KEYENCE** | 2 | **0** | **0%** |
| **OPCUA** | 3 | **0** | **0%** |
| **MTCONNECT** | 3 | **0** | **0%** |
| 其余(通用工程值/数值范围等) | 71 | 0 | 0% |

### ★ 该表的**强信号**:覆盖率与"该族有没有手册"高度相关

三张 80~100% 的表(**MX / HIKROBOT / INOVANCE**)恰好是本项目**真机核证或手册最充分**的三族;
而 **0% 的 5 族**(AB / S7 / KEYENCE / OPCUA / MTCONNECT)恰好是
**"手册未收录或只有概览"**的族 ⇒ **标注缺口的分布不是随机的,而是"有没有手册"的直接投影**。

**与各台账的独立结论互相印证**:AB(review-1035 P1:66 处漏标)、
S7(review-1034 P1:24 处引用指向库外已删源码)、KEYENCE(review-1028 §6.2)、
OPC-UA/MTCONNECT(只有概览层文档)。

### 可执行建议(零行为风险)

1. **优先补 0% 的 5 族** —— 它们的常量**没有任何锚点**,是依据链最薄处;
2. **`FINS` 34 个常量只标 32%** —— 其中**同批封装常量**在 review-1027 §3.2 已判 P2"无标注",
   本表给出**量化确认**;
3. **`MC` 39 个只标 21%**、**`MODBUS` 31 个只标 19%** —— 两族**手册已收录**,属
   "**有手册但常量未就近标注**",可低成本补齐;
4. 建议在 `constants.py` 文件头加口径说明:
   "**本文件协议面常量须就近标注依据或「待核」**;通用工程值(超时/缓存/数值范围)免标"
   —— 使"哪些该标"有明确规则,便于将来门禁化。

---

## 六、可复用审查探针(方法学产出 + 一处虚警留痕)

§四/§五 两张表是**可复算的**。为便于修复批与后续复核,复算逻辑已写成**只读探针**
(置于 `%TEMP%`,**不入库** —— 它面向审查而非产品):

```
.venv\Scripts\python.exe -B %TEMP%\review_probe.py
```

输出两部分:①全部码表的**表内重码**扫描(标出哪些是**已文档化别名**);
②`constants.py` 协议面常量的**依据/待核覆盖率**(按前缀分族)。

### ★ 该探针自身的一处虚警(留痕,避免下轮重复投入)

- 首版把 **`S7_MODEL_PRESETS`** 也当码表扫描,报出 **3 处"未文档化同码 0x100"**
  (`S7_1200`/`S7_1500`/`S7_300`/`S7_400` 共用首字段);
- 核实后确认**是探针判定口径错**,不是库的缺陷:该表的值是
  `S7ModelPreset(local_tsap, connection_type, rack, slot, tpdu_size_code, fixed_remote_tsap)`
  —— **首字段是"本端 TSAP",多机型合法共用 `256`**(实测四款型号均 `local_tsap=256`);
  语义是"这些型号用同一套连接参数",与"同一码值指不同软元件"**不是一回事**;
- 修法:探针加 `_NOT_CODE_TABLES` 显式排除,并在代码注释里写明该虚警的成因。

> **这条例子的意义**(与 §四/§五 的方法论配套):
> **结构化扫描能高效定位候选缺口,但"是否真缺陷"必须回到语义裁决**。
> 探针输出的**候选**若不逐条人工判断,就会把"合法的共用常量"误报成"码表撞码" ——
> 这正是本报告 §四 那张表(自证循环)想说明的同一个道理的另一面。

---

## 七、★ 跨族契约破口:`read_many([])` 在 **15/30** 个客户端上抛异常(主会话全库实测)

### 7.1 契约原文(亲验)

`core/base_client.py` 的 `read_many` / `write_many` docstring **明确区分了两种契约**:

> 空列表返回 ``[]``(与 `write_many` 一样;**注意 `read_batch`/`write_batch` 的空表语义不同 ——
> 它们抛 `ValueError`**)

即:**`read_many`/`write_many` 空表 → 返回 `[]`**;`read_batch`/`write_batch` 空表 → `ValueError`。
两者是**有意的不同契约**。

### 7.2 实测(全库 30 个可构造客户端)

```
read_many([], "ushort")   ->  合规 15 / 违规 15
write_many([])            ->  合规 30 / 违规  0
```

**违规的 15 个**(错误文案一致暴露根因 —— 它们**委托给了 `read_batch`**):

| 客户端 | 实测错误 |
| --- | --- |
| `MelsecMcTcpClient` / `MelsecMcUdpClient` / `MelsecMxClient` | `ValueError: read_batch 至少需要一个 (地址, 数据类型) 项` |
| `KeyenceMcTcpClient` / `KeyenceMcUdpClient` | 同上 |
| `PanasonicMcTcpClient` / `InovanceMcTcpClient` | 同上 |
| `OmronFinsTcpClient` / `OmronFinsUdpClient` | 同上 |
| `SiemensS7Client` | 同上 |
| `OpcUaClient` | `ValueError: read_batch 至少需要一个 (NodeId, 数据类型) 项` |
| `AllenBradleyEthIpClient` | `ValueError: read_batch 至少需要一个 (标签, 数据类型) 项` |
| `InovanceRtuClient` / `ModbusRtuClient` | `ValueError: 请先调用 configure_serial() 配置串口参数`(**串口路径在入参校验前就断言**) |

**合规的 15 个**(含 `ModbusTcpClient`、`ToyopucTcpClient/UdpClient`、
`KeyenceHostLinkTcpClient/UdpClient`、`PanasonicMewtocol*`、海康四族、`MTConnectClient`、
`MitsubishiEzSocketClient`、`KeyenceSrClient` 等)。

### 7.3 判定

- **定级 P2(全库系统性契约破口)**:①**一半客户端**违反**自己基类**写明的契约;
  ②`write_many` **完全合规** ⇒ **读写不对称**(同族里"写空表正常、读空表炸");
  ③现场影响真实 —— 按"空点表即空结果"写的上层循环(如"本周期没有要采的点")会**拿到异常**;
- **根因统一**:违规者都把 `read_many` 实现为**委托 `read_batch`**,于是继承了后者的
  "空表抛错"语义 ⇒ **在实现合并批量读时把两种不同契约混为一谈**;
- **已由各族独立发现、本表归并并补全**:`review-1034 §五 P3-3`(S7)、
  `review-1037 §三 P2-4`(OPC-UA)—— 本表**补全了另外 13 个客户端**。

### 7.4 修法(零行为风险、成本极低)

在每个违规客户端的 `read_many` 覆写**首行**加 `if not addresses: return []`;
串口两族应把该早返回放在 `configure_serial()` 断言**之前**(空表查询不该要求先配置串口)。
**并补一条跨族契约用例**:参数化遍历全部客户端断言 `read_many([], t) == []` ——
将来新增驱动若再"委托 read_batch"会被直接拦下。

---

## 八、★ 跨族错误码表盘点 + CIP 扩展状态覆盖缺口(主会话亲验)

### 8.1 全库错误码/文案表盘点

| 表 | 条数 | 键型 | 备注 |
| --- | --- | --- | --- |
| `FINS_END_CODE_TEXT` | **85** | int | review-1027 已用 `extract_tables()` 逐条对过手册,**零缺号零补号** |
| `AB_CIP_STATUS_TEXT` | 45 | int | CIP 通用状态码 |
| `TOYOPUC_ERROR_TEXT` | **27** | int | review-1026 §6.5 订正过计数(README 曾写 26) |
| `MC_DEVICE_CODES` / `MC_1E_DEVICE_CODES` | 28 / 15 | str | 软元件码表(见 §四) |
| `KV_ERROR_TEXT` | 10 | str | `E0~E9`(**来源自述"公开口径",非收录手册**) |
| `MODBUS_EXCEPTION_TEXT` | **9** | int | Modbus 异常码 1~9,**完整** |
| `HIKROBOT_CMD_ERRNO_TEXT` | 7 | int | 命令错误码 −1~−7,与手册逐字一致(review-1036) |
| `AB_EIP_STATUS_TEXT` | 6 | int | ENIP 封装状态码 |
| `AB_CIP_EXTENDED_STATUS_TEXT` | **55**(嵌套) | int→int | 见 §8.2 |
| `FINS_DAY_OF_WEEK_TEXT` | 7 | int | 时间字段文案 |

### 8.2 ★ CIP 扩展状态表在 **Omron 侧存在覆盖缺口**(亲验)

结构是**两级嵌套** `{通用状态: {16 位扩展码: 文案}}` —— 实测分布:

| 通用状态 | 扩展码条数 | 码域 |
| --- | --- | --- |
| `0x01` | **25** | `0x0100`~`0x011A`、`0x0203`~`0x0206`、`0x0301`~`0x0317` |
| `0x04` | 2 | `0x0000`/`0x0001` |
| `0x05` | 2 | `0x0000`/`0x0001` |
| `0x1F` | 1 | `0x0203` |
| `0xFF` | **25** | `0x0007`、`0x2001`~`0x2114` |

**对照 W506(Omron NJ/NX)抽核的两个码**:

| 手册码 | 含义 | 表内 |
| --- | --- | --- |
| `0x0109` | Invalid connection size | ✅ **已有** |
| `0x0114` | 电子钥匙(key segment)不匹配 | ❌ **缺失** |

**判定**:该表**按 Rockwell/Logix 口径建立**(`0x01xx` 与 `0xFFxx` 两大段都是 Logix 常见域),
而 **Omron CIP 复用同一张表**(`plc/omron/cip.py` 经 `codec_cip` 共用)⇒
**NJ/NX 特有扩展码(如 `0x0114`)退化为"无文案"**。查表逻辑本身是**两级正确查法**
(亲验 `_extended_status_text`:通用状态 + 从应答读 16 位扩展码),故这是**覆盖缺口**而非查表 bug。

**注**:review-1031 已指出这些表"零页码、扩展表指向网页/未收录文档";本节补的是**覆盖口径**问题
—— 建议表头注明"**按 Logix 口径建立;Omron 侧扩展码待补**",并在拿到 **CIP Vol 1 Appendix B** 后统一补全。

---

## 九、经核无问题(亲验)

| 项 | 核法 | 结论 |
| --- | --- | --- |
| native 族覆盖与声明一致 | 读 `native/__init__.py` 声明 + 逐目录清单 | ✔ 五族逐条对上 |
| aio 镜像完整 | 批量比对 `__all__` ↔ `aio` 属性 | ✔ 34/34,零缺失 |
| aio 参数名一致 | 34 对 `inspect.signature` 比对 | ✔ 仅两个**基类**不同(设计如此) |
| 测试密度 | 源码 6438 行 ↔ 测试约 6300 行 | ✔ ≈1:1 |
| 边界对拍机制**存在** | 读 `test_native_edge_parity.py` | ✔ 31 例 × 2 层,断言为"两层结果完全相等"(强) |
| 各族自有对拍**存在** | 读两个 native 测试文件 docstring | ✔ 帧逐字节 + 地址翻译 + 签名一致性 |

---

## 十、待补切片(覆盖边界)

> 本族只做了**覆盖矩阵与对拍机制**两项;以下**尚未展开**:
> 1. **native 传输层**:`native/transport.py`(652 行)的 asyncio 读写、超时、
>    取消(真中断语义)、连接生命周期,与同步 `transport/` 的口径对齐;
> 2. **native 基类**:`native/base.py`(1470 行)的事务/锁/重连/错误分类,
>    与 `core/base_client.py` 的**逐条对齐核查**;
> 3. **aio 层**:`aio/__init__.py`(2825 行)的线程池边界、`wait_for` 与真取消的差异、
>    事件循环亲和、`sync_client` 包装的异常传递;
> 4. **并发与取消语义**:两层在"取消/超时/重入"下的可观察差异。
> **覆盖边界声明**:§一~§九 的结论(覆盖矩阵、对拍覆盖面、镜像完整性)**独立成立**,
> 不依赖上述切片。

---

> **登记与排期**:本报告不落地任何修改。§二 属**测试加固**(零行为风险),
> 建议随审查确认后登记 `docs/todo.md`。
> **真机核验**:本报告结论为**库内静态亲验**(源码/测试结构 + 签名比对),**未上真机**。

---

## 修复状态回标(2026-10-15,见 `docs/review-1040.md` 总览索引)

> 维护者已落地本报告部分条目,此处**只回填落地事实与审查方的复核方式**,
> 不改动本报告原有结论、定级与节序。「复核方式」列区分 **亲验**(审查方本轮重读源码/实测)
> 与 **转述**(仅据提交信息,未逐行复验)。

| 本报告条目 | 落地提交 | 复核方式 | 现状 |
| --- | --- | --- | --- |
| §七 P2 `read_many([])` 在 15/30 客户端破契约 | `f0cc3ea` | **亲验** 全库 `def read_many` 共 **9** 处:8 处显式空列表早返回 + 基类 `core/base_client.py:802` 列表推导天然返回 `[]` ⇒ **9/9 合规** | **已修** |
| §三 P2 集中式边界对拍只覆盖 3/5 族 | —— | **已复算(review-1041)**:执行 `_cases()` = **31 例 / 62 次**(fins 9 / melsec 12 / modbus 10),3/5 族结论成立 | **未修** |
| §四 跨族测试强度量化(build_* 152 vs fromhex 131) | —— | 统计性结论,未复算 | **未修** |
| §五 常量依据覆盖率 54/218 = 25% | —— | **已复算(review-1041)**:54/25% **不可复现**,按本报告声明口径为 **45/218 = 20.6%**(HEAD 47/218 = 21.6%);「其余 71」应为 **31**,原表行合计 258 与标题 218 自相矛盾 | ⚠️ 数字待订正 |
| §八 CIP 扩展状态表在 Omron 侧覆盖缺口(0x0114) | —— | 未复验 | **未修** |

---

## 统计量复算订正(2026-10-15,见 `docs/review-1041.md`)

> 维护者落地修复批后,审查方对本报告全部定量声明做了**逐条按声明口径重算**。
> 本节**只订正数字**,不改本报告原有结论、定级与节序。

| 本报告条目 | 原值 | 复算值 | 判定 |
| --- | --- | --- | --- |
| §五 协议面常量总数 | 218 | **218** | ✅ 一致 |
| §五 16 族合计 | 187 | **187** | ✅ 一致 |
| §五 「其余(通用工程值/数值范围等)」行 | 71 | **31** | ❌ **真错**:致表行合计 = 258,与本表标题 218 **自相矛盾** |
| §五 有标注总数 / 覆盖率 | 54 / 25% | **45 / 20.6%**(d0971f0,按本报告**声明口径**);47 / 21.6%(HEAD);放宽到「赋值块文本」口径为 47 / 50 | ⚠️ **不可复现**,差 9 集中在 7 行的「有标注」判据 |
| §四 `build_*` 自产 | 152 | **151**(口径反解:接收者名含 `codec` 的 `.build_*` 且所在行含 `assert`) | ✅(±1) |
| §四 `codec_qna.build_*` | 54 | **54** | ✅ |
| §四 `bytes.fromhex` | 131 | **133** | 🔁 漂移(测试文件后续新增) |
| §四 手写字节字面量 | 0 | **0** | ✅ |
| §1.1 native 客户端 / 族 | 9 / 5 | **9 / 5** | ✅ |
| §1.2 aio 镜像 | 34/34 零缺失 | **34/34 零缺失** ✅;补充:aio 实有 **35** 个 `A*Client`,多出 `AAllenBradleyEthIpClient`,其同步类 `AllenBradleyEthIpClient` 不在 `__all__` | ⚠️ 补注 |
| §三 边界对拍计数 | 31 例 / 62 次 | **31 / 62**(执行 `_cases()` 取真实清单) | ✅ |
| §七 `read_many([])` 破契约 | 15/30 | 静态 MRO 口径 **14/34** | ✅(±1;分母口径:导出客户端 34 vs 可构造 30) |

**定性结论全部不受影响**:§三(只覆盖 3/5 族)、§四(自证循环指向的 5 族)、
§五(覆盖率与「有没有手册」相关、0% 的 5 族是依据链最薄处)、§七(读写不对称)——
其中「0% 的 5 族」与「3/5 族」均已逐个复现。

> **口径教训**:§四 的 152 需**逐文件指纹反解**口径才能复现(照字面读会得 **276**);
> §五 的 54 无论如何换口径都复现不出。**定量结论必须附可执行判据**,否则不可复核。
