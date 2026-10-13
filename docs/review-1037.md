# review-1037 OPC-UA 专项审查报告(只差不做)

> 2026-10-11 · 基线:master `fb2063b` 附近(review-1036 后;工作区另有维护者未提交批)
> 范围:`plc/opcua/client.py`(1406)+ `address.py`(157)= **1563 行**;
> 测试 `test_opcua_client.py`(1420);手册 `docs/protocol/opcua/`(9 份 PDF)。
> **本轮只检查与差距分析,不落地任何修改**。
>
> **页码口径(子代理实测)**:Part 1 的 **PDF 阅读器页 = 自印页 + 10**
> (如 §6.3.3 = PDF 页 24 / 自印页 14);下文凡引 Part 1 均标两者。
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019~1036 同款)。

## 〇、执行摘要

**依据状态**:**`docs/protocol/opcua/` 的 9 份 PDF 里没有 Part 3/4/6 规范正文**。
子代理用 pdfplumber 逐页扫描并**逐一验证关键词命中为 0**:
`DataChangeFilter`/`DeadbandType`/`MonitoringParameters`/`RequestedPublishingInterval`/
`CreateMonitoredItems`/`MonitoringMode`/`NamespaceIndex`/`NumericIdentifier`/
`GuidIdentifier`/`OpaqueIdentifier`/`NodeId` 语法 `ns=N;[isgb]=` —— **全部 0 命中**;
Part 3/4/6 **仅作为标题**出现在 Part1 PDF p.11/16/17 与培训材料里。
(OPC-DA Custom Interface v2.05A 是 **legacy COM 规范**,与 UA 服务面无关。)
⇒ **本族本质 = `asyncua` 封装层**,协议面断言绝大多数"**随 asyncua 行为**";
唯一可回溯的规范引用是 **Part 1**(子代理逐条核对确实存在:§6.3.3 AddressSpace、
§6.3.4.1/6.3.4.2 MonitoredItem/Subscription、§7.10/§7.11、订阅生命周期与序号重传)。

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| **契约一致性** | **14 个 asyncua 调用点 × 23 类异常 实跑穷举 → `Exception` 面 0 漏出** | — |
| **回调线程内退订** | **死锁会话**(真服务端实测 20s 未返回),而 docstring 宣称"幂等,线程安全" | **P1**(§二) |
| **browse 起始层异常** | 被吞 → 公共 API 报 **`(True, {})`**(亲验内层返回值) | **P2**(§三) |
| 分类漂移 | 订阅路径 `OSError/TimeoutError` → **UNKNOWN**;读写路径同类 → TRANSPORT/TIMEOUT | **P2**(§三) |
| 订阅静默死亡 | 无信号、不落 `last_error`、不失效句柄 | **P2**(§三) |
| **版本漂移** | 区间钉版 + 依赖 asyncua **sync 私有内部**,而 CI 只测 1.1.5 | **P2**(§三) |
| 静默错值 | `parse` 整串 strip → **首尾空白被丢**(可能寻址到另一节点) | **P3**(§四) |

---

## 一、审查范围与实证清单

| # | 实证项 | 结果 |
| --- | --- | --- |
| 1 | 9 份 PDF 的规范覆盖面 | **Part 3/4/6 正文 0 命中**(逐关键词扫描)✔ |
| 2 | Part 1 引用可回溯性 | §6.3.3 / §6.3.4.1/2 / §7.10 / §7.11 **逐条存在** ✔ |
| 3 | **异常面穷举** | **14 × 23 矩阵:0 漏出**;仅 `BaseException` 穿透 `except Exception`(有意)✔ |
| 4 | **真服务端实测** | `unsubscribe()` **在回调线程内 20s 未返回**;期间整个会话停摆(3s 慢回调 → 客户端读耗时 **5.23s**)❌ |
| 5 | 退订失败后的残留 | 服务端订阅**仍在**、`active_subscriptions` **仍列该句柄** ❌ |
| 6 | **browse 内层** | **亲验**:`get_children` 抛 `OSError` → 内层 `operation` 返回 **`{}` 且无异常** ⇒ 公共 API 报 `(True, {})` ❌ |
| 7 | Package 钉版 | `pyproject` opcua/all = `>=1.1.5,<2`、dev = `==1.1.5`、`uv.lock` = 1.1.5、`architecture.md` 写 `==1.1.5` —— **四处口径不一致** ❌ |
| 8 | PyPI 实测 | 1.1.5 未声明 `requires_python`;1.1.6 = **≥3.9**;2.x = **≥3.10** ⇒ **3.9+ 用户装 extra 会拿到 1.1.6**,而代码依赖 sync **私有内部** ❌ |
| 9 | 测试面错误标注 | 把 `0x80350000` 标成 `BadNodeIdUnknown`,实测该码 = **BadAttributeIdInvalid**(`BadNodeIdUnknown` = `0x80340000`)❌ |

