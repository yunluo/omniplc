# omniplc 评价审核报告(资深工控视角)

> **基准**:v0.35.0,评审日期 2026-09-25。
> **方法**:架构 / 协议栈 / 测试-CI-文档 三路并行深度代码走查,全部结论经 `file:line` 实测复核;实测数据(行数、用例数、断言数、git 时间线)来自仓库直接统计,不采信文档自述。
> **口径**:本报告为**评价审核**(技术评审),不含核价/商务评估。

---

## 一、总体结论

**综合评分:7.1 / 10**(八项分项评分等权算术均值,57/8 = 7.125)。项目自我定位 `Development Status :: 3 - Alpha`(`pyproject.toml:48`),诚实。

一句话定性:**协议自研深度与测试质量罕见地高,文档深度达商用水准;但真机验证闭环仅约 7%,CI 版本矩阵单薄与异步层正确性存在实质性缺口。** 属于"工程自律极强、验证闭环未完成"的个人冲刺型项目(126 commits / 8 天 / 45 版本)。

### 规模画像(实测)

| 维度 | 实测值 |
|---|---|
| 源码 | 70 文件 / **15,990 行**(`src/omniplc`) |
| 测试 | 41 文件 / **10,983 行** / **742 例** / **1,877 断言** / **299 `pytest.raises`** / 形式主义用例 **0** |
| 协议覆盖 | **15 家品牌/协议**;28 同步 + 28 异步 = **56 个客户端类** |
| 黄金报文 | 23 个 JSON + 3 个独立实现生成器 |
| 文档 | `architecture.md` **94KB**、README 30KB、CHANGELOG 18KB;267 个公开定义 docstring 缺失 = **0** |
| 依赖 | 核心**零第三方依赖**;可选 pyserial / comtypes / asyncua / pyads / python-snap7 |
| 迭代 | 126 commits、45 个版本,**8 天**(2026-09-18 → 09-25) |
| 类型与门禁 | 1013 个 `def` 全标注、`py.typed`;CI 四门禁(pytest + ruff + mypy + ty) |
| 真机验证 | 30 行清单仅 2 项通过 ≈ **7%** |

### 分项评分

| 维度 | 分 | 一句话判词 |
|---|---|---|
| 分层设计 / API 抽象 | **8** | 模板方法教科书级,异步是包装器不是复制 |
| 线程安全 | **7** | 无死锁回路,但有 2 处真实竞态/旁路 |
| 错误处理 | **7** | 三层语义清晰,统计口径有污染 |
| 协议栈深度(自研组) | **9** | 四大栈达生产级 |
| 测试质量 | **8** | 字节级断言、0 形式主义 |
| CI/CD | **7** | push/PR 四门禁 + tag 独立构建的互补触发设计合理;扣:矩阵单薄、lint 规则弱、无覆盖率 |
| 文档 | **8** | 深度超额、呈现不足 |
| 真机验证 | **3** | 29 项适用协议仅 2 项通过 |
| **综合** | **7.1** | 八项等权(57/8 = 7.125) |

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

- 742 例 / 1,877 断言 / `pytest.raises` 299 处 / 形式主义用例 **0** / `unittest.mock` 使用 **0**(全部手写 fake)。
- 发帧**逐字节比对**(`test_modbus_clients.py:33`、`test_fins_clients.py:117`);**五重状态断言**——返回值 + 连接状态 + `last_error_code` + `last_error_category` + `stats` 计数(`test_modbus_clients.py:60-65`)。
- 黄金样本由**独立实现生成器**算 CRC/组帧,规避"实现生成测试"的同源偏差(`tests/golden/generate_modbus_samples.py:21-28`,README 亦明示)。
- 三层假传输(`tests/unit/scripted.py:11-80`):`ScriptedTransport`(分片/粘包)、`ChunkSocket`(size 感知,专防"读满恰好 size"假阳性)、`mount_real_tcp`(真 `TcpTransport` + 假 socket,只 mock 一层)。
- 28/28 具体客户端均有专属测试文件;OPC-UA 有 13 例**进程内起真服务端**集成测试(`test_opcua_client.py:449-768`)。

### 2.5 诚实度与第三方审查闭环

- `docs/review.md` 曾把第三方审查报告入库并逐条追踪 backlog;README:449-465 公示"真机联测待做"表;267 个公开定义 docstring 缺失 = 0。工业库中极少见。

---

## 三、缺陷清单(评审重点)

### 3.1 P1 — 行为级,现场会产生真实故障现象

