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
- 第三方审查参考:`docs/review.md`(现场 PLC 视角评审,**复核 v2,2026-09-26**,含修复记录与"实锤/文档化限制/待核证"分档)
- **原生异步层(omniplc.native)**:首批 5 个客户端(Modbus TCP / MC 1E·3E over TCP·UDP / FINS TCP·UDP)是**独立于同步层的代码路径**——帧级已由"同步 × 异步对拍"测试锁死,但**真机尚未联测**,需与对应同步行一并核证(见 `docs/architecture.md` §12)
- 提交新真机记录:PR 模板「测试」节勾选"真机联测",并在 `CHANGELOG.md` 加条目

## 待真机核证项(v0.45.0 更新,自 README 迁入)

下表汇总散落各处的真机核证项(实现已完成,缺真机条件或排队中):

| 项                | 驱动               | 现状态                            |
|------------------|------------------|--------------------------------|
| Modbus FC24/22    | Modbus TCP/RTU   | FC24 FIFO、FC22 掩码字节序可配,需设备支持,待真机核证 |
| AB 0x0A 多服务包批量读  | AB Logix         | 已实现(超 32 条/480B 自动拆包),通用模拟器不支持,待真机核证 |
| AB connected RPI  | AB Logix         | RPI 默认 100ms(`rpi_us` 可配)、CIP 0x01/0x07 重连,待真机核证 |
| NJ CIP 0x0A 多服务包 | 欧姆龙 NJ/NX CIP    | 继承 AB,理论同,待真机核证                |
| 倍福 ADS           | TwinCAT          | 封装 pyads,需 TwinCAT 运行时;transport 通断码依 TE1000 §8 修正(0x06/0x07/0x0D/0x12/0x1B/0x1D + Router 0x0500~0x050D)、`set_timeout` 告警待真机核证 |
| 西门子 S7           | S7-300/1200/1500 | 封装 python-snap7,需 PLC 或 PLCSIM;STRING/WSTRING 读截断·写保留声明长、优化块访问错误提示待真机核证 |
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

---

## 核验记录表

按厂商 → 协议 升序排列;读/写独立单元格。

| 厂商 | 协议 | 读取 | 写入 | 备注 |
|---|---|---|---|---|
| 三菱 | MC 3E | | | 新增设备码 L/F/SB/V/DX/DY/TS/TC/TN/CS/CC/CN/SM/SD/SW 待真机核证(尤 **TN=0xC3 / CN=0xC6** 为推定);位软元件位号后缀(`M10.5`)已改 `ValueError`,字软元件位访问(`D100.3`)回归正常 |
| 三菱 | MC 4E | | | |
| 三菱 | MC 1E | | | |
| 三菱 | MC 1C(串口) | | | |
| 三菱 | MC 3C(串口) | | | |
| 三菱 | MC 4C(串口) | | | |
| 三菱 | MX Component | 三菱 FX3U ✓ | 三菱 FX3U ✓ | get_error_message(ActSupportMsg) 待核证 |
| 欧姆龙 | FINS TCP | | | |
| 欧姆龙 | FINS UDP | 欧姆龙 CP1H ✓ | 欧姆龙 CP1H ✓ | |
| 欧姆龙 | NJ/NX CIP(unconnected) | | | BOOL 数组按元素访问(应答类型自描述,回 DWORD 时按 Logix `//32` 回退)与 STRING(`len(u32)+字符`,写入回带模板号)待真机核证 |
| 欧姆龙 | NJ/NX CIP(connected, Forward Open) | | | |
| 罗克韦尔 | EtherNet/IP(unconnected) | | | 0x0A 多服务包自动拆包预算(≤32 条 / ≤480B)待真机核证(connected Large 4002 下 480B 偏保守,仅影响拆包次数) |
| 罗克韦尔 | EtherNet/IP(connected, Forward Open) | | | RPI 默认 100ms(`rpi_us` 可覆盖)、CIP 0x01/0x07 断线重连、Forward Close 应答解析待真机核证 |
| 倍福 | TwinCAT ADS | | | transport 类错误码 0x705/0x706/0x725 分流(断线惰性重连)、`set_timeout` 返回值告警待真机核证 |
| 西门子 | S7-300/1200/1500 | | | STRING/WSTRING 读超长按 `length` 截断、写保留 PLC 侧声明长(超声明长拒绝);优化块访问错误提示、PUT/GET 缺失文本细分待真机核证 |
| 汇川 | H3U/H5U Modbus TCP | 汇川 H5U 系列 ✓ | 汇川 H5U 系列 ✓ | 2026-09-28 真机读写通过(`InovanceTcpClient`,汇川 TCP/502) |
| 汇川 | H3U/H5U Modbus RTU | | | |
| 汇川 | H3U/H5U MC 协议兼容(3E) | | | |
| 松下 | MEWTOCOL TCP/UDP | | | |
| 松下 | MC 协议兼容(3E) | | | |
| 基恩士 | KV Host Link TCP | | | |
| 基恩士 | KV Host Link UDP | | | |
| 基恩士 | KV MC 协议兼容(SLMP 3E) | | | 位组记号核证(2026-09-26 复核后仍待真机,判据已定):本库按**记号数字原样**发帧(`R515` → 515)。核证法:**写 `R100`**,在 KV Studio 同时看 `R100`(组 1 位 0)与 `R604`(组 6 位 4 = 线性 100)——前者变化 = 现状正确;后者变化 = 需换算为 `组×16+位号`(那时改 `_translate_address` 并按行为变更记 CHANGELOG)。依据与反证详见 `plc/keyence/mc.py` 模块 docstring 与 `docs/review.md` §2.7.3 |
| 基恩士 | KV 的 MC 兼容走**三菱**客户端(`MelsecMcTcpClient` / `MelsecMcUdpClient`) | 基恩士 KV 系列 ✓ | —(本次未在 Y 上写) | 2026-09-28 真机:该 KV 的 SLMP 兼容**接受标准三菱记号与软元件码**(至少 D/M/Y 已验),现场三色灯输出点 `Y90` 经 `MelsecMcUdpClient`(3E/UDP)读通。**同一地址**用 `KeyenceMcUdpClient` 时在**组帧期被本库码表拒**(`Y` 不在 `R/B/W/DM/ZR` 五设备内,报 `不支持的 MC 软元件`,一帧未发出)——读 KV 的 X/Y 等设备请走三菱客户端。编号按三菱表为**十六进制**(`Y90` = 0x90 = 第 144 点;若该灯实为第 90 点须写 `Y5A`)。`KeyenceMc*` 的 X/Y 直读支持待手册或真机定论(未入库探针 `tools/_probe_kv_slmp.py`) |
| 基恩士 | SR 扫码枪 | — | — | SR 扫码枪为读码设备,读=扫码触发,写=不适用 |
| 丰田 | TOYOPUC 计算机链接 TCP/UDP | | | |
| Modbus | Modbus TCP | 汇川 H5U 系列 ✓ | 汇川 H5U 系列 ✓ | 2026-09-28 基础读写通过(`ModbusTcpClient`,同机汇川 H5U);FC22 掩码写(字节序可配) / FC23 读写多寄存器 / FC24 FIFO / FC43·14 设备标识待真机核证 |
| Modbus | Modbus RTU | | | FC22 掩码写 / FC23 读写多寄存器 / FC24 FIFO(按 byte count 增量收包) / FC43·14 设备标识(按对象头增量收包) / `inter_frame_delay` 帧间静默待真机核证 |
| OPC-UA | opc.tcp | | | 订阅/Browse 为 v0.35 新增,待真机验证 |
| CNC | MTConnect Agent HTTP/XML | | | |