---

## 二、P1:回调线程内 `unsubscribe()` **死锁会话**

### 2.1 现象(子代理真服务端实测)

- 在**数据变化回调线程内**调用 `unsubscribe()` → **20 s 未返回**;
- 期间**整个会话停摆**(实测 3 s 慢回调使客户端自己的读耗时 **5.23 s**);
- 返回 `False` 后**服务端订阅仍在**、`active_subscriptions` **仍列该句柄**;
- **docstring 却宣称**「取消订阅(**幂等,线程安全**)」(`client.py:462`)。

### 2.2 机理

`asyncua` 的 `sync.ThreadLoop.post` **从 loop 线程自投会阻塞**
(至 `sync_wrapper_timeout` = 120 s)—— 而回调正是在该 loop 线程上执行的
⇒ **回调内退订 = 自己等自己**。

### 2.3 定级与修法

- **定级 P1**:①**会话级死锁**(不是单次调用失败 —— 期间所有事务停摆);
  ②与 docstring 的"线程安全"承诺**直接冲突**;③触发方式**很自然**
  (用户想在"收到某个值后停止订阅"时就会这么写);
- **修法**:①`unsubscribe()` 检测"当前线程 == loop 线程"时**改走非阻塞路径**
  (置意图 + 由 loop 侧延后执行),或直接**抛明确异常**(`OmniPLCInternalError`,
  文案说明"不可在回调线程内退订,请从主线程调用");
  ②docstring **必须**去掉"线程安全"的无条件承诺,写明该限制;
  ③补"回调内退订"用例(可用假 loop 复现,不必真服务端)。

---

## 三、P2:四条(§三-1 为亲验)

### 3.1 ★ P2-1 `browse` 起始层异常**静默假成功**(亲验)

- **代码**(亲验):`_browse_node` 的起始层
  ```python
  try:
      children = node.get_children(refs=...) if reference_type_id is not None else node.get_children()
  except Exception:
      return {}          # ← 吞掉一切异常
  ```
- **亲验**:直接捕获内层 `operation` 的返回值 —— `get_children` 抛 `OSError` 时
  **返回 `{}` 且无异常** ⇒ 公共 `browse()` 会得 **`(True, {})`**;
  而**同一异常在 `read` 路径**会经 `_translate_ua_error` 抛出 → `(False, None)` + 分类;
- **子代理实跑**:23 类异常喂起始层 → **全部** `(True, {})`,且 `last_error` 空、`connected` 仍 `True`;
- **影响**:**链路断开 / 节点不存在 / 权限不足** 全部表现为"**枚举成功但树为空**" ——
  属**静默假成功**(调用方会据此认为"该设备没有变量");
- **注**:子节点的属性读失败按 docstring「跳过」是**设计意图**,但**起始层**的失败与之**语义不同**,
  不应共用同一分支;
- **修法**:起始层异常**上抛**(经 `_translate_ua_error`),子节点层保持"跳过";
  或至少把起始层失败记入 `last_error` 并返回 `(False, None)`。

### 3.2 P2-2 **分类漂移**:同一故障两种分类

订阅路径的 `OSError`/`TimeoutError` 被包成 `OmniPLCInternalError`
⇒ `last_error_category = **UNKNOWN**`;而**读写路径**同类异常 → `TRANSPORT`/`TIMEOUT`。
⇒ 上位机按分类分级告警时,同类故障在两条路径上**给出不同结论**。
**修法**:订阅路径沿用既有的 `_categorize` 口径。

### 3.3 P2-3 **订阅静默死亡无信号**

`asyncua` 1.1.5 的 `inform_subscriptions` 仅 `BadShutdown` 时调用;
`publish` loop 遇 `BadNoSubscription` **直接 return**,未知订阅仅 `warning`,**无 Republish**;
驱动侧 `status_change_notification` **仅 debug 日志**,**不落 `last_error`、不失效句柄**,
`auto_resubscribe` 只在**重连**时触发 ⇒ **订阅可能在链路仍连的情况下静默停更**,
而调用方无从察觉。**修法**:把 `status_change` 的非 Good 状态落 `last_error` 并标记句柄失效。

### 3.4 P2-4~7(择要)

