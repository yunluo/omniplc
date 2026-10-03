# 协议功能实现矩阵

> 2026-10-03 · 基线:master `cbfccff`(v0.51.1 + review-1005/1006 修复批)
> 逐协议对照官方手册的标准功能清单,标明**已实现 / 部分实现 / 未实现**。
> 手册索引:`docs/protocol/README.md`;真机核证状态:`docs/real-machine-checklist.md`;
> 用法示例:`docs/examples.md`;帧级出处与页码引用:各实现模块就近注释(铁律)。

**标记**:

- ✅ 已实现
- ⭕ 部分实现 / 有条件(见备注)
- ❌ 未实现
- ➖ 设备侧不支持(非本库缺口,无需实现)

三条口径:

1. "标准功能"以已收录手册的功能目录为准;手册「待补」的协议(松下 FP 两协议、
   TOYOPUC、KV Host Link)以参考实现双向裁决后的功能面为限,未实现清单不保证穷尽。
2. **❌ 不等于排期承诺**。部分未实现项受 `CONTRIBUTING.md` 铁律约束——无官方文档
   依据不得实现(如 MC 时钟读写,见 `docs/protocol/README.md`「待补」表);另有
   部分属有意不做,各节备注注明缘由。
3. 单元测试(黄金报文)证明"按手册的字节级契约";真机联测验"现场设备行为符合
   契约"。本矩阵只描述实现面,不代替真机核证记录。

---

## 0. 跨协议公共能力(全部客户端)

| 能力 | 状态 | 备注 |
|---|---|---|
| 类型化读写 `read_bool…read_double` / `write_bool…write_double` 与泛型 `read`/`write` | ✅ | 读返回 `(ok, value)`、写返回 `bool`,不抛自定义异常;失败原因在 `last_error` |
| `read_many` / `write_many` 逐点容错批量 | ✅ | 基类逐点循环;有原生批量能力的协议另有覆写(见各节) |
| 连续批量读 `read_range(起始地址, count, 类型)` | ⭕ | 有块读原语的驱动覆写为**单事务**(Modbus FC01~04 / MC 0401 / FINS 0101 / S7 read_area / MX ReadDeviceBlock / TOYOPUC 1C / MEWTOCOL RD 及其兼容子类);其余驱动明确抛 `ValueError`(标签/节点寻址协议无"连续地址"概念,不猜地址递增) |
| 点位表 `TagTable`(`from_json` / `from_csv` / `bind_tags` / `read_tag` / `write_tag`) | ✅ | tag_id → 地址+类型,自动 scale/offset;表构造后严格只读 |
| 惰性自动重连 + 指数退避门控 | ✅ | `reconnect_backoff` 默认开;`next_connect_in` 可查 |
| 超时/重试(`connect_timeout` / `receive_timeout` / `retries` / `write_retries`) | ✅ | |
| 连接健康统计 `stats`(ClientStats) | ✅ | |
| 探活 `ping()` + 自动心跳 `heartbeat_interval`(默认 30 秒,0 = 关) | ✅ | 见下「心跳保活」节;走线协议全覆盖,能力差异见探测命令列 |
| 全局报文调试 `set_debug`(收发十六进制转储) | ✅ | |
| 报错中英双语 `set_lang` + 错误分类 `last_error_category`/`last_error_code` | ✅ | 9 张协议码表全覆盖翻译 |
| `with` 上下文管理器 | ✅ | |
| aio 异步包装层(`omniplc.aio`) | ✅ | 同步客户端全镜像(类名前加 `A`) |
| native 原生 asyncio 层(`omniplc.native`) | ⭕ | 5 客户端:Modbus TCP / 三菱 MC 1E·3E·4E(TCP+UDP)/ FINS(TCP+UDP);真中断语义、批量合并复用同步纯助手 |

### 心跳保活(ping / 自动心跳)

TCP keepalive 只能证明 TCP 栈活着,证明不了 PLC 应用/固件没卡死;应用层
心跳由各驱动提供**零副作用探测命令**,按 `heartbeat_interval`(默认 30 秒)
由守护线程(同步层)/asyncio 任务(native 层)自动驱动,亦可手动调用
`ping()`。契约:走与读写完全相同的事务口径(惰性重连、退避门控、重试、
`last_error` 三件套),失败不抛;PLC 报错(DeviceError)不断线——能应答
错误码本身就证明链路活着;传输失败(OSError)拆连后下一 tick 自动重连
自愈;显式 `disconnect()` 停止心跳(断开是调用方的明确意图)。每 tick
计入 `stats["transactions"]` 与 `heartbeat_ok`/`heartbeat_fail`。
**错误记账与业务事务隔离**(review-1002 P1-2):心跳 tick **成功不清**
业务 `last_error`、不刷 `last_success_at`/`last_rtt`(应用按这些字段监测
业务失败的语义不被心跳劫持);**失败写 `last_error` 但不计**
`error_count`/`device_error_count`(不支持探测命令的从站每 30s 确定性
报错是已知形态,不是链路劣化);传输类真实故障(OSError)不受此限,照常
计数拆连。手动 `ping()` 默认无此标记,成功照旧清错。
**与业务事务互斥**(同一把事务锁),业务长事务期间心跳排队,反之亦然;
探测沿用读重试(`retries`)与 `receive_timeout`,单 tick 最长约
`(retries+1) × receive_timeout`(自身重试 + 业务事务排队窗口)——调大
重试的部署应相应放宽 `heartbeat_interval` 或接受该排队窗口
(review-1002 P3 披露)。退避门控激活期 tick 不发包,是「未尝试」而非
「尝试失败」,不计 `heartbeat_fail`。高频轮询场景(轮询本身就是心跳)
可置 0 关闭。

