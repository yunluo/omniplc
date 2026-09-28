# 全项目第五轮纯代码审查(review-0928)

> 日期:2026-09-28 · 触发:*重新整个项目不看文档完整审查一遍代码实现,包含各个协议的实现细节,多子代理审查*
> **修复进度(2026-09-28)**:P0 复审裁决后 **8 → 6 条**(2 条误报撤销,见下),其中 **5 条已修复**(OPC-UA×2、MC native×2+1、native 关闸×1;AB 0x29/0x2A 计 1 条)。**P1 13 项已全部修复**(1 项随 P0 批落地,2 项以文档披露口径修复)。门禁:3.7.9 **1358 passed** / ruff / mypy(74) / ty 全零。
>
> **路径提示(v0.46.0 起)**:本文内 `types.py` / `tag.py` / `convert.py` 的引用对应现
> `src/omniplc/core/` 下同名文件(三文件由根包迁入 `core/`,见 CHANGELOG v0.46.0)。
>
> **P0 复审裁决(关键)**:
> - **FINS 0104"缺存储区计数字段"→ 误报,撤销**。W342 §5-3-5(PDF 198-200 页,印刷 177-179)命令格式为逐条 `[区码+起始地址]` 直接拼接,**无前导计数**;手册明言 "If nothing is specified after the command code, a normal response will be returned"(命令码后可不接数据),与"有 2 字节计数字段"直接矛盾;167 出自注释里的网络上限表(Controller Link/Ethernet 167、SYSMAC LINK/DeviceNet 89),非计数字段域宽。
> - **AB Large Forward Open"参数域应 <<7"→ 误报,撤销**。pylogix 1.1.6 `lgx_comm.py:503`(`0x4200 << 16 += size`)与 pycomm3 1.2.16 `cip_driver.py:354-357`(`init_net_params=0b0100_0010_0000_0000` 后 `size | params << 16`,注 CIP Vol 1 §3-5.5.1.1)均与本项目 `0x4200 << 16 + size` **逐字节一致**;两个真机验证过的参考实现同型,子代理的 `<<7` 推导不成立(登记 P3·待核:仅当真机抓包证伪再议)。

> 范围:审查启动时点 HEAD=`97b1950` 的全部源码(76 文件 / ~24k 行);**不看项目文档与既有台账,纯代码逐行审查**
> 方法:10 个并行子代理按域审查(①core ②transport+OpenTcp ③Modbus ④三菱 MC ⑤欧姆龙+基恩士 ⑥AB+Beckhoff ⑦西门子+松下+丰田 ⑧OPC-UA+MTConnect ⑨汇川+aio ⑩native 机制+测试基建),
> 关键结论经抽检复核(P0 全部人工重验行号与代码);部分子代理用本地 snap7/asyncua 源码、黄金向量、参考实现双向核证。
> 分档:**P0** 错动作/数据损坏/功能失效 · **P1** 静默错误/丢数据 · **P2** 资源/能力缺口/口径分裂 · **P3** 可控隐患/文档失真
>
> **审查中途状态变更**:2026-09-27 21:58 用户决策移除 OpenTcp(`7c82805`,含 aio 镜像与常量)——transport 域报告中 OpenTcp 相关 4 条发现(P0 串帧、P2×2、P3×3)随文件删除**全部失效**,不计入本台账。

---

## 一、P0(8 条,均已抽检复核)

### OPC-UA(✅ 已修复,2026-09-28)

- **[P0] 事件订阅回调方法名错,事件通知被 asyncua 静默丢弃** — `opcua/client.py:325` — **已修复**
  `_EventHandler` 只定义 `event()`;asyncua 1.1.5 分发链(sync.py:213-214 → subscription.py:220-229)要求 `event_notification`。asyncua 内部 `except Exception` 吞掉 AttributeError → `subscribe_event` **100% 静默失效**(回调永不触发、不进 last_error)。
  **修复**:方法改名 `event_notification` 并注明分发链约束;新增真服务端回归 `test_subscribe_event_receives_notification`(get_event_generator + BaseEvent 触发,修复前必失败)。

