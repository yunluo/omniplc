# omniplc 架构设计

> 版本:v0.1.0 · 更新日期:2026-09-18 · 状态:骨架已落地,协议编解码待实现

omniplc 是面向多品牌、多协议 PLC 的 Python 统一通信库(Python 3.7+,uv 开发)。
本文档描述 v1.0 的完整架构:分层、类设计、继承树、线程安全模型、类型标注纪律、
地址语法、字序、连接状态机与测试策略。

---

## 1. 总体分层

```
┌──────────────────────────────────────────────────────────────┐
│ 用户 API 层                                                    │
│   协议×走线 具体客户端类(7 个同步 + 7 个异步 A 前缀镜像)          │
│   Tag / TagTable 可选点位表层                                   │
├──────────────────────────────────────────────────────────────┤
│ 驱动层 drivers                                                 │
│   modbus/(codec + address + client)                            │
│   plc/melsec/(codec_qna 3E/4E + codec_a 1E + address + client) │
│   plc/omron/(codec + address + client)                         │
├──────────────────────────────────────────────────────────────┤
│ 传输层 transport(可插拔)                                       │
│   BaseTransport → TcpTransport / UdpTransport / SerialTransport │
├──────────────────────────────────────────────────────────────┤
│ 公共基础层                                                      │
│   core/BaseClient(状态机/锁/重连/重试/类型化方法模板)             │
│   core/errors.py(错误类集中定义)                                  │
│   core/constants.py(全局常量集中定义)                             │
│   types.py(DataType/WordOrder)  convert.py(纯转换函数)          │
└──────────────────────────────────────────────────────────────┘
```

设计原则:

1. **codec 全部是纯函数**(bytes ↔ 结构),不接触 socket——可以用黄金报文样本
   做无硬件测试;
2. **协议层只依赖 `BaseTransport` 抽象**——新增走线不动协议层;
3. **公共逻辑在 `BaseClient` 收口一次**——锁、重连、重试、类型化读写全协议共享;
4. **对外 API 不抛自定义异常**——读返回 `(bool, value)`,写返回 `bool`,
   失败原因进 `last_error`(与 pyhsl 使用习惯一致)。

## 2. 类继承图

