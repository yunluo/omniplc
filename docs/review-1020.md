# review-1020 —— 全量审查(第十八轮,2026-10-08)

> 前轮口径:review-1017 起恢复落档;review-1017/1018 随闭环删除(1f69205),
> 本文档存活至修复闭环,届时同法删除,结论存续 = CHANGELOG + 协作记忆。
> 本轮为**全量审查**:7 路并行子代理切片 + 主会话逐条裁决;只查不修。
>
> **闭环状态(2026-10-08)**:修复批已随 **v0.55.7** 全量落地(e79dec0——
> P1×5 全修,含 1E 256 点帧字节修正「1 字节点数+恒零域」;P2×4 全修;
> P3×20 处置;P2-5 SR「裸 OK」判据待 SR-1000 手册/真机终裁暂不修,入真机
> 清单;真机核证五项入清单;门禁 1866 passed + 11 skipped。「EzFeedSpeedType
> 缺 FE 成员」审查补充项经 `git log -S` 核实不成立,撤销)。逐项处置明细见
> CHANGELOG v0.55.7 条目。§一~五转为审计存档;**§七方向清单仍待立项,
> 本文暂缓删档,随 §七 立项吸收后再删**。

## 一、范围与背景

- 起点:v0.55.4(647ab59)+ 汇川双记号三提交(f77a5f1 / 51b4176 / 61eb2c4,
  审查启动时尚未推送);门禁五件套全绿(**1851 passed / 11 skipped /
  0 failed**,本轮开头逐字确认)。
- **审查进行中的并行演进**:汇川双记号已随 v0.55.5 发布(91f01e7),
  read_range 漏挂翻译项已随 b80b6ed 修复并发布 v0.55.6(a4bd1c0)——
  本轮 P1-4 因此闭环(见下),其余发现不受影响(b80b6ed 仅动汇川
  read_range 挂钩与测试)。
- 风险加权:1018 修复批之后落地的大批量变更 = 重构清理 48c1b9b、性能两批
  fa2d174/271ea56、汇川双记号 f77a5f1,以及覆盖最少的新驱动(FANUC FOCAS /
  EZSocket M70 / MTConnect)。
- 切片:A 汇川双记号深审 / B 性能·重构批等价性 / C CNC 三驱动 / D 三菱族 /
  E Modbus+读码器族 / F 稳定族(欧姆龙·西门子·AB·松下·丰田·基恩士MC·OPC-UA)/
  G 跨切面基座(core/transport/native/aio)。
- 方法:子代理纯代码审查(帧面对档案页码 + 新旧全文对照),主会话对全部
  P1/P2 与 P3 抽样逐条复核;本轮**零误报**,两条「疑似」经手册/档案原文
  复核升级为实锤。

## 二、结论

**P0 = 0。P1 = 6(其中 1 条审查期间已随 b80b6ed 闭环),P2 = 5(其中 2 条
待真机终核),P3 ≈ 20(文档/登记为主)。**
性能批/重构批(三提交)经双向审查**零缺陷**(B 路逐文件新旧对照全部等价);
汇川双记号核心行为面(六方法挂载/翻译表/分组相互作用/纯 Modbus 零破坏)全部通过。

## 三、发现清单

### P1(6)