- **[P0] 订阅间隔毫秒被当秒,差 1000 倍** — `opcua/client.py:756,775,782` — **已修复**
  三处 `sampling_interval_ms / 1000.0` 传入 asyncua;asyncua 全线 API 单位是毫秒(`ua.Duration`,`create_subscription` docstring 明写 milliseconds)。
  **修复**:三处改 `float(sampling_interval_ms)` 原样传入并注释单位依据。

### 三菱 MC(均为 native 层与同步层守卫不一致;✅ 已修复,2026-09-28)

- **[P0] native `random_read` 允许 32 位类型进字访问列表,静默错值** — `native/melsec.py:423-434,393-410` — **已修复**
  同步版 `_random_plan` 字列表限 `(BOOL,SHORT,USHORT)`(melsec.py:447-451,四轮复审 P1 修复),native 却放行 `INT/UINT/FLOAT`。0403 字访问每点 1 字,解码按 2 字拼接 → 混合列表时把下一项数据拼进本项;且首个 32 位项之后所有 16 位项字下标错位。
  **修复**:`allowed` 收窄至与同步一致;补位号拒绝(`仅布尔类型支持位访问`)与 `reject_bit_suffix_on_bit_device`;docstring 订正;回归用例 `random_read_3e_word32_rejected`。

- **[P0] native `random_write` 不拒绝位号后缀,静默清零相邻 15 位** — `native/melsec.py:480-500` — **已修复**
  同步版 `plan_devices` 拒绝任意位号后缀(melsec.py:514-519),native 无此守卫。`random_write([("M10.5",1)])` 位号被丢,1402 按字写 → **M10~M25 中除 M10 外 15 位被 0 强制清 OFF**,破坏性静默错写。
  **修复**:plan_devices 补位号拒绝(与同步同措辞);回归用例 `random_write_3e_bit_suffix_rejected`。

- **[P1→P0 组] native `random_read` BOOL 位软元件位号后缀静默读 bit0** — `native/melsec.py:445-457` — **已修复**
  同步版有 `reject_bit_suffix_on_bit_device`,native `_random_plan` 漏。**修复**随上面 P0 一并落地;回归用例 `random_read_3e_bool_bit_suffix_rejected`。

### 欧姆龙 FINS

- **[P0→撤销] 0104 多存储区读缺"存储区数"计数字段(复审裁决:误报)** — `plc/omron/codec.py:177-184`
  子代理据 W342 §5-3-5 推断命令数据首字段为 2 字节"Number of designated areas";实际提取手册原文(PDF 198-200 页)核实:**无该字段**,命令格式为逐条 `[区码+起始地址]` 拼接,且手册明言命令码后可不接任何数据(见头部复审裁决)。167 是网络上限不是计数字段域宽。**撤销**。

### AB EtherNet/IP

- **[P0→撤销] Large Forward Open 参数域错位(复审裁决:误报)** — `plc/ab/codec_cip.py:573-589,635`
  子代理按 CIP Vol 1 推导 LFO 应 `0x4200<<7`;经 pylogix 1.1.6 与 pycomm3 1.2.16 双参考实现裁决(见头部复审裁决),现有 `0x4200<<16 + size` 与两库**逐字节一致**,**撤销**。登记 P3·待核(真机抓包证伪再议)。

- **[P0] 0x29/0x2A 下标段奇长且路径尾不补齐,数组下标 ≥256 的标签必抛 ValueError** — `plc/ab/codec_cip.py:390-396,402-404` — **已修复**
  0x29(3 字节)/0x2A(5 字节)为奇长段,0x91/0x28 恒偶长 → 路径总长必奇 → `_service_request` 的偶数断言必炸(帧根本不发出)。`MyArray[300]` 读/写/类型发现全命中;`plc/omron/cip.py` 复用同一编码同样受影响。
  **修复**:按 padded EPATH **段内补齐**——段头后插 1 字节 0x00 再接值(`<BBH`/`<BBI`,pylogix `pack('<HH',0x29,v)` / pycomm3 `LogicalSegment(padded=True)` 同型),路径恒偶长;黄金帧期望值同步订正;新增回归 `test_symbol_path_odd_index_segment_padded`。

### native 机制(✅ 已修复,2026-09-28)

