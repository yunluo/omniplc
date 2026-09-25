# omniplc 项目审核报告(资深工控视角)

> **基准说明**:本报告第一轮基于 v0.32.1(第一轮原始记录,保留不改)。第二/三轮复审见文末 **「十二、第二/三轮复审」**,基准 v0.34.0 + v0.35-WIP(2026-09-25),综合评分修正为 **8.1/10**。

## 一、项目定位与价值判断

omniplc 是一个面向中国工控市场(三菱/欧姆龙/基恩士/汇川/松下/丰田)兼及欧美主流(AB/倍福/西门子)的 **Python 统一 PLC 通信库**,v0.32.1,作者署名"云落"。其差异化定位在 **"国产 PLC 全栈 + 统一 API + 零核心依赖"**,这恰好是 pymodbus、python-snap7、asyncua 等单一协议库做不到的,也是 HslCommunication(.NET 商业)留下的生态位。

---

## 二、架构质量(优秀,8/10)

**做得对的事:**

| 设计 | 评价 |
|---|---|
| `BaseClient` 模板方法 + 类型化方法只写一次 | 工业库常见但少有做得这么干净的——`_read/_write` 两个原语覆盖20+ 类型化方法,新增协议成本极低 |
| `BaseTransport` 抽象 + 三走线实现 | 协议层不耦合 socket,新增 Modbus over TLS、Modbus over HTTP 不动 Modbus 实现 |
| codec全部纯函数 | 黄金报文样本(`tests/golden/*.json`)可做无硬件测试,这是 pyhsl、pylogix 都缺乏的 |
| `RLock` 单锁 + 整事务原子化 | 简单且正确,符合工业现场"少并发、保正确" |
| 协议原生批量 (`read_batch`) | MC 0406 / FINS 0104 / CIP 0x0A 真正"单事务",而不是循环单点——这比很多商用库都到位 |
| 异步镜像用 `组合 + ThreadPoolExecutor` | 协议逻辑只写一份,显著降低维护成本;STA 模型下 MX Component 天然契合 |
| 全类型标注 + `py.typed` + mypy + ty 双检查器 | 工业控制代码长期维护的关键,作者自律性强 |

**架构隐患:**

1. **异步侧性能欠优**:`ThreadPoolExecutor` 组合同步客户端,每台设备一个线程,数百设备并发会触发 GIL/线程切换开销。asyncio + 原生异步 socket 才能线性扩展。
2. **未引入连接池**:单客户端持锁串行化,意味着同一 PLC 多消费者必须各自开连接——文档虽承认,但没给"连接池"选项(架构图 §3 明示"留 v1.x")。
3. **无重连退避**:当前是"一次失败立即重连"模式,PLC 断电/网线松动会触发风暴。建议引入 `reconnect_backoff`(指数退避 + 抖动)。

---

## 三、工业可靠性设计(出色,9/10)

这一块是本项目最值得称道的部分,真实踩过现场坑:

- **整事务 deadline**(v0.30.0):涓流对端逐字节重置超时——原 MC 4C 最长 ~54 小时被收口,**这是真实攻击/故障场景**
- **网络长度域上限快失败**(v0.30.2):FINS `0xFFFFFFFF` →350MB 灌包被拦下,符合工业网络安全基线
- **TCP/UDP/串口超时语义分流**:TCP 拆连防串帧、UDP/串口超时不断线——文档对"为什么"讲得很清楚(`errors.py:42-49`)
- **TCP keepalive**(v0.30.0)+ Linux/Windows 平台差异处理:防止 PLC 断电半开连接
- **`BaseClient.stats`** 健康统计:现场"多久没成功"判断直接可用
- **TCP_NODELAY** + **整段 deadline**:小报文 PLC 通信的标配
- **MQ 握手 vs 字节流的错误翻译边界**(`S7Session`/`AdsSession` 等):会话型协议的失败分类是工业现场 debug 的关键

**还能加什么(加分项,不算扣分):**

