# omniplc 评价审核报告(资深工控视角)

> **基准**:v0.36.0 + **11 个修复提交** → **v0.37.0**(tag `a1564b6`)→ **v0.38.0**(`9c0d2ff`)+ **v0.39.0**(`94e1328`,2026-09-25 22:00 落地);v0.37→v0.39 期间 **14 个新提交**(`eca6e78`/`f48bb70`/`d8e2a0c`/`e23c787` + `9c0d2ff` + `6db7a9b`/`1c45545`/`148d3fa`/`6e828b5`/`4429fd9`/`9fd0111`/`c904809`/`2e152df` + `94e1328`),覆盖 convert BOOL/STRING + ClientStats TypedDict + 原生 asyncio 首批(异步核心 + 传输 + Modbus/MC/FINS)+ 审核修复 + 契约矛盾批 2 + P3 收口 + MEWTOCOL 错误码 + Host Link 分类口径,缺陷状态详见 §三、§7.4、§7.8。
> **方法**:架构 / 协议 / 测试-CI-文档 三路并行深度代码走查 + v0.36.0 增量 diff 走查 + **v0.37→v0.39 增量复审** + 缺陷修复逐项复验与修复批次代码级复核(门禁五轮实跑 + 24 点主复现脚本 + 6 点 `last_error` 终态补充断言 + 11 提交逐 diff 走查);关键结论经 `file:line` 实测复核;实测数据(行数、用例数、断言数、git 时间线、门禁实跑)来自仓库直接统计,不采信文档自述。
> **口径**:本报告为**评价审核**(技术评审),不含核价/商务评估。

---

## 一、总体结论

**综合评分:7.2 / 10**(八项分项评分等权算术均值,57.5/8 = 7.19)。项目自我定位 `Development Status :: 3 - Alpha`(`pyproject.toml:48`),诚实。

一句话定性:**协议自研深度与测试质量罕见地高,文档深度达商用水准;但真机验证闭环仅约 7%,CI 版本矩阵单薄与异步层正确性存在实质性缺口。v0.36.0 增量(FINS 加固/XXE 防御/FC23/FC43)质量扎实,交付时带出的 2 个 Modbus 批量 P1 与第一轮 5 个 P1/P2 缺陷,当日已被 11 个修复提交(随 v0.37.0 发布)收口 6/8、文档化部分收口 1 项(aio),仅余 OPC-UA 回调竞态;另由多角度自查额外发现并修复 6 处本报告未覆盖的缺陷(含 1 处静默读错数据);评审侧复核再发现 K1(Keyence MC 位组地址错位,高置信待核,§7.7)。** v0.37→v0.39 期间:convert BOOL/STRING 解码 + ClientStats TypedDict + 原生 asyncio 首批 5 客户端(异步核心 + 传输 + Modbus/MC 1E+3E/FINS)+ P3 全收口(write_bool/aio close/offset_device)+ 契约矛盾批 2(MEWTOCOL 错误码 + Host Link 分类口径 + UDP AF_INET 钉死)+ 自查 P0/P1 修复合入;K1 仍未修,K2 新发现(AB CIP session 漏注销,§7.8);门禁五轮全绿(806→826→842→861→1072)。** 属于"工程自律极强、验证闭环未完成、评审响应极快"的个人冲刺型项目(**185 commits / 8 天 / 49 版本**)。

### 规模画像(实测)

| 维度 | 实测值 |
|---|---|
| 源码 | **76 文件** / ≈**18,800+ 行**(`src/omniplc`;v0.39.0 较 v0.37.0 +6 文件:`native/` 模块五个——base/transport/modbus/melsec/omron,累计 ~1,750+ 行) |
| 测试 | **49 文件** / ≈**14,400+ 行** / **1,072 例, 0 skipped(v0.39.0 实跑全过;较 v0.37.0 842 例 +230;v0.38 +19、v0.39 +211)** / 形式主义用例 **0** |
| 协议覆盖 | **15 家品牌/协议**;28 同步 + 28 异步(thread-pool `aio`) + 5 原生异步(`native`) = **61 个客户端类** |
| 黄金报文 | **30 个 JSON**(v0.36 新增 7 个:FC23 / FC43 翻页·异常·私有对象)+ 独立实现生成器;v0.39 对拍表扩到全 10 种数据类型 + MC/FINS 字符串 + 三协议 `read_tag`/`write_tag` |
| 文档 | `architecture.md` 新增 **§12 原生异步层**(复用边界 / 超时与取消行为表 / 3.7 实测依据)+ §3 8 条 + §9 同步 × 异步对拍 + §10 asyncio 可用性;README 新增"两套异步"选型表与原生命中层用例边界;CHANGELOG 显著增厚 |
| 依赖 | 核心**零第三方依赖**(`native` 模块仍纯 stdlib asyncio);可选 pyserial / comtypes / asyncua / pyads / python-snap7;测试脚手架零新增(`asyncio.run` 包同步测试,无 pytest-asyncio) |
| 迭代 | **185 commits**、**49 个版本**(**5 个版本/天**;v0.36→v0.37→v0.38→v0.39,**8 天**(2026-09-18 → 09-25)) |
| 类型与门禁 | 1013+ 个 `def` 全标注、`py.typed`;CI 四门禁(pytest + ruff + mypy + ty)——**本轮独立实跑全部通过** |
| 真机验证 | 30 行清单仅 2 项通过 ≈ **7%**(v0.36 已在 Modbus 行补注 FC22/23/43 待真机) |

### 分项评分

| 维度 | 分 | 一句话判词 |
|---|---|---|
| 分层设计 / API 抽象 | **8** | 模板方法教科书级,异步是包装器不是复制 |
| 线程安全 | **7** | 无死锁回路,`scan` 旁路与 `close` 竞态已修,余 OPC-UA 回调竞态 |
| 错误处理 | **7.5** | 三层语义清晰;超时计数/`code=0`/SR 口径/`write_many` 终态已修并复验,余 OPC-UA;v0.39 原生层把"分类口径"作为内部契约双层一致(sync × native 守卫用例) |
| 协议栈深度(自研组) | **9** | 四大栈达生产级;Modbus 批量合并缺陷已修复并复验(§7.4);v0.39 原生 asyncio 层 5 客户端落地,同步/异步共用同一份编解码(`_transact` 边界),见 §7.8 |
| 测试质量 | **8** | 字节级断言、0 形式主义 |
| CI/CD | **7** | push/PR 四门禁 + tag 独立构建的互补触发设计合理;扣:矩阵单薄、lint 规则弱、无覆盖率 |
| 文档 | **8** | 深度超额、呈现不足 |
| 真机验证 | **3** | 29 项适用协议仅 2 项通过 |
| **综合** | **7.2** | 八项等权(57.5/8 = 7.19) |

---

## 二、技术亮点(经核实)

### 2.1 分层与抽象收口干净

- `BaseClient`(`core/base_client.py:63`)三档钩子:强制 `_create_transport/_read/_write`(`:713-730`)、可选 `_after_connect`(`:717`,FINS 握手 `omron.py:464` 与 AB Forward Open `ab.py:119` 复用同一挂点)、可选 `_read_string/_write_string`(`:732-745`,缺能力抛 `DeviceError` 而非拆线,语义正确)。
- 事务模板 `_execute`(`:614-654`)把"惰性重连 → 重试 → 错误转换"一处收口,驱动只需抛内部异常。
- `_bump_id(attr, bits)`(`:747-760`)让 MC 4E 序列号 / FINS SID / MBAP TID / Modbus TID 共用一套回绕逻辑。
- `read_many/write_many` 允许驱动覆写为协议原生单事务(MC 0406 / FINS 0104 / CIP 0x0A / OPC-UA UA Read),接口不变——"默认正确、可选优化"的分层是对的。

### 2.2 同步 + 异步双 API 不复制协议栈

- `ABaseClient`(`aio/__init__.py:152`)持同步实例,经 `max_workers=1` 的 `ThreadPoolExecutor` 投递(`:191-195`),**协议编解码只有一份代码**,顺序与同步版天然一致。
- **内省守卫测试**把"异步镜像遗漏"固化为门禁(`tests/unit/test_aio_mirror_surface.py:24-63`):基类面镜像、全部 A* 方法必须是协程函数、`pkg.__all__` 与 `aio.__all__` 双向一一对应(`test_package_exports.py:36-40`)。docstring 里还诚实记录了历史上漏过 `AOpcUaClient.endpoint`、`write_mask_register`。这类"防漂移测试"比功能测试更稀缺。

### 2.3 传输层踩过现场坑、处理正确

| 项 | 位置 | 评价 |
|---|---|---|
| 精确收满 + **整事务 deadline** | `transport/tcp.py:83-112,95-104` | 修掉 MC 4C "~54 小时"涓流挂死的历史坑 |
| 长度域**上下限双快失败** | `modbus/codec.py:311-322`、`melsec/codec_qna.py:249-260`、`omron/codec.py:327-339` | FINS 注释明确写"避免下游 `recv(0)` 阻塞到超时";上限 8192(`constants.py:527`) |
| 串口超时二分法 | `transport/serial.py:157-192` | 0 字节超时不断线;**部分字节后超时主动 `close()` 重同步**——正确处理串口残渣致帧错位这一最难问题 |
| UDP 截断分平台 | `transport/udp.py:111-154` | POSIX `MSG_TRUNC` 探真长 / Windows `WSAEMSGSIZE` 转错误 |
| 半开连接 | `tcp.py:138-157` | Linux `TCP_KEEPIDLE` vs Windows `SIO_KEEPALIVE_VALS`,失败静默降级 |
| 坏帧诊断 | `modbus.py:302-313`、`toyopuc/codec.py:136` | 异常文本携带原始帧 hex,坏帧拆线 + 重试 |

