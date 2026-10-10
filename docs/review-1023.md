# review-1023 Modbus 专项审查报告(只差不做)

> 2026-10-10 · 基线:master `1fd1a3f`(review-1022 台账后,v0.56.0 未发版)
> 范围:`src/omniplc/plc/modbus/`(modbus.py 2004 / codec.py 1219 /
> address.py 189 / `__init__.py` 13 = **3425 行**)+ native 镜像
> `native/modbus.py`(1163 行)+ 测试面(test_modbus_clients 127 例,无参数化 /
> test_modbus_codec 22 / test_golden_modbus 13 函数(5 参数化 → 实收 26 项) /
> test_native_modbus 17 / **18 个黄金帧样本**——子代理实测纠正了本节初稿的"20 个")。
> **本轮只检查与差距分析,不落地任何修改**;修复排期见 `docs/todo.md`。
>
> 方法:主会话逐段精读**客户端层**(地址解析 / 合并算法 / 事务层 /
> 入参守卫 / 边界)+ **动态对抗实证**(自造寄存器镜像做帧级往返、对抗性
> 畸形响应、穷尽式异常面喂入、合并分块不变量验算);两路只读子代理并行
> (**codec 帧面对规范 PDF 逐条核** / **测试覆盖矩阵**)。
> **亲验** = 主会话读原文或实跑确认;**审读** = 子代理结论,主会话抽查;
> **未核实** = 单源或未展开。
>
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019/1021/1022 同款)。

## 〇、执行摘要

Modbus 是全库**使用面最广**的驱动(协议清单首项、README 快速上手示例),
本轮实证结论:

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| **codec 帧面**(对三份规范逐条核) | **实现与规范逐字节吻合**(19 个功能码算例回灌全通);但**全模块零页码级依据注释** | **P1-1**(§8.1) |
| 帧面实现细节 | 10 项(P2×5 / P3×4 等):7 个 `parse_*` 漏异常分流、FC08/FC17 硬编码、FC22 little 无出处 | §8.2 |
| **超时属性守卫** | **`connect_timeout`/`receive_timeout` 接受 NaN/inf → 传输层抛非 OSError → FD 泄漏** | **P1-1**(§二) |
| 地址与入参守卫 | 区域×类型、跨度、只读区、单笔上限**全部入参期零发送拒绝** | — |
| 合并分块算法 | 9 组对抗验算**不变量全部成立**(含倒序输入/顶格/空洞密集) | — |
| 客户端契约 | 读/写/批量语义与文档一致 | P3 若干 |
| **测试面** | FC02 全链路零用例、翻页三守卫零用例、native station/word_order 零用例 | **P1-1~P1-5**(§9.1) |
| 测试质量 | 23 处同源断言、黄金样本无再生成门禁、2 处断言过弱(共同回归会全绿) | §9.3、§9.4 |

**本轮两条 P1,性质截然不同**:

- **P1-1(§二,超时属性 → FD 泄漏)** 是本轮**唯一的实质缺陷**——但它**不在
  Modbus 内部**,是 `core/base_client.py` + `transport/*` 的守卫缺口,
  经 Modbus 走线实测暴露,**影响全部客户端**。之所以收录本报告:它是本轮唯一被
  实证的失败通道,Modbus 是最容易触发的驱动。
- **P1-1(§8.1,codec 零依据注释)** 是**纪律红线**:1219 行帧面实现**逐字节正确**,
  但**无法回溯到手册**——而这正是本项目区别于"随便一个 Modbus 库"的立身依据。
  **反差点值得强调:代码是对的,账没记。**

**Modbus 帧面本身未发现任何静默错值或假成功**(§8.3),这是本轮最强的正面结论。

---

## 一、审查范围与动态实证清单

**主会话动态实证**(全部在 `.venv` 3.7.9 上以**自造寄存器镜像 / 桩传输**
完成,不联网、不落盘):

| # | 实证项 | 结果 |
| --- | --- | --- |
| 1 | 帧级往返:ushort/short/uint/bool(位提取)/批量混序/重复地址 | 全部与镜像一致 ✔ |
| 2 | 跨宽重叠:同址 `read_uint` + `read_ushort` + `read_bool(hr0.2)` | 三者各自正确解码 ✔ |
| 3 | 单笔上限边界:位 2000/2001、字 125/126、FLOAT 62/63(124/126 字) | 精确在规范上限处放行/拒绝 ✔ |
| 4 | 跨度越界:`hr65535` 单字可读、`hr65535` uint 拒、`hr65534` double 拒 | 零发送期 `ValueError` ✔ |
| 5 | 只读区写:`write_bool("ir0.3"/"di0.1")`、`write_short("di0")` | `ValueError` + **零帧发送** ✔ |
| 6 | 畸形响应(byte_count 多/少/奇数、零字节、异常码、protocol_id≠0、长度域过小) | 全部 `(False, None)` ✔ |
| 7 | **穷尽异常面**:19 个 codec 解析入场 × 2432 组随机/截断/畸形字节 | **零非契约异常** ✔ |
| 8 | 第二批穷尽:8 个解析/构建入场 × 1576 组 | **零非契约异常** ✔ |
| 9 | 合并分块不变量 9 组(同址 50/125、交错双串 200、126 连续切 2 块、2000 位顶格、隔一取一 500 chunk、uint 重叠 70、地址空间末端倒序 100) | 跨度恒 ≤ 上限、条目零丢失、**跨度算式与真实最大值一致** ✔ |
| 10 | 空/退化入参 | 见 §四 P3-1(与文档口径一致) |
| 11 | **超时属性 NaN/inf → 真 socket → FD 收不回** | **缺陷(§二)** ✘ |

---