- **[P0] close() 与协议调用的等锁窗口击穿关闸:已关闸客户端可复活建连** — `native/base.py:695-707,284-286` — **已修复**
  `_execute` 的关闸检查只在拿锁前一次;`close()` 置 `_closed` 后等锁期间,排队中的 `_execute` 拿到锁后循环体看到 `_connected=False` → `_connect_locked()` **重新建连并完成事务**。违反 close() 关闸契约;新建传输无人回收。
  **修复**:`_execute` 进锁后 + 每次重连前各补一次 `self._ensure_open()` 复查;新增回归 `test_close_wins_gate_race_and_queued_transaction_cannot_revive`(插队代理锁复现"close 先拿锁"时序,`git stash` 验证修复前 FAILED / 修复后 PASSED)。

---

## 二、P1(14 条;✅ 13 项已修复 2026-09-28,1 项已随 P0 批落地)

- **[P1] core 写超时原连接重发,双写风险与"重发安全"论据错误** — `core/base_client.py:722,741-745` + `core/errors.py:82-85` — **已修复(文档口径订正)**
  TransportTimeoutError 是接收超时,写请求已上线;errors.py "0 字节已读,原连接上重发安全"只证响应侧。默认 write_retries=0 缓解,但安全论据本身不成立(计数累加/脉冲/步进类非幂等写双写即事故)。
  **修复**:删除错误论据,TransportTimeoutError 与 `write_retries` docstring 均明示"写重试仅对幂等写安全,非幂等写保持默认 0";`_execute` 超时分支补竞态窗口注释。行为不改(重试机制本身保留,风险由文档披露)。
- **[P1] core 超时重试前无清理,迟到响应串入后续事务** — `core/base_client.py:741-745` — **已修复(文档化)**
  重试与原响应之间的窗口:旧响应被当重试应答消费,重试自身响应成残留被下一事务消费;Modbus RTU 帧内无事务号,同型帧静默返回陈旧值。
  **修复**:竞态窗口已注释披露(_execute 超时分支);根治需"发送前清空接收缓冲"机制,登记 P3 改进项。
- **[P1] core 类型收窄失败时 last_error 已被成功路径清空** — `core/base_client.py:493-498,737,924-941` — **已修复(语义裁决)**
  `read_bool/_narrow_int/_narrow_float` 返回 `(False,None)` 但底层 `_read` 实际成功、`_clear_error()` 已执行 → 失败无原因。
  **修复**:收窄统一到 `_narrow` 并注明契约——类型不符属"驱动返回了与声明类型不符的值"(库内缺陷)而非通信失败,不伪造通信错误、保持 last_error 契约一致;bool 排除仅对整数收窄生效(修掉统一过程中 read_bool 误拒 True 的回归,测试抓到)。
- **[P1] core Tag.scale 未拒 NaN/±Inf,可静默把 0 写入 PLC** — `tag.py:65-66,162-174` — **已修复**
  `scale==0` 校验拦不住 NaN/Inf(JSON 裸字面量可入);scale=inf 时 `scaled=0.0` → 整数点位静默写 0;读方向返回 (True, nan) 误成功。
  **修复**:TagTable 构造期 `math.isfinite(scale/offset)` 校验;`write_tag` 直传 Tag 实例路径(绕过表校验)同步补校验。
- **[P1] native Modbus `_write_pdu` 丢响应,缺 FC05/06/15/16 回显校验** — `native/modbus.py:931-933` 对照 `modbus.py:962-973` — **已修复**
  同步版逐字节校验写回显;native 对 FC22/FC21 有校验、唯独基础写路径没有 → 变形写响应被静默吞。
  **修复**:`_write_pdu` 改调 `codec.parse_write_response(response, pdu)`,与同步同口径。
- **[P1] native 扩展 FC 入参 `int()` 替代 `require_int`,静默截断** — `native/modbus.py:657,688-690,835,857` — **已修复**
  float `1.9→1` 静默写错值、bool/str 被接受;同步版同路径 `require_int` 拒绝。
  **修复**:write_mask_register/read_write_registers/read_file_record/write_file_record 四处全部改 `require_int`。
