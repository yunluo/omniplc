# review-1021 写安全总闸与契约一致性审查报告(只差不做)

> 2026-10-09 · 基线:master `8c60133`(v0.56.0 后,工作区仅 `docs/todo.md` 一处空白改动)
> 范围:全库写入口安全闸 / 公共契约一致性 / 测试面缺口 / 文档与工程基建四处切片。
> **本轮只检查与差距分析,不落地任何修改**;修复排期见 `docs/todo.md`。
>
> 方法:两路只读子代理分区并行(契约一致性切片、测试覆盖切片)+ 主会话逐项亲验。
> 每个条目的 `文件:行号` 均由主会话打开原文核对;**亲验** = 主会话读过原文逐字确认;
> **审读** = 子代理静态阅读结论,主会话抽查未见反例;**未核实** = 单源结论或未展开验证。
> 行号一律以本报告基线的 master 为准;行号为函数体位置时标注为"函数体于 X"。
>
> **性质声明**:本报告是**只读审查台账**,不含任何代码改动,也不代表修复承诺
> (与 review-1019 同款口径)。P0/P1 的严重度按"现场后果 × 触发概率"判定,
> 不按修复难度判定。
>
> **复核批注(2026-10-10,主会话格式核查批;同日修复批二次订正)**:
> ①**P0-1 机制订正**——A 形态 13 处入口全部以 `is_write=True` 进 `_execute`,
> `read_only` 总闸(`core/base_client.py:1414-1417`,native 同款
> `native/base.py:1168`)对它们**照常生效**(`read_only=True` 下首笔事务即抛
> `RuntimeError`、零字节发送),原稿"`read_only=True` 时照常发包"**不成立**;
> B 形态(读码器三 API)虽外层不经 `_execute`,内部 `_write_control`/
> `_read_status_word` 走**公共** `self.write`/`self.read`
> (`reader/hikrobot.py:356`/`:368`),两把闸经此同样生效,原稿"read_only 与
> last_error 双双失效"同样不成立。**read_only 的真实缺口收敛为 C 形态 2 条**
> (`generic_message` 硬编码 `is_write=False`、`command` 未传);**白名单的
> 真实缺口为 A 形态 13 条**(闸只在 `write()`/`write_string()` 入口);
> B 形态的残余问题收敛为 §三 的异常契约逃逸(P0-2),修复与 §三 合并。
> ②§七条目 1 的"aio 缺 read_only/write_whitelist 转发"经核**不成立**
> (`aio/__init__.py:579-595` 转发在位),全项撤销。③原 P2-5(todo 首行
> 空格)在当前工作区不复现,已标注消解;§六 编号缺 P2-4,随批顺位重编
> (原 P2-5~P2-10 → P2-4~P2-9)。④机械格式:全角逗号一处、§〇 交叉引用
> 范围订正。
>
> **实施批注(2026-10-10 修复批,`58ceb9e`)**:§十 第 1/2 批(写安全根修 +
> 契约五项)已落地,门禁五件套全绿(3.7.9 **1921 passed** / ruff format·check /
> mypy(3.12) / ty):
> - **A 形态**:13 入口(同步 7 + native 6)逐地址过 `_check_write_allowed`
>   (白名单逐 item,零发送期拒绝)+ 基类 `write_many` 入参前置全量校验
>   (P1-4);`read_only=True` 下 13 入口零发送用例锁死(行为已在,补测)。
> - **B 形态(与 §三 一并收口)**:`scan`/`clear_error` 整段握手改走
>   `_execute(is_write=True)` 单事务,`read_status` 改返
>   `(是否成功, 状态快照)`;内部手动 `_set_error` 撤除由外层统一记账。
> - **C 形态**:`generic_message` 按 CIP 服务码判写(0x4D Write Tag /
>   0x4E Read-Modify-Write,库内写标签同款服务码)+ `is_write` 显式覆盖
>   (0x53 分片写未实现不在判定表);`command` 按 `cmd_type` 分流
>   (Set/Exec 写、Get 读)。
> - **契约批**:`_set_error` 单点 `code=0→None`(§3.2 四处直写自动治好)、
>   `_extract_code` 对 TIMEOUT 强制 `None`(P1-2)、心跳 OSError 分支快照
>   守卫 2 行(P1-1,同步+native)、字符串能力缺省实现改 `ValueError`
>   (P1-5)、§5 表订正(P1-3)。
> - **登记表通杀守卫**:`tests/unit/test_review_1021_gates.py` 新文件——
>   `write*`/`*write`/`set_*` 命名面 ⊆ 登记表(新增写方法漏登记即门禁红);
>   `write_file_record`(FC21)经核 `is_write=True` 在位(报告 13 条清点
>   无误),随批登记;read_only 全客户端类在场守卫。
> - **行为变更披露(三项)**:`HikrobotIdModbusClient.read_status` 签名
>   `HikrobotStatus → (bool, Optional[HikrobotStatus])`;字符串能力缺失
>   由 `(False, None)`/`False` 改为同步抛 `ValueError`;`generic_message`
>   判写服务与 `command` Set/Exec 在只读模式下拒绝。CHANGELOG 随下批
>   发版统一落;§3.3 待裁决按推荐方向(收进契约)落地。

## 〇、执行摘要

本轮**没有发现协议帧面缺陷**,发现的是一处**结构性缺陷**及其衍生面:

**「写」在本库没有单一定义,只有逐点传的 `is_write=` 开关**。写安全两把闸
(`read_only` 只读闸、`write_whitelist` 白名单)完整装在 `write()` 与
`write_string()` 两个基类入口上;`read_only` 另有 `_execute` 总闸兜底
(按 `is_write=` 生效),**白名单没有第二道**。而全库另有 **16 条**独立写
入口(A 形态批量/随机/掩码/字符串写 13 条 + B 形态读码器握手 API 3 条)
与 **2 条**名义读实写入口(C 形态)。于是:

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| 写入口安全闸 | **A 形态 13 条绕过白名单闸(read_only 总闸仍生效);B 形态 3 条外层不进事务层(两把闸经内部公共 write/read 仍生效,残余=异常契约逃逸 §三);C 形态 2 条名义读实写(read_only 不拦)** | P0-1、P0-2(见 §二/§三) |
| 公共契约一致性 | 读码器一族例外逃逸 + 5 处数值/分类口径不一致 | P0-2、P1-1~P1-5 |
| 测试面 | 8 个 aio 镜像类零测试引用;镜像守卫不校验接线 | P0-3(见 §五) |
| 文档/工程基建 | 文档魔数全线漂移;台账自身有脏改动 | P2 批(见 §六) |

**最刺眼的一点**:文档已把安全承诺写死为"一切写入口显式拒绝"(`SKILL.md:96`)
与"`_execute` 总闸兜底驱动特有写"(`docs/architecture.md:1157` v0.56.0 条目)。
复核后口径收窄但仍然失真:`read_only` 对 A/B 形态**确实拦得住**(复核批注①),
对 C 形态两条不成立(`is_write` 缺失/写死 `False`);`write_whitelist` 的
"表外地址拒绝"对 A 形态 13 条不成立(批量/随机/掩码/字符串写可写任意合法
地址)。**这不是缺功能,是已发布的对外承诺与实现不一致**——因此 P0-1 的
定级依据是"安全承诺失真",不是"实现里少了一行"。

另:本轮 4 项经核**不成立**,已撤销登记(§七),避免下一轮重复投入。

---

## 一、审查范围与清点口径

