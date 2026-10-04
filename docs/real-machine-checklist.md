# 真机联测核验清单

在**真实 PLC 设备**上独立确认读/写能力的核验记录。**读取与写入独立勾选**(读到了
不代表写对了——尤其 Modbus 等含只读寄存器的协议)。

## 填写约定

**读/写单元格内容**:`<真机型号> <结果符号>`,留空或填 `—` 表示未测。

| 字段 | 含义 |
|---|---|
| 真机型号 | 品牌 + 系列 + 型号(如"三菱 MELSEC iQ-R R04CPU");仿真环境写"PLCSIM Advanced / TwinCAT Simulator"等 |
| 备注 | 任何异常、限制、注意事项(失败原因 / 限定条件) |

**结果标记**(写在读/写单元格内):
- `✓` 通过
- `✗` 失败(请在备注写原因)
- `~` 部分通过(限定条件,见备注)
- `—` 未测

**单元测试 ≠ 真机联测**:单元测试走黄金报文 + 模拟传输,保证"按手册/协议规范的字节级契约";
真机联测验"现场设备真实行为符合契约"。两者**不可互替**。

## 关联

- 协议帧级实现引用:`docs/architecture.md` §11 路线图与版本履历表
- 历史审查/修复记录:`CHANGELOG.md` 各版本条目(审查台账文档已按维护决策移出仓库,历史提交记录仍可回溯)
- **原生异步层(omniplc.native)**:首批 5 个客户端
  (Modbus TCP / MC 1E·3E over TCP·UDP / FINS TCP·UDP)是**独立于同步层
  的代码路径**——帧级已由"同步 × 异步对拍"测试锁死,但**真机尚未联测**,
  需与对应同步行一并核证(见 `docs/architecture.md` §12)
- 提交新真机记录:PR 模板「测试」节勾选"真机联测",并在 `CHANGELOG.md` 加条目

## 待真机核证项(v0.45.0 更新,自 README 迁入)

下表汇总散落各处的真机核证项(实现已完成,缺真机条件或排队中):

