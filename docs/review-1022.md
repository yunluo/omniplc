# review-1022 core 模块专项审查报告(只差不做)

> 2026-10-10 · 基线:master `2d155ff`(review-1021 复核批后,v0.56.0 未发版)
> 范围:**仅 `src/omniplc/core/`**(11 文件 / 6843 行:base_client 1729 /
> constants 1425 / i18n 1411 / monitor 815 / convert 582 / debug 300 /
> tag 199 / types 156 / errors 109 / validation 84 / `__init__` 33)。
> **本轮只检查与差距分析,不落地任何修改**;修复排期见 `docs/todo.md`。
>
> 方法:主会话逐文件精读核心逻辑面(base_client / monitor / convert / errors /
> types / validation / tag / debug 全读关键段)+ 两路只读子代理分区并行
> (i18n 取词表、constants 常量与码表)+ **动态实证**(在 `.venv` 3.7.9 上
> 以最小桩驱动复现,已知向量校验数值层)。
> **亲验** = 主会话读原文/实跑确认;**审读** = 子代理静态阅读结论,主会话
> 抽查未见反例;**未核实** = 单源结论或未展开验证。
>
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019/1021 同款)。
> P0/P1 按"现场后果 × 触发概率"判定,不按修复难度判定。

## 〇、执行摘要

core 是**唯一被全部 18 族协议共享的地基**(全库 88 个源文件里 80 个直接
`from ..core import …`)。本轮结论:

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| 数值/编解码层 | **零缺陷**;CRC16/LRC/位操作/字序往返全部实测正确 | — |
| 事务与错误契约 | 分类表、可达范围、`code=0→None`、退避溢出、心跳豁免**均与文档一致** | P3 若干 |
| 批量语义面 | **两处 `zip()` 静默截断 → 假成功**(实证复现) | P1-1、P1-2 |
| 状态暴露面 | `incident_frames` 返回内部 list(与 `recorded_frames` 惯例相反) | P2-1 |
| **i18n 取词表**(1313 键) | **3 张码表 68 条文案未收录 → en 模式静默回中文**(实测) | **P1-3** |
| **constants 常量面**(326 项) | **44.2% 协议常量无手册依据**;1E 写侧缺字上限闸门 | P2 × 7 |
| 文档与内聚 | Monitor 死区种子值两分支不一致(当前不可观测) | P3 |
| 测试面 | core 无专门覆盖的模块已收窄到 `validation.py` 一处 | P3 |

**本轮没有发现 P0**。最值得修的两条:**P1-1「一致性批量读会在驱动少返条目时
假成功」**(恰落在库自己的 P0 定义"假成功"上,但触发条件是**驱动侧契约违背**,
内置驱动全部合规,故定 P1)与 **P1-3「en 模式在 EZSocket / FOCAS / S7 三族
报错上静默回中文」**(切英文给外方看日志这一明确功能静默失效,而文档承诺
"码表全覆盖翻译")。

---

## 一、审查范围与清点口径

| 项 | 实测值 | 口径 |
| --- | --- | --- |
| core 规模 | 11 文件 / 6843 行 | 占全库源码(约 3.8 万行)约 18% |
| 公开导出面 | `core.__all__` 10 名(基类/统计/校验/2 默认值/4 异常类/分类枚举) | 见 `core/__init__.py` |
| core 复杂度热点 | **6 处**(`_execute` C901=13;`Monitor.__init__` C901=14/分支 14;`Monitor._cycle` C901=13/分支 14/语句 58) | `ruff --select C901,PLR0912,PLR0915 src/omniplc/core` |
| core lint / 格式 | **零告警、零漂移** | `ruff check` / `ruff format --check`(全库 148 文件) |
| 门禁基线 | 3.7.9 **1921 passed / 11 skipped**;mypy(3.12)83 文件 / ty 全绿 | 本轮实测(基线未改动) |
| core 测试覆盖 | 11 个专门测试文件、共 **296 例**(base_client 38 / v030 33 / v034 28 / monitor 41 / convert 43 / tag 13 / debug 19 / i18n 16 / write_safety 28 / native_core 31 / package_exports 6) | 仅 `validation.py` **无专门用例文件** |
| 未实现残留 | **零**(core 内无 `NotImplementedError`/TODO/FIXME/降级桩) | `rg` 全目录 |

**动态实证清单**(本轮新增,均在 3.7.9 门禁环境实跑,只读不落盘):
- `crc16(b"123456789") == 0x4B37`(Modbus 规范已知向量)✔
- `lrc(01 03 00 00 00 0A) == 0xF2`(Modbus ASCII 规范例)✔
- `to_signed(0xFFFF,16) == -1`、`set_bit/get_bit` 位操作 ✔
- **4 字序 × {LE,BE} × {reverse_words 开/关} × 8 数值类型往返全一致** ✔
- `read_many_strict` 少返条目场景 → 复现假成功(见 §二 P1-1)