```
BaseClient (ABC, 模板方法) ───────────── src/omniplc/core/base_client.py
│   连接状态机 / RLock 事务锁 / 惰性重连 / 重试 / last_error / 上下文管理器
│   read() / write() / read_many() / write_many()
│   read_bool … read_ulong / read_float / read_double / read_string + write_*
│   ——类型化方法在这里"只写一次",委托抽象原语 _read()/_write()
│   read_tag() / write_tag() / bind_tags()
│
├── ModbusBaseClient (ABC) ────────────── src/omniplc/modbus/modbus.py
│   │   _read/_write 落到位/寄存器原语;字序(ABCD/CDAB/BADC/DCBA)处理;
│   │   int16…float64 编解码与范围校验;station/word_order 属性
│   ├── ModbusTcpClient   → MBAP 帧      + TcpTransport(502)
│   └── ModbusRtuClient   → 站号+PDU+CRC16 + SerialTransport(configure_serial)
│
├── _MelsecMcBase (ABC, 私有) ─────────── src/omniplc/plc/melsec/melsec.py
│   │   帧型校验(3E/4E/1E)、软元件地址分发
│   ├── MelsecMcTcpClient → MC 3E/4E/1E 帧 + TcpTransport(2000)
│   └── MelsecMcUdpClient → 同帧型 over UDP + UdpTransport(2000)
│
├── MelsecMxClient ────────────────────── src/omniplc/plc/melsec/mx.py
│   │   三菱 MX Component(Windows,comtypes):ActUtlType 按逻辑站号,
│   │   _MxComLink 把 COM 会话(Open/Close)适配为传输对象外形
│   └── (无字节流收发;GetDevice/SetDevice/ReadDeviceBlock/WriteDeviceBlock)
│
└── _OmronFinsBase (ABC, 私有) ────────── src/omniplc/plc/omron/omron.py
    │   FINS 节点地址、软元件地址分发
    ├── OmronFinsTcpClient → FINS 帧+TCP 握手(_after_connect 钩子) + TcpTransport(9600)
    └── OmronFinsUdpClient → FINS 帧无握手 + UdpTransport(9600)

└── _KeyenceHostLinkBase (ABC, 私有) ──── src/omniplc/plc/keyence/hostlink.py
    │   ASCII 行式协议(RD/RDS/WR/WRS + CR 结束,响应行 CR/LF)
    ├── KeyenceHostLinkTcpClient → TcpTransport(8000,按行逐字节收包)
    └── KeyenceHostLinkUdpClient → UdpTransport(8000,一问一答一数据报)

└── _ToyopucBase (ABC, 私有) ──────────── src/omniplc/plc/toyopuc/toyopuc.py
    │   TOYOPUC 计算机链接二进制帧(00 00 LL LH CMD / 80 RC LL LH CMD);
    │   TCP 按帧头长度分段收包,UDP 一问一答一数据报
    ├── ToyopucTcpClient → TcpTransport(1025)
    └── ToyopucUdpClient → UdpTransport(1025,一问一答一数据报)

└── OpcUaClient ───────────────────────── src/omniplc/opcua/client.py
    │   OPC-UA opc.tcp 会话(封装 asyncua 1.1.5,官方继任 python-opcua);
    │   _OpcUaSession 把会话适配为传输外形,asyncua 异常在会话边界统一翻译
    └── (无字节流收发;get_node().read_value()/write_value() 按 VariantType 编解码)

KeyenceSrClient ─────────────────────── src/omniplc/scanner/keyence_sr.py
    基恩士 SR 扫码枪(TCP 9004,LON → 窗口 → LOFF → 读应答;
    scan() 返回 (是否读到, 条码文本);不实现 _read/_write 数据原语)

BaseTransport (ABC) ───────────────────── src/omniplc/transport/
├── TcpTransport      TCP_NODELAY,recv 精确凑齐 size 字节(流式粘包处理)
├── UdpTransport      已连接 UDP,recv 一次返回一条数据报
└── SerialTransport   pyserial(延迟导入),8N1 可配,SerialConfig 校验

Tag (dataclass) / TagTable (Mapping) ──── src/omniplc/tag.py(from_json/from_csv)

异步镜像(omniplc/aio/,类名 = 同步类名前加 A):
ABaseClient ── 组合同步实例 + 单线程 ThreadPoolExecutor,方法签名同名同型
├── AModbusBaseClient → AModbusTcpClient / AModbusRtuClient
├── AMelsecMcTcpClient / AMelsecMcUdpClient / AMelsecMxClient
├── AOmronFinsTcpClient / AOmronFinsUdpClient
├── AKeyenceHostLinkTcpClient / AKeyenceHostLinkUdpClient
├── AToyopucTcpClient / AToyopucUdpClient
├── AOpcUaClient
└── AKeyenceSrClient
```

v1 共 **13 个同步具体类 + 13 个异步镜像类**,三菱三帧型(3E/4E/1E)× 两走线(TCP/UDP)
另加 MX Component(Windows/COM,单线程 executor 天然满足 ActUtlType 的 STA 模型)。

### 2.1 继承设计要点(模板方法模式)

- `BaseClient` 定义抽象原语:**`_create_transport()` / `_read()` / `_write()`**,
  外加可选钩子 `_after_connect()`(FINS/TCP 握手)、`_read_string/_write_string`。
- 类型化方法 `read_float(addr)` 的实现只有一份:
  `read_float → read(addr, FLOAT) → _execute(锁内) → _read(addr, FLOAT)(驱动)`;
  新增协议只需实现 2 个原语,自动获得全部 20+ 个类型化方法。
- `ModbusBaseClient` 中间层封装"寄存器级"公共性(类型分发、字序、范围校验),
  三个走线子类只实现 `_transact()`(MBAP vs RTU 帧装拆)与 `_create_transport()`。
- 三菱 MC 的 TCP/UDP 子类完全共享帧编解码(帧内无走线信息);
  帧层按 **QnA 兼容(3E/4E,`codec_qna.py`)** 与 **A 兼容(1E,`codec_a.py`)**
  拆两个模块——1E 帧无网络号/PC 号前缀字段、软元件码表不同。