- **[P1] native MC `random_read` 不拒位软元件位号后缀,静默读 bit0** — `native/melsec.py:445-457` — **已修复(随 P0 批)**
  随 P0 批 random_read 守卫对齐一并落地(ExtCase `random_read_3e_bool_bit_suffix_rejected`)。
- **[P1] MC 1E 写响应缺尾部多余字节校验,残帧滞留缓冲** — `plc/melsec/codec_a.py:145-146` — **已修复**
  读路径有严格总长校验,写路径 `return []` 不校验 `len==2` → 多余字节滞留 TCP 缓冲污染下一事务。
  **修复**:parse 层写响应补 `len(frame) != MC_1E_RESPONSE_HEAD_SIZE` 校验(TCP 收包侧天然只收 2 字节,UDP 整包与解析层双防线)。
- **[P1] KV Host Link `.H` 十六进制读值 E0~E9 被误判为设备错误码** — `plc/keyence/codec.py:107-118` + `hostlink.py:112` — **已修复**
  `_ERROR_RE` 作用在整条响应上,读数据先过错误检查 → `RD …` 返回单令牌 "E5"(=229)被抛 DeviceError。
  **修复**:`_transact` 增 `check_errors` 参数;`_read_word_token`(唯一 .H 路径)传 False,数据令牌交 `parse_word_token` 按格式校验(非法令牌仍报 ProtocolFrameError,不静默);写路径与其他读路径保持默认。回归 `test_tcp_read_hex_data_e5_not_error_code`。
- **[P1] S7 半开连接下惰性重连永久失效** — `plc/siemens/client.py:329-351` — **已修复**
  `get_connected()`(1.x)是 C 库本地标志,socket 死亡不翻转 → 断电/拔线后错误被归类 DeviceError(不断线不重试),且 `_S7Session` 无 keepalive → 永不重连。
  **修复**:`_raise_link_aware` 改两级判据——snap7 传输类错误码(`errIsoSendPacket 0x00090000`/`errIsoRecvPacket 0x000A0000`/`errCliJobTimeout 0x02000000`,经 snap7.error 码表核实)优先判定真断连 → OSError 惰性重连;连接标志只兜底显式 disconnect;docstring 注明 1.x/3.x 行为差异。
- **[P1] aio `word_order` 读方向返回 str,同步/native 返回枚举** — `aio/__init__.py:509-516` — **已修复**
  `== WordOrder.CDAB` 跨层比较静默得 False。**修复**:getter 改返回 `WordOrder` 枚举,与同步/native 一致(写方向本就同型)。
- **[P1] aio `receive_timeout`/`connect_timeout` setter 抢事务锁,阻塞事件循环** — `aio/__init__.py:248-264` — **已修复**
  同步 setter 内 `with self._lock`,慢事务期间事件循环线程锁死数秒。
  **修复**:aio setter 改 `_run_sync_attribute_set`——在单 worker executor 线程上执行属性 setter(事务本就在该线程,锁无跨线程争用),事件循环线程不再碰锁;关闸校验保留。同步 `receive_timeout` docstring 补"事件循环线程勿直写"提示。
- **[P1] MTConnect `read_assets` 的 asset id 不做 URL 编码** — `cnc/mtconnect.py:480-485` — **已修复**
  同文件 `_query` 对 path/值一律 quote,唯独资产路径漏 → id 含空格/`?`/`#` 请求畸形、路径穿越。
  **修复**:asset id 逐个 `quote(_require_asset_id(item), safe="")` 后拼接,分隔符 `;` 不参与编码;新增 `_require_asset_id` 校验(空 id 拒绝)。回归 `test_read_assets_id_with_special_chars_is_quoted`。
- **[P1] MTConnect BOOL 值域缺规范 YES/NO,标准布尔项读取恒失败** — `cnc/mtconnect.py:75-76,645-651` — **已修复**
  MTConnect Part1 布尔表示是 YES/NO 而非 true/false → 对标准 Agent 布尔项 `read_bool` 永远 (False,None)。
  **修复**:`_BOOL_TRUE/_BOOL_FALSE` 并收 `yes/no`(小写比较不变),true/false 兼容保留;fixture 增 YES 用例。