## 二、P1:超时属性接受 NaN/inf,经传输层配置失败路径**泄漏文件描述符**

### 2.1 现象(三步实证)

**① 属性守卫缺口**(亲验):`connect_timeout` / `receive_timeout` 的 setter
只判 `<= 0`(`core/base_client.py:559`、`:583`),而 **NaN 与任何数比较恒为
False**、`inf <= 0` 亦为 False → **两者都通过校验**:

```
connect_timeout = nan   -> 接受,现值 nan      ← 缺 math.isfinite 守卫
connect_timeout = inf   -> 接受,现值 inf
receive_timeout = nan   -> 接受,现值 nan
receive_timeout = inf   -> 接受,现值 inf
receive_timeout = -1    -> ValueError(负值被拦)
heartbeat_interval= nan -> ValueError(已拦,有 isfinite 守卫)   ← 对照
```

**同库对照**:`heartbeat_interval`、`Monitor.deadband`/`jitter`、
`TagTable.scale/offset` 全部用 `math.isfinite` 拦 NaN/inf,
**唯独两个超时属性没有** —— 是守卫覆盖面的遗漏,不是口径选择。

**② 传输层抛非 OSError**(亲验真实 socket):

```
socket.settimeout(nan) -> ValueError: Invalid value NaN (not a number)
socket.settimeout(inf) -> OverflowError: timestamp too large to convert to C _PyTime_t
```

而 `transport/tcp.py` 与 `transport/udp.py` 的 `connect()` **只捕获
`OSError`**:

```python
# transport/tcp.py:56-68
sock = socket.create_connection((ip, port), timeout=self._connect_timeout)
try:
    sock.settimeout(self._receive_timeout)      # ← NaN/inf 在此抛 ValueError/OverflowError
    sock.setsockopt(...); _enable_keepalive(sock)
except OSError:                                 # ← 捕不到
    sock.close()                                # ← sock.close() 被跳过
    raise
self._socket = sock                             # ← 未执行,self._socket 仍为 None
```

**③ FD 收不回**(亲验,直接驱动传输层):

```
receive_timeout=nan  -> ValueError: Invalid value NaN (not a number)
     _socket = None  (close() 能否收 FD: False)
receive_timeout=inf  -> OverflowError: timestamp too large to convert to C _PyTime_t
     _socket = None  (close() 能否收 FD: False)
UDP receive_timeout=nan -> ValueError: Invalid value NaN (not a number)
     _socket = None  (close() 能否收 FD: False)
```

`create_connection` 已建立 TCP 连接并持有 FD,但异常路径跳过 `sock.close()`,
且 `self._socket` 未赋值 → 客户端侧兜底
(`BaseClient.connect` 的 `transport.close()`)也**收不到**(`_socket is None`)。
**该 FD 只能等 GC 回收**,而 `_execute` 的惰性重连 + 退避门控会**反复重试**:
每次重试新建一个 FD → **缓慢 FD 泄漏**,长跑进程最终 `EMFILE`。

**④ 失败信息误导**(亲验,走完整客户端路径):

```
receive_timeout=nan    -> connect() 返回 False,
                          last_error='连接 127.0.0.1:57110 失败:Invalid value NaN (not a number)'
connect_timeout=inf    -> last_error='连接 127.0.0.1:57110 失败:timestamp too large to convert to C _PyTime_t'
```

异常虽被 `BaseClient.connect` 的 `except Exception` 收进 `last_error`
(**未逃逸**,这点比预想好),但:
- 文案是 **socket 层的原始英文消息**,完全没有"超时参数非法"的提示;
- 分类落 `UNKNOWN`(不在 §5 分类表的任何一类),现场按分类分流会看到"未知连接失败";
- 指向的是"连接失败",而真正的原因是**参数非法**——排障方向被带偏。

### 2.2 影响面与定级依据

| 项 | 判断 |
| --- | --- |
| 影响客户端 | **全部**(两个属性在 `BaseClient` 共用;TCP/UDP 走线同款) |
| 触发条件 | 参数来自配置/计算(`nan` 在 JSON 里可写 `NaN`、`0/0`、缺失字段的浮点运算),属**真实可发生**的输入 |
| 后果严重度 | FD 泄漏(长跑进程资源耗尽)+ 误导性错误信息;**不是**数据损坏或假成功 |
| 定级 | **P1**(资源泄漏 + 契约面误导;非静默错值,故非 P0) |

### 2.3 修法建议(三处,都很小)

1. **属性期收口**(首选,与同库口径一致):在两个 setter 各加
   `if not math.isfinite(seconds): raise ValueError(...)`;
   同步补 `transport/base.py:48`/`:59` 的同名 setter(它同样只判 `<= 0`);
2. **传输层清理收口**:`tcp.py`/`udp.py` 的 `connect()` 把清理改为
   **不依赖异常类型**——`except BaseException: sock.close(); raise`
   或 `try/finally` 判定"未赋值即关",否则任何未来的非 OSError 配置异常
   都会重演本泄漏(注释里已写"配置失败同样要收 FD",但守卫只覆盖 OSError);
3. **用例**:`connect_timeout`/`receive_timeout` 传 `nan`/`inf` → 赋值期
   `ValueError`;并加一条**传输层**用例锁定"配置期异常必须收 FD"
   (可用 `socket.socketpair` 或计数式假 socket)。

**注意**:第 1 条属**公共 API 行为收紧**(原接受 → 现拒绝)。按
`CONTRIBUTING.md` §六,纯缺陷修正不受 deprecation 周期约束,但须在
CHANGELOG「行为变更提示」披露。

---

## 三、经核无问题(明确不成立,避免下轮重复投入)

本轮**大量**疑似项经实证**撤销**,逐条留痕:

| # | 曾疑似 | 核验结论 |
| --- | --- | --- |
| 1 | FC01 正常响应被拒(我第一轮测试出现 `(False, None)`) | **不成立**:是我把 MBAP 长度域写成 `6`(应为 `2+len(PDU)=4`);库的长度域校验**正确拒绝**了畸形帧 —— 我的错误用例反而验证了它 |
| 2 | `read_uint("hr1")` 返回值与期望不符 | **不成立**:我复用了上一段脚本残留的 `REG[2]`,镜像状态污染;清空后逐项一致 |
| 3 | codec 可能在畸形输入上抛出非契约异常 | **不成立**:19+8 个入场 × 4008 组随机/截断/畸形字节喂入,**零非契约异常**(全部 `ValueError`/`OmniPLCInternalError`) |
| 4 | 合并分块的跨度算式 `chunk[-1].offset + width - start` 可能漏算中间更宽条目 → 数组越界 | **不成立**:分组键含 `width`,组内宽度统一(位类 1、`BOOL` 1 字、16 位 1 字、32 位 2 字、64 位 4 字),故 `chunk[-1]` 恒为最大端;9 组对抗验算(含同址 125 个 2 字类型、uint 相邻重叠)证明"算式值 == 真实最大跨度"且恒 ≤ 上限 |
| 5 | `_coalesce_group` 的"重叠吸收"规则会切错块 | **不成立**:规则 5(重叠按合并跨度吸收)在实测中正确 —— 同址 `uint`+`ushort`+`hr0.2` 三者混批各自解码正确 |
| 6 | 只读区(ir/di)写可能被放行到组帧期 | **不成立**:`write_bool("ir0.3"/"di0.1")`、`write_short("di0")` 全部入参期 `ValueError` 且**零帧发送**;`_coalesce_and_write` 里保留 `INPUT_REGISTER` 分支属**无害死分支**(更早的 `_check_address` 已拒) |
| 7 | 单笔上限边界存在 off-by-one | **不成立**:位 2000 放行 / 2001 拒;字 125 放行 / 126 拒;FLOAT 62(124 字)放行 / 63(126 字)拒 —— 与规范上限精确吻合 |
| 8 | `read_range` 超限应自动分页(与"批量"直觉不符) | **设计如此**:docstring 明写"单笔上限"并 `:raises ValueError: ... 超单笔 FC 上限`;要分页用 `read_many`/`read_batch`。属**契约选择**,非缺陷(建议文档加一句对比,见 §四 P3-3) |
| 9 | MBAP 协议标识符未校验 | **不成立**:`codec.parse_mbap_header:646-652` 校验 `protocol_id == 0`,实测 `protocol_id=1` 被拒 |
| 10 | RTU 收包长度对变长响应(FC12/17/24/43)会错位 | **不成立**:四条变长路径各自**增量收包**且长度域先按 ADU 上限封顶校验(`modbus.py:1455-1486`) |
| 11 | 写响应回显校验过松 | **不成立**:`parse_write_response:289` 要求 `len(pdu) == 5 且 pdu == request_pdu[:5]`(逐字节回显) |
| 12 | 读响应字节计数未交叉校验 | **不成立**:`parse_read_response:268` 同时校验 `byte_count == expected` **且** `len(pdu) == expected + 2`(多/少/奇数全部拒绝) |
| 13 | `inter_frame_delay`/`broadcast_turnaround` 的 NaN 会 `time.sleep(nan)` | **不成立**:虽同缺 `isfinite` 守卫,但二者只进 `time.sleep`,`sleep(nan)` 是**立即返回**而非抛错(与 socket `settimeout` 的后果不同);且 `_broadcast_silence` 三者取 `max()`,`max(..., nan)` 的传播顺序可能让 nan 被忽略。**定 P3**(§四 P3-2),不属 P1 |
| 14 | 地址解析可能接受越界偏移 | **不成立**:`parse_address` 结果一律经 `_check_address:1755` 做 `offset + units ≤ 65536` 跨度校验(实测 `hr65535`/`hr65534` 分别按类型拒绝) |
| 15 | `lru_cache` 地址解析会被畸形输入污染 | **观察**:`parse_address` 对**非法地址也抛错**(不缓存异常结果),`is_modbus_address` 只判形态;缓存上限 `ADDRESS_CACHE_MAXSIZE` 有界,无内存隐患 |

---

## 四、P3:轻微项与文档口径

### P3-1 空表契约在读/写、批量/逐点之间不对称

实测:

| 入口 | 空入参行为 |
| --- | --- |
| `read_many([])` / `write_many([])` | 返回 `[]`(空列表) |
| `read_batch([])` / `write_batch([])` | `ValueError`(已由用例锁定:`test_modbus_clients.py:756`/`:1231`,注释注明"对齐 MC/FINS 契约") |
| `read_range(count=0/-1)` | `ValueError`(`require_count`) |

**判断**:**不是缺陷**——`read_many`/`write_many` 的空返回与基类逐点循环语义
自洽,`read_batch`/`write_batch` 的显式拒绝是"整批"契约的合理选择,且已锁测。
**唯一风险**是调用方把空返回当"成功",建议在 `read_many`/`write_many`
docstring 各加一句空表语义(现状未提),并把该不对称在
`docs/protocol-features.md` 的批量入口说明里点明。

### P3-2 `inter_frame_delay` / `broadcast_turnaround` 缺 `isfinite` 守卫(与 P1-1 同源但后果轻)

- `plc/modbus/modbus.py:1341`(只判 `< 0`)、`:1388`(只判 `< 0`);
- 后果:`nan`/`inf` 被接受并存入属性;`inf` 会让 RTU 每帧前 `sleep(inf)`
  → **永久阻塞**(实测 `time.sleep(inf)` 抛 OverflowError,即每次事务抛错);