| 驱动 | 探测命令 | 依据 / 备注 |
|---|---|---|
| Modbus 全系(含汇川 Modbus、海康 Modbus 模式除外) | FC08 子功能 0x0000 回显查询 | 规范 §6.8;FC08 非强制,从站不支持时异常码 01 应答 = 链路存活但 ping 为 False |
| 三菱 MC 3E/4E(KV MC、松下 MC 兼容继承) | 0101 CPU 型号读 | SH-080008 §11.2 印刷页 176-178;KV/松下对 0101 的支持待真机核证 |
| MC 1E / 3C / 4C、KV HostLink、MEWTOCOL、TOYOPUC、汇川 MC、SR、海康串口、SDK | 无探测命令,不启用 | ping 恒 False;心跳不运行 |
| 欧姆龙 FINS TCP/UDP | 0601 CPU Unit Status Read | W342 §5-3-17 印刷页 194-196;另公开 `read_cpu_unit_status()`(状态/模式/错误字解码) |
| AB EtherNet/IP | Identity Object GetAttributesAll | CIP Vol 1 §5-4;复用 `get_plc_info()` |
| 西门子 S7 | snap7 `GetCpuState` | 另公开 `get_cpu_state()`(枚举名透传,RUN/STOP);1.3/3.2 双轨核实 |
| 倍福 ADS | pyads `read_state` | 另公开 `read_state()`((adsState, deviceState),5=RUN/7=STOP) |
| OPC-UA | 读 `i=2258` Server 当前时间 | 0 命名空间标准变量(asyncua `nodes.current_time` 同款);顺带抑制空闲会话回收 |
| MTConnect | GET `/probe` | Part1 §8.3.1 p.98-100 |
| 海康 Modbus 模式 | FC03 读状态字 REG1 | 工业协议手册 V1.0.4 §3.5;不触发扫描握手 |
| 海康 TCP 命令 | `<Get,Acq>` 采集状态查询 | 通信指令手册 V1.0.3 §3;命令通道探活 |
| 三菱 MX(COM) | 有意不做 | COM 套间亲和 + GetCpuType 高位 0 出错码盲区(第八轮 P1-2) |

---

## 1. Modbus(应用协议 V1.1b3 + 串口链路 V1.02 + TCP 指南 V1.0b)

