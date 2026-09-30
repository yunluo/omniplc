# 全项目第八轮审查(review-0929-3)

> 日期:2026-09-29 · 触发:*忘记之前问题,重新全量审核,检查*(不依赖历史轮次结论的全新全量审查)
> 范围:HEAD `e3a8e2e` 全库(约 70 文件 / 3 万行 src)+ tools + 测试体系 + 文档一致性
> 方法:13 组并行只读子代理(MC/SLMP、Modbus、Omron、AB、Keyence、Panasonic+TOYOPUC、
> Inovance+Beckhoff、S7+OPC-UA、core、transport+native、aio+native MC、MTConnect+MX、横切面),
> **全程防锚定:不读 review*/CHANGELOG 修复记录**,直接对照 `docs/protocol/` 官方文档、
> `.venv` 依赖库源码(snap7 1.3/3.2.0、asyncua 1.1.5、pyads 3.5.1、comtypes 1.4.6)、
> 外部参考实现(OpENer、pylogix、libplctag、Wireshark、aphyt/omron-fins、omron-fins-rust、
> pykeyence)三方核证;关键结论(字序、进制、HRESULT 语义、文档原文)经手册原文逐字节对证。
> 性质:纯只读,**未改动任何文件**;分档沿用 P0/P1/P2/P3。
>
> **修复状态(2026-09-30 登记)**:主会话逐条核实,裁决 P0×1 + P1×5 + P2×20 全部确认
> 零误报,P3 抽核 11 项全命中;唯一勘误 = **P2-10 标题误标「Keyence Mc」,证据全部指向
> 汇川 H5U**(正文按汇川处置)。修复分五批落地(每批独立提交 + 双版本门禁):
> ①`8892e65` 紧急(P0-1 FINS 字序 + P1-1 3C 进制 + P1-5 native round);
> ②`b1a596c` 契约(P2-4~7/9/15/16);③`bf548e3` 依据(P1-3/P1-4 降级待核 +
> P2-17/18 + P2-1 + P3 引用订正 + 字序矩阵 §8.1);④`d034c8b` 披露(P2-8/10~14/19/20
> + tools 冒烟守卫 + README 29→27);⑤`f4f6519` P3 高价值项七条。
> **遗留**:P1-2(MX 高层包装吞高位 0 出错码)**完整修复未落**——三路径
> 改 `_raw_com_method` 原始通道的参数布局须真机逐方法核证(块读写通道
> "勿外推"教训),本批仅在三函数 docstring 如实披露盲区;P1 候补 A/B
> (MEWTOCOL 错误码宽度/字符串字节序)待真机终裁;
> P3 未随批项(归组清单中未单列的)与 §八待决点(tools 纳入静态门禁的完整方案)
> 留待后续;松下/TOYOPUC/KV 手册到位后按「待补」流程复核。

---

## 一、总览统计

| 分档 | 数量 | 说明 |
|---|---|---|
| **P0** | **1** | 静默数据错误(真机上正在发生) |
| **P1** | **5** | 含 2 条「待手册/真机终裁」 |
| **P2** | ~20 | 功能缺口 / 契约失真 / 伪锚定 |
| **P3** | ~60 | 引用页码、注释漂移、边界披露 |

---

## 二、P0(1 条)

### P0-1 FINS 32/64 位多字值字序颠倒(高/低字互换),同步+native 全部读写受影响

- **位置**:`src/omniplc/plc/omron/omron.py:632-639`(`_words_to_value`/`_value_to_words` 用
  `ByteOrder.BIG` 且无 `reverse_words`);波及 `omron.py:176-193`(读)、`195-232`(写)、
  `341`(read_batch 32/64 位条目);`native/omron.py:51-57,161-219,337` 复用同一助手(修一处两侧生效)。
  模块 docstring `omron.py:11`、`codec.py:17` 同样断言「FINS 多字数据为大端字序」。
- **机理**:W342 §5-2-1(p.163-164)只定义「元素=字、字内 2 字节、按地址序返回」;欧姆龙
  CS/CJ/CP 系 32/64 位数据**低字存低地址** → 线上 4 字节 = `[低字高, 低字低, 高字高, 高字低]`。
  REAL 100.5(0x42C90000)存 DM100=0x0000/DM101=0x42C9,响应 `00 00 42 C9`,现按大端解得 ≈5.9e-39。