| 项 | 实测值 | 口径 |
| --- | --- | --- |
| 源码 | 88 个 `.py` / 38034 行 | `src/omniplc`(含子包) |
| 公开客户端 | 根包 34 个 `*Client` + aio 34 个 `A*` + native 9 个 `Async*` | `len(omniplc.__all__)` 实测,非文档口径 |
| 测试 | 64 文件 / 28580 行 / 1529 个 `def test_*` | 参数化展开数未计入(故与 CHANGELOG 的 passed 数不同源) |
| `raise` 清点 | 1313 处(非 `ValueError` 253 处 + 变量式 re-raise 28 处) | 逐类归并,未发现第二类未收口异常(除 §三 P0-2) |
| 驱动自定义异常类 | **全库仅 1 个**:`S7ProtocolError`(继承 `ProtocolFrameError`,合规) | `src/omniplc/plc/siemens/codec.py:124` |
| `_check_write_allowed` 调用点 | **6 处**(2 处定义 + 4 处调用) | 见 §二 |
| 写路径总数 | **16 条 + 2 条名义读实写**(A 形态 13 + B 形态 3;C 形态 2) | 见 §二表 |

**门禁基线**:因会话沙箱为只读,pytest 无可用临时目录、`uvx` 无法初始化缓存,
本轮**未能实跑门禁**。以 `D:\bin\ruff.exe` 0.15.11 实测替代:
`ruff check --no-cache src tests` 零告警、`ruff format --check --no-cache` 147 文件零漂移。
**其余"测试全绿"结论均来自静态阅读,未经实跑验证**,修复批落地时须重新实跑五件套。

---

## 二、P0-1 写安全两把闸存在三形态绕过(16 条写路径 + 2 条名义读在闸外)

### 2.1 闸门现状(亲验)

写安全两把闸的完整实现只在 `BaseClient._check_write_allowed`
(`core/base_client.py:1163`),全库**仅 4 处调用**(白名单在全网库再无第二道;
`read_only` 另有 `_execute` 总闸,见下段与 §2.2):

| 调用点 | 入口 | 亲验 |
| --- | --- | --- |
| `core/base_client.py:763` | 同步 `write()` | ✔ |
| `core/base_client.py:1009` | 同步 `write_string()` | ✔ |
| `native/base.py:654` | native `write()` | ✔(审读) |
| `native/base.py:885` | native `write_string()` | ✔(审读) |

`read_only` 闸的另一条兜底(`_execute` 内 `if is_write and self._read_only`)
只在调用方**记得传 `is_write=True`** 时生效(`core/base_client.py:1414-1417`)。

### 2.2 绕过形态 A:自行组建事务绕过 `write()` 入口白名单闸(`read_only` 总闸仍有效)

**机制**:驱动覆写的批量/随机/掩码写**自行组建事务**(不经 `self.write`),
入口只调 `_check_address(..., is_write=True)`——该校验只判"地址是否落在可写区域",
**不判白名单**。这些事务最终仍以 `is_write=True` 进 `_execute`,故 `read_only`
总闸(`core/base_client.py:1414-1417`)照常生效:`read_only=True` 下首笔事务即
抛 `RuntimeError`、零字节发送——原稿"照常发包"系误判,已订正(复核批注①,
13 处调用点逐处核实,证据列于下表之后)。**真正被整批绕过的是白名单**:
`_check_write_allowed` 只在 `write()`/`write_string()` 入口调用,下表入口
一个都不经过它。

| # | 入口 | 位置(函数体) | 白名单闸 | 备注 |
| --- | --- | --- | --- | --- |
| A1 | Modbus `write_many` | `plc/modbus/modbus.py:461` | ✗ 无 | 只 `_check_address` |
| A2 | Modbus `write_batch` | `plc/modbus/modbus.py:508` | ✗ 无 | 同上 |
| A3 | Modbus `write_mask_register`(FC22 设备侧原子改位) | `plc/modbus/modbus.py:776` | ✗ 无 | 直接 `build_mask_write_pdu` |
| A4 | Modbus `read_write_registers`(FC23,**名似读、实为写**) | `plc/modbus/modbus.py:827` | ✗ 无 | 同一事务内写多寄存器 |
| A5 | MC `random_write`(1402,位软元件按整字 16 点覆盖) | `plc/melsec/melsec.py:668` | ✗ 无 | 无响应数据命令,写风险最高 |
| A6 | MX Component `write_batch`(WriteDeviceRandom) | `plc/melsec/mx.py:972` | ✗ 无 | 仅校验条数与值类型 |
| A7 | S7 `write_wstring` | `plc/siemens/client.py:783` | ✗ 无 | 直接 `_execute(_write_wstring_impl)` |
| A8 | native Modbus `write_many` | `native/modbus.py:568` | ✗ 无 | 与 A1 同源 |
| A9 | native Modbus `write_batch` | `native/modbus.py:597` | ✗ 无 | 与 A2 同源 |
| A10 | native Modbus `write_mask_register` / `read_write_registers` | `native/modbus.py:757` / `native/modbus.py:796` | ✗ 无 | 与 A3/A4 同源 |
| A11 | native MC `random_write` / native S7 `write_wstring` | `native/melsec.py:615` / `native/siemens.py:715` | ✗ 无 | 与 A5/A7 同源 |

`read_only` 总闸在位的证据(复核批注①,13 处 `is_write=True` 调用点):
同步侧 `plc/modbus/modbus.py:552`(write_batch 入口)/`:644`(RMW 项)/`:680`
(合并 chunk)/`:824`(FC22)/`:880`(FC23)、`plc/melsec/melsec.py:748`、
`plc/melsec/mx.py:1041`、`plc/siemens/client.py:794`;native 镜像
`native/modbus.py:624`/`:681`/`:711`/`:793`/`:841`/`:1011`、
`native/melsec.py:692`、`native/siemens.py:726`;native 总闸同款于
`native/base.py:1168`。

**联动面**:汇川 Modbus 三件(`plc/inovance/inovance.py:124`/`:136`/`:148`)
是**纯转发**到 `super().write_many` / `write_batch` / `write_mask_register`,
故 `InovanceTcpClient` / `InovanceRtuClient` 与 native 汇川同款全中
(`native/inovance.py:174`/`:187`/`:200`)。

**白名单模式更严重**:`write_whitelist=True` 的语义是"只允许写点位表内地址"
(`core/base_client.py:1143`,管辖范围已在 `:1148-1151` 披露为"地址型写入"),
而 `write_many`/`write_batch` 可写**任意**合法地址——等于整张白名单被绕过。

### 2.3 绕过形态 B:外层不进事务层(订正:两把闸经内部公共 write/read 仍生效,残余=异常契约逃逸)

**机制订正(复核批注①)**:读码器 `HikrobotIdModbusClient` 的三个公共 API
外层直接 `with self._lock: return self._xxx_locked(...)`,**没有外层
`_execute` 包裹**——但其内部 `_write_control`/`_read_status_word` 走
**公共** `self.write`/`self.read`(`reader/hikrobot.py:356`/`:368`),
`_check_write_allowed` 与 `_execute` 经此逐笔生效:`read_only=True` 下
`scan()`/`clear_error()` 在首笔控制字写处即抛 `RuntimeError`(零发送,
两者的首步都是控制字写),白名单同理;`last_error` 由 `_xxx_locked` 内
手动 `_set_error` 记账。原稿"read_only 与 last_error 双双失效"**不成立**。

