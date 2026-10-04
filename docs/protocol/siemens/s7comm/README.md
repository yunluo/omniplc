# S7comm 帧面事实档案(自研 SiemensS7Client 依据)

> 依据铁律退档口径:S7comm 无官方公开手册,帧面依据 = 参考实现逐字节比对。
> 本文记录 python-snap7 3.2.0(纯 Python 重写版,MIT,2026-03 发布)各层帧面
> 事实与出处(文件 + 函数),供 `plc/siemens/codec.py` 实现就近引用;交叉
> 裁决源:Sally7(C#,MIT)、S7netplus(MIT)、nodeS7(MIT);snap7 C++
> 源码(LGPL)只比对行为不抄码;Wireshark s7comm dissector 中立仲裁。
> 真机 pcap 是黄金帧的唯一权威——落地后随 P5 补抓包样本。
>
> 抽取日期:2026-10-03;python-snap7 3.2.0(PyPI sdist/wheel)。

## 1. 传输层:TPKT(RFC 1006)

出处:`snap7/connection.py::ISOTCPConnection._build_tpkt / receive_data`

- 头 4 字节 `>BBH`:版本 `0x03`、保留 `0x00`、**总长 u16 大端(含头)**
- 合法长度 7~65535;应答流 = 循环「读 4 字节头 → 读 总长-4」

## 2. 传输层:COTP(ISO 8073)

出处:`connection.py::_build_cotp_cr / _parse_cotp_cc / _build_cotp_dt / _parse_cotp_data`

PDU 类型:CR=`0xE0`、CC=`0xD0`、DT=`0xF0`、DR=`0x80`。

**CR(连接请求)**:
- 固定头 `>BBHHB`:PDU 长(= 6 + 参数长,不含本字节)、`0xE0`、dst_ref `0x0000`、
  src_ref `0x0001`、class `0x00`
- 参数(TLV:code 1B + len 1B + value;顺序恒 Calling → Called → PDU Size,
  与 IoTClient 标准帧「PDU Size 在前」的排序不同——TLV 序不敏感,不影响语义):
  - `0xC1` Calling TSAP:len 2,值按型号预设(缺省 `0x0100`)
  - `0xC2` Called TSAP:len 2,值 = **`(connection_type << 8) | (rack << 5) | slot`**,
    connection_type 缺省 1(PG)——即 rack 3 位(bit7~5)+ slot 5 位(bit4~0)。
    出处:`snap7/client.py::Client.connect`(L621)`remote_tsap = (connection_type << 8) | (rack << 5) | slot`
  - `0xC0` PDU Size:len 1,值 = 指数(2^值,`0x0A` = 1024)

**型号连接预设**(2026-10-04 型号参数化批;依据链「参考实现逐字节比对」
退档,全部待真机核证;落库表 `core/constants.py::S7_MODEL_PRESETS`):

| 型号 | Calling | Called | 资源类型 | 缺省 rack/slot | TPDU | 依据 |
|---|---|---|---|---|---|---|
| S7-300/400 | `0x0100` | `0x0102` | PG(1) | 0/2 | `0x0A` | python-snap7 L334/L621(缺省 PG);slot 2 = 300/400 CPU 惯例 |
| S7-1200/1500 | `0x0100` | `0x0101` | PG(1) | 0/1 | `0x0A` | 同上;slot 1 = 1200/1500 惯例(S7netplus 同) |
| S7-200 SMART | `0x1000` | `0x0300` | S7 基本(3) | 0/0 | `0x0A` | IoTClient `SiemensConstant.cs::Command1_200Smart`(L36-41)字节黄金;交叉 S7netplus `CpuType.S7200Smart`(rack0/slot1)与社区 `0x0301` 口径——分歧在槽位位,`slot` 覆写可切,真机裁决 |
| S7-200(经 CP243-1) | `0x4D57`("MW") | `0x4D57`("MW") | — | 0/0 | `0x09`(512) | IoTClient `SiemensConstant.cs::Command1_200`(L56-61)字节黄金;TSAP = Micro/WIN 记号 ASCII。S7-200 无内置以太网,仅限 CP243-1 模块接入 |

- IoTClient 标准型帧(300/400/1200/1500)与本库口径不同:其 Calling =
  `0x0102`、Called = `0x0100|rack<<5|slot`(资源类型恒 0x01,与 snap7 的
  Calling/Called 角色互换)——双方均被 CPU 接受(Calling TSAP 通常不被
  校验),本库从 python-snap7 主源口径。
- **HSL 7.0.1 交叉源坐实(2026-10-04,用户供源 `HslCommunication_Net45/
  Profinet/Siemens/SiemensS7Net.cs` L711-748)**:其 `plcHead1`/
  `plcHead1_200smart`/`plcHead1_200`/`plcHead2` 四组帧与 IoTClient
  `SiemensConstant.cs` **逐字节完全一致**——本库 SMART(0x1000/0x0300)
  与 200-CP243("MW"/TPDU 512)预设由两份独立分发的 C# 实现共同锁定。
  HSL 的型号槽位口径为旁证:1200/1500 用 slot **0**、400 用 slot **3**
  (本库从 snap7/S7netplus 的 1/2)——slot 位现场相关、CPU 宽容,`slot`
  覆写可切;V 区映射 HSL 不分型号(本库限 200/200SMART,防 1200+ 的
  DB1 静默别名);HSL 自注 T/C 读「未测试通过」(旁证本库暂不暴露 T/C)。
- 经典 S7-200 的串口 PPI 与本档案无关(PPI 帧格式另议,官方无公开文档,
  未实现,见 `docs/protocol/README.md` 待补表)。

**CC(确认)**:固定头 7 字节 `>BBHHB`(校验 type = `0xD0`),参数区可含
`0xC0` TPDU size(len 1 指数 或 len 2 原值);真机抓包(itpub,§11)CC 亦
回显 Calling/Called TSAP(`C1 02 …`/`C2 02 …`)与本库请求值一致——本库
`parse_cotp_cc` 除类型/长度外自 f481f44 起支持可选 `expected_remote_tsap`
校验 Called TSAP 回显(TLV 不按序扫描,CC 参数序 PDU Size 在前;不符拆连,
CC 未携带该参数时宽容跳过)。

**DT(数据)**:头 3 字节 `>BBB`:`0x02`、`0xF0`、`0x80`(EOT 位 + 序号 0);
数据 = S7 PDU。应答侧校验:pdu_len == 2、type == 0xF0、序号位(`& 0x7F`)为 0。

## 3. S7 报文头

出处:`snap7/s7protocol.py::S7Protocol.parse_response`(L1523-1554)

| PDU 类型 | 值 | 头长 |
|---|---|---|
| Request(Job) | `0x01` | 10 字节 `>BBHHHH` |
| ACK(写应答) | `0x02` | **12 字节** `>BBHHHHBB` |
| ACK_DATA(读/协商应答) | `0x03` | **12 字节** `>BBHHHHBB` |
| USERDATA | `0x07` | **10 字节**(无 error_class/error_code 两字节) |

头字段:协议 ID `0x32`、PDU 类型、冗余 `0x0000`、**PDU 引用 u16(序列号,
循环 +1)**、参数长 u16、数据长 u16(ACK/ACK_DATA 尾接 error_class+error_code
各 1 字节,error_class ≠ 0 即协议错误)。

## 4. 协商(SETUP_COMMUNICATION,功能 0xF0)

出处:`s7protocol.py::build_setup_communication_request`(L373-398)

- 参数 8 字节 `>BBHHH`:`0xF0`、`0x00`、max AMQ caller `1`、max AMQ callee `1`、
  PDU 长度(默认 480;按对端确认值生效)
- 应答:ACK_DATA,参数同形(`0xF0 0x00` + caller/callee/PDU 长 u16×3),
  **PDU 长度以应答为准**

## 5. 地址规范(12 字节 Any 指针)

出处:`snap7/datatypes.py::S7DataTypes.encode_address`(L76-122)

`>BBBBHHB3s`:

| 偏移 | 值 | 说明 |
|---|---|---|
| 0 | `0x12` | 变量规范类型 |
| 1 | `0x0A` | 后续长度 10 |
| 2 | `0x10` | 语法 ID S7-Any |
| 3 | 传输尺寸 | 见下表 |
| 4-5 | count u16 | 元素个数 |
| 6-7 | DB 号 u16 | 仅 DB 区非 0,其余区 0 |
| 8 | 区域码 | 见下表 |
| 9-11 | 地址 3 字节大端 | BIT:`byte<<3\|bit`;字类:`字节地址×8`;TM/CT:元素号 |

**区域码**(`S7Area`):PE `0x81`、PA `0x82`、MK `0x83`、DB `0x84`、
CT `0x1C`、TM `0x1D`。

**WordLen**(`S7WordLen`,地址规范用):BIT `0x01`、BYTE `0x02`、CHAR `0x03`、
WORD `0x04`、INT `0x05`、DWORD `0x06`、DINT `0x07`、REAL `0x08`、
COUNTER `0x1C`、TIMER `0x1D`。

## 6. 读变量(READ_AREA,功能 0x04;multi 即多项同功能)

出处:`s7protocol.py::build_read_request / build_multi_read_request`(L151-226)

- 参数 = `0x04` + 项数 1B + N × (地址规范 12 字节);multi 时 word_len 统一
  取 BYTE、count = 字节长(按地址规范的字节跨度编)
- 头的数据长 = 0

**读应答**(ACK_DATA)数据段逐项:返回码 1B(`0xFF`=成功)+ 传输尺寸 1B +
位长 u16 + 数据(传输尺寸 `0x04`=BIT 时位长/8 字节,否则按字节)+
**奇数长的项后跟 1 字节填充(非末项)**。
出处:`extract_multi_read_data`(L228-285)。

## 7. 写变量(WRITE_AREA,功能 0x05)

出处:`s7protocol.py::build_write_request`(L287-371)

- 参数 = `0x05` + 项数 1B + 地址规范 12 字节
- 数据段 = `>BBH`(保留 `0x00` + 数据传输尺寸 + 数据长)+ 数据
- **数据传输尺寸映射**(与地址规范的 WordLen 不同码):BIT→`0x03`(数据长
  =字节数)、BYTE/WORD/DWORD→`0x04`(数据长 = **位数**)、INT/DINT→`0x05`
  (位数)、REAL→`0x07`(**字节数**;review-1008 P2 订正,原误记位数——
  参考 `build_write_request` L357-358 的 `0x03/0x07/0x09` 同归字节数分支)、
  CHAR/COUNTER/TIMER→`0x09`(字节数)
- 地址规范 count = 数据长 // 元素宽(`build_write_request` L301-305),
  与数据段长度自洽(review-1008 P0-1:曾恒写 1,多字节写真机必拒绝)
- 写应答:ACK,数据段 = 逐项返回码 1 字节(`0xFF` 成功)

## 8. S7 数据段返回码

出处:`s7protocol.py::S7_RETURN_CODES`(L78-100)

`0xFF` 成功;`0x01` 硬件错误、`0x03` 不允许访问、`0x05` 地址非法、
`0x06` 类型不支持、`0x07` 类型不一致、`0x0A` 对象不存在、`0x21` PG 资源
耗尽(连接数满)等(全表见源码)。

## 9. USERDATA(SZL / 时钟 / 安全;首期只取 SZL 0x0424)

出处:`s7protocol.py::build_read_szl_request`(L1075-1120)

- 参数 8 字节 `>BBBBBBBB`:`0x00`、项数 `0x01`、`0x12`、长 `0x04`、
  方法 `0x11`(请求)、**type|group**(`0x40 | group`,SZL group `0x04` →
  `0x44`;时钟 group `0x07` → `0x47`;安全 group `0x05` → `0x45`)、
  子功能(SZL 读 = `0x01`)、DataRef(续传序号,首帧 `0x00`)
- 数据段 `>BBHHH`:返回码 `0x0A`(请求占位)、传输尺寸 `0x00`、长 u16、
  SZL ID u16、SZL Index u16
  - **前缀两字节四源分歧(review-1008 登记;2026-10-04 补 HSL 第四源)**:
    本库与 python-snap7 3.2.0 同款发 `0x0A/0x00`;snap7 C
    (`s7_micro_client.cpp` opReadSZL 的 `ReqDataFirst`)、Sharp7
    (`S7Client.cs` SZL 请求帧)、**HSL 7.0.1**(`SiemensS7Net.cs`
    `plcOrderNumber` L721-726,数据段 `FF 09 00 04 00 11 00 00`)
    发 `0xFF/0x09`(Ret=0xFF / TS=Octet)——**四源中三源 FF/09**,
    两派真机均可用(PLC 不严格校验该占位);**真机核证时若 SZL 被拒,
    首先改此字段为 FF/09**
- USERDATA 应答:头 10 字节;**参数区 12 字节**(python-snap7 3.2.0
  `_parse_userdata_response_params` L1663-1678:`[3]=0x08` 响应长、
  `[4]=0x12`、`[5]=0x84`(响应位 0x8|组)、`[6]` 子功能、`[10:12]`
  **参数级错误码**,与数据段返回码是两条错误通道,`check_userdata_response`
  L1460-1476 分别校验);应答数据段 = 返回码 1B + 传输尺寸 1B(参考桩
  `0x09`)+ 长 u16 + 载荷(SZL ID/Index/AddLen/AddCount + 记录区)

**CPU 状态(SZL 0x0424)**:python-snap7 3.2.0 的 `build_cpu_state_request/
extract_cpu_state`(L1486-1521)是**纯 Python 服务端桩实现**(源码自注
"in real S7 this would be a userdata function",`extract_cpu_state` 恒返
`"S7CpuStatusRun"`)——**不可作帧面依据**。状态字节偏移按 snap7 C 家族
`opGetPlcStatus` L2038(`opData[7]`;opData = AddLen/AddCount + 记录区,
即**记录区 [3] = bzu_id**:ereig 2B + ae 1B 之后)取值;0x08=Run /
0x04=Stop / 0x03=Stop(老 CPU 兼容)/ 其余 Unknown(有意不照搬 C 的
「未知一律按 STOP」,坏帧误报停机更危险,分歧点登记);Wireshark SZL
dissector 0x0424 记录布局交叉一致。**真机 pcap 复核保留**(登记
real-machine-checklist)。

## 10. 会话级参数(python-snap7 默认值)

出处:`snap7/client.py`(L333-341、L619-646)

- local TSAP `0x0100`、connection_type `1`(PG)、src_ref `0x0001`
  (python-snap7 缺省,即 S7-300~1500 预设;200 SMART/200-CP243 按上节
  型号预设表覆写,见 `core/constants.py::S7_MODEL_PRESETS`)
- PDU 长度请求 480(协商后取对端确认值;本库 read_area/write_area 按
  协商值自动分片:读侧容量 = PDU−18、写侧 = PDU−35,`client_base.py`
  L199-219 同款公式)
- 序列号 u16 循环(+1),应答校验 PDU 引用一致
- connect_timeout:连接期下发至 TCP 传输(review-1008 P1:曾漏下发恒用
  默认值;receive_timeout 同口径)
- COTP DR 断连帧 dst_ref = CC 应答回显值(`_send_cotp_disconnect`
  L431-450;review-1008 P2:曾恒 0)
- TCP:TCP_NODELAY + SO_KEEPALIVE(snap7 3.x 同款)

## 11. 中文逆向帖交叉对照(2026-10-04,itpub thread-2052649《西门子 PLC 的
S7 协议破解格式说明》,用户提供全文;第四方独立抓包口径,只作交叉不作依据)

