# 全项目四轮复审(review-1922)

> 日期:2026-09-27 · 触发:*重新整体再检查下项目*
> 范围:全库 76 个源文件 / ~24k 行(HEAD 于 v0.44.0 之后)
> 方法:分 6 域并行只读逐行审查(core/transport/opentcp · aio/native · Modbus ·
> 三菱 MC·MX · Omron/AB/ADS/S7 · OPC-UA/CNC/扫码/厂商杂项),对关键项以脚本化
> 传输在 3.7.9 门禁环境复现;有官方手册者按页码核对,无手册者标「待核」。
> 分档:**P0** 错动作/数据损坏/死锁 · **P1** 静默错误/丢数据 · **P2** 资源浪费/性能/文档化限制 · **P3** 可控隐患/可维护性

---

## 一、本轮已修复(附回归门禁)

- **[P0] 三菱 MC `random_read`/`random_write` 静默丢弃字软元件位号后缀**
  `plc/melsec/melsec.py`:`_random_plan` 与 `random_write.plan_devices` 只取 `parsed.number`,不校验 `parsed.bit`——`random_read([("D100.3",SHORT)])` 会静默读 `D100`(字),与 §2.2.1 同类 P0。
  **修复**:`_random_plan` 非 BOOL 带位号 → `ValueError`;BOOL 位软元件走 `reject_bit_suffix_on_bit_device`;`plan_devices` 任意位号后缀 → `ValueError`。
  门禁:`test_tcp_3e_random_read_rejects_invalid`、`test_tcp_3e_random_write_rejects_bit_suffix`。

- **[P1] 三菱 MC `random_write`(1402)从不校验结束码**
  `operation()` 只 `self._transact(request)` 后丢弃响应;PLC 拒绝的随机写被当成功。
  **修复**:收包后 `self._parse_write(response, False)`(SH-080008 §5.3 印刷页 46:1402 响应无数据但有结束码)。
  门禁:`test_tcp_3e_random_write_validates_end_code`。

- **[P1] 三菱 MC `random_read` 的 `word_items` 允许 32 位类型却按 1 字请求**
  `_random_plan(is_double=False)` 放行 INT/UINT/FLOAT,但每项只发 1 个字访问点;解码却取 2 字 → 末项报错、非末项**静默拼接相邻点**(SH-080008 §8.3:字访问 1 字/点,32 位须双字访问)。
  **修复**:`word_items` 限 `SHORT/USHORT/BOOL`,32 位强制落 `double_word_items`;docstring 订正。
  门禁:`test_tcp_3e_random_read_rejects_invalid`。

- **[P1] UDP `connect()` 失败泄漏套接字(惰性重连反复泄 FD)**
  `transport/udp.py`:`sock.connect(...)` 抛错时局部 `sock` 无人关闭,`close()` 因 `self._socket is None` 直接返回。
  **修复**:`try: settimeout/connect except OSError: sock.close(); raise`。
  门禁:`test_connect_failure_closes_socket`。

- **[P1] `write_tag` 非恒等逆缩放不取整,整数点位在常见小数 scale 下必失败**
  `core/base_client.py`:`(0.3-0)/0.1 = 2.9999999999999996` 非整数 → 非整 float 透传 → 整数驱动 `require_int` 拒收,`ValueError` 逃出公共 API。
  **修复**:非恒等分支对整数 DataType(SHORT/USHORT/INT/UINT/LONG/ULONG)`int(round(scaled))`。
  门禁:`test_write_tag_inverse_scale_rounds_float_noise`。

- **[P1] Modbus 输入寄存器位写(`irX.Y` + BOOL)未入参期拒绝**
  `modbus/modbus.py::_check_address` 写路径只拦了 `DISCRETE_INPUT`,漏 `INPUT_REGISTER`;`write_many`/`write_batch` 到锁内才抛,违反「只读区入参期拒绝、零字节发送」契约。
  **修复**:写路径 `bit 非空` 且区 ∈ {di, ir} 一律拒绝。
  门禁:`test_write_readonly_area_rejected_before_lock` 扩充(`ir0.3` / `write_batch`)。