---

## 二、P1:两处 `zip()` 静默截断 → 假成功 / 静默降质

### P1-1 `read_many_strict` 在驱动少返条目时返回 `(True, 短列表)`(亲验·已实证)

- **位置**:`core/base_client.py:790`(函数体 `:807-813`);native 同款
  `native/base.py:682`;aio 镜像 `aio/__init__.py:413`。
- **实现**:

  ```python
  pairs = self.read_many(addresses, data_type)
  values: List[PrimitiveValue] = []
  for _address, (ok, value) in zip(addresses, pairs):   # ← 短则截断
      if not ok or value is None:
          return False, None
      values.append(value)
  return True, values
  ```

- **实证**(最小桩驱动,`read_many` 对 3 个地址只返 2 条):

  ```
  请求地址数: 3
  read_many 实返: 2
  read_many_strict -> (True, [100, 101])
  → ok 为 True 但只交付 2 个值 = 假成功
  ```

- **影响**:该 API 的**全部存在理由**是"整批成功才交付,对接 MES/SCADA 快照
  语义"(docstring 原文)。少返时它既不失败也不报错,交付一个**静默缺项的
  快照**——上位机按 `len(values) == len(addresses)` 断言才会发现,而按
  `ok` 分流的代码会把缺项当"该点就是这个值"。
- **触发条件(定级依据)**:要求驱动违背"与地址等长"契约。**内置驱动全部
  合规**——Modbus/AB/FINS 三处覆写统一走 `_pack_read_results`
  (`plc/modbus/modbus.py:250`、`plc/ab/ab.py:709`、`plc/omron/omron.py:482`),
  该助手在 `values is None` 时按 `addresses` 长度补 `(False, None)`,
  等长由构造保证。故风险面 = **自定义/第三方驱动**(仓库明确支持并鼓励
  扩展:AGENTS/CONTRIBUTING 的"新增驱动"流程),以及未来某次覆写失误。
- **定级 P1**:属"假成功",但需驱动侧契约违背才触发 → 按"现场后果 ×
  触发概率"落 P1(库自身 P0 定义覆盖"假成功",故已在本报告显式说明为何非 P0)。
- **修法建议**:入口加**形状守卫**——`pairs` 长度与 `addresses` 不等即
  `raise ProtocolFrameError`(或返回 `(False, None)` 并写 `last_error`),
  理由:驱动返回形状错误是**库内缺陷信号**,静默截断掩盖它;参考
  `Monitor._cycle` 的组级兜底已有"参数类错误不死线程"的处理范式。
- **附**:`read_many_strict` 自身的**契约声明缺失**——docstring 未写
  `:raises`,而它实际会对非线性参数抛 `ValueError`(如 STRING 批量)。

### P1-2 `Monitor._cycle` 的 `zip()` 同款截断 → 静默降质(亲验)

- **位置**:`core/monitor.py:624`(`for point, pair in zip(group, pairs)`)。
- **影响**:组内少返时,截断掉的点位在 `results` 中缺席,`_cycle` 的
  快照循环按 `results.get(tag_id, (False, None))` 把它们当**读失败**处理
  → 快照降质为 `STALE` 且保留旧值。**数据"看起来正常但不再更新"**是现场
  最难查的形态之一;且 `any_ok` 仍可能为 True(其余点成功),整周期不计失败,
  `on_disconnect` 不触发、`stats["fail_count"]` 不动。
- **与 P1-1 同源**:都是"驱动返回形状"未校验。建议同一处收口。
- **旁证**:`_is_good_read` 与快照循环的谓词被有意写成"助手 + 内联"两份
  (`monitor.py:687-695`,注释自陈"互锚防漂移"),说明该模块对谓词漂移有
  意识,但**形状漂移未被同样对待**。

---

## 三、P2:状态暴露口径不一致

### P2-1 `incident_frames` 返回内部 list 本体(可被调用方改写)

- **位置**:`core/base_client.py:1202-1211`:`return self._incident_frames`
  ——**无拷贝**;对照 `debug.recorded_frames()`(`core/debug.py:131-141`)
  明确 `return list(_recorder)` 做快照拷贝,且 docstring 写明"返回后黑匣子
  继续滚动,不影响已取快照"。
- **影响**:①调用方 `client.incident_frames.clear()` / `.append(...)` 会**直接
  改写库内事故现场**(取证数据是排障的最后依据,应不可变);②该属性在
  `_execute` 的失败路径(可能来自心跳以外的任意线程)被整体替换
  (`self._incident_frames = records`),而读取方无锁——CPython 下引用替换
  原子,但"读到列表后它被替换"与"调用方原地改它"叠加会产生不可复现的取证结果。