- 上行心跳/订阅(用于 KeepAlive 异常时主动探测对端)
- 网络异常分类(ConnectionRefused / Timeout / Reset / DNS)细化到 `last_error`,便于上位系统分类告警
- 熔断器:连续 N 次失败后短时拒绝请求,避免重连风暴
- 报文录制回放(把现场报文存盘,离线复现)

---

## 四、协议覆盖广度(广,但有深度不均)

**协议清单(28+28 = 56 个客户端类):**

| 厂商 | 协议 | 覆盖度 |
|---|---|---|
| 三菱 | MC 3E/4E/1E + 串口 3C/4C + MX Component | ★★★★★ 极完整 |
| 欧姆龙 | FINS TCP/UDP + NJ/NX CIP(继承 AB) | ★★★★★ |
| 罗克韦尔 AB | EtherNet/IP Logix 标签 + connected 消息 | ★★★★★ 含0x0A 多服务包 |
| 基恩士 | KV Host Link TCP/UDP + MC 兼容(SLMP)TCP/UDP + SR 扫码枪 | ★★★★★ |
| 汇川 | H3U/H5U Modbus TCP/RTU + MC 兼容 | ★★★★☆ |
| 松下 | FP MC 兼容 + MEWTOCOL TCP/UDP | ★★★★☆ |
| 丰田 | TOYOPUC TCP/UDP | ★★★☆☆ |
| 倍福 | TwinCAT ADS(封装 pyads) | ★★★☆☆(依赖上游) |
| 西门子 | S7(封装 python-snap7,双版本兼容) | ★★★★☆ |
| Modbus | TCP/RTU + FC22 掩码写 | ★★★★★ |
| OPC-UA | 读写(封装 asyncua) | ★★★☆☆ |
| CNC | MTConnect(零依赖标准库) | ★★★★☆ |
| 通用 | OpenTcpClient(分隔符/定长成帧) | ★★★★★ |

**深度不均的几处:**

1. **OPC-UA 仅基本读写**——订阅(最常用特性)、方法调用、历史访问、安全策略都没做
2. **MTConnect 仅只读**——写入、`/sample` 历史流、订阅协议都未实现,无法做 SPC/品质分析
3. **AB 缺 UDT 整体读取**:工业现场大量 UDT/UDT 数组,只能逐字段拉,效率差
4. **倍福 ADS 是 pyads 转发**——没有自己的实现,版本/平台绑死在 pyads
5. **西门子 S7 仅支持基本数据类型**——无 S7-1200/1500 优化块访问(需用 PUT/GET),对新型 PLC 不友好
6. **OPC-UA 标签浏览**(Browse)未暴露——现场接入第一件事就是"枚举所有点"

---

## 五、测试与质量保障(良好,7.5/10)

**做得好的:**

- 黄金报文样本 20+ 个,3 个生成脚本(`tests/golden/generate_*.py`),覆盖 Modbus/MC/FINS 三家
- 100+单元测试,涵盖每个协议客户端 + base_client + transport + 收包语义
- 脚本化假 socket(`scripted.ChunkSocket`)、真实回环 echo 服务(`conftest.py`):测试真实传输/收包粘包行为,这是工业通信库的核心难题
- 整事务 deadline / 网络长度上限 / MTConnect 响应体上限等安全回归测试(v0.30.2 补 9 例)
- 真机核证清单写在 README(诚实标注"待真机核证"的项,这是工业库很难做到的诚信)

**欠缺:**

- **缺少协议仿真服务器**:像 pycomm3 的 `logixdriver`、HslCommunication 的 DemoBox,无法在 CI 跑真机回归
- **缺少 fuzz/模糊测试**:工业协议最怕"畸形报文导致 OOM/死锁",长度域限制已加但缺随机畸形流测试
- **并发测试薄弱**:仅单线程 RLock 测试,没有"多线程同时读写同一客户端""100 客户端并发"压力测试
- **MX Component 真机覆盖度低**——作者自述,MX 报头错误信息(ActSupportMsg)待核证