- **[P2] TCP `recv` 结束后未复位 socket 超时**
  `transport/tcp.py`:循环内 `settimeout(remaining)`,成功返回不还原 → 下一事务 `sendall` 继承极小超时,慢链路伪超时。
  **修复**:`finally` 还原 `self._receive_timeout`(守卫 OSError)。

- **[P2] AB `read_batch` 非 BOOL 类型的 `.bit` 后缀静默忽略**
  `plc/ab/ab.py`:非 BOOL 项直接建读请求,`parsed.bit` 被 `tag_type_path` 丢弃 → 读回整字,调用方以为拿到某位(单点 `_read` 会报错)。
  **修复**:非 BOOL 分支 `parsed.bit is not None → ValueError`。
  门禁:`test_read_batch_rejects`(连接后用例)。

- **[P2] AB 附加状态 size=1 时只读 1 字节,16 位扩展码高字节丢失**
  `plc/ab/codec_cip.py::_extended_status_text`:该字段单位为 16 位字,`word_count==1` 却取 `cip[4]` 单字节 → 如 0x0100 被截成 0x00,查表落空、扩展诊断静默丢失。
  **修复**:`word_count==1` 改 `struct.unpack_from("<H", cip, 4)[0]`。
  门禁:`test_extended_status_size_one_reads_full_word`。

- **[P2] OPC-UA `browse(reference_type_id=...)` 是死参数**
  `opcua/client.py`:参数解析成字符串后从未用于 `get_children`,且类型应是命名空间 0 的 `ObjectId(int)`。
  **修复**:解析为 int;`_browse_node` 以 `node.get_children(refs=<int>)` 透传;非 ns0/非数字标识符入参期 `ValueError`。
  门禁:`test_browse_reference_type_requires_ns0_numeric`。

- **[P2] OPC-UA `subscribe_data_change` 对服务端拒绝的 MonitoredItem 静默报成功**
  asyncua 对 list 入参不 `check()`,失败项以 `ua.StatusCode` 混在结果里,库侧未校验 → 返回 `(True, handle)` 但无回调。
  **修复**:遍历结果,含 `StatusCode` → 删订阅并抛 `DeviceError(code=0)`(设备侧拒绝,不断线)。

- **[P2] OPC-UA Event 退订误用事务锁**
  `opcua/client.py::subscribe_event._do_unsubscribe` 用 `self._lock`(其余订阅索引增删均 `_state_lock`)→ 与长事务争用、竞态。
  **修复**:统一 `_state_lock`。

- **[P2] MTConnect 整数读不做范围/符号收窄**
  `cnc/mtconnect.py::_coerce` 一律 `int(value)`,`read_short("X")` 可返回 40000;其它驱动均收窄。
  **修复**:按 DataType 范围收窄,越界 → `DeviceError(code=0)`(设备侧条件,不断线)。
  门禁:`test_read_integer_out_of_declared_range`。

- **[P2] 基恩士 SR 命令错误应答 `ER,<命令>,<码>` 被当条码**
  `scanner/keyence_sr.py::_scan_once`:仅特判 `ERROR`/`OK`/空;设备侧命令失败时 `scan()` 返回 `(True, "ER,LON,21")`。
  **修复**:`ER,` 前缀按命令错误抛 `DeviceError`(码取自第 3 字段),不断线。
  门禁:`test_scan_command_error_response_rejected`。

### 第二批(继续处理 P2)

- **[P2] `read_tag` 非恒等缩放对 64 位整数静默丢精度**
  `core/base_client.py`:`LONG` 配非恒等 `scale/offset` 走 float64,>2^53 静默舍入。
  **修复**:整数且 `|值|>2^53` 时输出 WARNING(非恒等缩放必经 float64,不中断既有用法);docstring 口径不变。

- **[P2] native `connect()` 清理钩子取消语义 + 跨循环 `close()` 泄漏**
  `native/base.py::_connect`:清理钩子 `await` 被 `except Exception: pass` 包住——3.7 吞取消(`CancelledError` 是 Exception 子类)、3.8+ 跳过 `transport.close()`(半开传输泄漏);`native/transport.py::AsyncTcpTransport.close` 与 `native/base.py::_disconnect_locked` 只捕 `OSError`,跨循环 `writer.close()` 抛 `RuntimeError` 致清理中断。
  **修复**:①钩子取消按 `_CANCELLED_ERRORS` 捕获 → 照常落地清理后 `raise`(不吞取消、不跳过清理);②两处 close 捕获放宽为 `Exception`(尽力而为,不中断断开流程)。
  依据:3.7 `CancelledError` 为 `Exception` 子类的历史坑(见 `core/errors.py` 注释)。