| # | 缺陷 | 位置 |
|---|---|---|
| 1 | **aio 同步属性阻塞事件循环**:`connected/stats/last_error*/receive_timeout/retries` 实现为同步 property 直转发(`:209-282`),而它们在同步侧都要拿 `self._lock`,该锁在 `_execute` 期间被**整个网络 I/O 周期**持有(`base_client.py:629`)。协程 A 正在 `await read()` 时,事件循环线程执行 `client.stats` 或 `client.receive_timeout = 5.0` 会**同步阻塞循环**最长 `receive_timeout`(默认 3s);`retries>0` + 重连时可达 `retries × connect_timeout`。`architecture.md:297` 只讲"保序",未提示此坑 | `aio/__init__.py:209-282` + `core/base_client.py:629` |
| 2 | **SR 扫码枪能力缺失误拆线**:`KeyenceSrClient._read/_write` 抛 `OmniPLCInternalError` → 走 `_execute:651-654` 断线分支 → `_mark_disconnected()` + 分类 UNKNOWN + `retries>0` 时无谓重连重试。应与 `opentcp:394` 一致抛 `DeviceError`(不断线)。`test_sr_scanner.py:145` 只断言 `last_error` 未断言 `connected`,故未测出 | `scanner/keyence_sr.py:215,219` |

### 3.2 P2 — 口径/竞态,影响监控与异步两大卖点

| # | 缺陷 | 位置 |
|---|---|---|
| 3 | **`device_error_count` 吞超时**:`TransportTimeoutError` 是 `DeviceError` 子类,超时走 `except DeviceError` 分支被 `+= 1`(`:647-650`),违背字段文档"PLC 明确返回错误码的次数(链路完好)"(`:340`);同时 `_categorize` 给的是 `TIMEOUT`——计数与分类口径互斥,看板误读。连带后果:UDP/串口超时**从不重试**(`:650` 直接返回),而 TCP `socket.timeout` 走 `:651` 断线 + 重试——同名 `retries` 跨走线行为不对称。**全库无任何测试注入 `TransportTimeoutError`**(`test_v030_reliability.py:663-674` 只测了 `OSError`) | `core/base_client.py:647-650` vs 文档 `:340` |
| 4 | **`code=0` 污染 `last_error_code`**:`_extract_code` 只把 `TransportTimeoutError` 特判为 `None`,而 `DeviceError(msg, code=0)`(产生点 `base_client:738,745`、`opentcp:394,398`、`keyence_sr:202-204`)会把 `0` 当有效协议码写入,违背 `:324`"无码为 None"声明 | `core/base_client.py:791-803` |
| 5 | **OPC-UA 回调竞态**:订阅回调在 asyncua 线程无锁写 `last_error` 三件套并 `+=1`,违反 `_set_error` 自身"须锁内调用"约定(`base_client.py:363`),与 `_execute` 的 `_clear_error`/计数构成非原子读改写 → 丢计数、成功后错误被污染 | `opcua/client.py:254-262,305-313` |

### 3.3 P3 — 边界与一致性(择要)