- **为何一直没暴露**:位/字(单字)读写不受影响,真机核证只测了位与字;黄金样本按同一错误
  假设生成,双向自洽拦不住。
- **证据三源互证**:
  1. aphyt/omron-fins(PyPI fins):`reverse_word_order` 注释原文 *"FINS reads come in from low
     to high bytes…the word order needs to be reversed"*,DINT/REAL/LINT 读写全部先反转字序;
  2. omron-fins-rust:`swap_words_32` 后 `from_be_bytes`,64 位同款;
  3. 库内旁证:同库松下 MEWTOCOL 对**完全相同**的线上约定(低字在前+字内大端)用了
     `reverse_words=True`(`mewtocol.py:283-300`),唯独 FINS 漏加。
- **双向裁决**:本地参考库 `tools/_ref_edge/.../OmronFins/FinsDataCodec.cs` 默认
  `HighWordFirst` **同错**——但那是与 Rockwell 共用的通用默认且自带 `LowWordFirst` 纠偏开关;
  **参考库默认不当,本库亦错**。
- **修法**:`_words_to_value`/`_value_to_words` 加 `reverse_words=True`;订正 `omron.py:11`、
  `codec.py:17` 为「字内大端、低字在前」;订正含 32/64 位的 FINS 黄金样本;
  `docs/real-machine-checklist.md` FINS 行补「REAL/DINT/LINT 读回」核证项。

---

## 三、P1(5 条 + 2 条待终裁)

### P1-1 MC 3C 帧点数域十进制组帧,手册规定 4 位 ASCII 十六进制——点数 ≥10 的 3C 读写全错

- **位置**:`src/omniplc/plc/melsec/codec_serial.py:538` `core = … + f"{points:04d}"`。
- **证据**:SH-080008 §8.1(印刷页 70):ASCII 通信点数转 **4 位十六进制**,算例 20 点→
  `"0014"`;SLMP 手册(印刷页 39-40)同款(20 点逐字节 `30H 30H 31H 34H`);同文件 1C 的
  2 位域 `codec_serial_a.py:164` 用了 `{:02X}` 是对的,唯 3C 用 `04d`。
- **后果**:20 点发出 `"0020"`,PLC 读 0x20=32 点 → 读响应 88 字符 vs 预期 80 →
  `ProtocolFrameError` 且串口缓冲残留 24 字节;写侧 PLC 按 32 点取数 → NAK。
  **1~9 点十进制=十六进制碰巧一致,掩盖了 bug**;3E/4E/4C/1C 不受影响。
- **修法**:`f"{points:04X}"`;补 10/16/20 点 3C 黄金样本(现向量只有 2 点,探不出进制差)。

### P1-2 MX 高层包装(GetDevice/GetClockData/GetCpuType)吞正数出错码,静默返回垃圾数据

- **位置**:`src/omniplc/plc/melsec/mx.py:155-168`(`_com_get_device`)、`:312-347`
  (`_com_get_clock_data`)、`:271-309`(`_com_get_cpu_type` 零参路径);辅证 `:106-108`(`_first` 丢弃码)。
- **证据链**(三段闭环):
  1. MX Component 手册第 7 章**确有高位 0 出错码**:`0x010A42A0`(访问口令不符)、
     `0x010A4030/42`(缓冲存储器写入出错)等;
  2. comtypes 1.4.6 `_memberspec.py:368-371` + ctypes 文档:仅 FAILED(高位 1)才抛 COMError,
     高位 0 出错码**不抛**;
  3. ctypes paramflags 语义:restype(HRESULT)不进入返回值 → 高层包装拿到的只能是出参缓冲
     残留/未定义值,无异常、无 `last_error`、不断线。
- **后果**:设了访问口令等场景,`read_short`/`get_clock` **静默把错误数据送进数据链**——比断线严重。
  SetDevice(纯入参,`_check_rc` 可见码)与块读写原始方法(`_check_com_call` 双层校验)**恰好不受影响**——
  正是这两条路径掩盖了缺口;真机注释(mx.py:20-23)只覆盖了高位 1 形态。
- **修法**:三路径改走既有 `_raw_com_method` 原始 vtable 通道(自备出参缓冲 + `lplRetCode`
  显式校验);高层出参路径降级保留并在降级时拒绝静默。

