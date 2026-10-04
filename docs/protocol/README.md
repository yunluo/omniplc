# PLC 协议文档库

omniplc 实现的各品牌 / 协议族对应的**厂商手册与公开规范**,按厂商归类,便于协议
对照、码表复核与新驱动开发的参考。

> 维护规则:新协议文档加入时,在「已收录」表追加一行(注明文档编号/名称,供代码
> 引用使用);omniplc 已实现但本目录暂无对应手册的协议,在「待补」表标注(行项目前
> 与 `docs/protocol-features.md` 实现矩阵对齐)。
>
> 引用规则:协议实现代码必须引用本库收录的文档并给出**页码级出处**(文档编号/名称 +
> 第几页 + 章节/表/字段),铁律全文见 `CONTRIBUTING.md`「二、代码约定」首条。

---

## 目录结构

```
docs/protocol/
├── hikrobot/      海康机器人 — 智能读码器工业协议操作手册(五种从站模式)+ 通信指令手册 + ID2000/ID3000 用户手册 + MvCodeReaderSDK C 指南(Development 开发包)+ 固件版本要求
├── inovance/      汇川 — H3U/H3S + H5U/Easy 编程手册
├── keyence/       基恩士 — SR-2000 1D/2D 扫码枪用户手册(中文)
├── mitsubishi/    三菱 — MC 协议参考手册 + MX Component 操作手册 + SLMP 参考手册
├── modbus/        Modbus(跨厂商)— 应用协议 + TCP 实施指南 + 串口链路规范
├── mtconnect/     MTConnect — ANSI/MTC 标准 + 入门指南 + 架构白皮书
├── odva/          ODVA / Rockwell / Schneider — CIP 全家族 + EtherNet/IP 工程指南(覆盖 AB / 欧姆龙 NJ-NX)
├── omron/         欧姆龙 — FINS W342 + NJ/NX EtherNet/IP (W506) + NX EtherNet/IP 单元 (W627)
├── opcua/         OPC-UA / OPC Classic — Part 1 规范 + 安全通讯 + Brochure + 培训 + 历史背景
├── overview/      综合 — PLC 协议选型培训 + PROFINET 工业以太网参考
└── siemens/       西门子 — S7 通讯系统 + S7-1500 通讯功能 + CPU-CPU 通讯(Compendium + 概述)
```

---

## 已收录