| # | 入口 | 位置 | 亲验证据 |
| --- | --- | --- | --- |
| B1 | `scan()` | `reader/hikrobot.py:226-227` | 外层原文即 `with self._lock: return self._scan_locked(...)`,无 `_execute` |
| B2 | `read_status()` | `reader/hikrobot.py:300-307` | 同上结构 |
| B3 | `clear_error()` | `reader/hikrobot.py:317-345` | **本方法自身就是写设备**(清错控制字) |

- B1 内部 `_write_control()` 经公共 `write()` 真发 FC06 写保持寄存器
  (`reader/hikrobot.py:356`),即 `scan()` 是**写操作**(闸门语义成立;
  原稿"FC05/FC16"为笔误,实为 `write()` 走线的 FC06 单寄存器写)。
- **同族反证**(说明这是该驱动形态而非全库设计):`KeyenceSrClient.scan()`
  走 `_execute(lambda: self._scan_once(...), is_write=True)`
  (`reader/keyence_sr.py:131-133`);同族 `HikrobotIdTcpClient.scan()`
  也走 `_execute`(`reader/hikrobot_tcp.py:367`)。
- **残余真问题 = §三 P0-2(异常契约逃逸)**:握手级失败以 `DeviceError`/
  `TransportTimeoutError` 直接抛给调用方,不走 `(False, None)` 契约;
  修复与 §三 合并(三 API 收进 `_execute(is_write=True)`)。
- **附带计数不同源**:General Fault 等手动 `_set_error` 计 `error_count`
  不经事务,与内层逐笔 `transactions` 计数不同源——收进 `_execute` 后消失。

### 2.4 绕过形态 C:声明为读、实际能写(语义错配)

| # | 入口 | 位置 | 亲验证据 |
| --- | --- | --- | --- |
| C1 | AB `generic_message(service, ...)` | `plc/ab/ab.py:334`,硬编码于 `:356` | 入参是**任意 CIP service**,却写死 `is_write=False` → 传 CIP 写服务即写设备,只读闸不拦、重试取读口径 |
| C2 | 读码器 TCP `command(cmd_type, cmd, param)` | `reader/hikrobot_tcp.py:490` | 显式接受 `Set`/`Exec`(`:473-480` 校验),`_execute(...)` **未传 `is_write=True`**;同文件 `set_acquisition()` 却传了(`:426-429`)——同一内部方法两处口径相反 |

- C2 的次生风险:`Exec` 为动作型命令(`Reboot`/`TriSoft` 等),当用户为读设了
  `retries>0` 时,该命令会**按读重试口径被重发**(非幂等)。
- C1 的次生风险同款:重试取 `retries` 而非 `write_retries`(默认 0)。

### 2.5 后果与定级依据

| 场景 | 后果 |
| --- | --- |
| 运维按文档开 `read_only=True` 做"只读观察期" | AB 通用写服务与读码器 TCP `Set`/`Exec`(C1~C2)**仍会发出并改设备侧状态**;批量/掩码/随机写被 `_execute` 总闸拦下、读码器握手被内部公共 `write` 入口拦下(复核批注①订正)——README 开篇安全警示对 C 形态两条仍防不住 |
| 按文档开 `write_whitelist=True` 限权 | 白名单只约束 `write()`/`write_string()`,批量入口可写任意地址 |
| 事故追查 | 握手级失败(General Fault 等)经手动 `_set_error` 记账不经外层事务、外层无 `_execute` 挂点(内层逐笔 `transactions` 照计)——收进 `_execute` 后事务/快照语义统一 |

**定级 P0 的理由**:不是"少了一道可选校验",而是
①已发布文档的安全承诺失真(白名单全量失真;`read_only` 对 B/C 五条失真);
②失真的方向是**"以为拦住了、实际没拦"**,对生产设备是危险侧的沉默失败;
③触发无需任何异常条件,正常调用即触发。

### 2.6 修法建议(一次收根,不建议逐点打补丁)

**根因**是"写语义靠逐点声明"。建议的收口方向(实施时按此评估,非强制形态):

1. **写法收口**:把"哪些方法是写"变成可在类型层面强制的事实——例如
   在 `BaseClient` 元类/`__init_subclass__` 层维护"写方法登记表",
   驱动覆写 `write_many`/`write_batch`/`random_write`/`write_mask_register`/
   `read_write_registers`/`*_wstring` 时强制过闸;新增写方法漏登记即门禁红。
2. **批量入口按 item 过闸**:白名单是**按地址**的,批量入口必须逐 item 校验
   (不能整批一次判)——注意"入参期抛 `ValueError`、零字节发送"的既有契约
   (`plc/modbus/modbus.py:492-506` 的口径)须保持。
3. **B1~B3 进事务层**:读码器三个 API 改走
   `_execute(lambda: self._xxx_locked(...), is_write=True)`,失败语义由
   基类统一转 `(False, None)` + `last_error` 三件套;若产品裁决要保留异常语义,
   须在 `docs/architecture.md` §5 显式登记为例外(与 §三 P0-2 合并处理)。
4. **C1/C2 按语义判写**:`generic_message` 按 CIP 服务码判定(0x4D/0x4E/0x4F
   等写服务)并允许调用方显式覆盖;`command` 按 `cmd_type` 分流
   (`Get` 读 / `Set`·`Exec` 写)。
5. **补"全库写入口"通杀守卫**(性价比最高,建议第一个写):用反射枚举所有
   公开写方法(`write_*`、`*_write`、`random_write`、`write_batch`、`scan`、
   `command`、`trigger`…),逐个断言 `read_only=True` 下**零字节发送**。
   否则下一批新增写方法仍会漏——这是本轮 13 条批量/随机/掩码入口全部绕过白名单闸的直接教训。

**回归用例清单**(实施时逐条落地):
- **既有行为补测锁定**(复核批注①:行为已在,缺用例):A 形态 13 入口在 `read_only=True` 下零字节拒发(`_execute` 总闸抛 `RuntimeError`,与 `write()` 同款);
- `write_whitelist=True` 下 `write_many` 表外地址拒发(表内放行);
- B1~B3 失败返回 `(False, None)` 且 `last_error` 有原因、`connected` 不拆连;
- C1 传写服务被 `read_only` 拦;传读服务放行;
- C2 `command("Exec", ...)` 在 `read_only=True` 下拒发、在 `write_retries=0` 下不重发;
- aio 层逐条镜像同断言(见 §五 P0-3)。

---

## 三、P0-2 读码器一族公共 API 直接抛自定义异常(契约例外未登记)

### 3.1 契约与实现

**契约**:公共 API 不抛自定义异常,读返 `(False, None)`、写返 `bool`,
原因进 `last_error`(`docs/architecture.md:363`;`core/base_client.py:8-9` 模块头);
内部异常"不逃逸到调用方"(`docs/architecture.md:420-424`)。

**实现**(亲验原文):

```
# reader/hikrobot.py:258-262
message = _("读码器内部故障(General Fault),请排查后调用 clear_error() 清除")
self._set_error(message, ErrorCategory.DEVICE, 0)
raise DeviceError(message, 0)
```

`DeviceError` 是 `OmniPLCInternalError` 子类,直接抛给调用方;`clear_error()`
与 `read_status()` 同族。**该驱动的 docstring 把逃逸写成了承诺**:
`:raises TransportTimeoutError:`(`reader/hikrobot.py:211`)、
`:raises DeviceError:`(`:212-214`、`:305`、`:327`)。

代码自身也承认绕开了事务层:`reader/hikrobot.py:403-409` 注释原文
"本方法不经 `_execute`,裸 raise 会漏 last_error"。