客户端:`ModbusTcpClient` / `ModbusRtuClient` / `InovanceModbusTcpClient` / `InovanceRtuClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| FC01/02 读线圈 / 读离散输入 | ✅ | 位读上限 2000 |
| FC03/04 读保持 / 输入寄存器 | ✅ | 字读上限 125 |
| FC05 写单线圈 | ✅ | |
| FC06 写单寄存器 | ✅ | |
| FC07 读异常状态 | ✅ | `read_exception_status` |
| FC08 诊断 | ✅ | `diagnostics`(子功能码透传) |
| FC11 / FC12 通信事件计数器 / 事件日志 | ✅ | |
| FC15 写多线圈 | ✅ | 批量写合并路径;上限 1968 |
| FC16 写多寄存器 | ✅ | 上限 123 |
| FC17 报告从站 ID | ✅ | `report_server_id` |
| FC20 / FC21 文件记录读 / 写 | ✅ | |
| FC22 掩码写寄存器 | ✅ | `write_mask_register` |
| FC23 读/写多寄存器 | ✅ | `read_write_registers`(先写后读;写 ≤121 / 读 ≤125) |
| FC24 读 FIFO 队列 | ✅ | |
| FC43/14 设备标识 | ✅ | `read_device_id`(basic/regular/extended;基本对象 0x00~0x02;分页续读) |
| 32/64 位类型跨寄存器 | ✅ | `word_order` 可调 |
| 批量读合并(`read_batch`) | ✅ | 同区连续地址合并(`_classify`/`_coalesce_group`) |
| 连续批量读(`read_range`) | ✅ | 位区 FC01/02 单笔 ≤2000 位;寄存器区 FC03/04 单笔 ≤125 字(16 位 1 字/元素、32 位 2 字、64 位 4 字);位号后缀/寄存器区 BOOL 拒绝 |
| 批量写合并(`write_batch`) | ✅ | 位走 FC15 / 字走 FC16;寄存器位写走 FC06 RMW,不入合并 |
| RTU 广播写(站号 0) | ✅ | 静默期 = max(帧间延迟,T3.5[>19200 bps 固定 1.750 ms],turnaround 缺省 200 ms);广播读显式拒绝 |
| TCP 走线 | ✅ | 502 |
| RTU 串口走线 | ✅ | `configure_serial`,缺省 8N1(汇川 8N2) |
| RTU ASCII 模式 | ❌ | **有意不做** —— 实际硬件太少(现场几乎全是 RTU 二进制;V1.02 附件 B 有定义),按铁律无需求场景不实现 |

---

## 2. 三菱 MC —— QnA 兼容 3E/4E(SH-080008 / SLMP SH-080956)

客户端:`MelsecMcTcpClient` / `MelsecMcUdpClient`(帧型 1E/3E/4E 可选)

| 功能 | 状态 | 备注 |
|---|---|---|
| 0401 批量读(字/位单位) | ✅ | 默认读路径 |
| 1401 批量写 | ✅ | 默认写路径 |
| 0403 随机读 | ✅ | `random_read`(字访问 / 双字访问双列表) |
| 1402 随机写 | ✅ | `random_write` |
| 0406 多块批量读(字块 + 位块) | ✅ | `read_batch`:同软元件连续位请求合并为位块(1 点 = 16 位,bit0 首);字块+位块上限 120(子命令 0000,不支持 iQ-R 扩展 0002) |
| 0401 成批读连续批量(`read_range`) | ✅ | 位软元件位单位 / 字软元件字单位单事务;所有帧型(3E/4E/1E/3C/4C)可用;点数上限**按帧型分流**(review-1002 P2):3E/4E/3C/4C 900 保守口径、1E 255(SH-080008 §12 副头部点数域)、1C 位 256/字 64(§17 BR/WR) |
| 0101 CPU 型号读 | ✅ | `get_cpu_type` |
| 1406 块写 | ❌ | 批量写由 1401/1402 覆盖,未做 |
| 远程控制 1001~1006(RUN/STOP/锁存清除等) | ❌ | **有意不做** —— PLC 运行态远程控制属运维风险面,本库定位为数据采集不纳入控制(SH-080008 运行/控制命令);MX 通道同步有意不做(见 §4) |
| 时钟数据读/写(0701/0702) | ❌ | SH-080008 与 SLMP 手册均无收录(`docs/protocol/README.md`「待补」),受铁律约束 |
| 文件 / 程序区访问 | ❌ | 未实现 |
| 1E / 3E / 4E 帧型 | ✅ | 4E 响应按序列号回显校验 |
| 路由字段(网络号 / PC 号 / 目标模块 I/O 等) | ✅ | 构造期范围校验 |
| X/Y 八进制编号开关(`xy_octal`) | ✅ | 按机型开启 |
| 监视定时器 | ✅ | |
| 字符串(D 起始连续字) | ✅ | |

> `read_many`/`read_batch` 的 0406 覆写仅 3E/4E 帧;1E/串口帧回落基类逐点独立事务。

---

## 3. 三菱 MC —— 串口 1C/3C/4C(C24)

客户端:`MelsecMcSerialClient`(帧型参数选择)

| 功能 | 状态 | 备注 |
|---|---|---|
| 1C 帧(A 兼容,文本) | ✅ | ENQ 定界 + 和校验 + CR/LF |
| 3C 帧(QnA 兼容,文本) | ✅ | 路由回显 / 帧 ID / CRLF / 和校验逐项校验 |
| 4C 帧(二进制格式 5) | ✅ | DLE STX/ETX 定界 + 附加码(10H 倍增)+ 和校验;成块收包 |
| 命令面 | ✅ | 与 3E/4E 同源(0401/1401 默认读写) |
| 站号 / 模块 I/O / message_wait 等串口参数 | ✅ | `configure_serial` |

---

## 4. 三菱 MX Component(Windows COM)

客户端:`MelsecMxClient`(comtypes;逻辑站号)

| 功能 | 状态 | 备注 |
|---|---|---|
| 字软元件块读/写(ReadDeviceBlock / WriteDeviceBlock) | ✅ | 块读写走原始 vtable 通道 |
| 随机读/写(ReadDeviceRandom / WriteDeviceRandom) | ✅ | |
| 位软元件 get/set | ✅ | |
| CPU 型号 | ✅ | |
| 时钟读/写 | ✅ | 仅 MX 通道;协议通道缺手册依据未做(见 §2) |
| 错误消息文本 | ✅ | `get_error_message` |
| `read_batch` / `write_batch` | ✅ | |
| 连续批量读(`read_range`) | ⭕ | 字软元件数值类型 = ReadDeviceBlock 单事务(≤960 字);BOOL 连续读拒绝(MX 位块读 16 点/字与单点读语义不一致) |
| 远程控制 `SetCpuStatus`(RUN/STOP/PAUSE/锁存清除) | ❌ | **有意不做** —— PLC 运行态远程控制属运维风险面,本库定位为数据采集不纳入控制(MX Component 编程手册运行控制章);MC 协议通道同步有意不做(见 §2) |

---

## 5. 欧姆龙 FINS(W342)

客户端:`OmronFinsTcpClient` / `OmronFinsUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 0101 存储区读 | ✅ | 位 + 字;大端解码 |
| 0102 存储区写 | ✅ | 位 + 字 |
| 0104 多存储区读 | ✅ | `read_batch` 单事务混读;条数上限按链路(FINS_MAX_MULTIPLE_ELEMENTS);BOOL 走包含字后本地提位 |
| 0101 连续批量读(`read_range`) | ✅ | 字区数值类型连续字读 / 位区(CIO/W/H/A)BOOL 连续位读,单命令 ≤999(位区按位、字区按字,两分支同限);T/C 完成标志、字区 BOOL 拒绝 |
| 存储区覆盖 | ✅ | CIO / W / H / A / D / EM(bank 0~15)/ T·C(位=完成标志只读,字=PV 可读写) |
| FINS/TCP(握手 + 节点自动推导) | ✅ | `local_node`/`destination_node` 缺省 None 自动(握手获取) |
| FINS/UDP | ✅ | 目标/源节点从 IP 末段推导(UDP connect 探测) |
| 路由参数(目的地/源 网络·节点·单元) | ✅ | 构造期范围校验 |
| SID / 命令码 / 响应 ICF 回显校验 | ✅ | 2026-09-25 加固批落地 |
| 其余命令组(参数区 / 程序区 / 文件内存 02xx~04xx,运行状态 0501,CPU 状态 0601~0603,时钟 0701/0702,消息 09xx) | ❌ | 当前仅 01xx 数据读写组 |
| FINS/TCP 封装与节点分配握手 | ⭕ | 帧面已核证,手册不在 W342 范围(依据待补,见 `docs/protocol/README.md`) |