| 厂商 | 协议 | 文件 | 来源 |
|---|---|---|---|
| 三菱 | MC 协议(3E/4E/1E/1C/3C/4C) | [mitsubishi/Mitsubishi_MC_SH080008_Communication_Protocol_Reference_Manual.pdf](./mitsubishi/Mitsubishi_MC_SH080008_Communication_Protocol_Reference_Manual.pdf) | SH-080008 |
| 三菱 | MX Component(Windows COM) | [mitsubishi/MX Component Version 4编程手册.pdf](./mitsubishi/MX%20Component%20Version%204编程手册.pdf) | 三菱 |
| 三菱 / CLPA | SLMP(Seamless Message Protocol) | [mitsubishi/Mitsubishi_SLMP_Reference_Manual.pdf](./mitsubishi/Mitsubishi_SLMP_Reference_Manual.pdf) | SH-080956 |
| 汇川 | H3U / H3S 系列编程手册 | [inovance/Inovance_H3U_H3S_Programming_Manual.pdf](./inovance/Inovance_H3U_H3S_Programming_Manual.pdf) | 19010394-SC_A20 |
| 汇川 | H5U / Easy 系列编程手册(中文) | [inovance/Inovance_H5U_Easy_Programming_Manual_CN.pdf](./inovance/Inovance_H5U_Easy_Programming_Manual_CN.pdf) | 19011157-SC_A21 |
| 基恩士 | SR-2000 1D/2D 扫码枪用户手册(中文) | [keyence/Keyence_SR-2000_1D2D_CodeReader_UserManual_Rev6.0_CN.pdf](./keyence/Keyence_SR-2000_1D2D_CodeReader_UserManual_Rev6.0_CN.pdf) | AS_103000 843CN Rev.6.0 |
| 海康机器人 | 智能读码器工业协议操作手册(覆盖 Modbus TCP / MELSEC-SLMP / FINS / EtherNet/IP / PROFINET 五种从站模式) | [hikrobot/Hikrobot_Smart_Code_Reader_Industrial_Protocol_Manual_V1.0.4.pdf](./hikrobot/Hikrobot_Smart_Code_Reader_Industrial_Protocol_Manual_V1.0.4.pdf) | V1.0.4(2025/7/17,随「智能读码器工业协议20250922.zip」发布) |
| 海康机器人 | 智能读码器工业协议固件版本要求 | [hikrobot/读码器固件版本要求.txt](./hikrobot/读码器固件版本要求.txt) | 随上包发布 |
| 海康机器人 | 工业读码器通信指令操作手册(命令面:Get/Set/Exec 格式、TriSoft 软触发、Acq 采集、错误码表;TCP/UDP/Serial 三走线) | [hikrobot/Hikrobot_Communication_Command_Manual_V1.0.3.pdf](./hikrobot/Hikrobot_Communication_Command_Manual_V1.0.3.pdf) | V1.0.3(2024/11/7,随 IDMVS 客户端 `Applications/common/doc/` 分发) |
| 海康机器人 | 极小型智能读码器用户手册(ID3000 系列;TCP 触发/TCP Server 结果输出/输出格式化标志符表) | [hikrobot/Hikrobot_ID3000_User_Manual_V1.7.0.pdf](./hikrobot/Hikrobot_ID3000_User_Manual_V1.7.0.pdf) | V1.7.0 |
| 海康机器人 | 超小型智能读码器用户手册(ID2000 系列;TCP 触发端口默认 2001 等) | [hikrobot/Hikrobot_ID2000_User_Manual_V1.1.2.pdf](./hikrobot/Hikrobot_ID2000_User_Manual_V1.1.2.pdf) | V1.1.2 |
| 海康机器人 | MvCodeReader SDK (C or C++) Developer Guide(SDK 封装依据:调用流程/结构体/错误码;本地随 Development 开发包 `Modules/MvCodeReaderSDK/Doc/` 分发) | 指南 V1.5.3(2024/01/10;SDK V2.0.0,win32/win64 双位宽 DLL) | 随 IDMVS Development 包分发 |
| 欧姆龙 | FINS | [omron/Omron_FINS_W342_Communications_Commands_Reference_Manual.pdf](./omron/Omron_FINS_W342_Communications_Commands_Reference_Manual.pdf) | W342 |
| 欧姆龙 | NJ/NX CPU 内置 EtherNet/IP 端口 | [omron/Omron_NJ_NX_CPU_EtherNetIP_Port_UsersManual_W506.pdf](./omron/Omron_NJ_NX_CPU_EtherNetIP_Port_UsersManual_W506.pdf) | W506 |
| 欧姆龙 | NX EtherNet/IP 单元 | [omron/Omron_NX_EtherNetIP_Unit_UsersManual_W627.pdf](./omron/Omron_NX_EtherNetIP_Unit_UsersManual_W627.pdf) | W627 |
| Modbus Organization | 应用协议 V1.1b3 | [modbus/Modbus_Application_Protocol_V1_1b3.pdf](./modbus/Modbus_Application_Protocol_V1_1b3.pdf) | Modbus Org |
| Modbus Organization | TCP 实施指南 V1.0b | [modbus/Modbus_Messaging_Implementation_Guide_V1_0b.pdf](./modbus/Modbus_Messaging_Implementation_Guide_V1_0b.pdf) | Modbus Org |
| Modbus Organization | 串口链路 V1.02 | [modbus/Modbus_over_serial_line_V1_02.pdf](./modbus/Modbus_over_serial_line_V1_02.pdf) | Modbus Org |
| 综合培训 | PLC 协议选型 | [overview/PLC通信协议培训_分类选型与避坑.pptx](./overview/PLC通信协议培训_分类选型与避坑.pptx) | 培训资料 |
| PI(Profibus International) | PROFINET Security Guideline v2.0 | [overview/PI_PROFINET_Security_Guideline_v2.0.pdf](./overview/PI_PROFINET_Security_Guideline_v2.0.pdf) | PI(经 SCADAhacker 镜像) |
| PI(Profibus International) | PROFINET System Description — Technology and Application | [overview/PI_PROFINET_System_Description.pdf](./overview/PI_PROFINET_System_Description.pdf) | PI(经 SCADAhacker 镜像) |
| MTConnect Institute | MTConnect 标准 ANSI/MTC1.4-2018 | [mtconnect/MTConnect_Part1_Overview_and_Fundamentals_v1.5.pdf](./mtconnect/MTConnect_Part1_Overview_and_Fundamentals_v1.5.pdf) | agent.mtconnect.org |
| MTConnect Institute | MTConnect 架构白皮书 | [mtconnect/MTConnect_Architecture_WhitePaper_2012.pdf](./mtconnect/MTConnect_Architecture_WhitePaper_2012.pdf) | mtcup.org |
| MTConnect Institute | MTConnect 入门指南 | [mtconnect/Getting_Started_with_MTConnect_Connectivity_Guide.pdf](./mtconnect/Getting_Started_with_MTConnect_Connectivity_Guide.pdf) | mtcup.org |
| ODVA | EtherNet/IP 技术概述 | [odva/PUB00138R8_EtherNetIP_Technology_Overview.pdf](./odva/PUB00138R8_EtherNetIP_Technology_Overview.pdf) | odva.org |
| ODVA | EtherNet/IP 开发者指南 | [odva/PUB00213R0_EtherNetIP_Developers_Guide.pdf](./odva/PUB00213R0_EtherNetIP_Developers_Guide.pdf) | odva.org |
| ODVA | CIP 与 CIP Networks 家族 | [odva/PUB00123R1_Common-Industrial_Protocol_and_Family_of_CIP_Networks.pdf](./odva/PUB00123R1_Common-Industrial_Protocol_and_Family_of_CIP_Networks.pdf) | odva.org |
| ODVA | CIP 通用工业协议(短版) | [odva/ODVA_CIP_Common_Industrial_Protocol.pdf](./odva/ODVA_CIP_Common_Industrial_Protocol.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | CIP Security Phase 1 | [odva/ODVA_CIP_Security_Phase1.pdf](./odva/ODVA_CIP_Security_Phase1.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | EtherNet/IP — CIP on Ethernet | [odva/ODVA_EtherNetIP_CIP_on_Ethernet.pdf](./odva/ODVA_EtherNetIP_CIP_on_Ethernet.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | ControlNet — CIP on CTDMA | [odva/ODVA_ControlNet_CIP_on_CTDMA.pdf](./odva/ODVA_ControlNet_CIP_on_CTDMA.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | DeviceNet — CIP on CAN | [odva/ODVA_DeviceNet_CIP_on_CAN.pdf](./odva/ODVA_DeviceNet_CIP_on_CAN.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | Securing EtherNet/IP Networks | [odva/ODVA_Securing_EtherNetIP.pdf](./odva/ODVA_Securing_EtherNetIP.pdf) | odva.org(SCADAhacker 镜像) |
| ODVA | EtherNet/IP Network Infrastructure 指南 | [odva/ODVA_Network_Infrastructure_EtherNetIP.pdf](./odva/ODVA_Network_Infrastructure_EtherNetIP.pdf) | odva.org(SCADAhacker 镜像) |
| Rockwell | EtherNet/IP Networking 基础 | [odva/Rockwell_Fundamentals_of_EtherNetIP_Networking.pdf](./odva/Rockwell_Fundamentals_of_EtherNetIP_Networking.pdf) | literature.rockwellautomation.com |
| Rockwell | Communicating with RA via EtherNet/IP Explicit Messaging | [odva/Rockwell_EtherNetIP_Explicit_Messaging_Guide.pdf](./odva/Rockwell_EtherNetIP_Explicit_Messaging_Guide.pdf) | literature.rockwellautomation.com |
| Rockwell | DF1 协议命令参考手册(legacy AB) | [odva/Rockwell_DF1_Protocol_Command_Reference.pdf](./odva/Rockwell_DF1_Protocol_Command_Reference.pdf) | literature.rockwellautomation.com |
| Rockwell | RA/A-B 产品 TCP/UDP 端口速查 | [odva/Rockwell_TCP_UDP_Ports_AB_Products.pdf](./odva/Rockwell_TCP_UDP_Ports_AB_Products.pdf) | literature.rockwellautomation.com |
| Schneider | EtherNet/IP 通讯原理 | [odva/Schneider_Principles_of_EtherNetIP.pdf](./odva/Schneider_Principles_of_EtherNetIP.pdf) | schneider-electric.com |
| OPC Foundation | OPC-UA Part 1 — Overview and Concepts (1.02) | [opcua/OPC-UA_Part1_Overview_and_Concepts_1.02.pdf](./opcua/OPC-UA_Part1_Overview_and_Concepts_1.02.pdf) | OPCF(经 SCADAhacker 镜像) |
| OPC Foundation | OPC-UA 安全通讯与 IEC 62541 | [opcua/OPC-UA_Secure_Communication_with_IEC_62541.pdf](./opcua/OPC-UA_Secure_Communication_with_IEC_62541.pdf) | OPCF(经 SCADAhacker 镜像) |
| OPC Foundation | OPC-UA Interoperability Brochure 2013 v2 | [opcua/OPC-UA_Brochure_2013v2.pdf](./opcua/OPC-UA_Brochure_2013v2.pdf) | OPCF(经 SCADAhacker 镜像) |
| OPC Foundation | OPC-UA Overview — Advantages and Possibilities | [opcua/OPC-UA_Overview_Advantages_and_Possibilities.pdf](./opcua/OPC-UA_Overview_Advantages_and_Possibilities.pdf) | OPCF(经 SCADAhacker 镜像) |
| OPC Foundation | OPC-UA Collaboration with PLCopen | [opcua/OPCF_OPC-UA_Collaboration_with_PLCopen.pdf](./opcua/OPCF_OPC-UA_Collaboration_with_PLCopen.pdf) | OPCF(经 SCADAhacker 镜像) |
| OPC Foundation | OPC-DA Custom Interface v2.05A Spec(legacy) | [opcua/OPCF_OPC-DA_Custom_Interface_v2.05A.pdf](./opcua/OPCF_OPC-DA_Custom_Interface_v2.05A.pdf) | OPCF(经 SCADAhacker 镜像) |
| ABB | OPC Unified Architecture 厂商视角 | [opcua/ABB_OPC_Unified_Architecture.pdf](./opcua/ABB_OPC_Unified_Architecture.pdf) | ABB(经 SCADAhacker 镜像) |
| Honeywell | OPC-UA Training Presentation | [opcua/Honeywell_OPC-UA_Training_Presentation.pdf](./opcua/Honeywell_OPC-UA_Training_Presentation.pdf) | Honeywell(经 SCADAhacker 镜像) |
| Matrikon | Guide to OPC | [opcua/Matrikon_Guide_to_OPC.pdf](./opcua/Matrikon_Guide_to_OPC.pdf) | Matrikon(经 SCADAhacker 镜像) |
| 西门子 | S7 通讯系统(Industrial Ethernet) | [siemens/Siemens_S7_Communication_System_IndustrialEthernet.pdf](./siemens/Siemens_S7_Communication_System_IndustrialEthernet.pdf) | MN_s7-cps-ie_76 |
| 西门子 | S7-1500 通讯功能手册 | [siemens/Siemens_S7-1500_Communication_Function_Manual.pdf](./siemens/Siemens_S7-1500_Communication_Function_Manual.pdf) | s71500_communication_function_manual |
| 西门子 | CPU-CPU 通讯 Compendium | [siemens/Siemens_CPU-CPU_Communication_Compendium.pdf](./siemens/Siemens_CPU-CPU_Communication_Compendium.pdf) | 78028908 |
| 西门子 | CPU-CPU 通讯 with SIMATIC(精简版) | [siemens/Siemens_CPU-CPU_Communication_with_SIMATIC.pdf](./siemens/Siemens_CPU-CPU_Communication_with_SIMATIC.pdf) | 405395 |

---

## 待补

omniplc 已实现且**仍完全无对应文档**的协议(需厂商账号或付费会员,公开渠道拿不到):

| 协议 | 缺口 | 关联客户端 |
|---|---|---|
| 日立/Via Mechanics MARK 系钻孔机 | Host 外部通信手册(SECS/GEM 选配说明、Host Link 报文格式)——向 Via 代理商索取(机种编号 + MARK 软件版本);公开渠道零文档。数采计划暂缓(todo 排期 #11,优先级:现场网关 OPC-UA/Modbus > Host Link 立项 > SECS 自研不做) | (未实现,暂缓) |
| FANUC FOCAS | FOCAS 库手册 + `fwlib32.h`(FANUC 官方 Development 包,需账号/经销商渠道)——DLL 封装族待拿依据(todo 排期 #9) | (未实现,规划 `cnc/`) |
| 三菱 CNC EZSocket | EZSocket 库手册 + SDK 头文件(三菱 CNC 渠道)——DLL 封装族待拿依据(todo 排期 #10) | (未实现,规划 `cnc/`) |
| 松下 FP | FP0H/FP7 通信手册 MC 篇 / MEWTOCOL-COM 手册(拿到后一并核:TC/CC 位写线圈命令的码表,松下独有——review-1005) | `PanasonicMcTcpClient` / `PanasonicMewtocolTcpClient` |
| 丰田 TOYOPUC | PC Link 通讯手册 | `ToyopucTcpClient` / `ToyopucUdpClient`(帧格式/命令码/基址表已由同源参考实现 `plc-comm-toyopuc` 4.2.0 双向裁决确认,打包字/字节编号口径缺陷已修,见 architecture.md §8.1;官方手册仍缺,拿到后须核:①扩展区 CMD 0x94/0x95、多站 0x60/0x61、状态/错误日志 0x70/0x7E 的帧格式;②PC10 CMD 0xC2~0xC6;③`TOYOPUC_ERROR_TEXT` 完整码表——0x44~0x51/0x53~0x65/0x71 等 26 条之外条目,review-1005) |
| 西门子 S7comm | S7comm 私有协议规范(PDU 格式/功能码/寻址规则) | `SiemensS7Client`——S7comm 为西门子私有协议,公开渠道无规范;`siemens/` 三份手册讲通讯架构而非 S7comm 报文格式。帧面依据已按参考实现退档铁律归档 `siemens/s7comm/`(python-snap7 3.2.0 逐字节抽取为主源,Sally7/S7netplus 交叉,Snap7 C 源与 Wireshark dissector 中立仲裁;v0.53 自研栈落地) |
| 西门子 S7-200 PPI(串口) | PPI 帧格式官方公开文档缺失(《S7-200 系统手册》只讲网络参数与主从关系,不含帧面);参考实现 python-snap7 3.2.0 `ppi.py`(libnodave 风格 PROFIBUS SD1/SD2 帧,V 区按 DB1 上线)——按铁律不新增实现,真机/物料到位前不立项;经典 S7-200 无 CP243 时的替代路径 = 官方 Modbus 从站指令库 + 本库 Modbus RTU | (未实现,登记待补;经 CP243-1 的 S7comm 接入已随 2026-10-04 型号批支持,`model=S7Model.S7_200`) |

omniplc 已实现且**有部分覆盖**但关键官方手册仍缺的协议:

| 协议 | 已有 | 缺口 |
|---|---|---|
| 海康机器人 ID 系列读码器(TCP 命令协议) | 《工业读码器通信指令操作手册》V1.0.3(命令面全量)+ ID2000/ID3000 用户手册 | **结果推送报文无帧界**——静默成帧间隔(`HIKROBOT_RESULT_SETTLE_INTERVAL` 0.05s)为本库工程口径,手册未定义推送帧界;**命令通道端口无出厂默认**(9989 为常用约定);「TriSoft→等结果推送」的时序组合为手册分节推断。三项均标待核,真机抓包定案 |
| 海康机器人 ID 系列读码器(Modbus 模式) | 《智能读码器工业协议操作手册》V1.0.4(寄存器模型/握手时序) | `byte_swap` 是否作用于长度寄存器 REG2 手册未明(印刷页 44 只写"交换结果数据字节序"),本库按"仅数据寄存器"解读,真机核证项 |
| 海康机器人 ID 系列读码器(SDK 会话) | MvCodeReaderSDK C 开发指南 V1.5.3 PDF(`hikrobot/`) | **V2.0.0 头文件(MvCodeReaderParams.h)未归档**(review-1002 P3)——EX2 布局守卫 `test_ex2_struct_size_matches_header` 引用的行号(行 751-823)库内无从对拍;拿到 Development 包后摘录头文件片段 + 两分支 sizeof 对拍表归档本目录 |
| 三菱 MC(QnA 兼容帧扩展命令) | SH-080008(0403 随机读/1402 随机写/0101 CPU 型号等已实现) | 时钟数据读/写(记法 0701/0702,QnACPU 串口扩展)在 SH-080008 与 SLMP 手册均无收录,需《QCPU 用户手册(基础系统篇)》;按铁律未实现,拿到手册后补 |
| Allen-Bradley EtherNet/IP | ODVA 公开白皮书 + Rockwell 官方 5 份 + Schneider 第三方教程(`odva/`,15 份) | Logix 标签编程手册(1756-RMxxx 系列)+ **1756-PM020**(Get_Attributes_List 应答示例);完整 CIP Volume 1/2 规范(需 ODVA 会员)。本批 Get_Attribute_List 逐项布局按 CIP Vol 1 §5-4 章节号 + OpENer 一致性栈/pycomm3 参考实现对照裁决(见 review.md「AB EtherNet/IP(CIP)专项」);拿到全本后补页码级引用 |
| 基恩士 KV PLC | SR-2000 扫码枪手册(`keyence/`);SLMP 参考手册(`mitsubishi/`,供 KV MC 兼容实现) | KV-8000/7500/7300 Host Link 通信命令手册 + KV MC 协议手册(需 MyKeyence 账号);KV 主机错误码表(`.H` 读路径 EA/EB 等扩展错码与十六进制数据同形状,review-1005) |
| OPC-UA | Part 1 概述(1.02)+ 安全通讯 + Brochure + Overview + OPCF/ABB/Honeywell/Matrikon/OPC-DA(`opcua/`,9 份) | Part 2-9 完整规范(需 OPC Foundation 付费会员);**Part 4 服务集**(Browse/Read/Write/Subscribe/TranslateBrowsePathsToNodeIds、Deadband DataChangeFilter §6.2.10)与 **Part 3 Address Space**(NodeId 编码规则)需页码级引用;`asyncua` 实现已覆盖核心契约(review-1005) |
| MTConnect | Part 1 概述 + 架构白皮书 + 入门指南(`mtconnect/`,3 份) | ANSI/MTC1.4-2018 完整标准(`docs.mtconnect.org` 服务器拉不下来,需等站点恢复后重试);MTConnect Agent 部署手册;**Part 3 Catalog**(条件/可用性数据项目录、Fault/Warning/Normal 三层级、Condition 值域)——当前以 Part 1 双口径并收,与标准 Agent 输出一致性待核(review-1005) |
| 欧姆龙 FINS | W342 命令帧/存储区码/结束码(`omron/`,FINS 本体,已按 §5-1-3/§5-2-2/§5-3-x 逐项核对) | **FINS/TCP 封装与节点分配握手**(魔数 `FINS`、命令 0/2、错误域、24 字节握手响应)不在 W342 范围,需欧姆龙以太网单元手册(如 W420/W465/W344);库内 `codec.py` 该段已标「依据待补」,拿到手册后补页码级引用 |

omniplc 已实现且**完全覆盖**的协议(已从本表移除,详见「已收录」):

- 欧姆龙 NJ/NX CIP — `omron/`(W506 + W627 + FINS W342)
- 西门子 S7 — `siemens/`(S7 通讯系统 + S7-1500 + CPU-CPU Compendium + 精简版)
- 汇川 — `inovance/`(H3U/H3S + H5U/Easy)

> 收到合适的手册 / 规范后,按厂商新建子目录并在「已收录」追加,「待补」对应行项的"缺口"列同步更新。