- 异步侧是**组合 + 镜像**:每个 `A*Client` 持有对应同步实例,方法签名与同步版
  完全一致(返回可 await),协议逻辑只有一份。

## 3. 线程安全设计

1. **单锁模型**:每个客户端实例一把 `threading.RLock`,保护三样东西:
   连接状态、请求/响应事务、`last_error`。`connected` / `last_error` 读到的是加锁快照。
2. **事务粒度持锁**:一次读/写在锁内完成全部步骤(必要时惰性重连 → 组帧 → send →
   recv → 校验 → 解码),保证请求/响应帧不被其他线程交错(interleaving)。
3. **文档化取舍**:持锁做网络 IO,意味着同一客户端的并发调用是**串行化**的——
   这保证正确性而非并行吞吐;需要高并发时使用多个客户端实例(连接池留 v1.x)。
4. **重试语义在锁内**:读失败按 `retries` 重试;写默认 `write_retries=0`
   (防止重复写入危险动作),可显式开启。重试与**惰性重连**配合:
   传输失败标记断开,重试前自动重建连接。
5. **PLC 明确报错(`DeviceError`)不断线、不重试**——链路是好的,只有传输层
   故障(OSError / 坏帧)才标记断开。
6. **Transport 自身非线程安全**,只由持有事务锁的客户端串行访问;
   异步侧所有调用经**单线程 executor** 串行执行,与同步侧同一把 RLock,双保险且保序。

## 4. 连接状态机与惰性重连

```
                 connect() 成功                    send/recv 失败
   [已断开] ─────────────────→ [已连接] ───┐
      ↑  │                              │ 读写/写失败(OSError/坏帧)
      │  │ connect() 失败                ↓
      │  └── 记录 last_error      _mark_disconnected()
      │      返回 False            静默 close transport
      │                                 │
      │        下一次 read/write 调用    ←(惰性:没有后台线程)
      └─────────────────────────────────┘
            锁内自动 connect() → 成功则重发,失败返回 (False, None)
```

- `connect()` 幂等:已连接直接返回 True;`disconnect()` 幂等。
- `with client:` 进入时连接,失败抛 `ConnectionError`(与 pyhsl 一致);
  `async with AClient(...):` 同语义。
- `_after_connect()` 钩子:连接建立后执行协议级初始化(FINS/TCP 节点分配握手)。

## 5. 错误处理约定

公共 API **不抛自定义异常**(pyhsl 风格):

| 操作 | 成功 | 失败 |
|---|---|---|
| `read_*` / `read` / `read_tag` | `(True, 值)` | `(False, None)` |
| `write_*` / `write` / `write_tag` | `True` | `False` |
| `read_many` / `write_many` | 逐点独立容错的结果列表 | 单点失败不影响其他点 |
| `connect` / `disconnect` | `True` | `False` |

- 失败原因一律记录在 `last_error` 属性(含 PLC 原始错误码,如 Modbus 异常码、
  MC 结束码、FINS 结束码);成功读写后清空。
- **参数校验错误**(非法地址、未知类型、范围越界、未绑定点位名)直接抛
  `ValueError`——这是调用方编码错误,静默吞掉反而有害。
- 内部异常(`omniplc.core.errors`,**错误类统一在 core 层定义**):
  `TransportClosedError` /
  `ProtocolFrameError` / `DeviceError(code)`,只用于库内控制流,
  由 `_execute()` 统一转换为元组语义,不逃逸到调用方。

## 6. 数据类型与类型标注

### 6.1 类型系统(`types.py`)

`DataType` 枚举:名称与 pyhsl 的 `read_*`/`write_*` 后缀一一对应——
`bool / short / ushort / int / uint / long / ulong / float / double / string`
(有符号整型依次为 16/32/64 位;float=float32;double=float64)。

字序 `WordOrder`:`ABCD`(大端默认)/ `CDAB`(字交换,现场最常见)/
`BADC`(字节交换)/ `DCBA`(小端)。各协议默认值与覆盖方式:

| 协议 | 字序 | 说明 |
|---|---|---|
| Modbus | 默认 ABCD,`word_order` 属性可配 | 现场设备常为 CDAB,读写共用同一配置 |
| 三菱 MC | 固定小端字序(低字在前) | 编码层处理,不暴露配置 |
| 欧姆龙 FINS | 固定大端 | 编码层处理,不暴露配置 |