### P1-3 基恩士 mc.py 引用 SH-080956「KEYENCE 设备格式约定」在手册中不存在(伪锚定)

- **位置**:`src/omniplc/plc/keyence/mc.py:20-24`。
- **证据**:对 `Mitsubishi_SLMP_Reference_Manual.pdf`(SH-080956,244 页)全文检索
  `KEYENCE/キーエンス/基恩士/KV-` **零命中**——SLMP 规范不含基恩士兼容内容;「R 编号=记号数字
  原样发帧」的核心组帧裁决实际处于**无文档依据**状态。
- **旁证**:`docs/real-machine-checklist.md:86`(2026-09-28 真机)记录 KV 侧 SLMP 兼容
  **接受标准三菱记号与软元件码**(D/M/Y 通),而 `KeyenceMc*` 因五设备表不含 Y 直接拒发——
  码表/记号口径问题比注释假设更复杂,更须以手册为准。
- **裁决**:库错(引用失实),非文档错。**修法**:删 SH-080956 引用,降「待核」并挂
  `docs/protocol/README.md` 待补表 KV MC 协议手册缺口行;真机判据已定,拿到手册后按铁律补页码。

### P1-4 基恩士 HostLink 帧型/端口无文档依据,公开参照与本库互斥,真机可能整体不可用

- **位置**:`codec.py:3`(「与官方手册一致」断言无编号页码)、`hostlink.py` 模块 docstring
  无依据注记、`constants.py:791`(KV_DEFAULT_PORT=8000)无出处。
- **证据**:KV-7500/8000 以太网上位链接公开参照实现(pykeyence 等)为 **TCP 8001 + `##` 帧头 +
  BCC**;本库为 **8000 + 裸 `RD …\r`**(无 `##`、无站号、无 BCC/FCS)。两种口径互斥;
  `docs/protocol/README.md:104` 已登记手册缺失,但代码断言与登记状态矛盾。
- **修法**:撤下「与官方手册一致」断言改「待核」;`real-machine-checklist.md` KV HostLink 行
  增设「带/不带 FCS、8000/8001、`##` 帧头」抓包核证条。

### P1-5 native `write_tag` 整数点位逆缩放缺 `int(round())`,与同步层行为分裂

- **位置**:`src/omniplc/native/base.py:667-671` vs `core/base_client.py:708-722`。
- **证据**:同步版整数点位(SHORT~ULONG)非恒等缩放后 `value = int(round(scaled))`(注释明言防
  `(0.3-0)/0.1=2.9999…` 被拒);native 版只有 `if isinstance(value, float) and value.is_integer(): value=int(value)`
  ——2.9999… 原样下传 → `require_int`(validation.py:22-23)拒收 float → `ValueError` 逃逸
  `_execute`,语义污染成「参数非法」。**两组独立复核互证**(transport 组与 aio 组各自发现)。
- **后果**:`write_tag(tag, 0.3)`(scale=0.1、整数点位)同步写 3 成功,native 报错——
  现场表现为「原生客户端偶发写失败」;两侧 docstring 均自称一致。
- **修法**:native 补整数点位 `int(round(value))` 分支(可提公共纯函数消双份逻辑);
  顺带补齐 native `read_tag` 的 2^53 精度告警(同步有、native 缺)。

### P1 候补 ×2(松下手册未收录,置信度 80~85%,建议真机一条脚本终裁)

- **A|MEWTOCOL 错误响应码疑 4 字符非 2 字符**(`codec_mewtocol.py:10,31,184`、`mewtocol.py:217-218`):
  错误码表 28 条键均为 2 位「类别」前缀,手册完整错误码应为 4 位(类别 2+细分 2,如 4004/4101/6005);
  多个独立实现(`!` 后读 4 字符)。若 4 位属实:TCP 按当前 `recv(5)` 收 9 字节**残留 2 字节脏缓冲**,
  下一事务逐次报帧头非法、连接实质报废;UDP 截丢细分码。
- **B|MEWTOCOL 字符串字内拼接用大端,疑与松下 STRING 首字符低字节存储相反**
  (`mewtocol.py:129,140` `to_bytes(2,"big")`):`"ABCD"` 存 DT0/DT1 后读回会得 `"BADC"`——
  每两字符颠倒,静默数据错误;库内旁证:同库 TOYOPUC 字符串走低字节在先(同族日系惯例一致)。
