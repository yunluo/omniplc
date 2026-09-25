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

- 实现完成但缺真机条件/排队中的项:`README.md`「真机联测待做」表
- 协议帧级实现引用:`docs/architecture.md` §11 路线图与版本履历表
- 第三方审查参考:`docs/review.md`(v0.36.0 基准,含 v0.35.0 基线与 v0.36.0 增量复审)
- 提交新真机记录:PR 模板「测试」节勾选"真机联测",并在 `CHANGELOG.md` 加条目

---

## 核验记录表

按厂商 → 协议 升序排列;读/写独立单元格。

| 厂商 | 协议 | 读取 | 写入 | 备注 |
|---|---|---|---|---|
| 三菱 | MC 3E | | | |
| 三菱 | MC 4E | | | |
| 三菱 | MC 1E | | | |
| 三菱 | MC 1C(串口) | | | |
| 三菱 | MC 3C(串口) | | | |
| 三菱 | MC 4C(串口) | | | |
| 三菱 | MX Component | 三菱 FX3U ✓ | 三菱 FX3U ✓ | get_error_message(ActSupportMsg) 待核证 |
| 欧姆龙 | FINS TCP | | | |
| 欧姆龙 | FINS UDP | 欧姆龙 CP1H ✓ | 欧姆龙 CP1H ✓ | |
| 欧姆龙 | NJ/NX CIP(unconnected) | | | |
| 欧姆龙 | NJ/NX CIP(connected, Forward Open) | | | |
| 罗克韦尔 | EtherNet/IP(unconnected) | | | |
| 罗克韦尔 | EtherNet/IP(connected, Forward Open) | | | |
| 倍福 | TwinCAT ADS | | | |
| 西门子 | S7-300/1200/1500 | | | |
| 汇川 | H3U/H5U Modbus TCP | | | |
| 汇川 | H3U/H5U Modbus RTU | | | |
| 汇川 | H3U/H5U MC 协议兼容(3E) | | | |
| 松下 | MEWTOCOL TCP/UDP | | | |
| 松下 | MC 协议兼容(3E) | | | |
| 基恩士 | KV Host Link TCP | | | |
| 基恩士 | KV Host Link UDP | | | |
| 基恩士 | KV MC 协议兼容(SLMP 3E) | | | |
| 基恩士 | SR 扫码枪 | — | — | SR 扫码枪为读码设备,读=扫码触发,写=不适用 |
| 丰田 | TOYOPUC 计算机链接 TCP/UDP | | | |
| Modbus | Modbus TCP | | | FC22 掩码写 / FC23 读写多寄存器 / FC43·14 设备标识待真机核证 |
| Modbus | Modbus RTU | | | FC22 掩码写 / FC23 读写多寄存器 / FC43·14 设备标识(RTU 按对象头增量收包)待真机核证 |
| OPC-UA | opc.tcp | | | 订阅/Browse 为 v0.35 新增,待真机验证 |
| CNC | MTConnect Agent HTTP/XML | | | |
| 通用 | OpenTcp(分隔符 / 定长) | | | |