**aio 层 1:1 上移**:`ABaseClient._run` 不做任何异常转换
(`aio/__init__.py:252-256`),转发点 `:1634`(scan)、`:1638`(read_status)、
`:1644-1646`(clear_error)。**同步层与异步层行为一致地违约。**

### 3.2 附带缺陷:`last_error_code` 直写成 `0`

四处在 `_set_error` 第三参直接写 `0`:
`reader/hikrobot.py:261`、`:291`、`:408`、`reader/hikrobot_tcp.py:442-446`。
而契约是"无具体错误码 = `None`"(`core/base_client.py:664-670`;
`docs/architecture.md:385`;`_extract_code` 用 `exc.code or None`,`core/base_client.py:1650-1651`)。
**同文件另两处已写 `None`**(`reader/hikrobot.py:274`、`:414`)→ 笔误级不一致。

后果:同一种"设备侧无码失败",经异常抛出时 `last_error_code is None`,
经 `_set_error` 直写时是 `0`;上位机按"`code is not None` 即视为 PLC 报码"
分流的逻辑会误判。且 §3.1 若按修法 3 收进 `_execute`,这四处值会从 `0` 变 `None`,
**行为随修法漂移**——两侧都要一致化。

**建议单点收口**:在 `_set_error` 内做 `code = code or None`(与 `_extract_code` 同口径),
四处自动治好,且新增调用点不会再犯。

### 3.3 待裁决(不预设结论)

读码器握手级失败**是否应保留异常语义**——两种都能自圆其说:

- **收进契约**(推荐方向):与同族 `KeyenceSrClient`/`HikrobotIdTcpClient`
  一致,调用方无需为读码器写特殊分支;
- **登记例外**:理由是其失败语义(握手/状态位等待)**不是一次读事务失败**,
  且"能应答错误码即证明链路活着"的分流在 `_execute` 内会被 `DeviceError`
  分支吞成 `(False, None)`,丢失"设备内部故障"与"普通读失败"的区分度。

**若选后者,必须在 `docs/architecture.md` §5 显式登记"读码器握手级 API 为例外"**,
并同步 aio 文档与 `SKILL.md`——否则 §三 与 §二 同性质:承诺与实现不一致。

---

## 四、P1 契约一致性 5 项

### P1-1 心跳 tick 的传输失败会覆盖"事故现场报文快照"

- **文档承诺**:`incident_frames` docstring "`_execute` 在 DeviceError / 拆连级传输
  失败时点把黑匣子当前留存拷贝到本快照(**心跳失败不覆盖**)"
  (`core/base_client.py:1191-1194`);`docs/architecture.md:1157` v0.56.0 条目同口径。
- **实现**(亲验原文 `core/base_client.py:1471-1476`):

  ```python
  except (OSError, OmniPLCInternalError) as exc:
      self._set_error(_describe(exc), _categorize(exc), _extract_code(exc))
      self._capture_incident()      # ← 无 heartbeat 守卫
      self._mark_disconnected()
  ```

  上一分支 `DeviceError` 确实守了 `if not heartbeat:`(`:1462-1463`),
  **`OSError` 分支没有**。而心跳 tick 的传输类真实故障(OSError)按设计
  **照常计数拆连**(`docs/protocol-features.md` 心跳节"传输类真实故障不受此限"),
  即该分支在心跳场景**必然可达**。
- **后果**:后台每 30 秒一次的心跳若发生一次拆连,就会把上一次真实事故的报文切片
  冲掉——而"心跳不覆盖"正是 v0.56.0 黑匣子取证能力的卖点。
- **native 同款**:`native/base.py:1210-1221`,且其 docstring 声明"语义同同步"(`:1050`)。
- **修法**:两处各加 `if not heartbeat:`(共 2 行)。
- **亲验**:✔ 主会话读原文确认。

### P1-2 `TransportTimeoutError → code=None` 靠 13 个调用点手写 `, 0` 维持

- **契约**:分类表 `TransportTimeoutError` → category `TIMEOUT`、code `None`
  (`docs/architecture.md:382`),理由"传输超时无协议码"。
- **实现**:`_extract_code` 为 `exc.code or None`(`core/base_client.py:1650-1651`),
  故不变式**完全依赖每个构造点手写 `, 0`**。已逐个核对 13 处:
  `transport/udp.py:133`、`:161`;`transport/serial.py:199`;
  `native/transport.py:543`;`reader/keyence_sr.py:195`;
  `reader/hikrobot.py:275`、`:415`;`reader/hikrobot_tcp.py:512`、`:563`;
  `reader/hikrobot_serial.py:308`、`:336`;`reader/hikrobot_sdk.py:1233`。
- **风险**:任一处改传非 0 码(或被复制粘贴成 `DeviceError(code)`)即出现
  "TIMEOUT + 非 None code"的组合,**无任何测试锁定该不变式**。
- **修法**:在 `_extract_code` 内对 `TransportTimeoutError` 强制归 `None`
  (类型级保证),并补一条断言 `TIMEOUT ⇒ code is None` 的门禁用例。

### P1-3 §5 分类表的"UNKNOWN 兜底行"实际不可达(文档与实现漂移)

- **文档**:§5 表末行"其他(含裸内部异常)→ `UNKNOWN` / `None`"
  (`docs/architecture.md:387`)。
- **实现**:`_execute` 只对三类异常调 `_categorize`——
  `TransportTimeoutError`、`DeviceError`、`(OSError, OmniPLCInternalError)`
  (`core/base_client.py:1444`、`:1455`、`:1471`)。**非 OSError、非
  `OmniPLCInternalError` 的异常(`RuntimeError`/`KeyError`/`struct.error`/`ValueError`)
  直接穿透,从不进分类表**;`UNKNOWN` 兜底实际只经 `connect()` /
  `_after_connect` 的宽 `except Exception` 可达
  (`core/base_client.py:267-277`、`:286-293`)。
- **影响**:读者按 §5 表推断"任何失败都有 category"会落空;
  而"裸内部异常"在本库**恰恰是要暴露的编码错误**(参数校验抛 `ValueError` 是有意设计)。
- **建议**:订正 §5 表**而非**加 `except Exception` 兜底——加兜底会把编码错误
  也吞成 `UNKNOWN`,与"参数非法抛 `ValueError`"的既有口径冲突。

### P1-4 `read_many`/`write_many` "逐点容错"语义在不同驱动上不一致

- **文档**:§5 表"`read_many`/`write_many` 逐点独立容错的结果列表;
  单点失败不影响其他点"(`docs/architecture.md:369`)。
- **实现**:基类为列表推导(`core/base_client.py:770-784`、`:850-860`),
  **任一非法地址/类型抛 `ValueError` 会中断整批**——而写场景下"前面的写已发出,
  调用方却拿不到部分结果"。Modbus 覆写做了入参前置校验(零字节发送,
  `plc/modbus/modbus.py:492-506`)故不受影响。`plc/melsec/melsec.py:316-320`
  的注释自认"codec `ValueError` 穿透 `_execute`(其不捕 `ValueError`)";
  `core/monitor.py:597-601` 亦依赖该行为。
- **影响**:同一 API 在 Modbus 与 MC/FINS 等驱动上语义不同:**前者"先校验后发",
  后者"边发边炸"**。写场景下是不可忽略的部分写入风险。