- **与 P1-1 的区别**:不只进 `socket.settimeout`,故不泄漏 FD,但会造成
  "每次事务必失败"的确定性故障;
- 建议:与 P1-1 一并加 `math.isfinite` 守卫(同批修,同批测)。

### P3-3 文档未点明 `read_range` 与 `read_many` 的分页差异

`read_range` 超单笔上限**拒绝**(见 §三-8),`read_many` 自动合并/分页。
`docs/protocol-features.md` 的「连续批量读」行提到"单事务",但未提示
"超限拒绝、需要分页请改用 read_many"。现场按"批量=自动分片"的直觉调用会
撞 `ValueError`。建议补一句对照。

### P3-4 广播路径的 `expect_response` 判定在两处独立表达

- `write_mask_register:817-819` 与 `_write_pdu`/FC21 路径各自写
  `not (self._station == 0 and self._BROADCAST_WITHOUT_RESPONSE)`;
- RTU 客户端把该策略交给 `_BROADCAST_WITHOUT_RESPONSE = True`
  (`modbus.py:1315`),但基类与各写入口各判一次 → 新增写入口时容易漏判
  (漏判 = 广播后等不到响应,每次超时+重试);
- 建议:抽一个 `_expects_response()` 助手单点表达(纯重构,行为不变)。

### P3-5 `_check_address` 的 `units` 与 `read_range` 的 `width_bits × count` 是两套跨度算术

前者用 `data_type.register_size`(单点),后者手算 `width_bits * count`
(`modbus.py:343-352`);两处都在做"起始 + 数量 ≤ 65536"的同一断言。
属可接受的小重复,但建议抽 `_check_span(start, units, label)` 助手
(codec 侧已有同名 `_check_span:1207`,可复用命名口径)。

---

## 五、待人工裁决(不预设结论)

1. **P1-1 的修法边界**:是"仅在客户端属性期收口"(改动最小、下游影响最小),
   还是"客户端 + 传输层双层收口 + 传输层 finally 清理"(更彻底)?
   我倾向**双层**——因为传输层的 `except OSError` 只覆盖了一类异常,
   任何未来的非 OSError 配置异常都会重演泄漏,这是**结构性**而非本参数的问题。
2. **P3-1 的空表语义**是否要统一(把 `read_many([])` 也改抛 `ValueError`)?
   这是**破坏性**变更(现有调用方可能依赖空返回),须权衡;
   我倾向不改,只补文档。
3. **P3-2 的 `inf` 语义**:`inter_frame_delay = inf` 是"永不发帧"的合法表达,
   还是非法参数?若判定非法,与 P1-1 同步收紧。

---

## 六、排期建议

| 序 | 批次 | 内容 | 依据 | 量级 |
| --- | --- | --- | --- | --- |
| **1** | `fix(超时守卫 + FD 清理)` | P1-1:两个 setter 加 `isfinite`;`transport/base.py` 同名 setter 同步;`tcp.py`/`udp.py` 的配置失败清理改为不依赖异常类型;P3-2 同批;+ 用例(属性期拒绝 + 配置期收 FD) | §二、§四 P3-2 | 半天 |
| **2** | `docs(modbus 依据补全)` | §8.1:codec.py 全模块补页码级 `依据:`(§8.3 已把页码逐条核出,可直接照抄)+ 模块 docstring 的第三方链接换手册编号;`modbus.py:1014` 同批补页码 | §八 | 半天 |
| **3** | `fix(modbus codec 一致性)` | §8.2:7 个 `parse_*` 补 `check_response_exception`(异常响应不再误报坏帧);FC08 数据域支持 N 字;FC17 Server ID 标待核 + 登记;FC20/21 长度常量删或改推导;FC22 little 标待核;`build_rtu_frame` 补 ADU 上限与站号 247 收口 | §8.2 | 1 天 |
| **4** | `test(modbus 缺口)` | §9.1 五项:FC02 全链路(含手写 hex 断言 + 黄金样本)、翻页三守卫、native `station`/`word_order` 行为、FC20/21 上限、写位 1968 切片;§9.3 加 `test_samples_reproducible` 再生成门禁;§9.4 两处过弱断言补强 + 三份 FC03 夹具去重上提 | §九 | 1~1.5 天 |
| **5** | `chore(modbus 文档/小重构)` | P3-1 空表语义入 docstring;P3-3 `read_range` vs `read_many` 差异入 protocol-features;P3-4/§P3-5 抽助手 | §四 | 2 小时 |

**建议顺序说明**:第 1 批是**唯一实质缺陷**,优先;第 2 批**不需要查手册**
(页码已核出)、零行为风险,可与第 1 批并行;第 3 批含**行为变更**(异常响应
从"坏帧"改判"设备错误",会改变 `last_error_category`:PROTOCOL → DEVICE),
须按 CONTRIBUTING §六在 CHANGELOG 披露。

**门禁要求**(修复批):五件套全绿(3.7.9 + 3.12 双腿)。

---

## 七、正面确认(本轮验证过的强度)

- **codec 健壮性**:4008 组随机/截断/畸形字节喂入 27 个入场函数,
  **零非契约异常**——这层"坏帧一律转内部异常、不裸抛"的纪律是扎实的;
- **入参守卫密度**:区域×类型、跨度、只读区、单笔上限、位号语义
  全部在**进事务锁之前**拒绝(零字节发送),与用户约定一致;
- **合并算法**:9 组对抗验算证明"组内宽度统一 → `chunk[-1]` 即最大端"
  这一隐含不变量成立,且跨度恒 ≤ 单笔上限;**性能优化(review-1019 P1-1
  的 O(n²)→O(n) 指针切片)未引入正确性回退**(注释里点名的不变量
  "chunk 恒为 group 连续切片"经实测成立);