### 2.4 测试是真测试

- 1,072 例, 0 skipped(本轮独立实跑全过,v0.39.0 tag `94e1328`)/ `pytest.raises` 300+ / 形式主义用例 **0** / `unittest.mock` 使用 **0**(全部手写 fake)。**边界与失败可见性回归已补齐**(`11828ae` +10 例:FC 读写上限切片 ×4、设备异常留痕、chunk 独立、写重试口径、越界零字节、整批失败留痕等;其中 7 例在旧实现上实测 FAILED,确认为真回归),另有 aio `close` +2、`write_bool` +6、4C 截断 +1、panasonic 换算 +1 等自查回归;原 3 例平台互斥跳过(MSG_TRUNC 截断分支从未执行)已改写为跨平台假 socket 验证(`4ca1df7`);v0.38 +19(convert BOOL/STRING + TypedDict)、v0.39 +211(原生 asyncio + 契约矛盾批 2 + 双事件循环对拍)。
- 发帧**逐字节比对**(`test_modbus_clients.py:33`、`test_fins_clients.py:117`);**五重状态断言**——返回值 + 连接状态 + `last_error_code` + `last_error_category` + `stats` 计数(`test_modbus_clients.py:60-65`)。
- 黄金样本由**独立实现生成器**算 CRC/组帧,规避"实现生成测试"的同源偏差(`tests/golden/generate_modbus_samples.py:21-28`,README 亦明示)。
- 三层假传输(`tests/unit/scripted.py:11-80`):`ScriptedTransport`(分片/粘包)、`ChunkSocket`(size 感知,专防"读满恰好 size"假阳性)、`mount_real_tcp`(真 `TcpTransport` + 假 socket,只 mock 一层)。
- 28/28 具体客户端均有专属测试文件;OPC-UA 有 13 例**进程内起真服务端**集成测试(`test_opcua_client.py:449-768`)。

### 2.5 诚实度与第三方审查闭环

- `docs/review.md` 曾把第三方审查报告入库并逐条追踪 backlog;README:449-465 公示"真机联测待做"表;267 个公开定义 docstring 缺失 = 0。工业库中极少见。

---

## 三、缺陷清单(评审重点)

### 3.1 P1 — 行为级,现场会产生真实故障现象

| # | 缺陷 | 位置 | 状态 |
|---|---|---|---|
| 1 | **aio 同步属性阻塞事件循环**:`connected/stats/last_error*/receive_timeout/retries` 实现为同步 property 直转发(`:209-282`),而它们在同步侧都要拿 `self._lock`,该锁在 `_execute` 期间被**整个网络 I/O 周期**持有(`base_client.py:629`)。协程 A 正在 `await read()` 时,事件循环线程执行 `client.stats` 或 `client.receive_timeout = 5.0` 会**同步阻塞循环**最长 `receive_timeout`(默认 3s);`retries>0` + 重连时可达 `retries × connect_timeout`。`architecture.md:297` 只讲"保序",未提示此坑 | `aio/__init__.py:209-282` + `core/base_client.py:629` | 🟡 **部分收口** `fd13a42`/`512a86e`:README/architecture 公示"同步 I/O + 单线程池包装、属性同步直读、无原生取消",`close()` 竞态修复(关闸→排空→`shutdown(wait=True)`,在途帧不再落线,+2 测试);**但"属性读取会因事务锁阻塞事件循环最长 `receive_timeout`"这一核心风险仍未披露,代码级修复未做** |
| 2 | **SR 扫码枪能力缺失误拆线**:`KeyenceSrClient._read/_write` 抛 `OmniPLCInternalError` → 走 `_execute:651-654` 断线分支 → `_mark_disconnected()` + 分类 UNKNOWN + `retries>0` 时无谓重连重试。应与 `opentcp:394` 一致抛 `DeviceError`(不断线)。`test_sr_scanner.py:145` 只断言 `last_error` 未断言 `connected`,故未测出 | `scanner/keyence_sr.py:215,219` | ✅ **已修** `c05b392`(能力缺失改抛 `DeviceError` 不断线;`scan()` 并入 `_execute`;测试补 `connected` 断言) |
| 3 | **〔v0.36.0〕Modbus 批量合并超限切片方向反了,读侧对合法地址抛 `ValueError`**:`_coalesce_group` 在条目并入 `current` **之后**才检查 `current_end - current[0].offset > max_unit`(`modbus.py:981-987`),溢出条目被一并 flush → 生成跨度 `max_unit+1` 的超限 chunk → codec 组帧期 `ValueError`。**实测复现**:126 个连续 USHORT `read_many` → `ValueError: 读寄存器数量超出范围 1~125:126`(0 字节发出);2001 连续线圈 → `1~2000:2001`;64 个连续 FLOAT(`hr0,hr2,…`)→ `1~125:126`。`read_batch` 同样抛出。而 CHANGELOG/architecture/docstring 三处均声称"超 FC 上限在连续区内二次切片"——**承诺未兑现**,契约上 `ValueError` 仅应来自非法入参(`read_many:209`) | `modbus/modbus.py:981-987`(切块顺序)→ `codec.py:91-93` | ✅ **已修** `11828ae`:并入前先判溢出;+3 例边界回归;复验 24/24 过 |
| 4 | **〔v0.36.0〕写侧批量把异常全部吞掉 → 静默失败且 `last_error=None`**:`_write_and_apply:1281-1284` 的 `except Exception` 吞掉**一切**异常(含 codec 越限 ValueError 与设备异常码 `DeviceError`),仅置槽位 False 返回;RMW 分支 `modbus.py:461-466` 同样 `except Exception` 逐项吞。**实测复现**:124 连续 USHORT `write_many` → 全 False、**0 帧、`last_error=None`**;1969 连续线圈写同;设备返回异常码 02 时 `write_batch` → `(False, None)` 且 **`last_error=None`/`category=None`**,而同场景 `read_batch` 正确记录 `DeviceError`+`DEVICE`——读写诊断口径割裂,违背"失败见 `last_error`"的对外契约 | `modbus/modbus.py:1281-1284`、`:461-466` vs `read_batch` 读侧路径 | ✅ **已修** `11828ae`:`_write_and_apply` → `_write_chunk` 不捕获异常;`write_batch` 异常穿透(复验:`last_error`+`DEVICE`+计数全落)、`write_many` 每 chunk/RMW 独立事务;附带补 4 处写事务 `is_write=True`(此前写命令吃读重试,可 4 次重发) |

### 3.2 P2 — 口径/竞态,影响监控与异步两大卖点

| # | 缺陷 | 位置 | 状态 |
|---|---|---|---|
| 5 | **`device_error_count` 吞超时**:`TransportTimeoutError` 是 `DeviceError` 子类,超时走 `except DeviceError` 分支被 `+= 1`(`:647-650`),违背字段文档"PLC 明确返回错误码的次数(链路完好)"(`:340`);同时 `_categorize` 给的是 `TIMEOUT`——计数与分类口径互斥,看板误读。连带后果:UDP/串口超时**从不重试**(`:650` 直接返回),而 TCP `socket.timeout` 走 `:651` 断线 + 重试——同名 `retries` 跨走线行为不对称。**全库无任何测试注入 `TransportTimeoutError`**(`test_v030_reliability.py:663-674` 只测了 `OSError`) | `core/base_client.py:647-650` vs 文档 `:340` | ✅ **已修** `f40da18`:`_execute` 超时独立分支——不拆连、不计 `device_error_count`、按 `retries` 重试、TCP/串口/UDP 口径统一;+4 例回归(注入 `TransportTimeoutError`,断言计数/分类/连接态) |
| 6 | **`code=0` 污染 `last_error_code`**:`_extract_code` 只把 `TransportTimeoutError` 特判为 `None`,而 `DeviceError(msg, code=0)`(产生点 `base_client:738,745`、`opentcp:394,398`、`keyence_sr:202-204`)会把 `0` 当有效协议码写入,违背 `:324`"无码为 None"声明 | `core/base_client.py:791-803` | ✅ **已修** `f40da18`:`code=0` 归 `None`(RTU 超时用例断言 `last_error_code is None`) |
| 7 | **OPC-UA 回调竞态**:订阅回调在 asyncua 线程无锁写 `last_error` 三件套并 `+=1`,违反 `_set_error` 自身"须锁内调用"约定(`base_client.py:363`),与 `_execute` 的 `_clear_error`/计数构成非原子读改写 → 丢计数、成功后错误被污染 | `opcua/client.py:254-262,305-313` | 🔴 **仍开放** |
| 8 | **〔v0.36.0〕`write_many` 部分 chunk 失败后返回含 `None` 的列表,违约 `List[bool]`**:chunk 失败即 `break`(`modbus.py:488-492`),**未执行**的后续 chunk 槽位保持初始 `None`(`:456`)。**实测复现**:2 个 chunk 中第 1 个设备异常 → 返回 `[False, None]`;`sum(...)`/`all(strict)` 直接 `TypeError`。同时与 `write_many` 自身 docstring"其他 chunk 各自独立"(`:327,333`)自相矛盾——实现是"一处失败即整批止写",注释(`:489-492`)与文档两套口径 | `modbus/modbus.py:456,488-492` vs docstring `:327-333` | ✅ **已修** `11828ae`:去 `break`、每 chunk 独立事务、**去掉外层 `_execute`**(内层失败记录不再被外壳按"成功"清空);复验 `[False×123, True]`、`sum()==1`、无 `None`,末笔失败 `last_error` 保留、后续成功按契约清空(6 点补充断言全过) |

