# review-1019 性能提升检查报告(只差不做)

> 2026-10-06 · 基线:master `48c1b9b`(v0.55.3 冗余清理批后)
> 范围:全库性能热点扫描(4 只读子代理分区并行 + 主会话逐项亲验),
> **本轮只检查与差距分析,不落地任何修改**;修复排期见 `docs/todo.md`。
> 方法:逐协议/逐层读代码找热点,排除网络往返主导项与已批量化的路径,
> 亲验确认每个报告的 file:line 与改法可行性后入表。

## 一、高收益热点(建议优先)

### P1-1 Modbus 批量写合并 O(n²) 深比较
- **位置**:`plc/modbus/modbus.py:662`(native 镜像 `native/modbus.py:698` 同源)
- **现状**:`chunk_items = [item for item in group if item[1] in chunk_entries]`
  每个 chunk 全量扫描 group,成员测试是 5 字段 NamedTuple 深比较
  (frozen dataclass `ModbusAddress` 逐字段 `__eq__`)。k 个 chunk × n 元素
  → O(n·k);最坏(逐点不连续、每点自成 chunk)O(n²),~1968 点 ≈ 390 万次
  深比较。
- **改法**:利用 `_coalesce_group` 输出恒为排序后 group 的连续切片这一不变量,
  指针按序切 `group[pos:pos+len(chunk)]`;或按 `id(entry)` 建映射一次。
  O(n²)→O(n)。
- **收益**:高(纯 CPU 浪费,大批量写毫秒级→微秒级;空洞越多收益越大)。
- **风险**:中(依赖"连续切片"不变量,需注释 + 测试锁定,行为不变)。

### P1-2 KV Host Link TCP 逐字节收行
- **位置**:`plc/keyence/hostlink.py:126`(`_transact` TCP 分支)
- **现状**:`while` 循环逐字节 `transport.recv(1)` 收响应行,每字节代价 =
  `time.monotonic()` + `receive_timeout` setter 赋值 + 底层 deadline 计算 +
  `sock.settimeout()` + `sock.recv(1)` ≈ 每字节 3~5 次系统调用。40 字符行
  ≈ 80 次系统调用;全库其他驱动(S7/EZSocket)都是头+余量共 2~3 次 recv。
- **改法**:改用 `transport.recv_some(max_bytes)` 块读(单次 `socket.recv`
  收尽已到达字节),从缓冲按 CR/LF 切行;保留超时 deadline、行首分隔符
  丢弃、`KV_MAX_LINE` 上限与超时抛 `socket.timeout` 语义。
- **收益**:高(KV TCP 所有读写的公共路径,每事务 ~N 次系统调用 → 1~3 次)。
- **风险**:中(须保持行语义:跨块 CR/LF 边界、超时内无数据抛错、行长上限
  报错带头部字节转储;`recv_some` 阻塞上限为 socket 超时,对端慢时按
  deadline 循环)。

### P1-3 MX Component 每笔块操作全 MRO 扫描
- **位置**:`plc/melsec/mx.py:121-143`(`_raw_com_method`)
- **现状**:每笔块读/随机读/写(`_com_read_words`/`_com_write_words`/
  `_com_read_random`/`_com_write_random`)都重新 `for klass in type(com).__mro__:
  for key in vars(klass): key.lower().endswith(suffix)` 定位原始 vtable 方法;
  每个 32/64 位条目调一次 `_read_words` → 一次全扫描,120 条 float 批 = 120 次
  扫描,含大量属性名 `.lower()` 字符串操作。
- **改法**:按 `(type(com), method)` 用 `functools.lru_cache` 记忆化解析结果
  (key 含 `type(com)` 天然兼容不同类型库版本)。
- **收益**:中高(块密集批省数毫秒)。
- **风险**:低(纯记忆化,行为不变)。

## 二、中收益热点(批量路径本地 CPU)