- **一致性依据**:同一模块已经把"快照返回拷贝"作为惯例
  (`stats` 拷 dict、`get_all()` 拷 dict、`recorded_frames()` 拷 list);
  `FrameRecord` 本身是不可变 `NamedTuple`,说明设计意图就是"不可变快照"。
- **修法建议**:`return list(self._incident_frames)`(一行),并在 docstring
  写明"返回快照拷贝"。

---

## 四、P3:轻微项与文档漂移

### P3-1 `Monitor` 死区种子值两分支不一致(`0.0` vs `1.0`)

- **位置**:`core/monitor.py:299-301`(TagTable 路径 `deadband=0.0`)对
  `:303-311`(Mapping 路径 `deadband=1.0`)。
- **当前不可观测**:两分支随后都调用 `self._apply_deadband(deadband)`
  (`:312`),而该函数对**数值形态必然全点回填**(`:407-417`),
  对**映射形态只回填列出的点**(`:424-439`)——构造函数的默认值是
  `deadband=0.0`(`:258`),故未列出的点最终都是 `0.0`,种子的 `1.0`
  被覆盖。**实测确认**:全库无任何调用点能在 `_apply_deadband` 之后看到 `1.0`。
- **为何仍要修**:①`1.0` 是**有语义的值**(死区 1.0 会压掉 <1.0 的变化),
  与"默认关闭"口径直接冲突,属可疑残留;②潜在风险——若将来把
  `_apply_deadband` 改为"仅在显式传参时调用"(一次很自然的重构),
  Mapping 形态会**静默获得 1.0 死区**,现场表现为"小变化不报事件"而无人知道
  原因;③两分支同义参数不同值,读代码者需反推哪个才对。
- **修法建议**:统一为 `0.0`,或把该形参从 `_add_point` 移除(死区一律由
  `_apply_deadband` 决定),后者更彻底。

### P3-2 `write_and_verify` 回读不符归 `ErrorCategory.UNKNOWN`

- **位置**:`core/base_client.py:1259-1267`(`_set_error(..., UNKNOWN, None)`)。
- **影响**:回读不符是**明确的数据安全问题**(写进去了但读回不是那个值),
  与 `read_string` 类型错、OPC-UA 用户回调异常共用一个 `UNKNOWN` 桶
  (`docs/architecture.md` §5 表亦未定义"非异常失败"的归类口径)。
  程序化告警若按 `DEVICE`/`PROTOCOL` 分流会把它当"未知"吞掉。
- **归属**:与前轮 review-1021 §八-4 同族("UNKNOWN 桶语义混用"),
  本轮补充证据:它是**写安全三件套**的一部分,应优先于回调异常那类。

### P3-3 `DataType.register_size` 文档串措辞

- **位置**:`core/types.py:78-81`——docstring 写"BOOL 按 1 个寄存器/位计",
  实现 `max(1, self.byte_size // 2)` 对 BOOL(`byte_size=1`)→ `max(1,0)=1`。
  数值正确,但"1 个寄存器**/位**"读起来像"1 位";BOOL 在各协议的实际访问
  宽度是**1 位**(位软元件)而非 1 个 16 位寄存器。建议措辞改为
  "BOOL 返回 1(占位,实际访问宽度由协议位寻址决定)"。

### P3-4 `core/validation.py` 是 core 唯一无专门用例文件的模块

- **位置**:84 行,7 个公开纯函数(`require_bool`/`require_int`/`require_float`/
  `check_int16`/`check_uint16`/`check_range`/`check_byte_field`/`require_count`)。
- **现状**:仅经驱动测试**间接**覆盖(`test_mc_codec.py:433` 的
  `check_byte_field` 直测;`require_int` 经 `test_base_client.py:354`、
  `test_native_core.py:433` 等间接断言);**`require_bool`/`require_float`/
  `check_range`/`require_count` 无直接用例**(实测 grep 命中 0)。
- **影响**:这些函数是**全部驱动的参数校验闸**,口径漂移(如 `require_int`
  开始接受 bool、`require_count` 开始接受 0)会同时污染 18 族协议的错误面,
  而现有测试只在个别驱动路径上顺带发现。
- **建议**:补 `tests/unit/test_validation.py`(纯函数、零依赖、极低成本),
  逐函数锁 bool 排除、边界值(inclusive)、异常类型与文案关键字。

### P3-5 `Monitor` 注册表在"只建不连"场景无上界(观察项,非缺陷)

- **机制**:`Monitor.__init__` 构造即 `client._register_monitor(self)`
  (`core/monitor.py:357`),注册表只在 `disconnect()` 联动清空
  (`core/base_client.py:1383-1395`)。长跑进程中反复 `create_monitor()`
  而从不 `disconnect()` 会让 `_monitors` 单调增长。
- **为何是观察项**:①每个监视器对象本身很小(字典 + 计数器);
  ②`stop()` 后仍在册是**有意设计**(review-1006 P1:在册 = 未终态,
  重启免重登记);③正常运行模式(建监视器 → connect → 长跑 → disconnect)
  不触发。仅当"周期性重建监视器"的非常规用法下值得注意。