| # | 现象 | 影响 |
| --- | --- | --- |
| P2-4 | `read_many([])` 抛 `ValueError`,违反基类 docstring"空列表返回 `[]`"(`write_many` 返回 `[]`)**不对称** | 与 review-1034 P3-3(S7 同病)**同型** ⇒ 建议全库统一 |
| P2-5 | **回调执行线程未披露**(实测 `tid != 主线程`),且"**回调阻塞即冻结会话**"未说明 | 与 §二 P1 同源,应一并写进文档 |
| P2-6 | **队列深度不可配且未披露**:asyncua 默认 `QueueSize=0 + DiscardOldest=True` ⇒ **突发变化静默丢通知**;而 **Part 1 §7.10(PDF 页 29)** 明说可配"排队全部 / 只留最新" | 数据完整性 |
| P2-7 | **版本漂移**:区间钉版 + 依赖 asyncua **sync 私有内部**(`ThreadLoop.post` 返回语义、`aio_obj._subscribe` 位置参数、`sync.Subscription` 无 `subscription_id`)⇒ **1.1.6 上行为可能变**;死区分支只判 StatusCode、**不校验句柄为 int**(若 `post` 语义变 → **静默假成功**) | 升级即风险 |

---

## 四、P3(11 条,择要)

| # | 现象 | 影响 |
| --- | --- | --- |
| 1 | `_translate_ua_error` 把 `UaError` 一律当状态码:`UaError`/`UaInvalidParameterError`/`UaStringParsingError`/`UaStructParsingError` **无 `.code`** → 报「OPC-UA 出错 **0x00000000**」+DEVICE,而 **0x00000000 实为 Good**;其中 `UaInvalidParameterError` 正是 asyncua `read_value` 在"Good 但值为空"时抛的 ⇒ `_coerce_read` 的「节点值为空」分支**在真服务端不可达** | 报错文案自相矛盾 + 一条分支是死代码 |
| 2 | `OpcUaSubscription.subscription_id` **名不符实**:实测 = `id(sync Subscription)`(如 97297872),而协议 `SubscriptionId` = 78 | 用户拿它去对服务端会错 |
| 3 | browse 子节点属性读失败按 docstring「跳过」,实际插入 `browse_name=""`/`node_class="Unknown"` **条目** | 与文档不符 |
| 4 | 入参类型未校验:`deadband_value="abc"`/`sampling_interval_ms="100"`/`max_depth="2"` → **`TypeError`**(非约定 `ValueError`);`deadband_value=True` **被静默当 1.0** | 契约面 |
| 5 | 事件回调 `node_id=str(event.SourceNode)` → `"NodeId(Identifier=2253, …)"`,与数据变化回调的 `"ns=2;i=1"` **格式不一致** | 回调面 |
| 6 | 订阅被拒文案 `str(StatusCode)` → `"StatusCode(value=2150891520)"` **无状态名**(而批量读用 `status.name`) | 排障 |
| 7 | `delete` 失败时 `unsubscribe` 返回 `False` 但**索引已摘**、服务端资源滞留到拆链;**`False` 三义混淆**;`release()` **无用例** | 资源 |
| 8 | 事件订阅 `publishing interval` **硬编码 0** 未披露 | 文档 |
| 9 | **★ 静默错值**:`parse` 整串 `strip` ⇒ `'ns=2;s=Tag '` → `'ns=2;s=Tag'`(而 asyncua **保留尾空格**)⇒ **字符串标识符首尾空白被静默丢弃,可能寻址到另一节点** | 静默错值(与"地址面禁 strip"的纪律冲突) |
| 10 | `address.py` 引 **OPC 10000-3(未收录)未标「待核」**;`deadband` 死区引 §6.2.10 同 | 铁律面 |
| 11 | **测试把 `0x80350000` 标成 `BadNodeIdUnknown`** —— 实测该码 = `BadAttributeIdInvalid`(`BadNodeIdUnknown` = `0x80340000`) | 测试断言错 |

---

## 五、经核无问题(该族相当扎实的一面)

1. **异常面零漏出**(14 个 asyncua 调用点 × 23 类异常 **实跑穷举**)
   —— 全部收口为 `(False, None)`/`False` + `last_error` 三件套 ✔
2. **`DeviceError` 不重试**(1 次事务)、**`OSError` 拆连 + 惰性重连自愈**
   (真服务端"杀掉→重启"后读 **0.01 s 恢复**)✔
3. **`connect`/`disconnect` 幂等**(3 次 connect 仅 1 次建链)✔
4. **`DataType` 10/10 对齐 `VariantType`**;写值/范围校验到位;服务端 Bad 状态读写**都会抛并被翻译** ✔
5. **死区路径位置参数逐位对齐 asyncua 1.1.5**;两条订阅路径**都显式判 `StatusCode`**
   (真服务端拒绝实测 `DeviceError`)✔
6. **别名 84/85/86/87/33/2258 与 asyncua `object_ids`(源自规范 `NodeIds.csv`)一致** ✔
7. **事件字段键集实测 = `BaseEventType` 的 9 个 `SelectClauses` 键**,
   且**无 `internal_properties` 泄漏** ✔