---

## 六、工程化与发布(出色,9/10)

- `pyproject.toml` 用 hatchling + 显式 extras(serial/mx/opcua/ads/s7),按 Python 版本自动选 snap7 1.3/3.x——这是非常成熟的依赖管理
- 双构建矩阵兼容 py3.7~3.12,`py.typed` 让下游拿到类型检查
- CI:GitHub Actions + OIDC 可信发布到 PyPI(`fork` 守卫)——企业级做法
- LICENSE: MIT,商业友好
- 路线图(README 末尾)按版本降序,变更日志极其详尽
- `architecture.md` 932 行,涵盖分层/继承/线程/状态机/类型/地址语法/协议矩阵——文档完整度在工业开源项目中罕见

**扣分点:**

- `dist/.gitignore` 在 v0.32.1 才修复发布物污染——说明此前确实出过事故
- `.python-version` 钉 3.7.9 但 CI 用 3.12 构建——已在 v0.31.4 修复
- 没有 `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` / issue模板——不利于社区贡献

---

## 七、API 设计

**优点:**

- `(bool, value)` 读返回 / `bool` 写返回 / `last_error` —— 与 pyhsl 一致,迁移成本低
- `with client:` 上下文管理器
- `read_batch` / `read_many` 全库统一契约,**单事务而非循环**
- `DataType` / `McFrame` / `WordOrder` / `ByteOrder` 枚举,IDE 自动补全友好
- `Tag` / `TagTable` 可选点位表,自动缩放(`scale`)
- `configure_serial()` 串口独立入口,符合"改参数 = 换对端"四分法

**扣分:**

- `read_batch` 整批容错 vs `read_many` 逐点容错——命名差异(都是"批")容易混淆,README 第 273 行写了契约但实际仍可能误用
- 失败时 `last_error` 是 str,丢失结构化信息——想做告警分类时只能 `str.contains`
- 类型化方法20+ 个 + `read(addr, dtype)` 通用方法并存,有些冗余
- 异步类用 `A` 前缀而非 `Async` 后缀,虽然与 pyhsl 一致,但与 Python 社区惯例(asyncio 协程、aiohttp、aiofiles)反向

---

## 八、安全与权限

- **无凭据管理**:OPC-UA 用户名/密码、S7 密码、ADS 安全端口都没暴露——现场如果用安全策略会很尴尬
- **无 TLS 支持**:TCP/OPC-UA 都没法启用 TLS,工业网络隔离虽然常态但合规要求日趋严格
- **OPC-UA 安全策略未覆盖**:None/Sign/SignAndEncrypt 三档缺位
- **会话型适配器的会话清理**(`_MxComLink`/`_AdsSession`)已正确处理 `Close()` + `CoUninitialize()`,这是真实踩过的坑,加分

---

## 九、与同类对比

| 维度 | omniplc | HslCommunication | pylogix/pycomm3 | pymodbus |
|---|---|---|---|---|
| 语言 | Python | .NET | Python | Python |
| 协议覆盖 | ★★★★★(国产强) | ★★★★★(国产最强) | ★(仅 AB) | ★(仅 Modbus) |
| 商业授权 | MIT 自由 | 商业收费 | MIT | BSD |
| 类型标注 | 100% + py.typed | N/A | 部分 | 无 |
| 真机核证 | 部分(文档诚实标注) | 厂商背书 | 较全 | 全 |
| 异步 | 镜像(线程) | .NET Task | 无 | 无 |
| OPC-UA | 仅基本 | 含 | 无 | 无 |

**结论**:对 **国产 PLC 集成 + Python 场景**,omniplc 是当前最完整的开源选择,商业替代品仅有 HslCommunication。

---

## 十、风险与建议

### 商业风险

1. **个人维护项目**:单一作者署名,bus factor = 1,企业接入需评估可持续性
2. **国产 PLC 协议细节**:H3U/H5U 地址映射、汇川 MC 配置等依赖现场经验,文档齐全但缺乏"现场 FAQ"
3. **snap7/asyncua/pyads 版本绑定**:上游 breaking change 会传导到 omniplc

