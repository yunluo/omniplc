# 三菱 CNC EZSocket(M70/M700 系)帧面档案

> 覆盖面:M70/M70V/M700/M700V(加工中心系)/M800 系 CNC 的 EZSocket
> 以太网数据采集协议(官方产品名 FCSB1224W000「三菱 CNC 用通信软件」)。
> 实现位置 `src/omniplc/cnc/ezsocket.py`(GIOP 直连,零依赖)。

## 0. 依据链与关键披露(先读)

| 层 | 依据 | 性质 |
| --- | --- | --- |
| COM 方法语义(方法名/参数含义/数据范围/错误码) | 官方手册 **FCSB1224W000 リファレンス IB-1501208**(262 页,OLE/COM 接口;PDF 本地留存 `docs/protocol/mitsubishi/FCSB1224W000_IB-1501208_EZSocket_Reference.pdf`,不入库;印刷页 2-7 起为各 I/F 详解,3-1 起为错误码表) | **官方**,权威 |
| GIOP 线上帧格式(TCP 报文逐字节) | **无官方公开文档**——手册只覆盖 COM 接口层,传输层(GIOP 变体帧)不对外。帧面依据 = 参考实现 **开源参考实现快照(内部留存,M70 真机验证)**(C 语言,MIT;作者声明已在 M70 真机验证,活跃维护至 2026-05)逐字节参照 | **单源参考**,真机核证必做 |
| 机型枚举数值(EZNC_SYS_MELDAS700M = 6) | 官方组件用法样例快照(内部留存) `m700.py`(官方 COM 组件真机样例,`Open2(6, ...)` 注释「マシンタイプ6=EZNC_SYS_MELDAS700M」)+ 开源参考实现 C 库 `typedef.h` | 双源一致 |
| section/sub_section 概念称谓 | 手册 §3 错误码 `EZNC_DATA_READ_SECT`(0x80040191)**大区分番号不正**、`EZNC_DATA_READ_SUBSECT`(0x80040192)**小区分番号不正**——官方证实该传输层参数即 NC 内部数据空间的「大区分/小区分番号」 | 官方(概念)/单源(编号值) |

**结论**:GIOP 帧格式与 section/sub_section 编号表无官方文档,编号值
全部来自参考实现单源;本驱动按「参考实现裁决 + 官方手册语义交叉 +
真机核证必做」口径落地(AB EtherNet/IP 先例同型)。真机核证项见
`docs/real-machine-checklist.md` M70/EZSocket 行。