- **[P2] native UDP `connect()` 失败泄漏套接字**
  `native/transport.py::AsyncUdpTransport._connect`:`sock.connect` 抛错时局部句柄未挂 `self._socket`,`close()` 收不到 → 惰性重连反复泄 FD。
  **修复**:`try: setblocking/connect except OSError: sock.close(); raise`(与同步层 UDP 同口径)。

- **[P2] Modbus FC22 未处理 RTU 广播**
  `modbus/modbus.py::write_mask_register` 未传 `expect_response=not(站号0)` → 广播掩码写永远等超时。
  **修复**:与 `_write_pdu`(FC05/06/15/16/21)同口径——广播下不等响应、跳过回显。

- **[P2] Modbus FC20 响应无 PDU 总长上界**
  `modbus/codec.py::build_read_file_record_pdu` 只校请求侧,35×125 记录可构造 `expected_response_length≈8822`(规范 §6.14:聚合响应 ≤253B);解析侧对 `pdu[1]` 无上限。
  **修复**:构造期按 `2+Σ(2+2×记录长) ≤ 247` 入参期拒绝;解析期 `pdu[1] > 0xF5` 按坏帧。

- **[P2] Modbus FC43 RTU 增量收包无 ADU 上界**
  `modbus/modbus.py::_recv_device_id_tail`:最坏可读 ~64KB(FC12/17/24 均已封顶 256,独漏 FC43)。
  **修复**:累计长度按 `MODBUS_RTU_MAX_ADU_SIZE-2` 封顶,越界按坏帧 `ProtocolFrameError`。

- **[P2] Modbus 多处 `int()` 静默截断浮点**
  `write_mask_register` 掩码、`read_write_registers` 读数量与写值、FC20/21 文件号/记录号/长度/值:`1.9→1` 静默写错值。
  **修复**:统一改 `require_int`(越界/非整数入参期 `ValueError`)。

- **[P2] 三菱 MC `get_cpu_type` 忽略客户端路由、4E 序列号不校验**
  `codec_qna.build_read_cpu_model` 硬编码 network 0/PC 0xFF;多站/跨网读错站。
  **修复**:签名补 `serial/network_number/pc_number/monitoring_timer`;同步与 native 客户端传自身路由 + `_next_serial()`,响应按 `expected_serial` 回显校验。

- **[P2] 三菱 MC 串口 PC 号被限死 0~3/FF**
  `plc/melsec/codec_serial.py::check_pc_number`:SH-080008 §6.2 印刷页 54 允他站 `01H~78H`(1~120),串口多站在入参期即被拒。
  **修复**:范围放宽为 `0~120 或 0xFF`(0~3 保留 A 系列直连旧口径);门禁更新(`pc_number=121/-1` 拒绝,`4/120` 放行)。

---

## 二、待修复(尚未处理,按严重度)

### P2
- **[P2·待核] S7 断线判定依赖 snap7 1.3 `get_connected()`** — `plc/siemens/client.py:336-351`:1.3 该 API 自述「有时断线仍返回 True」,死链可能被误判为在线→不再重连。建议结合异常/主动 disconnect,或 1.3 线改「读失败即拆连」保守口径(真机待核,未改)。

