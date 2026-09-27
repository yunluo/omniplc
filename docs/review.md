# Omniplc · 现场 PLC 开发专家评审报告(复核后 v2)

> 评审视角:会把这个库推到产线上的 PLC 工程师
> 范围:全协议 + 异步层 + 测试 + 工程配置
> 评级标准:**P0** 错动作 / 数据损坏 / 死机 · **P1** 静默错误 / 静默丢数据 · **P2** 资源浪费 / 性能损耗 / 文档化限制 · **P3** 可控隐患 / 可维护性
> 行号引用格式:相对路径:行号
> 条目分档:**实锤**(已逐行源码核验)/ **文档化限制**(代码注释或 README 已明示的已知取舍)/ **待核证**(需真机或手册确认)

---

## 复核记录(2026-09-26)

v1 评审稿逐条对源码复核后形成本版:

- **删除 8 条误报**:FINS 握手字节偏移(`frame[19]/frame[23]` 实为 4 字节节点地址字段的 LSB,即节点号,与 node-fins/finslib 口径一致)、`FINS_HANDSHAKE_LENGTH` 应为 8(实为 12,长度域=长度字段后字节数,总 20 字节)、MC 1E 错误码当成功(`codec_a.py:131-135` 对任何非 0 结束码均抛 `DeviceError`)、native MBAP/FINS 帧长不验(`parse_mbap_header` 与 `parse_tcp_head` 在 native 同样生效)、RTU 异常响应多读 3 字节(异常帧总长 5B,`head 2 + recv(3)` 恰好)、`_bump_id` 非原子(sync 层调用全在事务 RLock 内;native 层单事件循环无竞态)、1E 站号回显未校验(1E 响应帧格式无站号字段,无从校验)。
- **修正 12 条口径**:见各条目内"复核后"标注(disconnect 顺序、write_retries 默认值、MC 4C 收包预算、RPI 路径与影响、Keyence R5 编码争议、Panasonic SM 映射争议、OPC-UA 重订为设计边界等)。
- 复核确认成立的条目在标题后标注 **实锤**。

## 修复记录(2026-09-26)

以下必修项已随本评审落地修复(全量测试/ lint / mypy / ty 通过):

- **§2.2.1 MC 位软元件 `.bit` 静默丢弃(P0)**:校验下沉 `codec_qna.build_core` 与 `codec_a.build_request`(组帧层,同步/native/串口 3C·4C 共用);批量读路径(0406 位块)同步校验。松下等品牌子类的记号换算(如 `R1.15`)先于校验执行,不受影响。
- **§2.2.3 MC 设备码表扩容(P1)**:补 L/F/SB/V/DX/DY/TS/TC/TN/CS/CC/CN/SM/SD/SW;智能功能模块缓冲存储器不在 SLMP 软元件寻址范围,未收录。
- **§2.3.3 NJ BOOL 数组(P1)**:`OmronCipClient` 覆写元素读/写/批量路径——元素直读、实际类型由自描述应答决定;应答存储字类型(DWORD)时按 Logix `下标//32` 口径回退;批量读经 `_batch_bool_array_address` 定制点同口径。
- **§2.3.4 NJ STRING(P1)**:按 `len(u32)+字符` 布局实现读写;写入前先读模板实例号与声明尺寸,写入类型域回带实际模板号,值超声明尺寸拒绝。
- **§2.4.1 AB 0x0A 字节预算(P1)**:`read_batch` 改为按 (条数 ≤32, 估算字节 ≤480B) 双约束自动拆分事务,长标签名不再触顶未连接缓冲;条数硬上限取消。
- **§2.6.1/2.6.2 S7 STRING(P1)**:读超长按请求 length 截断返回(不再静默返空串);写保留 PLC 侧声明长字节、仅覆盖实际长,超声明长拒绝(防溢出污染相邻变量),未初始化区(声明长 0)兼容旧口径。
- **§2.5.1 ADS transport 错误分流(P1)**:transport 码集原误用 0x705/0x706/0x725(参数尺寸错/数据非法/许可过期,均属设备语义),经 **TE1000 §8「ADS Return Codes」**核实后改为全局组 `0x06/0x07/0x0D/0x12/0x1B/0x1D` + Router 组 `0x0500~0x050D`;参数/许可类 ADSError 回归 `DeviceError` 不断线。**二轮曾记「已修」但码集未动,此处订正。**
- **§2.2.2 FX5U X/Y 八进制(P2)**:新增 `xy_octal` 构造参数(同步 TCP/UDP + native 同名参数),X/Y 按八进制换算组帧;默认仍为 Q/L/R 十六进制口径。
- **§4.1 CI 版本矩阵(P1)**:按"3.7 必保、3.12 覆盖新环境"裁决,CI 改矩阵 `["3.7.9", "3.12"]`(3.7 腿用 `actions/setup-python`,uv 禁下载、显式 `--python`);**四道门禁(pytest / ruff / mypy / ty)两条腿都跑**;mypy 固定 `--python 3.12` 工具环境(防 3.7 腿回退旧 mypy 误报编码);pytest 步骤补 `--extra dev`。
- **§2.2.4 MC `read_batch` 位软元件字块(P1)**:计划段拒绝位软元件 + 非 BOOL(`_bit_device_word_access_allowed` 定制点,松下置 True)。
- **§2.2.5 串口 3C/4C 路由回显(P1)**:解析端校验响应路由与请求一致(对齐 1C);build/parse 共用路由构造函数。
- **§2.5.2 ADS STRING 写预检(P1)**:写前按符号声明长度拦截,取不到符号信息则跳过。
- **§4.2 故障注入测试(P1)**:新增真回环故障注入服务 `tests/unit/chaos.py` 与 6 例 `tests/unit/test_fault_injection.py`(半帧/慢速/错长度/断管/惰性重连)。
- **§2.3.13 FINS 结束码标志位(P1,真机带出)**:`0x9005` 等含 bit15 中继标志/bit6-7 目标 CPU 标志的码先屏蔽再查表;依据 W342-E1-18 §5-1-3。
- **§2.3.14 FINS 节点号范围(P1,真机带出)**:以太网节点范围由 126/127 修正为 **1~254**(W342-E1-18)。
- **§2.2.7 MC 0406 位块合并(P2)**:相邻同软元件位请求合并为一个位块(1 点=16 位),连续 100 M 点由 100 块降为 7 块。
- **§2.3.1 FINS 路由错误现场提示(P2)**:结束码附 `FINS_END_CODE_HINT`(0x0201/0x0101/0x0501-0x0504/0x1005 等)。
- **§2.3.5 FINS D/EM 位读回退(P2)**:0x1101 时自动改「字读+提位」。
- **§2.3.10 FINS 0x0504 文本修正(P3)**:改为「超过最大中继层数(最多 3 层网络)」。
- **§2.2.6 MC 0406 子命令核实(P2)**:核实 SH-080008 §8.4 + Appendix 1——0002/0080 为「设备扩展指定」(非跨网),上限 60 块;本库 0000 对标准软元件无缺失,设备扩展(Un\G 等)登记 v1.x。**不臆造扩展线格式**。
- **§2.1.2 Modbus Modicon 6 位地址(P2)**:按位数区分 5/6 位方案。
- **§2.1.6 Modbus FC08/11/12(P2)**:诊断/事件计数/事件日志(含 aio 镜像)。
- **§2.1.7 Modbus FC20/21 文件记录(P2)**:读写文件记录(含 aio 镜像)。
- **二轮复审 P0 MC `L` 设备码撞码(2026-09-26)**:`core/constants.py` `MC_DEVICE_CODES["L"]` 由 `0xA0` 改为 **`0x92`**(原值与 `B` 同码,致 3E/4E/4C 下所有 `L` 读写静默打进 `B` 空间);新增回归门禁 `test_mc_codec.py::test_mc_device_code_table_l_is_92_and_no_collisions`(锁 `L=0x92` 且设备码表无重码)。
- **二轮复审 P1 批(2026-09-26)**:①**ADS transport 码集**——改用 TE1000 §8 全局组 `0x06/0x07/0x0D/0x12/0x1B/0x1D` + Router 组 `0x0500~0x050D`,回归测试 `test_translate_ads_error_transport_codes` / `..._parameter_codes_are_device`;②**S7 `read_wstring` ASCII 返空**——`convert.decode_string` 对 UTF-16/UTF-32 改为「解码后按 NUL 字符截断」(字节层截断会把 `b"\x00A\x00B"` 整串截空),回归 `test_convert.py::TestString::test_utf16_ascii_not_truncated_by_high_nul` 与 `test_siemens_s7_clients.py::test_wstring_ascii_roundtrip`;③**`read_tag`/`write_tag` 64 位精度**——`scale=1.0/offset=0.0` 时原值直通,不经 float64 往返(>2^53 曾静默丢低位),回归 `test_base_client.py::TestTagScaling::test_{read,write}_tag_identity_scale_preserves_int64`;④**aio 属性读阻塞事件循环**——`connected/last_error/last_error_category/last_error_code/stats/next_connect_in` 同步 getter 改**无锁快照**(固定键集值级拷贝,GIL 下安全),aio 转发不再进事务锁,回归 `test_v034_reliability.py::TestAioBackoffAndErrorSurfaces::test_property_reads_do_not_block_on_transaction_lock`。门禁 1150 → **1158**。
- **二轮复审 P2 批 1(2026-09-26)**:①**native 镜像**——`native/modbus.py` 字符串补 `.bit` 拒绝、`native/omron.py` 位读补 0x1101 回退;②**Modbus 批量读 `.bit`**——`read_many`/`read_batch` 改用 `_check_address`(兼补跨度校验);③**写语义 `is_write=True`**——Modbus FC23/FC08-A/FC21 + OpenTcp `transact`/`transact_text`;④**FINS `read_batch`** 拒绝非 BOOL 位号(回归并入既有 `test_read_batch_rejects`,非新用例);⑤**AB 0x0A 预算**补偏移表 2×n;⑥**MTConnect DOCTYPE** 防 UTF-16 绕过(剔 NUL 后扫描);⑦**退避指数**按 32 封顶防 `OverflowError`。门禁 1158 → **1169**。
- **二轮复审 P2 批 2 + P3(2026-09-26)**:①**convert**——`word_order` 校验/归一化 + `registers_to_*` 寄存器数量校验;②**OPC-UA**——browse 默认深度上限(10)、`_coerce_read` 整数范围收窄、NodeId 组件切分(`s=` 含分号 / `srv=`/`nsu=`);③**OpenTcp**——接收超时归 TIMEOUT、`length_prefix` 发送补前缀、`max_frame` 上界 16MiB;④**Modbus FC20** 子响应边界校验(坏帧不再裸 `IndexError`);⑤**aio `close()`** 不再吞 `CancelledError`(3.7 专属)。门禁 1169 → **1179**。
- **二轮复审 P2 批 3 + P3(2026-09-26)**:①**MC `read_batch`** 非 BOOL 位号入参期拒绝;②**MEWTOCOL** 按字段宽度校验(字号 3 位/位号 1 位/起止 5 位/字值 16 位);③**convert** 整数编码越界/非整数改抛 `ValueError`(不再 `struct.error`/静默截断);④**`check_byte_field`** 改 `require_int`(拒绝 bool/float/str);并记录 Modbus `read_many` 跨度校验已随 P2 批 1 修复。门禁 1179 → **1183**。
- **二轮复审 P3 批 4(2026-09-26)**:①**MC 1E** 响应尾部严格长度校验;②**Modbus** FC20/21 记录长度上界 0x7D、FC08/11 响应定长(拒尾字节)、位写仅线圈/保持寄存器(输入寄存器/离散输入前置拒绝、零字节下发);③**`__description__`** 与 pyproject 描述同步。门禁 1183 → **1189**。
- **二轮复审 工程批 5(2026-09-26)**:①**Modbus FC43** 增 `expected_read_code` 回显校验(回归 `test_device_id_response_read_code_echo_validated`,同步订正 3 处测试夹具读取码回显);②**工程配置**——`pyproject` mypy `python_version` 3.9→3.10(注释说明 3.7 由 ruff py37 + 3.7.9 测试腿兜底)、`Development Status` Alpha→Production/Stable、`dev` extra `comtypes` 补 `platform_system == 'Windows'`;③**文档**——`CONTRIBUTING.md` 测试计数/mypy 目标、`architecture.md` §6.2 与 §10 静态检查描述订正。门禁 1189 → **1190**。
- **二轮复审 P3 批 6(2026-09-26)**:①**Modbus FC07** 新增 `read_exception_status`(codec 构造/解析 + 客户端 + aio 镜像 + 原生 pending);②**RTU 增量收包长度 cap**——FC12/FC24 的 byte count 按 `MODBUS_RTU_MAX_ADU_SIZE=256` 封顶,越界按坏帧拒绝(不再发起超大 recv)。门禁 1190 → **1193**。
- **二轮复审 MX 批 7(2026-09-26)**:`MxComponent.read_batch` 支持 32/64 位条目——16 位(BOOL/SHORT/USHORT)仍合并 `ReadDeviceRandom` 单笔;32/64 位(INT/UINT/FLOAT/LONG/ULONG/DOUBLE)按条目各发一笔 `ReadDeviceBlock`(地址原文透传,控件内部递增字号),同一锁内完成、顺序保持。**据手册 5.2.18/5.2.20 勘误**:`ReadDeviceBlock2`/`ReadDeviceRandom2` 是 16 位 SHORT 版而非 32 位,故实现走非 2 的 LONG 版块读。回归 `test_read_batch_wide_types`。门禁 1193 → **1194**。
- **二轮复审 OPC-UA 线程安全批 8(2026-09-26)**:①`BaseClient` 增独立**状态锁** `_state_lock`(与事务锁 `_lock` 分离,短临界区不涉 I/O),护 `_set_error`/`_clear_error`/`_record_error`/`stats`;订阅回调在 asyncua 线程写 `last_error`/`error_count` 不再丢计数或阻塞事件循环;②`OpcUaSubscription.unsubscribe` 增 `_unsub_lock`,并发首取消原子化。回归 `test_set_error_counter_is_thread_safe`、`test_unsubscribe_is_thread_safe`。门禁 1194 → **1196**。
- **工程批 9(2026-09-26)**:引入 **pytest-timeout** 防卡死——`dev` 依赖加 `pytest-timeout>=2.1`(解析 2.4.0,3.7 可用),`[tool.pytest.ini_options]` 设 `timeout=120` / `timeout_method=thread`(全局兜底,慢链路用例可就近 `@pytest.mark.timeout` 放宽);`CONTRIBUTING.md` 门禁说明同步。门禁仍 1196(纯门禁加固,无新用例)。
- **三轮复审 P0(2026-09-27)**:MC 定时器/计数器设备码修正——`MC_DEVICE_CODES` `TC 0xC2→0xC0 / TN 0xC3→0xC2 / CC 0xC5→0xC3 / CN 0xC6→0xC5`(依 SH080008 §8.1;原连续 C1..C6 排列致 3E/4E/4C 下 `TN`/`CN` 静默错区),`MC_1E_DEVICE_CODES["S"]` `0x5320→0x4D20`(1E `M/L/S` 同码);新增手册门禁 2 条。门禁 1196 → **1198**。
- **三轮复审 文档/引用纪律(2026-09-27)**:协议实现 × 文档要求审计并补足——MC 依据 `SH-080956`→`SH-080008`(`codec_qna.py:3`、`melsec.py:64`、`architecture.md:539/931/952`);FINS 依据 `W340`→`W342`(`omron/codec.py:3`、`core/constants.py`、`architecture.md` 5 处);`keyence/mc.py` `SH081257ENG`→SLMP `SH-080956`+待核;Pro-face 端口来源标第三方待核;**10 个文件补页码级引用**(地址解析 4 个 + S7/OPC-UA/MTConnect/SR/CIP/AB 封装);`CONTRIBUTING.md` 铁律补两条硬约束 + PR 列出依据手册编号+页码,CI 矩阵措辞订正。
- **三轮复审 MX 专项(2026-09-27)**:①P1 `MX_SUPPORT_MSG_PROG_ID` `"ActUtlType.ActSupportMsg"`→`"ActSupportMsg.ActSupportMsg"`(手册 §1.2.1);②P2 `_new_support_msg_com` 创建失败翻译为 `OmniPLCInternalError`(公共 API 收口);③P3 `MX_BIT_DEVICES` 去 `"ST"`、非 BOOL 位号/位软元件访问入参期拒绝(`_reject_non_bool_access`)、`write_batch` 位值限 0/1、`MX_MAX_BLOCK_WORDS` 注明驱动自定。新增门禁 7 条。当前全量门禁 **1212 passed**。
- **三轮复审 三菱 MC 专项(2026-09-27)**:①`MC_DEVICE_CODES["ZR"]` base 10→**16**、`MC_1E_DEVICE_CODES["X"/"Y"]` base 8→**16**(依 SH-080008 §8.1/§18.4);②MC `_read_string`/`_write_string` 拒绝 `.bit`;③常量注释订正(4E 响应 `D400`、1E 点数上限);④**补漏**——0406 响应总量入参期校验(超 8192 拒绝并提示拆分)、1E 字访问位软元件须首编号 16 倍数、1E 设备编号域注释订正。新增门禁 4 条。当前全量门禁 **1216 passed**。
- **二轮复审 MX 控件释放(2026-09-27)**:`MxComponent.get_error_message` 的 ActSupportMsg 控件改**配对释放**——创建点与 `_com_get_error_message` 双 `finally` 置空引用,成功/失败路径都即时回收 STA 代理(异常路径此前由 traceback 帧把引用扣到 GC);byref 回退路径移出探测异常处理块,探测期 TypeError 不再作为 `__context__` 挂链;控件维持**每次新建**(刻意的:STA 控件绑定创建线程,缓存跨线程复用不可用)。回归 `test_mx_clients.py::test_com_get_error_message_releases_reference_on_error`(旧实现实测 FAILED)+ `test_get_error_message_releases_support_msg_control`。门禁 1198 → **1200**。
- **二轮复审 Modbus FC17(2026-09-27)**:新增 **FC17 `report_server_id`**(报告从站 ID,规范 §6.13、印刷页 31,依 PyMuPDF 抽取手册文本核对)——codec 构造/解析(长度域与实收严格一致、下限 2 = 从站 ID + 运行指示)、客户端 + aio 镜像、**RTU 按 byte count 增量收包**并随 FC12/FC24 同口径按 `MODBUS_RTU_MAX_ADU_SIZE=256` 封顶;`expected_response_length` 对 FC17 显式抛错(长度随附加数据变化);原生层维持 pending(与 FC07 同)。回归 5 例(codec 1 + 客户端/RTU/aio 4);README 示例与待真机表、全协议矩阵同步。同批订正 README ADS 行残留的旧错误码集描述(0x705/0x706/0x725 → TE1000 §8 码集)。门禁 1200 → **1205**。
- **三轮复审 P1 批(2026-09-27)**:①**`write_tag` 恒等缩放回归**——恒等分支保留「整数值 float → int 还原」,`write_tag(整数点位, 5.0)` 恢复写 5(回归 `test_write_tag_identity_scale_restores_float_to_int`,旧实现实测 FAILED);②**native 镜像恒等缩放**——`native/base.py` `read_tag`/`write_tag` 各补恒等分支(64 位直通不过 float64),并入同步/异步对拍用例 `read_tag_identity_long`/`write_tag_identity_long`(Selector/Proactor 双循环,旧实现 4 例 FAILED);③**Modbus 区域×类型匹配**——`_check_address`(同步/native 共用收口点)对字类型仅放行 hr/ir,`read_short("c0")` 类错区请求入参期拒绝、零字节发送(回归 `test_word_type_rejects_bit_area_before_any_frame`,旧实现 DID NOT RAISE);BOOL 不限区域(hr.3 读词提位/读-改-写为既有语义);④**TOYOPUC 负 32/64 位写**——`_write_raw` 负值按二补数转无符号再编码(回归 `test_tcp_write_negative_int_and_long`,旧实现 OverflowError)。门禁 1216 → **1223**(+7:同步 2 + Modbus 1 + TOYOPUC 1 + native 对拍 Selector/Proactor 双循环读/写 4 −1 归并)。
- **三轮复审 P2 批 1(2026-09-27)**:①**AB `ListIdentity` 解析重写**——CPF 标准布局(ItemCount+Type 0x000C+Length+EncapVer 2+SocketAddr 16,Vendor ID 自载荷第 48 字节起),Item 头非法按坏帧拒绝;依据 Rockwell Explicit Messaging Guide p.20-21 + pycomm3 1.2.16 参考实现裁决(旧「2 字节兼容前缀」口径经实测裁决系误判,夹具一并订正,结论记 architecture.md §8.1);②**OPC-UA `active_subscriptions` 改状态锁**——快照与订阅索引增删全部走 `_state_lock`,`ua_sub.delete()` I/O 锁外;③**OPC-UA 读侧越界改 `DeviceError(code=0)`**——服务端返回越界属设备条件(写路径仍 `ValueError`)。回归 3 项(旧实现均 FAILED)。门禁 1223 → **1225**。
- **逐协议专项 Modbus(2026-09-27)**:依 Modbus 应用协议 V1.1b3 手册逐功能码核对(§6.5-§6.21 印刷页 17-44),核对通过项与 6 项修复见「Modbus 专项」小节——写响应回显校验(FC05/06/15/16,§6.5/6.6/6.11/6.12)、FC21/FC08-0x000A 广播合法化、FC12 byte count ≤0x46(§6.10 印刷页 27)、批量写只读区入参期拒绝(`_check_address` 增 `is_write`)、RTU FC24 变量名订正;汇川夹具非规范 FC16 回显一并订正。回归 7 例(4 例旧实现 FAILED)+ 汇川 1 例订正。门禁 1225 → **1231**。
- **逐协议专项 三菱 MC 串口(2026-09-27)**:依 SH-080008 §4/§17/Appendix 5/Appendix 7 逐字节核对 1C/3C/4C 三层实现(帧格式/控制码/识别码/和校验范围含 ETX/ACK·NAK 长度/1C 六上限/256 点传 00/附加码),**全部一致,无行为缺陷**;Appendix 7 黄金向量 7 例已逐字节在库。文档订正 2 处(`MC_MAX_TRANSFER_POINTS` 注释改述 3E/4E 与 3C/4C 共用口径并引 Appendix 5 印刷页 466 上限 960/480、codec_serial docstring 补黄金算例页码引用)。无新增用例,门禁仍 **1231**。
- **MC 能力补齐批(2026-09-27)**:新增三项 QnA 兼容帧能力(依 SH-080008,黄金样本探针逐字节核证后固化)——①**随机读 0403**(`random_read`:乱序不连续软元件单事务,字访问 + 双字访问分节,响应字/双字分节解析;上限 192 点,双字 = 32 位,限 INT/UINT/FLOAT);②**随机写 1402**(`random_write`:乱序写,加权点数 字×12+双字×14 ≤1920,无响应数据);③**CPU 型号 0101**(`get_cpu_type`:模型名 16 字节 + 代码 2 字节小端,Q02UCPU → 0x6302,§11.2 印刷页 176-178)。codec 层新增 6 函数(`codec_qna.build_random_read_devices/parse_random_read_devices_response/build_random_write_devices/build_read_cpu_model/parse_read_cpu_model_response/_random_device·_random_word_value/_decode_dword`)+ 常量 4 项;3E/4E 客户端 + aio 三类镜像(TCP/UDP/串口)对齐;**0701/0702 时钟命令经 SH-080008 与 SLMP 手册全文检索均无收录,按铁律不臆造,登记 `docs/protocol/README.md` 待补表**(缺 QCPU 用户手册基础系统篇)。回归 14 例(codec 7 + 客户端 7)。门禁 1231 → **1245**。
- **逐协议专项 AB EtherNet/IP(CIP)(2026-09-27)**:依 Rockwell EM Guide p.22 + CIP Vol 1 章节号,并以 pycomm3 1.2.16 / pylogix / OpENer 参考实现对照裁决——①**P1 Get_Attribute_List(0x03)应答逐项布局错**(原按 `属性数 + 每项[长度 u16][值]` 解,实为 `属性数 u16 + 每项[属性号 u16][状态 u16][值]`,值不带类型码与长度域;真机上 `get_attribute_list()` 必解码失败):解码器契约改「收剩余字节 → `(值, 已消费字节数)`」、逐项状态非 0 返 `None`、Identity 属性 1~7 **任意子集**走内置解器、非 Identity 原样返项区字节;②**P2 附加状态 16 位字步进**(`_parse_service_payload`/`parse_service_reply`/`parse_forward_open_reply` 三处 `4+N` → `4+2N`,统一 `_service_data_offset()` 收口,三轮 §2.4.7 结案);③**P3** 类型码 0xC9 `LWORD`→`ULINT`、Forward Open 连接尺寸构造期校验(普通 9 位/Large 16 位)。UC-Send 信封、Forward Open/Close、SendUnitData 逐字段与参考实现一致(无改动,核证结论记 architecture.md §8.1)。回归 8 例(codec 6 + 客户端 2,旧实现全 FAILED)+ 尺寸校验 1 例。门禁 **1270 passed**(本批 +8;同机另有 7 个 `test_opcua_client.py` 失败属另一代理在途 WIP)。
- **逐协议专项 汇川(2026-09-27)**:依 H3U 手册 9.4.3(印刷页 575-576)与 H5U&Easy 手册 9.5.1/16.4(印刷页 418-419/661)逐项核对地址表与 MC 码表——①**P2 X/Y 上限**由 256(H3U 口径)改 **1024**(H5U X0~X1777,0xF800-0xFBFF/0xFC00-0xFFFF),解开 H5U 上 X400~X1777 被误拒;②**P2 C200~C255 32 位计数器**补字访问——新增 `INOVANCE_C32_BASE/FIRST/LAST`(0xF700 起、每只占两个 16 位寄存器,手册算例 C205 → 0xF70A)+ `to_modbus_address` 双寄存器展开 + `check_counter_word_type` 32 位类型门控(INT/UINT/FLOAT;手册注明 32 位寄存器不支持 FC06,写路径落 FC16);③**P3** 串口默认停止位口径订正(8N2 是 H5U 默认、H3U 手册示例为 8N1)+ 机型差异(§2.8.1)与 MC 点数码表(§2.8.4)文档化;§2.8.3 结案为文档化边界(厂商私有 FC 无手册依据,自由协议帧走 `OpenTcpClient`)、§2.8.5 已核证(R → D8000 与手册一致)。回归 5 例(codec 3 + 客户端 2,前 4 例旧实现 FAILED)。门禁 **1290 passed**(本批 +5)。