参考实现物料:`%TEMP%\m700_ref\`(C 库全源码 + m700.py;已抓取核对)。

## 1. 连接与生命周期

- 走线:**纯 TCP**,默认端口 **683**(COM 样例 `SetTCPIPProtocol(ip, 683)`、
  C 库 `m70_cnc_connect("...", 683, ...)` 双源;EZSocket 默认口)。
- **连接 = TCP 三次握手,无握手帧**(C 库 `giop_connect` 只建 socket;
  GIOP 定义有 CloseConnection 消息类型但 C 库未使用,断开即 TCP close)。
- **request_id**:连接建立时取随机值 `0~0xFFFE`,**会话内恒定不递增**
  (C 库 `giop_connect` 中 `rand() % 0xFFFF`,此后 `build_request_pack_header`
  恒定引用,从不自增)。
- 机型(M70 首批仅 M700/M70 系;COM 手册 2.3.1 印刷页 2-7:

  | 值 | 符号 | 系列 |
  | --- | --- | --- |
  | 5 | EZNC_SYS_MELDAS700L | M700 车床系 |
  | 6 | EZNC_SYS_MELDAS700M | M700/M70 加工中心系(omniplc 缺省) |
  | 7 | EZNC_SYS_MELDASC70 | C70 |
  | 8 | EZNC_SYS_MELDAS800M | M800 加工中心系 |
  | 9 | EZNC_SYS_MELDAS800L | M800 车床系 |

  枚举数值 5~9 为 C 库 `typedef.h` 单源(0~4 为 MAGIC/600 系旧机型,
  不做);COM 层尚有「NC 制御ユニット番号 1~255」(多单元连接互异,
  手册 2-7 页)——GIOP 直连时请求里只有 `system_no`(系统号,1 起),
  无单元号字段,C 库同。
- 探活:读系统数(大区分 2/小区分 1,T_CHAR,响应 1 字节)——最轻
  零副作用读。

## 2. GIOP 请求帧布局

全部小端(CORBA GIOP 1.0 简化变体;逐字段出处 = C 库
`m70_ezsocket_private.h` 结构体 + `m70_giop.c` 组帧函数,行号指该库)。

```
偏移  大小  字段               值/含义
0     4    magic              'GIOP'(0x47 49 4F 50)
4     2    version            0x0001(写出 01 00;C 库 giop.version = 1)
6     1    byte_order         0x01 = little-endian
7     1    msg_type           0x00 = Request(Reply = 0x01)
8     4    data_length        本头之后的字节数(请求/响应通用)
---- 请求体(data_length 区)----
12    4    service_context    0x00000000
16    4    request_id         会话随机恒定值(§1)
20    1    response_expected  0x01
21    3    reserved           0x000000
24    4    object_key_length  0x00000004
28    4    object_key         0x00000001
32    4    operation_length   len(op)+1(含 NUL;如 mochaGetData → 0x0D)
36    N    operation          ASCII,定长 char 数组,NUL 结尾补零
...   4    principal          0x00000000
...   -    参数区(按操作,下表)
```

操作名与定长数组大小(C 库 `op_command_*` 常量 + 各 pack 结构体):

| 操作 | ASCII | op 数组 | operation_length |
| --- | --- | --- | --- |
| mochaGetData | 12 字符 | 16 | 13 |
| mochaSetData | 12 字符 | 16 | 13 |
| mochaGetCurrentAlarmMsgFirst | 28 字符 | 32 | 29 |
| mochaGetCurrentPrgBlockFirst | 28 字符 | 32 | 29 |
| mochaFSOpenFile / mochaFSReadFile / mochaFSStatFile | 15 字符 | 16 | 16 |
| mochaFSCloseFile / mochaFSWriteFile | 16 字符 | 20 | 17 |
| mochaFSCreateFile / mochaFSRemoveFile / mochaCancelModal2 | 17 字符 | 20 | 18 |
| mochaFSOpenDirectory / mochaFSReadDirectory | 20 字符 | 24 | 21 |
| mochaFSCloseDirectory | 21 字符 | 24 | 22 |

### mochaGetData 参数区(读,首批唯一使用)

```
principal 后依次(全 u32):
section       大区分番号(§4 编号表)
sub_section   小区分番号(§4 编号表)
system_no     系统号(1 起;0 = 全局数据如版本/时间)
axis_no       轴位标志:第 n 位 = 第 n 轴(1 → 0x01,2 → 0x02,…;
              非轴数据传 0)。注意 C 库 get_axis_real_no 是 1<<n-1
              直译,与注释「4 轴时第 4 位为 1」一致——**轴号不编码
              在低半字节以外**(response_review:此字段即位标志,
              非「轴号+0x80」)
u2            0x00000000
data_type     期望数据类型码(§4 表;响应可被服务端改写)
```

### mochaSetData 参数区(写,第二批;布局备查)

get_data 参数 + `byte_numbers`(数据字节数)+ `data_value` 联合
(u8/u16/u32/double/T_FLOATBIN 16 字节/T_STR `u32 长度 + 内容`)。
`data_length = 48 + 24(request 头) + byte_numbers`。

## 3. GIOP 响应帧布局

```
0     12   GIOP 头(msg_type = 0x01 Reply;data_length = 其后字节数)
12    4    service_context(0)
16    4    request_id(**omniplc 校验与请求回显一致**——C 库不校验,
           按 2026-09-25 FINS 加固确立的「应答身份回显校验统一模式」
           加严;不符按坏帧拆连)
20    4    is_error           0 = 成功;1 = user error;2 = system error
```

`is_error != 0` 时的错误体(**布局存疑,登记待真机核**;C 库
`receive_error_data_response` 的解析顺序照录):

```
u32     异常段长度 exceptionLen(语义未定,疑为描述串长度)
exceptionLen 字节  描述数据(丢弃)
11 字节 mel_error_code:char[3](丢弃)+ u32 error_code(§6 码表)
        + u32(丢弃)
```

`is_error == 0` 且操作为 mochaGetData 时的数据段:

```
常规(单轴/非 FLOATBIN):u32(0)+ u32 data_type + u32 data_length,
                         其后 data_length 字节数据(按 data_type 解码)
T_FLOATBIN 多轴特例:u32(0)+ u32 data_length + u32 data_type +
                     u32 data_count,其后 data_length−8 字节数据