### 3.3 P3 — 边界与一致性(择要)

- **TCP 陈旧超时**:`recv` 循环内 `sock.settimeout(remaining)`(`tcp.py:104`)返回后不恢复,而 `send`(`:73-81`)不做任何 `settimeout` → 上一事务涓流残留的极小超时会被**下一事务的 `sendall` 继承**,可能误报发送超时并断线+重试。串口侧经核实**无害**(`port.timeout` 只管读,`write_timeout` 独立配置)。
- **能力缺失异常三套口径并存**:`DeviceError`(`opentcp:394`)/ `OmniPLCInternalError`(`keyence_sr:215`,会拆线)/ `ValueError`(`opcua:458,466`、`ads:303`,直接抛给调用方);`mtconnect` 自身 `_read` 抛 `ValueError`(`:378`)而 `_write` 抛 `DeviceError`(`:392`),同文件自相矛盾。—— *部分收口:`c05b392` 已修 SR 侧;`e19021d` 为 mtconnect 补了分类理由文档(参数错误 vs 能力缺失),行为未改;opcua/ads 仍原样。*
- **`scan()` 绕过事务模板**:手写 `with self._lock` + 惰性重连(`keyence_sr.py:96-129`),丢退避根因保留、`transactions` 计数、重试;同文件 `reset()` 却走 `_execute`(`:139`)——同类两套事务语义。—— ✅ **已修** `c05b392`:`scan()` 并入 `_execute`(`is_write=True`),补事务计数/退避门控/重试口径断言。
- `convert.words_to_value` 对 `BOOL/STRING` 抛 `KeyError`(`convert.py:341`),逆函数 `value_to_words` 抛 `ValueError`(`:391`)。—— ✅ **已修** `e19021d`:显式 `ValueError` 对齐全库参数错误约定,+3 例参数化测试。
- `_categorize` 冗余分支:`ConnectionRefusedError/ConnectionResetError/socket.gaierror`(`:784-785`)全为 `OSError` 子类,与下一分支结果相同。
- `_execute:622` docstring 过度承诺"所有内部异常转换为 (False, None)",实际 `ValueError/struct.error/KeyError` 会带锁外逃逸(锁本身能正确释放)。—— ✅ **已修**:现 docstring 明确"调用方错误与编程错误仍直接抛出"(`base_client.py:628-630`)。
- 会话型协议(OPC-UA/S7/ADS/MTConnect/MX)**伪装 `BaseTransport` 但 `send/recv` 恒抛异常**(`opcua/client.py:150-156`、`siemens/client.py:205-211`)——务实、零重复状态机,但违反 LSP;更干净做法是引入 `BaseSession` 中间抽象。
- `disconnect()` 无法打断进行中的 I/O(`base_client.py:195-213`),需等当前事务超时才返回,现场"急停断开"不友好。

### 3.4 设计债(非缺陷,但持续付息)

1. **aio 是手写转发矩阵**:1,405 行中约 70% 为三行式转发样板;构造默认值同步/异步两处字面量重复(`melsec.py:469` vs `aio:633`),门禁不校验默认值。
2. **无连接池**:单客户端持锁串行,多消费者须各开实例(`architecture.md:290` 自认"留 v1.x")。
3. **生命周期 API 不对称**:同步侧只有 `disconnect()`/上下文管理器,异步侧有 `close()`(`aio:424`)。
4. **文档漂移**:`architecture.md:423` 称"不使用 `# type: ignore[...]` 工具特定抑制码",与 `transport/*.py:41,44` 等 7 处实际抑制矛盾。

---

## 四、协议成熟度评审

### 4.1 自研深度分档