原帖为 1200/300(314/315)真机抓包归纳,全部字节与本库帧面/参考实现互证:

- **COTP CR(L713 同帧)**:原帖 1200 交互一 PC 帧
  `03 00 00 16 11 E0 00 00 00 01 00 C1 02 01 00 C2 02 01 01 C0 01 09`
  ——与本库组帧逐字段一致(LI=0x11/CR/src_ref 0x0001/Calling 0x0100/
  Called=(类型<<8)|slot/TPDU 参数);**其 TPDU 报价 0x09(512)、本库
  0x0A(1024)**——均为合法报价、以对端 CC 确认为准,非分歧(snap7 亦
  0x0A)。CC 应答帧 `11 D0 00 01 00 06/04 00 C0 01 09 C1 02 01 00
  C2 02 01 01`:dst_ref 回显、CPU 小型号字节(1200=06/314=04/315=03)
  原帖自注「写程序不要校验此字节」——本库不校验该字节,一致;而
  **Called TSAP 回显(C2 参数)自 f481f44 起为可校验项**
  (`parse_cotp_cc` 的 `expected_remote_tsap`,不符拆连、缺参数宽容,
  见 §2 CC 段)——review-1017 P3-1 订正此处滞后描述。
- **协商(0xF0)**:原帖交互二请求 `…F0 00 00 01 00 01 07 80`(AMQ 1/1
  + PDU 0x0780=1920)与本库 `build_setup_comm` 同构(功能 0xF0/参数 8B
  /PDU 请求值可变),回复取对端确认——本库请求 480 与 1920 皆合法口径。
  原帖「第二个初始化报文 1200/314/315 完全一致可固化」与本库协商帧不
  含型号参数一致。