- 终裁路径:`tools/manual_panasonic_mewtocol_*.py` 触发一次错误响应+一次字符串往返即可;
  手册到位后按手册修。

---

## 四、P2(~20 条,按主题)

### 协议面

| # | 发现 | 位置/证据 | 修法 |
|---|---|---|---|
| P2-1 | MC 1E `5B` 异常码按 2 字节,手册为 **1 字节** | `constants.py:286`、`melsec.py:730-731`、`codec_a.py:143,11`;SH-080008 §18.2 印刷页 395:二进制用 1 字节,算例 `5BH 10H`(「2 字节」是 ASCII 记法字符数)。TCP 路径 recv(2) 部分已读超时拆连;UDP 整包 `len<4` 把合法 5B 错误当坏帧拒,真实异常码(如 10H=PC 号错)被吞 | `MC_1E_ERROR_EXTRA_SIZE=1`;完整性判据 `<2+1`;docstring 订正 |
| P2-2 | AB ListIdentity 应答长度校验差一,`IndexError` 逃逸公共 API | `codec_cip.py:1100,1113`:`len(body)<18+14` 后直读 `identity[14]`,Item Length 恰 32 时越界;`_execute` 不捕 IndexError,从 `list_identity()` 直抛调用方,违 errors.py:3-5 坏帧契约;同型问题在 `_parse_rr_data_cip`(:857-866)已修过一次,此处漏网 | 下限改 `18+15` 或读前判 `len(identity)<15` 抛 ProtocolFrameError;`parse_module_identity_payload:1140,1150` 同型 `<14`→`<15` |
| P2-3 | Modbus FC11 状态字非 0(0xFFFF=忙)计入 `device_error_count` | `modbus.py:822-829`、`native/modbus.py:814-822`;V1.1b3 §6.9 p.25:FF FF 仅表「前一程序命令仍在处理」,非异常响应;code=0xFFFF 挪用了异常码命名空间,忙轮询会打爆遥测 | 该分支改 `DeviceError(msg, 0)`(不计统计),状态字留消息文本 |

### 契约面(基类契约 vs 驱动实现的系统性漂移)

| # | 发现 | 位置/证据 | 修法 |
|---|---|---|---|
| P2-4 | **S7 `receive_timeout` 完全未落 snap7**(两组独立互证) | `siemens/client.py:204-279` 全文无 timeout 接线;基类 base_client.py:289-307 宣称「立即生效」;1.3 应经 `set_param(RecvTimeout=5, ms)`(C 库默认 5000ms),3.x 写 `_params[Parameter.RecvTimeout]`(默认 3000) | `_S7Session.connect` 后按秒×1000 下发;docstring 声明映射 |
| P2-5 | 会话型驱动超时联动缺口族 | ADS `ads.py:226` 仅 connect 时 `set_timeout` 一次,事后改无效(明明可在 setter 重发);OPC-UA `client.py:175` 会话期修改无效、connect_timeout 未用;MX COM 无接线;MTConnect `:208` 建连用 receive_timeout 顶替 connect_timeout(后者摆设);走线型 TCP/UDP/串口均正常 | ADS setter 重发 `set_timeout`(尽力式);基类两属性 docstring 按「走线型立即生效/会话型见各驱动」分类;各会话驱动 docstring 声明实际作用范围 |
| P2-6 | core 六个类型化写方法 `int(value)` 静默截断浮点 | `base_client.py:593-615`(write_short~write_ulong);与 `write_bool`(:580-591)的显式拒绝哲学矛盾;驱动层 `require_int` 被基类 `int()` 抢先截断看不到 float;`write_short(addr,1.9)`→写 1;`int(0.3*100)` 意图 3 写 2;`numpy.float64` 高发;附带 `int(inf)` 抛未翻译 OverflowError | 改 `require_int(value)` 后直传(与 write_bool 同款);`retries`/`write_retries` setter 顺带收紧 |
| P2-7 | MTConnect `_coerce` 错误口径分裂 | `mtconnect.py:713-729`(非数值文本→ValueError 逃逸公共 API)vs `:730-739`(整数越界→DeviceError(0));同属「Agent 返回文本与类型不符」的设备数据形态问题,`read_short("Sspeed")` 会异常逃逸而非 `(False,None)` | 解析失败统一转 DeviceError(0);ValueError 只留显式入口校验 |

