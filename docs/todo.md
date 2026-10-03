# 待办清单(todo)

> 全项目待办集中登记,取代散落各处的临时备注。
>
> 优先级:
> - P0 数据破坏 / 假成功
> - P1 静默错读 / 契约错
> - P2 一致性 / 测试缺口
> - P3 轻微 / 披露
>
> 关联台账:
> - `docs/review-1005.md`(逐协议深查)
> - `docs/review-1006.md`(监视器专项)
> - `docs/real-machine-checklist.md`(真机核证)
> - `docs/protocol/README.md`「待补」表

---

## 排期实现

> 按**实现简单程度**从易到难排列(2026-10-03 重排);已完成项沉底归档。

| # | 项 | 优先级 | 依据 / 出处 | 状态 |
|---|---|---|---|---|
| 1 | README「AI 欢迎策略」章节 | P3 | libplctag AI Policy 先例;已落地——**不排斥 AI 开发,但必须知道自己的代码有什么作用和干嘛的,对自己提交的代码负责;对待 AI 和对待 IDE 一样,都是工具**(用户口径,2026-10-03) | 已完成 |
| 2 | AB `list_tags` 点位枚举 | P2 | CIP Get Instance Attribute List(服务 0x55)+ Symbol Object(0x6B20);帧面经 pycomm3/pylogix 双参考实现对照裁决(1756-PM020 手册待补,protocol「待补」已登记);sync+aio 双层、自动分页、`AbTagEntry` 根包导出;+6 例测试(黄金帧/分页/坏帧/停滞/aio);真机核证项入清单 | 已完成(待发版) |
| 3 | Monitor 点位级死区(deadband) | P3 | 2026-10-03 现场调研「数据质量三害」:当前 `_changed` 严格不等比较,浮点传感器抖动逐周期误发 `on_change`;质量三态(INITIAL/GOOD/STALE)已有(review-1006),仅缺值变化死区——设计点:点位级 `deadband` 参数(|新-旧| < deadband 视为未变,快照照常刷新) | 待实现 |
| 4 | 错误现场环形缓冲(报文黑匣子) | P3 | 2026-10-03 现场调研:`set_debug` 是实时打印,进程崩溃后报文现场丢失,现场无人盯日志时无从排查;已落地——`set_frame_recorder(enabled, capacity=1000)` 只存不打印(1~100000 帧构造期校验),`recorded_frames()`/`clear_recorded_frames()`/`FrameRecord`(墙钟时间+方向+标识+原始字节),挂点与实时日志同在 `log_frame`(走线型全量覆盖,会话型不进),+8 例测试;排障指南 §四同步用法 | 已完成 |
| 5 | 串口原生异步层(3.8+ 环境标记) | P3 | pymodbus RTU asyncio 先例(serial_asyncio);RTU 不需要数据报端点,"3.7 Proactor 限制"仅约束 UDP——常规工程:新增依赖线+AsyncSerialTransport+RTU 客户端+双循环测试 | 待实现 |
| 6 | OPC-UA 断线自动重订(选项) | P3 | 2026-10-03 现场调研:会话/订阅恢复是 OPC-UA 现场普遍痛点(TransferSubscriptions 失败、订阅 stale,常见解法竟是重启服务);现状=断线不自动重订已披露(`plc/opcua/client.py`)——设计点:重连成功后自动重建订阅的选项(默认关),注意死区订阅 asyncua 私有 API 跨版本兼容坑先例 | 待实现 |
| 7 | examples 范例丰富化:各协议对外 API 全展示 | P3 | 2026-10-03 用户指令:「使用范例要丰富,把本库对外 API 都展示下」;已落地——新增「通用 API 面」大节(读写原语/超时重试退避心跳/告警分级/stats/点位表/Monitor/批量读写/全局开关含黑匣子/异步两层),各协议节补缺(MC random 双组签名+MX 时钟/错误码、FINS 0101、AB list_tags/list_identity/属性/GenericMessage、NJ 拒绝披露、S7 get_cpu_state/write_wstring、SR bank/reset、MTConnect sample/assets),新增「批量写」与「默认端口对照」两节(S 跨协议语义提示);API 清单经 inspect 全量盘点防漏 | 已完成 |
| 8 | FANUC FOCAS DLL 封装(`cnc/`) | P3 | **计划见下节「DLL 封装族」**;fwlib32/64.dll,厂商运行库前置——模板现成(海康 SDK 先例),卡在外部物料(FOCAS 手册+头文件) | 计划已列 |
| 9 | 三菱 CNC EZSocket DLL 封装(`cnc/`) | P3 | **计划见下节「DLL 封装族」**;SDK/手册待拿,绑定形态(ctypes/comtypes)拿到后裁决——比 FOCAS 多一层形态不确定 | 计划已列 |
| 10 | S7comm 自研(**直接替换** snap7 封装,计划见下节) | P2 | 2026-10-03 用户四点拍板:`SiemensS7Client` 名字与 API 不变、内部重写为纯 Python S7comm 栈、python-snap7 依赖整体退役、S7 回归核心零依赖;参考源定稿 python-snap7 3.2.0(纯 Python 重写,MIT)为主 + Sally7/S7netplus 交叉;3.7 无障碍(纯 TCP 三层栈)——数周级最大件 | 计划已列 |
| 11 | 日立(Via Mechanics)MARK 30/50/55 钻孔机数采 | P3 | **计划见下节;暂缓**(2026-10-03 用户裁决:先列计划,暂不考虑实现);MARK = Via 自研 CNC,FOCAS/EZSocket 不适用,公开零文档——启动条件未定(网关确认/手册到手),外部依赖最深 | 计划已列·暂缓 |
| 12 | FINS 时钟读/写(0701/0702) | P2 | W342 §5-3-19/20(印刷页 197-198)有明确依据;sync/native/aio 三层 + 10 例测试已落地 | 已完成 |
| 13 | README PLC 安全警示 | P3 | libplctag 先例(开篇免责:写操作失误可致生产/财产损失);已落地(a31c236,顺带修简介残留) | 已完成 |
| 14 | examples「采集→MQTT」上行示例 | P3 | neuron/thingsboard 核心场景;paho-mqtt 可选示例已落地(93db9ed) | 已完成 |