### 技术建议(优先级排序)

| 优先级 | 项 | 理由 |
|---|---|---|
| P0 | 引入重连退避(exponential backoff) | 防断电重连风暴 |
| P0 | 失败结构化(`last_error_code` 枚举) | 上位系统分类告警 |
| P1 | OPC-UA 订阅 + Browse | OPC-UA 主要应用场景 |
| P1 | AB UDT 整体读取 | 大幅提升 AB 吞吐 |
| P1 | 仿真服务器(每协议 stub) | CI 真机回归前置条件 |
| P2 | TLS 支持(TCP/OPC-UA) | 合规要求 |
| P2 | 连接池 | 多消费者场景 |
| P2 | 异步原生 asyncio(非 ThreadPoolExecutor) | 大规模并发 |
| P3 | 协议级 fuzz 测试 | 健壮性 |
| P3 | OpenTelemetry/Prometheus 指标 | 可观测性 |

---

## 十一、综合评分

| 维度 | 评分 | 评语 |
|---|---|---|
| 架构设计 | 8.5/10 | 模板方法 + 协议原生批量是亮点;异步性能有优化空间 |
| 工业可靠性 | 9.0/10 | 整事务 deadline + 长度域上限 + 超时语义分流——真正踩过坑 |
| 协议覆盖广度 | 9.0/10 | 国产 PLC 之王;OPC-UA/MTConnect 偏只读 |
| 测试质量 | 7.5/10 | 黄金报文 + 脚本化 socket 优秀;缺真机仿真与模糊测试 |
| 文档完整度 | 9.5/10 | 932 行 architecture.md + 详尽 README + 路线图 |
| API 设计 | 8.0/10 | pyhsl 风格熟悉者无门槛;`last_error` 结构化可改进 |
| 工程化 | 9.0/10 | hatchling + 双检查器 + OIDC 发布 |
| 安全 | 6.0/10 | 长度上限有,TLS/凭据/OPC-UA 安全策略全缺 |
| 可持续性 | 5.0/10 | 个人项目,企业接入需评估 |

**综合:8.0/10**——是一个**工程质量上乘、协议覆盖完整、工业级可靠**的开源 PLC 通信库,在国产 PLC + Python 场景下是首选;短板在个人维护的可持续性、安全/认证、异步扩展性。

---

## 附录:后续可深入方向

1. **架构改造建议**(异步侧迁移 asyncio 原生实现、连接池设计)
2. **安全加固路线图**(TLS/凭据/OPC-UA 安全策略分阶段落地)
3. **某协议的真机核证清单**(基于 README "待核证"项的优先级排序)
4. **OPC-UA/MTConnect 高级特性扩展设计**(订阅 / 历史 / 方法调用)
5. **第三方库选型与替代方案对比**(针对具体技术栈)

---

## 十二、第二/三轮复审(2026-09-25,基准 v0.34.0 + v0.35-WIP)

> 本章为合并复审记录:第二轮勘误基线与反馈落地核验,第三轮新增字节层核验、GitHub Actions 运行实况取证与记录一致性检查。方法升级:黄金报文逐字节拆解、Actions run 页面抓取、`uv` 命令语义验证、git 提交时间线溯源。复审期间仓库处于活跃开发状态(v0.35 OPC-UA 订阅 WIP 在工作区未提交)。

### 12.1 勘误

| 轮次 | 原结论 | 勘误 |
|---|---|---|
| 轮1 | 基准 v0.32.1 | 实际代码已演进至 **v0.34.0**(`__init__.py:77`),v0.35 OPC-UA 订阅部分在工作区 |
| 轮2 | "README CHANGELOG 引用失效" | 第三轮复核:`CHANGELOG.md` 已存在(`244ee81`,48 行),README 引用有效——该问题不再成立 |
| 轮1 | 工程化 9.0/10 | 后续加了 CI 门禁(见 12.3,当前为红灯)——第三轮修正为 8.0 |