```

mochaGetCurrentAlarmMsgFirst / mochaGetCurrentPrgBlockFirst 的数据段
为定长结构数组(GIOP 头 + 12 字节响应头之后的**全部剩余字节**,逐条):

```
alarm_string(264 B):i32 alarm_no + i32 alarm_length + char[256] text
prog_block(528 B):i32 current_block + i32 current_row + i32(未知)
                   + i32 block_length + char[512] text
```

## 4. mochaGetData 数据编号表(单源登记)

`data_type` 码(C 库 `typedef.h` `m70_data_type_e`;尺寸按 C 库
`get_data_type_length`):

| 码 | 名称 | 尺寸 | 解码 |
| --- | --- | --- | --- |
| 0x01 | T_CHAR | 1 | u8 |
| 0x02 | T_SHORT | 2 | i16 |
| 0x03 | T_LONG | 4 | i32 |
| 0x04 | T_DLONG | 8 | i64(C 库按 32 位读出低段) |
| 0x05 | T_DOUBLE | 8 | float64 |
| 0x06 | T_FLOATBIN | 16 | i16 整数位段数 + i16 小数位段数 + u32 option + float64 data |
| 0x10 | T_STR | 变长 | u32 长度 + ASCII 内容 |
| 0x21/0x22/0x23 | T_UCHAR/T_USHORT/T_UINT32 | 1/2/4 | 无符号 |

首批实现的 section(大区分)/sub_section(小区分)→ 语义
(全表 = C 库 `m70_ezsocket.c` 单源;**语义层**与官方手册方法对应
关系在右列——手册给出该方法的数据含义/范围,编号值本身待真机核):

| section | sub_section | data_type | 语义 | 官方手册锚点 |
| --- | --- | --- | --- | --- |
| 2 | 1/2/3/4/5 | T_CHAR | 系统数/NC 轴数/全轴数/主轴数/PLC 轴数 | §2.9 GetSystemData 组 |
| 2 | 100 | T_CHAR | 机床类型(1 = 车床,0 = 加工中心) | §2.4 系 |
| 35 | 10 | T_SHORT | 运转状态码(§5 表) | §2.10.5 GetRunStatus 语义族 |
| 35 | 11 | T_SHORT | 运转模式码(§5 表) | §2.10 组 |
| 35 | 20 | T_DLONG | 自动运转中(1 = 中) | §2.10.5 lIndex=1 同义 |
| 34 | 1 | T_DLONG | 主轴转速 rpm(按轴) | §2.9.6 GetSpindleMonitor 参数 2 同义 |
| 63 | 4 | T_DLONG | 主轴负载 %(按轴) | §2.9.6 参数 3 同义 |
| 59 | 4 | T_SHORT | 伺服负载(按轴) | §2.9 组 |
| 37 | 1~6 | T_FLOATBIN | 位置:1=工件 2=机械 3=当前 4=相对 5=程序 6=残指令 | §2.5.5 GetCurrentPosition/2.5.6 GetDistance 语义族 |
| 42 | 1/2/3/4 | T_FLOATBIN | 进给速度 FA/FM/FS/FE | §2.5 组 |
| 33 | 1 | T_FLOATBIN | 进给速度 FC(自动有效) | §2.5 组 |
| 45 | 100/101/102/103 | T_STR/T_STR/T_DLONG/T_DLONG | 主程序路径/程序号/顺序号/块号 | §2.7 Program 组 |
| 45 | 200/201/202/203 | 同上 | 子程序同序 | §2.7 组 |
| 25 | 1/2/3/4/10 | T_DLONG | 程序登录数/剩余数/容量字/剩余字/传送尺寸 | §2.7 组 |
| 67 | 1 | T_STR | NC 版本 | §2.4.1 GetVersion |
| 67 | 2 | T_STR | PLC 版本 | §2.4 组 |
| 68 | 1 | T_STR | NC 名称版本 | §2.4.1 |
| 40 | 1/2/3/4/5/6/7/8/100 | T_UINT32 | 电源通电/自动运转/自动启动/外部累计 1/2/日期/时刻/周期/切削时间 | §2.8 Time 组 |
| 126 | 8002 | T_LONG | 计数器 | — |
| 55 | 100000+R 号 | T_SHORT 等 | R 寄存器直读(R536 → 100536) | §2.18 Device 组(R 设备语义) |
| 53 | 10000+Y 号(十六进制) | T_CHAR 等 | Y 输出直读(Y188F → 16287) | §2.18 Device 组(Y 设备语义) |
| 54 | 10000+Y 号 | 同上 | Y 输出(另一区,C 库倍率读用) | 同上 |
| 127 | 1 | T_STR | 轴名(按轴) | §2.9 组 |

## 5. 状态码表(单源;官方 PLC 接口说明书为语义上游)

运转模式(C 库 `typedef.h` 注释,M700 PLC 信号名):

| 值 | 记号 | 含义 | 值 | 记号 | 含义 |
| --- | --- | --- | --- | --- | --- |
| 0 | MEM | 存储(自动) | 9 | HDL | 手轮 |
| 1 | DNC | DNC 运转 | 10/11 | STP | 步进 |
| 2 | LNK | 在线 | 12 | ZRN | 回零 |
| 3 | MDI | MDI | 13 | DRT | 手动进给 |
| 4 | PC | — | 14 | INI | 初始化 |
| 5 | MNL | 手动 | 15 | NON | 无 |
| 6 | JOG | JOG | 16 | LIN | — |
| 7/8 | J+H/R+H | JOG+手轮 等 | | | |

运转状态(`m70_run_status_e`):0=RST 复位、1=EMG 急停、2=RDY 就绪、
3=AUT 自动运转中、4=SYN 同步、5=CRS、6=BST、7=HLD 保持。

报警种类(`alarm_message_type_e`,传 mochaGetCurrentAlarmMsgFirst 的
msg_type):0x000 全部、0x100 NC 报警、0x200 停止因子、0x300 PLC 报警、
0x400 操作消息、0x1000 全部(不含停止因子);NC 细分 0x101 系统/
0x102 伺服/0x103 MCP/0x104 基本 PLC/0x105 用户 PLC/0x106 程序/
0x107~0x109 各警告/0x10A 操作/0x10B 操作报警。

进给速度种类(C 库注释):FA=F 指令、FM=手动有效、FS=同步、
FC=自动有效、FE=螺纹(§4 表 42/33 区)。

## 6. 错误处置

- **is_error**(GIOP 层):0 成功;1/2 = 三菱侧错误,取 mel_error_code
  的 error_code(手册 §3 官方码)分流:
  - `0x80A00101`(EZ_ERR_NOT_OPEN 通信回线未开)、`0x8202000A`
    (未 connect)→ 传输类:**拆连**(OSError,基类惰性重连);
  - 其余 → `DeviceError` 携原始码,不断线。
- 官方错误码文本表:手册 §3 表 3-1(3-1~3-11 页,约 200 条,
  EZ_*/EZNC_* 前缀);omniplc 落地首批高频子集(全表见手册 PDF)。
- **帧级**校验:magic 非 'GIOP'、msg_type 非 Reply、request_id 回显
  不符、长度不足 → `ProtocolFrameError`(带 format_hex)→ 拆连。

## 7. 未实现面(不臆造;均登记真机清单)

- **写**(mochaSetData):帧布局已在 C 库(§2),第二批随真机批落地;
  CNC 侧写操作危险性高,不随首批。
- **文件操作**(mochaFSOpenFile/ReadFile/WriteFile/CreateFile/
  CloseFile/RemoveFile/StatFile/OpenDirectory/ReadDirectory/
  CloseDirectory):DNC 程序传输场景,第二批(COM 层 m700.py 有
  read/write/delete/find_dir 用法样例可交叉)。
- **主轴/进给倍率**:三段 PLC 设备路由拼合(§4 表 53/54/55,含
  Y 区判别 + 码表换算),C 库注释单源,第二批真机核后落。
- mochaCancelModal2(模态信息取消,配报警读取):用途未定,不做。
- M800/C70 系机型:枚举值已有(§1),帧面是否一致待真机,不承诺。

## 8. 交叉核对记录

- C 库(GIOP)vs 官方组件样例 m700.py(COM)语义对照:
  `Status_GetRunStatus(1)`(自动运转中?)↔ (35,20,T_DLONG);
  `Position_GetCurrentPosition(axis)` ↔ (37,1~6,T_FLOATBIN);
  `Monitor_GetSpindleMonitor(2,1)`(转速)↔ (34,1)、参数 3(负载)
  ↔ (63,4);`System_GetAlarm2(rows,type)` ↔ mochaGetCurrentAlarmMsgFirst;
  `System_GetVersion(1,0)` ↔ (67,1,T_STR)。语义全对齐。
- request_id 恒定、无握手帧、断开即 TCP close:C 库唯一源,真机核。
- **omniplc 加严点(与 C 库差异,已披露)**:响应 request_id 回显
  校验(C 库不校验);GIOP 头 magic/msg_type 强校验;数据段长度
  以响应头 data_type/data_length 为准并做上界钳制(C 库按缓冲区
  直收)。