### 能力/披露面

| # | 发现 | 位置/证据 | 修法 |
|---|---|---|---|
| P2-8 | aio `AOpcUa.subscribe_data_change` 镜像**漏死区参数**,`browse` 漏 `reference_type_id` | `aio/__init__.py:1444-1455,1464-1470` vs sync `client.py:646-652,759-766`;aio 门户宣称「同名同型」(:4);sync 实例私有封装无绕行口 | 补透传;镜像守卫若只比方法存在性,同时锁参数集 |
| P2-9 | aio `__aenter__` 连接失败泄漏 executor 线程 | `aio/__init__.py:500-504`:connect 失败抛 ConnectionError,async with 语义不调 `__aexit__`,close(释放线程唯一入口)永不执行;`async with` 循环重试线性积累常驻线程 | 失败分支先 `await self.close()` 再 raise(close 幂等) |
| P2-10 | Keyence Mc 继承的 0406/0403/1402/0101 不在手册支持面,零披露 | `mc.py:4-5` 只声称 0104/0114;H5U 手册 16.4(印刷页 661)支持仅 0401/1401/0403/1402,16.5 p.662 C059=未识别指令;继承 `melsec.py:227-246,248,360,484,558` 全部激活 | 真机核证四命令;不支持则覆写门控或 docstring 明示「未经核证」 |
| P2-11 | 汇川 mc.py 点数上限四处与 H5U 手册 16.4 不符 | `mc.py:32-34`:批量位「iQ-R 1~1168」无出处(实为子指令 0003→7168 位);随机读「×2+双字≤96」两头手册均无权重(汇川/SH-080008 均无 ×2);随机写「×2」实为 **×12**(差 6 倍,现场按库文估权会多塞 6 倍被 PLC 拒);随机写位「1~64」实为 1~94 | 四数字按手册原文改写;「iQ-R」改「子指令 0002/0003 口径」 |
| P2-12 | ADS「读回静默截断到 80 字符」与 pyads 3.5.1 实际矛盾 | `ads.py:406-412` docstring;pyads `STRING_BUFFER=1024`(constants.py:23),STRING(120) 能完整读回;「改底层通道/调大常量」针对不存在的问题 | 改「pyads 读缓冲 1024,>1023 才截;length 为库侧再截断」 |
| P2-13 | Keyence SR 超时清理忽略 `_drain_line` 返回值,半行残留拼行 | `keyence_sr.py:171-176` 不接返回值一律 TransportTimeoutError;`:225-252` 契约明写 False=留半行;TransportTimeoutError 不拆连 → 旧条码文本可拼进下一行当新结果返回 | `if not drain: _mark_disconnected()` 后再抛 |
| P2-14 | 位软元件字单位单点读绕过 `_bit_device_word_access_allowed` 门控 | `melsec.py:158-174` 单点无检查 vs `:281-291` read_batch 拒绝;`KeyenceMcTcpClient.read("R515", SHORT)` 组出字单位读编号非 16 对齐;类注释(:88-91)宣称「默认 False=拒绝」与单点行为矛盾 | 单点 `_read/_write` 按同款门控 |
| P2-15 | native `disconnect()` 缺亲和检查,可跨循环 `transport.close()` | `native/base.py:256-263,286-295,830-849`;亲和检查只在 connect(:161)/_execute(:713);loop B 调 disconnect 时 `_guard` 静默换锁 + `writer.close()` 触达非线程安全 `call_soon`,debug 模式必炸 | disconnect 取锁前补 `_check_loop_affinity()` |
| P2-16 | MX connect 的 Open 抛 COMError 泄漏 CoInitialize 引用计数 | `mx.py:485-493`:`com.Open()` 在 try 外;`_com_initialize`(:469)已 +1,Open COMError 直接逃逸且 `_com=None` 使 close 短路 → 惰性重连每轮累积 | Open 并入 try,COMError 分支先 `_com_uninitialize()` 再抛 OSError |

### 依据铁律面(伪锚定/漏标待核)

