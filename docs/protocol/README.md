# PLC 协议文档库

omniplc 实现的各品牌 / 协议族对应的**厂商手册与公开规范**,按厂商归类,便于协议
对照、码表复核与新驱动开发的参考。

> 维护规则:新协议文档加入时,在「已收录」表追加一行;omniplc 已实现但本目录暂
> 无对应手册的协议,在「待补」表标注(行项目前与 `README.md` 的实现矩阵对齐)。

---

## 目录结构

```
docs/protocol/
├── beckhoff/      倍福 — TwinCAT 3 ADS 基础 + TwinCAT 2 ADS
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
| 倍福 | TwinCAT 3 ADS 基础(TE1000) | [beckhoff/Beckhoff_TwinCAT3_ADS_Basics_TE1000.pdf](./beckhoff/Beckhoff_TwinCAT3_ADS_Basics_TE1000.pdf) | Beckhoff |
| 倍福 | TwinCAT 2 ADS(TX1000) | [beckhoff/Beckhoff_TwinCAT2_ADS_TX1000.pdf](./beckhoff/Beckhoff_TwinCAT2_ADS_TX1000.pdf) | Beckhoff |
| 西门子 | S7 通讯系统(Industrial Ethernet) | [siemens/Siemens_S7_Communication_System_IndustrialEthernet.pdf](./siemens/Siemens_S7_Communication_System_IndustrialEthernet.pdf) | MN_s7-cps-ie_76 |
| 西门子 | S7-1500 通讯功能手册 | [siemens/Siemens_S7-1500_Communication_Function_Manual.pdf](./siemens/Siemens_S7-1500_Communication_Function_Manual.pdf) | s71500_communication_function_manual |
| 西门子 | CPU-CPU 通讯 Compendium | [siemens/Siemens_CPU-CPU_Communication_Compendium.pdf](./siemens/Siemens_CPU-CPU_Communication_Compendium.pdf) | 78028908 |
| 西门子 | CPU-CPU 通讯 with SIMATIC(精简版) | [siemens/Siemens_CPU-CPU_Communication_with_SIMATIC.pdf](./siemens/Siemens_CPU-CPU_Communication_with_SIMATIC.pdf) | 405395 |

---

## 待补

omniplc 已实现且**仍完全无对应文档**的协议(需厂商账号或付费会员,公开渠道拿不到):

| 协议 | 缺口 | 关联客户端 |
|---|---|---|
| 松下 FP | FP0H/FP7 通信手册 MC 篇 / MEWTOCOL-COM 手册 | `PanasonicMcTcpClient` / `PanasonicMewtocolTcpClient` |
| 丰田 TOYOPUC | PC Link 通讯手册 | `ToyopucTcpClient` |

omniplc 已实现且**有部分覆盖**但关键官方手册仍缺的协议:

| 协议 | 已有 | 缺口 |
|---|---|---|
| Allen-Bradley EtherNet/IP | ODVA 公开白皮书 + Rockwell 官方 5 份 + Schneider 第三方教程(`odva/`,15 份) | Logix 标签编程手册(1756-RMxxx 系列);完整 CIP Volume 1/2 规范(需 ODVA 会员) |
| 基恩士 KV PLC | SR-2000 扫码枪手册(`keyence/`);SLMP 参考手册(`mitsubishi/`,供 KV MC 兼容实现) | KV-8000/7500/7300 Host Link 通信命令手册 + KV MC 协议手册(需 MyKeyence 账号) |
| OPC-UA | Part 1 概述(1.02)+ 安全通讯 + Brochure + Overview + OPCF/ABB/Honeywell/Matrikon/OPC-DA(`opcua/`,9 份) | Part 2-9 完整规范(需 OPC Foundation 付费会员);`asyncua` 实现已覆盖核心契约 |
| MTConnect | Part 1 概述 + 架构白皮书 + 入门指南(`mtconnect/`,3 份) | ANSI/MTC1.4-2018 完整标准(`docs.mtconnect.org` 服务器拉不下来,需等站点恢复后重试);MTConnect Agent 部署手册 |

omniplc 已实现且**完全覆盖**的协议(已从本表移除,详见「已收录」):

- 欧姆龙 NJ/NX CIP — `omron/`(W506 + W627 + FINS W342)
- 倍福 TwinCAT ADS — `beckhoff/`(TwinCAT 3 ADS TE1000 + TwinCAT 2 ADS TX1000)
- 西门子 S7 — `siemens/`(S7 通讯系统 + S7-1500 + CPU-CPU Compendium + 精简版)
- 汇川 — `inovance/`(H3U/H3S + H5U/Easy)

> 收到合适的手册 / 规范后,按厂商新建子目录并在「已收录」追加,「待补」对应行项的"缺口"列同步更新。