| 项                | 驱动               | 现状态                            |
|------------------|------------------|--------------------------------|
| Modbus FC24/22    | Modbus TCP/RTU   | FC24 FIFO、FC22 掩码字节序可配,需设备支持,待真机核证 |
| AB 0x0A 多服务包批量读  | AB Logix         | 已实现(超 32 条/480B 自动拆包),通用模拟器不支持,待真机核证 |
| AB connected RPI  | AB Logix         | RPI 默认 100ms(`rpi_us` 可配)、CIP 0x01/0x07 重连,待真机核证 |
| NJ CIP 0x0A 多服务包 | 欧姆龙 NJ/NX CIP    | 继承 AB,理论同,待真机核证                |
| 西门子 S7           | S7-200/200SMART/300/400/1200/1500 | **v0.53 自研 S7comm 栈**(依赖已退役),2026-10-04 型号批:`model=S7Cpu` 选型(SMART/200-CP243 连接预设 + V 区记号),需 PLC 或 PLCSIM;全功能待真机核证(P5:与 python-snap7 3.2.0 独立脚本对拍),要点见核验记录表 |
| NJ STRING / BOOL 数组 | 欧姆龙 NJ/NX CIP    | 已实现(STRING 按 `len(u32)+字符`、BOOL 按元素自描述,回 DWORD 时 `//32` 回退),待真机核证 |
| MC 新设备码          | 三菱 Q/L/R         | L/F/SB/V/DX/DY/TS/TC/TN/CS/CC/CN/SM/SD/SW 已实现,待真机核证(TN=0xC3/CN=0xC6 为推定) |
| KV MC 0406 批量读   | 基恩士 KV MC        | 继承 MelsecMc,码表已覆写,待真机          |
| 基恩士 Host Link UDP | 基恩士 KV          | 收包缓冲余量、hex 转储截断、SR 残行读净判定,待真机核证 |
| OPC-UA           | opc.tcp          | 封装 asyncua,需 OPC-UA 服务器        |
| MTConnect        | MTConnect Agent  | 标准库 HTTP/XML,需 CNC 端 Agent     |
| MX Component     | 三菱 MX            | 读写/批量/CPU 型号/时钟已真机核证;get_error_message(ActSupportMsg)待核证 |
| Modbus FC22/23/24/43·14 | Modbus TCP/RTU | FC22 掩码写(可配字节序)、FC23 读写多寄存器、FC24 FIFO、FC43·14 设备标识均需设备支持,待真机核证 |
| Modbus FC07/08/11/12/17/20/21 | Modbus TCP/RTU | 异常状态(FC07)/诊断/事件计数·日志/报告从站 ID(FC17)/文件记录(FC20/21)按规范实现,设备支持情况待真机核证 |
| MC 1E 点数码表       | 三菱 A 系列 1E      | 字单位成批读上限按 255 放行(手册 1E 章节点数表未就地核证),真机按住机型分命令复核 |
| TOYOPUC X/Y 与 T/C 同址 | 丰田 TOYOPUC      | X/Y、T/C 基址表取自参考实现(官方手册缺),若实为分址则读写互踩,真机第一优先复核 |
| 海康 ID 读码器 Modbus 模式 | HikrobotIdModbusClient | 按工业协议手册 V1.0.4 实现(握手/结果区/字节交换/站号),待真机核证(要点见核验记录表该行备注) |
| 海康 ID 读码器 TCP 命令协议 | HikrobotIdTcpClient | 按通信指令手册 V1.0.3 + ID2000/ID3000 用户手册实现(命令应答/结果推送双通道),待真机核证(要点见核验记录表该行备注) |
| 海康 ID 读码器 MvCodeReaderSDK | HikrobotIdSdkClient | 按 SDK V2.0.0 ctypes 封装(假函数表测试,真 DLL 未联测),待真机核证(要点见核验记录表该行备注) |
| 海康 ID 读码器 RS-232 串口 | HikrobotIdSerialClient | 按 ID2000/ID3000 用户手册 + 通信指令手册实现(文本触发 + 结果行,SR 同型),待真机核证(要点见核验记录表该行备注) |
| 心跳探测命令 ping/0601 | FINS / KV MC / 松下 MC / S7 | ①FINS 0601 状态读应答布局(Status/Mode 两字节分立 + 26 字节总数)按 W342 §5-3-17 印刷页 194-196 解码,Mode 高半字节合位的变体待真机抓包排除;②KV MC / 松下 MC 对 0101 CPU 型号读的支持面待真机(不支持则 ping 恒 False,心跳失败计数增长但不断线);③S7 `get_cpu_state` 走自研 SZL 0x0424(状态字节 = 记录区[3] bzu_id,C 源+Wireshark 双源,pcap 复核保留),随真机联测核证 |

---

## 核验记录表

按厂商 → 协议 升序排列;读/写独立单元格。