### S7comm 自研计划骨架(2026-10-03 拍板:直接替换 snap7 封装)

**动机与形态**(2026-10-03 用户四点拍板):
- **直接替换,不并存**:`SiemensS7Client` 名字与 API 不变(构造参数 /
  rack·slot / 通用读写全族 / `get_cpu_state` / `read_range` /
  `read_many` / `read_batch` / `read_wstring` / `write_wstring`),
  内部从 snap7 封装重写为纯 Python S7comm 栈;python-snap7 依赖
  (s7 extra 双轨线)整体退役,S7 回归**核心零第三方依赖**;
- `dll_path` 参数移除(破坏性,CHANGELOG 披露)——snap7 DLL 按解释器
  分版本、32 位自备 DLL 的痛点正是自研动机;
- native 层同期新增 `AsyncSiemensS7Client`(原生 asyncio,摆脱 ctypes
  阻塞裹线程——自研核心红利);aio `ASiemensS7Client` 已存在,自动随
  新同步实例;
- **3.7 硬要求无障碍**:S7comm 纯 TCP 三层栈(TPKT→COTP→S7),无 UDP
  datagram,传输层复用 `TcpTransport`(TPKT 4 字节长度头与「读满 N 字节」
  语义吻合)零新代码;3.7~3.9 用户从此免装 python-snap7 1.3 + setuptools。