- **TCP 陈旧超时**:`recv` 循环内 `sock.settimeout(remaining)`(`tcp.py:104`)返回后不恢复,而 `send`(`:73-81`)不做任何 `settimeout` → 上一事务涓流残留的极小超时会被**下一事务的 `sendall` 继承**,可能误报发送超时并断线+重试。串口侧经核实**无害**(`port.timeout` 只管读,`write_timeout` 独立配置)。
- **能力缺失异常三套口径并存**:`DeviceError`(`opentcp:394`)/ `OmniPLCInternalError`(`keyence_sr:215`,会拆线)/ `ValueError`(`opcua:458,466`、`ads:303`,直接抛给调用方);`mtconnect` 自身 `_read` 抛 `ValueError`(`:378`)而 `_write` 抛 `DeviceError`(`:392`),同文件自相矛盾。
- **`scan()` 绕过事务模板**:手写 `with self._lock` + 惰性重连(`keyence_sr.py:96-129`),丢退避根因保留、`transactions` 计数、重试;同文件 `reset()` 却走 `_execute`(`:139`)——同类两套事务语义。
- `convert.words_to_value` 对 `BOOL/STRING` 抛 `KeyError`(`convert.py:341`),逆函数 `value_to_words` 抛 `ValueError`(`:391`)。
- `_categorize` 冗余分支:`ConnectionRefusedError/ConnectionResetError/socket.gaierror`(`:784-785`)全为 `OSError` 子类,与下一分支结果相同。
- `_execute:622` docstring 过度承诺"所有内部异常转换为 (False, None)",实际 `ValueError/struct.error/KeyError` 会带锁外逃逸(锁本身能正确释放)。
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
| | Modbus TCP/RTU | **9.5** | 18 黄金样本(含 FC23 规范 §6.17 示例逐字节)、CRC16 known vector、异常回显功能码错配校验、FC22、RTU 广播语义;**FC 01/02/03/04 连续地址合并读**(按 (area, kind, width, dtype) 5 类分组,读 N 个连续点 ≈ 1 笔事务)、**FC 15/16 连续地址合并写**(同上 + 寄存器位走 RMW 不入合并)、**FC 23 单事务读写多寄存器**、**FC 43/14 设备标识**(标准对象名映射 + More Follows 自动翻页 + RTU 按对象头增量收包)、**起始地址+数量越界组帧期拒绝**;**无 ASCII 走线、无 BCD、无文件记录/FIFO/串行诊断类功能码**;规范参考 [Modbus 应用协议 V1.1b3](https://www.modbus.cn/modbus-specifications) |
| | 欧姆龙 FINS TCP/UDP | **9.0** | 握手后节点自动分配(`omron.py:464-489`)、UDP 节点由 IP 末段推导、0104 批量、ICF 回显 + 长度 + 错误域三重校验;**真机 CP1H 已录** |
| **B 可用** | OPC-UA | **8.0** | 适配层最厚:NodeId 解析、类型映射、Browse(`:546`)、DataChange/Event 订阅、UA Read 批量(`:504`);扣:asyncua 1.1.5 钉版、无 TLS/X.509、无历史/聚合 |
| | MX Component | **7.5** | 798 行 ctypes/vtable 深度适配、HRESULT/返回码双校验、`write_batch`(`:753`);**真机 FX3U 两轮踩坑修复**(CHANGELOG v0.31.0/v0.31.1) |
| | KV MC(SLMP)/ 汇川 MC / 松下 MC | **7.5–8.0** | 继承 MC 全栈只换码表(`inovance/mc.py:76` 等),地址翻译挂批量与帧双路径一致(`:110-135`)——省成本且不漂移 |
| | MEWTOCOL / KV Host Link | **7.0–7.5** | BCC 自研 + 手册黄金向量(`test_panasonic_mewtocol_clients.py:43`,BCC=1D);Host Link 协议本身无 BCC,行收包带整段 deadline |
| | Omron CIP(NJ/NX) | **7.0** | 继承 AB,但**字符串显式不支持**(`cip.py:80-90`)、布尔元素非 Logix 打包(`:14`)、零真机 |
| **C 薄封装** | 西门子 S7 / 倍福 ADS | **6.0** | snap7/pyads 转接,协议价值在第三方库;S7 无块读写/SZL(`client.py:31`),地址解析是唯一无 `lru_cache` 的(`siemens/address.py:55`);ADS 无 address.py |
| **D 早期** | TOYOPUC / MTConnect / OpenTcp | **6.5** | TOYOPUC 命令面窄(仅 1C–21,扩展区留后续 `toyopuc.py:28`)、无黄金样本;MTConnect 零依赖健壮但**只读**(`:390-392`) |

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
| 6 | **迭代节奏与治理** | 8 天 45 版(日均 5.6 版);tag v0.1.0–v0.34.0 **同日批量回补**;CHANGELOG 无日期;commit 类型前缀非 100% 统一——长期维护性无历史证据 |
| 7 | **可持续性** | 单一作者、bus factor = 1;`snap7/asyncua/pyads` 上游 breaking change 会传导 |
| 8 | **国际化与呈现** | 全中文、无 API 文档站(mkdocs/Sphinx 均无)、README 无任何 badge、标题层级断在 h4、`CONTRIBUTING.md:36` "721 例"与实测 742 已漂移 |

---

## 六、评审结论

### 6.1 结论

**准予通过(附条件)**:

- 作为**技术预研 / 内部代码资产 / 国产 PLC + Python 统一 API 的选型基线**——质量优秀,四大自研协议栈(Modbus/MC/FINS/CIP)达可交付生产水准,测试与文档超同类开源均值。
- 作为**生产级交付物**——需完成 §6.2 整闭环方可验收;当前 Alpha 状态下,主力协议(MC 以太网/串口、Modbus、S7、AB)缺真机背书是最硬的短板。

### 6.2 优先整改清单(按投入产出排序)

| 优先级 | 项 | 位置 / 说明 |
|---|---|---|
| **P0** | aio 属性阻塞事件循环 | `aio/__init__.py:209-282` 改 `await self._run(lambda: ...)`,或至少文档化"可能停顿循环"并告警 |
| **P0** | SR 误用拆线 | `keyence_sr.py:215,219` 改抛 `DeviceError(..., 0)`(对齐 `opentcp:394`);`test_sr_scanner.py:145` 补 `assert client.connected is True` |
| **P1** | `TransportTimeoutError` 单独分支 | `base_client.py:647-650`:不计入 `device_error_count`;是否重试显式决策并补注入该异常的回归测试 |
| **P1** | `code=0 → None` | `_extract_code`(`:791-803`)统一处理 |
| **P1** | OPC-UA 回调加锁 | `opcua/client.py:254,305` 提供加锁版 `_set_error` 入口 |
| **P1** | CI 版本矩阵 + Linux 腿 | 3.7/3.9/3.12 × windows/ubuntu,否则 classifiers 是虚假承诺 |
| **P1** | 覆盖率门禁 | 引入 `pytest-cov`,协议 codec 层阈值 ≥85% |
| **P1** | 回填真机清单 | 优先 **Modbus TCP + MC 3E**——对可信度伤害最大的两项空白 |
| **P2** | 统一"能力缺失"异常口径 | 全库对齐 `DeviceError`(不断线);`mtconnect` `_read/_write` 自相矛盾先修 |
| **P2** | TCP 陈旧超时 | `send` 入口显式 `settimeout(receive_timeout)` |
| **P2** | `scan()` 并入 `_execute` | `keyence_sr.py:96-129` 消除第二套事务语义 |
| **P3** | 连接池 / fuzz / 异步原生 asyncio / TLS / AB UDT 整读 | 长期路线图 |

### 6.3 分项评分依据摘要

| 维度 | 分 | 支撑 | 主要扣分 |
|---|---|---|---|
| 分层设计 / API | 8 | 三档钩子 + 事务模板 + 批量可覆写;异步包装器 + 内省门禁 | aio 属性阻塞、会话型伪装传输违反 LSP、构造默认值双写 |
| 线程安全 | 7 | 单 RLock 整事务原子 + full-jitter 门控(`:667-680`)+ 8 线程并发回归;**无死锁回路** | OPC-UA 回调无锁、`scan` 旁路、`retries` 锁外读、无法打断进行中 I/O |
| 错误处理 | 7 | 三层语义文档与实现一致;`_categorize` 顺序正确且有 10 例专项测试 | 超时吞计数、`code=0` 污染、能力缺失三套口径、docstring 过度承诺 |
| 协议栈深度 | 9 | 四大栈全自研 + 黄金样本 + 长度域/回显/序列号全校验 | 真机 7%;4/15 为薄封装;写侧批量、BCD/结构体缺失 |
| 测试质量 | 8 | 742 例字节级断言、三层假传输、独立生成器、28/28 覆盖 | 无覆盖率实测、无 fuzz、无并发压测、黄金样本仅 3/15 协议 |
| CI/CD | 7 | push/PR 四门禁 + tag 纯构建的互补触发(标签取自已过门禁的 master 提交,无"未测即发布")+ 双类型检查器 + OIDC 发布 + fork 守卫 | 单 OS 单 Python、ruff 规则集弱、无 coverage/SAST |
| 文档 | 8 | 94KB 架构文档逐协议列手册章节、267/267 docstring、诚实公示待核证 | 纯中文、无 API 站、无 badge、CHANGELOG 无日期、数字漂移 |
| 真机验证 | 3 | 清单专业 + 待做项诚实 + 36 个联测脚本 + MX 两轮真机根因记录 | 29 项适用协议仅 2 项通过 |

---

## 附:评审方法与局限

- **做了**:三路并行代码走查(架构 / 协议 / 测试-CI-文档),`file:line` 级引用复核;`pytest --collect-only` 实测用例数;git 时间线与 tag 溯源;黄金报文与关键缺陷的源码核对。
- **没做**:未运行完整测试套件与 ruff/mypy(避免写缓存/依赖安装);未跑 coverage(工具链本就不存在);未接真机;未做协议 fuzz。
- **局限**:§4 覆盖率为静态推断而非实测;P1/P2 缺陷为代码走查结论,建议按 §6.2 补回归测试后复验。