| 厂商 | 协议 | 读取 | 写入 | 备注 |
|---|---|---|---|---|
| 三菱 | MC 3E | 三菱 FX5U ✓ | 三菱 FX5U ✓ | 2026-09-30 真机读写通过(第八轮修复批 v0.47.3 后验证;2026-10-02 补记真机型号 **FX5U**);FX5U 关联待核:①X/Y 八进制(`xy_octal=True`,SH-080008 记法为十六进制,出处应在 iQ-F 用户手册,待补页码)——本次核证若读 X/Y 点位,顺带核证进制口径;②`get_cpu_type`(0101)与心跳探活在此真机可一并核证 |
| 三菱 | MC 4E | | | |
| 三菱 | MC 1E | | | |
| 三菱 | MC 1C(串口) | | | |
| 三菱 | MC 3C(串口) | | | |
| 三菱 | MC 4C(串口) | | | |
| 三菱 | MX Component | 三菱 FX3U ✓ | 三菱 FX3U ✓ | get_error_message(ActSupportMsg) 待核证;review-1005 补录:高层包装盲区三处 `_raw_com_method` 修复(GetDevice/GetCpuType/GetClockData 高位 0 出错码,review-0929-3 遗留真机批)、`GetErrorMessage` 单参/双参形态、`_MxComLink.connect()` Open 返回 0 但网络层中途失败的清理路径 |
| 欧姆龙 | FINS TCP | | | |
| 欧姆龙 | FINS UDP | 欧姆龙 CP1H ✓ | 欧姆龙 CP1H ✓ | 32/64 位值(REAL/DINT/LINT/LREAL/UDINT)**读回核证**:2026-09-30 修正多字值字序为"低字在前、字内大端"(原误作整体大端),既往真机只核过位/单字;请以 CX-Programmer 写入 REAL(如 100.5 → D100=0/D101=0x42C9)后经库读回比对 |
| 欧姆龙 | NJ/NX CIP(unconnected) | | | BOOL 数组按元素访问(应答类型自描述,回 DWORD 时按 Logix `//32` 回退)与 STRING(`len(u32)+字符`,写入回带模板号)待真机核证 |
| 欧姆龙 | NJ/NX CIP(connected, Forward Open) | | | |
| 罗克韦尔 | EtherNet/IP(unconnected) | | | 0x0A 多服务包自动拆包预算(≤32 条 / ≤480B)待真机核证(connected Large 4002 下 480B 偏保守,仅影响拆包次数);**`list_tags`(0x55)点位枚举待真机核证**:①应答布局(instance UDINT + SHORT_STRING 名 + type UINT + 3×UDINT 维度)与分页状态 0x06 语义(双参考实现对照裁决,1756-PM020 手册待补);②程序域 `Program:` 标签是否出现在控制器域枚举;③大点位表(>1 万)分页轮数实测 |
| 罗克韦尔 | EtherNet/IP(connected, Forward Open) | | | RPI 默认 100ms(`rpi_us` 可覆盖)、CIP 0x01/0x07 断线重连、Forward Close 应答解析待真机核证 |
| 西门子 | S7-300/1200/1500/200SMART/200 | | | **v0.53 自研 S7comm 栈待真机核证(P5,review-1008 修复后;2026-10-04 型号批扩 200SMART/200)**:①帧面——连接序列(TSAP 编码/PDU 协商)与读写字节与 python-snap7 3.2.0 独立脚本对拍(写请求 count 已按 `len(data)//元素宽` 修正,review-1008 P0),pcap 归档补黄金帧;②**SZL 0x0424 状态字节 = 记录区[3] bzu_id**(snap7 C `opGetPlcStatus` opData[7] + Wireshark dissector 双源,0x08=Run/0x04=Stop/0x03=Stop 老CPU/其余 Unknown;真机 pcap 复核保留);SZL 请求前缀两字节三源分歧(本库 0x0A/00 vs C/Sharp7 0xFF/09)若被拒首先怀疑此字段;③STRING/WSTRING 读超长按 `length` 截断、写保留 PLC 侧声明长(超声明长拒绝);④优化块访问错误提示、PUT/GET 缺失文本;⑤半开断连三形态(拔线/断电/路由黑洞)拆连重连;⑥multi read(MAX_VARS=20)真机条目上限与奇数填充;⑦大跨度读写自动分片(读 PDU−18/写 PDU−35)真机对拍;⑧历史遗留(snap7 封装时代):`dll_path` 32 位 WinError 193 链路已随依赖退役作废。**型号批专项(2026-10-04)**:⑨200 SMART 连接预设(本端 0x1000/远端 0x0300,IoTClient 字节黄金;若被拒先试 `slot=1` → 0x0301,再试资源类型 PG)与 V 区记号= DB1 读写;⑩经典 S7-200 经 CP243-1(两侧 TSAP "MW"/TPDU 512)连接与 V 区读写;⑪300/400 slot 2 惯例与老 CPU SZL 行为 |
| 汇川 | H3U/H5U Modbus TCP | 汇川 H5U 系列 ✓ | 汇川 H5U 系列 ✓ | 2026-09-28 真机读写通过(`InovanceTcpClient`,汇川 TCP/502) |
| 汇川 | H3U/H5U Modbus RTU | | | |
| 汇川 | H3U/H5U MC 协议兼容(3E) | | | 真机核证要点(review-1005):①X/Y 上限(H5U 手册 X0~X1777 = 1024 点,MC 路径发帧 0x0~0x3FF);②R 编号上限(手册 16.4:R0~R32767 = D8000~D40767);③0101 CPU 型号读支持面(`_has_ping=False` 显式关闭,开启前先核证) |
| 松下 | MEWTOCOL TCP/UDP | | | **两条终裁项**(第八轮 P1 候补,2026-09-30):①错误响应码宽度——现按 2 字符收(按 recv(5) 截断),公开参照多按 4 字符(类别 2+细分 2,如 4004);若 4 字符属实 TCP 会残留 2 字节脏缓冲;终裁法:人为触发一次错误(如读越界地址)抓包看 `!` 后字符数;②多字值"低字在前"+字符串字内字节序——写 `"ABCD"` 到 DT0 读回比对(现按字内大端拼,若读回 `"BADC"` 即需改);③`L` 双语境(链接继电器 L 位 vs 链接寄存器 LT/L 字)真机接受度(review-1005) |
| 松下 | MC 协议兼容(3E) | | | |
| 基恩士 | KV Host Link TCP | | | **帧面整体待核**(2026-09-30 降级,原"与官方手册一致"断言撤下):①端口 8000 vs 公开参照 8001;②带/不带 FCS(BCC);③有无 `##` 帧头/站号——须真机抓包核证(公开参照 pykeyence 等为 8001+`##`+BCC,与本库 8000+裸 `RD…\r` 互斥) |
| 基恩士 | KV Host Link UDP | | | 同 TCP 行(帧面待核三项) |
| 基恩士 | KV MC 协议兼容(SLMP 3E) | | | 位组记号核证(2026-09-26 复核后仍待真机,判据已定):本库按**记号数字原样**发帧(`R515` → 515)。核证法:**写 `R100`**,在 KV Studio 同时看 `R100`(组 1 位 0)与 `R604`(组 6 位 4 = 线性 100)——前者变化 = 现状正确;后者变化 = 需换算为 `组×16+位号`(那时改 `_translate_address` 并按行为变更记 CHANGELOG)。依据与反证详见 `plc/keyence/mc.py` 模块 docstring;R 软元件字单位读支持面(`_bit_device_word_access_allowed=False` 默认)与 `R<BIG>` 编号口径(原样发帧,判据同上)一并待核(review-1005) |
| 基恩士 | KV 的 MC 兼容走**三菱**客户端(`MelsecMcTcpClient` / `MelsecMcUdpClient`) | 基恩士 KV 系列 ✓ | 基恩士 KV 系列 ✓ | 2026-09-28 真机:该 KV 的 SLMP 兼容**接受标准三菱记号与软元件码**(至少 D/M/Y 已验),现场三色灯输出点 `Y90` 经 `MelsecMcUdpClient`(3E/UDP)读通;2026-10-02 补记:**写入亦已真机核证**(读写均通过)。**同一地址**用 `KeyenceMcUdpClient` 时在**组帧期被本库码表拒**(`Y` 不在 `R/B/W/DM/ZR` 五设备内,报 `不支持的 MC 软元件`,一帧未发出)——读 KV 的 X/Y 等设备请走三菱客户端。编号按三菱表为**十六进制**(`Y90` = 0x90 = 第 144 点;若该灯实为第 90 点须写 `Y5A`)。`KeyenceMc*` 的 X/Y 直读支持待手册或真机定论(未入库探针 `tools/_probe_kv_slmp.py`) |
| 基恩士 | SR 扫码枪 | — | — | SR 扫码枪为读码设备,读=扫码触发,写=不适用 |
| 丰田 | TOYOPUC 计算机链接 TCP/UDP | | | 真机核证要点(review-1005):①X/Y、T/C 基址表取自参考实现(官方手册缺),若实为分址则读写互踩——第一优先;②`M0100W`/`M0201L` 打包字/字节字索引真机支持面(闭区间校验已按参考实现 `plc-comm-toyopuc` 4.2.0);③`M0201.5` 点号形式手册是否支持 |
| 海康机器人 | ID 系列读码器 Modbus TCP | | | `HikrobotIdModbusClient`,按工业协议手册 V1.0.4 §3.5/§3.6 实现;真机核证要点:①握手全流程(使能→Ready→触发→OK/NG→Ack→OK/NG 清零,时序 §3.6 印刷页 41-42);②结果区 `result_words` 与读码器「结果模块大小」一致;③从机地址(读码器默认 255 或 0,本库 0~247,不一致先改读码器侧);④「结果字节交换」开关与 `byte_swap` 对应;⑤General Fault→clear_error 闭环 |
| 海康机器人 | ID 系列读码器 TCP 命令协议 | | | `HikrobotIdTcpClient`,按通信指令手册 V1.0.3 + ID2000/ID3000 用户手册实现;真机核证要点:①命令通道端口/结果通道端口与 IDMVS「通信命令控制」「通信配置>TCP服务器」配置一致且互不相同;②前置状态:触发模式开启+触发源=软触发+已开始采集(IDMVS 或 `<Set,Acq,1>`);③`<Exec,TriSoft>` 应答形态(OK/0/errno)与结果推送时序;④结果成帧(静默间隔 0.05s)与多码/前后缀模板输出匹配;⑤NoRead 文本与 `noread_text` 一致 |
| 海康机器人 | ID 系列读码器 MvCodeReaderSDK | | | `HikrobotIdSdkClient`,按 SDK V2.0.0 头文件 + C 指南 V1.5.3 实现(ctypes);真机核证要点:①真 DLL 加载与结构体布局实测(内存布局按 MSVC 自然对齐,头文件行号对照);②枚举双路(EnumIDDevices 私有协议/EnumDevices GigE|USB)真机命中;③Ex2 帧 `UnparsedBcrList.pstCodeListEx2` 解析(多码/质量评分仅 ID5000 支持,其余全 0);④TriggerSoftware 前置(TriggerMode=On+TriggerSource=Software);⑤NODATA 超时口径与 `with_image` 图像复制 |
| 海康机器人 | ID 系列读码器 RS-232 串口 | | | `HikrobotIdSerialClient`,按 ID2000/ID3000 用户手册串口节 + 通信指令手册 TriSeri* 实现;真机核证要点:①波特率出厂默认值(手册未载,本库默认 115200,以 IDMVS「串口通讯协议/串口触发」为准);②触发/停止文本与读码器配置一致且长度不等;③结果行 CR/LF 结尾(「输出条形码换行符使能」开启)与扫描窗内即时流出时序;④「输出无读」文本与 `noread_text` 一致;⑤单口角色(触发与输出共口)与现场接线一致 |
| Modbus | Modbus TCP | 汇川 H5U 系列 ✓ | 汇川 H5U 系列 ✓ | 2026-09-28 基础读写通过(`ModbusTcpClient`,同机汇川 H5U);FC22 掩码写(字节序可配) / FC23 读写多寄存器 / FC24 FIFO / FC43·14 设备标识待真机核证 |
| Modbus | Modbus RTU | | | FC22 掩码写 / FC23 读写多寄存器 / FC24 FIFO(按 byte count 增量收包) / FC43·14 设备标识(按对象头增量收包) / `inter_frame_delay` 帧间静默待真机核证 |
| OPC-UA | opc.tcp | | | 订阅/Browse 为 v0.35 新增,待真机验证;真机核证要点(review-1005):①真实服务器(西门子/罗克韦尔/施耐德)联测——单测用 asyncua.sync.Server 临时启停,真机零核证;②死区订阅(DataChangeFilter)走 asyncua 私有 API(`_subscribe`/`tloop.post`),跨版本兼容待核;③`subscribe_event` 的 `event_filter` 与 asyncua `_SubHandler.subscribe_events` 签名对照;④Read 多节点应答顺序(规范只"应当"按请求序) |
| CNC | MTConnect Agent HTTP/XML | | | 真机核证要点(review-1005):①Agent keep-alive 超时异常类型与 `_STALE_CONNECTION_ERRORS` 覆盖面(真实 Agent 可能 Apache/自研);②业务数据是否嵌入 `Header` 元素(嵌入则解析路径需适配,P0 隐患);③`/sample`·`/asset`·多 Device 真机核证 |