8. **GUID/base64/UInt16/UInt32 严格校验,优于 asyncua** ✔
9. **回调异常被吞并落三件套**;心跳成功**不污染业务 `last_error`**、传输类心跳失败**照常计数** ✔

---

## 六、测试面缺口

`test_opcua_client.py` **1420 行**,但 **24/52 用例带 `skipif`(依赖 asyncua)** ——
未装 extra 时**行为面全跳**。缺:
**异常面穷举**(订阅/浏览的翻译、分类漂移、browse 静默 `{}` 均无用例)、
**Percent 死区端到端**、**queuesize**、`reference_type_id`(仅签名断言)、
`release()`/`delete` 失败/`_resubscribe_all` 部分失败/connect 失败翻译/
**回调线程与阻塞**/**`read_many([])` 契约**用例。

---

## 七、待人工裁决

1. **P1 的修法取向**(§二):回调内退订是**抛明确异常**(推荐,实现简单且诚实),
   还是**改走非阻塞路径**(体验好但需改 asyncua 调用姿势)?
2. **browse 起始层**(§3.1):上抛(我倾向)还是记 `last_error` 但保留 `(True, {})`?
3. **版本钉版**(P2-7):`>=1.1.5,<2` 与 `==1.1.5` 二选一 —— 若保留区间,
   须把"依赖 sync 私有内部"这一事实**写进 CHANGELOG/文档**,并补 1.1.6 的 CI 腿;
4. **`parse` 的 strip**(P3-9):是否改为"仅去首尾整体空白、不动标识符内部与两端"? —— 涉及**静默错值**。

---

> **登记与排期**:本报告不落地任何修改。§4(文档/文案)与 §三-1/3.2 的**分类订正**为零行为风险;
> §二(P1)与 §三-4 属**行为变更**,须补测试并按 CONTRIBUTING §六披露。
> **真机核验**:本报告结论为**规范逐关键词扫描 + 假对象异常矩阵 + 真 asyncua 服务端实测**,
> **未接真 PLC**(西门子/倍福/KEPServer)—— §七 相关项列真机前置。

---

## 八、经核无问题(该族相当扎实的部分,集中列出)

| 项 | 核法 | 结论 |
| --- | --- | --- |
| **异常面零漏出** | **14 个 asyncua 调用点 × 23 类异常实跑穷举** | ✔ 全部收口为 `(False,None)`/False + 三件套 |
| 拆连 + 惰性重连自愈 | 真服务端"杀掉→重启" | ✔ 首读失败置 `connected=False`,重启后 **0.01s 恢复** |
| `connect`/`disconnect` 幂等 | 实跑 3 次 | ✔ 仅 1 次建链 |
| `DataType` 映射 | 与 `VariantType` 逐项 | ✔ **10/10 对齐** |
| 死区路径位置参数 | 与 asyncua 1.1.5 源码逐位 | ✔ 一致 |
| 别名常量 | 与 asyncua `object_ids`(源自规范 NodeIds.csv) | ✔ **84/85/86/87/33/2258 全一致** |
| 事件字段键集 | 真服务端实测 | ✔ 恰为 `BaseEventType` 的 **9 个 SelectClauses**,无 `internal_properties` 泄漏 |
| NodeId 严格校验 | 与 asyncua 对照 | ✔ `b=AA==BB==`/`g=` 无连字符/`ns=65536`/`i=-1` 等**驱动拒绝而 asyncua 接受** ⇒ **本库更严** |
| 回调异常治理 | 真服务端 | ✔ 被吞 + 落三件套 + 订阅存活 |
| `_set_error` 跨线程 | 8×2000 并发 | ✔ 计数值正确无丢失 |

## 九、虚警留痕

| 疑点 | 排查结论 |
| --- | --- |
| 一度疑「订阅路径的 OSError 分类漂移会导致**静默错值**」 | **收窄**:后果是**告警分级误导**(UNKNOWN vs TRANSPORT),**不是错值** ⇒ 维持 P2 而非 P1(§3.2) |
| 一度疑「`BaseException` 穿透是缺陷」 | **不成立**:`KeyboardInterrupt` 穿透是**正确行为**;且实测 `concurrent.futures.CancelledError` 在 3.7.9 与 3.12 **仍是 `Exception` 子类**(取消路径不穿透)⇒ 仅登记"未加固",**未升级为确认问题** |
| 一度疑「Percent 死区不可用」 | **未核实而非缺陷**:需 Part 4 正文与 `EURange` 属性,库内无正典 ⇒ 列"需规范",**不作问题**(§七) |