- **建议**:文档补一句"监视器数量应与点位组数同量级;需要频繁重建的
  场景用 `disconnect()` 收口",不建议引入弱引用/自动摘除(会破坏
  review-1006 的"在册=未终态"口径)。

---

## 五、经核无问题(明确不成立,避免下轮重复投入)

| # | 曾疑似 | 核验结论 |
| --- | --- | --- |
| 1 | `DeviceError.code` 标注 `int` 与契约"无码=`None`"冲突 | **不成立**:全库无 `DeviceError(..., None)` 调用点(`rg` 命中 0),类型与用法自洽;`code=0 → None` 由 `_set_error`(`base_client.py:726`)与 `_extract_code`(`:1682`)两处收口 |
| 2 | `convert._reorder_bytes` 四种字序非对合变换 | **不成立**:读实现确认 ABCD=恒等、DCBA=整体反转、CDAB=整字反转、BADC=字内字节交换,四者均为对合;实测 4 字序 × 8 类型往返全一致 |
| 3 | CRC16/LRC/位操作数值正确性 | **不成立**(即正确):CRC16 已知向量 0x4B37、LRC 0xF2、`to_signed`/`set_bit`/`get_bit` 全部符合规范;查表法与逐位法等价(黄金样本已锁定) |
| 4 | 退避指数未封顶会浮点溢出 | **不成立**:`_register_connect_failure`(`base_client.py:1513-1518`)先 `min(_connect_fail_count, RECONNECT_BACKOFF_MAX_EXPONENT=32)` 再算幂,且 `min(..., 30.0)` 兜底;指数 32 处 `2**32` 仍在 float 安全域 |
| 5 | `connect()` 的退避门控会误伤"门控刚过期"的事务 | **不成立**:`next_connect_in`(`:640-646`)在 `remaining <= 0` 时返回 `None`,而 `_execute` 判的是 `is not None`(`:1442`),门控过期即照常尝试 |
| 6 | 心跳线程会强引用客户端导致无法回收 | **不成立**:`_start_heartbeat_locked` 用 `weakref.ref(self)` + 等待期间 `del client`(`:472-489`),已丢弃的客户端可被 GC |
| 7 | `_stop_monitors` 持注册表锁调 `monitor._terminate()` 可能与监视器线程形成环形等待 | **不成立**:`disconnect()` **先停监视器再取事务锁**(`:329-333`,注释明示),`_terminate` 只置终态 + `set()` 事件;`stop()` 的 join 在事务锁外且有 `_join_budget()` 上界(`monitor.py:550-562`) |
| 8 | `Monitor` 快照/锚点被跨线程读写会撕裂 | **不成立**:`_snap` 整体替换(单次引用赋值)、`PointSnapshot`/`MonitorEvent` 为不可变 NamedTuple;`stop()` 的 join 使调用方返回时监视线程已收尾(慢周期超预算时按文档"线程仍在收尾末拍,无害") |
| 9 | `_set_error` 内 `_record_error()` 递归取同一把锁 | **不成立**:两把都是 `RLock`(`:186-189`),可重入;顺序恒为 `_state_lock` 内层再取,无环 |
| 10 | core 内存在降级桩 / 未实现残留 | **不成立**:`rg NotImplementedError|TODO|FIXME|HACK src/omniplc/core` 命中 0 |
| 11 | `incident_frames` 会被心跳失败覆盖(review-1021 P1-1) | **不成立**(前轮已修):`base_client.py:1490-1495` 与 `native/base.py:1231-1233` 均有 `if not heartbeat:` 守卫 |
| 12 | core 死区锚点取"上一拍快照值"导致缓慢漂移漏报 | **不成立**:锚点 = 该点**上次报告值**(`monitor.py:330-335` 注释 + `:681` 赋值),与 OPC-UA DataChangeFilter「与上次发送值比」同款 |

---

## 六、待人工裁决(不预设结论)

1. **P1-1/P1-2 的形状守卫该"抛"还是该"降级"?** 抛(`ProtocolFrameError`)
   能让驱动缺陷立刻可见,但会打破"`read_many` 逐点容错、不抛自定义异常"
   的既有口径;降级(`(False, None)` + `last_error`)守住契约但缺陷仍可能被
   忽略。**我倾向抛**(驱动返回形状错误属库内缺陷,与"参数非法抛 `ValueError`"
   同类),但这会新增一条"公共读 API 抛内部异常"的例外,须在
   `docs/architecture.md` §5 显式登记。
2. **`write_and_verify` 回读不符的 category**(P3-2):新增 `DATA` 类?
   归 `DEVICE`(设备侧不接受写入)?还是维持 `UNKNOWN` 并在 §5 表补一行
   "非异常失败"口径?涉及 `ErrorCategory` 公开枚举的向后兼容
   (下游可能已按五类做映射),须按 deprecation 周期评估。