`convert.py` 提供全部转换纯函数:`crc16 / lrc / get_bit / set_bit`、
`bytes ↔ int16/uint16`、`registers ↔ int32/uint32/float32/float64(四字序)`、
`encode_string / decode_string`。字序变换为对合变换,编解码共用一套实现。

### 6.1.1 字符串参数枚举化(类型检查与 IDE 补全)

凡取值封闭的参数一律用**枚举**定义,字符串仅作兼容输入:

| 参数 | 枚举类型 | 取值 |
|---|---|---|
| `read/write(data_type)` | `omniplc.types.DataType` | `DataType.FLOAT`、`DataType.SHORT`… |
| Modbus 区域(`ModbusAddress.area`) | `omniplc.modbus.ModbusArea` | `COIL / DISCRETE_INPUT / HOLDING_REGISTER / INPUT_REGISTER` |
| Modbus 字序(`word_order`) | `omniplc.types.WordOrder` | `ABCD / CDAB / BADC / DCBA` |
| 字节序(`byteorder`) | `omniplc.types.ByteOrder` | `BIG / LITTLE` |
| 三菱 MC 帧型(`frame`) | `omniplc.types.McFrame` | `FRAME_3E / FRAME_4E / FRAME_1E` |
| 串口校验位(`parity`) | `omniplc.types.SerialParity` | `NONE / EVEN / ODD` |

约定:枚举成员为**权威定义**;所有公开参数标注为 `Union[枚举, str]`,
内部经统一的 `coerce` 辅助函数解析,非法值抛 `ValueError`。
开放集合(如 MC 软元件记号 `D/M/X…`、FINS 存储区)仍用 `str`。

### 6.2 类型标注纪律(100% PEP 484)

- 所有类/方法/函数的参数与返回值**全部显式标注**;
- 文件头统一 `from __future__ import annotations`,3.7 运行期只用
  `typing.Tuple/List/Optional/Union/Sequence`;
- 返回类型精确到方法(`read_short → Tuple[bool, Optional[int]]`),
  内部用 `_narrow_int/_narrow_float` 收窄,不用 `Any` 敷衍;
- 上下文管理器用 `TypeVar(_C, bound="BaseClient")` 保持 self 类型;
- 发布 **py.typed**(PEP 561),下游项目可直接获得类型检查;
- CI 固定跑 `mypy --python-version 3.7`(配置见 `pyproject.toml`)与 `ruff`
  (`target-version = "py37"`)双静态检查,语法越界在 CI 就被拦下;
- 追加 **ty**(Astral)作为第二类型检查器(`uvx ty check`,配置见
  `pyproject.toml` 的 `[tool.ty]`),双检查器交叉验证;
  不使用 `# type: ignore[...]` 工具特定抑制码,可空传输引用一律用
  局部变量 + 断言收窄(两种检查器通用)。

### 6.3 核心接口签名(完整版见源码 docstring)