| # | 发现 | 位置 | 修法 |
|---|---|---|---|
| P2-17 | MTConnect 模块头小节号错位 6 处 | `mtconnect.py:3-5`「§8.2 /probe p.13-14」(实为 §8.3.1 p.98-100;p.13-14 是术语表)、「§8.3.1 /current」(实为 §8.3.2 p.101-104);`:612` probe_all「§8.2」→§8.3.1;`:77-80` Boolean YES/NO 定义在 Part 3(未收录)应标待核;`:82-84` Condition 三层级同;`:458-461` at 参数出处应为 §8.3.3.3 p.111 | 按实改+待核标注 |
| P2-18 | 松下三模块全文无「待核」标注(与 TOYOPUC/keyence 纪律不一致) | `panasonic/{mc,mewtocol,codec_mewtocol}.py`;mc.py 引《FP0H 以太网通信手册》无页码;codec_mewtocol.py:3 引库外手册并把待核断言写成定论(P1 候补 A 直接相关) | 三模块补待核段(与 TOYOPUC 同格式),交叉引用 README 待补行 |
| P2-19 | manual_common 白名单缺口——真机核证通道静默错址 | `tools/manual_common.py:425-518`:缺 `xy_octal`(FX5U X/Y 会按十六进制**静默错址**)、AB `rpi_us`、SR `encoding/encoding_errors`、MC 串口 `message_wait`、RTU `inter_frame_delay/broadcast_turnaround` | 补 `_p(conn,…)` 透传;最低限度脚本头注明限制 |
| P2-20 | Omron D/EM 位写 0x1101 回退 RMW 非原子未披露 | `omron.py:369-389`、`native/omron.py:365-382`;CP1E/部分 CS1 返回 0x1101 时回退「字读→字写」两笔事务,扫描周期内丢更新;docstring 只称「对调用方透明」 | docstring 明示非原子;可选开关禁用回退 |

---

## 五、P3(~60 条,归组)

**MC/SLMP(7)**:①`MC_DEVICE_CODES` 缺保持定时器 SS=C6H/SC=C7H/SN=C8H(印刷页 68,补码或登记待补);②0101 页码 176→178(三处,同文件 constants.py:250 引 178 正确、自相矛盾);③`0x6302` 注释算术错(实为 0x0263,`codec_qna.py:551-552`、`constants.py:250-252`、`melsec.py:561-562`);④1402「§5.3 页 46」→44;⑤1E 点数域注释「2 字节小端」实为「1 字节点数+固定值 1 字节」(≤255 字节流碰巧一致,应按手册改述);⑥FX5U X/Y 八进制引 SH-080008 不成立(该手册 X/Y=十六进制,应引 iQ-F 手册或待核);⑦3E/4E 响应不校验网络号/PC 号回显(迟到帧窗口,建议对齐串口路径)。

**Modbus(5)**:①TCP 站号上限 247 拒 0xFF/0x00(网关直连设备无解,TCP 覆写放宽、RTU 保持);②RTU 单播不强制 T3.5(已文档化取舍,可选加自动开关);③`_recv_device_id_tail` ADU 上限判据漏算 CRC 差 2 字节;④FC23 异常 02 附加提示把地址越界一律解读为网关跨段;⑤(并入 P2-3)。

**Omron(6)**:①FINS/TCP 握手命令域不校验(`codec.py:369-388,401-418`,错帧报障点后移);②`_is_normal_end_code` 把 bit7(致命 CPU 错误)当正常放行(仅 0x0040 应视为附带标志);③`_write_bits` 缺位单位点数上限;④read_batch 拒 T/C 完成标志文案与手册不符(是未实现非协议限制);⑤`_check_identity` 不验 DA/SA 回显(低风险);⑥`constants.py:729`「约 500B」实为 ≈2022B。

**AB(2)**:①`parse_module_identity_payload` 同型差一(并入 P2-2);②`_(table.get(extended))` None 契约违例(运行期无害,类型卫生)。

**Keyence(5)**:①`constants.py:829` 孤儿 docstring「0~15」与 SR bank 1~16 矛盾(陈旧残留应删);②KV E 码取 `int(text[1])`,E0→code=0 不计 device_error_count(统计失真),`KV_ERROR_TEXT` 缺 E3/E7/E8/E9;③address.py 十进制字软元件含 A~F 抛英文原生 ValueError 绕过 i18n;④KV_MAX_LINE「最多读 8 字」与字符串路径无上限矛盾;⑤SR docstring「LOFF 之后才发」是配置项非协议事实、「OK(无读出)」分支需标注仅默认响应配置。