3. **`Monitor` 死区种子值**(P3-1):统一为 `0.0`,还是把形参从
   `_add_point` 移除?后者更彻底但改动面稍大(构造期两处调用点)。
4. **`validation.py` 是否值得单独成文件 + 单独用例**——它只有 84 行、
   7 个纯函数,当前由驱动间接覆盖;补一个 40 行的直测文件成本极低,
   但也可以判定为"间接覆盖已足够"。属投入产出判断,非技术分歧。

---

## 七、排期建议

| 序 | 批次 | 内容 | 依据 | 量级 |
| --- | --- | --- | --- | --- |
| **1** | `fix(core 批量形状)` | P1-1 + P1-2 统一形状守卫(`read_many_strict` 与 `Monitor._cycle` 同一收口)+ 用例(桩驱动少返条目:strict 不假成功、monitor 不静默降质) | §二 | 半天 |
| **2** | `fix(i18n 覆盖)` | P1-3:68 条文案补表(EZ 29 + FOCAS 38 + S7 1)+ 守卫清单改 **AST 自动发现**(不再硬编码 9 张)+ 文档"9 张"口径同步 | §9.1 | 半天 |
| **3** | `fix(constants 硬项)` | P2-1 1E 写侧字上限(**取值须查手册 1401 A 列**)+ P2-2 KV 端口裁决 + P2-5/§10.1 依据补引用或整族标待核 | §10.2 | 1~2 天(依赖查手册) |
| **4** | `fix(core 状态暴露)` | P2-1 `incident_frames` 返回拷贝 + docstring;P3-2 category 口径(待 §六-2 裁决)+ 用例 | §三、§四 | 半天 |
| **5** | `test(守卫补强)` | P3-4 `tests/unit/test_validation.py`;§9.2-2 占位符一致性门禁、§9.2-3 重复键门禁;P1-1 的 `read_many` 长度契约守卫 | §四、§9.2 | 半天 |
| **6** | `chore(core 文档)` | P3-1 死区种子统一 + P3-3 `register_size` 措辞 + P3-5 监视器数量文档;§9.2-1 删 12 条死键;§10.2-12 删 8 条死常量 + 2 个死函数 | §四、§十 | 半天 |

**门禁要求**(修复批落地时):五件套全绿(3.7.9 + 3.12 双腿),
并在 PR/CHANGELOG 披露行为变更(若 §六-1 选"抛",属公共 API 行为变更,
按 deprecation 周期评估——但该场景仅自定义驱动可触发,可判为"缺陷修复"
按 CONTRIBUTING §六"纯缺陷修复不受 deprecation 约束"处理,须在条目中说明)。

---

## 八、切片覆盖说明(两路已回报)

> 本报告基线时,**i18n 取词表**(1411 行)与 **constants 常量与码表**(1425 行)
> 两路只读子代理审查尚未回报,曾以本节标注覆盖边界。**两路已于同日回报**,
> 结论**(§九 / §十)**,两路头号项均由主会话逐条**亲验/复算**(非转述):
> - §九:68 条漏收录以版本兼容 AST 重算坐实 + `set_lang("en")` 实跑取词验证;
> - §十:1E 读/写上限不对称、`KV_DEFAULT_PORT` 自相矛盾、各表值域与缺号
>   均按源码核对。
>
> 至此 **core 全 11 文件覆盖完毕**。

---

## 九、i18n 切片(1411 行取词表)

### 9.1 头号问题(P1,已亲验):12 张码表中 3 张的文案未收录,en 模式静默回中文

- **机制**:`_(表值)` 取词 → `_TRANSLATIONS.get(message, message)`
  (`core/i18n.py:1411`),**未收录兜底返回中文原文**。故"漏收录"在 en 模式
  下不报错、不告警,直接输出中文。
- **亲验(本轮实跑,非静态推断)**:`set_lang("en")` 后逐条取词,

  | 取词入参 | en 模式实际返回 | 判定 |
  | --- | --- | --- |
  | `地址非法`(S7 `RETURN_CODE_TEXT[0x05]`) | `'地址非法'` | 仍中文 |
  | `通信错误未打开(EZ_ERR_NOT_OPEN)` | `'通信错误未打开(EZ_ERR_NOT_OPEN)'` | 仍中文 |
  | `忙(EW_BUSY)` | `'忙(EW_BUSY)'` | 仍中文 |
  | `库句柄错误(EW_HANDLE)` | `'库句柄错误(EW_HANDLE)'` | 仍中文 |
  | 对照:`ip_address 不能为空` | `'ip_address must not be empty'` | 正常 |
  | 对照:`读码器无读出(无条码)` | `'The reader produced no read (no barcode)'` | 正常 |