---

## 三、P2(28 条;✅ 全部处理完毕 2026-09-28:24 项代码修复 + 2 项文档化披露 + 2 项待真机核证)

**core / convert** — ✅ 已修复
- 统计计数器两把锁混护 — **已修复**:`connect_count`/`disconnect_count`/`transactions`/`device_error_count`/`last_connect_at`/`last_success_at`/`last_rtt` 全部改在 `_state_lock` 内变更(状态锁与事务锁分离的既定设计落地),stats 快照不再可能读到跨锁序中间态;`disconnect()` 失败路径也计入断开(连接事实已终结)。
- `words_to_bytes/registers_to_canonical` 静默 `&0xFFFF` — **已修复**:超范围字改抛 ValueError(写错值比报错危险)。
- `bytes_to_short/bytes_to_ushort` 不校验长度 — **已修复**:长度必须恰 2 字节,驱动切片错位在源头报错。
- `read_string` 用 `str()` 强转 — **已修复**:非 str 返回显式拒绝并记 last_error(原 bytes 会被包成 `b'...'` repr 伪装成功)。

**transport** — ✅ 已修复(口径统一)
- UDP 数据报截断跨平台不一致 — **已修复**:POSIX 探测到截断(MSG_TRUNC 真长)与 Windows(WSAEMSGSIZE)**同口径**——WARNING 日志 + 抛 `DeviceError`,不再返回残缺帧;诊断码取负值 `-10040`。
- TCP_NODELAY 无守卫 — **已修复**:`create_connection` 后的全部配置(settimeout/setsockopt/keepalive)包 try/except,失败即关 FD 再抛。
- WSAEMSGSIZE 计入 `device_error_count` — **已修复**:诊断码取负(`-10040`),基类只对 `code >= 0` 计入设备错误(同步/native 基类同口径)。

**Modbus** — ✅ 已修复
- RTU 广播写后无 T3.5 — **已修复**:广播写(expect_response=False)发送后显式静默 `_broadcast_silence()`(T3.5 = 3.5×11 位/波特率,取 `inter_frame_delay` 与计算值的较大者);`SerialTransport` 新增只读 `baud_rate` 属性。
- FC43 翻页 ValueError 逃逸 — **已修复**:设备下发的 next_object_id 落在保留区(0x07~0x7F)或越界改抛 `DeviceError`(设备侧坏指针),不再以调用方错误逃出 `read_device_id`;同步/native 同构。
- native 站号 0 拒读口径分裂 — **已修复**:native 显式声明 `_BROADCAST_WITHOUT_RESPONSE=False`(TCP 路由字段),`_reject_broadcast_read` 只拦具备广播语义的走线,与同步 `ModbusTcpClient` 一致。

**三菱 MC** — ✅ 处理完毕(3 修复 + 1 待核)
- MX COM CoInitialize 计数泄漏 — **已修复**:`connect()` 拆两级 try——初始化成功而控件创建/Open 失败时配对 `_com_uninitialize()`。
- UDP 最大合法帧超缓冲 — **已修复**:`MC_MAX_RESPONSE_CONTENT_DATAGRAM=8179`,0406 批量读响应预算按 UDP 整包缓冲收紧(库自己允许的请求必须自己收得回);常量注释披露算术。
- 1E 点数上限 255 — **待核**:手册 1E 章节点数表未能就地核证(本地无该章节 PDF 页证据),按铁律不臆改;已登记 `docs/protocol/README.md` 待补表(真机联测时按机型分命令复核)。
- `_translate_address` 钩子契约 — **已修复(文档化)**:`_build_frame` docstring 明确单点路径换算契约(品牌子类覆写必须两路同源),不再只靠子类自觉。