---

## 6. 欧姆龙 NJ/NX 内置 EtherNet/IP CIP(W506 / W627)

客户端:`OmronCipClient`(继承 AB 客户端,覆写路由与变量面)

| 功能 | 状态 | 备注 |
|---|---|---|
| 符号(变量)读/写 | ✅ | 继承 AB 0x4C/0x4D;数组元素 / 结构成员路径语法 |
| 批量读 | ✅ | 继承 Get_Attribute_List 打包 |
| 位写 | ✅ | 继承 0x4E 读-改-写 |
| unconnected / connected 双通道 | ✅ | 继承 |
| 通用 CIP 消息 / list_identity | ✅ | 继承 |

---

## 7. Allen-Bradley EtherNet/IP(ODVA CIP + Rockwell)

客户端:`AllenBradleyEthIpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| Read Tag(0x4C) | ✅ | 标签类型自描述并校验(`_ensure_type`) |
| Write Tag(0x4D) | ✅ | 原子类型;写应答回显宽容(联测实帧核证) |
| Read-Modify-Write(0x4E) | ✅ | 整型标签 `Tag.3` 位访问设备侧原子修改 |
| Unconnected Send(0x52) | ✅ | 默认通道 |
| Connected Messaging | ✅ | `connected_messaging` 开关;Forward Open/Close + UnregisterSession(断链注销) |
| `list_identity` | ✅ | |
| `get_plc_info`(控制器信息) | ✅ | |
| `get_attribute_all` / `get_attribute_list` | ✅ | 批量读打包分片(`_chunk_batch_requests`) |
| `generic_message`(任意服务/类/实例) | ✅ | |
| 字符串(STRING 4 字头)/ BOOL 数组元素 | ✅ | |
| 分片读/写(Read/Write Tag Fragmented 0x52/0x53) | ❌ | 超大字符串/数组受限 |
| 标签列表枚举(Get_Instance_Attribute_List 0x55) | ❌ | 未实现 |
| CIP Security | ➖ | 永不考虑(内网部署口径,项目红线) |

---

## 8. 倍福 TwinCAT ADS(TE1000 / TX1000,pyads 封装)

客户端:`BeckhoffAdsClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 按符号名读/写(read_by_name / write_by_name) | ✅ | net_id 缺省由 IP 自动推导 |
| 符号类型自描述 | ✅ | `symbol_type` |
| DataType ↔ pyads PLCTYPE 映射 | ✅ | |
| 字符串 | ✅ | 声明长度自适应 |
| 按 index group/offset 裸读写 | ❌ | pyads 能力未暴露 |
| SUM 读/写(0xF080/0xF081 多条打包) | ❌ | 未实现 |
| 设备信息/状态(ReadDeviceInfo / ReadState / WriteControl) | ❌ | 未实现 |
| ADS 通知(Add/DeleteDeviceNotification) | ❌ | 未实现 |

---

## 9. 基恩士 KV Host Link(手册待补)