- **精确复算(主会话按 AST 重算,3.7 用 `ast.Str`/3.8+ 用 `ast.Constant` 双兼容)**:

  | 码表 | 位置 | 文本值 | 漏收录 |
  | --- | --- | --- | --- |
  | `EZ_ERROR_TEXT`(三菱 CNC EZSocket) | `cnc/ezsocket.py:284`,取词 `:761-763` | 29 | **29(全表)** |
  | `EW_TEXT`(发那科 FOCAS) | `cnc/focas.py:107`,取词 `:410-413` | 39 | **38**(唯一命中 `成功` 系与 AB 表同文巧合) |
  | `RETURN_CODE_TEXT`(西门子 S7) | `plc/siemens/codec.py:100`,取词 `:134-137` | 12 | **1**(裸 `地址非法`) |

  **合计 68 条**。en 模式下 EZ/FOCAS/S7 的报错 detail 会夹整段中文
  (如 `Reading the CNC status failed: ...(无读出数据(EZNC_DATA_READ_NODATA))`)。
- **根因(比漏收录更值得修)**:**守卫清单是硬编码的 9 张表**
  (`tests/unit/test_i18n.py:137-147`),文档也照抄"9 张协议码表全覆盖翻译"
  (`docs/protocol-features.md:43`、`docs/architecture.md:1155`),
  而实际经 `_()` 取词的码表是 **12 张** → 新增的三张**从未进门禁**,
  且文档的"全覆盖"口径与代码不符。
- **修法建议**:①68 条补表;②守卫清单改为**自动发现**——AST 扫
  `_(非字面量)` 的实参所引用的模块级 dict,与表值集对拍(本次复算脚本即原型);
  ③文档"9 张"改口径。**为何列 P1**:它使现场"切英文给外方看日志"这一
  明确功能在三条协议上静默失效,且已有文档承诺"全覆盖"。

### 9.2 其余确认项

| # | 现象 | 证据 | 严重度 |
| --- | --- | --- | --- |
| 1 | **12 条真死条目**(无 `_()` 调用点、也不是任何码表的值) | `i18n.py:380/381/592/739/740/1023/1104/1105/1121/1122/1256/1284`(其中 7 条有同族活键,5 条 S7 键无替代) | P3 |
| 2 | **占位符一致性无门禁**:现存测试只查"同一条模板内自动/手动编号混用"(`test_i18n.py:90-108`),不查 en 值下标是否越出 zh 键参数个数;`architecture.md:1155` 自述的"构建脚本"**不在仓库** | `test_i18n.py:90-108` + `tools/` 零命中 | P3(潜在 P1:en 值写成 `{3}` 而键只有 3 参 → **报错路径自己抛 IndexError**) |
| 3 | **重复键无门禁** | 实测 1313 键 0 重复;dict 字面量重复键静默后者覆盖 | P3 |
| 4 | **边界标记 `<<<I18N_BEGIN>>>` 无人消费** | 全仓只有 `i18n.py:65`/`:1392` 两处出现 | P3 |
| 5 | **组织方式**:前约 1100 条无分组注释,后段按"审查批次"追加(R1001 批/review-1021 批);同驱动键分散(MC 散布 L600+/L838+/L1290+) | `i18n.py:1186-1344` 的 11 处批次注释 | P3 |
| 6 | 测试夹具硬编码**已废弃**文案(佐证死条目没人清) | `tests/unit/test_v030_reliability.py:741` 用 `"当前驱动暂不支持字符串读取"`,src 现役文案是 `i18n.py:1282` 带 `{}` 的版本 | P3 |

### 9.3 经核无问题(该切片已验证面)

- **正向键覆盖 0 漏**:1407 处字面量 `_()` 调用(1030 个不同键)与表键
  精确匹配(含跨行隐式拼接键 `plc/modbus/modbus.py:889-892` 等);
- **非字面量调用点 16 处穷尽**,全部是"码表值 → `_()`"形态;
  283 条"无字面量调用点"键 = 270 条动态可达 + 12 条死 + 1 条自用;
- **占位符实测 0 缺陷**(1313 条:0 越界 / 0 参数个数不等 / 0 命名不匹配 /
  0 规格差异);**重复键 0**;**en 值含中文 0**;**全角标点/未译 0**;
- **键值编码 0 缺陷**:0 条非 NFC、0 条 NBSP/零宽/双向控制符;仅 2 处刻意异常空白;
- **无裸中文 raise**(整库 AST 扫描 0 处);`_` 无别名;9 张已守卫码表值集全收录;
  3 张"不过 `_()`"的表(`_SDK_CODE_TYPE_NAMES`/`_VARIANT_TYPE_NAMES`/
  `_CIP_TYPE_LAYOUTS`)属**有意不译**。

---

## 十、constants 切片(1425 行常量与码表)

### 10.1 依据分布(核心口径问题)