- **事务层校验完整**:MBAP 事务号 + 站号 + 协议标识 + 长度域上限;
  RTU 站号 + CRC + ADU 上限 + 变长增量收包;
- **边界精确**:位 2000 / 字 125 / FLOAT 62 三处顶格全部精确。

---

## 八、codec 帧面切片(对三份规范 PDF 逐条核)

> 子代理**亲自用 pdfplumber 抽取** `docs/protocol/modbus/` 三份 PDF 全文
> (产物落 `$env:TEMP`,未入库),并**实锚定页码口径**——三份 PDF 的 PDF 页
> = 自印页(页脚分别 `N/50`、`N/46`、`N/44`,各抽样 5 处页脚直证)。
> 下文页码一律为**自印页**。主会话对下方 P1-1 与 P2-1/2/3/5 逐条**亲验**
> (含行为实证)。

### 8.1 P1:全模块零页码级 `依据:` 注释(纪律红线)

- **实测**(亲验):`rg '依据:' src/omniplc/plc/modbus/codec.py` → **0 命中**;
  唯一含"依据"的是模块 docstring `codec.py:6`「帧布局依据《Modbus 通信协议规范》」,
  **无文档编号、无页码、无章节**,且指向**第三方镜像**
  `https://www.modbus.cn/modbus-specifications`(`:8`)而非
  `docs/protocol/modbus/` 的归档件。
- **对照**:`modbus.py:1014` 已有 `依据:Modbus 应用协议 V1.1b3 §6.8` 形式
  (同缺页码);即**同协议两个文件都未达铁律要求**。
- **反差点**:帧面本身**逐条核对全部正确**(见 9.3)——1219 行代码实现是对的,
  **缺的是可回溯性**。而这正是本项目区别于"随便一个 Modbus 库"的立身依据。
- **严重度 P1**(违反 AGENTS.md「协议依据铁律」/ CONTRIBUTING 二·首条)。
- **修法**:逐函数补 `依据:Modbus 应用协议 V1.1b3 p.<页> §<节>(<表/字段>)`;
  MBAP 补 `TCP 实施指南 V1.0b p.5 §3.1.3`;RTU 补 `串口链路 V1.02 p.13 §2.5.1`;
  模块 docstring 的第三方链接换成手册编号。**§8.3 已把页码逐条核出,可直接照抄**。

### 8.2 确认问题(帧面/实现层)

| # | 现象 | 证据 | 亲验 | 严重度 |
| --- | --- | --- | --- | --- |
| 1 | **7 个 `parse_*` 未调 `check_response_exception`** → 异常响应(0x80\|FC)被误报为**坏帧**而非设备错误。已调的 6 个:`:251`/`:282`/`:328`/`:397`/`:494`/`:1160`;未调:`parse_read_file_record_response:1049`、`parse_write_file_record_response:1136`、`parse_diagnostics_response:839`、`parse_read_exception_status_response:863`、`parse_report_server_id_response:880`、`parse_comm_event_counter_pdu:908`、`parse_comm_event_log_pdu:929` | 实证:7 个解析器**全部**返回 `ProtocolFrameError`,而同批对照 `parse_read_response(83 02, FC03)` 正确返回 `DeviceError(code=2)` | ✔ 实跑确认 | P2 |
| 2 | `MODBUS_MAX_FILE_RECORD_LENGTH = 0x7D` **无出处且不可达**(「看似连续」推断痕迹) | `constants.py:163` 注"规范 0x7D = 125 个寄存器";而 §6.14 p.32-33 / §6.15 p.34-35 对 Record Length **只写 `2 Bytes N`,无上下限**;0x7D 全篇只出现在 FC03/04/23 语境(p.15/16/38) | ✔ 实测:FC20 121 过/122 拒(聚合 247 字节上限先触发),FC21 122 过/123 拒(0xFB 先触发)→ **123~125 三值在任何路径都不可达** | P2 |
| 3 | FC08 诊断**数据域硬编码 1 个字**(`MODBUS_DIAGNOSTICS_PDU_SIZE = 5`,校验 `len(pdu) != 5`) | 规范 §6.8 p.21-22:请求/响应均为 **Data N × 2 Bytes**;§6.8.1 p.22 子功能 00 要求"应答与请求逐字节相同" | 子代理实测 `parse_diagnostics_response(08 00 00 A5 37 12 34, 0)` → `ProtocolFrameError`(N=2 的合法回显被拒) | P2 |
| 4 | FC17「Server ID」长度**硬编码 1 字节** | `codec.py:900` `return pdu[2], pdu[3], pdu[4:]`;规范 §6.13 p.31 该栏为 **device specific**,未定长度 | ✔ 实测:`11 02 AB FF` 通过;**`11 03 41 42 43 FF`(Server ID 2 字节)被拒** → 该形态设备无法解析 | P2(须标待核 + 登记) |
| 5 | FC22 掩码写的 `byte_order="little"` **无出处** | `codec.py:307-308`/`:317-324`;规范 §6.16 p.36-37 两掩码均 2 Bytes,§4.2 p.5 明文"MODBUS uses a **big-Endian** representation",无机型例外;`docs/protocol/` 未收录施耐德 Modbus 手册 | 审读(未展开) | P2(臆造风险) |
| 6 | `build_rtu_frame` **无 ADU 上限守卫** | `codec.py:692-705` 只校验站号与 PDU 非空;`MODBUS_RTU_MAX_ADU_SIZE` 在该文件既未 import 也未使用;对照 `build_mbap:624` 有 253 守卫 | 子代理实测构造出 **257 字节**帧不报错,违 §2.5.1 p.13「maximum size is 256 bytes」 | P3(库内构造器最大 PDU=253,仅外部直接调用可触发) |
| 7 | `build_rtu_frame` 站号允许 **248~255**(规范为 Reserved) | `codec.py:700` `0 <= station <= 0xFF`;串口链路 §2.2 p.8:0=广播、1~247=从站、248~255=Reserved | 子代理实测 248 通过;线上不受影响(`modbus.py:110-118` 用 247 收口) | P3 |
| 8 | `parse_mbap` **静默忽略长度域外多余字节** | `codec.py:677-684`;子代理实测 `parse_mbap("0001000000040103021234")` 返回 PDU `03 02 12`,尾字节 `34` 被丢弃 | 审读 | P3(公共 API 有"静默接受坏帧"风险;TCP 走线按长度精确收包,不受影响) |
| 9 | `MODBUS_SERVER_ID_MIN_BYTE_COUNT=2` 系推导值未标「推导」;模块 docstring(`:612`)称 MBAP 用于「Modbus **TCP/UDP**」,而全库无 Modbus UDP 客户端 | 见行号 | 审读 | P3 |

