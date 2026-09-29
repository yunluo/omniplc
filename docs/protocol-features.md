# 协议功能实现矩阵

> 2026-09-30 · 基线:master `e3a8e2e`(v0.47.2 + 第七轮修复批复核后)
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
| 点位表 `TagTable`(`from_json` / `from_csv` / `bind_tags` / `read_tag` / `write_tag`) | ✅ | tag_id → 地址+类型,自动 scale/offset;表构造后严格只读 |
| 惰性自动重连 + 指数退避门控 | ✅ | `reconnect_backoff` 默认开;`next_connect_in` 可查 |
| 超时/重试(`connect_timeout` / `receive_timeout` / `retries` / `write_retries`) | ✅ | |
| 连接健康统计 `stats`(ClientStats) | ✅ | |
| 全局报文调试 `set_debug`(收发十六进制转储) | ✅ | |
| 报错中英双语 `set_lang` + 错误分类 `last_error_category`/`last_error_code` | ✅ | 9 张协议码表全覆盖翻译 |
| `with` 上下文管理器 | ✅ | |
| aio 异步包装层(`omniplc.aio`) | ✅ | 同步客户端全镜像(类名前加 `A`) |
| native 原生 asyncio 层(`omniplc.native`) | ⭕ | 5 客户端:Modbus TCP / 三菱 MC 1E·3E·4E(TCP+UDP)/ FINS(TCP+UDP);真中断语义、批量合并复用同步纯助手 |

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
| 批量写合并(`write_batch`) | ✅ | 位走 FC15 / 字走 FC16;寄存器位写走 FC06 RMW,不入合并 |
| RTU 广播写(站号 0) | ✅ | 静默期 = max(帧间延迟,T3.5[>19200 bps 固定 1.750 ms],turnaround 缺省 200 ms);广播读显式拒绝 |
| TCP 走线 | ✅ | 502 |
| RTU 串口走线 | ✅ | `configure_serial`,缺省 8N1(汇川 8N2) |
| RTU ASCII 模式 | ❌ | V1.02 附件 B 有;现场罕见,未实现 |

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
| 0101 CPU 型号读 | ✅ | `get_cpu_type` |
| 1406 块写 | ❌ | 批量写由 1401/1402 覆盖,未做 |
| 远程控制 1001~1006(RUN/STOP/锁存清除等) | ❌ | 未实现 |
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

---

## 5. 欧姆龙 FINS(W342)

客户端:`OmronFinsTcpClient` / `OmronFinsUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 0101 存储区读 | ✅ | 位 + 字;大端解码 |
| 0102 存储区写 | ✅ | 位 + 字 |
| 0104 多存储区读 | ✅ | `read_batch` 单事务混读;条数上限按链路(FINS_MAX_MULTIPLE_ELEMENTS);BOOL 走包含字后本地提位 |
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

## 16. 丰田 TOYOPUC(手册待补,同源参考实现双向裁决)

客户端:`ToyopucTcpClient` / `ToyopucUdpClient`

| 功能 | 状态 | 备注 |
|---|---|---|
| 连续字读/写(CMD 1C/1D) | ✅ | |
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