---

## 二轮复审(2026-09-26,全量)

> 范围:76 个源文件 / ~21k 行,分 6 域(核心 / 异步 / Modbus+OpenTcp / MC 家族 / Omron·AB·S7·ADS / OPC-UA·MTConnect·工程)。
> 方法:逐行读源 + 只读探针(Python 3.7.9)+ 与 pyads/asyncua 安装源比对。标「已核验」者为二轮亲自复现。
> 结论:新增 **1 条 P0、4 条 P1** 及若干 P2/P3;并纠正上一轮 **5 条已关闭项 / 3 条记录不实**。其中 P0(MC `L` 撞码)与 **P1 四项(ADS 码集 / S7 WString ASCII / Tag 64 位精度 / aio 属性读锁)均已于 2026-09-26 修复**,P2 前七项(native 镜像 / Modbus 批量 `.bit` / `is_write` / FINS 批量 `.bit` / AB 0x0A 预算 / MTConnect DOCTYPE / 退避溢出)同日修复(见「修复记录」与各条「修复」小节);另 P2 批 2(convert / OPC-UA / OpenTcp / Modbus FC20)与 P3 的 aio `close()` 取消吞没亦同日修复;P2 批 3(MC `read_batch` `.bit`、MEWTOCOL 字段边界)与 P3 的 convert 编码越界、`check_byte_field` 收窄亦同日修复。

### P0

- **三菱 MC `L` 设备码 0xA0 与 `B` 撞码 → 静默误寻址(已核验)——已修复(2026-09-26)**
  - `core/constants.py:252` `"L": (0xA0, 1, 10)` 与 `:254` `"B": (0xA0, 1, 16)` 同码;`L` 正确值为 **0x92**(同库 `INOVANCE_MC_DEVICE_CODES["S"] = (0x92, …)` 的注释「按三菱 L(92h)」即为反证)。
  - 3E/4E/4C 二进制路径下所有 `L` 读写实际落入链接继电器 `B` 空间且无报错;3C/1C(ASCII 走码名)不受影响。
  - 由 v0.41.0「设备码表扩容」引入,`L` 零测试覆盖;§2.2.3 台账仅标 TN/CN 待核,遗漏此条。
  - **修复**:`MC_DEVICE_CODES["L"] = (0x92, 1, 10)`;新增门禁 `test_mc_codec.py::test_mc_device_code_table_l_is_92_and_no_collisions`(`L=0x92` 且表内无重码)。

### P1

- **ADS transport 错误码集合错误,§2.5.1 名为已修实为未修(已核验)——已修复(2026-09-26)**
  - `plc/beckhoff/ads.py:98` `_ADS_TRANSPORT_ERROR_CODES = {0x705, 0x706, 0x725}`,`:119` 据此决定是否断线重连。
  - pyads 3.5.1 `errorcodes.py`:`0x705`=「parameter size not correct」、`0x706`=「invalid parameter value(s)」属**参数错误**;真正的通断码为 `0x06`(Target port not found)/`0x07`(Target machine not found)/`0x0D`(Port not connected)/router `0x0500~0x050D`。
  - 后果:TwinCAT 重启/路由丢失**不触发重连**;普通参数错误反而**误判断线重连**。v0.41.0 CHANGELOG 将本条列为已修,记录不实(2026-09-27 核订:v0.42.0 的 ADS 条目仅 `set_timeout` WARNING,误记「已修」实为 v0.41.0 条目 (7))。
  - **修复(2026-09-26,依 TE1000 §8)**:码集改为全局组 `0x06/0x07/0x0D/0x12/0x1B/0x1D` + Router 组 `0x0500~0x050D`;`0x705/0x706/0x725` 回归 `DeviceError`。回归测试 2 条。

- **S7 `read_wstring` 对纯 ASCII/拉丁文本返回空串(已核验)——已修复(2026-09-26)**
  - `plc/siemens/client.py:532` 调 `convert.decode_string(data[4:4+actual*2], "utf-16-be")`;`convert.py:280` 先 `data.split(b"\x00", 1)[0]`。UTF-16BE 的 ASCII 字符高字节恒为 `0x00`(`"AB" == b"\x00A\x00B"`),split 后为空 → 返回 `""`。
  - 现有 `test_wstring_roundtrip` 仅用中文(无 `\x00`),未覆盖。§2.6.6 修复遗漏 ASCII。
  - **修复(2026-09-26)**:`decode_string` 对 UTF-16/UTF-32 改在**解码后**按首个 NUL 字符截断;单字节编码维持字节层截断。回归 `test_convert.py::TestString::test_utf16_ascii_not_truncated_by_high_nul` + `test_wstring_ascii_roundtrip`。

- **`read_tag` / `write_tag` 让 64 位整数经 float64 往返,静默丢低位(已核验)——已修复(2026-09-26)**
  - `core/base_client.py:626` `value * resolved.scale + resolved.offset`、`:639` `(value - offset) / scale`;即便 `scale=1.0/offset=0.0` 也强制过 float64。
  - `LONG/ULONG` 且值 > 2^53 时静默丢精度;32 位类型安全。标签常由 CSV/JSON 省略 scale,故可达。
  - **修复(2026-09-26)**:`scale=1.0/offset=0.0` 时原值直通(读返回原值、写不逆缩放),非恒等缩放仍走 float。回归 `test_base_client.py::TestTagScaling::test_{read,write}_tag_identity_scale_preserves_int64`。

- **aio 属性读仍进入同步事务锁,阻塞事件循环(§3.1,复核仍在,已核验)——已修复(2026-09-26)**
  - `aio/__init__.py:223` `return self._sync.connected` → `core/base_client.py:250` `with self._lock`;`connected / last_error* / stats / next_connect_in` 与 timeout setter 同病。
  - 探针:持锁 0.5s 期间 `aio_client.connected` 卡住事件循环 0.458s。修复记录未触及。
  - **修复(2026-09-26)**:上述只读 getter 改**无锁快照**(`stats` 依赖固定键集,值级拷贝 GIL 安全);timeout **setter** 仍持锁(需串行化传输层写)。回归 `test_property_reads_do_not_block_on_transaction_lock`。

### P2

- **native 未镜像已有同步修复(已核验)——已修复(2026-09-26)**:`native/modbus.py:162/173` 的 `_read_string/_write_string` 补 `.bit` 拒绝;`native/omron.py` `_read_bit_impl` 补 0x1101 D/EM 字读提位回退(镜像同步侧)。回归 `test_native_modbus.py::test_string_rejects_bit_suffix`、`test_native_omron.py::test_d_area_bit_read_falls_back_on_1101`。
- **Modbus `read_many`/`read_batch` 对非 BOOL 静默忽略 `.bit`(已核验)——已修复(2026-09-26)**:两入口改用 `_check_address`(同时补齐地址跨度校验)。回归 `test_read_many_rejects_bit_suffix_for_non_bool` / `test_read_batch_rejects_bit_suffix_for_non_bool`。
- **写语义操作漏 `is_write=True`(已核验)——已修复(2026-09-26)**:`modbus/modbus.py` FC23 / FC08-A 清计数 / FC21、`opentcp/client.py` `transact`/`transact_text` 补 `is_write=True`(FC08 仅 `0x000A` 视为写)。回归 `test_write_semantics_gate_retries`、`test_transact_uses_write_retries`。
- **FINS `read_batch` 非 BOOL 静默忽略 `.bit`——已修复(2026-09-26)**:`plc/omron/omron.py` 读批量入参期拒绝非 BOOL 位号。回归并入 `test_read_batch_rejects`。
- **AB 0x0A 分块预算漏算偏移表(已核验)——已修复(2026-09-26)**:`plc/ab/ab.py:819` 每条 `entry` 补 2 字节偏移项。回归 `test_chunk_batch_requests_counts_offset_table`(15×30B 须拆块)。
- **MTConnect DOCTYPE 防护可被 UTF-16 绕过(已核验)——已修复(2026-09-26)**:`cnc/mtconnect.py` 同时扫描剔除 NUL 字节后的形式(响应体上限在 `_read_body` 读取期已强制,先于解析)。回归 `test_doctype_utf16_payload_rejected`。
- **连接退避指数无界(已核验)——已修复(2026-09-26)**:`core/base_client.py:730` 指数先按 `RECONNECT_BACKOFF_MAX_EXPONENT=32` 封顶再算。回归 `test_backoff_exponent_bounded`。
- **MC `read_batch` 字软元件 `.bit` 静默忽略——已修复(2026-09-26)**:`plc/melsec/melsec.py` 非 BOOL 带位号入参期拒绝(与单点 read 一致),回归 `test_read_batch_rejects_bit_suffix_on_word_type`。**MEWTOCOL 字段溢出——已修复(2026-09-26)**:`codec_mewtocol.py` 单接点(字号 3 位/位号 1 位)与数据区(起止 5 位/字值 16 位)按字段宽度校验,回归 `test_codec_field_width_bounds`。**MX 32/64 位批读未支持 —— 已修复(2026-09-26)**:`read_batch` 16 位条目仍合并 `ReadDeviceRandom`,32/64 位条目改按条目各发一笔 `ReadDeviceBlock`(地址编号原文透传、控件内部递增字号,规避进制未知导致的相邻字推导),同一 `_execute` 内完成、顺序保持;回归 `test_read_batch_wide_types`。**勘误**:原「手册复核」称 `ReadDeviceBlock2`/`ReadDeviceRandom2` 为「32 位版」——经 MX Component 手册 5.2.18/5.2.20 核对,二者标注「**2 字节数据**」、出参 `Short`,实为 **16 位(SHORT 元素)版本**;非 2 版(5.2.3/5.2.5)才是 LONG 元素。本次实现据后者,未使用 `...2` 变体。
- **`convert`(已核验)——已修复(2026-09-26)**:`_reorder_bytes` 校验/归一化 `word_order`(非法抛 `ValueError`,字符串按枚举归一化,不再静默走 BADC);`registers_to_int32/uint32/int64/uint64/float32/float64` 校验寄存器数量。回归 `TestWordOrder32::test_bad_word_order_rejected` + 计数回归。
- **OPC-UA——已修复(2026-09-26)**:`browse` 在 `max_depth=None` 时应用 `OPCUA_BROWSE_DEFAULT_MAX_DEPTH=10` 安全上限;`_coerce_read` 按声明类型做整数范围收窄(与写路径对称);NodeId 解析改组件切分,`s=` 吞掉其余(可含 `;`/`=`)、支持 `srv=`/`nsu=`。回归 `test_coerce_read_narrows_integer_range`、`test_browse_none_max_depth_uses_default_cap`、`test_parse_nodeid`。
- **OpenTcp——已修复(2026-09-26)**:接收超时改抛 `TransportTimeoutError`(归类 TIMEOUT 而非 DEVICE);`length_prefix` 发送端补长度域(与收帧对称);`max_frame` 增上界 `OPEN_TCP_MAX_FRAME_LIMIT=16MiB`。回归 `test_receive_timeout_keeps_connection`、`test_send_text_prepends_length_prefix`、`test_max_frame_upper_bound_rejected`。
- **Modbus FC20 坏帧抛裸 `IndexError` 逃逸——已修复(2026-09-26)**:`modbus/codec.py` 引用类型字节读取前先做子响应长度边界校验。回归 `test_fc20_truncated_subresponse_is_frame_error`。

### P3(合并)

- `convert` 越界整数编码抛 `struct.error` 而非 `ValueError` — **已修复(2026-09-26)**:`int32/uint32/int64/uint64_to_registers` 先 `require_int` + `check_range`,回归 `test_encode_overflow_raises_value_error`。`words_to_bytes`/`registers_to_canonical` 对超范围字静默 `&0xFFFF`(`convert.py:129/318`)仍待办。
- `core/validation.py:60` `check_byte_field` 用 `int()` 收窄,静默接受 float/bool/str — **已修复(2026-09-26)**:改 `require_int`,回归 `test_check_byte_field_rejects_non_int`。
- Modbus:~~`read_many` 地址跨度溢出不满足「零字节发送」~~(**已随 P2 批 1 的 `_check_address` 一并修复**);~~file-record 长度无上界~~ / ~~FC08/11 接受尾字节~~ / ~~`write_bool("ir0.0")`/`di` 先发一次读再抛~~(**已修复 2026-09-26**:记录长度上界 0x7D、FC08/11 响应定长、位写区域前置校验);~~FC43 不回显读码~~(**已修复 2026-09-26**:`parse_device_id_response` 增 `expected_read_code` 校验回显,回归 `test_device_id_response_read_code_echo_validated`);~~缺 FC07~~(**已新增 FC07 `read_exception_status` + aio 镜像 + 原生 pending,2026-09-26**);~~缺 FC17~~(**已新增 FC17 `report_server_id`(规范 §6.13,印刷页 31)+ aio 镜像 + RTU 按 byte count 增量收包(ADU 上限封顶),2026-09-27**);~~RTU 增量长度字段无 cap~~(**已修复 2026-09-26**:FC12/FC24 增量长度按 `MODBUS_RTU_MAX_ADU_SIZE=256` 封顶,回归 `test_rtu_incremental_length_capped`)。
- OpenTcp:缓冲头 `del self._buffer[:n]` O(n);无发送进度回调;无 IPv6(§2.13.4/6/8)。
- ~~MC 1E 帧不校验尾多余字节(`codec_a.py:139`)~~(**已修复 2026-09-26**:尾部长度严格匹配,回归 `test_parse_response_1e_trailing_bytes_rejected`);~~MX `get_error_message` 新建 COM 控件不释放(`mx.py:877`)~~(**已修复 2026-09-27**:创建点 + helper 双 `finally` 置空引用,异常路径不再经 traceback 扣留 STA 代理;回归 `test_com_get_error_message_releases_reference_on_error`,旧实现 FAILED);`MX_SUPPORT_MSG_PROG_ID` 存疑(`constants.py:679`)。
- TOYOPUC 打包段校验口径不一致(`toyopuc/address.py:133` vs `:141`)。
- OPC-UA:~~回调线程无锁写 `last_error`(订阅回调在 asyncua 线程调用 `_set_error`)~~(**已修复 2026-09-26**:`BaseClient` 新增独立**状态锁** `_state_lock`,仅护错误三件套/计数(`_set_error`/`_clear_error`/`_record_error`/`stats`),短临界区不涉 I/O,不与可能被长事务持有的 `_lock` 争用;回归 `test_set_error_counter_is_thread_safe`);~~`unsubscribe` 非线程安全~~(**已修复 2026-09-26**:`OpcUaSubscription` 增 `_unsub_lock`,首次取消 check-then-set 原子化,回归 `test_unsubscribe_is_thread_safe`);`browse` 单点失败吞成 `{}`;aio 回调异常不落 `last_error`(与 README:485 矛盾);`browse` 仅 Hierarchical。
- MTConnect:缺 `/sample`/`/asset`;`/probe` 仅首 Device;条件项无过滤;空元素 UNAVAILABLE 误报「不存在」;keep-alive 异常集偏窄(`mtconnect.py:49`)。
- S7 缺多变量批读;ADS 缺 >32KB 分块与句柄失效(0x1D)处理。
- 工程:~~`pyproject.toml:112` `python_version="3.9"` 与运行时 3.7.9 错位~~(**已修复 2026-09-26**:改 `3.10`——mypy 已不支持 <3.10,3.7 语法由 ruff target-version=py37 兜底并注释说明);ruff 未含 B(暂缓);~~`Development Status Alpha`~~(**已改 5 - Production/Stable**);~~`dev` extra 的 `comtypes` 缺 `platform_system`~~(**已补 `; platform_system == 'Windows'`**);~~无 `pytest-timeout`~~(**已加 2026-09-26**:`dev` 依赖 + `[tool.pytest.ini_options] timeout=120 / timeout_method=thread`,防卡死);~~`CONTRIBUTING.md:9/36/38` 数字 stale~~(**已订正**:测试计数、mypy 目标与语义);~~`src/omniplc/__init__.py:79` `__description__` 未同步~~(**已修复 2026-09-26**,回归 `test_description_covers_supported_protocols`);~~`docs/architecture.md:687` OpenTcp 长度域「留 v1.x」过时~~(**已订正**)、`§6.2` 静态检查描述过时(**已订正**,含 §6.2 与 §10 两处)。