- **读请求(A 帧规律)**:A[24:26]=count(字节)、A[26:28]=DB 号、
  A[28]=区域码(0x81 Input/0x82 Output/0x83 Flag/0x84 DB)、
  A[29:32]=偏移(**位地址 = 字节×8**)——与 `build_address_spec`
  `>BBBBHHB3s` 逐字段一致;「Input/Output/Flag 的 DB 号可乱写」与
  本库非 DB 区 DB 号恒 0 一致(PLC 不校验);demo6/7/8 的 X/Y/M 读帧
  区域码与 `_AREA_CODES` 全对上。
- **读应答(B 帧规律)**:B[14:16]=参数长(0x000E 请求↔0x0002 应答,
  原帖「02+0E=10 补码感」实为 ACK_DATA 头 12B + 参数 2B 的必然)、
  B[18:24]=`00 00 04 01 FF 04` + 位长 u16——即本库条目解析
  返回码 0xFF/传输尺寸 0x04/位长 = count×8;「B[16:18]=请求数+4」
  = 数据段长(条目 4B 头 + 数据);奇数 count 的应答(demo3:16 字节
  数据)无需填充位长按字节,与本库 BYTE 口径一致。
- **写请求**:demo1 字写 `…12 0A 10 02 00 02 …84 …00 04 00 10 FF FE`
  与 `build_write`(WORD_LEN_BYTE:参数 count=2、数据段传输尺寸 0x04、
  数据长=位数 0x10=2×8)逐字节同构;demo2 位写(方式 0x01/数据段方式
  0x03/位长 1/数据 0x01)与 `_WRITE_TRANSPORT_SIZES[BIT]=0x03` 一致;
  「A[16:18]=写长+4」= 参数长 12×N+2 的单条目特例。原帖写应答
  `03 00 00 16 02 F0 80 32 03 00 00 00 05 00 02 00 01 00 00 05 01 FF`
  = ACK + 数据段 1 字节 0xFF,与 `parse_write_response` 单条目口径
  一致。
- **结论**:原帖全部帧面事实与本库实现零冲突;其「按 bit 连续写实践
  有问题,建议逐个写」与本库位写走读-改-写(按字节写)的既有取舍一致;
  型号差异仅 CPUSlot(交互一 A[18])与 CC 小型号字节——与本库
  `S7_MODEL_PRESETS` 的 slot 惯例口径同源。本节仅登记交叉,帧面依据仍
  以参考实现逐字节档案(§2~§9)与真机 pcap 为准。