### 8.3 经核无问题(帧面正确性 = 本轮最强的正面结论)

子代理**逐功能码把规范算例回灌进实现**,主会话抽查未见反例:

- **规范算例逐字节全通**:FC01 `0103CD6B05`→19 位(LSB 起)、FC02 `0203ACDB35`、
  FC03 `0306022B00000064`、FC04 `0402000A`、FC05 构造 `0500ACFF00`、
  FC06 `0600010003`、FC07 `076D`→109、FC08 `080000A537`、FC11 `0BFFFF0108`、
  FC12 `0C080000010801212000`、FC15 构造 `0F0013000A02CD01`(**与 p.29 算例逐字节相同**)、
  FC16 `100001000204000A0102`、FC17 `1102ABFF`、FC20 构造
  `140E0600040001000206000300090002`(**与 p.33 逐字节相同**)、
  FC21 构造 `150D0600040007000306AF04BE100D`(**与 p.35 逐字节相同**)、
  FC22 `16000400F20025`、FC23 构造+解析 **与 p.38-39 一致**、FC24 `180006000201B81284`、
  FC43 流式/翻页/个体/私有全通;
- **CRC16 双向坐实**:`crc16(02 07)=0x1241` 与串口链路 **p.41** 逐位移位算例末态
  `0001 0010 0100 0001` 完全一致;`crc16("123456789")=0x4B37` = CRC-16/MODBUS 标准值;
  初值 FFFF、POLY 0xA001(p.39-40)、低字节在前(p.13/14)、CRC 范围=站号+PDU 全对;
- **MBAP**:`build_mbap(0x1234,0xFF,FC03)` → `1234 0000 0006 FF …`,
  长度 6 = Unit ID(1)+PDU(5),与 TCP 指南 p.5「including the Unit Identifier」
  **逐字对应**;协议号≠0 / length<2 / length>254 三种坏头实测全拒;