| # | 位置 | 现状 | 改法 | 收益 | 风险 |
|---|------|------|------|------|------|
| P2-1 | `core/convert.py:151` `bytes_to_words` | 逐字切片 + `int.from_bytes`(实测 100 字 13.5µs vs `struct.unpack` 0.75µs,**≈18x**) | 一次 `struct.unpack(f"{'<'/'>'}nH")`(偶数长度校验前置;`BytesLike` 含 `Sequence[int]` 时先 `bytes()` 一次) | 中(全库共享层,同步/异步写路径全受益) | 低 |
| P2-2 | `core/convert.py:132-136` `words_to_bytes` | 校验循环 + 生成器对同一 word 两次 `int()` + 每字中间 bytes 对象(实测 100 字 ≈3x) | 校验循环保留(范围错误语义),算好的 number 列表直接 `struct.pack` 一次 | 中(3x,零中间对象) | 低(校验前置则行为逐字节一致) |
| P2-3 | `plc/toyopuc/codec.py:67` `unpack_u16` | 逐字下标+移位+或(512 字 ≈200µs) | 长度偶数校验保留,改一次 `struct.unpack("<nH")` | 中 | 低 |
| P2-4 | `plc/omron/codec.py:326` FINS 响应字数据 | 逐字切片 + `from_bytes`(999 字) | 一次 `struct.unpack(">nH")` | 中 | 低 |
| P2-5 | `plc/omron/codec.py:854-857` FINS 写数据 | 逐字 `to_bytes` + 两遍遍历 | 校验遍保留,打包改一次 `struct.pack(">nH")` | 中 | 低 |
| P2-6 | `plc/panasonic/mewtocol.py:196` | 逐 4 字符切片 + `int(,16)` | `bytes.fromhex(data_text)` 一次 + `struct.unpack(">nH")`(`fromhex` 非十六进制同样抛 `ValueError`,现有错误包装语义不变;空白输入行为需测试确认) | 中 | 中 |
| P2-7 | `plc/siemens/client.py:914-930` S7 `read_range` 解码 | 逐元素切片 + `struct.unpack`/`from_bytes` | 先校验 `len(data) == size*count`,整块一次 `struct.unpack(">{}h"…)` | 中(约 3~5x) | 低 |
| P2-8 | `plc/siemens/address.py:72` `parse_s7_address` | **全库唯一无 `lru_cache` 的地址解析**(KV/MEWTOCOL/OPC-UA 均有,注释明言高频轮询免重复正则) | 加 `@lru_cache(maxsize=ADDRESS_CACHE_MAXSIZE)`(纯函数 + 不可变 NamedTuple 返回,缓存安全) | 中(read_many 20 项每轮省 20 次正则+int) | 低 |
| P2-9 | `plc/panasonic/mewtocol.py:269-284` read_range | 每元素切片 + `words_to_value` 三重遍历(每元素 3~4 次遍历、每字一次 `to_bytes`) | 整块一次解码(SHORT/USHORT 直接取值;INT/UINT/FLOAT 整块拼字节一次 `struct.unpack(">nf")`);注意 `reverse_words`(低字在前)按块整体保持语义 | 中~高 | 中(须逐块语义等价 + 测试) |
| P2-10 | `plc/melsec/codec_qna.py:178/393-397/483-487` + `codec_a.py:187/203` MC 响应/写负载 | 逐元素切片 + `from_bytes` / 逐字 `to_bytes` join | 同 P2-1/2 合并改(只动字分支,位路径半字节打包不动) | 中(960 字批 2~5x) | 低 |

## 三、低收益热点(顺手项)

