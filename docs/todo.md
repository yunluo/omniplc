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

| # | 项 | 优先级 | 依据 / 出处 | 状态 |
|---|---|---|---|---|
| 1 | FINS 时钟读/写(0701/0702) | P2 | W342 §5-3-19/20(印刷页 197-198)有明确依据;sync/native/aio 三层 + 10 例测试已落地 | 已完成 |
| 2 | README PLC 安全警示 | P3 | libplctag 先例(开篇免责:写操作失误可致生产/财产损失);已落地(a31c236,顺带修简介残留) | 已完成 |
| 3 | examples「采集→MQTT」上行示例 | P3 | neuron/thingsboard 核心场景;paho-mqtt 可选示例已落地(93db9ed) | 已完成 |
| 4 | AB `list_tags` 点位枚举 | P2 | CIP Get Tag List 服务(0x55),依 Rockwell 1756-PM020;pylogix/gologix/libplctag 均有,现场"看 PLC 里有哪些标签"刚需(2026-10-03 GitHub 同类库调研唯一确凿功能缺口)——需新服务封装+分页续传+变长解析 | 待实现 |
| 5 | 连接池 / 并行采集原语 | P2 | S7netplus #49/#238/#295 实证单连接串行是吞吐瓶颈、用户自建多连接池;v1.x 履历已排期——并发生命周期设计与退避/锁模型交互,本期最大件;短期可先落 README「多实例并行」指引 | 待实现 |
| 6 | 串口原生异步层(3.8+ 环境标记) | P3 | pymodbus RTU asyncio 先例(serial_asyncio);RTU 不需要数据报端点,"3.7 Proactor 限制"仅约束 UDP——需新增依赖线+环境标记+双循环测试,与 #5 同量级或更大 | 待实现 |
| 7 | S7comm 自研(立项级,**计划见下节**) | P2 | 摆脱 python-snap7 C 库依赖(阻塞 DLL、按解释器双轨);Sally7 证明纯 asyncio 可行;2026-10-03 用户拍板立项,先列计划 | 计划已列 |
| 8 | README「AI 欢迎策略」章节 | P3 | libplctag AI Policy 先例:**承认本项目大量使用 AI**(实现/测试/文档全程 AI 辅助)并欢迎 AI 辅助贡献;协作指引指向 AGENTS.md(协议引用铁律/全中文/门禁)与 CONTRIBUTING.md | 待实现 |

### S7comm 自研计划骨架(立项级,2026-10-03 拍板)

**动机**:摆脱 python-snap7——C 库按解释器双轨(3.7→1.3 带 DLL/3.10+→3.x)、
ctypes 阻塞调用只能裹线程进 aio;自研后原生 asyncio 单栈、核心回归零第三方
依赖、Windows/ARM 部署免 TcAdsDll 式运行库纠缠。Sally7(C#,MIT)证明纯
asyncio S7 完全可行。

**依据先行(铁律)**:S7comm 无官方公开手册——依据源 = Wireshark
s7comm dissector 源码(协议树即规范)+ snap7 文档 + **真机抓包样本**(黄金帧
的唯一权威);`docs/protocol/README.md` 待补表 S7comm 行已登记,收集的
dissector 说明/pcap 样本按厂商新建 `docs/protocol/siemens/s7comm/` 收录。
出处口径退一档:手册页码级不可得时,标注「Wireshark dissector 函数名 +
抓包样本字节偏移」(铁律允许的"明确引用位置"形态,与「手册待补」同级披露)。

**分阶段**:
1. **P1 依据与建帧**:收 dissector/pcap;TPKT(RFC 1006)/COTP(CC/CR/DT)
   /S7 头(ROSCTP)三层建帧,黄金帧测试先行(样本字节锁定);
2. **P2 单点读写**:S7 PDU 读/写变长(Read/Write Var),DB/I/Q/M 绝对寻址,
   rack/slot 路由,PDU 协商;
3. **P3 能力补齐**:STRING/WSTRING(头 2 字节布局)、read_multi_vars
   (S7 VarFun MultiRead)、位读写、优化块访问错误识别;
4. **P4 对拍**:与 snap7 封装双轨同批响应对拍(帧语义一致 + 错误口径映射
   对齐——`_SNAP7_ERRORS`/S7Error 谱系翻译表);
5. **P5 并存**:新驱动命名 `S7CommClient`(或按命名惯例定),snap7 封装
   `SiemensS7Client` 保留不撤(对拍期两轨),aio/native 同面;
6. **P6 默认切换**:真机核证(S7-300/1200/1500,PUT/GET 授权与优化块
   访问——真机清单已有对应项)后另行裁决是否默认,不自动换。

**风险**:PUT/GET 授权与 1200/1500 优化块的行为差异只能真机核证(同
snap7 封装现状);协议无官方规范,帧面争议以抓包样本裁决。工作量数周级,
排 v1.x;非本期。

---

## 有意不做(登记口径,不再实现)

| # | 项 | 依据 / 出处 | 状态 |
|---|---|---|---|
| 1 | PLC 远程控制(MC 1001~1006 / MX SetCpuStatus) | 运维风险面,本库定位数据采集;protocol-features §2/§4 | 已披露 |
| 2 | Modbus RTU ASCII 模式 | 实际硬件太少,现场几乎全是 RTU 二进制(V1.02 附件 B);protocol-features §1 + architecture §8 | 已披露 |
| 3 | Modbus FC43/13 CANopen General Reference | 规范 §6.20,CiA 授权,CANopen 对象字典访问,超出 PLC 通信定位;protocol-features §1 | 已披露 |
| 4 | TLS/SSL/X.509 加密栈 | 内网部署口径,项目红线(AGENTS.md) | 已披露 |
| 5 | FINS 运维命令(0103 填充/0105 传送/0401·0402 运行停止/2301 强制置复位) | 运维/控制面,与 MC/MX 远程控制同口径;2026-10-03 用户裁决不考虑(命令本身 W342 有据,排除属产品定位,非无依据) | 已披露 |

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
- **protocol-features §4 MX**:
  `SetCpuStatus` 远程控制已在 §2/§4 标「有意不做」
- **PLC 子包 `__init__.py`**:
  公共 API 顶层导出统一(ASiemensS7Client 等)
- **constants.py**:
  `MX_MAX_BLOCK_WORDS=960` 数值依据注释、
  MX_BIT_DEVICES 表外 SD 字软元件分类口径
- **松下 MC 模块入口**:
  TwinCAT TE1000 引用补全(如有误挂)