客户端:`KeyenceHostLinkTcpClient` / `KeyenceHostLinkUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| RD 单点读 / RDS 连续读 | ✅ | |
| WR 单点写 / WRS 连续写 | ✅ | |
| X/Y 十六进制编号 | ✅ | KV 的 X/Y 为十六进制,已按手册口径处理 |
| 数据格式(.H/.U) | ✅ | 读校验随格式开关 |
| 错误码表(KV_ERROR_TEXT) | ✅ | 入 i18n 翻译守卫 |
| TCP / UDP 走线 | ✅ | 8000 |
| 其余 Host Link 命令(区块传输 / 状态 / 时钟等) | ❌ | 手册待补(MyKeyence 账号),见 `docs/protocol/README.md`「待补」 |

---

## 10. 基恩士 KV MC 兼容(SLMP 3E)

客户端:`KeyenceMcTcpClient` / `KeyenceMcUdpClient`(继承三菱 MC)

| 功能 | 状态 | 备注 |
|---|---|---|
| 继承 MC 全命令面(0401/1401/0403/1402/0406/0101) | ✅ | 软元件码走基恩士表 |
| 帧型 3E | ✅ | TCP + UDP |
| 4E / 1E | ➖ | 基恩士 SLMP 兼容模式不提供 |

---

## 11. 汇川 H3U/H5U(Modbus 映射)

客户端:`InovanceModbusTcpClient` / `InovanceRtuClient`(继承 Modbus)

| 功能 | 状态 | 备注 |
|---|---|---|
| 地址记号映射(M/D/X/Y/S/T/C → Modbus 区) | ✅ | H3U/H3S(19010394)与 H5U/Easy(19011157)编程手册 |
| TCP / RTU(9600-8N2) | ✅ | |
| 其余继承 Modbus 全功能面 | ✅ | 含 FC07~FC43 扩展与广播写 |

---

## 12. 汇川 MC 兼容(Easy/H5U)

客户端:`InovanceMcTcpClient`(继承三菱 MC)

| 功能 | 状态 | 备注 |
|---|---|---|
| 3E 帧继承全命令面 | ✅ | 汇川记号 → 三菱帧编号换算(如 X17 → 0x0F 十六进制) |
| 4E / UDP / ASCII | ❌ | 设备支持,我方留后续 |

---

## 13. 松下 MC 兼容(FP0H/FP7)

客户端:`PanasonicMcTcpClient`(继承三菱 MC)

| 功能 | 状态 | 备注 |
|---|---|---|
| 3E 帧继承全命令面 | ✅ | 松下记号线性化映射(_linearize) |
| 4E | ➖ | 松下 MC 兼容模式仅提供 QnA 兼容 3E 二进制 |

---

## 14. 松下 MEWTOCOL(手册待补)

客户端:`PanasonicMewtocolTcpClient` / `PanasonicMewtocolUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 接点读 RCS / 接点写 WCS | ✅ | 位写对数据区走读-改-写 |
| 数据区成批读 RD / 成批写 WD | ✅ | 字内字节序:高字节在前 |
| 连续批量读(`read_range`) | ⭕ | 数据区数值类型 RD 单事务(编号域 0~99999);接点区无批量接点命令、BOOL 连续读拒绝 |
| 站号(01~99)+ BCC 异或校验 | ✅ | |
| TCP / UDP 走线 | ✅ | 1024;UDP 整包预算拦截 |
| 字符串 | ✅ | |
| 其余命令组(定时/计数经过值、程序区、状态、自诊断) | ❌ | 手册待补,见 `docs/protocol/README.md`「待补」 |

---

## 15. 基恩士 SR 扫码枪(SR-2000 手册 Rev6.0)