| 档 | 协议 | 分 | 评审意见 |
|---|---|---|---|
| **A 生产级(自研栈)** | 三菱 MC 3E/4E/1E + 串口 1C/3C/4C(2,877 行,755 行串口测试,`test_mc_serial_clients.py`) | **9.5** | 6 帧型全自研(含 DLE 转义、和校验、4C 长度域/帧识别码);0406 批量 + 1E 错误扩展头(`melsec.py:443-451`)等边角全处理;黄金 7 样本 |
| | AB EtherNet/IP + CIP(1,024 行手写 `ab/codec_cip.py`) | **9.5** | RegisterSession/Forward Open 三路由/SendUnitData/0x4C·0x4E 标签/0x91 符号路径/0x0A 多服务包全手写;`test_ab_codec.py` 字面字节断言 + 会话复用/ENIP 错误重注册回归;缺口仅 UDT 整读 |
| | Modbus TCP/RTU | **9.0** | 18 黄金样本(含 FC23 规范 §6.17 示例逐字节)、CRC16 known vector、异常回显功能码错配校验、FC22、RTU 广播语义;**FC 01/02/03/04 连续地址合并读**(按 (area, kind, width, dtype) 5 类分组,读 N 个连续点 ≈ 1 笔事务)、**FC 15/16 连续地址合并写**(同上 + 寄存器位走 RMW 不入合并)、**FC 23 单事务读写多寄存器**、**FC 43/14 设备标识**(标准对象名映射 + More Follows 自动翻页 + RTU 按对象头增量收包)、**起始地址+数量越界组帧期拒绝**;**P1:合并超限切片方向反了,读侧越界抛 `ValueError`、写侧静默失败(§3.1 缺陷 3/4),修复前扣 0.5**;**无 ASCII 走线、无 BCD、无文件记录/FIFO/串行诊断类功能码**;规范参考 [Modbus 应用协议 V1.1b3](https://www.modbus.cn/modbus-specifications) |
| | 欧姆龙 FINS TCP/UDP | **9.0** | 握手后节点自动分配(`omron.py:464-489`)、UDP 节点由 IP 末段推导、0104 批量、ICF 回显 + 长度 + 错误域三重校验;**v0.36 加固**:构造期路由范围校验(network/node 0~127、unit 0~255,拒 `& 0xFF` 静默截断)、节点推导范围校验(1~126)、应答帧 **ICF/SID/命令码三层身份回显**(`omron/codec.py:_check_identity`)、TCP 长度域下限前移;**真机 CP1H 已录** |
| **B 可用** | OPC-UA | **8.0** | 适配层最厚:NodeId 解析、类型映射、Browse(`:546`)、DataChange/Event 订阅、UA Read 批量(`:504`);扣:asyncua 1.1.5 钉版、无 TLS/X.509、无历史/聚合 |
| | MX Component | **7.5** | 798 行 ctypes/vtable 深度适配、HRESULT/返回码双校验、`write_batch`(`:753`);**真机 FX3U 两轮踩坑修复**(CHANGELOG v0.31.0/v0.31.1) |
| | KV MC(SLMP)/ 汇川 MC / 松下 MC | **7.5–8.0** | 继承 MC 全栈只换码表(`inovance/mc.py:76` 等),地址翻译挂批量与帧双路径一致(`:110-135`)——省成本且不漂移 |
| | MEWTOCOL / KV Host Link | **7.0–7.5** | BCC 自研 + 手册黄金向量(`test_panasonic_mewtocol_clients.py:43`,BCC=1D);Host Link 协议本身无 BCC,行收包带整段 deadline |
| | Omron CIP(NJ/NX) | **7.0** | 继承 AB,但**字符串显式不支持**(`cip.py:80-90`)、布尔元素非 Logix 打包(`:14`)、零真机 |
| **C 薄封装** | 西门子 S7 / 倍福 ADS | **6.0** | snap7/pyads 转接,协议价值在第三方库;S7 无块读写/SZL(`client.py:31`),地址解析是唯一无 `lru_cache` 的(`siemens/address.py:55`);ADS 无 address.py |
| **D 早期** | TOYOPUC / MTConnect / OpenTcp | **6.5** | TOYOPUC 命令面窄(仅 1C–21,扩展区留后续 `toyopuc.py:28`)、无黄金样本;MTConnect 零依赖健壮但**只读**(`:390-392`);**v0.36 新增 XXE/实体炸弹防御**(解析前拒 `<!DOCTYPE>`,3.7.9 stdlib 无 `forbid_dtd` 约束下的正确取舍,3 例 payload 测试) |

### 4.2 共性功能缺口

| 缺口 | 证据 |
|---|---|
| **写侧协议级批量** | MX(`mx.py:753`) + Modbus(`write_batch` FC 15/16 按组合并,同 MC/FINS/AB/OPC-UA 风格的"单事务多条目");其余 `write_many` 逐点(`base_client.py:439`) |
| **BCD / 数组 / 结构体类型** | `types.py` 仅 10 种标量;AB UDT 整体读为自评 P1 未闭环(`review.md` 旧版 §四) |
| **符号寻址** | 仅 AB 标签 / OPC-UA NodeId / ADS 符号;其余为软元件编号(协议使然) |
| **fuzz / 并发压测 / 集成测试层** | 无 `tests/integration/`;单锁模型无多线程同客户端压力用例 |
| **连接池** | `architecture.md:290` 明示留 v1.x |

### 4.3 地址与类型系统

- 地址解析高度统一:9/10 家遵循"正则 + `@lru_cache` + 不可变结果 + `ValueError`"模式;仅 `siemens/address.py:55` 无缓存、`opcua/address.py:31` 缓存上限硬编码 4096 与全局常量不一致。
- 字序处理是真正功夫:Modbus 用户可配 4 种 `WordOrder`(`modbus.py:66-93`)、MC 恒小端、FINS 恒大端、MEWTOCOL 低字在前+字内高字节在前(`mewtocol.py:271-288`)、KV 小端拼字、TOYOPUC 小端低字在前——各协议差异被正确隔离在驱动内。

---

## 五、工程治理与风险

| # | 项 | 证据与影响 |
|---|---|---|
| 1 | **真机验证 ≈ 7%** | `docs/real-machine-checklist.md` 30 行仅 2 行有值(MX Component/FX3U `:45`、FINS UDP/CP1H `:47`);**MC 全系、Modbus、S7、AB、ADS、CIP、汇川、松下、基恩士、TOYOPUC 全空**。90% 协议正确性靠"手册字节核验 + 模拟器"背书。记录体系本身专业(读/写独立单元格、✓/✗/~/— 约定),但覆盖面是可信度瓶颈 |
| 2 | **CI 单 OS × 单 Python** | `ci.yml:30` 仅 `python-version: "3.12"` + `runs-on: windows-latest`;而 `pyproject.toml:6,53-58` 声明 `>=3.7.9` 与 3.7–3.12 classifiers——**6 个声明版本里 5 个从未在 CI 跑过**;`transport/udp.py` 的 `MSG_TRUNC`、`TCP_KEEPIDLE` 等 Linux 分支在 CI 永不执行 |
| 3 | **发布触发设计(经复核为合理,非缺口)** | 早前评审曾记"发布路径零测试门禁",**经复核撤回该结论**:`ci.yml:5-9` 在 push/PR master 时强制执行 pytest/ruff/mypy/ty 四门禁,`build-release.yml:7-10` 仅 `v*` 标签触发纯构建;而标签是从**已通过 CI 的 master 提交**上打的,`ci.yml:4` 注释亦明示两者互补("本工作流 push+PR 触发,发布仍走 tag 触发的 build-release.yml")。故 build 再跑测试属**同一批次的二次执行**,`4b0c8d6` 撤回 build 内 test 作业属合理的防重复设计,不构成"未测即发布"风险 |
| 4 | **无覆盖率工具链** | `pytest-cov` 不在 `pyproject.toml:81-90` dev extra;全仓 `coverage` 零命中;协议层覆盖率仅为静态推断 70–85%,无实测背书,无门禁 |
| 5 | **lint/type 规则偏松** | ruff 仅 `["E4","E7","E9","F"]`(`pyproject.toml:107`),无 B/I/SIM/行宽、无 format 门禁;mypy 未开 `strict`,且 CI 只查 `src/omniplc`(`ci.yml:44`)使 `pyproject` 的 `files=["src","tests"]` 被覆盖 |
| 6 | **迭代节奏与治理** | 8 天 46 版(日均 5.75 版);tag v0.1.0–v0.34.0 **同日批量回补**;CHANGELOG 无日期;commit 类型前缀非 100% 统一——长期维护性无历史证据 |
| 7 | **可持续性** | 单一作者、bus factor = 1;`snap7/asyncua/pyads` 上游 breaking change 会传导 |
| 8 | **国际化与呈现** | 全中文、无 API 文档站(mkdocs/Sphinx 均无)、README 无任何 badge、标题层级断在 h4;用例数漂移已修(`347fb94`,现写"842 例,随批次增长",不再承诺固定值)——手工同步用例数属反模式,建议由 CI 生成 |

---

## 六、评审结论

### 6.1 结论

**准予通过(附条件)**:

- 作为**技术预研 / 内部代码资产 / 国产 PLC + Python 统一 API 的选型基线**——质量优秀,四大自研协议栈(Modbus/MC/FINS/CIP)达可交付生产水准,测试与文档超同类开源均值。
- 作为**生产级交付物**——需完成 §6.2 整闭环方可验收;当前 Alpha 状态下,主力协议(MC 以太网/串口、Modbus、S7、AB)缺真机背书是最硬的短板。

### 6.2 优先整改清单(按投入产出排序)

> 已闭环(本轮复验确认,不再列入):Modbus 合并切片、写侧吞异常、`write_many` `None` 槽位与 `last_error` 终态、SR 拆线、`scan()` 旁路、超时计数口径、`code=0`、`convert` KeyError、`_execute` docstring、`write_bool` 真值、写事务 `is_write`、4C 截断拆连、panasonic 批量错址、`close()` 竞态、用例数漂移——共 15 项,落点见 §7.4。

| 优先级 | 项 | 位置 / 说明 |
|---|---|---|
| **P0** | aio 属性阻塞事件循环(文档化未覆盖核心风险) | `fd13a42` 已公示"同步直读/非原子快照",但未写明**属性读取会因事务锁阻塞事件循环最长 `receive_timeout`**;代码级修复(改 `await` 通道)仍未做。`aio/__init__.py:209-282` |
| **P1** | OPC-UA 回调加锁 | `opcua/client.py:254,305` 提供加锁版 `_set_error` 入口(本批次唯一未动的 P2) |
| **P1** | CI 版本矩阵 + Linux 腿 | 3.7/3.9/3.12 × windows/ubuntu,否则 classifiers 是虚假承诺 |
| **P1** | 覆盖率门禁 | 引入 `pytest-cov`,协议 codec 层阈值 ≥85% |
| **P1** | 回填真机清单 | 优先 **Modbus TCP + MC 3E**——对可信度伤害最大的两项空白 |
| **P2** | 能力缺失异常口径收尾 | SR 已修;`opcua:458,466`/`ads:303` 仍抛 `ValueError`,`mtconnect` 读写分类仅文档澄清 |
| **P2** | TCP 陈旧超时 | `send` 入口显式 `settimeout(receive_timeout)` |
| **P2** | 用例数由 CI 生成 | 手工同步已三次漂移(721→838→840→842);`347fb94` 已同步并改为"随批次增长"措辞,仍建议由 CI 生成 |
| **P3** | 连接池 / fuzz / 异步原生 asyncio / TLS / AB UDT 整读 | 长期路线图 |

### 6.3 分项评分依据摘要

| 维度 | 分 | 支撑 | 主要扣分 |
|---|---|---|---|
| 分层设计 / API | 8 | 三档钩子 + 事务模板 + 批量可覆写;异步包装器 + 内省门禁 | aio 属性阻塞、会话型伪装传输违反 LSP、构造默认值双写 |
| 线程安全 | 7 | 单 RLock 整事务原子 + full-jitter 门控(`:667-680`)+ 8 线程并发回归;**无死锁回路** | OPC-UA 回调无锁、`scan` 旁路、`retries` 锁外读、无法打断进行中 I/O |
| 错误处理 | **7.5** | 三层语义文档与实现一致;`_categorize` 顺序正确且有 10 例专项测试;**超时计数/`code=0`/SR 能力缺失三项口径缺陷已修**(`f40da18`/`c05b392`) | OPC-UA 回调无锁、`write_many` 失败 `last_error` 终态恒空(§7.4 残留)、`_categorize` 冗余分支 |
| 协议栈深度 | **9** | 四大栈全自研 + 黄金样本 30 个 + 长度域/回显/序列号全校验;v0.36 新增 FC23/FC43/FINS 加固/XXE 防御;**Modbus 批量合并 P1 已修复并复验通过** | 真机 7%;4/15 为薄封装;BCD/结构体缺失 |
| 测试质量 | 8 | 1,072 例字节级断言(本轮实跑全过,含 0 skipped)、三层假传输、独立生成器、**61/61** 覆盖(含 5 原生客户端);**FC 上限边界与失败可见性回归已补 +10**;v0.39 原生用例双事件循环(Selector + Proactor)对拍 | 无覆盖率实测、无 fuzz、无并发压测、黄金样本仅 3/15 协议 |
| CI/CD | 7 | push/PR 四门禁 + tag 纯构建的互补触发(标签取自已过门禁的 master 提交,无"未测即发布")+ 双类型检查器 + OIDC 发布 + fork 守卫 | 单 OS 单 Python、ruff 规则集弱、无 coverage/SAST |
| 文档 | 8 | 94KB 架构文档逐协议列手册章节、267/267 docstring、诚实公示待核证 | 纯中文、无 API 站、无 badge、CHANGELOG 无日期、数字漂移 |
| 真机验证 | 3 | 清单专业 + 待做项诚实 + 36 个联测脚本 + MX 两轮真机根因记录 | 29 项适用协议仅 2 项通过 |

---

## 七、v0.36.0 增量复审(2026-09-25,基线 v0.35.0 → v0.36.0)

> 范围:`af6d7b7..5cceca6` 共 8 个提交(2 个安全/校验加固 + 4 个 Modbus 能力 + 文档/发版)。方法:增量 diff 走查 + 独立复现脚本实测 + 门禁四件套实跑。

### 7.1 版本内容与核验结论

| 提交 | 内容 | 复审结论 |
|---|---|---|
| `9a1b9e9` | **FINS 校验加固**:构造期路由范围(network/node 0~127、unit 0~255)、节点推导范围(1~126,IP 末段超限抛错)、应答帧 ICF/SID/命令码三层身份回显(`_check_identity`,短帧先于索引校验防 IndexError 掩盖)、TCP 长度域下限前移 | ✅ **通过**。设计正确(串话/迟到响应按坏帧处理)、+182 行测试、UDP 失败走 `_after_connect` 干净收口 |
| `048b3d7` | **MTConnect XXE/实体炸弹防御**:解析前扫描拒 `<!DOCTYPE>`(3.7.9 stdlib 无 `forbid_dtd`,唯一 stdlib-only 方案;协议合法负载不用 DOCTYPE,零误伤) | ✅ **通过**。3 例 payload 参数化测试;`_parse_document` 与 `_MtConnectSession.request` 两个调用点全替换 |
| `3cc245a` `3a407c2` | **Modbus 原生批量读 + FC15/16 合并写**:5 类分组、组内 gap=0 合并、寄存器位 RMW 不入合并 | ⚠️ **有缺陷**——见 7.2/7.3;合并思路与 MC 0406/FINS 0104 对齐,方向正确 |
| `db3c75a` | **FC23 + FC43/14 + 越界前置**:`_check_span` 组帧期拒 `offset+count > 65536`;FC23 规范上限(读 125/写 121)分离;FC43 翻页收敛保护(`next_object_id` 不动即报、`MAX_PAGES` 上限)、个体访问、RTU 按对象头增量收包;`parse_device_id_response` 截断/尾字节自洽校验 | ✅ **通过**(除受 7.2 波及的合并路径外)。编解码严格、黄金样本取规范 §6.17 示例逐字节;`_check_span` 是本轮质量最高的新增 |
| 发版 | 五落点同步(`pyproject`/`__init__`/CHANGELOG/architecture 状态链+履历/`uv.lock`)、真机清单补注 FC22/23/43 待核证 | ✅ **通过**。记录诚实 |

### 7.2 新缺陷一:合并超限切片方向反了(P1)

`_coalesce_group`(`modbus.py:981-987`)**先把条目并入再判溢出**,flush 出来的 chunk 恒含溢出条目、跨度 = `max_unit+1`,下游 `build_read_pdu` 的上限校验(`codec.py:91-93`)直接 `ValueError`。

**独立复现(全为合法地址、0 字节发出):**

| 场景 | 实测结果 |
|---|---|
| `read_many` 126 × USHORT | `ValueError: 读寄存器数量超出范围 1~125:126`,frames=0 |
| `read_batch` 126 × USHORT | 同上抛出 |
| `read_many` 2001 × 线圈 BOOL | `ValueError: ...1~2000:2001` |
| `read_many` 64 × FLOAT(`hr0,hr2,…`) | `ValueError: ...1~125:126` |
| 对照 125 × USHORT / 2000 × 线圈 | 正常 1 笔事务 ✅ |

文档三处承诺("超 FC 上限二次切片"——`modbus.py:262`、CHANGELOG v0.36.0、architecture §11)均未兑现;`read_many` docstring 声称 `ValueError` 仅来自非法入参(`:209`),实际合法入参也会抛。**修复**:先判 `offset + width - chunk_start > max_unit` 再决定 flush/append,一行顺序调整 + 边界测试即可。—— ✅ **`11828ae` 已按此修复并复验通过**(见 §7.4)。

### 7.3 新缺陷二:写侧吞异常 → 静默失败(P1)+ `None` 槽位(P2)

`_write_and_apply:1281-1284` 用 `except Exception` 无差别捕获,异常不逃逸到 `_execute` → 错误三件套完全不落盘。**独立复现:**

| 场景 | 实测结果 |
|---|---|
| `write_many` 124 × USHORT(超 FC16 上限 123) | 全 `False`、**frames=0、`last_error=None`** |
| `write_many` 1969 × 线圈(超 FC15 上限 1968) | 全 `False`、`last_error=None` |
| `write_batch`,设备回异常码 02 | `(False, None)`,**`last_error=None`、`category=None`** |
| 对照 `read_batch`,设备回异常码 02 | `(False, None)`,但 `last_error=DeviceError...`、`category=DEVICE` ✅ |

外加 `write_many` 两 chunk 场景第 1 个失败即 `break`(`:488-492`),未执行槽位保持 `None` → 实测返回 `[False, None]`,`sum()` 直接 `TypeError`;且与自身 docstring"其他 chunk 各自独立"(`:327-333`)矛盾。**修复**:`except Exception` 收窄为"仅标记槽位 + 上抛协议/设备异常给 `_execute` 记账";失败 chunk 的后续槽位置 `False` 而非 `None`;文档与 `break` 语义二选一对齐。—— ✅ **`11828ae` 已落地并复验通过**;复验中另发现的 `write_many` `last_error` 终态残留(外层 `_execute` 成功即清空)也在同一提交中通过去掉外层壳修复,6 点补充断言全过(§7.4)。

### 7.4 缺陷状态追踪(截至本轮复验,2026-09-25 晚)

v0.36.0 发布后同日 19:00 起出现修复批次:**11 个提交全部落地**(`f40da18`/`c05b392`/`e19021d` + Modbus 收口 `11828ae` + core 口径 `1ceab51` + 4C `cba5d43` + 松下 `cc15693` + 文档 `347fb94`/`fd13a42` + aio `close` `512a86e` + 测试跨平台化 `4ca1df7`;提交信息逐条引用本报告缺陷号)。逐项状态:

| 本报告编号 | 缺陷 | 状态 | 落点 |
|---|---|---|---|
| P1-1 | aio 属性阻塞事件循环 | 🔴 **仍开放** | `aio/__init__.py` 本批次仅动 `close()` 与文档(属性通道未改) |
| P1-2 | SR 能力缺失误拆线 | ✅ 已修 | `c05b392`:改抛 `DeviceError`(code=0)不断线;`scan()`/`reset()` 统一事务模板;测试补 `connected` 断言 |
| P1-3 | Modbus 合并超限切片 | ✅ 已修 | `11828ae`:`_coalesce_group` 改为**并入前先判溢出**;+3 例回归 |
| P1-4 | 写侧吞异常无诊断 | ✅ 已修 | `11828ae`:`_write_and_apply` → `_write_chunk` 不捕获异常;`fail_fast` 分流(`write_batch` 穿透 / `write_many` 每 chunk 独立 `_execute`);+2 例回归 |
| P2-5 | `device_error_count` 吞超时 | ✅ 已修 | `f40da18`:`_execute` 超时独立分支,不拆连/不计设备错误/按 `retries` 重试,三走线口径统一;+4 例回归 |
| P2-6 | `code=0` 污染 | ✅ 已修 | `f40da18`:`code=0` 归 `None` |
| P2-7 | OPC-UA 回调竞态 | 🔴 **仍开放** | `opcua/client.py` 本批次零改动 |
| P2-8 | `write_many` 返回 `None` 槽位 | ✅ 已修 | `11828ae`:去 `break`、每 chunk 独立事务;+1 例回归 |
| 自查 | 写事务漏 `is_write=True`(写命令吃读重试,可 4 次重发) | ✅ 已修 | `11828ae`(4 处) |
| 自查 | 越界跨度只在组帧期拦 → 批量写先落线再抛 | ✅ 已修 | `11828ae`:入参期零字节拒绝 |
| 自查 | 松下 MC 缺 `_translate_address` → 批量路径**静默错址** | ✅ 已修 | `cc15693`(与汇川写法对齐) |
| 自查 | `write_bool("c0","0")` 被 `bool()` 吞成真值写出 | ✅ 已修 | `1ceab51`:非 bool/int 显式拒绝 |
| 自查 | `device_error_count` 计入无码失败(与字段文档相反) | ✅ 已修 | `1ceab51`:有码才计 |
| 自查 | 4C 帧中途超时误用"0 字节已读"语义(不拆连 + 重试) | ✅ 已修 | `cba5d43`:按截断改抛 `TransportClosedError` 重同步 |
| P3 | `scan()` 旁路 | ✅ 已修 | `c05b392` |
| P3 | `convert.words_to_value` KeyError | ✅ 已修 | `e19021d` |
| P3 | `_execute` docstring 过度承诺 | ✅ 已修 | 现文案区分"传输/设备异常转换 vs 调用方/编程错误直接抛"(`base_client.py:628-630`) |
| P3 | 能力缺失三套口径 | 🟡 部分 | SR 侧已修;opcua/ads 原样;mtconnect 仅补文档理由,行为未改 |
| P3 | TCP 陈旧超时 / `_categorize` 冗余 / 会话型 LSP / `disconnect` 不可打断 | 🔴 仍开放 | 本批次未涉及 |
| **K1**(§7.7 新发现) | Keyence MC 位组地址静默错位(R 组号≥1) | 🟠 **复核更正:反证不成立,现状不改(2026-09-26,§7.7-B-8)** | `keyence/mc.py` 未覆写 `_translate_address`(按记号数字原样发帧);项目侧复核:第 4 条"3E 设备号装不下 199915"不成立(该字段实为 **3 字节**),且 Mitsubishi SH081257ENG 明文支持"上位字号 + 低 2 位位号",故按"证据不足不改"处置,终核判据入真机清单 |
| **K2**(§7.8 新发现) | AB EtherNet/IP `_after_connect` 失败时 CIP session 在 PLC 侧泄漏 | 🟢 **已修(2026-09-26)** | 基类新增 `_after_connect_failure` 钩子(同步 `core/base_client.py` + 原生 `native/base.py`),AB 覆写为尽力 Forward Close + UnregisterSession;**调用点在 `transport.close()` 之前**(放之后 = 注销帧发不出去且异常被静默吞掉,实测线上零字节);+5 例(含"关闭后拒发"假传输、20 次失败重连注册/注销帧数相等),旧实现上 3 例实测 FAILED |

**修复引入的残留(复验中发现,已闭环)**:`write_many` 失败的 `last_error` 曾**终态恒为 `None`**——内层每 chunk `_execute` 记录后,外层包裹的 `_execute` 成功返回时 `_clear_error()`(`base_client.py:651`)必然清掉它(`error_count`/`device_error_count` 有留痕但读不到原因)。**`11828ae` 已修**:去掉外层 `_execute` 壳,单 chunk 失败时 `last_error` 保留(实测 `[False]` + `last_error=DeviceError…异常码 0x02` + `category=DEVICE` + `transactions=1`);多 chunk 场景按 `last_error` "成功即清空"契约,以返回值列表为准。docstring 已把两种入口的语义写清(`modbus.py:334-337`)。

### 7.5 本轮独立验证记录

| 验证项 | 结果 |
|---|---|
| `pytest tests -q` | v0.36.0 基线 **806 passed** → 修复批次提交后 **826 passed, 2 skipped** → 全批次落地后 **842 passed, 0 skipped**(原 3 例平台互斥跳过改写为跨平台假 socket 验证),3 轮均全绿 → v0.38.0 `861 passed`(+19:convert BOOL/STRING + TypedDict)→ v0.39.0 **1,072 passed**(+211:原生 asyncio + 双事件循环对拍 + 契约矛盾批 2) |
| `ruff check src tests` | All checks passed(五轮) |
| `mypy src/omniplc` | Success: **76** source files(五轮) |
| `ty check src/omniplc` | All checks passed(五轮) |
| 缺陷复现(修复前) | 7.2/7.3 表格 8 场景全部独立复现(复现脚本,非仓库测试) |
| 修复复验(修复后) | **24/24 断言全过**:读侧 126 字/2001 线圈/64 FLOAT 正确切 2 笔且全成功、125 字仍 1 笔不过度拆分;写侧 124 字/1969 线圈切 2 笔且全 True;`write_batch` 设备异常 → `last_error`+`DEVICE`+`device_error_count=1`;`write_many` chunk1 失败 → `[False×123, True]`、`sum()==1`、chunk2 照常发出、计数器留痕 |
| 黄金样本 | 23 → **30**(+FC23 规范示例、FC43 翻页/异常/私有对象、RTU 变长) |
| 文档漂移 | `CONTRIBUTING.md:36` 的 "721 例"已修(`347fb94`,现写"842 例,随批次增长");v0.39.0 `1,072 passed` 已在 §一 同步标注 |
| 最终轮门禁(v0.39.0 tag `94e1328`) | `pytest` **1,072 passed** / `ruff` / `mypy`(**76** files)/ `ty` 全零(评审侧独立复跑) |

### 7.6 复审结论

- **v0.36.0 的"校验加固"两件(FINS、MTConnect)与 FC23/FC43/越界前置质量扎实**,测试与文档同步到位,属于把评审意见落进代码的正面样本。
- **旗舰新能力"批量合并"交付即带 2 个 P1 + 1 个 P2**,且全部落在测试盲区(FC 数量上限边界 0 覆盖)——印证了 §5 关于"无覆盖率门禁、无边界/fuzz 测试"的系统性风险判断:**测试量在涨(742→826),测试的"面"此前没有向边界扩展**;好在修复批次已补齐 6 例边界回归。
- **评审响应速度罕见**:v0.36.0 当日即按本报告落地 11 个修复提交,逐条引用缺陷号,且修复质量高(超时三分支口径、`fail_fast` 分流、`scan` 并入模板都是结构性正确解,不是打补丁)。8 项 P1/P2 已修复 6 项,另由自查额外修复 6 处(含 1 处静默读错数据),全部已提交并带回归测试。
- **综合评分上调 7.1 → 7.2**(错误处理 7→7.5、协议深度 8.5→9,因缺陷修复已复验):余下硬伤为 **aio P1、OPC-UA P2、真机 7%、CI 矩阵单薄**。
- **复核中新发现 K1(既有缺陷,非修复批次引入)**:Keyence MC 路径对位组记号(组号+位号)**无换算直发十进制原文**,`R515` 发 515(=官方记号 R03204)而非线性 95,组号≥1 的 R 地址全部静默错位(证据链与反证见 §7.7-B)。K1 经手册/真机坐实则协议深度 9→8.5、综合回调 7.2→7.1;终核前按"高置信待核"计入结论与附录局限。

### 7.7 修复批次代码级复核(11 提交 diff 走查)与新发现 K1

**A. 复核范围与结论**:`5cceca6..4ca1df7` 共 11 个提交逐 diff 走查(评审侧不改代码,验证手段 = 独立复现脚本 + 门禁四件套 + 帧级断言核对)。全部结构性正确,重点核验点:

| 提交 | 复核要点 | 结论 |
|---|---|---|
| `f40da18` | 超时独立分支三走线口径统一;`_extract_code` 的 `exc.code or None` 边界(`OSError.errno=0` → None,可接受);`_categorize` 删冗余分支后 `ConnectionRefused/Reset`、`gaierror` 仍被 `OSError` 子类兜底 | ✅ |
| `c05b392` | 能力缺失 `DeviceError(code=0)` → 归 `None` 不拆线;`scan()`/`reset()` 并入 `_execute` 后 `is_write` 传递正确 | ✅ |
| `11828ae` | 溢出判断先于并入;`_write_chunk` 异常穿透 + `fail_fast` 分流;去外层 `_execute` 后 `last_error` 终态语义;chunk 循环内 lambda 立即调用,无晚绑定;`result` 槽位 cast 前全路径填满 | ✅ |
| `1ceab51` | 非 bool/int 显式拒绝;`device_error_count` 有码才计 | ✅(备忘 P3-1) |
| `cba5d43` `cc15693` `e19021d` `347fb94` `fd13a42` `512a86e` `4ca1df7` | 4C 截断重同步;松下换算覆写;ValueError 契约;文档漂移;aio 边界公示;close 三步;UDP 假 socket 跨平台 | ✅(备忘 P3-2/P3-3) |

独立验证:`verify_modbus_v036.py` **24/24** + `verify_wm_last_error.py` **6/6**;v0.37.0 tag(`a1564b6`,仅版本/CHANGELOG/README,无行为变更)与 v0.39.0 tag(`94e1328`,原生异步 + 契约矛盾批 2)下门禁四件套复跑全绿(842→1,072 / ruff / mypy / ty)。CHANGELOG v0.37.0 条目与提交事实逐一相符,行为变更提示(`device_error_count` 口径、`write_many` 事务粒度、`write_bool` 拒绝范围)披露完整;v0.39.0 CHANGELOG 条目详尽,但因 `e23c787` 入库本报告 `docs/review.md` 的过程被同期 `9c0d2ff` v0.38.0 release 覆盖,故本报告头部基准逐版本回溯为 a1564b6 → 9c0d2ff → 94e1328。

**复核中的 P3 备忘**(不阻塞发布):

- **P3-1** `write_bool` 守卫接受**任意 int**(`base_client.py:513` 仅拒 `str`),`write_bool(addr, 5)` → `bool(5)=True` 写出,而 docstring `:param` 写"兼容 int 0/1"(`base_client.py:509`)——建议补 `value not in (0, 1)` 拒绝或放宽文档口径。
- **P3-2** aio `close()` 的 `CancelledError` 回退在事件循环线程**直接** `executor.shutdown(wait=True)`(`aio/__init__.py:466-469`),与 docstring "不阻塞事件循环"(`:446-447`)在取消路径不符(提交说明已提示该权衡,建议 docstring 补一句边界)。
- **P3-3** `offset_device`(`keyence/address.py:109`)为无引用死代码。

**B. 新发现 K1(高置信,待手册/真机终核):Keyence MC 位组地址静默错位**

1. **记号语义(官方)**:Keyence 官方规格——KV-X 系列入/出/内部辅助继电器 **R00000~R199915,合计 32000 点**;Visual KV 手册同记号(`0000 to 17915`)。即 **R 记号 = 组号(十进制)+ 位号两位(00~15)**,线性位号 = 组×16+位——`1999×16+16 = 32000` 与官方点数严丝合缝。本仓库自身文档一致:`core/constants.py:644-645`(`KV_BIT_BANK_DEVICES`,"R515 = 组5 位15")、`keyence/address.py:48`。
2. **HostLink 路径正确**:帧发文本 `RD R515`(`tests/unit/test_kv_clients.py:118`),PLC 按自身记号解析 = 线性 95。
3. **MC 路径缺换算**:`_KeyenceMcCodeMixin` 仅换软元件码表(R→`0x90`、base 10,`core/constants.py:294-300`),`_translate_address` 保持默认透传(`melsec/melsec.py:373-379`);melsec 解析 `"R515"` → `device_number` 十进制 `int("515")` **直发** → 帧内 515 = 线性 515 = 官方记号 **R03204**。单点(`melsec.py:252` 经同一 identity 钩子)与批量两路**都错**,与松下 `cc15693`(仅批量错、单点正常)同因不同面,范围更大。
4. **位组十进制串装不进帧(反证)**:3E 帧设备号字段 16 位(≤65535),而 `"R199915"` 记号串 = 199915 > 65535 → KV MC 不可能按记号十进制串收数,**必为线性编号**——K1 成立不依赖个例。
5. **测试盲区**:Keyence MC 测试仅 `R5`(`test_keyence_mc_clients.py:120-123`,组0,两种解释重合)、B1F/W10/DM100/ZR100(线性/十六进制,无组位换算需求),**无任何组号≥1 的 R 用例**——与 v0.36.0 合并切片、松下错址同型的"记号/边界盲区"。
6. **影响面**:R 组号≥1 的全部地址静默读写错位(如 `R100` → 应发 16 实发 100、`R515` → 应 95 实 515,读到/写错别的继电器);MC 路径还不校验低两位 00~15——`"R16"` 可发帧(= 线性 16),恰等于 HostLink 的 `R100`,而 HostLink 按记号拒收 `"R16"`:`address.py:83`,**同一字符串在两条传输上语义分裂**。MR/CR 属同类记号(`KV_BIT_BANK_DEVICES`),但 `KEYENCE_MC_DEVICE_CODES` 仅含 R/B/W/DM/ZR,MR/CR 在 MC 路径查不到码会抛错(非静默),故静默影响限于 R。
7. **建议**(评审侧不改代码):参照 `cc15693`/`11828ae` 模式在 `_KeyenceMcCodeMixin` 覆写 `_translate_address`——对 `KV_BIT_BANK_DEVICES` 校验低两位 00~15 并换算 `组×16+位`;补帧断言(`R100` → `10 00 00`、`R515` → `5F 00 00`)与"HostLink 同字符串对照"用例;KV 用户手册 MC 章设备编号表核对与真机对照列入 `docs/real-machine-checklist.md`(KV MC 条目已在清单,需点名组号≥1 用例)。
8. **复核更正(项目侧 2026-09-26,后续勿再引第 4 条当反证)**:第 4 条的"决定性反证"**不成立**——3E 二进制的设备号(head device)字段是 **3 字节小端**(`plc/melsec/codec_qna.py:125,407` 的 `number.to_bytes(3, "little")`),199915 = 0x030CEB 装得下,不能据此断定线上必为线性编号。反向证据同时存在:三菱 SLMP 手册 SH081257ENG 对"通信对象为 KEYENCE(KV 系列)"明文规定"除 B 设备外,设备格式由上位字号 + 低 2 位位号构成"(即线上编号 = 记号数字原样,`R515` → 515 = 本库现状);KV 公布的点数范围亦按该记号书写(`R00000~R199915`),未见线性 `0~31999` 的写法;本库码表 `B=十六进制 / R=十进制` 的分工正对应该条的"B 设备例外"。**处置:现状不改**——按线性改会让组号≥1 的 R 静默读写到别的继电器(风险不对称),"两条传输语义分裂"(第 6 条)在线性读法下**依然存在**(那时 `R16` 反而合法),不能当换算理由。终核判据已写入 `docs/real-machine-checklist.md`:写 `R100`,看 KV Studio 里 `R100`(组 1 位 0)还是 `R604`(组 6 位 4 = 线性 100)变化。MC 路径是否补"位号低两位 00~15"校验,待真机终核后一并决定。

**C. 评分影响**:K1 属**既有缺陷**(非本轮修复引入)。按 §一评分口径,若经手册/真机坐实,协议深度 9 → 8.5、综合 7.2 → 7.1;终核完成前按"高置信待核"计入 §7.4/§7.6 与附录局限,评分暂不调整。**2026-09-26 项目侧复核更正**:第 4 条反证不成立(设备号字段实为 3 字节)且存在反向外部证据(§7.7-B-8),K1 降级为"证据不足、现状不改",上述评分回调暂不适用。

### 7.8 v0.37→v0.39 增量复审(2026-09-25 晚,锚点 v0.39.0 tag `94e1328`)

**A. 锚点与基线**:`a1564b6` (v0.37.0) → `9c0d2ff` (v0.38.0) → `94e1328` (v0.39.0),14 个提交,**76 文件 / 18,800+ 行 / 49 测试文件 / 1,072 passed**;门禁五轮全绿(842→861→1,072 / ruff / mypy / ty)。

**B. v0.37→v0.39 期间 4 大类变更落地核验**

1. **原生 asyncio 首批(5 提交)**:`6db7a9b`/`1c45545`/`148d3fa`/`4429fd9`/`9fd0111` —— 评审此前给原生异步化提的 6 条路径中,实际落地为 **TCP `open_connection` 流式(我推荐 1A,不是 1B)+ UDP `loop.sock_recv_into/sock_sendall`(`setblocking(False)`,接近推荐 2B 但用 `_into` 变体)+ 自建 `_await_with_timeout` 替代 `asyncio.wait_for`(规避 3.7→3.11 `TimeoutError` 类变动 + 显式取消内层 task)**。文档 `transport.py:7-37` 把选型依据写到位:3.7 `ProactorEventLoop` 无数据报端点(仅 selector 路径)、无 `sock_recvfrom`(3.11+)→ 此两条路径在本机复现过,定位精确。**第一批 5 客户端**:`AsyncModbusTcpClient` / `AsyncMelsecMcTcpClient`(兼容 1E+3E)/ `AsyncMelsecMcUdpClient` / `AsyncOmronFinsTcpClient` / `AsyncOmronFinsUdpClient`,**只重写薄分发层,协议逻辑与传输边界仅 `_transact`**,编解码/地址/字序/`_describe`/`_categorize`/`_extract_code`/`ClientStats` 全部复用同步侧同一实现——架构干净,不与实现漂移。**与同步层的语义差异**(也是评审重点关注):① 属性直接读字段、不取锁(单线程事件循环原子快照);② `asyncio.Lock` 非重入 → 只有公开入口取锁;③ 按首次使用时的事件循环惰性创建(3.7+ `Lock` 构造即绑循环);④ **真取消**:按"请求是否已发出"分流——已发出保守拆连重同步、未发出仅排空保持连接,`CancelledError` 原样传播——直击 aio P1-1 取消语义不真的根因。**串口/OPC-UA/MX 走线**按既定口径不动(thread-pool / asyncua 第三方 / COM-STA 不可异步)。**批量与各家扩展方法留后续批次,缺口由守卫测试的表项锁定**(白名单测试)— 工程克制,避免一次性铺面引入未对拍风险。
2. **契约矛盾批 2**:`2e152df` —— `write_bool` 三层一致收紧(仅接 `bool` 或 `int 0/1`,此前 `bool(5)=True` 静默写真;`core/base_client.py:535-546`、`native/base.py:467-476`、`aio/__init__.py:368` 委托同步侧守卫——隐式 ✅);`MEWTOCOL` 非数字错误码不再抛裸 `ValueError`(`int(code)` 遇表外码如 `E1` 会穿破 `read()` 契约,现兜底 `code=0` 并保留原文);**Keyence Host Link 分类口径** 6 处响应形状不符由 `OmniPLCInternalError`(UNKNOWN)改 `ProtocolFrameError`(PROTOCOL),与 `_transact` docstring 承诺一致,见 `hostlink.py:65,74,103,185,278,296,386` 与 `codec.py:46,48,52,89,92,100,133,141,145,148,153`。`Keyence Host Link` 整体由 `UNKNOWN` 升 `PROTOCOL` 分类,**与 `_categorize` 文档承诺对齐**——此前本报告 P1-2 SR 能力缺失分类问题在 Host Link 这一支同样存在,本次同批修复。
3. **审核修复(自审本轮代码 P0/P1)**:`4429fd9` —— **UDP 走线主机名解析钉 `AF_INET`**(原用 `getaddrinfo` 首个结果,本机 `localhost` 首个是 `AF_INET6` → 同步侧通 IPv4、原生侧 `::1` 报 `ConnectionResetError`,两层行为分裂,**已复现并修复**);`_clear_stale_selector` 同时 `remove_writer`(覆盖"发送缓冲打满"路径);3.7 取消 `sock_recv_into`/`sock_sendall` 时 selector 注册不会立即摘除、关套接字触发 Windows `WSAENOTSOCK`(10038)带崩事件循环——超时路径显式摘 reader+writer。**这三个 fix 的工程价值** ≥ 同期任一对外功能——原生的"3.7 平台差异实测"为后续真机清单 §10 提供证据。
4. **测试与文档**:`9fd0111` 对拍表扩到**全 10 种数据类型** + MC/FINS 字符串 + 三协议 `read_tag`/`write_tag`,新增 7 个核心语义用例("读失败重连重发"/"`write_retries=0` 时写失败只发 1 次"/退避门控不污染 `error_count`/`TransportTimeoutError` 不拆连/连接期取消干净清场/`close()` 等锁不锯断在途事务);**原生用例在 Selector 与 Proactor 两种事件循环上各跑一遍**——3.7 Proactor 不支持数据报端点,必须双跑才能证明 UDP 选型正确(也即 §B-1 transport.py:7-37 的"自验"代码即来自此测试)。测试脚手架零新增依赖(`asyncio.run` 包同步测试,无 `pytest-asyncio`)。文档:README 新增"两套异步"选型表 + 原生层用户边界;`architecture.md` 新增 §3(8 条)+ **§12 原生异步层**(复用边界、超时与取消行为表、3.7 实测依据)+ §9 同步 × 异步对拍 + §10 asyncio 可用性;`docs/real-machine-checklist.md` 把原生层标为**独立代码路径**待真机核证。**CHANGELOG v0.39.0 条目详尽**(含 UDP AF_INET 钉死的复现过程、`_await_with_timeout` 与 `wait_for` 的差异理由)。

**C. v0.38.0 增量(转换助手与类型面收口)**:convert `words_to_value` 新增 BOOL/STRING 解码(BOOL 按"非 0 字 → True",不参 `byteorder`;STRING 不限字数、`byteorder` 拼字节后走 `decode_string`,NUL 截断;新 `encoding` 参数默认 ascii,与各 `read_string` 同口径——寄存器文本默认**大端**,读文本需显式 `ByteOrder.BIG`);反向 `value_to_words` 维持只收数值类型(不对称理由写进 docstring,报错文案改为"value_to_words 只支持数值类型")。`stats` 返回 `ClientStats`(TypedDict),9 字段与 `BaseClient.stats` docstring 一一对应,异步镜像同型;**3.7 与 3.8+ 条件分支导出真类型 vs `dict` 子类**——不引 `typing_extensions` 运行期依赖、核心零依赖不动;两套检查器口径不一(mypy 收窄、ty 不认 `TypedDict.copy()`)用 `cast(ClientStats, ...)` 兼容,**零 `# type: ignore`**。`docs/review.md` 入库 v0.37.0 复审修订(`e23c787`)即我此前提交,被 v0.38.0 release 覆盖后本报告按当前 HEAD `94e1328` 重新定位。

**D. K1 / K2 / aio P1-1 / OPC-UA P2-7 状态收口**

| 编号 | v0.37.0 状态 | v0.39.0 状态 | 差异 |
|---|---|---|---|
| **K1** Keyence MC 位组错位 | 🟠 待终核 | 🟠 **复核后现状不改** | `keyence/mc.py` 未覆写 `_translate_address`(记号数字原样发帧,`R515` → 515);项目侧 2026-09-26 复核:第 4 条反证不成立(设备号字段是 **3 字节**,199915 装得下),Mitsubishi SH081257ENG 明文"针对 KEYENCE 除 B 设备外 = 上位字号 + 低 2 位位号",故按"证据不足不改";真机判据(写 `R100` 看 `R100` 还是 `R604` 变)入 `real-machine-checklist.md`(§7.7-B-8) |
| **K2** AB CIP session 泄漏 | — | 🟢 **已修(2026-09-26)** | 原缺陷:`ab.py:119-128` 注册 session 后 Forward Open 失败,base `connect()` 的 `_after_connect` 失败分支只关 transport 不调 `_unregister_session` → PLC session 表残留至超时回收(ControlLogix 典型 ≤16,反复失败重连可耗尽)。修法:基类提供 `_after_connect_failure` 钩子(默认 no-op,**关传输之前**调用;原生基类同名镜像),AB 覆写"尽力 Forward Close + UnregisterSession";+5 例守卫(假传输关闭后拒发 `TransportClosedError`、失败路径注销帧数 == 注册帧数、20 次失败重连零泄漏、成功路径不污染),旧实现上 3 例 FAILED |
| **aio P1-1** 取消语义不真 | 🔴 开放 | 🟢 **结构性解决** | 原生 asyncio 层 `native/base.py:163-167` 通过 `_await_with_timeout` + 取消时按"是否已发出请求"分流(已发 → 保守拆连;未发 → 排空保连接,`CancelledError` 原样传播);**aio 旧 wrapper 仍存在,作为兼容过渡**,新用户走 `native` 即可;若开发者有计划在 v0.40+ 移除 `aio` 包装,建议列入 `real-machine-checklist.md` 标记弃用 |
| **OPC-UA P2-7** 回调竞态 | 🔴 开放 | 🔴 **仍开放** | `opcua/client.py` v0.37→v0.39 零改动 |
| **真机清单 7%** | 30 项 2 通过 ≈ 7% | 仍 7%(清单同步原生层标注独立路径待核证) | `real-machine-checklist.md` 把 `native` 模块标为独立代码路径,需真机核证;清单未新增条目 |

**E. 新增 P3 备忘(不阻塞,留待 v0.40+ 决定)**

- **P3-5** 原生层与 `aio` 包装双轨过渡期:`aio/__init__.py:368-370` 委托同步侧守卫,与同步层和原生层隐式一致——但 `aio` 文档(README "异步"节)未明示"新用户应使用 `native` 包,`aio` 仅为兼容保留";补一句 README 即可。
- **P3-6** `native/transport.py:_await_with_timeout` 自建等待器绕开 `wait_for` 的 3.7→3.11 `TimeoutError` 类变动——但代价是每条 I/O 都新建 `task + wait` 两层;在数千并发场景下有微小开销;3.11+ 可用 `asyncio.timeout` 上下文管理器简化(仍保留显式取消内层 task 的语义)。建议:在 `native/transport.py` 顶部加版本分支(3.7~3.10 用自建、3.11+ 用 `asyncio.timeout`),并加注释引用本备忘。
- **P3-7** `_categorize` 在原生层也走同一份函数(`base_client.py:833`);但原生层抛 `socket.timeout`(TCP recv/send)、`TransportTimeoutError`(UDP recv)与同步层口径**逐字相同**——这是同步 × 异步对拍能成立的核心保证,但**没有任何测试断言 `_categorize` 对同一异常在两层产生同一枚举值**;建议新增 1-2 个 `test_native_*.py` 用例。

**F. 评分影响**:K1 维持高置信待核;K2 新发现 P2(协议服务端会话表耗尽 → 服务端 DoS,**与 K1 同类"既有缺陷"性质**);aio P1-1 由原生层结构性解决。按 §一评分口径:**综合 7.2 维持**;K1+K2 终核前按"高置信待核"计入 §7.4/§7.8 与附录局限。若 K1+K2 双双坐实,协议深度 9 → 8、综合 7.2 → 6.9(0.3 = -0.1 × 2);若仅 K1 坐实,综合 7.2 → 7.1。

---

## 附:评审方法与局限

- **v0.35 轮**:三路并行代码走查(架构 / 协议 / 测试-CI-文档),`file:line` 级引用复核;`pytest --collect-only` 实测用例数;git 时间线与 tag 溯源;黄金报文与关键缺陷的源码核对。
- **v0.36 轮**:增量 diff 走查(`af6d7b7..5cceca6`)+ 独立复现脚本实测 8 个缺陷场景 + 修复批次(`f40da18`/`c05b392`/`e19021d`/`11828ae`/`1ceab51`/`cba5d43`/`cc15693`/`347fb94`/`fd13a42`/`512a86e`/`4ca1df7`)逐 diff 走查与 24 断言复验(§7.7-A)+ **门禁四件套四轮实跑**(806 → 826 → 842 → v0.37.0 tag `a1564b6` 复跑,全绿 / ruff / mypy / ty)。
- **v0.37→v0.39 增量复审轮(本轮 §7.8)**:HEAD 锚点 `94e1328` (v0.39.0,2026-09-25 22:00 落地),14 提交覆盖 4 大类变更(原生 asyncio 首批 + 契约矛盾批 2 + 审核修复 P0/P1 + 测试/文档);仅 §7.8 追加报告,不改动既有 §7.1-§7.7;K2(AB CIP session 漏注销,P2 高置信待核)纳入 §7.4 状态表;门禁第五轮实跑(`a1564b6` → `9c0d2ff` → `94e1328`,842 → 861 → 1,072 全绿 / ruff / mypy / ty)。
- **没做**:未跑 coverage(工具链不存在);未接真机;未做协议 fuzz;FC23/FC43/FINS 加固、原生 asyncio 层(`native/` 模块,5 客户端)、CIP session 漏注销(`K2`)均未在真实 PLC 上验证;K1(Keyence MC 位组错位)未取得 KV 用户手册 MC 章设备编号表与真机对照,仅完成代码链路推演与 16 位字段反证(§7.7-B);`docs/review.md` 在 v0.39.0 期间被 `9c0d2ff` v0.38.0 release 覆盖过我此前 `e23c787` 入库的修订,本报告已按当前 HEAD 重定位。
- **局限**:§4 覆盖率为静态推断而非实测;P1/P2 状态基于复验当日代码(v0.39.0 tag `94e1328`);**v0.36 轮修复**已随 v0.37.0 发布,**v0.38/v0.39 增量修复**(原生 asyncio + 契约矛盾批 2 + P3 全收口)分别随对应版本发布;aio P1-1 由原生 asyncio 层(`native/`)结构性解决(§7.8-D),旧 `aio/` wrapper 仍保留为兼容过渡;OPC-UA P2-7 仍开放;**K1(Keyence MC 位组)+ K2(AB CIP session 漏注销)** 双双高置信待核(§7.7-B / §7.8-D),双双坐实则协议深度 9 → 8、综合 7.2 → 6.9,任一坐实则 7.2 → 7.1。[2026-09-26 项目侧复核与修复:K2 已修(先注销后关传输,含 5 例守卫);K1 反证不成立(设备号字段 3 字节)且存在反向外部证据,按"证据不足不改"处置——上述评分回调暂不适用。]