### 台账纠偏(二轮)

**复核关闭(原条不成立):**

| 原条 | 复核结论 |
|---|---|
| §3.5 close 先置 `_executor=None` | 不可能命中:`_ensure_open` 与 `run_in_executor(self._executor, …)` 相邻无 `await`,进行中操作从不读 `_executor`;`test_close_gate_refuses_new_calls_while_draining` 已锁行为 |
| §3.9 UDP `peer_ip` 回退走阻塞 DNS | `AsyncUdpTransport.connect` 总给出字面量 `peer_ip`,`omron.py:454` 取到的必是 IP,永不解析 |
| §3.12 取消计入 `disconnect_count` | 取消时 `pending=True` 确可能留下无配对响应,拆连计数正确;`test_native_modbus.py:288` 显式断言 `disconnect_count == 1` |
| §2.2.13 `SetClockData` 忽略 `day_of_week` | `mx.py:361` 已透传,`test_get_set_clock` 断言 7 元组 |
| §2.11.3 NodeId 不识别 `t=`/`n=` | 误报:asyncua 1.1.5 `uatypes` 本身也只认 `ns/i/s/g/b/srv/nsu`;真实缺口是 `s=` 含分号与 `srv=`/`nsu=`(见 P2) |

**记录不实(需订正):**

- §2.5.1 标「已修复」实为错误码集合未修正(见 P1)。**已订正并修复(2026-09-26)**。
- §2.3.3 修复记录称「批量读经 `_batch_bool_array_address` 定制点同口径」,但 `OmronCipClient` 从未覆写该方法;实际机制是 `ab.py:645-649` 的自描述类型分支。功能无误,记录失实。
- §2.13.1/2「长度域成帧留 v1.x」与 v0.42.0 已落地的 `length_prefix` 不符(`docs/architecture.md:` 描述过时)。**已订正(2026-09-26)**:架构文档改述三种成帧 + 收发双向对称。

### 一致性 / 无问题

- **版本五落点一致**:pyproject `0.42.0`、`__init__.__version__` `0.42.0`、CHANGELOG 首条 v0.42.0、architecture 头 v0.42.0、uv.lock `omniplc 0.42.0`、tag `v0.42.0`。
- **Python 3.7**:全量 `py_compile` 通过,无 3.8+ 语法/API。唯一 3.7 特有缺陷:`aio/__init__.py:468` `except Exception` 在 3.7 会吞掉 `CancelledError`(3.7 里它是 `Exception` 子类),使取消 `close()` 返回正常;3.8+ 不受影响。**已修复(2026-09-26)**:该处先 `except _CANCELLED_ERRORS: raise` 再捕 `Exception`,取消向上传播;回归 `test_close_propagates_cancellation`。
- `modbus/address.py`、`core/errors.py`、`types.py`、`transport/base.py` 无新发现。

### 手册复核(docs/protocol,2026-09-26)

以 `docs/protocol/` 收录的官方手册/规范复核二轮「待核证」项(文本层用 pypdf 抽取;FINS W342 为扫描件、无文本层):

- **§2.2.3 / 二轮 P0 `L=92H`(SLMP 手册)**:**「Latch relay (L) L*** … (92H)」** —— 独立证实 P0 修复值正确。
- **§2.2.6 0406(SH-080008 §8.4 + §8.1)**:子命令 0000 标准 / 0080·0082 设备扩展;「bit device is **16-bit for one point**」;位字打包**首软元件在 bit15**(M10~M14 首点占最高 nibble,32 点 M16~M47 的 `AB1234CD` 以 M16 为最高 nibble);块数上限 `01H~78H`(1~120)。→ 与本库 `codec_qna.py:13` 口径一致,二轮「只能由黄金向量自洽」的疑虑**消除**。
- **§2.5.1 ADS 错误码(TE1000)**:`0x6`=Target port not found、`0x7`=Target machine not found、`0xD`=Port not connected、`0x12`=Port disabled、`0x1B`=Host unreachable、`0x1D`=TLS send error;Router 组 `0x500+`;而 **`0x705`=ADSERR_DEVICE_INVALIDSIZE「Parameter size not correct」**(`0x706`=Invalid data / `0x725`=License expired 同属设备语义)。→ 手册独立印证二轮 P1:原码集把「参数尺寸」当 transport,漏了真通断码。**已据此修复(2026-09-26)。**
- **§2.2.11 MX 批量读(MX Component 手册)**:原记「`ReadDeviceBlock2`/`ReadDeviceRandom2` 为 32 位版」**有误**——手册 5.2.18/5.2.20 标题为「(2 字节数据)」、出参 `Short`,是 **16 位(SHORT 元素)版**;LONG 元素版为非 2 的 5.2.3/5.2.5。功能缺口结论仍成立(原库拒绝 32/64 位批量),已用**非 2 的块读**按条目实现 32/64 位批量(见「P2」与「修复记录」)。
- **§2.12.1/2.12.4 MTConnect(Part 1 标准)**:定义 `/probe`、`/current`、`/sample`、`/assets`、`/asset/{id}`。→ 缺 `/sample`、`/asset` 属实。
- **§2.7.8 SR `LON` bank(SR-2000 手册)**:存在 `LON,b`(b=01~16 库编号)。→ 带 bank 的 LON 为设备能力;老固件兼容需按机型。
- **§2.8.2 Inovance C200~C255(H3U 手册)**:C200~C255 为 32 位计数器,占两个 16 位寄存器。→ 型号事实确认。

仍待核(无对应手册或扫描件):FINS W342(§2.3.9 EM bank / §2.3.13-14)、松下 MEWTOCOL(§2.9.2-3)、TOYOPUC(§2.10.x)、基恩士 KV Host Link/MC(§2.7.3-4)、OPC-UA Part 2-6(§2.11.x 细节)。相关手册 PDF 仅本地留存(`.gitignore` 的 `docs/**/*.pdf|pptx|ppt`),索引见 `docs/protocol/README.md`。

---

## 三轮复审(2026-09-27,全量)

> 范围:76 个源文件 / ~22.5k 行(v0.43.0),6 域(核心·异步·native / Modbus+OpenTcp / MC 家族 / Omron·AB·ADS·S7 / OPC-UA·MTConnect·TOYOPUC·工程)。
> 方法:逐行读源 + 只读探针(Python 3.7.9)+ 与 `docs/protocol` 官方手册文本比对;标「已核验」者为三轮亲自复现。
> 结论:新增 **P0 ×1、P1 ×6(含 1 条 v0.43.0 自引入回归)**,及若干 P2/P3。**P0(MC 设备码)已于 2026-09-27 修复**(见「修复记录」);**P1 批 4 条(write_tag 恒等缩放回归 / native 镜像 / Modbus 区域×类型 / TOYOPUC 负写)亦已修复(2026-09-27)**。

### P0

- **MC 设备码 `TC/TN/CC/CN` 错(已核验,手册 SH080008 §8.1 / SLMP)—— 已修复(2026-09-27)** — `core/constants.py` `MC_DEVICE_CODES`。
  - 现值 `TC=0xC2 / TN=0xC3 / CC=0xC5 / CN=0xC6`;手册为 **`TC=0xC0 / TN=0xC2 / CC=0xC3 / CN=0xC5`**(`Timer Contact TS C1H / Coil TC C0H / Current TN C2H`;`Counter Contact CS C4H / Coil CC C3H / Current CN C5H`)。
  - 后果:3E/4E/4C 下 **`TN`/`CN` 静默读到错误区**(分别落 CC/STC 线圈区),`TC`/`CC`(线圈)按字设备位写被 PLC 拒绝。v0.41.0 扩容引入;`no_collisions` 门禁(仅查表内无重码)抓不到跨手册错值。
  - 旁证:同库 `PANASONIC_MC_DEVICE_CODES` 的 `TN=0xC2/CN=0xC5` 与手册一致。
  - **修复(2026-09-27)**:改 `TC=0xC0/TN=0xC2/CC=0xC3/CN=0xC5`,并新增手册门禁 `test_mc_timer_counter_device_codes_match_manual`(锁六码 + 字宽)。
  - **同类(已一并修)**:`MC_1E_DEVICE_CODES["S"]` 由凭空值 `0x5320` 改为手册值 `0x4D20`(1E 表 `M/L/S` 同码),门禁 `test_mc_1e_step_relay_code_matches_manual`。

### P1

- ~~**`write_tag` 恒等缩放回归(v0.43.0 自引入,已核验复现)**~~:**已修复(2026-09-27)**——恒等分支保留「整数值 float → int 还原」;回归 `test_write_tag_identity_scale_restores_float_to_int`(旧实现 FAILED)。原描述:`core/base_client.py:657-662` 为修 64 位精度在 `scale=1/offset=0` 时整段跳过逆缩放,连带跳过「整数值 float → int」还原;`write_tag(整数点位, 5.0)` 现抛 `ValueError`(旧版写 5)。现场「算得 float 再写整数点位」大范围受影响。
- ~~**native 未镜像恒等缩放(已核验)**~~:**已修复(2026-09-27)**——`native/base.py` `read_tag`/`write_tag` 各补恒等分支并入对拍用例(双循环,旧实现 4 例 FAILED)。原描述:`native/base.py:557/570` 的 `read_tag`/`write_tag` 仍强制 float64 往返,64 位丢低位(v0.43.0 同步侧已修,native 漏)。
- **MC `ZR` 进制错(已核验,手册)**:`constants.py:279` `ZR` base 10;手册为 **Hexadecimal**。`ZR100` 实际访问 ZR64,`ZR1F` 抛 `ValueError`。
- **MC 字符串读写忽略 `.bit`(已核验)**:`plc/melsec/melsec.py:202-215` `_read_string`/`_write_string` 不校验 `parsed.bit`,`read_string("D100.3")` 静默读 D100(Modbus/Keyence/MX/MEWTOCOL 均拒)。
- ~~**Modbus 区域×类型不匹配静默错功能码(已核验复现)**~~:**已修复(2026-09-27)**——`_check_address` 收口校验(同步/native 共用),字类型仅 hr/ir,入参期拒绝零字节发送;回归 `test_word_type_rejects_bit_area_before_any_frame`(旧实现 DID NOT RAISE)。原描述:`modbus/modbus.py:115/134` 不校验「字类型仅寄存器区/位类型仅位区」;`read_short("c0")` 发 FC01、`read_int("c0")` 返回 65537;写落到 FC05/15。
- ~~**TOYOPUC 负 32/64 位写抛 `OverflowError`(已核验复现)**~~:**已修复(2026-09-27)**——`_write_raw` 负值按二补数转无符号再编码;回归 `test_tcp_write_negative_int_and_long`(旧实现 FAILED)。原描述:`plc/toyopuc/toyopuc.py:246` `raw.to_bytes(..., signed=False)`;`write_int("D0100", -5)` 逃出公开 API 契约。

### P2

- **aio `active_subscriptions` 仍进事务锁(已核验)——已修复(2026-09-27)**:`opcua/client.py` 的 `active_subscriptions` 快照、订阅索引增删(`subscribe_*`/`_do_unsubscribe`/`disconnect`)全部改走 `_state_lock`(与错误三件套同口径,短临界区纯字典操作);`ua_sub.delete()` 的 I/O 保持锁外。回归 `test_active_subscriptions_does_not_block_on_transaction_lock`(旧实现 FAILED)。原描述:事务在途时读该属性冻结事件循环。
- **`convert.decode_string` 漏 `utf16`/`utf32` 别名(已核验复现)**:`convert.py:317` 前缀只认带连字符写法;`encoding="utf16"` 走单字节路径 → 返回 `\ufffd`。
- **`TagTable` 接受 NaN/Inf `scale/offset`(已核验)**:`tag.py:65` 仅拒 `==0`;JSON `"scale":"nan"` 可把 NaN 写进 FLOAT/DOUBLE 寄存器。
- **TCP `recv` 收窄 socket 超时未复位(已核验)**:`transport/tcp.py:95-112` 残超时被下次 `sendall` 继承,慢链路伪超时断连(OpenTcp 已复位,基础协议未)。
- **AB `ListIdentity` 解析偏移错(已核验)——已修复(2026-09-27)**:`codec_cip.parse_list_identity_reply` 重写为 CPF 标准布局——ENIP 头 24 + Item Count(2)+ Item Type 0x000C(2)+ Item Length(2)+ Encap Version(2)+ Socket Address(16)= **Vendor ID 从载荷第 48 字节起**;Item 计数/类型码/长度域非法按坏帧拒绝。依据:Rockwell《Communicating with RA Products Using EtherNet/IP Explicit Messaging》p.20-21(封装头 24 字节 + CPF Item 结构)+ **pycomm3 1.2.16 `ListIdentityObject` 参考实现裁决**(临时环境安装实测:标准布局帧逐字段解出,旧 2 字节前缀帧 `is_valid=False`);原「2 字节兼容前缀」口径与测试夹具均系误判,一并订正(结论记 architecture.md §8.1)。回归 `test_parse_list_identity_reply_full_fields` 重写 + 新增 `test_parse_list_identity_reply_bad_item`;客户端夹具 `_identity_list_reply` 同步改标准布局。原描述:只跳 2 字节前缀,身份字段全错。
- ~~**ADS transport 码集缺 `0x1A ERR_TCPSEND`(已核手册)**~~:**已修复(2026-09-27,ADS 专项)**——TE1000 §8 p.128 `0x1A ERR_TCPSEND` 补入(原 `0x1B/0x1D` 独缺 `0x1A`,TCP 发送失败当设备错误不断线)。
- **OPC-UA `_coerce_read` 设备越界抛 `ValueError`(已核验复现)——已修复(2026-09-27)**:`_narrow_int` 捕获范围校验的 `ValueError` 转译为 `DeviceError(code=0)`——越界值来自**服务端**属设备侧条件(与"节点值为空"同口径,`_execute` 转 `(False, None)` 不断线);写路径维持 `ValueError`(调用方参数错误)。回归更新 `test_coerce_read_narrows_integer_range`(旧实现 FAILED)。原描述:`opcua/client.py:867` 读侧设备返回越界(如 `read_ushort` 遇 70000)应 `DeviceError` 却抛 `ValueError` 逃出 `_execute`。
- **Keyence SR `bank` 范围错(已核手册)**:`scanner/keyence_sr.py:95` 暴露 0~15;手册 `LON,b` 为 **01~16** → `bank=0` 发非法 `LON,00`、设备 bank 16 被拒。
- 其余 P2:**native 退避指数未封顶**(`native/base.py:670`,`OverflowError` 逃出);~~**ADS transport 错误分类为 UNKNOWN**(`ads.py:135`,应 TRANSPORT)~~(**已修复 2026-09-27**:transport 分支改抛 `TransportClosedError`,归 TRANSPORT);~~**ADS `write(addr, STRING)` 绕过声明长预检**(`ads.py:382`)~~(**已修复 2026-09-27**:STRING 分支路由 `_write_string` 预检);**AB/FINS/MX 非 BOOL `.bit` 未拒/未处理**(`ab.py` read_batch、`omron.py` 字符串、`mx.py` 读;其中 **FINS 字符串已修 2026-09-27** 见 FINS 专项);**1E `X/Y` 八进制 vs 手册十六进制**(`constants.py:291`,待核);**Inovance 0406 未列手册**(`melsec.py:242`,待核);**AB 自定义 STRING<88 仍按 88 写**(`codec_cip.py:1174`);**AB 单条超预算不拆**(`ab.py:818`)。

### P3(合并)

- Modbus:FC20 上界 `0x7D` 非规范(聚合响应无上界,`codec.py:877/896`;**专项复核维持自加宽容**——超限帧交 PLC 侧异常码 02 裁决,客户端不预拦);RTU FC43 增量无 ADU cap(`modbus.py:1409`;**已核 FC43 走对象头增量收包,无此问题**,见 Modbus 专项);~~FC05/06/15/16 写响应不回显~~(**已修复 2026-09-27**,Modbus 专项);~~批量写只读区在锁内才抛~~(**已修复 2026-09-27**);~~FC08 清计数器广播被误拒~~(**已修复 2026-09-27**);异常响应对尾字节宽容(`codec.py:186`);~~FC12 事件 0~64 未限~~(**已修复 2026-09-27**:byte count ≤0x46);掩码/批量值 `int()` 静默截断 float;设备标识翻页非法 `NextObjectId` 抛裸 `ValueError`。
- OpenTcp:`max_frame` 不严格遵守(单分片自带分隔符可超)、`recv_chunk_size` 无上界、长度前缀 `to_bytes` 可溢出发送端。
- native:`__aexit__` 用 `disconnect()`(aio 用 `close()`);UDP `close` 在无当前 loop 时可能漏关 FD。
- 工程:`docs/architecture.md` §6.2/§10 与 `CONTRIBUTING.md:9` 仍称「CI 裸 mypy / 仅 3.12」与 3.7.9+3.12 矩阵不符(review 台账曾误标已订正;已订正 2026-09-27);`[tool.mypy] files=["src","tests"]` 因显式路径而失效(tests 实际未检查);MTConnect 单点读每次拉整份 `/current`;~~**S7 `_write` 未支持类型抛 `KeyError`、docstring 引用不存在的 `DataType.BYTE`**~~(**已修 2026-09-27**:_SIZES 校验 + STRING 路由预检 + docstring BYTE 引用修,S7 专项);TOYOPUC packed 地址校验与编码口径自相矛盾(`address.py:132` vs `139`,待手册)。
- **文档/引用纪律(审计 + 补足,2026-09-27)**:协议实现 × `docs/protocol` 索引全面核对——①**MC 依据误标** `SH-080956`(SLMP)统一改为 **`SH-080008`**(MC 协议);②**FINS 依据误标** `W340` → **`W342`**(§5-1-3 结束码 / §5-2-1·§5-2-2 存储区):`omron/codec.py`、`core/constants.py` 及 `architecture.md` 5 处;③`keyence/mc.py` 的 `SH081257ENG` 未见于索引 → 改引 SLMP `SH-080956` 并标 **待核**;④`core/constants.py` MEWTOCOL 端口来源 Pro-face 标 **第三方非官方·待核**;⑤**补足缺失引用**:`melsec/address.py`(SH-080008 §8.1/8.2)、`modbus/address.py`(Modbus §4.4 + Modicon 记法存档)、`omron/address.py`(W342 §5-2)、`ab/address.py`(Rockwell Explicit Messaging)、`siemens/client.py`·`address.py`(S7-1500 §3.5/§6.4/§3;S7comm 编码待核)、`opcua/client.py`(Part1 §6.3.3/§7.11;Part 4 待核)、`cnc/mtconnect.py`(Part1 HTTP 端点)、`scanner/keyence_sr.py`(SR-2000 LON,b)、`plc/omron/cip.py`(W506 §7/W627)、`plc/ab/codec_cip.py`(ODVA EtherNet/IP/PUB00123/Rockwell)。`CONTRIBUTING.md` 铁律补两条硬约束(引用须指向正确文档编号、码表/错误码禁止按连续性推断须逐项对表),PR 流程加「列出依据手册编号+页码/章节」;CI 版本矩阵措辞(3.7.9+3.12)订正。**无文档依据者一律标「待核」**,不臆造。