- **边界常量逐条对表全中**:2000(p.11)/125(p.15)/1968(p.29)/123(p.30)/
  121 与 125(p.38)/FIFO 31(p.40)/FC20 byte count 0x07~0xF5(p.32)/
  FC21 0x09~0xFB(p.34)/引用类型 6/访问码 1~4/对象 0x00~0x06/保留 0x07~0x7F/
  私有 0x80~0xFF/MEI 0x0E/Conformity 值域(p.43-44);**FC20 子请求 35 条
  (= 0xF5//7)推导正确**,36 条拒;
- **异常码表**(`constants.py:201-211`)= §7 p.48-49 逐条一致,
  **正确跳过 07/09/0C**,无补号痕迹(与本报告 §一 的"不得推断"口径吻合);
- **坏帧拒绝全覆盖**:字节数不符/截断/多余字节/引用类型非 6/对象数不符,
  全部 `ProtocolFrameError`;异常响应识别(`:203-248`)对 `81 02` 给
  `DeviceError`、对错配 FC 给坏帧、对空 PDU/缺异常码给坏帧。

**结论**:Modbus 帧面**实现质量高、与规范逐字节吻合**,本轮**未发现任何
静默错值 / 假成功**;问题全部集中在**可回溯性**(P1-1)与**若干未标依据的
自造常量/假设**(P2 批)。

---

## 九、测试覆盖切片

> 子代理**纠正了本报告 §一 的一处数字**:黄金样本实测 **18 个**(非 20);
> `test_modbus_clients.py` 的 127 个 `def test_` **全部无参数化**;
> `test_native_edge_parity.py` 的 modbus 族 = shared 6 + 4 = **10 条**(26 是三协议合计)。
> 主会话已复核前两项(目录枚举 18、`ndjson` 计数一致),**§一 的"20 个黄金帧样本"应订正为 18**。
> 下方 P1-1/P1-2 两条由主会话**亲验**(见各条"实证"栏)。

### 9.1 P1 缺口(按优先级)

| # | 缺口 | 证据 | 亲验 | 为何重要 |
| --- | --- | --- | --- | --- |
| 1 | **FC02(离散输入 `di`)全链路零用例**——任何层级都没有 `di` 的**成功读** | `test_modbus_clients.py` 内 `di` 仅 5 处且**全是拒绝路径**(`:405`/`:2139`/`:2145`/`:2214` + `test_address.py:18/92`);`build_read_pdu(2,…)`、`parse_read_response(…, FC2, …)`、`expected_response_length` 的 FC02 分支从未执行;18 个黄金样本**无 FC02** | ✔ `rg` 确认无成功读 | FC02 与 FC01 共用全部代码路径(位打包/字节数/上限),**只差功能码常量**——正是"看似等价、复制粘贴改错"最易漏测的形态 |
| 2 | **设备标识翻页三守卫零用例** | `modbus.py:1150`(指针非法→`DeviceError`)、`:1174`(翻页不收敛→`ProtocolFrameError`)、`:1179`(超 `MODBUS_DEVICE_ID_MAX_PAGES` →`ProtocolFrameError`);native `:1082`/`:1085`/`:1058` 同 | ✔ `rg '翻页不收敛\|翻页次数超过上限\|翻页指针非法' tests` → **零命中** | 这是**防恶意/故障设备把客户端钉死**的唯一循环上界(MoreFollows 恒 FF 的设备会让 `while` 永不退出);三守卫全无回归锁定 |
| 3 | **native Unit ID(`station`)与 `word_order` 零用例** | `native/modbus.py:126`(station)/`:132`/`:137`(word_order);同步侧有对等用例(`test_modbus_clients.py:44` 0xFF 放行 / 300 拒,`:539`/`:883` CDAB) | 审读 | **正是 review-1001 R9-1 / review-1002 修过的漂移类型**(`native/modbus.py:108-110` 注释自陈);方法集守卫只锁"方法存在",锁不住**行为** |
| 4 | FC20/21 的 **35 子请求 / 0xF5 / 0xFB 上限**零用例 | `codec.py:995`/`:1011`/`:1020`/`:1061`/`:1127`;`rg '35'` 零命中 | 审读 | 与本报告 §8.2-2(该上限**无规范出处**)叠加:既无依据又无回归,是最脆弱的一处 |
| 5 | **写位 1968 上限从未触发切片** | `rg '1968'` 全库零命中;写字 123 有强用例(`:1686`)而写位缺;`_coalesce_group` 的写位分支(`modbus.py:671`,max_unit=1968)正是 **review-1019 P1-1 优化**的同款逻辑 | 审读 | 优化过的分支里,读侧/写字侧都有用例,**只有写位侧没有** |

### 9.2 P2(次级缺口)

- **MBAP 站号不匹配(TCP)零用例**(`modbus.py:1292`、native `:1156`);
  RTU 站号不匹配**有**用例(`:227`)——两侧不对称;
- `read_many([])`/`write_many([])` 返回 `[]` 而 `read_batch([])`/`write_batch([])`
  抛 `ValueError` 且**有用例**(`:756`/`:1231`)→ 见本报告 §四 P3-1:
  **四个批量 API 契约不对称,两个 `_many` 侧零用例**;
- `build_mbap` PDU 超长(`codec.py:624-626`)与事务号越界零用例;
- RTU 走线上 FC07/FC11/FC20/FC21(带响应)**无成功往返**,FC12 仅"长度封顶拒绝"
  (`:2283`)→ `_transact` 的增量收包/推算分支对这些 FC 只被 TCP 用例绕开;
- 客户端级合法上限(125·121、FC24 的 31、`read_range` 位区 2000)**无端到端用例**。

**功能码覆盖**:黄金帧**零样本 10 个**(FC02/07/08/0B/0C/11/14/15/16/18);
客户端级**零往返 1 个**(FC02);native 对拍表 `_EXT_CASES` **覆盖全部 11 个扩展功能码** ✓。

### 9.3 同源偏差与门禁缺口

- **生成器独立性达标**:`tests/golden/generate_modbus_samples.py:12-20` 只 import
  `json/struct/pathlib/typing`,`crc16` 自写,全文**无 `omniplc`** ✓
  —— 符合 `tests/golden/README.md` 的"独立实现"约定;
- **但缺"再生成门禁"**:`rg generate_modbus_samples tests/` 只命中
  `test_golden_modbus.py:4` 的 docstring → **手改 JSON 无任何测试能发现**
  (README:44 反而鼓励手工添加样本)。建议加 `test_samples_reproducible`
  (用 `runpy` 重跑生成器并逐字节比对);
- **客户端层 23 处同源断言**:期望值写成 `sent == codec.build_*`(由被测 codec
  自产),只能证明"客户端调了 codec"而非"帧对"(`codec.build_mbap` 出现 47 次、
  `build_rtu_frame` 18 次);未被打手写向量的客户端帧:FC02/07/08/0B/0C/11/14/15/18。
  **好范例**:`:1877` 用 `bytes.fromhex` 手写期望;
- `test_golden_modbus.py:186-188` 的 FC23 解码期望取自**同源 JSON** 的
  `expect.values`,建议改手写字面量。

### 9.4 已量化的测试质量问题

| 项 | 实测 |
| --- | --- |
| `[:7]` 切片样板 | 64 次;`_ScriptedTransport` 92 次;`setattr(_create_transport)` 87 次;函数内 `import asyncio` 11 次 |
| **逐字重复的夹具** | `_fc03_response:566` 与 `_fc03_read_response:1034` **实现逐字相同**;FC03 夹具第三份在 `test_native_modbus.py:37` |
| 断言过弱(1) | `test_native_modbus.py:1-6` docstring 声称对拍 `last_error`,但 `_snapshot:417-428` **只记 `has_error_text` 布尔**,错误文本不参与对拍 |
| 断言过弱(2) | `ExtCase:657-670` **无 expect 字段**,`test_sync_async_parity_extended:892-907` 只比"两层相等 + 功能码序列"→ **两层共同回归会全绿**(对照 `:506-511` 有显式期望核对) |
| 硬编码 sleep | `test_native_modbus.py:589`(0.05s)、`:614`(0.2s)、`:626`/`:628`;耗时断言 `:1107 elapsed < delay*3` 易假红。**正确做法见 `:2076`**(monkeypatch 替换 `time.sleep`) |
| `skip`/`xfail` | **零命中**(四个目标文件 + edge_parity);`tests/` 与 `tests/unit/` 均**无 `conftest.py`** |

### 9.5 已锁死、无需重复投入(该切片正面结论)

- **native 公开面镜像**已有结构性守卫:`test_native_surface.py:82-89` 断言
  `_public(ModbusTcpClient) - _public(AsyncModbusTcpClient) == {"create_monitor"}`,
  `:193-228` 再锁签名 → **方法集/签名无需重复投入**,真正缺的是 `station`/`word_order`
  的**行为**用例与 6 类未入 `edge_parity` 的边界入参(mask byte_order 非法 /
  `read_device_id` level 非法 / FC20/21 字段越界等);
- `modbus.py` 的 **21 个公开方法全部至少 1 个直接用例**;
- 另有 18 条契约已锁(MBAP/RTU 逐字节、坏帧文本带原始 hex、DEVICE vs PROTOCOL
  断线口径、超时分流、批量切片不变量 `:1607`/`:1636`/`:1659`/`:1686`/`:2335`、
  写侧三件套、只读区收口、广播语义、扩展 FC 同步 TCP 全绿、RTU 增量收包与封顶、
  地址语法全边界、native 生命周期与并发)。

### 9.6 该切片未核实

`parse_read_file_record_response` 的 0xF5 上限是否有直接断言;
`configure_serial` 参数组合校验;`is_modbus_address` 在汇川侧的间接断言强度;
规范是否要求校验 `conformity_level` 取值域。

---

## 十、实施批注(2026-10-10 修复批,主会话落地)

> 修复批 `ac7af9d`(§六第 1 批)/ `63905d4`(第 2~3 批)/ 第 4 批测试
> `test_review_1023_gaps.py`(随第 5 批提交);与 review-1022 修复批同日,
> 门禁 3.7.9 全量 1978 passed。逐项状态:

| 项 | 状态 | 落点 |
| --- | --- | --- |
| P1-1(§二)超时 NaN/inf FD 泄漏 | ✅ 已修(`ac7af9d`) | 双层:客户端/传输 setter isfinite 收口(sync+native 四处 + S7 两处覆写)+ tcp/udp/native-transport connect 清理改 `except BaseException`;P3-2 inter_frame_delay/broadcast_turnaround 同批 |
| P1-1(§8.1)codec 零页码依据 | ✅ 已修(`63905d4`) | 模块 docstring 三手册章节页码总表(印刷页=PDF 页,页脚 N/50·N/46·N/44 抽样直证)+ 32 函数就近标注;第三方链接换手册编号;modbus.py ping_probe 页码同批 |
| §8.2-1 七解析器异常分流 | ✅ 已修(`63905d4`) | FC07/08/11/12/17/20/21 补 `check_response_exception`;**行为变更**:异常响应 PROTOCOL → DEVICE,随下批发版 CHANGELOG 披露 |
| §8.2-2 0x7D 常量 | ✅ 已修(`63905d4`) | 删 `MODBUS_MAX_FILE_RECORD_LENGTH` 与字段闸(FC03/04/23 语境误挪);聚合 byte count(0xF5/0xFB)收口;边界用例 121/122、122/123 顶格锁定 |
| §8.2-3 FC08 数据域 N 字 | ✅ 已修(`63905d4`) | parse 按 §6.8(Data N×2)接受整字数并返回全部数据字;客户端按"请求恒单字"校验字数取首字——N=2 回显不再误报坏帧,单字路径零破坏 |
| §8.2-4 FC17 Server ID | ✅ 标待核 + 登记(`63905d4`) | 代码 docstring 待核口径 + 真机清单「review-1023 修复批随批核证两项」① |
| §8.2-5 FC22 little | ✅ 标待核 + 登记(`63905d4`) | 代码 docstring 待核 + 真机清单同上② |
| §8.2-6/7 RTU 守卫 | ✅ 已修(`63905d4`) | build_rtu_frame 站号 0~247(§2.2 印刷页 8)+ ADU ≤ 256(§2.5.1 印刷页 13) |
| §8.2-8 parse_mbap 尾部静默 | ⏳ 未动(TCP 走线按长度精确收包不受影响,公共函数收口属行为变更) | 登记 todo |
| §8.2-9 推导标注/TCP-UDP 措辞 | ✅ 已修(`63905d4`) | build_mbap 措辞订正(全库无 Modbus UDP 客户端);`MODBUS_SERVER_ID_MIN_BYTE_COUNT` 推导标注随 FC17 待核批处理 |
| §9.1 五项测试缺口 | ✅ 已补(第 5 批) | FC02 全链路三连 / 翻页三守卫 / native station+word_order / 1968 切片 / FC20 35 条边界 |
| §9.3 再生成门禁 | ✅ 已补(第 5 批) | runpy 重跑独立生成器逐字节比对;23 处同源断言与夹具去重**未动**(重构类,登记 todo) |
| §9.4 弱断言补强 | ⏳ 未动(对拍框架改造) | 登记 todo |
| P3-1/3/4/5 | ✅ 部分 | 空表语义入 docstring(P3-1 文档半)、read_range 分页差异入 protocol-features(P3-3)、_expects_response/_check_span 抽助手(P3-4/5)未动登记 todo |

**§五待人工裁决的采择**:P1-1 修法采**双层**(报告倾向:传输层 `except BaseException`
属结构性收口);P3-2 inf 语义采**收紧**(sleep(inf) 每事务必失败,"永不发帧"
无合法表达);P3-1 空表语义**维持现状只补文档**(报告倾向,破坏性变更不做)。