### P3
- **[P3] MC 随机读上限 192 取 iQ-R/Q/L 口径;QnA 实为 96** — `codec_qna.py:396-397`:QnA 超 96 点必被 PLC 拒(现交 PLC 裁决)。建议按机型/子命令分档或文档化默认。
- **[P3] MC 1E `X/Y` 进制 docstring 说「八进制」与实现(16)矛盾** — `plc/melsec/address.py:12-14`;1E 表 `M/L/S` 同码注释有 `L` 但字典无 `L`;1E 点数上限 255 丢掉手册「256→00H」特殊值;CPU 型号文档写 `0x6302` 实现为 `0x0263`(手册 `63H 02H`)。均为文档/表订正类。
- **[P3] FINS D/EM 位回退比较未屏蔽结束码标志位** — `plc/omron/omron.py`:`exc.code == 0x1101` 精确比较,带 bit6/7/15 标志(如 0x1141)时不触发回退。建议屏蔽标志后比较。
- **[P3] S7 未初始化 STRING(声明长 0)写入 >255 字节抛裸 `ValueError`** — `plc/siemens/client.py`:在 `_execute` 内执行,异常逃逸而非落 `last_error`。建议提前按 STRING 上限校验并给明确文案。
- **[P3·待核] AB Forward Open 超时乘数 0x03 疑为 ×32 而非自述 ×4** — `plc/ab/codec_cip.py` + `core/constants.py`:若确为 32×RPI,则「降 RPI 消除连接抖动」方向相反(真机待核)。
- **[P3] OPC-UA GUID 校验允许单侧花括号** — `opcua/address.py`:正则两侧各自可选,`g={...`/`g=...}` 也通过;应成对约束。
- **[P3] OPC-UA 惰性重连不清订阅索引** — `core/base_client.py::_mark_disconnected` 只关传输,不清 `_active_subscriptions`,故障后 `active_subscriptions` 仍报失效句柄。
- **[P3] MTConnect `/asset/{id;id}` 的 id 未 URL 编码** — `cnc/mtconnect.py`:含 `?`/`#`/空格 的 id 破坏请求行。建议 `quote(id, safe="")`。
- **[P3] 统计计数由两把锁混护 / 退避门控计入 `transactions` / `connect()` 覆盖根因 / `write_short` 静默截断 float** — `core/base_client.py`:一致性与语义偏差,低危但建议统一口径。
- **[P3] OpenTcp 长度前缀发送未做范围校验** — `opentcp/client.py`:`len(payload).to_bytes(prefix)` 溢出抛 `OverflowError` 而非契约 `ValueError`。
- **[P3] `convert` 两处边界** — `float32_to_registers` 未把 `OverflowError` 归一 `ValueError`;`_reorder_bytes` 对奇数长静默补 0;`registers_to_canonical` 对越界寄存器 `&0xFFFF` 静默掩码。
- **[P3] `validation.check_range` 未先 `require_int`;`types.from_name` 非 str 抛 `AttributeError`** — 与其它入口口径不一致。
- **[P3] aio `word_order` 返回 str(同步/native 返回枚举)** — `aio/__init__.py`:跨层比较会分支错误。
- **[P3] native 取消/超时细节** — 超时竞态可能丢弃已到响应(写侧重试有双写风险);外层取消不 `await` 内层落地;`_clear_stale_selector` 主线程无循环时可能顺手创建事件循环。
- **[P3] Modbus FC22 little-endian 掩码非规范** — `modbus/codec.py`:注释称施耐德机型需要,地址大端而掩码小端自相矛盾(待核)。
- **[P3] Modbus More-follows 仅认 0xFF** — `modbus/codec.py`:非 0x00/0xFF 时应坏帧而非静默截断分页。
- **[P3] Modbus `write_batch` 整批在单个写事务内** — `modbus/modbus.py`:`write_retries>0` 时传输失败整批重放,已成功 chunk 重复写(默认 0 可规避)。

---

## 三、待核 / 需手册

- 松下 FP(MC/MEWTOCOL)、基恩士 KV Host Link/MC、丰田 TOYOPUC 扩展命令(0x94/95、0x60/61、0x70/7E、PC10 0xC2~C6):官方手册缺(`docs/protocol/README.md`「待补」),帧字段/码表未断言。
- OPC-UA Part 4(Browse/Read/Write/Subscription)与 asyncua 以外服务细节。
- snap7 1.3 `get_connected()` 真机死链行为;AB Forward Open 空闲超时真机口径。
- UDP `MSG_TRUNC` 在 macOS 语义(`transport/udp.py:111-131`,目标平台 Windows,未实测)。

---

## 四、门禁与结论

门禁(3.7.9):**1387 passed** / ruff / mypy(76 files) / ty 全零。
累计修复 24 项(P0×1、P1×5、P2×18),新增回归门禁 20+ 条;其余 P3 与待核项已登记如上,按项目排期处理。整体无 P0/P1 残留;核心传输/协议契约未发现新的数据损坏级缺陷。
