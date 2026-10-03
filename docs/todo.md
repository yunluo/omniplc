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
| 1 | FINS 时钟读/写(0701/0702) | P2 | W342 §5-3-19/20(印刷页 197-198)有明确依据,已排期 | 待实现 |
| 2 | AB `list_tags` 点位枚举 | P2 | CIP Get Tag List 服务(0x55),依 Rockwell 1756-PM020;pylogix/gologix/libplctag 均有,现场"看 PLC 里有哪些标签"刚需(2026-10-03 GitHub 同类库调研唯一确凿功能缺口) | 待实现 |
| 3 | README PLC 安全警示 | P3 | libplctag 先例(开篇免责:写操作失误可致生产/财产损失);本库全部客户端可写,README 无警示——一行成本 | 待实现 |
| 4 | examples「采集→MQTT」上行示例 | P3 | neuron/thingsboard 核心场景;paho-mqtt 可选依赖示例,库本体保持零依赖 | 待实现 |
| 5 | 连接池 / 并行采集原语 | P2 | S7netplus #49/#238/#295 实证单连接串行是吞吐瓶颈、用户自建多连接池;v1.x 履历已排期,此处集中登记;短期先在 README/docstring 补「多实例并行」指引 | 待实现 |
| 6 | 串口原生异步层(3.8+ 环境标记) | P3 | pymodbus RTU asyncio 先例(serial_asyncio);RTU 不需要数据报端点,"3.7 Proactor 限制"仅约束 UDP——3.8+ 标记提供,3.7 用户退回 aio 层 | 待实现 |

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