| 依据档位 | 数量 | 占比 |
| --- | --- | --- |
| 页码级(文档号 + 页 + 章节/表) | 52 | 16.0% |
| 章节/表级(有 § 无页) | 65 | 19.9% |
| 仅文档名 | 23 | 7.1% |
| **协议常量但无手册依据**(仅散文说明) | **144** | **44.2%** |
| **协议常量且零注释** | **7** | 2.5% |
| 无需协议依据的通用工程常量 | 34 | 10.4% |
| 合计 | 326 | 100% |

**口径意义**:铁律要求"协议常量必须有文档号 + 页 + 章节/表/字段"。实测
**44.2% 的协议常量没有任何手册依据**(典型:`constants.py:77-79` MODBUS 502 /
MC 2000 / FINS 9600 三个端口**零注释**;`:94-199` 整段 Modbus 字段常量
只写"规范 §x"或什么都不写;`:201-212` 异常码文案表零引用)。这不是"可选的
注释风格问题",而是**这套库的立身依据(引用可回溯)在常量层有一半没落地**。

### 10.2 确认问题

| # | 现象 | 证据 | 严重度 |
| --- | --- | --- | --- |
| 1 | **1E 字单位写侧无 64 字闸门(读侧有)**——同帧型读写口径不一致。亲验:`_write_words`(`plc/melsec/melsec.py:838-843`)直接 `_build_frame`,1E 组帧校验(`plc/melsec/codec_a.py:101`)位/字**共用** `MC_1E_MAX_POINTS=256`;读侧则分口(位 256 / 字 64,`melsec.py:321-328`) | 可达路径:`write_string` 写 65~256 字 → 库放行 → PLC 拒帧 | **P2** |
| 2 | `KV_DEFAULT_PORT = 8000` 与**其唯一被引来源**冲突(docstring 自述"公开参照实现为 8001"却取值 8000) | `constants.py:909-911` | P2 |
| 3 | FINS/TCP 封装常量块(魔数/命令 0-2/24 字节握手)无手册依据且**未标「待核」**(README「待补」已登记该缺口,codec 也有标记,唯独 constants 没有) | `constants.py:812-828` vs `docs/protocol/README.md:120` | P2 |
| 4 | 引用了**本库未收录**的文档 | `constants.py:1248`「ODVA CIP Vol 1 §5-4.4.2」vs `README.md:116` 明列"完整 CIP Vol 1/2"为缺口 | P2 |
| 5 | 注释把两份 Modbus 手册版本号合并成不存在的书名 | `constants.py:92`「V1.1b3 TCP 指南 p.23」;README 分列「应用协议 V1.1b3」与「TCP 实施指南 V1.0b」 | P2 |
| 6 | TOYOPUC 整块 18 常量(含 27 条错误码表)是参考实现口径却**无「待核」标记** | `constants.py:1088-1151`(全库"待核"仅 5 处,不含它) | P2 |
| 7 | 海康命令端口 `9989` 硬编码两处,constants 无对应常量 | `reader/hikrobot_tcp.py:282`、`aio/__init__.py:1655` | P2 |
| 8 | `MC_MAX_RESPONSE_CONTENT_DATAGRAM=8179` 注释推导式与取值不符(自称按 3E 头 9 推导,实际差值 13 = 4E 头) | `constants.py:282-284` vs `:291` | P3 |
| 9 | `MC_MAX_DATAGRAM=8192` 与 3E/4E 理论最大帧自相矛盾(**已自认**),不变量只靠 UDP 入参校验维持 | `constants.py:272-280` | P3 |
| 10 | `S7_SLOT_MAX = AB_EIP_SLOT_MAX` 跨协议借值,依据错挂(实由 TSAP 位域决定)+ 隐式耦合 | `constants.py:1339-1340` vs `plc/siemens/client.py:136` | P3 |
| 11 | KV 码表 `ZR` 进制 10 与 MC 码表 `ZR` 进制 16 不同,注释却称"仅记号/码不同"→ **二者必有一错** | `constants.py:439` vs `:349` | P3 |
| 12 | **死常量 8 条** + **2 个公开函数零引用** | `constants.py:588/632/634/636/690/997/1333/1335`;函数 `bits_to_bytes:43`/`pad_even_bytes:52` | P3 |
| 13 | 同义异名 `FINS_NODE_MAX` / `FINS_NODE_DERIVED_MAX`(同值 254、同依据),仅下限用法不同 | `constants.py:604-608` vs `:611-614` | P3 |
| 14 | `MC_DEVICE_CODES` 注释的进制枚举**漏 `M`**(表中 M 属十进制族) | `constants.py:353-356` vs `:324` | P3 |
| 15 | 第二张 MC 码表(`_MC_DEVICE_CODES_FX5U_XY`)定义在**驱动内**,未集中,且承载着"待核·iQ-F 八进制"结论 | `plc/melsec/melsec.py:79-85` + `constants.py:358-361` | P3 |
| 16 | `FINS_HANDSHAKE_LENGTH(12)` / `FINS_HANDSHAKE_RESPONSE_SIZE(24)` **未标请求/响应侧**,响应长度域 16 无常量、偏移用裸字面量 | `constants.py:825-828`;`plc/omron/codec.py:646-661`/`:695-725` | P3 |
| 17 | `INOVANCE_WORD_DEVICES["C"]=(0xF400,256)` 表义"连续"与真值不符(C200+ 在 0xF700)。**经核当前无功能缺陷**(有特判兜住),但属后续改动陷阱 | `constants.py:474`/`:479-485`;`plc/inovance/address.py:142-155` | P3 |
| 18 | `SERIAL_DEFAULT_STOP_BITS=1.0` 零依据,且注释里已承认对汇川机型不成立 | `constants.py:84` vs `:521-524` | P3 |
| 19 | `MC_1E_ERROR_EXTRA_SIZE`(312)零注释(信息只在兄弟常量注释里) | `constants.py:307-312` | P3 |
| 20 | `MC_DEVICE_CODES` 的"iQ-F 八进制待核"**未登记 README「待补」**(只见真机清单/protocol-features)→ 违反"标待核 **且** 登记"双要求 | `constants.py:358-361` | P3 |
| 21 | `MC_MAX_TRANSFER_POINTS=900` 宽于 QnA 机型上限 480(已披露取舍) | `constants.py:225-231` | P3 |