```python
class BaseClient(ABC):
    def connect(self) -> bool: ...
    def disconnect(self) -> bool: ...
    @property
    def connected(self) -> bool: ...
    @property
    def last_error(self) -> Optional[str]: ...
    @property
    def connect_timeout(self) -> float: ...          # setter 校验 > 0,即时生效
    @property
    def receive_timeout(self) -> float: ...
    @property
    def retries(self) -> int: ...                    # 读重试次数
    @property
    def write_retries(self) -> int: ...              # 写重试次数,默认 0

    def read(self, address: str, data_type: str) -> Tuple[bool, Optional[PrimitiveValue]]: ...
    def write(self, address: str, data_type: str, value: PrimitiveValue) -> bool: ...
    def read_many(self, addresses: Sequence[str], data_type: str
                  ) -> List[Tuple[bool, Optional[PrimitiveValue]]]: ...
    def write_many(self, items: Sequence[Tuple[str, str, PrimitiveValue]]) -> List[bool]: ...

    def read_bool(self, address: str) -> Tuple[bool, Optional[bool]]: ...
    def read_short(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_ushort(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_int(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_uint(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_long(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_ulong(self, address: str) -> Tuple[bool, Optional[int]]: ...
    def read_float(self, address: str) -> Tuple[bool, Optional[float]]: ...
    def read_double(self, address: str) -> Tuple[bool, Optional[float]]: ...
    def read_string(self, address: str, length: int = 32,
                    encoding: str = "ascii") -> Tuple[bool, Optional[str]]: ...
    # write_bool(address, value: bool) → bool,write_short(address, value: int) → bool,
    # …write_double(address, value: float),write_string(address, value: str, encoding)

    def bind_tags(self, table: TagTable) -> None: ...
    def read_tag(self, tag: Union[str, Tag]) -> Tuple[bool, Optional[PrimitiveValue]]: ...
    def write_tag(self, tag: Union[str, Tag], value: PrimitiveValue) -> bool: ...

    def __enter__(self: _C) -> _C: ...               # 失败抛 ConnectionError
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...

    # 以下为驱动子类协议原语
    @abstractmethod
    def _create_transport(self) -> BaseTransport: ...
    @abstractmethod
    def _read(self, address: str, data_type: DataType) -> PrimitiveValue: ...
    @abstractmethod
    def _write(self, address: str, data_type: DataType, value: PrimitiveValue) -> None: ...
    def _after_connect(self) -> None: ...            # 可选钩子(FINS/TCP 握手)
```

## 7. 地址语法

| 协议 | 语法示例 | 说明 |
|---|---|---|
| Modbus | `hr0` / `c7` / `di10` / `ir3` / `hr0.15` / `40001` | 前缀语法为主;兼容 Modicon 1 基风格(自动转 0 基);位号 0~15;已实现(`modbus/address.py`) |
| 三菱 MC | `D100` / `M10` / `X1F` / `Y40` / `W100` / `R100` / `Z0` / `ZR100` / `D100.3` | 已实现(`plc/melsec/`);编号进制按码表:X/Y/W/B 十六进制、其余十进制(Q/L/R 口径,SH-080956;1E 帧下 X/Y 八进制),地址解析保留数字原文 |
| 欧姆龙 FINS | `D100` / `CIO0` / `CIO0.5` / `W10` / `H20` / `A0` / `E0_100` | 已实现(`plc/omron/`);存储区码随帧 codec 实现,EM 区 bank 用下划线 |
| 丰田 TOYOPUC | `D0100` / `D0100L` / `D0100H` / `M0201` / `M0201W` / `X0010H` | 已实现(`plc/toyopuc/`);编号一律十六进制(手册口径);字区 S/N/R/D/B,位区 P/K/V/T/C/L/X/Y/M;L/H=低/高字节(字节访问),W=位软元件打包字 |
| OPC-UA | `ns=2;s=Device.Tag` / `ns=4;i=100` / `i=2258` / `b=AAECAw==` / `g=…` | 已实现(`opcua/`);标准 NodeId 字符串,ns 省略默认 0;前缀大小写规范化,标识符值保留原文(`opcua/address.py`) |

解析失败统一抛 `ValueError`(参数错误约定)。

## 8. v1 协议 × 走线矩阵与实现选型

| 协议 | TCP | UDP | RTU(串口) | MX Component |
|---|---|---|---|---|
| Modbus(FC 01/02/03/04/05/06/0F/10) | ✅ `ModbusTcpClient` | — | ✅ `ModbusRtuClient` | — |
| 三菱 MC 3E/4E(QnA 兼容) | ✅ `MelsecMcTcpClient(frame="3E"/"4E")` | ✅ `MelsecMcUdpClient` | v1.x(2C/3C/4C 帧) | ✅ `MelsecMxClient` |
| 三菱 MC 1E(A 兼容,A 系列) | ✅ `frame="1E"` | ✅ | v1.x | ✅ |
| 欧姆龙 FINS | ✅ `OmronFinsTcpClient`(含握手) | ✅ `OmronFinsUdpClient` | v1.x(Host Link) | — |
| 基恩士 KV Host Link | ✅ `KeyenceHostLinkTcpClient` | ✅ `KeyenceHostLinkUdpClient` | — | — |
| 基恩士 SR 扫码枪 | ✅ `KeyenceSrClient`(9004) | — | — | — |
| 丰田 TOYOPUC 计算机链接 | ✅ `ToyopucTcpClient` | ✅ `ToyopucUdpClient` | — | — |
| OPC-UA(opc.tcp) | ✅ `OpcUaClient`(封装 asyncua) | — | — | — |