### 待核(需手册/真机)

- 1E PC 号合法直连值(默认 0xFF 对 1E 非法)、TOYOPUC packed 段字索引、Inovance 0405/0406 支持面、FINS 字符串带位号的 PLC 端行为。(1E `X/Y` 进制已据手册修为十六进制,见 MC 专项,2026-09-27)

### 逐协议深入复核(2026-09-27 起)

自本轮起以**逐个协议深入核查**替代一次性全量扫描;每协议逐方法对照厂商手册(文本用
pdfplumber 抽取),产出「核对通过 / P0~P3 / 待核」并就地修复、门禁锁定。

| 协议 / 驱动 | 状态 | 结论摘要 |
|---|---|---|
| 三菱 MX Component | ✅ 已完成 | P1 ProgID(`ActSupportMsg.ActSupportMsg`)、P2 控件创建兜底、P3 位软元件字访问口径统一——见下「MX 专项」 |
| 三菱 MC 以太网(3E/4E/1E) | ✅ 已完成 | P0 设备码(三轮)+ ZR/1E X·Y 进制、字符串位号(本批)全修;见下「三菱 MC 专项」 |
| 三菱 MC 串口(1C/3C/4C) | ✅ 已完成 | 三层实现与手册逐字节一致(黄金向量在库),点数上限口径注释订正——见下「三菱 MC 串口专项」 |
| 欧姆龙 FINS | ✅ 已完成 | P1 结束码 0040 正常完成 + 字符串 `.bit`、P2 D/EM 位写直写、P3 元素上限/区界/尾字节/文案——见下「欧姆龙 FINS 专项」 |
| 欧姆龙 NJ/NX CIP | ✅ 已完成 | 与 AB 同栈(继承):F1 Get_Attribute_List 逐项布局、附加状态字步进两项修复同时作用于 NJ 直发路径;走线(无 UC-Send、空路由段)与 W506 口径一致——见下「AB EtherNet/IP(CIP)专项」 |
| Modbus(TCP/RTU) | ✅ 已完成 | 写响应回显校验、FC21/FC08 广播合法化、FC12 上界 0x46、写区域收口、FC24 变量名——见下「Modbus 专项」;另 FC20 上界 0x7D 维持自加宽容(见 P3 残留) |
| AB EtherNet/IP(CIP) | ✅ 已完成 | P1 Get_Attribute_List 逐项布局(属性号+状态+值)、P2 附加状态 16 位字步进、P3 类型名 ULINT / 连接尺寸构造期校验;UC-Send 与 Forward Open/Close 逐字段对照通过——见下「AB EtherNet/IP(CIP)专项」 |
| 倍福 TwinCAT ADS | ✅ 已完成 | P2 补 0x1A ERR_TCPSEND / transport 分类改 TRANSPORT / `write(STRING)` 补声明长预检;P3 自动 NetId 要求 IP 字面量——见下「倍福 TwinCAT ADS 专项」 |
| 西门子 S7 | ✅ 已完成 | P2 多变量批量读(snap7 read_multi_vars 双线 1.x ctypes / 3.x dict);P3 `_write` KeyError 修 + `DataType.STRING` 路由预检 + docstring `DataType.BYTE` 修——见下「西门子 S7 专项」 |
| OPC-UA | ✅ 已完成 | P2 Deadband 透传(DataChangeFilter);P3 GUID 格式严格校验(8-4-4-4-12)+ browse ReferenceType;§2.11.5/7/8 已由既有修复覆盖——见下「OPC-UA 专项」 |
| MTConnect | ⏳ 待查 | — |
| 汇川 H3U/H5U(Modbus + MC 兼容) | ✅ 已完成 | P2 X/Y 上限按 H5U 放宽到 X1777(1024 点)、P2 C200~C255 32 位计数器字访问(0xF700 双寄存器 + 32 位类型门控);地址表逐项对照 H3U 9.4.3 / H5U 9.5.1 与 MC 16.4——见下「汇川专项」 |
| 丰田 TOYOPUC / 松下 / 基恩士 KV·SR | ⏳ 待查 | 部分厂商手册在「待补」表 |
| native / aio 层 | ⏳ 待查 | 三轮 P1 恒等缩放(同步回归 + native 镜像)、Modbus 区域×类型、TOYOPUC 负写已修(2026-09-27);退避封顶等 P2 待处理 |

### MX 专项(2026-09-27,逐协议深入;PyMuPDF 抽取《MX Component Version 4 编程手册》全 564 页)

核对通过:`Open`/`Close` 码值、逻辑站号 0~1023、`GetDevice/SetDevice` 出参口径、块读写自定义 I/F 4 参 `(设备/列表, 点数, 缓冲, lplRetCode)`、`GetCpuType(szCpuName, lCpuType)` 顺序、`GetClockData/SetClockData` 七字段顺序、`ReadDeviceBlock2/Random2` 为 16 位 SHORT 版、`GetErrorMessage` 在 Act(ML)SupportMsg 上。

- **P1 `MX_SUPPORT_MSG_PROG_ID` 与手册不符** — `core/constants.py` 现值 `"ActUtlType.ActSupportMsg"`;手册 §1.2.1 控件一览为 `ActSupportMsg.dll → ActSupportMsg`(`ActMLSupportMsg` 为 ML 变体),ProgID 应 **`"ActSupportMsg.ActSupportMsg"`**。现值下 `get_error_message()` 的 `CreateObject` 必失败。**已修复(2026-09-27)**:改值 + 引用手册;门禁 `test_support_msg_progid_matches_manual`。
- **P2 `get_error_message` 创建失败未翻译** — `_new_support_msg_com` 的 `CreateObject` 无兜底,失败异常逃出公开 API。**已修复(2026-09-27)**:创建失败翻译为 `OmniPLCInternalError`(公共 API 收口 `(False, None)`);门禁 `test_support_msg_create_failure_translated`、`test_get_error_message_creation_failure_returns_false`。
- **P3 — 已修复(2026-09-27)**:`MX_BIT_DEVICES` 去掉手册外记号 `"ST"`(保留 `STS`/`STC`);新增 `_reject_non_bool_access`——非 BOOL 的位号后缀/位软元件访问在 `_read`/`_write`/`read_batch` 入参期拒绝(统一 GetDevice 位语义与块读 16 点/字口径,与 MC `read_batch` 一致);`write_batch` 对位软元件的整数值限 0/1;`MX_MAX_BLOCK_WORDS` 注明为驱动自定(手册 `lSize` 上限远大于此)。门禁 `test_bit_device_table_excludes_invalid_st`、`test_bit_device_rejects_non_bool`、`test_word_device_bit_suffix_rejects_non_bool`、`test_write_batch_bit_device_value_must_be_0_or_1`。

### 三菱 MC 专项(1E/3E/4E,2026-09-27;SH-080008 手册文本核对)

核对通过:3E/4E 副头部(请求 `50 00`/`54 00`、响应 `D0 00`/`D4 00`)、访问路由与请求数据长定义、4E 序列号+保留字段、核心命令线上字节序(`01 04`/`01 14`/`06 04`)、子命令 `0000`/`0001`、软元件码表(§8.1 除 ZR 外全部相符)、0406 位块 1 点=16 位且首软元件在 bit15、120 块上限、1E 副头部 `00/01/02/03` 与响应 `+0x80`、结束码 1 字节、位半字节高位在前。

- **P1 `ZR` 进制错** — `MC_DEVICE_CODES["ZR"]` base 10;手册 §8.1 `ZR … Hexadecimal`(码 B0H 正确)。`ZR100` 实际访问 ZR64、`ZR1F` 抛错。**已修复(2026-09-27)**:base 16;门禁 `test_mc_device_radix_matches_manual`。
- **P1 MC 字符串读写忽略 `.bit`** — `_read_string`/`_write_string` 不校验 `parsed.bit`,`read_string("D100.3")` 静默读 D100。**已修复(2026-09-27)**:两处拒绝位号后缀;门禁 `test_string_rejects_bit_suffix`。
- **P2 1E `X/Y` 进制错** — `MC_1E_DEVICE_CODES["X"/"Y"]` base 8;手册 §18.4 `X=5820H/Y=5920H … **Hexadecimal**`。**已修复(2026-09-27)**:base 16(原「待核」据手册落定)。
- **P3 文档/一致性(已订正)**:`MC_RESPONSE_SUBHEADER_3E` 注释误称「4E 写响应 = D0」(手册 4E 响应固定 `D400`,读/写同);`MC_1E_MAX_POINTS` 注释「高字节恒 0」不准(手册位单位/字设备上限 256,点数域 2 字节小端——本库保守取 255)。
- **P3 补漏(2026-09-27)**:
  - **0406 响应总量缺前置校验 → 已修**:手册 §8.4 定每块 1~960 点、总块 ≤120(本库每块 ≤900、总块 ≤120 均满足),但未校「响应总字节 ≤ `MC_MAX_RESPONSE_CONTENT`(8192)」——多块合计点数 >~4095 会先发请求、再在 `parse_response_head` 判坏帧。现 `build_random_read` 入参期按 `2 + Σ点数×2` 拒绝并提示拆分;门禁 `test_build_random_read_response_budget`。
  - **1E 字单位访问位软元件须首编号为 16 的倍数 → 已修**(手册 §18.4);门禁 `test_1e_word_access_bit_device_requires_multiple_of_16`。
  - **1E 设备编号域**:手册为 **4 字节**;本库按 2 字节 + 2 字节 0 等价实现(仅 ≤0xFFFF),codec_a 帧布局注释已订正。
- **P3 待核 / 能力缺口(未实现)**:
  - **1E PC 号(站号)范围手册为 `01H~40H`(1~64)**,而 1E 客户端默认沿用 `MC_DEFAULT_PC_NUMBER=0xFF`(3E/4E 直连约定)→ 默认对 1E 非法(PLC 回 `5BH`/异常码 `10H`)。合法直连值待真机核证,暂不改默认(避免误伤既有用法)。
  - 1E 表缺手册支持的 `F/B/W/T(N·S·C)/C(N·S·C)`;3E/4E 表缺长定时器/计数器(`LTS/LTC/LTN`、`LSTS/LSTC/LSTN`、`LCS/LCC/LCN`)、`LZ`、`RD`(§8.1;注 §8.4 的 0406 禁用长定时/计数与 LZ)。
  - 手册另注 AnACPU 下 `L/S/R` 不可访问(本库 1E 表含,按模块差异登记)。

### 三菱 MC 串口专项(1C/3C/4C,2026-09-27,逐协议深入;SH-080008 §4/§17/Appendix 5/Appendix 7 手册文本核对)

核对通过(三层:codec_serial / codec_serial_a / melsec 串口走线,均与手册逐字节一致):
3C 帧格式 4(ENQ + 帧识别码 "F9" + 路由 8 + 核心命令 + 和校验 + CR LF;§4.2 印刷页
31-32)、4C 帧格式 5(DLE STX 定界 + 数据长小端 + 附加码规则 + 和校验 ASCII;§4.2
印刷页 33 + §4.3 附加码算例印刷页 35)、帧识别码 F9/F8 与 2C=FB/1C 无识别码(§4.3
印刷页 36)、控制码表(ENQ/STX/ETX/ACK/NAK/DLE/EOT/CL;§4.3 印刷页 34)、和校验 =
范围字节和低 8 位且读响应**含 ETX**(Appendix 7 算例:请求 22BH+3DF=60AH、响应
22BH+18F=3BAH,黄金向量测试已锁定)、ACK/NAK 响应长度(13/17 字节,NAK 错误代码
4 位)与走线 `recv` 预算、1C 帧 BR/WR/BW/WW 命令与 256 点传 "00"(§17.2 印刷页
353「Specify '00' for 256 points」)、1C 点数上限六常量 = 手册 A 列(BR 256/BW
160/WR·WW 64 字/位软元件按字 WR 32·WW 10)、1C 错误代码 2 位规格、1C T/C 三位编号
与 TS/TC/TN 映射、4C 路由 7 字节(站/网/PC/模块 IO 小端/局/本站)、4C 应答识别码
FFFFH + 结束代码、4C 收包超时按整帧 deadline 拆连重同步(§1.4 既有结论)、3C/4C
命令域 "0401"/"1401"(Appendix 7 印刷页 479-482)、软元件码 `*` 补位与编号域 6 字符。

- **P3 文档订正(已落地)**:`MC_MAX_TRANSFER_POINTS=900` 注释曾称「3E/4E 单事务」
  却同时约束 3C/4C——手册 Appendix 5(印刷页 466)上限为 960 点(iQ-R/iQ-L/Q/L)/
  480 点(QnA);900 不越规但未分机型,注释改述共用口径与依据,维持不分机型收紧
  (超限由 PLC 异常码裁决)。
- **P3 引用补足(已落地)**:codec_serial 模块 docstring 和校验段补 Appendix 7
  黄金算例页码(印刷页 479),与既有黄金向量测试对应。
- **无新增回归测试**:Appendix 7 黄金向量(3C 读/写请求与响应、4C 读/写请求与响应、
  附加码、位写 10H 填充)已有 7 例逐字节门禁(test_mc_serial_clients.py::golden),
  本专项核证其与手册印刷页 479-482 一致,无需新增。

### Modbus 专项(2026-09-27,逐协议深入;Modbus 应用协议 V1.1b3 手册文本核对)

核对通过:FC01-04 数量上限(2000 位/125 寄存器)与读响应长度自洽校验、FC05/06 值域、
FC07 响应 2 字节、FC08 响应回显 5 字节与子功能回显校验、FC11 响应 5 字节、FC15 数量
0x07B0=1968、FC16 数量 0x007B=123、FC17 长度域下限 2(§6.13 印刷页 31)、FC20 请求
byte count 0x07~0xF5 与子请求 7 字节结构(§6.14 印刷页 32-33)、FC21 请求 0x09~0xFB
与请求回显(§6.15 印刷页 34-35)、FC22 掩码写 7 字节回显(§6.16 印刷页 37)、FC23 读
0x007D=125/写 0x0079=121(§6.17 印刷页 38)、FC24 FIFO ≤31 与 byte count 自洽
(§6.18 印刷页 40)、FC43/14 对象号保留区 0x07~0x7F 与 MoreFollows/NextObjectId 语义
(§6.21 印刷页 43-44)、MBAP 长度域上限与 RTU ADU 256、RTU 增量收包(FC12/17/24 按
byte count、FC43 按对象头)与 CRC 低字节在前。

- **P2 写响应不校验回显(已修复)**:`_write_pdu` 曾把 FC05/06/15/16 响应 PDU 直接
  丢弃;规范 §6.5/6.6/6.11/6.12(印刷页 17/19/29/30)正常响应为请求 PDU 前 5 字节
  回显。现 `_write_pdu` 经 `parse_write_response` 校验回显,不符按坏帧(错配/串包);
  广播(expect_response=False)跳过。回归 `test_write_response_echo_mismatch_rejected`
  (旧实现 FAILED);顺带订正汇川夹具的非规范 FC16 全帧回显。
- **P3 FC21 广播误拒(已修复)**:`write_file_record` 曾调 `_reject_broadcast_read`——
  FC21 是写语义(§6.15),RTU 广播写合法不等响应,与 FC05/06/15/16 口径一致;广播下
  跳过回显校验。回归 `test_fc21_broadcast_write_allowed`(旧实现 FAILED)。
- **P3 FC08 广播误拒(已修复)**:`diagnostics` 曾一律拒广播;`0x000A` 清计数器为写
  语义允许广播(§6.8,印刷页 22,设备不回包),其余只读子功能仍拒。回归
  `test_fc08_clear_counters_broadcast_allowed`(旧实现 FAILED)+ 只读仍拒用例。
- **P3 FC12 事件字节无上界(已修复)**:响应 byte count 曾仅查下限 6;规范 §6.10
  (印刷页 27)事件字节 0~64,byte count ≤ **0x46**,越界按坏帧拒绝(与 FC12 RTU
  增量收包的 ADU 封顶互补)。回归 `test_fc12_event_log_byte_count_capped`(旧实现 FAILED)。
- **P3 批量写只读区锁内才抛(已修复)**:`_check_address` 增 `is_write` 参数——写入口
  收口为可写区域(字类型仅 hr、BOOL 仅 c/hr 位号 RMW),ir/di 写在入参期拒绝、零字节
  发送;`_write`/`write_many`/`write_batch` 与 native `_write` 同步收口。回归
  `test_write_readonly_area_rejected_before_lock`。
- **P3 RTU FC24 增量收包变量名误导(已订正)**:byte count 域值曾名 `fifo_count`,
  实为「Byte Count = 2 + 2×N」;逻辑复核无误(剩余 = 计数(2)+值(2N)+CRC(2)),仅正名
  并补注释。

### 欧姆龙 FINS 专项(2026-09-27;W342 pdfplumber 全 270 页核对)

核对通过(逐项对照手册):§3-3-3 帧头字段与范围——ICF `0x80/0xC0`、RSV `00`、GCT `02`(多网 07)、网络号 0~127、节点 1~254(FF=广播)、单元 00/FE/10-1F/E1、SID 00~FF;§5-2-2 存储区码——CIO `30/B0`、W `31/B1`、H `32/B2`、A `33/B3`、D `02/82`、T/C 完成标志 `09` / PV `89`、EM bank0-F 位 `20~2F`/字 `A0~AF`(与 §5-3-2/§5-3-3 附表一致);§5-3-2/§5-3-3 0101/0102 布局(字 2 字节、位 1 字节;T/C 仅 PV 可写);§5-3-5 0104 布局与上限(条目 = 码 + 字 2 + 位 0;Ethernet/Controller Link 167、SYSMAC LINK/DeviceNet 89);§5-2-2 p.168 元素上限(读 999 字/写 997 字);各区字地址上界(§5-2-2 p.165-166)。

- **P1 正常完成结束码可能为 `0040` — 已修复**:W342 §5-1-3 p.161「Basically, the end code of a sent command that is completed normally is **0040**」——bit6/7(`0x00C0`)是**目标 CPU 单元错误标志**(非致命/致命,如电池电压低),与主/子码正交;CPU 存在该错误时,命令正常完成也会随 0040 返回。原实现 `end_code != 0` 一律抛 `DeviceError`,把正常读写误报失败且文本错指「目标 CPU 单元出错」。现 `codec._is_normal_end_code` 屏蔽 `0x00C0` 后主/子码为 0 即成功(接受 `0000/0040/0080/00C0`);**bit15 中继错误标志仍按错误**(响应会附中继错误码双字节、数据布局改变)。门禁 `test_parse_response_accepts_normal_completion_flag_codes`、`test_parse_response_rejects_relay_flag_with_zero_base`、`test_udp_end_code_0040_treated_as_success`。
- **P1 字符串读写忽略 `.bit` — 已修复**:`_read_string`/`_write_string`(同步 + native)不校验位号后缀,字码访问却带非零位号(手册未定义,PLC 行为不定)——与 MC 专项同类。现两处拒绝;门禁 `test_string_rejects_bit_suffix`(断言零发帧)。
- **P2 D/EM 位写统一读-改-写 — 已修复**:手册 §5-3-3 可写表含 **DM Bit `02` / EM Bit `20~2F`**(CS/CJ/CP/NSJ),位码直写可用;原实现一律 RMW(2 事务 + 并发写丢失窗口)。现 **0102 位码直写优先,遇 `0x1101`(老固件)回退读-改-写**(与位读回退对称);门禁 `test_udp_d_area_bit_write_direct`、`test_udp_d_area_bit_write_falls_back_on_1101` + native 直写/回退对拍。
- **P3 0101/0102 元素上限未收口 — 已修复**:新增 `FINS_MAX_READ_ELEMENTS=999` / `FINS_MAX_WRITE_ELEMENTS=997`(§5-2-2 p.168,Ethernet/Controller Link),客户端 `_read_words`/`_write_words`(同步 + native)入参期拒绝;门禁 `test_string_element_count_capped`。
- **P3 地址未按区门控 — 已修复**:新增 `FINS_MEMORY_AREA_MAX`(CIO 6143 / W·H 511 / A 959 / D·EM 32767 / T·C 4095,§5-2-2 p.165-166),`parse_fins_address` 解析期拒绝(超限不再靠 PLC 返回 1104);门禁 `test_parse_fins_address_rejects_out_of_area_range`。
- **P3 响应尾字节未校验 — 已修复**:`parse_response`/`parse_multiple_area_read` 成功路径长度严格(读:14+数据;写:14),多余字节按坏帧(防帧失步,与 MC 1E 同口径);门禁 `test_parse_response_rejects_trailing_bytes`、`test_parse_multiple_area_read_rejects_trailing_bytes`。
- **P3 结束码文案 8 条订正 — 已修复**:§5-1-3 逐项对表:`0202` 无指定单元(原误「无指定节点号」)、`0304` 单元号设置错误(原「节点号」)、`1101` 存储区码非法或存储区不存在、`1109` Relational error、`110A` 重复数据访问(微分监视/数据跟踪互斥)、`2202` 运行中不可执行(原「模式错误(停止)」反向)、`2502` 存储器内容错误(原「奇偶/校验和」)、`250A` CPU 总线单元错误。
- **待补文档 — 已登记**:FINS/TCP 封装与节点分配握手(魔数 `FINS`、命令 0/2、错误域、24 字节握手响应)不在 W342 范围(需欧姆龙以太网单元手册 W420/W465/W344),已登记 `docs/protocol/README.md`「待补」+ `codec.py` 该段注释标「依据待补」。
- **待核 / 能力缺口(未实现)**:节点 `FF`(广播,§3-3-3)不支持(节点范围 0~254);EM bank 10-18(位 `E0~E8`/字 `60~68`,CJ2/CS1D-CPU68HA 专属)与 EM 当前 bank(位 `0A`/字 `98`/`BC`)未实现(§5-2-2 注 1/2);位访问单次 1 点(位批量走 0104 字码提位)——设计边界。