### 12.2 反馈吸收时间线

`bf42e55`(第一轮报告入库)之后的提交序列:

| 提交 | 内容 | 对应审查项 |
|---|---|---|
| `ec1ff4f` | 代码审查收口(门控根因 + 传输超时码) | 轮内自审 |
| `7b8bf09` | push/PR 强制门禁 CI(pytest+ruff+mypy+ty) | 轮1 工程化扣分 |
| `244ee81` | CHANGELOG.md 创建 + README 引用 | 轮2 |
| `daebdf2` | CONTRIBUTING/CoC/SECURITY + PR/Issue 模板 | 轮1 社区文件 |
| `da42c29`/`21bd60d` | 真机联测清单 | 轮1 测试基建 |
| `f68027f` | ty 第二类型检查器 | 轮1 工程化 |
| `43f13d4` | v0.35 OPC-UA 订阅+Browse 设计(spec 明引 `review.md §10 P1`) | 轮1 P1 |

第一轮双 P0(重连退避/失败结构化)已在 v0.34 落地(`553a9ca`/`3cfac81`/`a6ff4ab`,v0.34 设计文档日期 9-22 早于报告入库,不能全归因于本审查)。

**模式观察**:反馈吸收速度极快(一天内 CI+社区文件+清单全落地),但**建设快于闭环**——CI 红灯 3 次无人应答、真机清单建表未回填(见 12.3)。加分驱动,缺校验环节。

### 12.3 硬发现(第三轮新增)

**① CI 强制门禁三连红,从未通过(最严重)**

`ci.yml`(由 `7b8bf09` 加入的"强制门禁")在 GitHub Actions 上连续 3 次 Failure:

| 运行 | 触发提交 | 耗时 | 结论 |
|---|---|---|---|
| ci #1 | `da42c29` | 11s | 失败 |
| ci #2 | `21bd60d` | 13s | 失败 |
| ci #3 | `f68027f` | 13s | **Failure,exit 2**(run 36021004162 页面核实) |

根因已定位:`ci.yml:31` 使用 `uv sync --group dev`,但 `pyproject.toml:81` 的 `dev` 定义在 `[project.optional-dependencies]`(extra),仓库无 `[dependency-groups]` 表。`uv sync --help` 已核实 `--extra`↔optional-dependencies、`--group`↔dependency-group 的对应关系;uv 报组不存在、退出码 2,与运行注记吻合,8 秒内挂掉——pytest(660 例)从未执行。**修复是一行**:`--group dev` → `--extra dev`。

**② 发布流水线零测试门禁**