MX Component 说明:``MelsecMxClient`` 经三菱 MX Component 的 ``ActUtlType``
COM 控件(实用程序设置型)通信,通信参数在**通信设置实用程序**中配置为逻辑站号
(0~1023),Python 侧经 comtypes 调用(``pip install omniplc[mx]``)。地址语法与
MC 驱动一致(如 ``D100``/``M10``),软元件可用范围与进制由配置的 CPU 决定,
编号原文直接透传给控件;错误以十六进制出错代码记入 ``last_error``(手册第 7 章)。
COM 调用全部在客户端事务锁内串行;异步镜像经单工作线程执行,天然满足 STA。

KV Host Link 说明:``KeyenceHostLinkTcpClient/UdpClient`` 使用 ASCII 行式命令
(RD/RDS/WR/WRS,CR 结束;响应行以 CR/LF 结束,出错应答 ``E0``~``E9`` 记入
``last_error``)。地址语法:位软元件 ``R515``(位组:组号+两位位号)/``B1F``、
``W100``(十六进制)/``X0F``(组号十进制+位一位十六进制)/``M100``;字软元件
``DM100`` 等,16/32 位整型经 ``.S/.U/.L/.D`` 后缀由 PLC 原生解析,float32 为
连续两字小端拼接,64 位整型/浮点为连续四/八字小端拼接;字软元件位访问
(``DM100.5``,omniplc 约定十进制位号)走读-改-写。

TOYOPUC 计算机链接说明:``ToyopucTcpClient/UdpClient`` 使用二进制帧
(命令 ``00 00 LL LH CMD [数据]``,响应 ``80 RC LL LH CMD [数据]``,
帧长 = CMD + 数据字节数,小端)。地址语法:字软元件 ``D0100``(S/N/R/D/B,
编号十六进制)→ 字访问(CMD=1C/1D);``D0100L``/``D0100H`` 低/高字节 →
字节访问(CMD=1E/1F,字符串默认从低字节起);位软元件 ``M0201``
(P/K/V/T/C/L/X/Y/M)→ 位访问(CMD=20/21);``M0201W`` 打包字 → 字访问。
软元件基地址(字/字节/位三套)与位编号段范围照手册实现(L/M 有 0x1000
起的第二段)。多字数据小端、低字在前(32/64 位类型与 float32/64 一致);
出错响应 ``RC=10`` 的详细出错代码(0x40 地址越界等)记入 ``last_error``。
v0.7 覆盖基础软元件区;扩展区(CMD=94~99)、PC10(CMD=C2~C6)、
中继(CMD=60)与时钟/CPU 状态(CMD=32/A0)留 v1.x。

OPC-UA 说明:``OpcUaClient`` **不自研协议**(OPC-UA 是完整规范栈:
二进制编码/会话/订阅/X.509 安全栈,自研数月且加密做错即安全事故),
封装官方继任库 **asyncua**(``pip install omniplc[opcua]``)。
版本钉 ``1.1.5``——官方弃用的 python-opcua(2022-07)之外唯一生态,
且为最后支持 Python 3.7 的版本(其后版本 requires_python ≥3.8)。
``_OpcUaSession`` 把 asyncua 同步会话适配为传输对象外形
(connect 建 opc.tcp 会话 / close 断开并回收其后台事件循环线程,
无字节流收发),DataType ↔ ``ua.VariantType`` 显式映射,读写走
服务端原生编解码;``UaError``(Bad 状态码)翻译为 ``DeviceError``
不断线,连接故障标记断开惰性重连。v0.8 为匿名/NoSecurity 连接下的
节点读写;安全策略配置、订阅/浏览(不符合本库拉模式)留 v1.x。

实现选型:MC、FINS 无支持 Python 3.7 的成熟维护库 → 自研;
pymodbus 2.5.3 已停止维护且 3.x 不支持 3.7 → **Modbus 也自研**
(报文简单,超时/重连/错误语义与另两家完全统一;如遇特殊需求,
`ModbusBaseClient` 层也允许替换为 pymodbus 适配,对外 API 不变)。

