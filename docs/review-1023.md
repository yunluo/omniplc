# review-1023 Modbus 专项审查报告(只差不做)

> 2026-10-10 · 基线:master `1fd1a3f`(review-1022 台账后,v0.56.0 未发版)
> 范围:`src/omniplc/plc/modbus/`(modbus.py 2004 / codec.py 1219 /
> address.py 189 / `__init__.py` 13 = **3425 行**)+ native 镜像
> `native/modbus.py`(1163 行)+ 测试面(test_modbus_clients 127 例 /
> test_modbus_codec 22 / test_golden_modbus 13 / test_native_modbus 17 /
> 20 个黄金帧样本)。
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
| 帧面 codec | **穷尽喂入零非契约异常**(2432 + 1576 组);长度域/协议标识/回显/字节计数**逐项拒绝** | codec 切片待回报(§八) |
| 地址与入参守卫 | 区域×类型、跨度、只读区、单笔上限**全部入参期零发送拒绝** | — |
| 合并分块算法 | 9 组对抗验算**不变量全部成立**(含倒序输入/顶格/空洞密集) | — |
| 客户端契约 | 读/写/批量语义与文档一致 | P3 若干 |
| **超时属性守卫** | **`connect_timeout`/`receive_timeout` 接受 NaN/inf → 传输层抛非 OSError → FD 泄漏** | **P1-1** |
| 测试面 | 边界锁得很密,但 NaN/inf 零覆盖 | 测试切片待回报(§八) |

**P1-1 是本轮唯一实质缺陷**,且**不在 Modbus 内部**——它是
`core/base_client.py` + `transport/*` 的守卫缺口,经 Modbus 走线实测暴露,
**影响全部客户端**(所有客户端共用这两个属性)。之所以放在本报告,
是因为它是本轮审查中唯一被实证的失败通道,且 Modbus 是其最容易触发的驱动。

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
| **2** | `test(modbus 补齐)` | 依测试切片结论补用例(待 §八 回报) | §八 | 待定 |
| **3** | `chore(modbus 文档/小重构)` | P3-1 空表语义入 docstring;P3-3 `read_range` vs `read_many` 差异入 protocol-features;P3-4/§P3-5 抽助手 | §四 | 2 小时 |

**门禁要求**(修复批):五件套全绿(3.7.9 + 3.12 双腿);
P1-1 属行为收紧,CHANGELOG 须按「行为变更提示」披露。

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

## 八、待补切片(codec 帧面对规范 / 测试覆盖矩阵)

> 本报告基线时两路只读子代理**尚未回报**:
> - **codec 帧面切片**:`plc/modbus/codec.py` 1219 行对
>   `docs/protocol/modbus/` 三份规范(V1.1b3 应用协议 / TCP 实施指南 V1.0b /
>   串口链路 V1.02)的**逐功能码逐字段**核对,含注释页码与手册实核页码的一致性;
> - **测试覆盖切片**:`plc/modbus/*` 与 `native/modbus.py` 的覆盖矩阵、
>   黄金帧缺口、边界用例盘点、测试质量反模式、生成器同源偏差风险。
>
> 两切片到位后按本报告体例追加为 §九 / §十,并同步更新 §〇 摘要与 §六 排期。
> **覆盖边界声明**:在 §九 到位前,本报告对**帧面字段级合规性**不作结论
> (仅给出"拒绝行为正确"这一层实证结论);§二 P1-1 与 §三/§四 的结论
> **不依赖**两切片,已可独立成立。

---

> **登记与排期**:本报告不落地任何修改;§六 第 1 批(超时守卫 + FD 清理)
> 建议随本轮审查确认后登记 `docs/todo.md`。修复批完成后按维护惯例回写状态,
> 并将本文件按"存活至发版后删除"的惯例处理。
> **真机核验**:本报告结论为**库内静态 + 本地动态**结论(镜像往返、对抗响应、
> 异常面喂入、真实回环 socket),未上真机;P1-1 的修复不改变任何协议帧面,
> 无需真机核证。