门禁:全量 **1256 passed**(+11)/ ruff / mypy(76) / ty 全零。

### 西门子 S7 专项(2026-09-27;S7-1500 pdfplumber 全 98 页,§3.5/§7 核对)

核对通过(Siemens_S7-1500_Communication_Function_Manual.pdf):
- **§3.5 p.22**:「ISO on TCP 102 (4) TCP ISO-on-TCP (according to RFC 1006) ... S7 communication with ES, HMI, OPC server, etc.」(端口 102、ISO-on-TCP ✓)
- **§7 p.50**:PUT/GET 指令——「Data blocks for PUT/GET instructions: only data blocks with absolute addressing. Symbolic addressing not possible. You must also enable this service for protection in the CPU configuration」(PUT/GET 授权 + 优化块限制 → `_raise_link_aware` 提示依据)
- **rack/slot 约定**(硬件差异):1200/1500 slot 1;300/400 CPU slot 2——`S7_DEFAULT_SLOT=1`(用户真机硬件决定,按需调)
- driver 封装 pyS7snap7(1.3 C-ext / 3.x 纯 Python 双线);`S7comm` 编码(TPKT/COTP/S7 PDU)公开手册无逐条收录,标**待核**(依赖 pyS7snap7 含其 `MAX_VARS`)。

- **P2 多变量批量读(§2.6.7)——已修复**:`read_batch`/`read_many` 基于 snap7 `read_multi_vars` 单事务混读多区域多类型(BOOL 按 1 字节提位,STRING 拒变长;上限 **20 条**= `S7_MAX_MULTI_VARS` = snap7 `MAX_VARS`)。**双线适配**(依 `type(client).MAX_VARS` 判定):1.x/2.x C 封装线 → ctypes `S7DataItem` 数组(`WordLen=S7WLByte`、逐条 `pData` 缓冲、per-item `Result` 校验,失败条目索引附在 DeviceError);3.x 纯 Python 线 → dict 列表(内部优化器合并相邻读)。area 取 `.value`(1.x `Areas` 是普通 `Enum` 非 IntEnum)。aio 镜像;门禁 3.x dict / 1.x ctypes 各一例 + 校验拒绝 + 单事务断言 + aio 对拍 + 失败语义(在线=DeviceError 不断线)+ docstring `v1 范围` 收口。
- **P3 `_write` 未支持类型抛 `KeyError`(三轮)——已修复**:`_INT_FORMATS[data_type]` 对 STRING 抛 KeyError 逃出公开 API 契约。现 `_write` 顶部加 `_SIZES` 校验;**`DataType.STRING` 路由 `_write_string`**(含 PLC 侧声明长预检,与 ADS 专项一致);门禁 `test_typed_write_string_prechecked`。
- **P3 docstring 引用不存在的 `DataType.BYTE`(三轮)——已修复**:warn 文档原建议 `write(addr, DataType.BYTE, v)`——`DataType` 无 BYTE 成员(为 9 项数值 + STRING),不可达。改为「协议无单字节置位/复位原语,多写者请让同一字节只由一个写者负责」;门禁同 P3-2 用例覆盖。
- **待核/能力缺口(登记)**:`S7comm` 帧格式(依赖 pyS7snap7);SZL 系统状态列表;块操作;DB 寻址全部经绝对寻址,优化块访问(`_raise_link_aware` 提示)。

门禁:全量 **1267 passed**(+8)/ ruff / mypy(76) / ty 全零。

### OPC-UA 专项(2026-09-27;Part1 pdfplumber 全 30 页核对)

核对通过:Part1 §6.3.3 p.15(AddressSpace / Node / Reference 概念)、§7.11 p.19(Subscription Service Set 集合);`Browse`/`Read`/`Write` 服务集合见 Part4(本目录未收录,**待核**);GUID 文本格式见 OPC 10000-3 / RFC 4122(W342 不含);driver 封装 asyncua 1.1.5(钉版)。

- **P2 §2.11.2 推模式无 Deadband 过滤——已修复**:`subscribe_data_change` 新增 `deadband_value`/`deadband_type` 关键字参(`absolute`/`percent`),构造 `asyncua.ua.DataChangeFilter(Trigger, DeadbandType, DeadbandValue)`。因 asyncua 1.1.5 高层 `subscribe_data_change` **不收** `mfilter`,死区非 None 时走底层 `aio_obj._subscribe(nodes, attr, mfilter, queuesize, monitoring, sampling_interval)`(同步包装未暴露,需 `ua_sub.tloop.post(...).result()` 同步等待;asyncua 1.1.5 同步包装仅代理几个高层方法——asyncua 包装口径依赖留待记录);None 路径仍走原始高层方法,不增加 tloop.post 开销。门禁:`_build_data_change_filter(None, None) is None`;`(0.5, None) → DataChangeFilter(DeadbandValue=0.5, DeadbandType=Absolute)`;`(1.0, "Percent") → Percent`;非法 `deadband_type` 抛 `ValueError`。
- **P3 §2.11.10 GUID 格式不校验——已修复**:`parse_opcua_nodeid` 的 `g=` 校验由 `[0-9A-Fa-f-]+` 宽松匹配改为 **8-4-4-4-12** 共 32 位十六进制 + 4 连字符(允许带花括号;依 OPC 10000-3 / RFC 4122);非法样本(`g=ABC`、`g=0F1E2D3C-4B5A-6978-8796` 缺一段、字符越界、方括号等)解析期拒绝。门禁 `test_parse_nodeid_guid_format_strict`。
- **P3 §2.11.9 `browse` 仅默认 ReferenceType——已修复**:`browse` 新增 `reference_type_id` 关键字参(标准 NodeId 字符串如 `"i=33"` = HierarchicalReferences;`None` = 全部参考),解析为 asyncua 文本后透传到枚举。
- **已由既有修复覆盖(标记)**:
  - §2.11.5 `browse` 深树递归爆栈:`max_depth is None` 时应用 `OPCUA_BROWSE_DEFAULT_MAX_DEPTH=10`,递归深度 ≤ 10,Python 默认 1000 远大于此——已安全;显式栈迭代为后续优化。
  - §2.11.7 类型强校验拒收 bool→数值:协议层定型(`_coerce_read`/`_coerce_write` 严格拒绝 bool 充整数),文档化。
  - §2.11.8 回调跨关闭 loop 触发被吞:已由 `_state_lock`(短临界区纯字典操作,disconnect 前主动 `unsubscribe()`)修复。
- **待核/能力缺口(登记)**:§2.11.11 aio 层订阅关闭 asyncua 内部线程释放(asyncua 1.1.5 上游 bug,待升级);§2.11.4 安全策略/证书(内网口径,设计边界)。

门禁:全量 **1278 passed**(+11)/ ruff / mypy(76) / ty 全零。

### 倍福 TwinCAT ADS 专项(2026-09-27;TE1000 pdfplumber 全 133 页核对,§8 p.128-129)

核对通过:§8 ADS Return Codes 分组与码值(全局 `0x0000~0x001F`、Router `0x0500~0x050D`、General `0x0700+`);**AMS 端口表(p.9-10)**:851 = TC3 PLC runtime 1(库默认 ✓,TC2 为 801,多 runtime 852+)、501 = Router;AMS NetId 6 字节(p.11,「通常与 IP 无关」→ 库默认 IP.1.1 仅便利约定,显式路由需覆盖——已文档化);`pyads 3.5.1` 为参考实现(钉版,封装边界不变)。驱动本身不组帧(封装 pyads),专项聚焦错误码集、分类与字符串预检。

- **P2 transport 码集缺 `0x1A ERR_TCPSEND`(三轮 P2)——已修复**:TE1000 §8 p.128「0x1A 26 0x9811001A ERR_TCPSEND TCP send error」属全局组通断码;原码集有 `0x1B/0x1D` 独缺 `0x1A`,TCP 发送失败(路由中断/网线断)落 `DeviceError` 不断线。现补入;门禁 `test_translate_ads_error_transport_codes` 增 0x1A。
- **P2 transport 错误分类 UNKNOWN(三轮 P2)——已修复**:transport 分支原抛裸 `OmniPLCInternalError`(`_categorize` 落 UNKNOWN);改抛 `TransportClosedError`(OmniPLCInternalError 子类,归 **TRANSPORT**,`_execute` 同样标记断线+惰性重连,仅分类修正);门禁断言分类 + 客户端级 `test_tcp_send_error_disconnects_with_transport_category`(断线 + `last_error_category=TRANSPORT` + 原始码入文)。
- **P2 `write(addr, DataType.STRING)` 绕过声明长预检(三轮 P2)——已修复**:`_write` 原直接 `write_by_name`(仅 `_write_string` 预检);现 STRING 分支路由 `_write_string`(含 `symbol_type` 声明长预检 + 非 str 拒绝);门禁 `test_typed_write_string_prechecked`。
- **P3 自动 NetId 遇主机名拼出非法值——已修复**:`ip_address` 传主机名时自动拼 `"plc01.1.1"`(ADS 经本机路由器按 NetId 路由,主机名不参与解析,必死路由);现自动拼装要求 **IPv4 字面量**,否则构造期 `ValueError` 提示显式传 `net_id`(显式 net_id 时主机名可作占位);门禁 `test_auto_net_id_requires_ip_literal`。
- **待核/能力缺口(登记)**:结构体成员路径(需 TwinCAT 侧导出独立符号,pyads 边界);>32KB 大块符号不切块(当前仅标量/STRING,单值不触及);Online Change 句柄失效无显式处理(无客户端句柄缓存,实际影响有限,待真机)。

门禁:全量 **1259 passed**(+3)/ ruff / mypy(76) / ty 全零。

### AB EtherNet/IP(CIP)专项(2026-09-27,逐协议深入;Rockwell EM Guide / CIP Vol 1 章节号 + pycomm3 1.2.16 / pylogix / OpENer 参考实现对照)

核对通过(逐字段对照):ENIP 封装头 24 字节与命令集(0x65/0x66/0x6F/0x70/0x63)、
CPF 项(0x0000 空地址 / 0xA1+0xB1 连接式 / 0xB2 未连接 / ListIdentity 0x000C);**UC-Send 信封**
(服务 0x52 + CM 路径 `20 06 24 01` + 优先级/超时 + 消息长 + 奇偶补齐 + 路由段
`[字数][保留][端口][槽号]`)与 Rockwell《EtherNet/IP Explicit Messaging Guide》p.22 Table 5
示例 `01 00 port 1 slot 0` 逐字节一致(pycomm3 `PADDED_EPATH(length=True, pad_length=True)`
同构);Forward Open(0x54/0x5B)与 Forward Close(0x4E)逐字段与 pycomm3
`cip_driver._forward_open/_forward_close` 一致(含 Large 参数域 `0x4200<<16 | 尺寸`、超时乘数
+3 保留字节、连接路径字数、RPI 100ms);SendUnitData 的 0xA1/0xB1 项布局、序列号回显与 T->O
ID 校验;标签读 0x4C / 写 0x4D / 读-改-写 0x4E / 多服务包 0x0A(条数 + 偏移自条数域起算 +
逐条偶对齐);88 字节 STRING 结构(4B LEN + 82 字符 + 补齐)、DWORD 打包 BOOL 数组与位访问口径;
NJ/NX 直发(不包 UC-Send、连接路径仅消息路由对象)与 W506 口径。

- **P1 Get_Attribute_List(0x03)应答逐项布局错——已修复**:原实现按
  `属性数(u16) + 每项[长度 u16][值]` 解;规范应答为 `属性数(u16) + 每项[属性号 u16][状态 u16]
  [值(仅状态 0 时存在)]`,值**不带类型码与长度域**(长度由属性类型决定)。证据三链:①ODVA
  CIP Vol 1 §5-4(Get_Attribute_List 应答);②Rockwell 1756-PM020(Logix 5000 手册,外部检索)
  Get_Attributes_List 应答示例——计数后每项为「属性号(16 位)+ 状态(16 位)+ 属性数据」;
  ③OpENer `cipcommon.c: GetAttributeList` 服务端写回顺序(属性号 → 状态 → 保留 0 → 值)与
  pycomm3 1.2.16 `get_plc_time` 解码 `Struct(n_bytes(6), ULINT)`(= 计数 2 + 每项前导 4)吻合。
  真机上原实现 `get_attribute_list()` 必解码失败。现解码器契约为「收该属性起始处剩余字节 →
  `(值, 已消费字节数)`,逐项状态非 0 返回 `None`;Identity 属性 1~7 内置解器(vendor /
  product_type / product_code / revision / status / serial / product_name),**任意子集**请求
  均走内置解器(原先仅恰好 7 项才解);非 Identity 对象原样返回项区字节。门禁
  `test_parse_get_attribute_list_payload_golden` / `_item_status` / `_errors`、
  `test_get_attribute_list_decodes_identity_attributes`、`test_get_attribute_list_subset_and_failed_item`
  (旧实现 FAILED)。
- **P2 附加状态步进按字节而非 16 位字(三轮 §2.4.7)——已修复**:`_parse_service_payload` /
  `parse_service_reply` / `parse_forward_open_reply` 三处曾按 `4 + 附加状态长` 定位数据域,
  而该长度字段单位为 **16 位字**(数据域自 `4 + 2×N` 起;本库 `_extended_status_text` 与
  pycomm3 `get_extended_status` 的 `size × 2` 均按字计)。正常帧(长 0)不受影响,带附加状态的
  成功帧会错位。现统一经 `_service_data_offset()` 收口;门禁
  `test_service_reply_additional_status_word_step`、`test_forward_open_reply_additional_status_step`
  (旧实现 FAILED)。
- **P3 类型码 0xC9 误标 `LWORD`——已修复**:CIP 基本类型表 0xC9 = **ULINT**(LWORD 是 0xD4,
  pycomm3 码表同此);仅影响 `type_name()` 错误文案;门禁
  `test_type_name_matches_cip_elementary_codes`(旧实现 FAILED)。
- **P3 Forward Open 连接尺寸无构造期校验——已修复**:普通形式尺寸域 9 位(≤0x1FF)、Large 16 位
  (≤0xFFFF),超限会溢出污染参数属性位(P2P/固定尺寸位);现 `_forward_open_params` 入参期拒绝;
  门禁 `test_forward_open_connection_size_bounds`。
- **待核/能力缺口(登记,未实现)**:0x0A 部分响应(CIP 0x06 INSUFFICIENT_PACKETS / 0x12)不续读
  (§2.4.2 P2);类型缓存命中后不失效(在线改 UDT 后 stale,§2.4.4 P2);UDT 16 位成员 ID(0x8B 段)
  与 >255 字符符号段名不支持(§2.4.5 P2);标签枚举(0x6B / Get_Instance_Attribute_List 0x55)未实现
  (§2.4.12 P3)。**文档**:ODVA CIP Vol 1/Vol 2 全本与 Logix 1756-PM020 未收录(本地仅概览级 PDF),
  本批按**章节号 + 参考实现(OpENer 一致性栈 / pycomm3 / pylogix)对照**裁决,已登记
  `docs/protocol/README.md` 待补表。

门禁:全量 **1270 passed**(本批 +8)/ ruff / mypy(76) / ty 全零。(同机同时段另有 7 个
`test_opcua_client.py` 失败,属另一代理 OPC-UA 订阅重构在途 WIP,非本批引入。)

### 汇川专项(2026-09-27,逐协议深入;H3U 手册 9.4.3 + H5U&Easy 手册 9.5.1/16.4 pdfplumber 逐项核对)

核对通过(逐字段对照 H3U 9.4.3「Modbus 通信地址」印刷页 575-576 与 H5U&Easy 9.5.1「被
ModBus 访问的线圈地址」印刷页 418-419、16.4「支持规格」印刷页 661):Modbus 为两机型共同
底座——从站地址 1~247(由 D8121 设定)、从站 FC 集 01/02/03/04/05/06/0F/10、FC01 数量
≤2000、FC10 数量 ≤123;线圈/寄存器基址逐项一致(M `0x0000`、SM/SD `0x2400`、S `0xE000`、
T `0xF000`(位 T0~T511 / 字 T0~T255)、C 接点 `0xF400`(C0~C255)、D `0x0000`、
R `0x3000`(R0~R32767));串口默认 9600-8N2(H5U 印刷页 418)。MC 兼容侧 16.4「支持软元件
范围」与库内码表逐一一致:X/Y(位,八进制编码)、M→三菱 M、S→三菱 L(92h)、B、D、R
(统一编址 R n = D(8000+n),手册原文「访问 R0 需通过访问 D8000 实现」)、W。

- **P2 X/Y 上限取 H3U 口径,拒掉 H5U 合法地址——已修复**:常量表 `X/Y = (0xF800/0xFC00,
  256)` 按 H3U(H3U 仅 X0~X377)收口,而 H5U 表为 **X0-X1777(八进制)= 1024 点**
  (0xF800-0xFBFF / 0xFC00-0xFFFF),X400~X1777 被入参期误拒。现上限改 **1024**;H3U 侧
  超出 256 的地址落设备地址空洞(H3U 表 0xF900-0xFBFF 无定义)由 PLC 报异常,不影响
  现场。门禁 `test_xy_extended_range_h5u`(旧实现 FAILED)。
- **P2 C200~C255 32 位计数器无字访问(§2.8.2)——已修复**:原表 `C 字 = (0xF400, 200)` 把
  C200 及以后一律拒绝。H3U 9.4.3(印刷页 575-576)明载 C200~C255 为 **32 位寄存器**,自
  **0xF700** 起、每只占两个 16 位寄存器空间(手册算例:C205~C208 → Modbus 地址 0xF70A、
  寄存器数量 8),且**32 位寄存器不支持写单个寄存器(FC06)**。现新增
  `INOVANCE_C32_BASE/FIRST/LAST` 常量、`to_modbus_address` 双寄存器展开(``C205`` →
  ``hr63242``)与 `check_counter_word_type` 类型门控(仅放行 32 位类型 INT/UINT/FLOAT,
  16/64 位类型与字符串在入参期拒绝、零字节发送;写路径自然落 FC16 而非 FC06),接点位访问
  仍走线圈(``C205`` → ``c62669``)。门禁 `test_c32_counter_word_mapping` /
  `test_tcp_read_uint_c32_counter` / `test_tcp_write_uint_c32_counter`(旧实现 FAILED)/
  `test_c32_counter_type_gate`。
- **P3 机型差异文档化(§2.8.1)——已补**:合并表以 **H3U 为宽口径**(H3U M0~M7679 +
  M8000~M8511、D0~D8511;H5U M0~M7999、D0~D7999;SM/SD/T/C 仅 H3U Modbus 表收录,
  H5U 表只有 M/B/S/X/Y 线圈与 D/R 寄存器)。两机型超范围地址在两机型都落**地址空洞**
  (H3U 7680-7999、H5U 8000-12287),由 PLC 返回 Modbus 异常 02,**不会静默落错软元件**;
  `core/constants.py` 两张表 docstring 已逐项标注机型与手册页。不做运行期机型探测
  (无读型号的公开途径)。
- **P3 串口默认停止位口径订正——已修**:`INOVANCE_SERIAL_DEFAULT_STOP_BITS` 与
  `configure_serial` 原笼统写「汇川缺省 9600-8N2」;实为 **H5U&Easy 9.5.1(印刷页 418)**
  的从站默认,而 H3U 手册的 Modbus-RTU 示例是 **9600-8N1**(D8120=H081)。默认值维持
  2(H5U 口径),文档改为按机型说明并提示以现场 D8110/D8120 为准。