| # | 位置 | 现状 | 改法 |
|---|------|------|------|
| P3-1 | `plc/modbus/modbus.py:741` | `int_values = [1 if flag else 0 for flag in values]` 冗余(bool 是 int 子类) | 直接传 `values`,删列表分配 |
| P3-2 | 三处 `_write_string`(siemens client.py:730 / mewtocol.py:152 / hostlink.py:197) | 同一字符串两次 `encode`(外层取长度 + `encode_string` 内部再编) | 先 `raw = value.encode(encoding)` 取长度再 ljust 补齐 |
| P3-3 | `plc/melsec/melsec.py:614` | `_decode_32(list(words[i:i+2]))` 的 `list()` 白复制(切片已是新列表) | 删 `list()` |
| P3-4 | `plc/melsec/mx.py:933-934` | read_batch block 路径 plan 存原始地址字符串,operation 每 block 条目重复 `_check_address` + `_device_text` | plan 直接存 `device_text` |
| P3-5 | `core/convert.py:453/467-468` `words_to_value` | 不反转时也整份 `list()` 拷贝 + 内层外层对同一 byteorder 各校验一遍 | 拷贝惰性化(非 reverse 直接用原序列;BOOL 只取 `seq[0]` 无需拷贝);byteorder 结果复用 |
| P3-6 | `core/convert.py:57-71` `crc16` | 逐位 Python 循环 | 查表法(仅 Modbus RTU 串口走线使用,串口吞吐上限 ~11.5KB/s,优先级最低) |
| P3-7 | `plc/omron/codec.py:188-193/253-262` FINS 多区读 | 逐条目三次 `to_bytes` 追加 / 逐条切片+`from_bytes`(上限 167 条) | 一次 `struct.pack(">HB"*n)` / `unpack_from` 免切片 |
| P3-8 | `plc/ab/ab.py:773-780` | read_batch 解码循环内重解析地址字符串(有 lru_cache 仍多一次哈希)+ `parsed.base` 每次 `".".join(members)` 重建 | plan 存 `parsed` 对象;base 构建 plan 时算好存下 |

## 四、确认无问题(排查排除,不列入)

- **无逐点 IO**:各驱动 `read_many`/`read_batch` 均为协议级单/少事务
  (Modbus `_coalesce_and_read` K 笔、MC 0406 单事务、FINS 0104 单事务、
  S7 Read Var 单 PDU);基类逐点版是 MC 1E/写路径等无批量原语协议的
  设计行为。
- **正则全预编译**:各协议 address.py 均模块级编译;KV/MEWTOCOL/OPC-UA
  地址解析已 `lru_cache`(唯一例外 P2-8)。
- **Modbus codec 已批量**:`codec.py:191/276/417` 已单次 `struct.pack/unpack`;
  AB codec 无逐元素问题(各处 count=1)。
- **无 O(n²) 除 P1-1**:批量路径均线性;`FINS_TIMER_COUNTER_AREAS` 等
  小元组 in 查找可忽略;`_merge_bit_blocks`/`_coalesce_group`/FINS
  `_recv_device_id_tail` 已线性/已优化。
- **帧构造**:均 bytearray +=,无 str 拼接热点;`_transact` 的 send+2×recv
  是 TCP 流式协议必需(长度未知须先收头)。
- **FOCAS/EZSocket/OPC-UA 驱动层**:无热路径循环,网络/ctypes 主导;
  EZSocket 逐轴往返(轴数 ≤8)是协议无批量原语的固有开销。

## 五、结论与建议排期

网络往返主导下无单事务量级热点;收益集中在算法级(O(n²))、系统调用级
(逐字节收行)与 COM 调用级(逐块 MRO 扫描),以及大批量路径(500+ 字)的
本地 CPU。推荐顺序:

1. **P1-1**(O(n²) 算法级,纯浪费)→ **P1-2**(系统调用级)→ **P1-3**(记忆化)
2. **P2-1 + P2-2**(convert 共享层一处改全库受益,风险最低)→ P2-3~P2-10
   各协议批量解码(与 P2-1 合并改、两侧一致)
3. **P3 顺手项**随批清理(P3-1/P3-3/P3-4 零风险可随任意批)

> 验证口径:每批改完跑门禁五件套(3.7.9 全量 pytest / ruff format+check /
> mypy / ty);P2-9 等含 `reverse_words` 语义的改动用黄金样本逐块对拍;
> P1-2 收行语义用假 socket 契约测试锁定。