客户端:`KeyenceSrClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 触发扫码(LON,bank 1~16 → LOFF → 读应答) | ✅ | `scan()`;动作型走写语义(重试默认 0,防重复触发) |
| 复位 | ✅ | `reset()` |
| ER,错误应答解析 | ✅ | 手册 §12-1 印刷页 76 |
| 读码等待(scan_dwell)可调 | ✅ | |
| TCP 走线 | ✅ | 9004 |
| 其余命令(参数读写 / 用户数据等) | ❌ | 按需再补 |

---

## 15A. 海康机器人 ID 系列智能读码器(工业协议手册 V1.0.4)

客户端:`HikrobotIdModbusClient`(继承 `ModbusTcpClient`,读码器即 Modbus TCP 从站)

依据:《海康机器人智能读码器工业协议操作手册》V1.0.4(`docs/protocol/hikrobot/`);
适用 ID2000/ID3000/ID5000 系列、IDH 手持终端、SC2000A 导航传感器(固件要求见同目录 txt)。

**协议角色裁决**(手册全文研读结论,决定库内可落地范围):五种工业协议中读码器侧角色不同——
Modbus TCP 服务端模式读码器为**从站**(本库主站对接 ✅);MELSEC-SLMP(轮询 PLC D 区)/
FINS(轮询 PLC DM 区)读码器为**主站**、对接方须为被动服务器,读码器↔PLC 点对点,上位库无角色 ❌;
EtherNet/IP 为 **Class 1 隐式 I/O 适配器**(EDS 仅声明 Class 1 周期连接,无显式报文通路文档)❌;
PROFINET 为设备侧,需 RT 控制器,库无 PROFINET ❌。

| 功能 | 状态 | 备注 |
|---|---|---|
| 触发读码全握手(scan) | ✅ | §3.6 印刷页 41-42:使能→Ready→Trigger 上升沿→Ack 后回落→轮询 OK/NG→Ack 应答→等 OK/NG 清零(闭环防陈旧结果) |
| 结果区解码(长度字 + ASCII) | ✅ | §3.5 印刷页 41;NoRead 使能时 OK+"NoRead" 原样透传 |
| 结果字节交换解码 | ✅ | `byte_swap` 参数,对应读码器侧「ModBus 结果字节交换」开关(§3.6 印刷页 44) |
| Results NG 三态区分 | ✅ | NG(未读到码)→ `(False, None)`;General Fault / 超时 → 抛异常(细分自动化处置) |
| 状态区快照(read_status) | ✅ | §3.5 状态区 REG1:Ready/Ack/Acquiring/Decoding/OK/NG/General Fault |
| 错误清除(clear_error) | ✅ | 控制字 bit15 置位→轮询故障清零→复位控制字 |
| 状态+结果同笔 FC03 快照读 | ✅ | REG1..REG2+N 连续,read_batch 合并,防"状态 OK 而结果区仍旧值"错配 |
| 结果区大小匹配(4~500 字) | ✅ | `result_words` 须与 IDMVS「结果模块大小」一致(默认 100,§3.5 印刷页 41) |
| 元数据(质量/码制/位置等) | ⭕ | 经 IDMVS「数据处理」配置并入输出串随结果区透传;**结构化元数据**(多码列表/质量分/位置)需读码器原生 TCP 命令协议,手册待取,另行驱动 |
| 站号 0~247 | ✅ | 读码器默认 255 或 0(§3.2 印刷页 31);本库按 Modbus 规范钉 0~247,缺省取 0,现场配 255 需改 |
| MELSEC-SLMP / FINS / EIP / PROFINET 模式 | ❌ | 读码器侧为主站/适配器/设备,见上方角色裁决——库不实现被动服务器、Class 1 I/O 扫描器与 RT 控制器 |

---

## 15B. 海康机器人 ID 系列读码器(TCP 命令协议)

客户端:`HikrobotIdTcpClient`(BaseClient 子类,双通道会话)

依据:《工业读码器通信指令操作手册》V1.0.3(随 IDMVS 分发,`docs/protocol/hikrobot/`)+
《极小型(ID3000)智能读码器用户手册》V1.7.0 / 《超小型(ID2000)智能读码器用户手册》V1.1.2。

**双通道架构**(手册要求两端口不重复):命令通道 = 读码器作 TCP 服务端(「通信命令控制」
模块),本库发 `<Get/Set/Exec,cmdStr[,param]>`、收 `<...,...,OK/errno>` 角括号应答
(指令手册 §1.3 印刷页 1-2);结果通道 = 读码器作 TCP 服务端(「通信配置 > TCP 服务器」)
主动推送结果报文(极小型手册 §8.6.5 印刷页 137),按静默间隔成帧。

| 功能 | 状态 | 备注 |
|---|---|---|
| 软触发 + 等结果(scan) | ✅ | `<Exec,TriSoft>`(指令手册印刷页 9)→ 应答 OK → 结果推送成帧;动作型写语义 |
| 被动收结果(read_result) | ✅ | 不触发只收推送——IO 硬触发场景(触发源=管脚)下 PC 侧纯接收 |
| 软触发命令(trigger) | ✅ | 仅 `<Exec,TriSoft>`,不等结果 |
| 采集控制/查询 | ✅ | `<Set,Acq,0/1>` / `<Get,Acq>`(印刷页 7) |
| 低阶命令面(command) | ✅ | 手册 §3 全部 Get/Set/Exec 指令透传(印刷页 7-12);回显校验 + 坏帧带原始帧 |
| errno 错误码表 | ✅ | -1~-7 中文(印刷页 12-13),`last_error_code` = 负 errno |
| 结果成帧(静默间隔) | ✅ | 手册未定义帧界,按 `settle_interval` 静默成帧;多码/前后缀模板透传 |
| NoRead 判定 | ✅ | 「输出无读」默认 `NoRead`(极小型印刷页 150),`noread_text` 可配 |
| 元数据(质量/码制/位置/触发号等) | ⭕ | 读码器「输出格式化」16 个占位符(`<code_quality>`/`<code_cen_pos>`/`<code_type>` 等,极小型印刷页 150)随结果文本透传,调用方按现场模板解析;SDK 级结构化结果需 SmartSDK 封装(另行评估) |
| TCP 触发文本通道(2001 start/stop) | ❌ | 手册另有 2001 端口文本触发(超小型印刷页 45-47),软触发已由 TriSoft 覆盖,流式扫描窗口控制暂不做 |
| UDP 命令/Serial 命令走线 | ❌ | 指令手册支持 UDP/Serial(§2.2/§2.3),按需再补 |

---

## 15C. 海康机器人 ID 系列智能读码器(MvCodeReaderSDK ctypes 封装)

客户端:`HikrobotIdSdkClient`(BaseClient 子类,SDK 会话型)

依据:MvCodeReaderSDK V2.0.0(随 IDMVS Development 开发包分发)+《MvCodeReader SDK
(C or C++) Developer Guide》V1.5.3(`Modules/MvCodeReaderSDK/Doc/`);结构体/枚举按头文件
`MvCodeReaderParams.h` 就近行号引用,调用流程按指南 §2(印刷页 9-11)与 §3.3.3(印刷页 27-28)。

| 功能 | 状态 | 备注 |
|---|---|---|
| 设备枚举 + 按 IP 匹配(connect) | ✅ | 私有协议 `EnumIDDevices` + GigE/USB `EnumDevices` 双路;CreateHandle→OpenDevice→StartGrabbing(指南 §2 印刷页 10-11) |
| 软触发 + 取一帧(scan) | ✅ | `TriggerSoftware` 命令(指南 §3.5.12 印刷页 44)→ `GetOneFrameTimeoutEx2`(指南 §3.3.3 印刷页 27-28);动作型写语义 |
| 被动取帧(read_frame) | ✅ | 不触发只取帧——连续模式/外部触发场景 |
| **条码全量元数据** | ✅ | `MV_CODEREADER_BCR_INFO_EX2`(Params.h 行 606-635):内容/码制/四点坐标/角度(10 倍)/PPM/算法耗时/清晰度/读码评分/触发时间戳(s+µs)/库号/ROI 号 |
| **条码质量评分** | ✅ | `MV_CODEREADER_CODE_INFO`(Params.h 行 513-561):2D 十项等级(对比度/均匀性/纠错/打印伸缩等)+ 1D 六项等级(边缘/反射率/可译码性等);**仅 ID5000 系列支持,其余设备全 0**(指南 §3.3.3 印刷页 28) |
| 多码输出 | ✅ | `nCodeNum` 遍历,单帧最多 300 条(扩展,Params.h 行 68) |
| 参数访问面 | ✅ | Int/Enum/Bool/Float/String Get/Set + SetCommandValue(指南 §3.5 印刷页 37-45,GenICam 节点名) |
| NODATA 超时三态 | ✅ | `MV_CODEREADER_E_NODATA`(ErrorDefine.h 行 15)→ (False, None) 不断线;SDK 错误码 → DeviceError code 原始值 |
| 位宽匹配 | ✅ | 按解释器位数自动选 win32/win64 子目录(SDK 双位宽 DLL 齐备);`dll_path` 显式覆盖 |
| 图像原始数据 | ⭕ | `with_image=True` 随帧复制(默认关);图像格式转换/保存(SaveImage)未做 |
| 面单抠图/OCR/AGV/通道(MSC) | ❌ | 指南 §3.6/多通道 API 按需再补;回调模式与轮询互斥(指南 §3.3 印刷页 33) |
| 依赖 | — | MvCodeReaderSDK 运行库(核心零依赖,ctypes 直绑);前置 MVS SDK Runtime ≥3.0.0(指南 §2 印刷页 9) |

---

## 15D. 海康机器人 ID 系列读码器(RS-232 串口)

客户端:`HikrobotIdSerialClient`(BaseClient 子类,串口走线,SR 同型)

依据:《超小型(ID2000)智能读码器用户手册》V1.1.2 §4.6.1/§4.6.2(串口触发文本
start/stop,印刷页 45-47)+ §4.7.3 串口通讯协议(印刷页 52)+《极小型(ID3000)
用户手册》V1.7.0 §8.6.3(印刷页 134-135)与「输出条形码换行符使能」(印刷页 151)
+《通信指令手册》V1.0.3 TriSeriStart/TriSeriStop(印刷页 9)。

**单串口角色**:设备仅一路 RS-232(工业协议手册 §3 印刷页 29),串口触发与
串口结果输出共用同一物理口。结果在扫描窗**内**即时流出(SR 是关窗后应答),
故时序为 start → 读结果行 → stop。

| 功能 | 状态 | 备注 |
|---|---|---|
| 串口触发 + 读结果行(scan) | ✅ | start(默认文本,可配 1~31 字符)→ 读 CR/LF 结果行 → stop 收窗;超时也尽力停窗 |
| 多码编排(trigger/read_result/stop) | ✅ | 手工触发 → 逐行收 → 收窗 |
| 触发/停止文本可配 | ✅ | 手册默认 start/stop;**长度不可相等**(指令手册印刷页 9,构造期校验) |
| NoRead 判定 | ✅ | 「输出无读」默认 `NoRead`,`noread_text` 可配 |
| 半行残留断线 | ✅ | 超时后 drain:读到字节未达行尾 → 断线重同步(SR P2-13 同口径) |
| 结果行字节上限 | ✅ | 65536(格式化模板可含多码长文本) |
| 串口命令协议(`<Exec,TriSoft>` 走串口) | ❌ | 「通信命令控制」选 Serial 的命令帧走线与文本触发混流需按现场角色二选一,本类按文本触发口径;如需命令帧另开形态 |
| 元数据 | ⭕ | 「输出格式化」占位符随结果行透传(SR/TC同口径);结构化元数据走 SDK 封装(§15C) |
| 波特率 | — | 手册未载出厂默认,构造默认 115200 以 IDMVS 配置为准(可配 4800~115200,TriSeriBaud 印刷页 8) |

---

## 16. 丰田 TOYOPUC(手册待补,同源参考实现双向裁决)

客户端:`ToyopucTcpClient` / `ToyopucUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 连续字读/写(CMD 1C/1D) | ✅ | |
| 连续批量读(`read_range`) | ⭕ | 字软元件数值类型 CMD=1C 单事务(≤512 字);位软元件无批量位读命令(仅 CMD 20 单点),BOOL 连续读拒绝 |
| 连续字节读/写(CMD 1E/1F) | ✅ | 字符串走此(高/低字节起点可选) |
| 单位(位)读/写(CMD 20/21) | ✅ | |
| 响应 CMD 回显校验 + RC 结束码表 | ✅ | |
| TCP(按帧头长度分段收包)/ UDP | ✅ | |
| 扩展区(CMD 0x94/0x95)、PC10(0xC2~0xC6)、多站/中继(0x60/0x61)、状态/错误日志(0x70/0x7E) | ❌ | 官方手册待补;拿到后须逐项核证帧格式(待补表已登记要点) |