### 8.1 协议实现参考资料(C# HslCommunication)

帧格式实现对照本地 C# 源码 `D:\DOWNLOAD\Hsl7.0.1\新建文件夹\
HslCommunication_7.0.1_Vs2019\...\HslCommunication_Net45\`
(**只参考组帧/解析逻辑与默认值,不移植其 API;语言与架构保持 Python 原生**):

| 本库模块 | C# 参考文件 |
|---|---|
| Modbus 编解码 | `ModBus/ModbusInfo.cs`(功能码/异常码/MBAP 组帧)、`Core/IMessage/ModbusTcpMessage.cs`(事务号/协议号校验、按长收包) |
| Modbus TCP/RTU 客户端 | `ModBus/ModbusTcp/ModbusTcpNet.cs`、`ModBus/ModbusRtu/ModbusRtu.cs`(CRC16 校验) |
| 三菱 MC(3E/4E/1E,已实现) | `Profinet/Melsec/MelsecMcNet.cs`(3E)、`MelsecMcAsciiNet.cs`(ASCII 帧,v1.x)、`MelsecA1ENet.cs`(1E)、`MelsecMcDataType.cs` / `MelsecA1EDataType.cs`(软元件码表)、`MelsecHelper.cs`(核心命令构造) |
| 三菱 MX Component(已实现) | `docs/MX Component Version 4编程手册.pdf`(ActUtlType 逻辑站号、Open/Close/GetDevice/SetDevice/ReadDeviceBlock/WriteDeviceBlock 数据布局、第 7 章出错代码) |
| 基恩士 KV Host Link(已实现) | 本地 Python 参考库 `D:\DOWNLOAD\plc-comm-hostlink-python-main\src\hostlink\`(RD/RDS/WR/WRS 命令、.U/.S/.D/.L/.H 数据格式、E0~E6 出错代码、float32 两字小端、位组/X-Y 编号规则) |
| 基恩士 SR 扫码枪(已实现) | 本地 Python 参考库 `D:\DOWNLOAD\vention_barcode_scanner-0.8.3.tar\...\scanners\keyence.py`(TCP 9004、LON/LOFF 时序——应答在 LOFF 之后才发送、bank 0~15、BCLR/RESET、ERROR/OK 应答) |
| 丰田 TOYOPUC 计算机链接(已实现) | 本地 Python 参考库 `D:\DOWNLOAD\plc_comm_toyopuc-4.2.0.tar\...\toyopuc\`(**只学习协议本身**:帧格式 `00 00 LL LH CMD`/`80 RC LL LH CMD`、CMD=1C~21 基础区字/字节/位命令、软元件字/字节/位基地址与编号段、RC=10 出错码表、低字在前多字节序;**架构不参考**,仍用本库 BaseClient/Transport 模式) |
| OPC-UA(已实现) | `asyncua==1.1.5` 安装源码(`.venv\...\asyncua\`,sync.Client 会话/`ua.VariantType` 类型表/`ua.uaerrors` 异常层次);python-opcua 已弃用仅作背景,不作为依赖 |
| 欧姆龙 FINS(TCP/UDP,已实现) | `Profinet/Omron/OmronFinsNet.cs`、`OmronFinsUdp.cs`、`OmronFinsNetHelper.cs`(帧组装/解析)、`OmronFinsDataType.cs`(存储区码)、`Core/IMessage/FinsMessage.cs`(TCP 握手/帧长) |

三菱帧实现另对照本地 Python SLMP 参考库
`D:\DOWNLOAD\plc-comm-slmp-python-main\slmp\`(SH-080956 口径,pcap 验证):
**4E 帧带序列号**(请求副头部恒 `54 00`、响应 `D4 00`,与 Hsl 的 0x58 说法不同,
以 SLMP 库为准)、软元件编号进制(X/Y/W/B 十六进制、ZR 十进制)、
应答数据长字段校验均与该库一致。

## 9. 测试策略

分层推进,CI 全部无硬件可跑;**不内置 PLC 模拟器**,协议联调使用
用户自有的模拟器工具:

1. **纯函数单测**(已就位):`convert` / 地址解析 / `SerialConfig` 校验。
2. **传输层测试**(已就位):本机回环 TCP/UDP echo 服务验证字节精确往返、
   粘包凑齐、超时校验、拒绝连接。
3. **黄金报文样本**(已就位,`tests/golden/`,格式见其 README):
   独立实现生成的标准帧 JSON,编解码双向断言,含异常码路径;
   MC/FINS 阶段按同格式补充,真机/模拟器抓包可持续入库。
4. **脚本化传输链路测试**(已就位):假传输按脚本应答,验证各走线的
   组帧、按长收包、事务号/站号/CRC 校验、坏帧断线重连、异常码不断线、
   寄存器位"读-改-写"。
5. **外部联调(本地,不进 CI)**:用户自有模拟器工具;可选专用软件——
   Modbus:`Modbus Slave`、`diagslave`、`ModRSSim2`、`OpenPLC`;
   三菱:GX Works + GX Simulator3(面向 GX Works 内部仿真,对外以太网
   MC 联调依版本/SLMP 配置);欧姆龙:CX-Simulator(接受外部 FINS 命令,
   基本仅 UDP/9600,FINS/TCP 建议真机验证)。
6. **真机手动验证清单**:发版前用真实 PLC 过一遍(不进 CI)。

## 10. Python 3.7 兼容纪律

- 运行期注解:`typing.Optional/Union/...` + `from __future__ import annotations`;
- 不用 walrus(3.8)、`X | Y` 类型(3.10)、`asyncio.to_thread`(3.9)、
  `str.removeprefix`(3.9);异步用 `loop.run_in_executor` + 单线程池;
- **常量集中管理**:所有默认端口/超时/站号边界/报文常量统一定义在
  `core/constants.py`(大写下划线命名,运行期只读),业务代码禁止内联
  魔法数字;
- **值对象统一用 dataclass**(3.7 原生支持):不可变值对象用
  `@dataclass(frozen=True)`(如 `ModbusAddress`),可变配置用
  `@dataclass`(如 `Tag`、`SerialConfig`),结构固定的多返回值用
  `NamedTuple`(如 `McAddress`、`FinsAddress`);禁止手写
  `__eq__/__hash__/__repr__` 样板;
- 开发环境 `.python-version=3.8`(与 3.7 同代,可运行 mypy 1.4.1),
  CI 静态检查钉住 py37;发布前用本机 Python 3.7.9 做导入验证。

## 11. 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 本次 | 架构文档 + 项目骨架 + 公共层/传输层完整实现 + 测试基座 | ✅ 完成 |
| v0.2 | Modbus TCP/RTU 编解码 + 黄金样本 + 脚本化链路测试 | ✅ 完成 |
| v0.3 | 三菱 MC 3E/4E/1E(TCP/UDP)+ 欧姆龙 FINS TCP/UDP(握手/节点分配)+ 黄金样本 | ✅ 完成 |
| v0.4 | 三菱 MX Component(comtypes,逻辑站号)+ Modbus UDP 移除 + MC float 解码修正 | ✅ 完成 |
| v0.5 | 基恩士 KV Host Link(TCP/UDP,RD/RDS/WR/WRS,位组/十六进制地址)| ✅ 完成 |
| v0.6 | 基恩士 SR 扫码枪(TCP 9004,LON/LOFF 触发扫码,bank 预设)| ✅ 完成 |
| v0.7 | 丰田 TOYOPUC 计算机链接(TCP/UDP,基础区字/字节/位访问,CMD=1C~21)| ✅ 完成 |
| v0.8 | OPC-UA opc.tcp 会话(封装 asyncua==1.1.5,NodeId 读写,会话适配)| ✅ 完成 |
| 之后 | Tag 完善 + 示例 → v1.0 | 待开工 |
| v1.x | MC 串口帧(2C/3C/4C)、FINS Host Link、TOYOPUC 扩展区/PC10/中继/时钟、OPC-UA 安全策略/订阅、心跳保活、轮询器、连接池 | 规划 |
| v2 | 西门子 S7(drivers 插槽已预留,沿用 BaseClient 原语模式) | 规划 |