- **建议**:二选一并全库统一——①基类 `write_many` 改为"入参先全量校验再逐点执行"
  (与 Modbus 对齐,零字节前置拒绝);②或 §5 表改写为"入参非法抛 `ValueError`,
  不保证部分结果"并逐驱动披露差异。**推荐 ①**(写安全侧更保守)。

### P1-5 「能力缺失」两种口径并存

- `read_range` 在无块读原语的驱动上抛 `ValueError`
  (`core/base_client.py:841-848`、`native/base.py:703-720`);
- 而同性质的**字符串能力缺失**走 `DeviceError` → `(False, None)`
  (`core/base_client.py:1571`、`:1578`)。
- §5 把"参数校验错误(非法地址/未知类型/范围越界/未绑定点位名)"定义为 `ValueError`,
  但**未定义"驱动不支持该能力"属于哪一类**。
- **建议**:统一为一种(我倾向 `ValueError`,理由:能力缺失是调用方需在编码期
  发现的错误,静默返回 `(False, None)` 会让人误判为设备不支持),
  并在 §5 增一行口径。

---

## 五、P0-3 / P1 测试面缺口

### P0-3 8 个 aio 镜像类零测试引用,且镜像守卫不校验接线

**实测命中数**(`rg -c '\bA<类名>\b' tests/`,亲验):

| aio 类 | tests 命中 | 同步侧对照 |
| --- | --- | --- |
| `AHikrobotIdModbusClient` / `AHikrobotIdTcpClient` / `AHikrobotIdSdkClient` / `AHikrobotIdSerialClient` | **全部 0** | `test_hikrobot*.py` 共 71 例 |
| `AMelsecMcUdpClient` | **0** | 12 |
| `AKeyenceHostLinkUdpClient` | **0** | 8 |
| `APanasonicMewtocolUdpClient` | **0** | 4 |
| `AToyopucUdpClient` | **0** | 4 |
| `AModbusRtuClient` / `AOmronFinsTcpClient` | 1(仅 `test_smoke.py` 构造冒烟) | 29 / 16 |

**为什么"零测试"在这里等于"接错也看不见"**(关键机制,亲验):

1. 镜像守卫 `tests/unit/test_aio_mirror_surface.py:56-68` 只比对 `dir()` 名字集,
   `:107-146` 只比对方法签名——**都不校验 `A*.__init__` 里注入的同步类是不是对的**;
2. `ABaseClient._typed()` 的类型断言(`aio/__init__.py:241-250`)**只在真正调用
   方法时才执行**,构造期不校验;
3. 故"`AHikrobotIdXxx` 复制粘贴时注入了错误的同步类(TCP↔UDP、Modbus↔TCP)"
   这类缺陷在当前测试网下**完全不可见**——而这恰好是 §二 那 18 条闸外写路径最容易
   被修错的形态。

**同源风险**:4 个 aio **UDP** 类零引用,而 UDP 走线的关键契约是"发送前排空陈旧
数据报"(`BaseTransport.drain`,同步侧已锁死:`test_fins_clients.py:118-142`
断言 `scripted.events == ["drain","send"]`)。aio UDP 类若接错成 TCP 同步类,
**`drain` 语义静默消失而全部守卫仍绿**。反证:`AKeyenceMcUdpClient` 因
`test_keyence_mc_clients.py` 收了 UDP 而幸免。

**建议补两条通杀守卫**(一次覆盖全库,防止下一批再漏):

```python
# tests/unit/test_aio_mirror_surface.py 增补
def test_async_client_wires_expected_sync_class():
    """每个 A* 类注入的同步实例类型 == 去掉 'A' 前缀的同名同步类。"""
    # 反射 34 个 A* 类构造(用各自最小合法参数或直接读 __init__ 默认值),
    # 断言 type(client._sync).__name__ == cls.__name__[1:]
```

```python
# tests/unit/test_aio_udp_mirror.py(新增)
@pytest.mark.parametrize("cls", [AMelsecMcUdpClient, AKeyenceHostLinkUdpClient,
                                 APanasonicMewtocolUdpClient, AToyopucUdpClient])
async def test_aio_udp_drains_stale_datagram(cls): ...
    # 断言 ①scripted.datagram is True ②events == ["drain","send"]
    #      ③读回值取自本轮分片而非陈旧帧 ④type(client._sync) 为对应 UDP 同步类
```

### P1-6 负错误码计数豁免:注释承诺、零用例锁定

- **契约**:`core/base_client.py:1464-1469` 原文"只计 PLC 明确返回错误码的次数:
  `code=0` 的无码失败…与**负码诊断**(本地缓冲/配置问题,如 UDP 报文超长 -10040)
  不计入"。判定条件是 `code is not None and code >= 0`。
- **测试现状**:只锁了 `code=0` 分支(`test_v030_reliability.py:731-749`);
  **负码**只在传输层被断言(`test_transport.py:239-241`、
  `test_native_transport.py:253-276` 断言 `excinfo.value.code == -10040`),
  **没有任何用例把负码 `DeviceError` 喂进 `_execute` 断言 `device_error_count == 0`**。
- **风险**:`code >= 0` 是唯一把"本地诊断失败"与"PLC 报错"分开的门。若被改成
  `code is not None`,现场会看到设备错误率虚高,而**现有测试全绿**。
- **建议补**:`test_negative_code_not_counted_as_device_error` —— 注入
  `DeviceError("UDP 报文超长", -10040)`,断言 `device_error_count == 0`、
  `error_count == 1`、`last_error_code == -10040`、`connected is True`、
  `last_error_category is ErrorCategory.DEVICE`。

### P1-7 aio 层普遍只有 happy-path,且缺 aio↔sync 边界对拍

- **实测**:逐文件 `asyncio.run(` 计数 vs 用例数:`test_mc_clients.py` 52→**1**、
  `test_kv_clients.py` 29→**1**、`test_siemens_s7_clients.py` 69→**1**、
  `test_inovance_mc_clients.py` 16→1;仅 `test_modbus_clients.py`(127→11)、
  `test_v030_reliability.py`(33→8)、`test_write_safety.py`(28→7)较厚。