---

## 17. OPC-UA(IEC 62541 概览;asyncua 1.1.5 封装)

客户端:`OpcUaClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 读 / 写(单点,NodeId 字符串) | ✅ | |
| 批量读(`read_batch`) | ✅ | |
| DataChange 订阅 | ✅ | `subscribe_data_change`(间隔 / 队列 / 死区) |
| 事件订阅 | ✅ | `subscribe_event`;asyncua 默认过滤器聚合 BaseEventType 全部属性(**含 SourceNode/Time**);自定义 `event_filter` 透传 |
| Browse | ✅ | 别名表 + 递归;不存在节点返回空树不报错 |
| DataValue 时间戳 / 质量 | ❌ | 仅取值 |
| 方法调用(Call) | ❌ | 未实现 |
| 历史访问(HistoryRead) | ❌ | 未实现 |
| TranslateBrowsePathsToNodeIds | ❌ | 以别名表替代 |
| TLS / 加密栈 | ➖ | 永不考虑(内网部署口径,项目红线) |

---

## 18. CNC MTConnect(ANSI/MTC1.4 概览;标准库 HTTP/XML)

客户端:`MTConnectClient`(只读)

| 功能 | 状态 | 备注 |
|---|---|---|
| current 快照 | ✅ | `snapshot()` |
| sample(时间窗采样) | ✅ | `read_sample` |
| probe(设备能力描述) | ✅ | `probe` / `probe_all` |
| conditions(报警/条件) | ✅ | `read_conditions` |
| assets(工装/资产) | ✅ | `read_assets` |
| 地址 = 数据项 id | ✅ | item id 即地址,自动类型推断 |
| 写 | ❌ | 协议为只读采集语义,基类 `write` 显式拒绝 |
| XXE 防御(解析前拒 DOCTYPE)+ 响应体 16 MiB 上限 | ✅ | 安全加固批落地 |
| keep-alive 失效原位重建透明重试 | ✅ | |

---

## 19. 西门子 S7(python-snap7 封装)

客户端:`SiemensS7Client`

| 功能 | 状态 | 备注 |
|---|---|---|
| DB 读写(DBX 位 / DBB / DBW / DBD / DBS) | ✅ | 须**非优化块**(绝对寻址) |
| I / Q / M 读写(位 / 字节 / 字 / 双字) | ✅ | |
| 位写 | ✅ | 本地读-改-写(非设备原子) |
| S7 String / WString | ✅ | |
| 批量读(`read_batch` = snap7 read_multi_vars) | ✅ | 单事务混读,每请求 ≤20 项(snap7 上限) |
| 连续批量读(`read_range` = read_area) | ✅ | 同区域字节起点起 `count × 类型字节数` 一次读回;BOOL/STRING 拒绝 |
| 存储区覆盖 | ✅ | DB(0x84)/ I=PE(0x81)/ Q=PA(0x82)/ M=MK(0x83) |
| T / C 定时器计数器区 | ❌ | snap7 支持,地址面未暴露 |
| SZL 系统状态列表读 | ❌ | 未实现 |
| CPU 控制(RUN/STOP)/ 时钟 / 组态 | ❌ | 未实现 |
| S7-1200/1500 优化 DB | ➖ | 经典 S7comm 协议边界(优化块需符号访问);DB 访问失败的错误消息已带指引 |
| 依赖双轨 | ✅ | 3.7~3.9 → snap7 1.3 / 3.10+ → 3.x;错误双线翻译 |

---

## 维护约定

- 新增协议 / 命令面时**同步更新本矩阵**;「待补」协议拿到手册后,把对应节的
  ❌/⭕ 项按手册复核一遍(可能存在"手册有而我们以为设备没有"或反之)。
- 本矩阵与 `docs/protocol/README.md`「待补」表联动:缺口登记以那边为准,
  这边只描述实现面。
- 评审协议面对照(如与第三方库横向比较)时,以本矩阵为事实底数。