`build-release.yml`(#1-#7,7 次发布已上 PyPI)不含任何测试步骤;唯一应为门禁的 `ci.yml` 当前为红。发布路径至今没有自动化测试把关。

**③ 真机核验清单是空表,与 README 声明矛盾**

`docs/real-machine-checklist.md` 结构与填法约定专业,但**全部记录行为空**;而 `README.md:436` 声称 MX Component"读写/批量/CPU 型号/时钟已真机核证"。清单系 `da42c29` 新建的模板,历史核证记录未回填——记录体系与对外声明当前不一致。

**④ 工作区卫生(v0.35 进行中痕迹)**

- 未提交 WIP:+916 行 OPC-UA 订阅(`aio/__init__.py`、`opcua/__init__.py`、`opcua/client.py`、`test_opcua_client.py`)
- 4 个未跟踪临时文件未被 gitignore 覆盖:`_o.txt` / `_p.txt` / `_opcua_test.txt` / `_tmp_check.py`
- `_opcua_test.txt` 内容 `...................FF.F...FF`——WIP 存在失败测试痕迹
- 提醒:v0.35 入库前须清理残留并建议 `.gitignore` 补临时文件模式

### 12.4 字节层核验(通过)

**黄金报文逐字节核验**(`tests/golden/mc_3e_bit_read_001.json`):

```
请求 5000 00 ff ff03 00 0c00 0a00 | 0104 0100 0a0000 90 0300
      ↑副头 ↑网 ↑PC ↑I/O ↑站 ↑长12 ↑定时10 ↑批量读 ↑位子命令 ↑M10(3B LE) ↑M码90 ↑3点
响应 d000 00 ff ff03 00 0400 | 0000 1010
      ↑D0  ↑网 ↑PC ↑I/O ↑站 ↑长4  ↑结束码0 ↑位打包 1,0,1
```

与三菱 SH-080008 手册一致;位打包 0x10(高半字节)/0x01(低半字节)正确,`codec_qna.py:143` 解析吻合。黄金向量是真对过手册的,不是摆设。

**解码防御一致性**:`_execute`(`base_client.py:649`)只捕 `OSError`/内部异常,曾担心畸形响应 `IndexError` 穿透——实测各 codec 解码前均有长度校验并转 `ProtocolFrameError`:MC `codec_qna.py:281`、Modbus `codec.py:188` + `modbus.py:376` 按 `expected_response_length` 收包、FINS `codec.py:213/259/338` 三处、MC 串口/1E `codec_serial.py:313`/`codec_a.py:141`。**全库纪律一致,畸形响应不会逃逸为未捕获异常。**

**UDP 超时语义**:`udp.py:88-91` 将 `socket.timeout` 映射为 `TransportTimeoutError`(DeviceError 子类)→ 不断线,符合数据报整收无残留的链路特性,与 TCP 拆连防串帧语义正确分流。

**测试基建亮点**:`test_aio_mirror_surface.py` 用内省守卫把"异步镜像遗漏"这一历史事故固化为测试。

### 12.5 修正评分

| 维度 | 轮1 | 轮3 | 变动理由 |
|---|---|---|---|
| 架构设计 | 8.5 | 8.5 | 不变 |
| 工业可靠性 | 9.0 | 9.5 | 字节层核验 + v0.34 退避/错误分类背书 |
| 协议覆盖广度 | 9.0 | 9.0 | 不变 |
| 测试质量 | 7.5 | 8.0 | 660 例 + 镜像守卫 + 黄金向量实证;门禁红=测试未进流水线 |
| 文档完整度 | 9.5 | 9.5 | CONTRIBUTING/CoC/SECURITY/CHANGELOG 补齐 |
| API 设计 | 8.0 | 8.5 | 失败结构化落地(`ErrorCategory`/`last_error_*`) |
| **工程化** | 9.0 | **8.0** | ⬇ CI 三连红未修 + 发布路径零测试门禁 |
| 安全 | 6.0 | 6.5 | 安全文档落地;TLS/凭据/OPC-UA 策略仍缺 |
| **记录一致性** | — | **7.0** | 新维度:真机清单空表 vs README 声明矛盾 |
| 可持续性 | 5.0 | 5.5 | issue/PR 模板 + 路线图公开 |

**综合:8.0 → 8.1/10**——三轮反馈吸收与代码质量拉高分值,工程化红灯与记录矛盾抵消部分涨幅。

### 12.6 行动清单(建议,未执行)

| 优先级 | 项 | 说明 |
|---|---|---|
| P0 | `ci.yml:31` `--group dev` → `--extra dev` | 一行修复,重推验证 CI 变绿 |
| P0 | `build-release.yml` 构建前插入测试步骤 | 发布依赖测试通过 |
| P0 | 红灯响应机制 | 3 次失败运行无人应答,建议开启 GitHub Actions 通知 |
| P1 | 回填真机清单 MX 已核证项,或撤回 README"已真机核证"措辞 | 声明与记录二选一对齐 |
| P1 | `.gitignore` 补临时文件模式 + 清理 4 个残留 | 防止 v0.35 误入库 |
| P1 | 修复 `_opcua_test.txt` 中的失败测试再提交 | v0.35 发布前全量测试 |
| P2 | 连接池/协议 stub 仿真/TLS 的 spec 排期 | 轮1 轮2 建议仍有效 |