- **缺口**:项目已为 native 建立**边界对拍范式**
  (`test_native_edge_parity.py:177-183` 断言"同一边界入参在两层给出完全相同的
  `(kind, exc_type, str)`",26 组合),但**aio↔sync 无同类对拍**。
  而 `ValueError` 的抛出时机("入参期就抛" vs "连上再失败")在 aio 手写转发里
  是最容易漂移的点。
- **建议补**:`tests/unit/test_aio_edge_parity.py`,复用 `_cases()` 表结构,
  至少覆盖 modbus/melsec/fins/inovance/s7/ab/opcua 七族。

### P1-8 aio 独有私有基类 `_AFinsRoutingClient` 无任何专门用例

- `aio/__init__.py:2071-2129` 是 **aio 独有**的私有基类(无同步孪生),
  含 6 个路由 property + 3 个 async 转发(`read_cpu_unit_status`/`read_clock`/`write_clock`);
- `AOmronFinsTcpClient` 在 tests 中命中 **2 次,全部在 `test_smoke.py:134-135`**(构造 + close);
- 同步侧对应能力均有用例(`test_fins_clients.py` 49 例,含 0601/0701);
- 镜像守卫**天然兜不住"没有同步孪生"的这一层**;
  `destination_node`/`source_node` 在自动推导模式下是**握手后才刷新**的值
  (`aio/__init__.py:2089`、`:2104` docstring 明示),错接线只能靠行为用例发现。
- **建议补**:`test_aio_fins_clock_roundtrip`、`test_aio_fins_routing_props_forward_after_handshake`、
  `test_aio_fins_rejects_foreign_sync_instance`(对应 `:2078-2079` 的 `TypeError`,当前无用例)。

### P1-9 `AModbusRtuClient.configure_serial` 等"同步透传豁免项"永不被测

- `test_aio_mirror_surface.py:40` 把 `configure_serial` 列入**同步透传豁免**白名单;
  "豁免"的实际含义是**永不被测**。对照:`AInovanceRtuClient.configure_serial`
  有用例(`test_inovance_clients.py:456-462`),而 `AModbusRtuClient` 没有
  → **同类能力覆盖不对称**。
- **建议补**:`test_modbus_clients.py::test_async_rtu_configure_serial_defaults`
  (断言 `AClient.configure_serial("COM3")` 后 `sync._serial_config.baud_rate == 115200`、
  `stop_bits == 1`,与同步侧同断言)。

### P1-10 opcua 测试 skip 口径不一致(违反"两腿各自全绿"约定)

- 同文件 24 处 `@pytest.mark.skipif(not _HAVE_ASYNCUA, ...)`(`test_opcua_client.py:631-1310`)
  + 1 处 fixture 级 skip(`:621-623`);
- 但 `test_build_data_change_filter`(`:109-121`)在**函数体内** `import asyncua.ua`,
  **无守卫** → 无 asyncua 环境下是 **ImportError 失败**而非 skip;
- 违反 `CONTRIBUTING.md`「依赖线差异按需跳过…保证两条腿各自全绿」。
- **建议**:给 `:109` 加同款 skipif,或改 `pytest.importorskip("asyncua.ua")`。
- **附带**(未核实):`_OPCUA_TEST_PORT` 在 import 期 `bind(0)` 后立即 `close()`
  选定端口(`:552-555`),module 级 fixture 到 `server.start()`(`:563`)之间
  存在端口被抢占窗口;未观察到实际发生。

### P1-11 `@pytest.mark.timeout` 全库 0 处(120s 上限对真服务端用例同样生效)

`pyproject.toml:90-93` 写明"真机/慢链路用例如需放宽,就近用
`@pytest.mark.timeout(...)`",但全库实测 **0 处**。即 OPC-UA 真 asyncua 服务端
用例与模拟器联测用例都受 120s 硬顶——CI 抖动下的假红风险未按设计手段缓解。

---

## 六、P2 文档与工程基建(机械活,零风险)

### P2-1 文档"魔数"全线漂移(实测值对照)

| 声明位置 | 文档写的 | 实测 | 亲验 |
| --- | --- | --- | --- |
| `README.md:13`、`README.md:73`、`SKILL.md:9` | "32 个同步客户端" | **34** | ✔ |
| `docs/examples.md:194` | "包装层 32 客户端全镜像" | 34 | ✔ |
| `docs/architecture.md:16`(§1 分层图) | "28 个同步 + 28 个异步" | 34 / 34 | ✔ |
| `docs/architecture.md:237`、`:1310` | "30 个同步具体类 + 30 个异步镜像类" | 34 / 34 | ✔ |
| `docs/architecture.md:1319` | "原生异步…覆盖五协议 8 个" | 5 族 **9** 个客户端 | ✔ |
| `docs/architecture.md:3`(抬头) | "版本:v0.54.0" | pyproject **v0.56.0**(差 2 个 minor) | ✔ |
| `CONTRIBUTING.md:53` | "当前 1699 例" | 1529 个 `test_*`(末次 CHANGELOG 记 1811 passed)——三个数不同源 | ✔ |

**根因**:仓库已有完善的"代码面守卫"传统(`test_package_exports.py` 导出面、
`test_aio_mirror_surface.py` 签名、`test_manual_common_smoke.py` 对 tools 脚本做
AST 冒烟),**但没有任何测试读文档**——实测 `rg 'examples\.md|README\.md|architecture\.md' tests/`
仅 2 处命中,且都在 docstring 里提到文档名。

**建议**:新增 `tests/unit/test_docs_consistency.py`(门禁内、零网络),一次防住本轮
全部漂移:

1. 扫 `README.md`/`SKILL.md`/`examples.md`/`architecture.md` 的"N 个客户端 /
   N 个同步类 / 覆盖 N 协议 N 个",与 `len([n for n in omniplc.__all__ if n.endswith("Client")])`、
   `len(omniplc.native.__all__)` 对拍;
2. `architecture.md` 抬头版本 == `omniplc.__version__` == `pyproject.toml` version;
3. 把 `examples.md` 的 python 代码块抽出 AST 解析,断言其中的 `read_*`/`write_*`
   方法名与构造类名都在真实公开面上(v0.31.2 已手工做过一次 AST + 真实签名审计,
   把它固化成用例);
4. `docs/protocol/` 下每个子目录都出现在 `docs/protocol/README.md` 的表里(防 §七 的孤儿目录复发);
5. `tools/manual_test.json` 的 `driver` 去重集合 ⊆ `test_manual_common_smoke.py`
   的驱动清单(见 P2-3)。

### P2-2 `docs/protocol/beckhoff/` 是孤儿目录

`docs/protocol/beckhoff/` 下有 `Beckhoff_TwinCAT2_ADS_TX1000.pdf`(7.8MB)与
`Beckhoff_TwinCAT3_ADS_Basics_TE1000.pdf`(4.8MB),但 `docs/protocol/README.md`
的「目录结构」与「已收录」表**都没有 beckhoff**(ADS 驱动已于 v0.52.0 移除,
索引随批删除,物料留下)。**后果**:未来重启 ADS 立项时无人知道依据在哪,
且与索引表"目录结构"节自述不一致。**建议**:或加一行标"归档(驱动已移除,
重启立项用)",或删除。

### P2-3 `tools` 冒烟白名单漏 `ab_eip`(与该用例的唯一存在理由冲突)

- `tests/unit/test_manual_common_smoke.py:1-8` 自述"把 **27 个驱动**的
  `build_client` 构造全部冒烟…S7 位序错位曾潜伏五个版本";
- 实际 `_connections()`(`:27-60`)只列 **25** 个,**漏 `ab_eip`**;
- 而 `tools/manual_common.py:504` 明确支持 `if d == "ab_eip"`,
  `tools/manual_test.json:1102` 也配了该驱动。
- **后果**:该用例的唯一价值就是堵 tools 构造漂移盲区,漏项使
  `AllenBradleyEthIpClient(ip, port, slot, connected_messaging, rpi_us)` 的
  参数顺序/默认值回到无守卫状态。
- **建议**:补 `{"driver": "ab_eip"}` 与带 params 的两行,并加
  `test_driver_list_matches_manual_test_json`(见 P2-1 第 5 条)。

### P2-4 `docs/todo.md` 首行被插入空格(基线时点观察,已消解)

基线工作区 `git status` 有 ` M docs/todo.md`;审查时点实测文件前 5 字节为
`b' # \xe5\xbe'`(行首多一个空格,标题文本本身不变),Markdown 一级标题
失效,且该 diff 会一直污染后续提交。

**复核批注(2026-10-10)**:当前工作区与 HEAD(`8c60133`)首行均为无前导
空格的 `# 待办清单(todo)`,`git diff` 亦不含首行改动——该脏项已随 todo
登记批的编辑消解,**无需再处理**。

### P2-5 `docs/protocol/` 手册只存本机,无取得渠道与校验清单