- **P3 MC 帧点数码差异文档化(§2.8.4)——已补**:16.4(印刷页 661)点数码与三菱 Q/L 不同
  ——批量 960 字、批量位 1~3584(iQ-R 1168)、随机读加权「字×2 + 双字」≤96、随机写加权
  「字×2 + 双字×14」≤960、随机写位 1~64,已在 `plc/inovance/mc.py` docstring 列明。本库
  MC 层沿用三菱上限(更宽处放行),汇川设备侧超限以故障码 **C052/C053**(16.5 印刷页 662)
  拒绝,属明确报错;未做提前收口——手册该表的加权式在文本抽取中列边界有损,按铁律不臆造。
- **待核/能力缺口(登记)**:①厂商私有功能码(配方传输等)不在 H3U/H5U 编程手册的 Modbus
  章节(9.4 只列标准 FC 01~10),不臆造——自由协议帧可用 `OpenTcpClient`(STX/ETX、
  长度前缀、编码回退)自组(§2.8.3 结案为文档化边界);②站号 >31 的第三方网关兼容
  (§2.8.6)无手册依据,维持标准 1~247;③H5U 的 R 与 B 基址同为 0x3000、SM/SD 同为
  0x2400 是**不同地址空间**(线圈 vs 保持寄存器),非笔误。

门禁:全量 **1290 passed**(本批 +5)/ ruff / mypy(76) / ty(本批文件零诊断)。

### 发布记录(2026-09-27)

- **v0.43.1**(补丁):汇总本台账三轮复审 P0 与逐协议专项——①三轮 P0 MC 设备码(`TC/TN/CC/CN`、1E `S`);②文档引用审计与补足(MC `SH-080008`/FINS `W342`、10 模块补页码级引用、CONTRIBUTING 铁律加固);③三菱 MX 专项(P1 ProgID / P2 创建兜底 / P3 位口径统一 + 控件配对释放);④三菱 MC 专项(`ZR`/1E `X·Y` 进制、字符串位号、0406 响应预算、1E 16 倍数);⑤Modbus FC17;⑥AGENTS.md。五落点(pyproject / `__version__` / CHANGELOG / architecture 版本行 + §11 / README)+ `uv.lock` 一致;tag `v0.43.1` → `34289c7`;已推 `origin`(Gitee)+ `github`。门禁 3.7.9 **1216 passed** / ruff / mypy(76) / ty 全零。**注**:区间含 `feat: Modbus FC17`(新增公共 API),本次按用户要求并入补丁发布,严格 semver 应为次版本,特此登记。
- **v0.43.0**(次版本,2026-09-26):二轮复审修复收口 + 小功能扩展;门禁 1196 passed。
- **v0.42.1**(补丁,2026-09-26):MC `L` 设备码回归修复(`0xA0→0x92`)+ 测试清理;门禁 1150 passed。

---

## 一、总体架构层面

### 1.1 `BaseClient.disconnect()` 先清状态再关闭 — **P3(实锤)**
- `core/base_client.py:233-246`
- `_transport`、`_connected`、退避门控在 `transport.close()` 之前清理。复核:`close()` 仍经局部变量调用,FD 不泄露;仅当 `close()` 抛 `OSError` 时返回 `False` 而 `disconnect_count` 不计——状态语义与计数口径轻微不一致。
- **修复**:close 成功后再计数;失败时区分"已关"与"未关"。

### 1.2 `write_retries > 0` 时写超时重发可能双写 — **P3(实锤,默认关闭)**
- `core/base_client.py:120, 679, 698-702`
- 复核:**默认 `_write_retries=0`**(base_client.py:120),aio docstring 亦明写"默认 0,防重复写入"。双写仅在用户显式开启写重试后发生(超时=请求已发出但响应丢失,重试=写第二次)。
- **修复**:文档强化"写重试仅对幂等写安全";或在超时场景区隔"请求已发出"状态。

### 1.3 `last_rtt` 含全部重试时长 — **P3(实锤)**
- `core/base_client.py:682, 696`
- `started` 在重试循环前捕获一次,重试成功后 `last_rtt` 为总耗时,SCADA 健康看板误报"PLC 慢"。
- **修复**:每次重试迭代重置计时。

### 1.4 MC 4C 串口整帧受单次 `receive_timeout` 预算约束 — **P3(实锤)**
- `plc/melsec/melsec.py:762-824`
- 复核:整帧有单一 deadline(`melsec.py:770`,docstring 明说防逐字节重置超时),v1 稿"逐字节重置超时"说法不成立。残余问题:9600 波特下 3s 预算约合 2800 字节,接近 `MC_SERIAL_MAX_FRAME=4096` 的大帧会超预算截断(截断后走拆连重同步,不脏数据)。
- **修复**:串口传输层按波特率推算超时下限,或大帧预算自适应。

### 1.5 串口截断后重开串口是否彻底清残留 — **P3(待核证)**
- `transport/serial.py:178-189`
- 复核:`_timeout_exit` 关串口 + 惰性重开即为重同步手段,设计注释明说(OS 关句柄释放缓冲)。"Windows close 不清 RX"的 v1 论断存疑,待真机 485 多站验证。
- **核证判据**:485 总线上制造半帧后,下一笔事务应干净超时而非读到残渣。

### 1.6 传输层 `receive_timeout` 与 recv 循环的互踩 — **P3(待核证)**
- `transport/tcp.py`、`core/base_client.py:277-284`
- 复核:客户端级 setter 持事务锁(阻塞到当前事务结束),经公开 API 不存在 v1 所述竞态;仅直接持有 transport 对象才可能互踩。降级留档。
- **修复**:如需暴露 transport 级并发配置,给 setter 加锁。

### 1.7 `AsyncUdpTransport._clear_stale_selector` 吞所有 OSError — **P3(待核证)**
- `native/transport.py:486-508`
- `except (NotImplementedError, OSError, ValueError): pass`,fd 用尽时变成不透明崩溃。
- **修复**:`errno in (EBADF, EINVAL)` 时记 WARNING。

### 1.8 `last_error` 成功即清,无历史 — **P3(文档化限制)**
- `core/base_client.py:411-415, 694`
- 现场趋势分析需要错误历史。
- **修复**:加 `last_error_history`(环形队列,最近 N 条)。

### 1.9 `set_debug(False)` 不摘除 stderr handler — **P3(待核证)**
- `core/debug.py:130-142`
- **修复**:关闭时 `removeHandler`。

### 1.10 调试输出明文打印报文(含会话凭据风险) — **P3(文档化限制)**
- `core/debug.py:62-71`
- OPC-UA 握手期 token、S7 明文协议均在 dump 范围。
- **修复**:文档高亮"生产日志严禁开 debug";可选 scrub。

### 1.11 标签表 `data_type` 不在加载时校验 — **P3(实锤)**
- `tag.py:94-110`
- `"floot"` 错字运行时才炸。
- **修复**:`_tag_from_record` 内即时调 `DataType` 校验。

### 1.12 `OpenTcpClient.max_frame` 无上限 — **P3(已修复:2026-09-26)**
- `opentcp/client.py:88-124, 331`
- **修复**:新增 `OPEN_TCP_MAX_FRAME_LIMIT=16 MiB` 构造期上界校验。回归 `test_max_frame_upper_bound_rejected`。

---

## 二、按协议

### 2.1 Modbus

#### 2.1.1 RTU 无 3.5 字符帧间隔强制 — **已修复(2026-09-26)**
- `modbus/modbus.py`(`ModbusRtuClient.inter_frame_delay` 属性,默认 0)
- 复核:主站单线程事务模型下 Python 周转通常已 >3.5c,实际影响集中在高波特率+密集轮询。新增 `inter_frame_delay`(秒,默认 0 保持现状)在每次发送前 `sleep`,供现场按波特率收紧;负值拒绝。

#### 2.1.2 Modicon 6 位扩展地址不支持 — **已修复(2026-09-26)**
- `modbus/address.py`(`_parse_modicon` 按数字位数分派)
- 仅 5 位方案时,Wago/Phoenix/TwinCAT Modbus server 的 `100001`/`400001` 全部 `ValueError`。现按**位数**区分:6 位 `000001~065536`(线圈)/`100001~165536`/`300001~365536`/`400001~465536`,5 位 `00001~49999` 不变;消除 `40001`(5 位保持)与 `040001`(6 位线圈)的数值歧义。

#### 2.1.3 FC23 跨段校验不完备 — **已修复(2026-09-26)**
- `modbus/modbus.py`(`read_write_registers` 捕获异常码 0x02)
- 设备对跨段 FC23 回 ILLEGAL DATA ADDRESS 时,`last_error` 追加"部分网关/老设备不支持跨段 FC23(读、写地址分属不同网段),可改用 write_many + read_many";不再只有干巴巴的异常码。

#### 2.1.4 FC22 掩码字节序不可配置 — **已修复(2026-09-26)**
- `modbus/codec.py` + `modbus/modbus.py`(`write_mask_register(..., byte_order="big")`)
- 新增 `byte_order`(`"big"`/`"little"` 或 `ByteOrder`):规范为 big,部分施耐德机型按本机字序解释掩码,可按现场切换;地址恒 big-endian,回显逐字节校验不受影响。

#### 2.1.5 FC43 对象 ID 不校验范围与重复 — **已修复(2026-09-26)**
- `modbus/codec.py`(`check_device_id_object` + `parse_device_id_response`)
- 请求侧:`build_device_id_pdu`/`read_device_object` 拒绝保留区间 0x07~0x7F;响应侧:同一响应内对象号重复或落在保留区间按坏帧(`ProtocolFrameError`)拒绝。

#### 2.1.6 缺 FC08/FC11/FC12 诊断 — **已修复(2026-09-26)**
- `modbus/codec.py` + `modbus/modbus.py`
- 新增 `diagnostics(sub_function, data)`(FC08,含 0x000A 清计数器 / 0x000B~0x000E 读计数)、`get_comm_event_counter()`(FC11)、`get_comm_event_log()`(FC12;RTU 走 byte-count 增量收包);均含 aio 镜像。

#### 2.1.7 缺 FC20/FC21 文件记录 — **已修复(2026-09-26)**
- `modbus/codec.py` + `modbus/modbus.py`
- 新增 `read_file_record(requests)`(FC20,按规范 §6.14 子响应 `File resp. length = 1+2N` 逐组解析)、`write_file_record(records)`(FC21,响应须请求回显);字段范围(file 1~65535 / record 0~9999)与 PDU 上限校验;均含 aio 镜像。

#### 2.1.8 缺 FC24 FIFO — **已修复(2026-09-26)**
- `modbus/codec.py` + `modbus/modbus.py`
- 新增 `read_fifo_queue(address)`(FC24):响应 = 字节计数(2)+FIFO 计数(2,≤31)+值(2N);RTU 走 byte-count 增量收包;含 aio 镜像。

#### 2.1.9 STRING 地址静默吞 `.bit` 后缀 — **已修复(2026-09-26)**
- `modbus/modbus.py`(`_read_string`/`_write_string`)
- 寄存器位号后缀(`hr100.3`)对字符串读写不再静默忽略:显式 `ValueError`。

---

### 2.2 三菱 MC / MX Component

#### 2.2.1 MC 客户端静默丢弃位软元件上的 `.bit` 后缀 — **已修复(2026-09-26)**
- `plc/melsec/melsec.py:327-329, 333-337`
- 复核确认:`_read_bool_impl` 对位软元件直接 `_read_bits(parsed, 1)`,`M10.5` 的位号 5 在组帧时被丢弃,实际读写 M10。写路径(`melsec.py:155-168`)同病。MX Component 有校验(`mx.py:915-917`),MC 侧没有——两套驱动行为不一致。
- **现场影响**:位号后缀对位软元件完全失效且无任何报错,最危险的"对得上但错位"型 bug。
- **修复**:MC 侧补"位软元件拒位号后缀"校验,与 MX 口径对齐。

#### 2.2.2 FX5U(iQ-F)X/Y 按十六进制处理,与 FX5U 八进制口径错位 — **已修复(2026-09-26;`xy_octal`)**
- `plc/melsec/melsec.py`(`xy_octal` 构造参数 + `_MC_DEVICE_CODES_FX5U_XY` 码表)
- 复核:客户端已提供 `xy_octal=True` 切换 `X/Y` 八进制口径(FX5U/iQ-F),`_effective_codes` 在组帧层换码表,其余软元件仍按 Q/L/R 口径;README 已示例 `X17`。默认 False 保持 Q/L/R 口径。**结论:已实现**;`X/Y` 编号含 8/9 是否需额外报错提示,视现场是否需要再定。

#### 2.2.3 MC 设备码表缺大量 Q/L/R 软元件 — **已修复(2026-09-26)**
- `core/constants.py:218-229`
- 仅 X/Y/M/S/B/D/W/R/Z/ZR。缺:L/F/V/TS/TC/TN/CS/CC/CN/SM/SB/SD/SW/DX/DY/G。
- **现场影响**:定时器/计数器(TS/TC/TN/CS/CC/CN)、特殊继电器 SM/SD、模拟量模块缓冲 G、报警器 F 全部"未支持设备"。
- **修复**:扩表;注意 L=0xA0 与 B 同码需消歧。

#### 2.2.4 `read_batch` 不拒位软元件进 0406 字块 — **已修复(2026-09-26)**
- `plc/melsec/melsec.py`(`read_batch` 计划段)
- `read_batch([("M10", "short")])` 把 M 塞进字块,PLC 拒收或按 16 点/字错读。现于计划段校验:位软元件 + 非 BOOL ⇒ `ValueError`(零字节发送)。
- **定制点**:`_bit_device_word_access_allowed` 默认 False(三菱 M/X/Y 等纯位语义,拒绝);松下 MC 置 True(其 R/X/Y/L 为"字号×16+位号"位软元件,按字访问是正常用法,`R1003`→线性 1603 读所属字)。
- **残留**:单点 `read("M10", "short")` 仍允许(与字单位 16 点对齐语义一致,未在本次范围)。

#### 2.2.5 串口 3C/4C 响应不校验路由回显 — **已修复(2026-09-26)**
- `plc/melsec/codec_serial.py`
- 多站串口下站间响应串扰无防线;1C 有校验,3C/4C 不一致。现 `parse_3c_response` 校验响应 8 字符路由(站号/网络号/PC 号/本站号)、`parse_4c_response` 校验 7 字节路由(含模块 I/O·局号),与请求不符抛 `ProtocolFrameError`;`build_*` 与 `parse_*` 共用 `_route_text_3c`/`_route_bytes_4c` 保证编码同源;客户端 `_parse_serial` 传入实际配置路由。

#### 2.2.6 0406 仅子命令 0000 — **已核实(2026-09-26;设备扩展为独立特性,登记 v1.x)**
- `plc/melsec/codec_qna.py`
- 复核(Omron… 实为 Mitsubishi **SH-080008 §8.4 + Appendix 1**):子命令 **0000** 为标准多块读(总块数 ≤120);**0002(iQ-R)/0080(iQ-L·Q/L)** 是「**设备扩展指定**」——用于链接直接 `Jn\…`、模块访问 `Un\G`(智能功能模块缓冲)、CPU 缓冲 `U3En\G/HG`,总块数 ≤60。**并非 v1 评审所述的“跨网单事务”**(原表述有误)。
- **结论**:本库按 0000 + 标准软元件寻址,标准软元件(D/M/X/Y/…)**无功能缺失**;设备扩展(`Un\G` 模块缓冲等)需新增地址语法 + 扩展块格式,与既有寻址模型正交,列为 **v1.x 特性**(MC 手册 SH-080008 已下载至 `docs/`,gitignore 本地留存)。
- **手册复核二期(2026-09-26,SH-080008 文本层已抽取)**:§8.4 明示「bit device is **16-bit for one point**」;§8.1 位字打包**首软元件在 bit15**(M10~M14 首点占最高 nibble;32 点 M16~M47 的 `AB1234CD` 以 M16 为最高 nibble);块数上限 `01H~78H`(1~120)。→ 与本库 `codec_qna.py:13`「位块 1 点=16 位、首软元件在 bit15」及 120 块上限完全一致。
- **设备码 (SLMP 手册)**:`Latch relay (L) L*** … (92H)` —— 独立证实 §2.2.3 中 `L` 应为 `0x92`。