**欧姆龙 / 基恩士** — ✅ 处理完毕
- NJ STRING 声明尺寸假设 — **已修复(文档化 + 报错上下文)**:docstring 明示"设备回读满缓冲"假设与真机核证要求;`usable <= 0` 报错补回读载荷字节数,现场可自行判断是否固件未 NUL 填充。
- NJ STRING 长度域按字节数 — **已修复(文档化)**:明确长度域为字节数(encoding 多字节时按字节预算),归并入上一条 docstring。
- KV X/Y 混合口径 — **已修复(文档化)**:模块 docstring 披露"组号十进制+位 hex"口径、`XA5` 类全 hex 记号不支持、≥160 建议 KV Studio 核对(依据缺,不臆改)。
- SR 硬编码 UTF-8 — **已修复**:`KeyenceSrClient` 新增 `encoding`/`encoding_errors` 构造参数(默认 utf-8 + strict,非法序列抛错不静默乱码);`_read_line` 改返回 bytes 由调用点按配置解码。

**AB / ADS** — ✅ 已修复
- ADS STRING 写预检按字符数 — **已修复**:预检改按 UTF-8 字节数比较(80 汉字=240 字节现被拒),报错文案说明字节口径。
- ADS 读侧固定 STRING(80) — **已修复(文档化)**:`_read_string` docstring 披露 pyads 默认 80 字符缓冲限制与长变量改走底层通道。
- AB 连接式 RRData 短前缀 struct.error — **已修复**:连接式分支补前缀长度下限校验(与 NullAddress 分支同款),坏帧走 ProtocolFrameError 不裸抛。

**OPC-UA / MTConnect** — ✅ 已修复
- OPC-UA read_batch 丢单节点 StatusCode — **已修复**:`read_values` 的 None 结果不再一律报"节点值为空",批次内 Bad 节点按设备侧拒绝分类。
- OPC-UA 被动断线不清订阅索引 — **已修复**:`_mark_disconnected` 路径同步清 `_active_subscriptions`。
- OPC-UA 死区 Trigger 固定 StatusValueTimestamp — **已修复**:改 `StatusValue`(仅值变化触发评估;Timestamp 刷新不再绕过死区)。
- OPC-UA 底层订阅传 SyncNode — **已修复**:改传 `node.aio_obj`(aio 节点),不再依赖 SyncNode 属性转发巧合。
- OPC-UA `ns=` 无范围校验 — **已修复**:构造期校验 UInt16(0~65535)。
- OPC-UA `b=` base64 过松 — **已修复**:严格 RFC 4648 §4 分组校验(`AA==BB==` 类静默错址现被拒)。
- MTConnect nan/inf 进数据链 — **已修复**:非有限值拒绝(与 UNAVAILABLE 同径)。
- MTConnect `/current` id/name 冲突 — **已修复**:两遍展开(先全部 id 再补 name 别名,键位占用即跳过),条件项子元素(Fault/Warning/Normal)剔除出数据项映射。
- MTConnect `read_sample` 参数校验 — **已修复**:count/from_sequence/at 显式 require int(str 抛 ValueError 而非 TypeError,float 不再静默截断)。

**松下 / 丰田 / aio** — ✅ 处理完毕
- MEWTOCOL UDP 长读不匹配 — **已修复**:UDP 走线发送前按 `parse_expected_size` 预算拦截(超 2048 缓冲即入参期拒绝,零字节发送)。
- TOYOPUC X/Y、T/C 同址 — **待核**:官方手册缺(已标),沿用参考实现口径;真机第一优先复核(登记 `docs/protocol/README.md` 待补表)。
- AOpcUaClient 缺 unsubscribe 镜像 — **已修复**:新增 `async def unsubscribe(subscription)`,网络往返经 executor 不阻塞事件循环;回归用例含幂等与入参校验。
- 汇川批量/诊断绕过 `_translate` — **已修复(文档化)**:客户端 docstring 明确批量/诊断方法按 hr/c 裸 Modbus 记号,单点用汇川记号(显式报错非静默)。

---

## 四、P3(52 条,归组列出)