`.gitignore:152-154` 排除 `docs/**/*.pdf|pptx|ppt`(与 `CONTRIBUTING.md`
"PDF 仅本地留存不入库"口径一致)。代价是全库 40+ 处「待核/待补/待真机」的
**裁决依据只存在于单机磁盘**,丢失后无法按图索骥。
**建议**(不改现有不入库口径):在 `docs/protocol/README.md`「已收录」表加两列
——**取得渠道**与 **SHA256**(或文件版本号/发布日期),使"引用可回溯"不依赖本机磁盘。

### P2-6 依赖与 CI 面两处

- `pyproject.toml:62` `opcua = ["asyncua==1.1.5"]` 用 `==` 精确钉版 → 下游
  与其他 OPC-UA 工具共存时直接冲突。**建议** `>=1.1.5,<2` 或 `~=1.1.5`,
  把钉版留给 `uv.lock`(项目已有 lock 保证 CI 可复现,`==` 提供的额外确定性有限)。
- CI 版本矩阵 `["3.7.9", "3.12"]`(`.github/workflows/ci.yml:38`),
  `classifiers` 到 3.12 为止,无 3.13/3.14 腿。**建议**加一条 3.14 腿(哪怕只跑
  pytest),提前暴露标准库/C 扩展行为变化。

### P2-7 测试基建三处重复与脆弱点

1. **`_mount()` 同一实现逐字复制 11 份**(docstring 都相同):
   `test_ab_ethip_clients.py:73`、`test_inovance_clients.py:25`、
   `test_inovance_mc_clients.py:57`、`test_keyence_mc_clients.py:56`、
   `test_mc_clients.py:67`、`test_mc_serial_clients.py:137`、
   `test_modbus_clients.py:1855`、`test_omron_cip_clients.py:54`、
   `test_panasonic_mc_clients.py:57`、`test_panasonic_mewtocol_clients.py:41`、
   `test_sr_scanner.py:29`;同类还有 `_chunks*` 8 份、假 `_create_transport` 8 份、
   `_make_client` 5 份。**建议**上提到已被 31 处 import 的 `tests/unit/scripted.py`。
2. **4 处"裕量型"硬编码 sleep**(高负载/CI 抖动即假红):
   `test_v034_reliability.py:257`(uniform=0.05 + `sleep(0.08)`,裕量 **30ms**)、
   `:312` 同款;`test_v030_reliability.py:394`(`sleep(0.15)`)、`:417`。
   **反证**:`test_monitor.py:657` 已写明"真线程;`Event` 等待,**零固定 sleep**"
   ——项目已有更稳范式(`_wait_until` 轮询),这 4 处属可回收技术债。
3. **Monitor 行为验证几乎全建立在打桩 `read_many` 上**(`test_monitor.py` 内
   `monkeypatch.setattr(client, "read_many", fake)` 出现 14 次)。Monitor 与
   真实客户端失败面(拆连、超时、`DeviceError` 分类)的联动仅 `:782` 一条触及。
   **建议**补一条 `test_monitor_sees_transport_timeout_as_stale_but_link_kept`
   ——真 `ModbusTcpClient` + `ScriptedTransport` 注入 `TransportTimeoutError`,
   断言 `quality is STALE` 且 `client.connected is True`。

### P2-8 `MonitorQuality` 成员全集无守卫

每个状态跃迁都锁得很细(`test_monitor.py:117`/`:130`/`:156-176`/`:364-379`),
`MonitorStats` 有硬编码键集契约(`:635-653`),但实测 `__members__` 零命中
——**没有任何用例断言 `MonitorQuality` 恰好是三态**。三态是"点位快照是否可信"
的公开语义,新增第四态会静默改变下游 `if quality is GOOD` 的判断面。
**建议**:`assert {q.name for q in MonitorQuality} == {"INITIAL", "GOOD", "STALE"}`。

### P2-9 黄金帧采样来源双轨

`tests/golden/*.json` 只有 Modbus(19)/MC(6)/FINS(5)三族,由**独立生成器**产出
(`tests/golden/README.md:42-44` 明示"生成器独立实现以规避同源偏差");
而 AB/CIP、西门子、MEWTOCOL、EZSocket 的"golden"是**文件内联常量**
(`test_siemens_s7_clients.py:26-210`、`test_ab_codec.py:52-512`、
`test_panasonic_mewtocol_clients.py:48`、`test_ezsocket_clients.py:86-122`)。
内联向量**无法核验是否与实现同源**,README 的"独立计算校验"约定对这批不适用。
**建议**:至少在文件头标注向量来源(手册算例页码 / 真机抓包 / 参考实现逐字节对比),
使"是否同源"可判断。

---

## 七、经核不成立 / 已撤销(避免下轮重复投入)

| # | 曾疑似 | 核验结论 |
| --- | --- | --- |
| 1 | aio 层缺 `write_and_verify`/`wait_value`/`read_only`/`write_whitelist` 镜像 | **不成立(修复批二次复核)**:四者镜像均在——`write_and_verify`/`wait_value` 转发(`aio/__init__.py:602`/`:618`)、`read_only`/`write_whitelist` 转发(`:579-595`);原稿"ABaseClient 无转发属性"系核查疏漏,全项撤销(转发行为已加用例锁定) |
| 2 | native 层 `_execute` 分类与同步层漂移 | **不成立**:native 直接 `from ..core.base_client import _categorize, _describe, _extract_code`(`native/base.py:55-59`),分类映射逐条一致,这是**正确的复用设计** |
| 3 | RMW 位写(Modbus/MC/FINS/S7)会重复写 | **不成立**:整条"读-改-写"在**同一个** `is_write=True` 事务内,默认 `write_retries=0` 不重发;开后重跑整条的风险已在 `core/errors.py:91-94` 披露 |
| 4 | 驱动自定义异常类普遍逃逸 | **不成立**:全库**仅 1 个**驱动自定义异常类 `S7ProtocolError`,且继承 `ProtocolFrameError`(合规);1313 处 `raise` 逐类归并后仅读码器一族例外(§三) |

---

## 八、可疑但需人工裁决(不预设结论)

1. **读码器握手级失败是否保留异常语义** —— 见 §3.3(两种都能自圆其说,选任一种都必须消除"文档承诺与实现不一致")。
2. **`_narrow` 静默失败不写 `last_error`** —— 底层读成功但驱动返回类型不符时返
   `(False, None)` 且 `last_error is None`(`core/base_client.py:1673-1697`,
   注:此时 `_execute` 已把三件套清空);同场景 `read_string` 却写 `UNKNOWN`
   (`:936-946`)。前者 docstring 自称"不伪造通信错误"。与 §5"失败原因一律记录"
   冲突,需统一口径。
3. **`Monitor.get()` 抛 `KeyError`** vs 基类点位不存在抛 `ValueError`
   (`core/monitor.py:531-534` vs `core/base_client.py:1116`/`:1120`)——
   同类"名字不存在"两种异常类型,是否统一?
4. **`ab.py` 读路径的运行期 `ValueError`**(标签实际类型不是请求的 BOOL 时抛出,
   函数体于 `plc/ab/ab.py:822`)——属**运行期设备属性不符**而非入参非法,会穿透
   `_execute`;是否改 `DeviceError`?(**未核实**:能否经 `read()` 单点路径到达未展开验证)。
5. **`scan()` NG 无原因** —— `reader/hikrobot.py:294-296` 在 Results NG 时
   `return False, None` 且此前成功读已清空三件套 → `last_error is None`;
   同族四个读码器都显式记 DEVICE"无读出"(`reader/keyence_sr.py:140-147`、
   `reader/hikrobot_tcp.py:371-379`、`reader/hikrobot_serial.py:218-226`、
   `reader/hikrobot_sdk.py:1088-1093`)。疑为漏改,**未核实**(未找到注释/变更记录佐证)。