### 10.3 经核无问题(该切片已验证面)

- **无重名重定义**(326 个顶层赋值);**码表无"同值两义"**——22 张 dict 值→键
  反查,唯 3 处重复均为**已披露别名**(MC 1E 的 M/S 同码、FINS T/C 同码、
  AB 扩展状态 0x04/0x05 同文);`MC_DEVICE_CODES` 28 个码值**两两互异**;
- **缺号符合协议**(Modbus 异常码缺 07/09 是规范固有、AB CIP 状态码 0x00~0x2C 连续、
  海康 errno -1~-7 连续且消费点有 `.get` 兜底);
- **帧长算式全部自洽**(7+253=260、1+253+2=256、0xF5//7=35、MC 三帧头 9/13/2 等);
- **MIN/MAX 与容量关系全成立**(无"上限小于下限";汇川 Y 区上限恰落在
  `MODBUS_ADDRESS_MAX` 内);**分块上限在消费点确实生效**(非纸面常量);
- **端口无驱动侧散落硬编码**(唯一例外即 P2-7 的 9989);
- **无 MC/SLMP 张冠李戴**(全文 0 处 `SH-080956`,MC 一律 `SH-080008`)。

### 10.4 该切片"需人工裁决"清单(须翻 PDF,主会话未臆断页码)

52 条页码级引用**全部未开 PDF 核对**;其中需逐列复核的同页多命令项:
`MC_MAX_TRANSFER_POINTS:228`、`MC_1E_MAX_POINTS:296`、
`MC_1E_MAX_WORD_READ_POINTS:304`、`MC_RANDOM_READ_MAX_POINTS:255`、
`MC_MAX_RANDOM_BLOCKS:267`(同引 Appendix 5 印刷页 466 但分属不同命令/子命令列)。
另有:`MC_RANDOM_READ_MAX_POINTS=192` 注释自相矛盾(称"取保守值 96 的 2 倍",
但 192 是宽松侧)、`MC_MAX_RANDOM_BLOCKS=120` 与注释"扩展子命令 60"的从属方向、
1E 写命令 1401 的字单位上限取值(**勿沿用读侧 64**)、Modbus 异常码 07/09 是否规范固有、
KV 端口 8000/8001、汇川 T 位区 512 与字区 256 不等、H3U X/Y 是否按机型收窄、
`MODBUS_STATION_MAX` 的 p.23 引文归属、`§18.2` 印刷页 395 与 README 记的 394 差 1 页。

---

> **登记与排期**:本报告不落地任何修改;§七 第 1~3 批(批量形状守卫 /
> i18n 覆盖 / constants 硬项)建议随本轮审查确认后登记 `docs/todo.md`。
> 修复批完成后按维护惯例回写状态,并将本文件按"存活至发版后删除"的惯例处理。
> **真机核验**:本报告结论为**库内静态 + 本地动态**结论(桩驱动实证、
> 已知向量校验、`set_lang("en")` 实跑取词),未上真机;P1-1/P1-2/P1-3 的修复
> 不改变任何协议帧面,无需真机核证。**唯一例外**:§10.2-1(1E 写侧字上限)
> 的取值必须查 SH-080008 Appendix 5「1401 成批写」A 列后落地,该页号
> **本轮未核**(不得沿用读侧 64),落地时须一并入真机清单。
