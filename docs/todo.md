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
> - `docs/real-machine-checklist.md`(真机核证)
> - `docs/protocol/README.md`「待补」表
>
> (历轮审查台账(review-1005/1006/1017/1018)已按维护惯例清除,
> 修复记录存 CHANGELOG 对应版本条目与协作记忆。)

---

## 排期实现

> 按**实现简单程度**从易到难排列(2026-10-03 重排);已完成项沉底归档。

| #   | 项                                                   | 优先级 | 依据 / 出处                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 状态                                                                                                                                        |
| --- | ---------------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | FINS 计数器 C 区地址补 0x8000 前缀                   | P1     | review-1018 P1-1:W342 §5-2-2 印刷页 165(CNT 地址列 `800000 to 8FFF00`);现 C/T 同码同址逐字节相同,真机可能静默命中 Timer PV——先真机核证 C10 PV 读落哪个区,裁决后重锚测试;C 语言参考实现/fins 双参考实测均无前缀(真分歧),披露已落 constants 注释 + 真机清单 FINS UDP 行                                                                                                                                                                                                                                                                                                 | 待核证待修复                                                                                                                                |
| 2   | AB connected 模式 list_tags 拆连                     | P1     | review-1018 P1-2:`_transact_with_status` connected 分支把 SendUnitData 应答(0x70)交给只放行 0x6F/0x66 的 `_parse_rr_data_cip` → 必 ProtocolFrameError;修法:改走 `parse_send_unit_data_reply` 与 `_transact` 同构;补 connected list_tags 黄金用例                                                                                                                                                                                                                                                                                                                      | 已修复(8c78b0d:新 parse_send_unit_data_reply_with_status + 黄金用例)                                                                        |
| 3   | native Modbus FC11 忙态错误码 code=0                 | P1     | review-1018 P1-3:sync 已按 R9-2 改 `DeviceError(...,0)`(不计 device_error_count),native 仍透传 0xFFFF;补 native 忙态镜像测试                                                                                                                                                                                                                                                                                                                                                                                                                                          | 已修复(8c78b0d)                                                                                                                             |
| 4   | MC 位软元件字单位写门控(同步+native)                 | P2     | review-1018 P2-1/2:`_write`(melsec.py:235-246)与 native 单点 `_read`/`_write`(native/melsec.py:146-198)缺 `_bit_device_word_access_allowed` 门控(读侧有写侧无);native random_write 位软元件边界恒 -15 未随同步 `byte_count*8-1` 修(P2-3)                                                                                                                                                                                                                                                                                                                              | 已修复(d5305e7:门控助手共用 + native 边界对齐,+4 例)                                                                                        |
| 5   | OPC-UA 五条 P2 缝隙                                  | P2     | review-1018:①GUID 花括号不成对放行(address.py:34-39)②deadband_type 校验上提到入参期(client.py:117-124)③browse None=分层引用非所有参考(client.py:803-804)④read_values 应答数量校验(client.py:259-269)⑤stale 句柄 sub_id 别名误删活跃索引(client.py:1063-1064)                                                                                                                                                                                                                                                                                                          | 已修复(cd9564e:①②④⑤修+③文档订正,+4 例)                                                                                                      |
| 6   | AB 0x55 16 位实例段填充位置                          | P2     | review-1018 P2-7:codec_cip.py:528-531 `25 00 01 00` vs 参考 `25 00 00 01`;>255 点分页潜伏风险                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | 误报撤销(2026-10-06 实测 同类开源参考实现 LogicalSegment `_encode` + 同类开源参考实现 `pack('<HH')` 双源均小端 LE `25 00 01 00`,与本库一致) |
| 7   | Modbus STRING 批量吞错 + FC43 RTU off-by-2           | P2     | review-1018 P2-8/9:read_many/read_batch/write_batch STRING 静默 (False,None);FC43 增量收包 tail 上限在 CRC 前检查帧可达 258>256                                                                                                                                                                                                                                                                                                                                                                                                                                       | 已修复(699eaa7:FC43 预留 CRC +1 例);STRING 吞错误报撤销(实测三路径均同步抛 ValueError 符合契约)                                             |
| 8   | 松下 MEWTOCOL/松下 MC 三缺口                         | P2     | review-1018 P2-11/12/13:read_range 拒绝 L(LT)字访问复现;UDP 预算裸 ValueError 逃出契约;MC 点号形式 R1.15 被基类门控误拒                                                                                                                                                                                                                                                                                                                                                                                                                                               | 已修复(699eaa7:UDP 预算入参期预检 +1 例);L 双语境(P2-11)/点号语义(P2-13)待手册,披露已落 constants 注释 + 真机清单                           |
| 9   | KV EA/EB 扩展错码                                    | P2     | review-1018 P2-10:`_ERROR_RE=^E[0-9]$` 只认 E0~E9;EA 读路径拆连、.H 路径静默当数据 0xEA=234;需手册确认语义后扩 `^E[0-9A-F]$`                                                                                                                                                                                                                                                                                                                                                                                                                                          | 待核证待修复(披露已落 codec.py 注释 + 真机清单 KV 行)                                                                                       |
| 10  | S7 SZL 0x84 与读应答长度宽容度                       | P2     | review-1018 P2-19/20:强校验严于参考(0x44 回显拆连 / 老 300 填充怪癖无宽容)——真机核证项                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 待真机核证(已落真机清单 S7 行⑬)                                                                                                             |
| 1   | README「AI 欢迎策略」章节                            | P3     | 同类开源库 AI Policy 先例;已落地——**不排斥 AI 开发,但必须知道自己的代码有什么作用和干嘛的,对自己提交的代码负责;对待 AI 和对待 IDE 一样,都是工具**(用户口径,2026-10-03)                                                                                                                                                                                                                                                                                                                                                                                                | 已完成                                                                                                                                      |
| 2   | AB `list_tags` 点位枚举                              | P2     | CIP Get Instance Attribute List(服务 0x55)+ Symbol Object(0x6B20);帧面经 同类开源参考实现 双参考实现对照裁决(1756-PM020 手册待补,protocol「待补」已登记);sync+aio 双层、自动分页、`AbTagEntry` 根包导出;+6 例测试(黄金帧/分页/坏帧/停滞/aio);真机核证项入清单                                                                                                                                                                                                                                                                                                         | 已完成(v0.52.2 发布 / 0.52.3 修复)                                                                                                          |
| 3   | Monitor 点位级死区(deadband)                         | P3     | 2026-10-03 现场调研「数据质量三害」:当前 `_changed` 严格不等比较,浮点传感器抖动逐周期误发 `on_change`;已落地——`Monitor`/`create_monitor` 新增 `deadband` 参数(数值=全点统一,`Dict[tag_id, 死区]`=逐点指定,0=关闭),锚点=该点上次报告值(OPC-UA DataChangeFilter 同款,防单步差压线漏报),死区内快照照常刷新仅压事件;首拍/质量跨界/bool/NaN 不受压制;aio 镜像透传;+11 例测试                                                                                                                                                                                               | 已完成                                                                                                                                      |
| 4   | 错误现场环形缓冲(报文黑匣子)                         | P3     | 2026-10-03 现场调研:`set_debug` 是实时打印,进程崩溃后报文现场丢失,现场无人盯日志时无从排查;已落地——`set_frame_recorder(enabled, capacity=1000)` 只存不打印(1~100000 帧构造期校验),`recorded_frames()`/`clear_recorded_frames()`/`FrameRecord`(墙钟时间+方向+标识+原始字节),挂点与实时日志同在 `log_frame`(走线型全量覆盖,会话型不进),+8 例测试;排障指南 §四同步用法                                                                                                                                                                                                   | 已完成                                                                                                                                      |
| 5   | OPC-UA 断线自动重订(选项)                            | P3     | 2026-10-03 现场调研:会话/订阅恢复是 OPC-UA 现场普遍痛点(TransferSubscriptions 失败、订阅 stale,常见解法竟是重启服务);已落地——构造选项 `auto_resubscribe`(默认关):订阅成功登记**订阅意图**,重连成功(显式/惰性)后 best-effort 按序重建,单项失败记日志;语义拆分「退订 vs 断开」(`unsubscribe()` 移除意图,disconnect 联动 `release()` 保留意图);aio 镜像;+6 例测试。**2026-10-03 讨论定调:封装层解法,优先于栈自研(栈自研裁决见「有意不做」#7)**                                                                                                                           | 已完成                                                                                                                                      |
| 6   | examples 范例丰富化:各协议对外 API 全展示            | P3     | 2026-10-03 用户指令:「使用范例要丰富,把本库对外 API 都展示下」;已落地——新增「通用 API 面」大节(读写原语/超时重试退避心跳/告警分级/stats/点位表/Monitor/批量读写/全局开关含黑匣子/异步两层),各协议节补缺(MC random 双组签名+MX 时钟/错误码、FINS 0101、AB list_tags/list_identity/属性/GenericMessage、NJ 拒绝披露、S7 get_cpu_state/write_wstring、SR bank/reset、MTConnect sample/assets),新增「批量写」与「默认端口对照」两节(S 跨协议语义提示);API 清单经 inspect 全量盘点防漏                                                                                     | 已完成                                                                                                                                      |
| 7   | FANUC FOCAS DLL 封装(`cnc/`)                         | P3     | **首批已落地(2026-10-05,`FanucFocasClient`)**:`docs/protocol/fanuc/` 物料归档(社区打包参考实现快照(内部留存) 的 `fwlib32.h` 主源 + 帧面档案)、连接生命周期 + sysinfo/rddynamic2/statinfo2 只读三件、仅 Windows、端口 8193、假函数表 25 例 + sizeof 守卫、aio 镜像;详情见下节「DLL 封装族」#7                                                                                                                                                                                                                                                                          | 首批完成(二批候选见待补表)                                                                                                                  |
| 8   | 三菱 CNC EZSocket 封装(`cnc/`)                       | P3     | **首批已落地(2026-10-05,`MitsubishiEzSocketClient`,GIOP 直连 TCP 683 零依赖)**:官方手册 IB-1501208 + 开源参考实现 C 库双物料归档,首批只读面 + 31 例测试 + aio 镜像;真机 M70 联测待现场(帧面单源是最大风险点);二批候选见下节 #8                                                                                                                                                                                                                                                                                                                                        | 首批完成(真机核证待做)                                                                                                                      |
| 9   | S7comm 自研(**直接替换** 同类开源库 封装,计划见下节) | P2     | 2026-10-03 用户四点拍板:`SiemensS7Client` 名字与 API 不变、内部重写为纯 Python S7comm 栈、同类开源封装 依赖整体退役、S7 回归核心零依赖;参考源定稿 同类开源参考实现(3.2.0 快照)(纯 Python 重写,MIT)为主 + 交叉参考实现 交叉;3.7 无障碍(纯 TCP 三层栈)——2026-10-03 开工:**P1~P4 已落地**(codec 三层建帧+黄金帧、连接/读写、API 冻结面、`dll_path` 移除、依赖退役、测试重写 33 例、文档四件;帧面依据 docs/protocol/siemens/s7comm/);review-1008 修复批落地(2026-10-04);**native `AsyncSiemensS7Client` 已落地(2026-10-04,会话型适配,对拍守卫)**;余 P5 真机核证(SZL 待核) | 已落地(P5 真机核证)                                                                                                                         |
| 10  | 日立(Via Mechanics)MARK 30/50/55 钻孔机数采          | P3     | **计划见下节;暂缓**(2026-10-03 用户裁决:先列计划,暂不考虑实现);MARK = Via 自研 CNC,FOCAS/EZSocket 不适用,公开零文档——启动条件未定(网关确认/手册到手),外部依赖最深                                                                                                                                                                                                                                                                                                                                                                                                     | 计划已列·暂缓                                                                                                                               |
| 11  | FINS 时钟读/写(0701/0702)                            | P2     | W342 §5-3-19/20(印刷页 197-198)有明确依据;sync/native/aio 三层 + 10 例测试已落地                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 已完成                                                                                                                                      |
| 12  | README PLC 安全警示                                  | P3     | 同类开源库 先例(开篇免责:写操作失误可致生产/财产损失);已落地(a31c236,顺带修简介残留)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 已完成                                                                                                                                      |
| 13  | examples「采集→MQTT」上行示例                        | P3     | 主流数采平台 核心场景;paho-mqtt 可选示例已落地(93db9ed)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 已完成                                                                                                                                      |
| 14  | UDP 陈旧数据报防串扰(事务发送前排空)                 | P1     | 2026-10-05 现场稳定性盘点:UDP 一问一答走线中,上一事务超时后迟到的响应留在接收缓冲,下一事务误当本轮应答——FINS(SID/ICF 回显)按坏帧报错触发不必要的拆连重连,无事务号协议(TOYOPUC/KV Host Link/MC 族 UDP/MEWTOCOL)**静默读到旧值**;已落地——`BaseTransport.drain()` 默认 no-op(流式走线有意不动:超时即拆连重同步)+ `UdpTransport`/`AsyncUdpTransport` 非阻塞排空(被排帧进黑匣子,上限 64 防灌包占锁,WSAEMSGSIZE 照排不报)+ 七族 UDP 走线接线 + native FINS/MC 镜像;+15 例测试(门禁 1811 passed,bebff02);机制口径见 architecture §5                                          | 已完成                                                                                                                                      |

### S7comm 自研计划骨架(2026-10-03 拍板:直接替换 同类开源库 封装)

**动机与形态**(2026-10-03 用户四点拍板):
- **直接替换,不并存**:`SiemensS7Client` 名字与 API 不变(构造参数 /
  rack·slot / 通用读写全族 / `get_cpu_state` / `read_range` /
  `read_many` / `read_batch` / `read_wstring` / `write_wstring`),
  内部从 同类开源库 封装重写为纯 Python S7comm 栈;同类开源封装 依赖
  (s7 extra 双轨线)整体退役,S7 回归**核心零第三方依赖**;
- `dll_path` 参数移除(破坏性,CHANGELOG 披露)——同类开源库 DLL 按解释器
  分版本、32 位自备 DLL 的痛点正是自研动机;
- native 层下批新增 `AsyncSiemensS7Client`(原生 asyncio,摆脱 ctypes
  阻塞裹线程——自研核心红利);aio `ASiemensS7Client` 已存在,自动随
  新同步实例;
- **只实现客户端侧**(2026-10-03 用户补充约束,与 AGENTS「不引入库内
  模拟器」红线同口径):不做服务端/模拟器/Partner;测试照旧黄金帧 +
  ScriptedTransport + 真机核证(P5),**不引 同类开源封装 的 Server 做
  集成测试**(3.7 门禁环境也装不了);
- **3.7 硬要求无障碍**:S7comm 纯 TCP 三层栈(TPKT→COTP→S7),无 UDP
  datagram,传输层复用 `TcpTransport`(TPKT 4 字节长度头与「读满 N 字节」
  语义吻合)零新代码;3.7~3.9 用户从此免装 同类开源封装(1.3) + setuptools。

**依据先行**(退档口径,2026-10-03 定稿):同类开源参考实现(3.2.0 快照)(2026-03
纯 Python 重写,MIT,**3.10+**)= 首要帧面参考源(内部 `Connection`/
`S7Protocol`/`S7Function`/`S7PDUType`/`S7Area`/`S7WordLen`/`Tags` 模块
即帧面与地址语法权威);交叉参考实现 A(C#,MIT)与 交叉参考实现 B(MIT)交叉双向
裁决(新重写自身或有 bug,勿单源盲信);同类开源库 C++ 源码(LGPL)**只比对
行为不抄码**;Wireshark s7comm dissector 中立仲裁;真机 pcap 为黄金帧
唯一权威。代码就近注释「参考实现文件 + 函数 + 逐字节比对日期」。

**分阶段**(P1~P4 已落地 2026-10-03):
1. **P1 依据与建帧 ✅**:帧面事实归档 docs/protocol/siemens/s7comm/;
   抽取源码要点 + `codec.py` 三层建帧 + 黄金帧测试(参考实现字节锁定):
   原 P1 计划原文:抽 同类开源参考实现(3.2.0 快照) 建帧源码要点归档
   `docs/protocol/siemens/s7comm/`;TPKT/COTP/S7 三层建帧,黄金帧测试
   先行(参考实现字节锁定);
2. **P2 连接与单点读写**:连接序列(TPKT CR → COTP CC → S7 协商
   ROSCTR/ACK_DATA,PDU 大小协商);Read/Write Var(区域 PE/PA/MK/DB/C/T
   × BIT/BYTE/WORD/DWORD/REAL);rack/slot → TSAP 计算;错误码 →
   DeviceError 映射 + i18n 双语;
3. **P3 API 冻结面补齐**:STRING/WSTRING(头 2 字节布局)、multi read
   (0xF0)→ `read_batch`/`read_many`(MAX_VARS=20 口径保持)、
   `read_range`(read_area 连续)、`get_cpu_state`(SZL 0x0424——现有
   封装 ping 探活依赖它,必做)、native `AsyncSiemensS7Client`(下批);
4. **P4 清除与披露**:pyproject 撤 同类开源封装 双轨线(s7 extra 删除)、
   `dll_path` 移除、同类开源库 相关 helper/测试全删重写(黄金帧 +
   ScriptedTransport,3.7 门禁环境装不了 3.10+ 的 同类开源封装,不可能
   引它做测试依赖)、文档四件(README/protocol-features/architecture/
   examples)口径同步;
5. **P5 真机核证**:封装已删无双轨对拍 → 改为与 同类开源参考实现(3.2.0 快照)
   独立脚本对拍(真机 S7-300/1200/1500;PUT/GET 授权、优化 DB 行为——
   真机清单既有项),pcap 样本归档补黄金帧。

**风险**:帧面争议无官方文档(多实现交叉 + 真机抓包裁决);同类开源封装
3.x 全新重写自身或有 bug(交叉裁决兜底);优化 DB 绝对寻址不可用(与
同类开源库 封装现状一致,遇到明确报错);工作量数周级。

### DLL 封装族计划(FOCAS / EZSocket,2026-10-03 列)

**共同形态**(海康 MvCodeReaderSDK 先例 1:1 复制,`HikrobotIdSdkClient`
即模板):ctypes 直绑(DLL 路径构造可配,按位数选 32/64 库)+ **厂商头文件
为权威**(结构体 sizeof 对拍守卫——海康 EX2 漏 24 字节堆溢出的教训)+
假函数表测试(不依赖 DLL 可测,真机核证人工)+ SDK 缺失明确报错
(区分未安装/位数不符/导出缺失)+ 返回码表进 constants 与 i18n 双语 +
可选 extra(`fanuc` / `ezsocket`),核心保持零依赖。定位 `cnc/` 包
(与 MTConnect 同域,数采只读优先)。

**#7 FANUC FOCAS(首批已落地,2026-10-05)**:
1. 依据:`docs/protocol/fanuc/` 已建——`fwlib32.h`(16043 行,社区打包
   仓库 社区打包参考实现快照(内部留存);官方 Development 包渠道物料到手后 diff 一次)+ README
   帧面档案(连接流程/EW 码表/结构体布局/系列宏判断);
2. 首批只读面已落地(`cnc/focas.py`):连接生命周期(`cnc_allclibhndl3`/
   `cnc_freelibhndl`/`cnc_settimeout`/`cnc_rdcncid`)+ `cnc_sysinfo` +
   `cnc_rddynamic2` + `cnc_statinfo2`(兼探活);仅 Windows(WinDLL/stdcall);
   端口 8193;Ethernet 通用 DLL(系列宏全不定义,MAX_AXIS=32);
3. 测试:假函数表 25 例 + sizeof 守卫(ODBSYS=18/ODBDY2 前部 28+union 512/
   ODBST2=26);真机:FANUC 0i 系列联测(登记真机清单,待现场);
4. 二批候选:`cnc_rdprgnum`/`cnc_rdaxisdata`/PMC 族/参数族(见 protocol
   待补表 FOCAS 行);
5. 依赖:ctypes 直调无 Python 侧依赖(不加 extra,海康同款);
   fwlib32.dll 运行库现场自备,`sdk_dir`/`dll_path` 二选一。

**#8 三菱 CNC EZSocket(首批已落地,2026-10-05)**:
1. 依据:双级依据链——语义层 = 官方手册 FCSB1224W000 リファレンス
   IB-1501208(262 页 OLE/COM 接口,issue 附件渠道取得,PDF 本地留存
   `docs/protocol/mitsubishi/`);帧面层 = **GIOP 线上格式无官方公开文档**,
   逐字节参照 开源参考实现快照(内部留存,M70 真机验证)(MIT,作者声明
   M70 真机验证;单源,真机核证必做)+ 官方组件用法样例快照(内部留存)
   官方 COM 组件真机样例(机型枚举 6=MELDAS700M 双源);档案
   `docs/protocol/mitsubishi/m70-ezsocket/README.md`;
2. 形态裁决:**纯协议 GIOP 直连(TCP 683,零依赖),不走 COM/DLL**——
   官方 FCSB1224W000 是商业 COM SDK(产品 ID 激活,部署门槛远高于
   FOCAS 免费下载),而 C 参考库已把线上帧面完整反向并真机验证,与 S7
   自研退役 同类开源库 同方向;黄金帧离线测试与库测试范式全兼容;
3. 首批只读面已落地(`cnc/ezsocket.py`):系统/轴数五件(读系统数兼
   探活)+ 版本三件/机床类型 + run_state 三段拼合 + 轴位置六种
   (FLOATBIN)+ 轴名/主轴转速负载/进给速度/主子程序号/程序块/报警
   (17 类)/时间统计六件/计数器/当前刀号(R536 路由)/程序文件信息;
   omniplc 加严:响应 request_id 回显校验(C 库不校验)、GIOP 头强校验、
   data_length 16KB 钳制、T_DLONG i64 全宽解码;测试 31 例(黄金帧 +
   脚本化传输全链路 + aio 镜像);
4. 真机:M70/M700 系联测(端口 683,真机清单登记——GIOP 帧面/编号表
   单源是最大风险点,真机比对是最终裁决);
5. 二批候选:写面(mochaSetData)、文件操作(mochaFS* 十一操作,DNC
   程序传输)、主轴/进给倍率(Y/R 设备路由拼合);M800/C70 帧面一致性。

### 日立/Via Mechanics MARK 系钻孔机数采计划(暂缓,2026-10-03)

**背景**:MARK 30/50/55 = Via Mechanics(原日立産機,2021 分立)PCB 钻孔机
自研 CNC 系统(ND-5/ND-6 系机身),**非 FANUC/MELDAS 通用数控**——DLL 封装
族(#7/#8)不适用;通信规范(SECS/GEM 选配、Host Link 私有协议、FTP 程序
传输)全部厂商 NDA 资料,公开渠道零文档。

**三条路径与预裁决**:
1. **现场网关优先(零开发)**:MARK 工控机形态,PCB 厂常配 商用网关/
   厂商网关把机台数据转 OPC-UA/Modbus 暴露——现有 `OpcUaClient`/
   `ModbusTcpClient` 直接可采;**若现场确认有网关,本条目关闭**,转配置工作;
2. **SECS/GEM 选配**:倾向**不做自研**(SEMI 大标准族与本库帧级定位差异大,
   立项级),用现成 现成开源栈 栈对接;最终口径待用户裁决(可能进「有意不做」);
3. **Host Link 私有协议**:需向 Via 代理商索取《外部通信/Host Interface
   手册》(机种编号 + MARK 软件版本);拿到后按海康先例**依据先行**立项
   (transport 层可复用,成帧按手册),拿不到不动。

**启动条件(三选一)**:①现场确认网关形态(→ 走路径 1,关闭本条);
②拿到 Host 通信手册(→ 走路径 3,正式立项);③用户另行拍板。
`docs/protocol/README.md`「待补」表已登记文档缺口。

---

## 有意不做(登记口径,不再实现)

| #   | 项                                                                    | 依据 / 出处                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 状态   |
| --- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 1   | PLC 远程控制(MC 1001~1006 / MX SetCpuStatus)                          | 运维风险面,本库定位数据采集;protocol-features §2/§4                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 已披露 |
| 2   | Modbus RTU ASCII 模式                                                 | 实际硬件太少,现场几乎全是 RTU 二进制(V1.02 附件 B);protocol-features §1 + architecture §8                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 已披露 |
| 3   | Modbus FC43/13 CANopen General Reference                              | 规范 §6.20,CiA 授权,CANopen 对象字典访问,超出 PLC 通信定位;protocol-features §1                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 已披露 |
| 4   | TLS/SSL/X.509 加密栈                                                  | 内网部署口径,项目红线(AGENTS.md)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 已披露 |
| 5   | FINS 运维命令(0103 填充/0105 传送/0401·0402 运行停止/2301 强制置复位) | 运维/控制面,与 MC/MX 远程控制同口径;2026-10-03 用户裁决不考虑(命令本身 W342 有据,排除属产品定位,非无依据)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 已披露 |
| 6   | 连接池 / 并行采集原语(含 README「多实例并行」指引)                    | 2026-10-03 用户裁决:**和使用场景不符**——目标场景(中小规模多品牌采集)靠 `read_batch`/`read_range` 合并 + 多客户端实例已覆盖,池的复杂度与受益面(数百点 10Hz+ 高吞吐)不匹配;交叉参考实现 B #49/#238/#295 瓶颈实证属他家场景。方案要点曾评审(显式借还/池不插手重连/FINS·TCP 节点号冲突约束),如场景变化可循此重启                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 已披露 |
| 7   | OPC-UA 协议栈自研(asyncua 退役)                                       | 2026-10-03 用户发起讨论,裁决**当前不值得自研**:①动机非堵点——asyncua 1.1.5 纯 Python 零 DLL 钉版稳定,自研仅能甩掉 cryptography(TLS 永不考虑下属清洁度收益),S7 自研的 DLL 分版本/32 位不可用类硬堵点在此不存在;②工作量高一个量级且失败形态更毒——UA Binary 类型系统(Variant/NodeId 四编码/DataValue 掩码)+ 会话/SecureChannel 双层状态机(None 策略下 OpenSecureChannel 帧面仍绕不开)+ 订阅 Publish 确认与 keep-alive,类型边界错误多为**静默错值**(P1 类)而非 S7 式当场断线;③参考源单向——Python 生态仅 asyncua 一家(上游同类库 系其前身同源),其他语言栈 为 Java/C,**双向裁决方法论失效**,各国服务器互操作怪癖需重踩(OPC-UA 真机联测本就为待办弱项)。真痛点(订阅不恢复)已走 #5 封装层解法(auto_resubscribe);**重启条件**:asyncua 1.1.5 出现不可修缺陷(安全洞/3.7 轮子断供)、订阅类私有 API 坑反复发作封装层兜不住、Python 生态出现第二家纯 Python 栈可恢复交叉裁决 | 已披露 |
| 8   | 串口原生异步层(Modbus RTU / MC 1C·3C·4C 的 asyncio 版)                | 2026-10-04 用户裁决:**串口不考虑异步**——串口场景(低速点检/老设备)无真异步需求,同步轮询已覆盖;排期表原 #5 撤项。若场景变化可循 同类参考实现 serial_asyncio 先例重启(新增依赖线+AsyncSerialTransport+RTU 客户端+双循环测试,3.8+ 环境标记)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 已披露 |

---

## 待真机核证

> 详细清单见 `docs/real-machine-checklist.md`。

- **MX 高层包装盲区三处 `_raw_com_method` 修复**
  (GetDevice/GetCpuType/GetClockData 高位 0 出错码)+ GetErrorMessage 单参/双参形态
- **FINS** 32/64 位多字值字序(低字在前、字内大端)真机读回比对
- **S7** 半开断连三形态(拔线/断电/路由黑洞)、1200/1500 PUT-GET 授权提示充分性、
  MAX_VARS=20 与 同类库 3.x 实际值
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

> 来源 review-1005 §7.3;**2026-10-04 已清缴**(逐条再核后补测/确认已覆盖,
> 详见各测试文件「盲点清缴」标注用例)。

- **MX**:GetDevice/GetCpuType/GetClockData 高位 0 静默路径——**不采纳**:
  已知盲区待真机批(docstring 已披露),单独补测只能锁错误行为,随真机批
  修+测一起落;read_many 拒绝路径——已由 read_batch 拒绝测试覆盖(委托同路);
  connect/disconnect 循环 COM init 平衡——**已补**(3 轮循环计数配平);
  aio 双客户端 STA 隔离——**已补**(双站独立控件实例并发不串数据)
- **KV HL**:W 字软元件写字节级断言——**已补**(`WR W100.U`);低速滴流
  对端——已覆盖(dribble deadline);UDP 粘包首字节——**已补**(粘包双响应/
  非 ASCII 首部噪声两形态)
- **KV MC**:read_batch 多设备组合——**已补**(DM+W 单笔 0406);位号越界
  拦截——**已补**(位号 16 拒绝不发包);`_has_ping` 与披露一致——**已补**
  (0101 探活开启 ↔ protocol-features 待真机核证口径)
- **松下 MC**:TN 字写/CS 位写——**已补**(C2/C4 帧锚定);R0005+R9005
  同事务——**已补**(SM 换算单笔 0406);SM 字单位读——已覆盖(D≥90000→SD)
- **MEWTOCOL**:RFF 消息含地址——**已补**(match 断言);read_range 数据区
  BOOL 拒绝——**已补**;L0F/T10/T1F 形态——**已补**(位/字语境分流解析)
- **TOYOPUC**:read_range INT×257 边界——**已补**(514 字拒/512 字界放行);
  write_string 空串——**已补**(基类空串拒绝不发包);ErrorCategory 分类
  ——**已补**(DEVICE/PROTOCOL 断言)
- **汇川**:random_read/random_write 记号换算——read_batch 已覆盖,
  random_write **已补**(1402 路径 R100→D8100);T300 字越界——**已补**
  (按 review-1011 裁决:T/C 不在码表,拦截形态 = 不支持软元件)
- **小驱动族**:`last_error_category` 在 ProtocolFrameError 时的分类——
  已覆盖(基类 `_categorize` 全表测试 + 各驱动断言)

---

## 文档/披露待补

> 来源 review-1005 §7.4。

- **examples.md**:
  各 MC 子类默认端口对照表(松下 2000 / KV MC 5000 / 汇川 MC 2000)+
  `S` 跨协议语义提示(MEWTOCOL SV vs MC 位软元件)
  ——**已随 examples 丰富化落地(2026-10-03,「各走线默认端口对照」节)**
- **protocol-features §4 MX**:
  `SetCpuStatus` 远程控制已在 §2/§4 标「有意不做」——**已完成**
- **PLC 子包 `__init__.py`**:
  公共 API 顶层导出统一(ASiemensS7Client 等)
  ——**已完成(2026-10-04):OpcUaSubscription 进根包(公共返回类型口径
  与 AbTagEntry/FinsClock 对齐)+ test_package_exports 新增「返回类型 ⊆ 根包」
  守卫;地址解析助手(parse_*)有意留子包**
- **constants.py**:
  `MX_MAX_BLOCK_WORDS=960` 数值依据注释、
  MX_BIT_DEVICES 表外 SD 字软元件分类口径
  ——**已补(2026-10-03,MX 手册 §5.2.3 印刷页 320/§5.2.5 印刷页 328 无固定上限 + SH-080008 Appendix 5 印刷页 466 字单位 960 点交叉;SD=字软元件 §2.4 印刷页 46)**
