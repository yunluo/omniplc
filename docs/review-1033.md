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
| **合计** | **32 例 × 2 层 = 64 次对拍** |
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

## 五、经核无问题(亲验)

| 项 | 核法 | 结论 |
| --- | --- | --- |
| native 族覆盖与声明一致 | 读 `native/__init__.py` 声明 + 逐目录清单 | ✔ 五族逐条对上 |
| aio 镜像完整 | 批量比对 `__all__` ↔ `aio` 属性 | ✔ 34/34,零缺失 |
| aio 参数名一致 | 34 对 `inspect.signature` 比对 | ✔ 仅两个**基类**不同(设计如此) |
| 测试密度 | 源码 6438 行 ↔ 测试约 6300 行 | ✔ ≈1:1 |
| 边界对拍机制**存在** | 读 `test_native_edge_parity.py` | ✔ 32 例 × 2 层,断言为"两层结果完全相等"(强) |
| 各族自有对拍**存在** | 读两个 native 测试文件 docstring | ✔ 帧逐字节 + 地址翻译 + 签名一致性 |

---

## 六、待补切片(覆盖边界)

> 本族只做了**覆盖矩阵与对拍机制**两项;以下**尚未展开**:
> 1. **native 传输层**:`native/transport.py`(652 行)的 asyncio 读写、超时、
>    取消(真中断语义)、连接生命周期,与同步 `transport/` 的口径对齐;
> 2. **native 基类**:`native/base.py`(1470 行)的事务/锁/重连/错误分类,
>    与 `core/base_client.py` 的**逐条对齐核查**;
> 3. **aio 层**:`aio/__init__.py`(2825 行)的线程池边界、`wait_for` 与真取消的差异、
>    事件循环亲和、`sync_client` 包装的异常传递;
> 4. **并发与取消语义**:两层在"取消/超时/重入"下的可观察差异。
> **覆盖边界声明**:§一~§五 的结论(覆盖矩阵、对拍覆盖面、镜像完整性)**独立成立**,
> 不依赖上述切片。

---

> **登记与排期**:本报告不落地任何修改。§二 属**测试加固**(零行为风险),
> 建议随审查确认后登记 `docs/todo.md`。
> **真机核验**:本报告结论为**库内静态亲验**(源码/测试结构 + 签名比对),**未上真机**。