**core/convert/types/tag/debug/constants(17)**
- `float32_to_registers`/`require_float` OverflowError 裸逃逸非 ValueError(convert.py:248-252;validation.py:26-30)
- `write_tag` 整数点位银行家舍入未声明(base_client.py:681-683)
- `last_rtt` 含重试/重连耗时与"往返耗时"口径不符(base_client.py:725,738-739)
- 直连 `connect()` 退避拒绝覆盖根因 last_error,与 `_execute` 口径相反(base_client.py:173-181 对照 728-731)
- constants 三处孤儿 docstring,其一与正式口径矛盾(0~15 vs 01~16)(constants.py:598-599,605,816-820;另 SR 常量注释同款)
- `MC_1E_DEVICE_CODES` 缺 "L" 与注释"M/L/S 同码"矛盾(constants.py:323-336)
- `DataType.STRING.register_size` 返回 1 语义误导(types.py:75-78)
- `set_debug(False)` 不拆 stderr handler、不复位 propagate(debug.py:50-54,130-143)
- `disconnect()` close 失败时 disconnect_count 少计(base_client.py:239-252)
- `_reorder_bytes` 奇数长 CDAB 分支静默丢首字节(当前调用点不可达)(convert.py:483-485)
- `validate_endpoint` int(port) 静默截断浮点(base_client.py:88-91)
- (其余 core 域 P3 见子代理报告,均为同档注释/口径类)

**transport(5)**:base.recv_some 默认实现退化"读满恰好 max_bytes"(SerialTransport 继承陷阱);docstring OSError(含超时)与实现 TransportTimeoutError 不符;opentcp 死分支(已随文件删除失效,留档);serial recv 无 finally 还原 port.timeout;RS-485 方向控制未实现未声明
**注**:opentcp 相关 4 条 P3(recv_some 死分支、_coerce_timeout inf/NaN、udp.py 文档失实、keepalive 注释)中,`_coerce_timeout`/`udp.py` 文档/keepalive 注释三条仍有效(文件未删)。

**Modbus(2)**:FC11/FC12 规范节号引用错位(modbus.py:813,831 + native);FC43 RTU 增量收包 ADU 上限少减 CRC 2 字节(modbus.py:1514,CRC 兜底不致错数据)

**MC(5)**:random_write docstring 称位写值 0/1 实现放行 0~65535(melsec.py:492-495,527-533 + native 同构);native `_next_serial` docstring "3E"应为"4E";codec_serial_a 跨模块用 codec_serial 私有成员;0406 字块不合并相邻条目(120 块上限对 ≥121 相邻点误拒)+ 位单位 900 统一上限低于规格;1C 帧 PC 编号放行 0~120 超 A 兼容规格

**欧姆龙/基恩士(8)**:FINS TCP 握手不校验命令回显与冗余尾;`_write_bits` 缺位单位上限(当前不可达);`_check_identity` 不校验 DA/DA1/DA2/SA 回显;0x1101 回退读改写两事务非原子未披露;KV Host Link TCP 逐字节 recv(1) 性能;RDS 无客户端侧点数上限;KV 软元件表收录不存在的 D/E/F、LR 缺低位校验;条码内容恰为 "OK"/"ERROR" 误判无读出

**AB/ADS(8)**:build_get_attributes_all/decode_identity_string 死代码;`_unregister_session` 注释把工程取舍归因规范;`_extended_status_text` 4 字扩展状态用 u32 键查 16 位码表命中为零;encode_value FLOAT 无范围预检 OverflowError 逃逸;`_known_types` 按基名缓存无失效机制(在线改型后写侧类型码陈旧);Forward Open 被拒丢附加状态回落不可诊断;应答回显宽容口径 RRData 与 SendUnitData 不一致;read_string 默认 32 静默截断(AB 上限 82)+ write 静默 str() 强转

**S7/MEWTOCOL/TOYOPUC(8)**:`_is_connected` 注释与 snap7 3.x"主动探测"不符(3.x 行为反而更准);rack/slot 浮点静默截断;多变量读条目错误缺寻址上下文、错误码不入 DeviceError.code;`_write_string` 声明长 0 回退"首次写入定长"易踩;connect 失败不销毁 snap7 Client;MEWTOCOL 位写 RMW 竞态无警示(S7 侧有,不对称);站号校验单向放行 EE;TOYOPUC `_require_byte_range` 透传 W 后缀报错误导、字软元件无段校验暴露点晚