#### 2.2.7 0406 位块每条目仅 1 点 — **已修复(2026-09-26)**
- `plc/melsec/melsec.py`(`read_batch` + `_merge_bit_blocks`)
- 原每个位请求各自成一个 1 点位块(100 个 M 点即顶满 120 块上限)。现按 SH-080008 §8.4 合并:同软元件且编号连续的位请求归为一个位块(1 点 = 16 位,块内首软元件在 bit15,块内第 k 个软元件落 ``k//16`` 字的 ``15-k%16`` 位);非连续/跨软元件/乱序各自成块。100 个连续 M 点由 100 块降为 7 块。

#### 2.2.8 0406 响应不逐块校验长度 — **已修复(2026-09-26)**
- `plc/melsec/codec_qna.py`(`parse_random_read_response`/`parse_response`/`_locate_data`)
- 说明:0406 响应**本身无每块长度字段**(仅按请求块顺序拼接数据),无法真正"逐块"断言;改为**严格校验 `内容长 == 2 + Σ点数×2`**:数据段不再按 `expected` 静默截断,尾部有多余字节或声明长与请求点数不符一律 `ProtocolFrameError`(含收到的原始帧)。

#### 2.2.9 MX Component `ActSupportMsg` ProgID 存疑 — **P2(待核证)**
- `core/constants.py:604`
- 现值 `"ActUtlType.ActSupportMsg"`;对照 MX 手册疑应为 `"ActSupportMsg.ActSupportMsg"`。代码注释自标"真机待核证"。
- **修复**:真机核证后修正。

#### 2.2.10 MX Component 默认逻辑站号 0 — **P3(文档化限制)**
- 实际工程几乎从 1 起。**修复**:文档强调或改默认。

#### 2.2.11 MX Component 32/64 位批量读取未支持 — **P2(待核证)**
- `mx.py:689-732`
- ReadDeviceRandom 理论上可按行追加设备拼 32 位值,v1 稿断言"无法拆"存疑。
- **修复**:真机核证后放开。

#### 2.2.12 MC UDP 不做数据报边界校验 — **已修复(2026-09-26)**
- `plc/melsec/codec_qna.py`(`_locate_data` 尾部多余字节检查)+ `plc/melsec/melsec.py`(`_transact` 文档)
- UDP 一次 `recv` 整包后,解析层拒绝**超出长度域声明的尾部字节**(数据报边界/串包异常);TCP 路径因按长度收包恒等,不受影响。

#### 2.2.13 MX `SetClockData` 的 `day_of_week` 传入即被忽略 — **P3(待核证)**
- `mx.py:350-368`
- **二轮复审复核关闭(2026-09-26)**:`mx.py:361` 已把 `day_of_week` 纳入 `fields` 透传,`test_get_set_clock` 断言 7 元组,原条不成立。

---

### 2.3 欧姆龙 FINS / NJ-NX CIP

#### 2.3.1 跨网 FINS 路由需显式 `destination_network`,无引导 — **已修复(2026-09-26)**
- `core/constants.py`(`FINS_END_CODE_HINT`)、`plc/omron/codec.py`(`_end_code_text`)
- 复核:握手响应仅携带节点号(4 字节地址字段低字节),不含网络号,无法自动填充;多网拓扑(Controller Link / EIP 网桥)下默认 0 全返 0x0201。
- **修复**:结束码文本附现场排查提示(0x0201 → 检查 `destination_network`/路由表;0x0101/0x0106/0x0501-0x0504/0x1005 同理),现场无需查手册即可定位配置类错误。

#### 2.3.2 `_local_ip_for` 依赖系统路由表与阻塞 DNS — **P2(实锤)**
- `plc/omron/omron.py:59-67`
- 多网卡/VPN 切换后源节点漂移,PLC 偶发返 0x0106(节点重复)。
- **修复**:实际发包后 `getsockname` 复核;文档推荐显式 `source_node`。

#### 2.3.3 NJ BOOL 数组沿用 AB 的 DWORD 32 位打包 — **已修复(2026-09-26)**
- `plc/ab/ab.py:530-535`,经 `plc/omron/cip.py` 继承
- 复核确认:`OmronCipClient` 仅覆写路由/String,未覆写 `_read_bool_array_element`。NJ BOOL 数组 1 元素/字节,`下标//32` 定词使 `BoolArray[5]` 永远落到 BoolArray[0] 的 bit5。
- **现场影响**:NJ 上每条 BOOL 数组元素读写都是错的。
- **修复**:NJ 侧重写为 `下标//8` 口径或按元素类型探测。

#### 2.3.4 NJ STRING 读写直接拒绝 — **已修复(2026-09-26)**
- `plc/omron/cip.py:77-85`
- 复核确认:显式抛 `DeviceError`,代码注释明说"布局待真机核证"——是刻意的保守闸,不是隐藏 bug。但 NJ STRING 布局(`len(u32)+chars[N]`)无 82 字节限制,可实现。
- **现场影响**:HMI 批号/报警文本场景 NJ 客户端不可用。
- **修复**:真机核证布局后放开;放开前文档显式声明边界。

#### 2.3.5 D 区位读对老固件无回退 — **已修复(2026-09-26)**
- `plc/omron/omron.py`(`_read_bit_impl`)
- CP1E/部分 CS1 不支持 D/EM 位区码(结束码 0x1101)。现捕获该码且区为 D/EM 时,回退「字读 + 本地按位提取」,对调用方透明(单测:位读被拒后自动改字读)。

#### 2.3.6 `read_batch` 无字节预算切分 — **P3(实锤)**
- `plc/omron/omron.py:277-308`
- 167 条上限不含字节数维度,大批量可能超 MSS。
- **修复**:按估算字节数自动拆批。

#### 2.3.7 `FINS_MAX_TCP_FRAME=8192` 宽于规范 — **P3(实锤)**
- `core/constants.py:561`
- 规范 2012/4032;校验存在,仅 DoS 窗口偏大。
- **修复**:下调默认,大帧可选放开。

#### 2.3.8 NJ POU 作用域标签名不支持 — **P2(实锤)**
- `plc/ab/address.py:23-26`
- 正则只认 AB `Program:` 前缀。
- **修复**:加 NJ 实例限定语法。

#### 2.3.9 EM bank 上限 15 未按型号门控 — **P3(实锤)**
- `core/constants.py:575`
- CP1H 仅 0-7。**修复**:文档或按型号收口。

#### 2.3.10 0x0504 错误码文本有误 — **已修复(2026-09-26)**
- `core/constants.py`
- 原文本「超过最大中继节点数(2)」有误;对照 W342-E1-18「Too many relays: over 3 networks away」改为「超过最大中继层数(最多 3 层网络)」。

#### 2.3.11 无 FINS-to-CIP 桥接 — **P3(文档化限制)**
- NJ 作 FINS 网关访问老 CJ/CS 从站不可用。**修复**:文档化。

#### 2.3.12 Connected messaging `vendor_id=0x1337` — **P3(待核证)**
- 部分 NJ 固件刷警告。**修复**:改注册厂商 ID 或暴露参数。

#### 2.3.13 FINS 结束码未处理标志位(bit15 / bit6-7)— **已修复(2026-09-26)**
- `plc/omron/codec.py`、`core/constants.py`
- 真机 `0x9005` = 网络中继错误标志(bit15)+ 主码 0x10/子码 0x05「头错误」;原实现按原始 16 位查表 → 落入"未知码"。现 `_end_code_text` 先屏蔽 `FINS_END_CODE_RELAY_ERROR_FLAG(0x8000)` 与 `FINS_END_CODE_CPU_ERROR_FLAGS(0x00C0)` 再查表,并把标志附于文本末尾(依据 Omron W342-E1-18 §5-1-3)。`last_error_code` 仍保留 PLC 原始码。

#### 2.3.14 FINS 以太网节点号范围过窄(126/127)— **已修复(2026-09-26)**
- `core/constants.py`、`plc/omron/omron.py`、`native/omron.py`
- 原 `FINS_NODE_DERIVED_MAX=126` / `FINS_NODE_MAX=127`,致本机/PLC IP 末段 >126(如 `192.168.0.192`)被误拒或推导失败。Omron W342-E1-18 明文「01 to FE: Ethernet (1 to 254 decimal)」「Node address 1 to 254」→ 改为 **254**;`_node_from_host` 与构造期路由校验随常量生效(显式节点 1~254、0 仍为自动标记；网络号仍 0~127、单元号 0~255)。

---

### 2.4 罗克韦尔 AB / Logix CIP

#### 2.4.1 0x0A 多服务包不查 504 字节非连接缓冲预算 — **已修复(2026-09-26)**
- `plc/ab/codec_cip.py:429-460`,`plc/ab/ab.py:571-595`
- 仅限 32 条数;504B 缓冲扣 UC 包裹后约 480B,长标签名 32 条必超,PLC 返资源错误,报错不指因。
- **修复**:按标签路径字节数估算,双约束切块。

#### 2.4.2 0x0A 部分响应(CIP 0x06/0x12)未续读 — **P2(实锤)**
- `plc/ab/codec_cip.py:463-496`
- 大批量字符串/数组截断即报错。
- **修复**:识别部分传输状态后续读。

#### 2.4.3 STRING 结构按 88 字节硬编码 — **P2(实锤)**
- `plc/ab/codec_cip.py:180-187`
- 复核:88B 为标准 Logix STRING(4B LEN + 82 字符 + 填充)结构,标准 STRING 读写正确;自定义长度 STRING(>82)受影响。v1 稿"写 100 字溢出"对标准 STRING 不成立。
- **修复**:按模板实际尺寸读写。

#### 2.4.4 类型缓存命中后不失效 — **P2(实锤)**
- `plc/ab/ab.py:92`
- 在线改 UDT 后缓存 stale,错数据无报错。
- **修复**:CIP 0x2107 时失效该条;可选 TTL。

#### 2.4.5 UDT 16 位成员 ID(0x8B 段)不支持 — **P2(实锤)**
- `plc/ab/codec_cip.py:345-374`
- **修复**:0x91 路径 0x05 时回退 0x8B。

#### 2.4.6 Forward Open RPI ≈ 2.1s — **已修复(2026-09-26)**
- `plc/ab/codec_cip.py`(`FO_OT_RPI`/`FO_TO_RPI`)+ `plc/ab/ab.py`(`rpi_us` 参数)
- 默认 RPI 由 ≈2.1s 降到 **100ms**(`AB_EIP_DEFAULT_RPI_US`),避免轮询间隔大于 ≈4×RPI 时连接被反复重建;新增构造参数 `rpi_us` 可按现场覆盖,`build_forward_open(..., rpi_us=...)` 同步暴露。

#### 2.4.7 `parse_service_reply` 附加状态口径含混 — **P3(实锤)**
- `plc/ab/codec_cip.py:853-857, 892-899`
- 复核:嵌入应答的扩展状态按 16 位字读取正确(`codec_cip.py:925-936`);UC 信封第 4 字节按库内口径为"附加状态长"但以字节步进偏移,两处口径不一致。正常帧(信封保留字节为 0)不受影响,异常帧偏移可能差 2 字节/字。
- **修复**:统一按规范口径并以单测注入非零附加状态验证。

#### 2.4.8 ENIP 0x66 命令号宽容接受 — **P3(文档化限制)**
- `plc/ab/codec_cip.py:267-307`
- 模拟器兼容的刻意放行,混杂网络下可能误配对。**修复**:可配开关。

#### 2.4.9 Forward Close 应答不解析 — **已修复(2026-09-26)**
- `plc/ab/ab.py`(`_forward_close`)
- `codec_cip.parse_forward_close_reply` 现被调用:非 0 状态记一条调试日志(关闭仍尽力而为),解析异常照旧静默。

#### 2.4.10 Slot 默认 0 对 1756-L8x 不适配 — **P3(文档化限制)**
- `core/constants.py:746`
- **修复**:文档强调。

#### 2.4.11 CIP 0x07(connection lost)不触发重连 — **已修复(2026-09-26)**
- `plc/ab/codec_cip.py`(`CIP_STATUS_CONNECTION_LOST` + `is_connection_reset_status`)+ `plc/ab/ab.py`
- 新增 0x07 Connection lost 常量并纳入"连接失效状态集"(0x01/0x07),connected 事务遇二者均按坏帧断开、下次惰性重连并重新 Forward Open。

#### 2.4.12 缺标签枚举(0x6B/0x0100) — **P3(实锤)**
- HMI 组态工具无法枚举标签。

#### 2.4.13 CPF 数据项长度双规约 — **P3(待核证)**
- `plc/ab/codec_cip.py:815-821`
- 部分 v32+ 固件长度口径差异。
- **修复**:宽容两种口径。

---

### 2.5 倍福 TwinCAT ADS

#### 2.5.1 transport 类 ADS 错误误分类为设备错误 — **P1(已修复:2026-09-26,依 TE1000 §8)**
- `plc/beckhoff/ads.py:97-113, 202-232`
- 0x0705/0x0706/0x0725(device/router removed)被当 DeviceError,不触发断线标记;TwinCAT 重启后客户端持续在死连接上失败。
- **修复**:transport 集错误抛 OSError 走惰性重连。
- **二轮复审订正(2026-09-26)**:上述修复所用错误码集 **0x705/0x706/0x725 本身是错的**——pyads 3.5.1 `errorcodes.py` 中它们是「parameter size / invalid parameter value」参数错误;真正的通断码是 `0x06`(Target port not found)/`0x07`(Target machine not found)/`0x0D`(Port not connected)/`0x12`(Port disabled)/`0x1B`(Host unreachable)/`0x1D`(TLS send)/router `0x0500~0x050D`。
- **最终修复(2026-09-26,TE1000 §8)**:码集改为上述正确集合,`0x705/0x706/0x725` 回归 `DeviceError`;回归测试 `test_translate_ads_error_transport_codes` + `test_translate_ads_error_parameter_codes_are_device`。

#### 2.5.2 STRING 写不预检声明长度 — **已修复(2026-09-26)**
- `plc/beckhoff/ads.py`
- 超长写靠 PLC 侧报错,紧邻变量布局下有污染邻区风险。现 `_write_string` 写前经 `_AdsSession.symbol_type` 取符号类型字符串(`STRING(N)`/`STRING`),`_declared_string_chars` 解析声明长度,超长抛 `ValueError`;符号信息不可用(旧路由/固件)时返回 None 跳过预检(不改变原行为)。

#### 2.5.3 Online Change 后句柄失效无显式处理 — **P2(待处理)**
- `plc/beckhoff/ads.py`
- 0x1D 错误未按"瞬时缓存 miss"分流。pyads 无稳定句柄语义,现有实现按符号名读取、无客户端侧句柄缓存,实际影响有限;待真机确认后再决定是否对特定错误码设置短线重试。

#### 2.5.4 默认 NetId `IP.1.1` 覆盖面 — **已文档化(2026-09-26)**
- `plc/beckhoff/ads.py`(模块 docstring + README)
- 默认 `IP.1.1` 覆盖常见单路由场景;自定义路由/TC2 多 runtime 需显式传 `net_id`。README ADS 段已注明。

#### 2.5.5 `set_timeout` 在部分路由器固件上无效 — **已修复(2026-09-26)**
- `plc/beckhoff/ads.py`(`_AdsSession.connect`)
- `set_timeout` 现校验返回值:返回 False 或抛异常时输出 WARNING(提示实际按 pyads 默认超时),不再静默忽略;连接仍照常建立。

#### 2.5.6 AMS 端口 851 默认对 TC2/多 runtime 不适配 — **已文档化(2026-09-26)**
- `core/constants.py`(`ADS_DEFAULT_ADS_PORT`)+ README
- 851 为 TC3 默认;TC2/多 runtime 需显式传 `ads_port`。README 已注明。

#### 2.5.7 >32KB 数据不切块 — **P2(待处理)**
- `plc/beckhoff/ads.py`
- 当前客户端仅支持标量/STRING 单点类型,单值不会触及 32KB;数组/大块符号支持时需按 32KB 分片,列为后续版本。

#### 2.5.8 `encoding` 参数静默忽略 — **已文档化(2026-09-26)**
- `plc/beckhoff/ads.py`(模块 docstring)
- ADS STRING 编解码由 pyads 固定 UTF-8,`encoding` 参数不生效;已在模块 docstring 明示(非静默:文档声明)。

#### 2.5.9 结构体成员路径不支持 — **P3(文档化限制)**
- pyads `read_by_name` 仅精确符号名,结构体成员需在 TwinCAT 侧导出为独立符号;属 pyads 能力边界,文档说明。

---

### 2.6 西门子 S7

#### 2.6.1 STRING 读实际长超 `length` 时静默返空串 — **已修复(2026-09-26)**
- `plc/siemens/client.py:414-416`
- `if actual <= 0 or actual > length: return ""` — 声明长大于入参时拿到空串,HMI 显示空白且无告警。
- **修复**:按 `length` 截断返回;读长改按 PLC 侧声明 max。

#### 2.6.2 STRING 写把声明 max 字段一并覆盖 — **已修复(2026-09-26)**
- `plc/siemens/client.py:419-429`
- `header = bytes([len(encoded), len(encoded)])` 声明长字节被改为实际长;后续按声明长处理的客户端判超长。docstring 自认"均按本次编码长度"。
- **修复**:先读声明 max,仅覆盖实际长字节。

#### 2.6.3 BOOL 位写为读-改-写,非原子 — **已修复(2026-09-26;文档+告警)**
- `plc/siemens/client.py`(`_write` docstring)
- S7 协议本身按字节写,无原子置位/复位原语,无法在协议层修复。已在 `_write` 加 **warning**:BOOL 写为非原子读-改-写,多写者场景请用 `write(addr, DataType.BYTE, v)` 写整字节。

#### 2.6.4 优化块访问错误无指引 — **已修复(2026-09-26)**
- `plc/siemens/client.py`(`_S7Session._raise_link_aware`)
- DB 区域读写出错且连接在线时,`DeviceError` 追加提示:"若为 S7-1200/1500,请确认该 DB 已在 TIA 中取消 Optimized block access"。

#### 2.6.5 PUT/GET 权限错误与离线不可区分 — **已修复(2026-09-26;文本细分)**
- `plc/siemens/client.py`(`_S7Session.connect`)
- snap7 连接失败无结构化错误码,无法程序化细分;连接失败文本改为列出三类可能原因:① IP/机架/槽位 ② 网络/防火墙阻断 102 ③ 1200/1500 未开启 PUT/GET 授权。

#### 2.6.6 无 WSTRING(UTF-16)支持 — **已修复(2026-09-26)**
- `plc/siemens/client.py`(新增 `read_wstring`/`write_wstring`,含 aio 镜像)
- WString 布局:声明长(2)+实际长(2)+UTF-16BE 字符;读按请求 `length` 截断,写保留 PLC 侧声明长、超声明长拒绝、非 BMP(代理对)拒绝。

#### 2.6.7 无多变量批量读 — **P2(待处理)**
- HMI 多点轮询 = N 次往返。**修复**:基于 snap7 多变量读(read_multi)实现批量,列为后续版本。

#### 2.6.8 8 字节类型无地址语法 — **已修复(2026-09-26;文档)**
- `plc/siemens/address.py`(模块 docstring)
- 明晰:地址只定位区域+字节起点,长度由 DataType 决定;LONG/ULONG/DOUBLE(8 字节)沿用 `DBD`/`DBB`/`DBW` 起点即可,`DB1.DBD6` 按 `read_long` 取 6~13 八字节。

#### 2.6.9 DB 编号边界未校验 — **已修复(2026-09-26)**
- `plc/siemens/address.py`(`_check_byte_index` + DB 编号校验)
- DB 编号 1~65535(DB0 拒绝)、字节起点 0~24 位(0~16777215);越界在解析层 `ValueError`,不再拖到运行时。

#### 2.6.10 `dll_path` 构造期不校验 — **已修复(2026-09-26)**
- `plc/siemens/client.py`(`__init__`)
- `dll_path` 非空但文件不存在时构造期 `ValueError`,不再拖到连接期才暴露。

---

### 2.7 基恩士 KV Host Link / MC / SR

#### 2.7.1 Host Link / MEWTOCOL 逐字节 recv 的整体截止时间 — **已核实(2026-09-26;无缺陷)**
- `plc/keyence/hostlink.py`(`_transact` TCP 分支)
- 复核:TCP 收行在进入循环前算一次 `deadline`,每字节只把 `receive_timeout` 收窄为 `remaining`,**不会**逐字节重置——整体受单一 deadline 约束。v1 稿断言不成立;MEWTOCOL 同法。

#### 2.7.2 Host Link UDP 固定 4096 缓冲 — **已修复(2026-09-26)**
- `core/constants.py`(`KV_MAX_DATAGRAM = KV_MAX_LINE + 16`)
- 原缓冲恰等于行长上限,响应行接近上限时 Windows 报 10040 / POSIX 静默截断(丢 CR/LF)。现留出 CR/LF 与余量。

#### 2.7.3 KV MC 位组记号 R 的编号口径 — **有争议(待真机核证)**
- `plc/keyence/mc.py:19-26`,`docs/real-machine-checklist.md`
- 复核:代码含 2026-09-26 决策注释"勿按 review K1 改"——现行"记号数字原样"(`R515→515`)依据 SH081257ENG 的 KEYENCE 设备格式约定,真机核证判据已留档。v1 稿"R515 实际打到 R2.3"的反向断言缺乏依据,撤回。
- **行动项**:按 checklist 真机核证后收口;核证前文档标警示。

#### 2.7.4 KV MC 缺 T/C 类软元件 — **P2(文档化限制)**
- `core/constants.py:306`
- 定时器/计数器当前值读不出。**修复**:补表或明确绕行路径。

#### 2.7.5 KV SLMP 无 4E 帧 — **已核实(2026-09-26;文档化)**
- `plc/keyence/mc.py`(模块 docstring)
- 基恩士 SLMP 兼容模式本身不提供 4E/1E,`frame` 恒为 3E;3E 无序列号字段,但库内事务锁保证同一连接串行收发,多 worker 仅排队、不会交错。已在模块 docstring 明示"仅 3E"。

#### 2.7.6 SR 仅 level-trigger — **P2(待处理)**
- `scanner/keyence_sr.py:81-117`
- 无 TRG/auto 模式,传送带高速场景不可用。**修复**:`mode` 参数(后续版本)。

#### 2.7.7 SR `scan_dwell` 持锁 sleep — **P2(待处理)**
- `scanner/keyence_sr.py:129`
- 多触发串行化。**修复**:拆分启停/轮询(后续版本;单枪场景串行化本身是正确语义)。

#### 2.7.8 SR `LON` 不带 bank 的老固件兼容 — **P2(待核证)**
- `scanner/keyence_sr.py:127`

#### 2.7.9 SR 超时后残响应可能串台 — **已缓解(2026-09-26)**
- `scanner/keyence_sr.py`(`_drain_line`)
- 超时后调用 `_drain_line` 尽力读到行尾清掉半行;`_drain_line` 现返回是否读净(供调用方判定残字节风险)。因"超时不断线"是既有契约(测试显式断言,滴流对端也保持连接),未强制拆连;迟到应答的彻底隔离需真机确认后再定。

#### 2.7.10 KV 设备范围不按型号门控 — **P3(待处理)**
- `plc/keyence/address.py:64-72`
- 各机型 DM/R/ZR 点数不同;需 `plc_model` 参数门控(后续版本)。

#### 2.7.11 超长 hex dump 刷日志 — **已修复(2026-09-26)**
- `plc/keyence/hostlink.py`(`_truncate_hex`)
- 响应行超限/缺结束符的错误文本改用 64 字节截断转储(带"共 N 字节"尾注),不再整段 4096B 刷屏。

---

### 2.8 汇川

#### 2.8.1 M 软元件上限合并 H3U/H5U — **已修复(2026-09-27)**
- `core/constants.py`(INOVANCE_BIT_DEVICES / INOVANCE_WORD_DEVICES)
- 复核:上限 8512 按 H3U 口径,H5U 的 M8000+ 越界报原始 Modbus 异常 02,不指因。
- **落地**:两表 docstring 逐项标注机型与手册页(H3U 9.4.3 印刷页 575-576 / H5U 9.5.1
  印刷页 418-419),说明超范围地址在两机型都落**地址空洞**(不会静默落错软元件);
  顺带按 H5U 放宽 X/Y 上限到 1024 点(X0~X1777)——见「汇川专项」。

#### 2.8.2 C200~C255 32 位计数器无字访问 — **已修复(2026-09-27)**
- `core/constants.py`(`INOVANCE_C32_BASE/FIRST/LAST`)+ `plc/inovance/address.py`
- **落地**:依 H3U 9.4.3(印刷页 575-576)C200~C255 自 **0xF700** 起、每只占两个 16 位
  寄存器空间(手册算例 C205 → 0xF70A、数量 8),补双寄存器展开 + 32 位类型门控
  (INT/UINT/FLOAT;手册注明 32 位寄存器不支持 FC06,写路径自然落 FC16)。

#### 2.8.3 缺汇川厂商 FC — **已结案(文档化边界,2026-09-27)**
- 配方传输等厂商功能不可用:H3U/H5U 编程手册的 Modbus 章节(9.4)只列标准
  FC 01/02/03/04/05/06/0F/10,厂商私有帧无手册依据,按铁律不臆造。
- **绕行**:自由协议帧用 `OpenTcpClient`(STX/ETX、长度前缀、编码回退链)自组。

#### 2.8.4 MC 帧与三菱的固件差异未文档化 — **已文档化(2026-09-27)**
- `plc/inovance/mc.py`(docstring)
- **落地**:手册 16.4(印刷页 661)点数码与三菱 Q/L 的差异列明——批量 960 字、批量位
  1~3584(iQ-R 1168)、随机读加权 ≤96、随机写加权 ≤960、随机写位 1~64;库内沿用三菱上限,
  汇川设备侧超限以故障码 C052/C053(16.5 印刷页 662)明确拒绝。

#### 2.8.5 R 设备映射的机型差异 — **已核证(2026-09-27)**
- 手册 16.4(印刷页 661-662)原文:通过 MC 访问 R 按访问 D 的方式写即可,PLC 自动将 D、R
  统一编址(访问 R0 → 访问 D8000);S 按三菱 L 写。与本库换算(`R n → D(8000+n)`、S→L 92h)
  一致;R 仅 H5U/Easy 侧存在,与实现文档相符。

#### 2.8.6 站号 >31 的网关兼容 — **P3(待核证)**
- 手册明载从站地址由 D8121 设定、取值 **1~247**(H3U 9.4.2),库内按标准 1~247 校验;
  >31 的网关限制属第三方设备侧行为,无手册依据,维持现状待现场核证。

---

### 2.9 松下 MC / MEWTOCOL

#### 2.9.1 R9000+ → SM 映射口径 — **待核证**
- `plc/panasonic/mc.py:50-96`
- 复核:现行"线性编号减 14400"为文档化口径(`R9008→SM8`、`R9010→SM16`);v1 稿主张"R9010 应为 SM10"把十六进制记法当十进制,论据不成立。库同时提供 SM 直接寻址路径,可绕开 R→SM 换算。
- **行动项**:对照 FP0H 以太网手册 SM 映射表真机核证;核证前文档标警示。

#### 2.9.2 MEWTOCOL 5 位地址字段溢出 — **P2(实锤)**
- `plc/panasonic/codec_mewtocol.py:93-103`
- 超界地址溢出字段,PLC 静默拒。
- **修复**:预校验报 `ValueError`。

#### 2.9.3 MEWTOCOL 3 位 X/Y 字字段溢出 — **P2(实锤)**
- `plc/panasonic/codec_mewtocol.py:83-90`

#### 2.9.4 T/C 字访问指引的准确性 — **P2(待核证)**
- `plc/panasonic/mewtocol.py:71-86`
- S=设定值/K=经过值的语义需对照手册复核。

#### 2.9.5 错误响应长度硬编码 — **P2(待核证)**
- `plc/panasonic/mewtocol.py:204-208`
- 不同代际 BCC 宽度差异。

#### 2.9.6 SD 位访问未按机型门控 — **P3(待核证)**
- `plc/panasonic/mc.py:64-65`

---

### 2.10 丰田 TOYOPUC

#### 2.10.1 缺扩展区命令(CMD 0x94/0x95) — **P2(实锤)**
- `plc/toyopuc/toyopuc.py`

#### 2.10.2 缺多站命令(CMD 0x60/0x61) — **P2(实锤)**

#### 2.10.3 缺 PLC 状态/错误日志查询(CMD 0x70/0x7E) — **P2(实锤)**

#### 2.10.4 L 第二段位编码假设未文档化 — **P3(待核证)**
- `plc/toyopuc/address.py:53-63`

---

### 2.11 OPC-UA

#### 2.11.1 断线不自动重订订阅 — **设计边界(文档化限制)**
- `opcua/client.py:326-327, 424-438`
- 复核:docstring 与 README 双处明示"订阅重建策略留调用端",属已知设计取舍,不作为缺陷计。
- **建议**:长期看仍应提供 `auto_resubscribe` 选项,否则监控类应用需自行保存订阅规格。

#### 2.11.2 推模式无 Deadband 过滤 — **P2(实锤)**
- `opcua/client.py:630-704`
- 高频模拟量回调洪泛。
- **修复**:`deadband_value/deadband_type` 透传 DataChangeFilter。

#### 2.11.3 NodeId 不识别 `t=`/`n=` — **P2(实锤)**
- `opcua/address.py:25-28`
- **二轮复审订正(2026-09-26)**:本条论据不成立——asyncua 1.1.5 `uatypes` 本身也只认 `ns/i/s/g/b/srv/nsu`,`t=`/`n=` 非标准 NodeId 标识符。真实缺口是正则拒绝 `s=` 内含分号与 ExpandedNodeId 的 `srv=`/`nsu=`(转 P2「OPC-UA」条)。

#### 2.11.4 无安全策略/证书支持 — **设计边界(文档化限制)**
- `docs/architecture.md` 明示内网口径。强制 SignAndEncrypt 的服务器接不上。
- **建议**:随 v1 评估放开。

#### 2.11.5 `browse` 深树递归爆栈 — **P2(实锤)**
- `opcua/client.py:587-624`
- **修复**:显式栈迭代。

#### 2.11.6 `read_many` 强制同类型 — **P3(实锤)**
- `opcua/client.py:490-503`

#### 2.11.7 类型强校验拒收 bool→数值 — **P3(实锤)**
- `opcua/client.py:828-840`

#### 2.11.8 回调跨关闭 loop 触发被静默吞 — **P3(实锤)**
- `opcua/client.py:239-263`

#### 2.11.9 `browse` 仅默认 ReferenceType — **P3(实锤)**
- `opcua/client.py:596`

#### 2.11.10 GUID 格式不校验 — **P3(实锤)**
- `opcua/address.py:25-28`

#### 2.11.11 aio 层订阅关闭不保证释放 asyncua 内部线程 — **P2(待核证)**
- `aio/__init__.py:1262-1313`

---

### 2.12 MTConnect

#### 2.12.1 无 `/sample` 历史流 — **P2(实锤)**
- `cnc/mtconnect.py`

#### 2.12.2 UNAVAILABLE 仅文本值检测 — **P3(待核证)**
- `cnc/mtconnect.py:46, 390`
- 复核:标准 Streams XML 中 UNAVAILABLE 即文本值,现行检测覆盖主路径;v1 稿"2.0 元素式表示"论断存疑。
- **修复**:补空元素形态兼容(低成本)。

#### 2.12.3 `/probe` 仅返首个 Device — **P2(实锤)**
- `cnc/mtconnect.py:359-369`

#### 2.12.4 `/asset` 未实现 — **P2(实锤)**

#### 2.12.5 条件项无过滤维度 — **P3(实锤)**
- `cnc/mtconnect.py:334-358`

#### 2.12.6 HTTP keep-alive 策略不可配 — **P3(实锤)**
- `cnc/mtconnect.py:124-127`

---

### 2.13 OpenTcp(通用 TCP)

#### 2.13.1 无编码回退链 — **已修复(2026-09-26)**
- `opentcp/client.py`(新增 `encoding_fallback`,`_decode_bytes` 逐候选编码尝试)
- 首选编码解码失败不再立即判坏帧;按 `encoding_fallback` 顺序回退(如 `["gbk"]`),全部失败才按坏帧抛错并列出候选编码。

#### 2.13.2 无 STX/ETX、无"长度+分隔符"组合成帧 — **已修复(2026-09-26)**
- `opentcp/client.py`(新增 `start_marker`、`length_prefix`(+`length_prefix_byteorder`))
- 分隔符模式可选 `start_marker`(标记前噪声丢弃、跨分片保留标记前缀);新增长度前缀成帧(1/2/4 字节长度域 + 该长度载荷,与 `delimiter`/`frame_length` 互斥)。

#### 2.13.3 帧长上界检查时机 — **已修复(2026-09-26)**
- `opentcp/client.py`(`_buffer_limit`/`_receive_frame`)
- 缓冲硬上限改为 `max_frame + 成帧开销`(长度域/起始标记 + 分隔符),条件 `>=`;既不多驻留 1 字节,也不误伤"内容满 `max_frame` 但分隔符尚缺"的合法帧。

#### 2.13.4 buffer 头部删除 O(n) — **P3(待处理)**
- `opentcp/client.py:365, 378`
- **修复**:读指针或 deque。

#### 2.13.5 `recv_chunk=256` 硬编码 — **已修复(2026-09-26)**
- `opentcp/client.py`(新增 `recv_chunk_size` 参数与只读属性,`_receive_frame` 按其读取)

#### 2.13.6 大块发送无进度回调 — **P3(待处理)**

#### 2.13.7 部分帧残数据不留诊断 — **已修复(2026-09-26)**
- `opentcp/client.py`(新增 `last_partial_frame` 属性;超时时保存缓冲残字节,成帧成功后清空,重连清空)

#### 2.13.8 无 IPv6 — **P2(实锤)**
- `transport/tcp.py`、`transport/udp.py`
- **修复**:`family` 参数 + `getaddrinfo`。

---

## 三、异步层

### 3.1 aio 属性读会进入同步事务锁 — **P1(实锤)**
- `aio/__init__.py:220-293`,`core/base_client.py:248-252`
- 复核确认:aio 的 `connected`/`last_error*`/`stats` 直接转发同步实例属性,而这些属性在同步侧持事务锁;事件循环读属性最多阻塞一个完整事务(含重试可达数秒)。docstring"不发报文也不切线程"字面为真,但"不阻塞"会被误读。
- **现场影响**:多协程轮询状态灯在事务高峰期串行排队。
- **修复**:同步侧属性改无锁快照(原子字段),或 aio 层维护镜像字段。

### 3.2 每客户端独立单线程 executor — **P2(实锤)**
- `aio/__init__.py:183-185`
- 多设备高扇出时线程数与 GIL 争用;文档已说明收益边界。
- **修复**:文档强化推荐 native 层;可选共享池。

### 3.3 `wait_for` 不能取消已提交的写事务 — **P2(文档化限制)**
- `aio/__init__.py:202-206`
- docstring 已明示。**修复**:提供对齐 `receive_timeout` 的助手。

### 3.4 aio close 吞异常 — **P3(实锤)**
- `aio/__init__.py:464-466`
- **修复**:白名单 + WARNING。

### 3.5 close 先置 `_executor=None` 再排空 — **P2(实锤)**
- `aio/__init__.py:457-477`
- 进行中事务的重试路径可能读到 None 抛错。
- **修复**:generation counter。
- **二轮复审复核关闭(2026-09-26)**:不可能命中——`_ensure_open` 与 `run_in_executor(self._executor, …)` 相邻且无 `await`,进行中操作从不读 `_executor`;`test_close_gate_refuses_new_calls_while_draining` 已锁 FIFO/排空行为。

### 3.6 native UDP 写重试的双写风险 — **P3(实锤,默认关闭)**
- `native/base.py:613-655`
- 复核:与 §1.2 同口径——默认 retries 关闭时的风险是 opt-in;UDP 写超时重发语义文档需强化。

### 3.7 native 跨循环/跨线程使用不检测 — **P2(实锤)**
- `native/base.py:717-723`
- **修复**:loop 失配即抛 RuntimeError。

### 3.8 native `connect()` 取消后半开 socket — **P3(实锤)**
- `native/base.py:160-220`,`native/transport.py:252-262`
- 3.11+ 未 await `wait_closed()`。
- **修复**:按版本分支 await。

### 3.9 UDP `peer_ip` 回退走阻塞 DNS — **P2(待核证)**
- `native/omron.py:441-458`
- 主机名配置时事件循环冻结风险。
- **二轮复审复核关闭(2026-09-26)**:`AsyncUdpTransport.connect` 总把已解析对端以字面量 `peer_ip` 暴露,`omron.py:454` 取到的必是 IP,`_node_from_host`/`_local_ip_for` 永不解析主机名。

### 3.10 native 无 IPv6 — **P2(实锤)**
- `native/transport.py:386-398`

### 3.11 TCP connect 取消的 fd 残留 — **P3(待核证)**
- `native/transport.py:235-239`

### 3.12 取消计入 `disconnect_count` — **P3(实锤)**
- `core/base_client.py:716-725`
- **修复**:取消不计断开。
- **二轮复审复核关闭(2026-09-26)**:取消时 `pending=True` 确可能留下无配对响应,按保守拆连计数是正确口径;`test_native_modbus.py:288` 显式断言 `disconnect_count == 1`,原「取消不计断开」建议会破坏该守卫。

---

## 四、测试与工程

### 4.1 CI 仅 Python 3.12,声明支持 3.7.9 — **已修复(2026-09-26)**
- `pyproject.toml:6` + `.github/workflows/ci.yml`
- 版本策略已裁决:**3.7 必保**(现场老设备 vendor SDK 依赖 3.7),**3.12 覆盖新环境**(能装 3.8~3.11 的设备同样兼容 3.12),3.7 + 3.12 两点即覆盖全区间,**不砍 3.7**。
- CI 改为矩阵 `["3.7.9", "3.12"]`:3.7 腿用 `actions/setup-python` 安装(uv 托管下载不含 3.7),`UV_PYTHON_DOWNLOADS=never` 仅限 `uv sync`/`uv run` 两步(设 job 级会挡住 `uvx` 建工具环境),`uv sync`/`uv run` 一律显式 `--python`(压过 `.python-version` 的 3.7.9);**ruff / mypy / ty 两条腿都跑**——ruff 与 ty 为 Rust 独立二进制(不依赖解释器),mypy 用 `uvx --python 3.12` 固定在 3.12 工具环境执行(否则 3.7 腿会回退到 3.7 时代的 mypy 1.4.1 + typed-ast,其 tokenizer 解析 UTF-8 中文源码误报 `non-utf8 code starting with '\xc8'`);检查目标仍由 `python_version` 决定。
- **残留**:GitHub Actions 行为只能由推送后实跑验证;`windows-latest` 未来若移除 3.7 构建需改 `windows-2019`。

### 4.2 无故障注入测试 — **已修复(2026-09-26)**
- `tests/unit/chaos.py` + `tests/unit/test_fault_injection.py`(新增)
- 无半帧、慢速、错长度、断管等注入;模拟器全绿不等于产线可用。
- 现新增真回环故障注入服务 `chaos_server(behavior)`(每连接可编排故障,支持 `recv_request`/`drip` 慢速工具),覆盖:半帧后关闭(拆连)、逐字节滴帧(跨分片成帧)、静默超时(不被拖死、OpenTcp 超时不断线归 `DEVICE`)、MBAP 声明长度不达(超时)、断管后惰性重连(第二次成功)、3E 半帧头后关闭(拆连)。
- **残留**:UDP 截断(`MSG_TRUNC`)跨平台语义差异大,未纳入本轮。

### 4.3 aio/native 镜像仅验方法存在不验转发 — **P2(实锤)**
- `tests/unit/test_aio_mirror_surface.py:51-62`
- **修复**:补真调用烟测。

### 4.4 无 `pytest-timeout` — **P2(实锤)**
- 死锁测试可挂满 CI 超时。

### 4.5 pyserial 及 asyncua/pyads/comtypes 全 `==` 锁 — **P2(实锤)**
- `pyproject.toml:65-68`
- 上游 bugfix 进不来(asyncua 订阅泄漏修复对应 §2.11.11)。
- **修复**:`>=X,<Y` 范围。

### 4.6 dev extras 与协议 extras 重声明 — **P3(实锤)**
- `pyproject.toml:62-90`

### 4.7 mypy `python_version=3.9` 与运行时下限错位 — **P3(实锤)**
- `pyproject.toml:112`

### 4.8 ruff 规则集过窄 — **P3(实锤)**
- `pyproject.toml:106-107`
- **修复**:补 B 组(raise-from、mutable default)。

### 4.9 `Development Status :: 3 - Alpha` — **P3(实锤)**
- `pyproject.toml:48`
- 受控行业审计按分类器直接挡门外,与实际质量无关。**修复**:升 Production/Stable。

### 4.10 缺 PEP 735 dependency-groups — **P3(实锤)**
- `pyproject.toml:81-90`

### 4.11 S7 依赖双轨(1.3 C-ext / 3.2.0 pure-py) — **P3(实锤)**
- `pyproject.toml:76-80`
- 32 位需自备 DLL 无 marker 把关。
- **修复**:`platform_machine` marker。

### 4.12 测试夹具非 Windows 进程安全 / 仅 Selector 覆盖 — **P3(待核证)**
- `tests/conftest.py:30-38`,`tests/unit/test_native_transport.py:163-194`
- Windows Proactor(产线默认)路径未覆盖。

### 4.13 属性读不阻塞事件循环的断言缺失 — **P3(已修复:2026-09-26)**
- §3.1 的主张无测试保护。
- **修复**:连同 P1「aio 属性读阻塞」一并落地——同步只读 getter 改无锁快照,回归测试 `test_v034_reliability.py::TestAioBackoffAndErrorSurfaces::test_property_reads_do_not_block_on_transaction_lock`(事务锁被占时逐一断言 `connected/last_error*/stats/next_connect_in` 读取不阻塞)。

---

## 五、按优先级排的"先修这 10 条"(复核后)

> 状态(2026-09-26):本表 10 条**均已落地**(9 条代码修复 + CI 版本矩阵已按"3.7 必保"裁决修改)。
> **二轮复审(2026-09-26)修正**:其中 §2.5.1(ADS transport 错误码)落地有误——所用错误码集本身是错的(2026-09-26 **已依 TE1000 §8 订正修复**);另新增 MC `L` 设备码撞码 P0(同日修复)。详见文首「二轮复审」。
> 下一批候选见「二轮复审」与对话记录。

| 优先级 | 项 | 章节 | 现场影响 |
|---|---|---|---|
| P0 | MC 位软元件 `.bit` 后缀静默丢弃(`plc/melsec/melsec.py:327-329`) | §2.2.1 | `M10.5` 实际读写 M10,错位无报错 |
| P1 | MC 设备码表缺 T/C/SM/SD/G 等(`core/constants.py:218-229`) | §2.2.3 | 定时器/模拟量/特殊寄存器全部不可用 |
| P1 | NJ BOOL 数组 DWORD 打包(`plc/ab/ab.py:530-535`) | §2.3.3 | NJ 上每条 BOOL 数组元素错位 |
| P1 | NJ STRING 拒绝(`plc/omron/cip.py:77-85`) | §2.3.4 | HMI 文本场景 NJ 客户端不可用 |
| P1 | AB 0x0A 不查 504B 预算(`plc/ab/codec_cip.py:429-460`) | §2.4.1 | 长标签名批量读必失败 |
| P1 | S7 STRING 读超长返空串(`plc/siemens/client.py:414-416`) | §2.6.1 | HMI 文本静默空白 |
| P1 | S7 STRING 写覆盖声明长(`plc/siemens/client.py:425`) | §2.6.2 | 破坏声明 max,跨客户端读全错 |
| P1 | ADS transport 错误误分类(`plc/beckhoff/ads.py:97-113`) | §2.5.1 | TwinCAT 重启后客户端卡死(**已修复:TE1000 §8 码集**) |
| P1 | CI 仅 3.12 + 无故障注入(`.github/workflows/ci.yml:30`) | §4.1/4.2 | 3.7 产线环境与真实网络工况未验证 |
| P2 | FX5U X/Y 十六进制口径(`core/constants.py:232-234`) | §2.2.2 | 文档化限制,但 FX5U 静默错位风险真实 |

---

> 本文档仅记录问题与现场影响。v2 复核删除 8 条误报、修正 12 条口径;条目按"实锤 / 文档化限制 / 待核证"分档。2026-09-26 二轮全量复审新增 P0/P1/P2/P3 清单及台账纠偏,见文首「二轮复审」;同日修复 P0(MC `L` 撞码)与 P1 四项(ADS 码集依 TE1000 §8、S7 WString ASCII、Tag 64 位精度、aio 属性读锁)、P2 批 1(native 镜像、Modbus 批量 `.bit`、`is_write`、FINS 批量 `.bit`、AB 0x0A 预算、MTConnect DOCTYPE、退避溢出)、P2 批 2(convert 校验、OPC-UA 深度/收窄/NodeId、OpenTcp 超时/前缀/上界、FC20 边界)与 P3 的 aio `close()` 取消吞没。修复路线图不列入,按项目排期另起 `docs/fix-roadmap.md`。