**依据先行**(退档口径,2026-10-03 定稿):python-snap7 3.2.0(2026-03
纯 Python 重写,MIT,**3.10+**)= 首要帧面参考源(内部 `Connection`/
`S7Protocol`/`S7Function`/`S7PDUType`/`S7Area`/`S7WordLen`/`Tags` 模块
即帧面与地址语法权威);Sally7(C#,MIT)与 S7netplus(MIT)交叉双向
裁决(新重写自身或有 bug,勿单源盲信);snap7 C++ 源码(LGPL)**只比对
行为不抄码**;Wireshark s7comm dissector 中立仲裁;真机 pcap 为黄金帧
唯一权威。代码就近注释「参考实现文件 + 函数 + 逐字节比对日期」。

**分阶段**:
1. **P1 依据与建帧**:抽 python-snap7 3.2.0 建帧源码要点归档
   `docs/protocol/siemens/s7comm/`;TPKT/COTP/S7 三层建帧,黄金帧测试
   先行(参考实现字节锁定);
2. **P2 连接与单点读写**:连接序列(TPKT CR → COTP CC → S7 协商
   ROSCTR/ACK_DATA,PDU 大小协商);Read/Write Var(区域 PE/PA/MK/DB/C/T
   × BIT/BYTE/WORD/DWORD/REAL);rack/slot → TSAP 计算;错误码 →
   DeviceError 映射 + i18n 双语;
3. **P3 API 冻结面补齐**:STRING/WSTRING(头 2 字节布局)、multi read
   (0xF0)→ `read_batch`/`read_many`(MAX_VARS=20 口径保持)、
   `read_range`(read_area 连续)、`get_cpu_state`(SZL 0x0424——现有
   封装 ping 探活依赖它,必做)、native `AsyncSiemensS7Client`;
4. **P4 清除与披露**:pyproject 撤 python-snap7 双轨线(s7 extra 删除)、
   `dll_path` 移除、snap7 相关 helper/测试全删重写(黄金帧 +
   ScriptedTransport,3.7 门禁环境装不了 3.10+ 的 python-snap7,不可能
   引它做测试依赖)、文档四件(README/protocol-features/architecture/
   examples)口径同步;
5. **P5 真机核证**:封装已删无双轨对拍 → 改为与 python-snap7 3.2.0
   独立脚本对拍(真机 S7-300/1200/1500;PUT/GET 授权、优化 DB 行为——
   真机清单既有项),pcap 样本归档补黄金帧。

**风险**:帧面争议无官方文档(多实现交叉 + 真机抓包裁决);python-snap7
3.x 全新重写自身或有 bug(交叉裁决兜底);优化 DB 绝对寻址不可用(与
snap7 封装现状一致,遇到明确报错);工作量数周级。

### DLL 封装族计划(FOCAS / EZSocket,2026-10-03 列)

**共同形态**(海康 MvCodeReaderSDK 先例 1:1 复制,`HikrobotIdSdkClient`
即模板):ctypes 直绑(DLL 路径构造可配,按位数选 32/64 库)+ **厂商头文件
为权威**(结构体 sizeof 对拍守卫——海康 EX2 漏 24 字节堆溢出的教训)+
假函数表测试(不依赖 DLL 可测,真机核证人工)+ SDK 缺失明确报错
(区分未安装/位数不符/导出缺失)+ 返回码表进 constants 与 i18n 双语 +
可选 extra(`fanuc` / `ezsocket`),核心保持零依赖。定位 `cnc/` 包
(与 MTConnect 同域,数采只读优先)。

**#9 FANUC FOCAS**:
1. 依据:`docs/protocol/fanuc/` 新建——FOCAS 库手册 + `fwlib32.h`
   (官方 Development 包,需 FANUC 账号/经销商渠道,拿到前不写一行绑定代码);
2. 绑定核心面:`cnc_allclibhndl3`/`cnc_freelibhndl`(句柄生命周期)+
   首期只读三件:`cnc_rddynamic`(实时状态)/`cnc_rdprgnum`(程序号)/
   `cnc_rdaxisdata`(轴数据)——与 MTConnect `/current` 同场景可互验;
3. 测试:假函数表 + sizeof 守卫;真机:FANUC 0i 系列联测(登记真机清单);
4. 依赖:fwlib32.dll/64.dll 按解释器位数装载,extra `fanuc`。

**#10 三菱 CNC EZSocket**:
1. 依据:EZSocket 库手册 + SDK 头文件(`docs/protocol/mitsubishi/` 收录;
   需三菱 CNC 渠道,拿到前不立项动码);
2. 绑定形态**拿到 SDK 后裁决**:纯 C 接口走 ctypes(同 FOCAS)、COM 组件
   走 comtypes(同 MX Component 先例)——两者库内都有成熟模板;
3. 面向:CNC 数据采集(与 MTConnect 同域),首期只读;
4. 真机前置(判据同 ADS:部署面复杂、厂商运行库)——真机清单登记后启动。

### 日立/Via Mechanics MARK 系钻孔机数采计划(暂缓,2026-10-03)

**背景**:MARK 30/50/55 = Via Mechanics(原日立産機,2021 分立)PCB 钻孔机
自研 CNC 系统(ND-5/ND-6 系机身),**非 FANUC/MELDAS 通用数控**——DLL 封装
族(#9/#10)不适用;通信规范(SECS/GEM 选配、Host Link 私有协议、FTP 程序
传输)全部厂商 NDA 资料,公开渠道零文档。

**三条路径与预裁决**:
1. **现场网关优先(零开发)**:MARK 工控机形态,PCB 厂常配 KEPServer/
   厂商网关把机台数据转 OPC-UA/Modbus 暴露——现有 `OpcUaClient`/
   `ModbusTcpClient` 直接可采;**若现场确认有网关,本条目关闭**,转配置工作;
2. **SECS/GEM 选配**:倾向**不做自研**(SEMI 大标准族与本库帧级定位差异大,
   立项级),用现成 secsgem 栈对接;最终口径待用户裁决(可能进「有意不做」);
3. **Host Link 私有协议**:需向 Via 代理商索取《外部通信/Host Interface
   手册》(机种编号 + MARK 软件版本);拿到后按海康先例**依据先行**立项
   (transport 层可复用,成帧按手册),拿不到不动。

**启动条件(三选一)**:①现场确认网关形态(→ 走路径 1,关闭本条);
②拿到 Host 通信手册(→ 走路径 3,正式立项);③用户另行拍板。
`docs/protocol/README.md`「待补」表已登记文档缺口。

---

## 有意不做(登记口径,不再实现)

| # | 项 | 依据 / 出处 | 状态 |
|---|---|---|---|
| 1 | PLC 远程控制(MC 1001~1006 / MX SetCpuStatus) | 运维风险面,本库定位数据采集;protocol-features §2/§4 | 已披露 |
| 2 | Modbus RTU ASCII 模式 | 实际硬件太少,现场几乎全是 RTU 二进制(V1.02 附件 B);protocol-features §1 + architecture §8 | 已披露 |
| 3 | Modbus FC43/13 CANopen General Reference | 规范 §6.20,CiA 授权,CANopen 对象字典访问,超出 PLC 通信定位;protocol-features §1 | 已披露 |
| 4 | TLS/SSL/X.509 加密栈 | 内网部署口径,项目红线(AGENTS.md) | 已披露 |
| 5 | FINS 运维命令(0103 填充/0105 传送/0401·0402 运行停止/2301 强制置复位) | 运维/控制面,与 MC/MX 远程控制同口径;2026-10-03 用户裁决不考虑(命令本身 W342 有据,排除属产品定位,非无依据) | 已披露 |
| 6 | 连接池 / 并行采集原语(含 README「多实例并行」指引) | 2026-10-03 用户裁决:**和使用场景不符**——目标场景(中小规模多品牌采集)靠 `read_batch`/`read_range` 合并 + 多客户端实例已覆盖,池的复杂度与受益面(数百点 10Hz+ 高吞吐)不匹配;S7netplus #49/#238/#295 瓶颈实证属他家场景。方案要点曾评审(显式借还/池不插手重连/FINS·TCP 节点号冲突约束),如场景变化可循此重启 | 已披露 |

---

## 待真机核证

> 详细清单见 `docs/real-machine-checklist.md`。

- **MX 高层包装盲区三处 `_raw_com_method` 修复**
  (GetDevice/GetCpuType/GetClockData 高位 0 出错码)+ GetErrorMessage 单参/双参形态
- **FINS** 32/64 位多字值字序(低字在前、字内大端)真机读回比对
- **S7** 半开断连三形态(拔线/断电/路由黑洞)、1200/1500 PUT-GET 授权提示充分性、
  MAX_VARS=20 与 snap7 3.x 实际值
- **TOYOPUC** X/Y 与 T/C 同址(第一优先)、M0100W/M0201L 字索引支持面、M0201.5 点号形式
- **KV Host Link** 帧面三项(端口 8000 vs 8001、有无 FCS/BCC、有无 `##` 帧头)、
  KV MC 位组记号编号口径、0101 CPU 型号支持面
- **汇川 MC** X/Y 上限(X0~X1777)、R 编号上限(R0~R32767)、0101 支持面;
  汇川 Modbus RTU 真机
- **松下 MEWTOCOL** 错误码宽度(2 vs 4 字符)、多字值低字在前、L 双语境
- **OPC-UA** 真实服务器联测(西门子/罗克韦尔/施耐德)、
  死区订阅 asyncua 私有 API 跨版本兼容
- **MTConnect** 真实 Agent keep-alive 行为、Header 嵌入业务数据
- **海康** V2.0.0 头文件(MvCodeReaderParams.h)归档 + sizeof 对拍表
- **欧姆龙 FINS/TCP** 封装握手手册(W420/W465/W344)页码级引用

---

## 测试覆盖盲点

> 来源 review-1005 §7.3,采纳前主会话再核。

- **MX**:
  GetDevice/GetCpuType/GetClockData 高位 0 静默路径模拟、
  read_many 拒绝路径、connect/disconnect 循环 COM init 平衡、aio 双客户端 STA 隔离
- **KV HL**:
  W 字软元件写字节级断言、低速滴流对端、UDP 粘包首字节
- **KV MC**:
  read_batch 多设备组合、位号越界拦截、_has_ping 与披露一致
- **松下 MC**:
  TN 字写/CS 位写、R0005+R9005 同事务、SM 字单位读
- **MEWTOCOL**:
  RFF 裸 ValueError 消息含地址、read_range 数据区 BOOL 拒绝、L0F/T10/T1F 形态
- **TOYOPUC**:
  read_range INT×257 边界、write_string 空串、ErrorCategory 分类
- **汇川**:
  random_read/random_write 记号换算、T300 字越界
- **小驱动族**:
  `last_error_category` 在 ProtocolFrameError 时的分类

---

## 文档/披露待补

> 来源 review-1005 §7.4。

- **examples.md**:
  各 MC 子类默认端口对照表(松下 2000 / KV MC 5000 / 汇川 MC 2000)+
  `S` 跨协议语义提示(MEWTOCOL SV vs MC 位软元件)
  ——**已随 examples 丰富化落地(2026-10-03,「各走线默认端口对照」节)**
- **protocol-features §4 MX**:
  `SetCpuStatus` 远程控制已在 §2/§4 标「有意不做」
- **PLC 子包 `__init__.py`**:
  公共 API 顶层导出统一(ASiemensS7Client 等)
- **constants.py**:
  `MX_MAX_BLOCK_WORDS=960` 数值依据注释、
  MX_BIT_DEVICES 表外 SD 字软元件分类口径
- **松下 MC 模块入口**:
  TwinCAT TE1000 引用补全(如有误挂)