**OPC-UA/MTConnect(6)**:browse docstring "None=所有参考" 实为层级参考;subscription_id 实为 id() 内存地址非 UA SubscriptionId;`_read_string` 冗余 str() + 双份整数范围表;GUID 花括号不对称漏到 asyncua 报错;Condition severity 属性 v1.x 恒空;/sample 坏 nextSequence 静默吞、_query 参数键不编码

**aio/native(6)**:四品牌 A* 类绕过直接父类 __init__ 直调 ABaseClient.__init__;`__aenter__` 连接失败不释放 executor 线程;H3U X/Y 256 点上限未机型区分(已文档化取舍);汇川 mc docstring 端口禁区未落地;scripted_async hang 分支注释与实现不符(等 3 倍超时);native UDP recv `received is None` 不可达 + "整事务 deadline"措辞夸大 + recv_some 预留接口 + Task 每事务多次分配(P2 性能项归并此档)

**native 机制(2)**:DeviceError/TransportTimeoutError 分支不清 pending,下次事务取消时误拆干净连接(保守方向);`_guard` locked() 换锁防御不覆盖 acquire 排队态(跨线程窗口);`_await_with_timeout` 兜底 except 吞外层新到取消(升级为 P1 见上);外层取消只请求不等落地(docstring 名不符实)

---

## 五、去重与失效登记

1. **与四轮复审(review-1922)重复,不重复计**:S7 get_connected 断线判定(本轮升格 P1 并给出修复口径)、FINS 0x1101 标志位屏蔽(子代理确认 bit6/7 屏蔽已实现,撤销)、MC QnA 随机读 96/192 分档、OPC-UA GUID 花括号(本轮细化为"不对称漏到 asyncua 报错")、native 超时竞态/取消细节、aio word_order(已在四轮列过,本轮确认仍未修)。
2. **OpenTcp 移除失效**:transport 域 P0×1(缓冲残帧串帧)、P2×2、P3×4 全部随 `7c82805` 删除,不计。
3. **本轮撤销的既有疑虑**:FINS 结束码标志位屏蔽经查已实现(codec.py:438-448);KV Host Link "@单元号+BCC" 疑虑不成立(以太网变体无此字段,双参考实现核证);MEWTOCOL BCD 混用疑虑不成立(站号/编号十进制 ASCII,无 BCD 面);汇川 32 位计数器展开公式(X1777→coil 0xFBFF 等)逐项验算正确。

## 六、门禁与结论

审查启动时点门禁:3.7.9 **1387 passed** / ruff / mypy(76) / ty 全零(97b1950);OpenTcp 移除后 1347 passed / mypy(74)(7c82805,未推)。
**P0 复审裁决后净 6 条,已修复 5 条**(OPC-UA×2、MC native×2、native 关闸×1、AB 0x29/0x2A×1);修复后门禁:3.7.9 **1356 passed** / ruff / mypy(74) / ty 全零。新增回归:OPC-UA 事件订阅真触发×1、MC native 守卫×3(ExtCase 表)、AB 路径段补齐×1、native 关闸竞态×1(经 stash 前后验证)。
**遗留 P0:0 条。**

结构性结论:
1. **同步层协议编码经黄金向量/参考实现双向核证基本无协议级错误**(MC 3E/4E/串口 1C/3C/4C、Modbus 全 FC、FINS 头、MEWTOCOL、TOYOPUC 帧、AB ENIP 头/CPF 均核证通过);P0 集中在三处:OPC-UA 封装层(2)、native 与同步守卫不一致(2+1)、AB CIP 组帧(1)、native 关闸竞态(1)。
2. **native 层是本轮重灾区**:P0 中 3 个在 native(与同步守卫不一致×2 + 关闸竞态×1),另有写回显缺失等 P1×2——"薄分发层"的守卫纪律(拒绝条件、回显校验、require_int)需逐条对表补齐。
3. **OPC-UA 封装层两处 P0 意味着事件订阅与订阅间隔从未真正可用过**(无真机联测覆盖);已修复,真机验证仍待做。
4. **两个撤销的 P0(FINS 0104、AB LFO)均系子代理对手册/规范的推断性误读**,按 W342 原文与 pylogix/pycomm3 实现裁决驳回——引用依据必须落到手册原文页码或参考实现逐字节比对,不接受"按规范推断"的单源结论。