| # | 位置 | 问题 | 证据 |
|---|------|------|------|
| P1-1 | cnc/focas.py:512 | `set_debug(True)` 时 FOCAS connect **必失败**:`log_op("会话已建立(cnc_id={})")` 用 `{}` 占位,`log_op` 是 %-风格(core/debug.py:204 `% args`)→ TypeError 被 connect 捕获记「连接初始化失败」 | 调试排障场景 FOCAS 无法建连,last_error 误导;默认关闭时测试全绿未覆盖(绿着错同型) |
| P1-2 | cnc/ezsocket.py:1067 | `read_feed_speed(FE)` 请求 42/**5**,应 42/**4**:`int(feed)+1` 只对 FA/FM/FS 成立 | 同文件 :226 docstring 自证「FE(42/4)」;档案 m70-ezsocket README §4 表 + C 库 `case FE: 42,4` 双证;FC 已特判 33/1 |
| P1-3 | cnc/focas.py:522 | `cnc_settimeout` 档案明示单位 **ms**(fwlib32.h 15128 行,形参名 ms;同表 allclibhndl3 才是秒),代码传 `int(receive_timeout)` 秒值 → 默认 3 秒收超时变 **3ms** | 档案 docs/protocol/fanuc/README.md §1 + 测试假 DLL 形参名 ms 双证;真机预期 EW_SOCKET 超时风暴,单位结论随修复批真机验证 |
| P1-4 | tests/test_virtual_servers_integration.py:245 | ~~汇川集成用例启用即恒红~~ **已闭环(审查期间)**:`read_range("D400",…)` 走 Modbus 基类抛「无法解析 Modbus 地址」,D7021 回归断言永不可执行——b80b6ed 给 read_range 挂上 translate_batch_address(同步+native)后用例可达,随 v0.55.6 发布 | 原证据:OMNIPLC_INTEGRATION=1 + 服务器在位时 100% 失败且易误诊为模拟器 RST 劣化(恒红掩盖);修复后该用例转为有效回归 |
| P1-5 | native/base.py:798-820 | native 六个类型化写(write_short~write_ulong)用 `int(value)` **静默收窄**(int(1.9)→1、int(True)→1、int("3")→3),同步侧同方法 require_int 显式拒绝(core/base_client.py:902-924,第八轮 P2-6 有意加固);retries/write_retries setter 同病(:542-546/:553-557) | 同文件 write_bool(:794)自己就写了「与同步同口径」的显式拒绝——移植/双栈场景同调用两侧行为分叉,native 侧静默错值写 |
| P1-6 | plc/melsec/codec_a.py:122 | MC **1E 帧 256 点编码与手册相悖**:`points.to_bytes(2,"little")` 在 256 点发 `00 01`;SH-080008 印刷页 402「Number of device points……**Send a 1-byte numerical value**」「256 点指定 00H」+ 印刷页 403/405 独立 **Fixed value 恒 00H** 域,算例尾域 `0CH 00H`/`02H 00H` | 线上实为「1 字节点数 + 1 字节恒 0」,256 应发 `00 00`;1~255 两种写法字节巧合一致,唯恰 256(=MC_1E_MAX_POINTS 上限)相悖;E71 实际接受度真机终核,修法=低字节点数(256→00)+高字节恒 0 并同步 constants 注释(review-1009 口径注释一并订正) |

### P2(5)

| # | 位置 | 问题 | 证据/状态 |
|---|------|------|------|
| P2-1 | cnc/ezsocket.py:545-560 | `decode_get_data` 对「类型所需字节不足」无防护:data_length=0 + T_CHAR → IndexError;不足 2/4/8/16 字节 → struct.error,**逃逸 _execute 契约直抛且不拆连** | 同文件 decode_prog_block(:590)有防护先例;坏帧应 ProtocolFrameError+format_hex 收口(库约定)。实锤 |
| P2-2 | native/base.py:374-376 | native `close()` 缺 `_check_loop_affinity`,与 `disconnect()`(:321,第八轮 P2-15)不对称:跨循环 close 经 `_guard` 静默换锁 → 心跳 cancel 跨循环 await 抛错被吞 → transport.close 跨循环,清理可能未落地(fd 泄漏) | 实锤;单循环正常用法无碍 |
| P2-3 | codec_a.py:136-156、melsec.py:1348-1401 等 | MC **1E 全部坏帧路径 + 4C 收包层坏帧缺 format_hex 原始数据转储**(1E 响应头不足/副头部不符/缺扩展字节;4C 非 DLE STX/数据长非法/F8 识别码/附加码后非 10H/1C·3C 控制码) | 违背坏帧诊断口径;3E/4E/3C/4C codec 层均带转储,唯此两处缺口。实锤 |
| P2-4 | native/omron.py:409 | `read_range` BOOL 分支在 `_execute` **之外**预组帧:无显式 connect 首调时,auto 节点模式以 DA1=0(非法域,合法 1~254)出帧;同步侧在事务闭包内组帧(omron.py:434-439,惰性重连+握手**之后**);字分支在事务内(:427-429),唯此一处不对称 | 结构实锤;宽容 PLC 收下/严格拒绝,影响程度待真机核 DA1=0 接受度 |
| P2-5 | reader/keyence_sr.py:137 | 「裸 OK=无读出」判据手册无据:SR-2000 手册仅证「ERROR=未读到」,未检得「无读输出 OK」原文;对比模式/条码内容恰为 "OK" 时**真读被吞**成 (False,None) | 疑似(可能承自 SR-1000/家族惯例),待 SR-1000 口径手册或真机终核 |

### P3(20,登记/文档为主)

| # | 位置 | 问题 |
|---|------|------|
| P3-1 | native/inovance.py:12-13 | 模块 docstring 陈旧:仍写「批量直承 Modbus 按 Modbus 记号,汇川记号拒绝」,与同文件六批量覆写(f77a5f1 新增)直接矛盾;类 docstring 已更新,模块头漏更 |
| P3-2 | plc/inovance/inovance.py:56-57 | 类 docstring 超 claim:「单点读写…都支持双记号」——实测单点仅认汇川记号(hr100/40001 单点均拒);「双记号」只在批量成立 |
| P3-3 | plc/inovance/address.py:169-181 | `translate_batch_address` 的 C32 类型门控为死代码:C 软元件必先被 `is_modbus_address` 的 Modbus `c` 前缀分支拦截,门控永不触发,「与单点同款」披露失真(行为本身与 C 歧义拍板自洽) |
| P3-4 | modbus.py:1096、native/modbus.py:1013 | `read_fifo_queue`(FC24,收地址参数)未挂汇川翻译钩子且零披露,与 read_range 同性质(read_range 已随 b80b6ed 修复,FC24 仍未挂),建议并入同一登记项 |
| P3-5 | cnc/focas.py:828-838 | connect 内 `apply_receive_timeout()` 只捕 (DeviceError,OSError),EW_NODLL/EW_MMCSYS 分支抛 OmniPLCInternalError 会从 connect() 逃逸(会话已建),违背 connect()→bool 契约 |
| P3-6 | cnc/focas.py:326-345/474-498 | DLL 生命周期:每次 connect 新增模块引用计数从不 FreeLibrary(不重复映射,危害轻,登记口径) |
| P3-7 | cnc/mtconnect.py:269-272 | 每连接首个请求 `conn.sock is None` 跳过 settimeout,首请求实际超时=构造参数 connect_timeout(5s≠3s) |
| P3-8 | cnc/mtconnect.py:459-465 | `_ping_probe` docstring「逐请求建连」与实现(keep-alive 复用+失效原位重建)口径不符 |
| P3-9 | cnc/focas.py:514-524 | receive_timeout 连接后修改只写传输属性不重发 DLL;作用域未按第八轮 P2-5 分类口径声明(OPC-UA/MX 同类有声明) |
| P3-10 | cnc/ezsocket.py:904-939 | read_run_state 任一段失败即整体 (False,None),C 库同函数失败段仅告警返回已得状态——语义更严非错值,与「C 库同构」声明存在未登记差异 |
| P3-11 | plc/melsec/codec_qna.py:11-12 | 模块头「字块数(2)…位块数(2)」与 1009 修复后的 1 字节实现(:274/:277)矛盾,易被按注释回改 |
| P3-12 | plc/melsec/melsec.py:1422 | 注释「字 0x2030」应为 0x3020(印刷页 114 位图线上字 30H 20H);解码逻辑本身正确 |
| P3-13 | native/melsec.py:859/898-934 | native 侧 4E 帧文档缺位:`_next_serial` 写「3E 序列号」应为 4E;类与构造 docstring 写「1E/3E」而 _SUPPORTED_FRAMES 含 4E |
| P3-14 | codec_qna.py:651-654 等 | MC 结束码/错误码无文本表(登记项;可收依据 SH-080008 §5.3 印刷页 44 / §4.3 印刷页 38 / §18.2 印刷页 395;库内 KV/TOYOPUC 先例在) |
| P3-15 | melsec.py:675-677、native/melsec.py:624 | random_write 位软元件 1402 字访问=整字下发,写 1 点清相邻 15 点,docstring 缺警示(帧面本身正确) |
| P3-16 | mx.py:466-467 | GetErrorMessage 零参形态未校验返回码(疑似,同 GetDevice 族盲区,建议并入同一真机批) |
| P3-17 | native/modbus.py:81 | 类 docstring 站号仍写「1~247」,实际收口 0~255(native/modbus.py:114;1001 R9-1 同步侧已修,native 一行漏改) |
| P3-18 | modbus.py:446 | 同步读合并 max_unit 硬编码 2000/125,未用 MODBUS_MAX_* 常量(写侧与 native 均用常量;数值当前一致,纯漂移风险) |
| P3-19 | reader/keyence_sr.py:234/268 | 收行/排残仍逐字节 recv(1),未复用 KV fa2d174 的 recv_some 批收(每字节=超时下发+系统调用,约 60 次/30 字符;帧面正确,性能/一致性) |
| P3-20 | ab.py:658、opcua/address.py:65、base_client.py:113/147、native/base.py:783-785、transport/udp.py:124-150 | 五处小项:①ab docstring 声称 NJ/NX 覆写 `_batch_bool_array_address` 实际不存在(行为正确,注释误导);②OPC-UA lru maxsize 硬编码 4096 未引 ADDRESS_CACHE_MAXSIZE;③validate_endpoint `int(port)` 静默收窄(502.9→502、True→1);④native read_string 对非 str 返回 `str(value)` 包装(同步侧显式拒);⑤UDP MSG_TRUNC 截断探测疑为 Linux 特有,macOS/BSD 可能静默截断(平台特定待核,协议层长度校验兜底) |

## 四、逐项核实(零发现面)

- **B 路(性能/重构等价)零缺陷**:Modbus 写合并 O(n) 的「连续切片」不变量、
  RMW 恒先、返回序、部分失败语义与旧实现等价;KV 收行 deadline/EOF/分包一致;
  MX 记忆化键完备(类型,方法)+异常不入缓存+绑定方法不缓存在位;convert
  struct 化全调用点字节序核对无误;271ea56 各 codec struct 化处全部有严格长度
  前置;48c1b9b 20 文件删除项确证死代码、助手复用语义等价。两处有意语义差异
  (KV 残留字节丢弃/MEWTOCOL fromhex 严格化)均已有披露。
- **汇川双记号(A)**:六方法同步/native 镜像覆写齐全,aio 转发不丢翻译;
  翻译表与 1011 已核换算表一致;纯 Modbus 面(address.py 仅追加
  is_modbus_address)与海康继承族零波及;分组/合并不受翻译影响。
- **S7(F)**:271ea56 地址解析缓存键安全(缓存函数不含 model,V 区翻译与
  WString 门禁在缓存外实例层);**review-1017 两项开放发现确认已修复在位**
  (native/siemens.py:683-686 length 守卫 + 档案 §11 订正,随更早批次落地);
  竞品名中性化未删弱依据注释(「依据」注释数前后相等)。
- **Modbus(E)**:codec 全 FC 面复认通过(含 FC24/FC43 与异常码表);写合并
  新实现帧面正确;海康四驱动握手/出参/双通道复核通过。
- **跨切面(G)**:回显校验全库盘点**无新缺口**;心跳/监视器线程生命周期、
  UDP drain、超时三分语义、`_after_connect_failure` 顺序、constants 抽查
  (FC 上限/1E=64/S7 分片 −18−35/六款预设)全部在位。
- CNC(C):FOCAS 七函数签名/结构体布局/ctypes 出参全对;EZSocket 帧
  字节数/错误码偏移/类型码与 C 库一致;MTConnect 16MiB/XXE/锚点页码属实。

## 五、测试态与真机核证项

- 门禁五件套:起点(61eb2c4)1851 passed / 11 skipped / 0 failed;收口
  重跑(v0.55.6,a4bd1c0)**1857 passed / 11 skipped / 0 failed**,
  ruff format/check、mypy、ty 全绿(逐字确认 0 failed)。
- 新增待真机核证(随修复批入清单):
  1. MC 1E 256 点:`00 00` 编码 E71 实际接受度(P1-6 修复后验证);
  2. FOCAS cnc_settimeout ms 单位实测(P1-3 修复后验证);
  3. FINS DA1=0 首调接受度(P2-4,顺带核严格 PLC 拒绝形态);
  4. 基恩士 SR「裸 OK=无读出」判据 + 对比模式输出形态(P2-5);
  5. MX GetErrorMessage 零参返回形态(P3-16,并入既有 MX 真机批)。

## 六、方法论备忘

- 全零对拍盲区之外新增一条:**「字段宽度巧合一致」盲区**——1~255 点两种
  编码逐字节相同,唯边界值 256 暴露「计数高位 vs 恒零域」的语义分歧;帧面
  核对须覆盖协议允许的**极值**,不只抽中值。
- 子代理「疑似」标注有效:两条疑似(C-P1-3、D-P1-1)经主会话回查档案/
  手册原文升级实锤;本轮七路零误报,「先全文后结论」纪律执行到位。

## 七、现场视角提升方向(2026-10-08,工控专家评估)

> 定位:**方向性提案,非代码缺陷**。协议正确性层经十八轮审计已达同类库
> 罕见高度,以下按「现场价值 × 实现代价」排序,全部**不碰协议帧面**。
> 现状核实(2026-10-08):核心包零硬依赖(serial/mx/opcua 均 extras);
> 帧黑匣子已有(core/debug.py `recorded_frames()`,deque maxlen);
> 写保护/wait 原语 git grep 全无。

### 甲、写安全(现场第一事故源,目前完全空白——优先级最高)

1. `read_only=True` 只读模式:写方法一律拒绝(现场铁律:新接产线先只读
   观察一个班次再开写;AI 写错地址风险真实,SKILL.md 面向 AI 复制)。
2. 写白名单:绑 TagTable 后拒写表外地址(表已严格只读,加开关即可)。
3. `write_and_verify` 写后回读:BOOL 非原子 RMW、32 位跨字设定值最需要
   (现场「打点确认」;部分软元件只写不可读,verify 须可选)。

### 乙、排障与取证(现场没有调试器,事后取证全靠黑匣子)

1. 黑匣子一键导出(带方向/时间/标签的文本)+ **拆连/DeviceError 自动
   快照**——现场复现窗口经常只有几秒,不能靠事后开调试复现。
2. 错误码现场话术层:补 MC 文本表(顺路 P3-14,依据页码已在手)+ 高频码
   叠「现场先查什么」(FINS 0x2108→核对 destination_network 与路由表,
   2026-09-24 真机教训;S7 地址越界→DB 长度/优化块;Modbus 异常 02/03→
   先对站号地址表);走 i18n 守卫通道。
3. 按协议排障速查卡(连不上/读到 None/间歇断线三棵决策树,审计积累的
   真机教训编入)。

### 丙、数据语义(对接 MES/SCADA)

1. 点位级质量+时间戳(ipc-edge 2026-09-24 对照登记的「质量枚举/快照
   平台层」思想落地;现在 last_rtt/last_error 是客户端级)。
2. 一致性快照:「全部成功才交付/最大陈旧度」开关(批量部分成功时调用方
   现在要自己翻元组)。
3. `wait_value(address, predicate, timeout)` 等信号原语(「等气缸到位
   M100=1」是现场最高频套路;Monitor 管持续上报,等条件没人管;AI 友好)。

### 丁、运行细节(现场长跑才暴露)

1. Monitor 组错峰 jitter + 周期 P50/P99/超时率(多组同打会打出网络尖峰;
   抖动统计可作现场网络验收依据)。
2. 断线告警合并:首条告警 + 每 N tick 汇报「仍离线,已重试 N 次」
   (50 台客户端 × 检修 2h = 上万条重复 warning)。
3. 断线恢复补扫语义:确认现有行为并写文档(接历史曲线场景必须补扫)。

### 戊、工程与驱动面

1. RS-485 多站总线复用(共享串口 + 站号级互斥调度;一条 485 挂 N 台从站
   轮询是现场常态,串口独占现状下多客户端同 COM 口直接失败)——v1.x 立项级。
2. 吞吐基准表:review-1019 micro-bench 落 README(典型机型 × 点数 × 实测
   周期),随下一次真机批顺带。
3. deprecation 周期:破坏性变更提前一个 minor 发 DeprecationWarning
   (0.x 破坏频繁,S7 构造签名都破过)。

### 未提(与既有拍板冲突或已裁决)

TLS 永不考虑 / 库内模拟器不做 / 连接池有意不做 / 串口原生异步不做;
主备冗余切换与连接池沾边,v1.x 再议。

### 优先级建议

甲(写安全三件套)> 乙1/2(黑匣子导出+错误话术)> 丙3(wait_value)。
理由:均为薄封装层,不碰帧面、审计风险低;把库从「协议正确的库」推到
「敢直接接产线的库」。
