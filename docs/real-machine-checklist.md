# 真机联测核验清单

在**真实 PLC 设备**上独立确认读/写能力的核验记录。每行一个测试(某协议 + 某操作);
**读取与写入独立勾选**(读到了不代表写对了——尤其 Modbus 等含只读寄存器的协议)。

## 填写约定

| 字段 | 含义 |
|---|---|
| 真机型号 | 品牌 + 系列 + 型号(如"三菱 MELSEC iQ-R R04CPU");仿真环境写"PLCSIM Advanced / HSL / TwinCAT Simulator"等 |
| 固件 | 设备固件版本号 |
| omniplc 版本 | 测试时的 `omniplc.__version__` |
| 日期 | `YYYY-MM-DD` |
| 备注 | 任何异常、限制、注意事项(可用 `!` 标失败) |

**结果标记**(写在"结果"列):
- `✓` 通过
- `✗` 失败(请在备注写原因)
- `~` 部分通过(限定条件,见备注)
- `—` 未测

**单元测试 ≠ 真机联测**:单元测试走黄金报文 + 模拟传输,保证"按手册/参考实现的字节级契约";
真机联测验"现场设备真实行为符合契约"。两者**不可互替**。

## 关联

- 实现完成但缺真机条件/排队中的项:`README.md`「真机联测待做」表
- 协议帧级实现引用:`docs/architecture.md` §10 版本履历表
- 第三方审查参考:`docs/review.md`(v0.32.1 基准)
- 提交新真机记录:PR 模板「自检」勾选"真机联测",并在 `CHANGELOG.md` 加条目

---

## 核验记录表

按厂商 → 协议 → 操作(读取 / 写入)升序排列;同一协议下读取在上、写入在下。

| 厂商 | 协议 | 操作 | 真机型号 | 固件 | omniplc 版本 | 日期 | 结果 | 备注 |
|---|---|---|---|---|---|---|---|---|
| 三菱 | MC 3E | 读取 | | | | | | |
| 三菱 | MC 3E | 写入 | | | | | | |
| 三菱 | MC 4E | 读取 | | | | | | |
| 三菱 | MC 4E | 写入 | | | | | | |
| 三菱 | MC 1E | 读取 | | | | | | |
| 三菱 | MC 1E | 写入 | | | | | | |
| 三菱 | MC 1C(串口) | 读取 | | | | | | |
| 三菱 | MC 1C(串口) | 写入 | | | | | | |
| 三菱 | MC 3C(串口) | 读取 | | | | | | |
| 三菱 | MC 3C(串口) | 写入 | | | | | | |
| 三菱 | MC 4C(串口) | 读取 | | | | | | |
| 三菱 | MC 4C(串口) | 写入 | | | | | | |
| 三菱 | MX Component | 读取 | | | | | | |
| 三菱 | MX Component | 写入 | | | | | | |
| 欧姆龙 | FINS TCP | 读取 | | | | | | |
| 欧姆龙 | FINS TCP | 写入 | | | | | | |
| 欧姆龙 | FINS UDP | 读取 | | | | | | |
| 欧姆龙 | FINS UDP | 写入 | | | | | | |
| 欧姆龙 | NJ/NX CIP(unconnected) | 读取 | | | | | | |
| 欧姆龙 | NJ/NX CIP(unconnected) | 写入 | | | | | | |
| 欧姆龙 | NJ/NX CIP(connected, Forward Open) | 读取 | | | | | | |
| 欧姆龙 | NJ/NX CIP(connected, Forward Open) | 写入 | | | | | | |
| 罗克韦尔 | EtherNet/IP(unconnected) | 读取 | | | | | | |
| 罗克韦尔 | EtherNet/IP(unconnected) | 写入 | | | | | | |
| 罗克韦尔 | EtherNet/IP(connected, Forward Open) | 读取 | | | | | | |
| 罗克韦尔 | EtherNet/IP(connected, Forward Open) | 写入 | | | | | | |
| 倍福 | TwinCAT ADS | 读取 | | | | | | |
| 倍福 | TwinCAT ADS | 写入 | | | | | | |
| 西门子 | S7-300/1200/1500 | 读取 | | | | | | |
| 西门子 | S7-300/1200/1500 | 写入 | | | | | | |
| 汇川 | H3U/H5U Modbus TCP | 读取 | | | | | | |
| 汇川 | H3U/H5U Modbus TCP | 写入 | | | | | | |
| 汇川 | H3U/H5U Modbus RTU | 读取 | | | | | | |
| 汇川 | H3U/H5U Modbus RTU | 写入 | | | | | | |
| 汇川 | H3U/H5U MC 协议兼容(3E) | 读取 | | | | | | |
| 汇川 | H3U/H5U MC 协议兼容(3E) | 写入 | | | | | | |
| 松下 | MEWTOCOL TCP/UDP | 读取 | | | | | | |
| 松下 | MEWTOCOL TCP/UDP | 写入 | | | | | | |
| 松下 | MC 协议兼容(3E) | 读取 | | | | | | |
| 松下 | MC 协议兼容(3E) | 写入 | | | | | | |
| 基恩士 | KV Host Link TCP | 读取 | | | | | | |
| 基恩士 | KV Host Link TCP | 写入 | | | | | | |
| 基恩士 | KV Host Link UDP | 读取 | | | | | | |
| 基恩士 | KV Host Link UDP | 写入 | | | | | | |
| 基恩士 | KV MC 协议兼容(SLMP 3E) | 读取 | | | | | | |
| 基恩士 | KV MC 协议兼容(SLMP 3E) | 写入 | | | | | | |
| 基恩士 | SR 扫码枪 | 扫码 | | | | | | |
| 丰田 | TOYOPUC 计算机链接 TCP/UDP | 读取 | | | | | | |
| 丰田 | TOYOPUC 计算机链接 TCP/UDP | 写入 | | | | | | |
| Modbus | Modbus TCP | 读取 | | | | | | |
| Modbus | Modbus TCP | 写入 | | | | | | |
| Modbus | Modbus RTU | 读取 | | | | | | |
| Modbus | Modbus RTU | 写入 | | | | | | |
| OPC-UA | opc.tcp | 读取 | | | | | | |
| OPC-UA | opc.tcp | 写入 | | | | | | |
| CNC | MTConnect Agent HTTP/XML | 读取 | | | | | | |
| 通用 | OpenTcp(分隔符 / 定长) | 读取 | | | | | | |
| 通用 | OpenTcp(分隔符 / 定长) | 写入 | | | | | | |