**Panasonic/TOYOPUC(3)**:①MEWTOCOL 无单事务字数上限防护(TOYOPUC 有,口径不一致);②UDP 截断 docstring 陈旧(与传输层现行为矛盾);③(并入 P2-18 待核标注)。

**Inovance/Beckhoff(5)**:①`set_timeout` 返回值分支死代码(pyads 恒返 None,删 False 分支);②ADS `receive_timeout` 事后修改不生效(并入 P2-5);③address.py FC06 引用超出手册内容(32 位不支持 FC06 无出处,改库内口径);④mc.py「批量 900」误称三菱 Q/L 上限(实为 960,900 是库内保守值);⑤C052/C053 引注与实际触发场景不符(泛化 C051~C054)。

**S7/OPC-UA(4)**:①S7 §3.5 p.22→22-23;②DataChangeFilter Part4 引用未逐处标待核+Percent 死区未前置校验 0~100;③批量读 Uncertain 质量静默当有效值(至少打点或文档声明);④S7 `_read` 短读 struct.error 按编程错误逃逸(防御性校验长度)。

**core(11)**:①`stats` docstring「无锁快照」与实现在 `_state_lock` 内矛盾;②udp.py 陈旧注释「归 PROTOCOL」实为 DEVICE;③基类 docstring 引用不存在的 `aio.ABaseClient.configure`;④`read_tag` 缺 isfinite 对称防线(直传 Tag(scale=inf) 静默产出 nan);⑤`write_tag` ±inf 抛 OverflowError 逃逸 i18n;⑥`register_size` 对 STRING 返回 1 与 byte_size=0 语义相悖(现使用点未踩中);⑦`from_name` 非必要局部导入;⑧constants.py 三处悬空/重复 docstring(SR_BANK_MIN/FINS_MEMORY_AREA_MAX/FINS_END_CODE_OK);⑨TOYOPUC 0x66/0x72 同文案待核、KV ZR 进制两表矛盾待核;⑩`validate_endpoint` int(port) 宽松收窄;⑪read_string 库内缺陷路径统计时序瑕疵。

**transport+native(7)**:①native UDP 恰满缓冲必刷 WARNING(Modbus 满量程读 260B 必触发,降 debug 或协议层失败后补记);②UDP 超长报文处置跨层分裂(同步 DeviceError(-10040) vs native 静默截断,登记 architecture.md §8.1);③`disconnect_count` 口径分裂(同步失败也计、native 不计);④native `_execute` DeviceError 分支不 `mark_synced`(取消判据遗留误拆连);⑤三同步+两 native 传输 connect() 重复调用泄漏旧句柄(公共 API 面,契约未定义);⑥`recv_some` 默认实现串口语义不符(未覆写零调用方);⑦BaseTransport docstring 异常类型清单缺 TransportTimeoutError。

**aio+native MC(5)**:①`_run_sync_attribute_set` 注释动机不成立(慢事务在途时 setter 仍阻塞事件循环整事务时长,docstring「毫秒级」前提不成立);②native MC 类注释漏 4E 帧披露(`native/melsec.py:726,744,775`、`native/__init__.py:56` vs `_SUPPORTED_FRAMES` 含 4E)+`xy_octal` 仅 3E/4E 生效未注明;③native random_write 丢协议页码引用(sync 有 §8.3 印刷页 104-106/§5.3 页 46,native 无)+native `read_string` 非 str 强转漂移;④FINS aio 构造默认值用字面量 0 而非 `FINS_DEFAULT_*` 常量(漂移隐患);⑤`word_order` setter 注解收窄为 str(sync 是 Union[WordOrder,str])。

**MTConnect/MX(8)**:①(引用项并入 P2-17);②`_BOOL_TRUE`/`_CONDITION_LEVELS` 出处在 Part 3(标待核);③mx.py 全文引用只有节号无页码(补:ReadDeviceBlock p.317、ReadDeviceRandom p.325、SetDevice p.333、GetDevice p.335、GetClockData p.348、SetClockData p.353、GetCpuType p.358、GetErrorMessage p.398、第 7 章 p.496 起);④get/set_clock「0=星期日」手册未定义(弱化或补 QCPU 手册出处);⑤数值解析接受下划线分隔与 Unicode 数字(`int("1_0")`=10、`int("١٢")`=12,改正则严格匹配)。