6. **`UNKNOWN` 桶语义混用** —— 回读不符 / `read_string` 类型错 / OPC-UA 用户回调
   异常都归 `UNKNOWN`(`core/base_client.py:1245-1251`、`:939-945`、
   `plc/opcua/client.py:348-354`),而 `keyence_sr` 把同性质的"设备应答无读出"
   记 `DEVICE`(`reader/keyence_sr.py:146`)。§5 表只按异常类型定义分类,
   **未定义"非异常失败"的归类口径**。
7. **`ElementTree.ParseError`(SyntaxError 子类)是否可能逃逸** ——
   `cnc/mtconnect.py:129` 抛出;读到唯一调用点有 `except ElementTree.ParseError`
   收口为 `ProtocolFrameError`(`:140-145`),**未发现逃逸路径**,
   但调用链是否 100% 覆盖**未核实**。
8. **MTConnect keep-alive 透明重试** —— 一次 `read()` 内可能发 2 次 HTTP GET
   (`cnc/mtconnect.py:254-265`),使 `retries` 的实际发包次数与文档口径不完全对齐;
   仅 GET(幂等),未见写入路径受影响(**未核实**影响面)。
9. **`_extract_code` 对负码的实现细节**、**`scripted.py` 是否覆盖 UDP `drain` 语义**
   —— 子代理自述未确认,主会话未展开(**未核实**)。

---

## 九、已锁死、无需重复投入的契约(正面清单)

避免下一轮审查重复劳动,以下契约**已有专门用例锁定**:

| 契约 | 锁定用例 |
| --- | --- |
| `ClientStats` 键集 = 声明字段 | `test_v030_reliability.py:661-666`(+隔离拷贝 `:668`、双路径导出 `:677`、初值全零 `:566`) |
| `last_error_category` 全分类表 | `test_v034_reliability.py:83-188`(DEVICE/TRANSPORT+errno/PROTOCOL/TIMEOUT/UNKNOWN/连接失败/成功后清空/文案兼容)+ `test_v030_reliability.py:764`(gaierror 仍 TRANSPORT) |
| `TransportTimeoutError` 优先级高于 `DeviceError` | `test_v034_reliability.py:124-132` |
| 超时语义分流(TCP 拆连 / 串口·UDP 不拆连) | 串口 `test_modbus_clients.py:163`、`test_transport.py:418`;TCP `test_modbus_clients.py:188-199`;native `test_native_core.py:223-231` vs `:241-248`、`test_native_modbus.py:542` |
| UDP 陈旧数据报发送前排空 | `test_fins_clients.py:118-142`、`test_native_melsec.py:762`、`test_native_omron.py:601` |
| `read_many`/`write_many` 边界 | Modbus `test_modbus_clients.py:586-800,1040-1235,1607-1810,2108-2147` |
| `read_batch`/`write_batch` 边界 | MC `test_mc_clients.py:307-372,527-531,938-981`;FINS `test_fins_clients.py:403-496`;AB `test_ab_ethip_clients.py:1077-1209`;S7 `test_siemens_s7_clients.py:520-525,798-813`;native `test_native_modbus.py:692-723,910-926,1015-1037` |
| `read_many_strict` 三层语义 | `test_write_safety.py:385-441`(sync/native/aio 各一条) |
| Monitor 质量三态跃迁 + `MonitorStats` 键集 | `test_monitor.py:117,130,141-179,364-379,584-592,629,635-653` |
| 写安全(只读总闸/非幂等写警示/verify) | `test_write_safety.py`(28 例)——**注意:其"只读总闸"用例只覆盖 `write`/`write_tag`/`_execute` 层三条路径,批量面零覆盖,见 §二** |
| native↔sync 边界入参一致性 | `test_native_edge_parity.py:177-183`(26 组合) |
| aio 镜像公开面/签名/`__all__` 双射 | `test_aio_mirror_surface.py`(6 例)、`test_package_exports.py:42-46` |

---

## 十、排期建议(合并三路结论后的推荐顺序)

| 序 | 批次 | 内容 | 依据 | 量级 |
| --- | --- | --- | --- | --- |
| **1** | `fix(写安全)` 根修 | §二 收口 A/B/C 三形态(A=白名单逐 item 过闸,`read_only` 总闸已在位只补测;aio 转发经核已在位)+ **写方法登记表守卫** | §二 | 1~1.5 天 |
| **2** | `fix(契约)` 收口 | §四 五项(`_set_error` 单点 `code or None`、`_extract_code` 收 `TransportTimeoutError`、心跳快照 2 行守卫、§5 表订正、能力缺失口径统一) | §四、§3.2 | 半天 |
| **3** | `test(守卫)` | §五:`_sync` 接线通杀守卫 + aio UDP/读码器镜像行为用例 + 负码计数用例 + opcua skip 一致化 + 30ms 裕量 sleep 换 `_wait_until` | §五、§六 P2-7 | 1 天 |
| **4** | `test(docs)` | §六 P2-1 `test_docs_consistency.py` + P2-3 驱动集对拍 | §六 | 半天 |
| **5** | `chore` | §六 其余(beckhoff 归属、`_mount` 11 份上提、`asyncua` 钉版、3.14 CI 腿、手册清单加渠道/SHA256、内联黄金帧标来源;todo 首行空格已消解,见 P2-4) | §六 | 半天 |

**第 1、2 批建议合成一次提交序列**——共用同一套"写入口"用例,否则回归测试要写两遍。

**修复批落地时的门禁要求**(本轮未实跑,须在修复批补齐):
`uv run --extra dev python -m pytest tests -q`(3.7.9)/ `uvx ruff format --check src tests` /
`uvx ruff check src tests` / `uvx --python 3.12 mypy src/omniplc` / `uvx ty check src/omniplc`,
并补 3.12 腿实跑。新增/改动用例须在各批次内落地,不后置。

---

## 十一、审查方法学附记(供下轮复用)

1. **两个切片互相印证的价值**:契约切片找出了"路径 B(不进 `_execute`)",
   测试切片找出了"路径 D(镜像接线无守卫)"——单看任一路都拼不出完整图景。
   下轮建议固定三切片:**写面语义 / 契约口径 / 测试-文档守卫**。
2. **"守卫测试的守卫"是最高杠杆的补测方向**:本项目已有导出面守卫、签名守卫、
   tools 脚本冒烟守卫,但**恰好缺"文档 ↔ 代码"与"镜像接线 ↔ 类型"两条**,
   而本轮 4 个切片里有 2 个的根因都落在后者。
3. **反例优先**:本轮每条"文档承诺"都做了正反对照(承诺出处 + 实现出处),
   4 项疑似问题据此撤销(§七)。下轮维持该口径,避免台账注水。

---

> **登记与排期**:§十 第 1/2 批(写安全根修 + 契约五项)已于 2026-10-10
> 修复批落地(`58ceb9e`,门禁五件套全绿,见卷首实施批注);第 3~5 批
> (测试守卫扩展/文档对拍/chore)待排期。本文件按惯例存活至发版后删除。
> **真机核验**:本报告全部结论为静态阅读结论;修复批的现场行为
> (`read_only` 闸全入口生效、读码器握手失败契约、`generic_message`/`command`
> 判写)已登记 `docs/real-machine-checklist.md`,待真机确认一次。