**横切面(9)**:①README「29 客户端」实为 27(三处)+「16 族协议」口径漂移;②AGENTS/CONTRIBUTING「tools/manual_test.\* 不可入库」与现状矛盾(manual_test.json 已入库,gitignore 只忽略 .py——条文收窄或停止跟踪);③docs/protocol 47 份 PDF 全部 gitignore(合理,但应在 README/CONTRIBUTING 注明「引用复核需在持有手册的环境」);④manual_ab_eip_generic `--debug` 只调 logging 未调 `set_debug(True)` 抓不到帧;⑤manual_common JSON 读取裸 traceback(改 utf-8-sig+友好提示);⑥CONTRIBUTING 用例数 1196 过期(现 1387,删数字或指 CI);⑦sdist 默认收录 tools/tests/uv.lock/.github(hatch 补 sdist exclude);⑧KV HostLink 无 FCS——checklist 未列「带/不带 FCS」核证点(补);⑨tests/ 守卫盲区:tools/ 完全在四件套门外(S7 位序两度漂移即此盲区,建议加 manual_common build_client 冒烟守卫)。

---

## 六、双向裁决记录(库对、别处错——有据)

1. **asyncua `read_values` 把 Bad 状态静默压成 None 是参考库缺陷**——本库批量读绕行(`read_attributes`+逐节点 StatusCode 校验)正确。
2. **SendUnitData = 0x0070**(非 0x0072):OpENer/pylogix/libplctag/Wireshark/Rockwell 抓包五源一致,库正确、黄金样本正确。
3. **UC-Send 路由段序 `[path_size][reserved][path]`**(`codec_cip.py:365`):Rockwell Table 5+抓包+libplctag 三源一致,库正确。
4. **pyads 3.5.1 `set_timeout` 恒返 None**——ads.py 死分支判定成立,仅注释失实(即 P3)。
5. **松下 MEWTOCOL 32/64 位低字在前、TOYOPUC L/H 记号无空悬、AB BOOL 数组 32 位打包**——初判疑点均被库方推翻。
6. **参考库同错项**:`_ref_edge/OmronFins/FinsDataCodec.cs` 默认 HighWordFirst(见 P0-1);edge 各类通用默认与日系低字在前惯例不符处,结论已入台账。

## 七、结构性结论

1. **「字序」是系统性风险点**:松下对了、TOYOPUC 对了、FINS 错了——同型数据三种口径,缺一张全局字序矩阵(建议 architecture.md 增「多字值字序对照表」:各协议×各类型)。
2. **COM「出参+HRESULT」通道只有块读写走对**:高层便利包装(GetDevice/GetClockData/GetCpuType)是 MX 域最大薄弱面(P1-2)。
3. **基类契约与驱动实现系统性漂移 ×4**:超时立即生效(S7/ADS/OPC-UA/MX)、类型化写显式拒绝(int() 截断)、(bool,value) 收口(MTConnect ValueError)、镜像完备(aio 死区参数)——文档说 A、驱动做 B。
4. **依据铁律**:引用存在的抽核 15+ 处全对,但「伪锚定」(P1-3)比裸奔更危险;松下域整体漏标待核(P2-18)。
5. **真机核证通道自身有错址风险**(P2-19 xy_octal)——核证结论可能建立在错误地址上,优先补。

## 八、建议修复分批与待决点

| 批次 | 内容 | 性质 |
|---|---|---|
| ① 紧急 | P0-1 FINS 字序 + P1-1 3C 进制 + P1-5 native round(+黄金样本/真机清单随批) | 全部静默数据错误 |
| ② 契约 | P2-4~7(S7 超时/会话型超时/int 截断/MTConnect 收口)+ P2-15/16/9(native 亲和/MX CoInit/aio 泄漏) | 行为修复 |
| ③ 依据 | P1-3/P1-4(伪锚定降级待核)+ P2-17/18 + 引用页码批量订正 | 文档/口径 |
| ④ 披露 | P2-8~14/19/20 + P1 候补 A/B 真机终裁脚本 | 披露+核证 |
| ⑤ 随批 | P3 按 |

**待开发者裁决**:①⑤ 批次顺序;松下两条 P1 候补先真机终裁还是等手册;字序矩阵是否立项;tools 纳入门禁与 build_client 冒烟守卫(P3 横切⑨,两轮连续命中同一盲区)。
