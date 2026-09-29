# 全项目第六轮只读核查(review-0929)

> 日期:2026-09-29 · 触发:*重新核查项目代码实现,只查不改*
> 范围:HEAD `68e951e`(v0.47.0)全部源码 ~24k 行(74 文件,含 v0.46/0.47 新迁 `core/` 三文件与 i18n 层)
> 方法:12 域并行只读子代理逐行审查(4 个限流重发),对每域下发「已知已修/已知 P3」清单防重复计;
> **全部 P0 与关键 P1 由主会话亲自抽检复验**——SH-080008 手册原文逐字节、asyncua 1.1.5 /
> snap7 1.3 依赖库源码、同步↔native↔aio 代码对照;子代理报告其余项按推理链完整性复核采信。
> 分档:**P0** 错动作/数据损坏/功能失效 · **P1** 静默错误/丢数据 · **P2** 资源/能力缺口/口径分裂 · **P3** 可控隐患/文档失真
> 性质:**纯只读核查,未改动任何文件**;本文为核查结论登记,修复另起批次。

---

## 一、P0(4 条,全部亲验)

### 三菱 MC(3 条,同根因:扩展命令请求侧从未对照手册通信例)

- **[P0] 0406 多块批量读块条目「码+编号」反序** — `plc/melsec/codec_qna.py:667-672`
  `_random_block` 发出 `码 + 编号3B小端 + 点数2B小端`(如 `A8 000000 0400`);SH-080008 §8.4
  印刷页 114 通信例列头明确 **Device number → code → Number of device points**,即
  `00H 00H 00H A8H 04H 00H`(D0~D1023)。真机上 PLC 解读为「编号 0x0000A8 + 非法码 00H」,
  整批 0406 请求必被拒绝,`read_batch`/`read_many` 位块路径真机全失败。
  **反证**:同文件 0401/0114 条目(编号在前,codec_qna.py:152-156)与 4C 串口黄金向量
  `640000 90 0200` 均正确——同一手册同一字段序,唯 0406 组帧反序。
- **[P0] 0403/1402 随机读/写软元件条目同样反序** — `codec_qna.py:578-582`
  `_random_device` 发出 `码 + 编号`;SH-080008 §8.3 印刷页 101(0403:`00H 00H 00H A8H`/
  `64H 00H 00H 90H` 等)与印刷页 107(1402:`00H 00H 00H A8H 50H 05H`)均为**编号在前**。
  0403 随机读、1402 随机写真机全失败(响应侧解析是对的,仅请求组帧错)。
- **[P0] 0406 位块解码位向镜像(首软元件应为 b0,实现取 b15)** — `plc/melsec/melsec.py:1214`
  `_plan_read_batch` 位块 plan 写 `15 - offset % 16`。SH-080008 §8.4 印刷页 113-114 位图
  标注 **M15→M0 且 bit 序左 M15 右 M0**(字 0x2030 = M4/M5/M13 ON),即块内第 k 个软元件 =
  第 k//16 字的 bit(k%16)。每个位块读取值按 16 位镜像静默错位。
  **反证**:同手册 0403 随机读位软元件解码 `raw & 1`(melsec.py:422)与 1402 位写 bit0=首点
  均正确——0406 同向。**历史更正**:review.md 记载的「首软元件在 bit15」(v0.42.1 台账)系对
  手册位图从 M15 画到 M0 的**方向误读**,台账与 `codec_qna.py:13-14`、`melsec.py:1185-1188`
  docstring、`test_mc_clients.py:565/584`、`test_mc_codec.py:227` 需一并订正。

**为何五轮门禁全没抓到**:0406/0403/1402 的 golden 测试期望帧系**照错误实现反写**
(`test_mc_codec.py:193-211` 自称引 §8.4 但期望 core `030000000400` 与手册例
`000000a80400` 不符);回归测试变「防修复」。

### OPC-UA(1 条,第五轮修复新引入路径零覆盖)

- **[P0] 死区订阅 100% 失败且拆线:`fut.result()` 作用在 list 上** — `opcua/client.py:813-823`
  `fut = ua_sub.tloop.post(ua_sub.aio_obj._subscribe(...))` 后 `mids = fut.result()`;而
  asyncua 1.1.5 `sync.py` `ThreadLoop.post` **返回的已是协程结果**(内部
  `run_coroutine_threadsafe(...).result(timeout)`),对 list 调 `.result()` 抛
  AttributeError → 被外层 `except Exception` 吞 → `ua_sub.delete()` →
  `_translate_ua_error` 转 `OmniPLCInternalError` → `_execute` 按内部异常**拆掉整条会话**。
  即每次带 `deadband_value` 的 `subscribe_data_change` 都把连接打死。修法:删除 `fut` 与
  `.result()` 直接取返回值;补死区分支用例(现全库订阅用例均不传 deadband_value)。

---

## 二、P1(7 条;标「亲验」者主会话复核)

- **[P1] native `random_write` 丢弃 1402 响应,PLC 拒绝被当成功**(亲验)— `native/melsec.py:511-521`
  `operation` 仅 `await self._transact(request)`,响应不解析;TCP 路径只过
  `parse_response_head`(不查结束码),UDP 路径连头部校验都不过。同步版
  `melsec.py:553` 走 `_parse_write(response, False)`(结束码非 0 → DeviceError)。
  本库 `random_write` 其余单点写(native:591/598)都有校验,唯独 1402 漏。
- **[P1] i18n en 模板自动/手动编号混用 ×2,en 模式该错误路径必抛格式化 ValueError**(亲验)— `core/i18n.py:588、759`
  `'随机写值超出 {} 字节无符号范围:{}={}'` 的 en 值为 `{}` + `{1}={2}` 混用;汇川
  `'C{}(32 位计数器)不支持位号后缀:{!r}…'` 同病。`str.format` 同串混用自动/手动编号必抛
  `ValueError: cannot switch from automatic field numbering to manual field specification`
  → `set_lang("en")` 下真实校验错误被格式化异常吞掉。修法:en 值改 `{0}`/`{1}` 全手动编号。
- **[P1] S7 半开断连判据在 snap7 1.3 线(=3.7.9 门禁钉版)完全失效**(亲验)— `plc/siemens/client.py:95-126`
  1.3 `check_error` → `error_text` → **C 库 `Cli_ErrorText`**(common.py:92-118),文本形如
  `" ISO : An error occurred during recv"`(bytes repr);`_is_snap7_transport_error` 只匹配
  `"0x000A0000"`(hex)与 `"errIsoRecvPacket"`(名称)两个子串——1.3 已知码不走
  "Unknown error (0x…)" 回退分支,两子串恒不出现 → 半开(拔线/断电)被判 DeviceError,
  **永不重连**。3.x 线 `S7ConnectionError` 文本含 err* 名称,判据成立。修法:补 C 库文本
  模式(`"occurred during send"`/`"occurred during recv"`/`"Job Timeout"`)三线并存,
  并用 1.3 真实文本形态(含 `b'…'` repr)补判据单测。
- **[P1] native `connect()` 跨循环重入洗白循环亲和检查** — `native/base.py:146-158、828-832`
  `connect()` 无 `_check_loop_affinity`;已连接时换循环调 `connect()`:`_guard()` 见旧锁
  空闲 → 换锁并改写 `_lock_loop` → `_connect_locked` 见 `_connected` 短路返回 → 原循环
  绑定证据被抹,后续亲和检查永远放行 → 在新循环上对旧循环的传输收发(静默失败/串帧)。
  docstring 承诺「已连接换循环发起事务报错」只对事务入口成立。修法:connect 进 guard 前
  查亲和,或 `_guard` 换锁分支 `if self._connected: raise`;补 connect 版用例。
- **[P1] native `write_tag` 缺 scale/offset isfinite 守卫,inf 逆缩放静默写 0** — `native/base.py:641-655`
  只有 `scale == 0` 拒绝;直传 `Tag(scale=float("inf"))`(绕过 TagTable 构造校验)时
  `(100-0)/inf = 0.0` → `is_integer()` → **静默写 0**;NaN 同理。同步版
  `core/base_client.py:693-699` 已有该守卫(第五轮修),native 漏镜像。
- **[P1] OPC-UA 事件字段提取恒走兜底,回调只收 `{"raw": repr}`** — `opcua/client.py:354-365`
  `event.keys()/event[key]` —— asyncua 1.1.5 `Event`(common/events.py:14-44)是纯属性
  对象,无 `keys()/__getitem__` → 恒 AttributeError → 被吞 → 字段字典永远是 repr。且默认
  SelectClauses 只选 Property/Variable 子节点,`SourceNode`/`Time` 不在其中 →
  `node_id` 恒 `""`、时间戳恒 None。修法:`vars(event)` 排除 `internal_properties`
  (镜像 server 端口径);docstring 注明 SourceNode/Time 需自定义 EventFilter。
  现有回归只断言「回调触发」,不查字段内容,故漏网。
- **[P1] KV 单字读 `check_errors=False` 豁免过宽,真实 E0~E9 被误判坏帧拆连** — `plc/keyence/hostlink.py:288`
  `_read_word_token` 是单字读唯一通道(read_short/ushort/int/uint 与字软元件位读全走它)
  且一律 `check_errors=False`(第五轮为 `.H` 十六进制读歧义修的豁免被放大到全部格式);
  PLC 回 E0~E9 错误行 → 不查错 → `parse_word_token(".U")` 不匹配 → ProtocolFrameError →
  **拆连** + 丢错误码。写路径同码正确转 DeviceError(不断线)。修法:豁免收窄为
  `check_errors=(data_format != ".H")`;补「RD 回 E0 → DeviceError 不断线」用例。

---

## 三、P2(9 条)

- **[P2] native UDP WSAEMSGSIZE 用正码 +10040,与同步 -10040 分裂且污染统计** — `native/transport.py:502`
  同步层 `transport/udp.py:140/165` 契约:诊断码取负(避开协议码空间、不计
  `device_error_count`);native 抛 `code=+10040` → `code >= 0` 判据成立 → 计数 +1、
  `last_error_code=10040` 与 16 位 PLC 结束码空间撞码。测试两侧各钉一个符号、互相矛盾。
  (两个子代理独立发现一致。)`native/base.py:732-733` 注释「native 无负码来源」与事实相反。
- **[P2] Proactor 循环(3.8+ Windows 默认)下 native UDP 静默截断,WSAEMSGSIZE 分支是死代码** — `native/transport.py:486-510`
  子代理 3.7.9/3.12.10 双解释器回环实测:Proactor `sock_recv_into` 对超长报文**成功返回
  缓冲字节数、无任何异常**(IOCP 完成回调丢弃 MSG_TRUNC);Selector 循环与同步 `recv` 才抛
  10040。超长报文当完整帧下发协议层;帧定长且恰 ≤ 缓冲时长度校验也兜不住。模块 docstring
  「Windows 的 WSAEMSGSIZE 照旧映射为 DeviceError」在主部署形态(3.8+ asyncio.run)不成立。
  修法:`received == size` 记 WARNING(截断嫌疑)+ docstring 如实声明;不强切 Selector。
- **[P2] Modbus RTU 广播后静默只取 T3.5(≈4ms@9600),不满足规范 Turnaround delay** — `modbus/modbus.py:1106-1118`
  串口规范 V1.1b3/V1.02 印刷页 10 §2.4.1:广播后 Turnaround delay **must** 足够从站处理完
  请求,典型 100~200ms;T3.5 只满足帧界定(§2.5.1.1)。写 EEPROM/闩锁类广播后 4ms 发下一帧
  → 从站丢帧 → 主站超时重试。另 §2.5.1.1 Remark:>19200bps 时 t3.5 用固定 1.750ms,现算式
  38400/115200 下仅 1.00/0.33ms。修法:广播路径 `max(inter_frame_delay, turnaround, t3.5)`,
  turnaround 100~200ms 可配;>19200 封底 1.75ms。
- **[P2] MX `get_error_message` 失败误拆健康 ActUtlType 连接** — `plc/melsec/mx.py:383-426、945`
  ActSupportMsg 是独立控件,创建失败/文本查询失败抛 `OmniPLCInternalError` → `_execute`
  按传输级失败 `_mark_disconnected()` → 每次失败拆一次线(上层轮询预翻译时反复重连抖动)。
  同类「能力缺失」基类惯例是 `DeviceError(msg, 0)`(不断线、code=0 不计数)。
- **[P2] `AMelsecMcTcp/UdpClient` 构造签名缺 `xy_octal`,FX5U 八进制异步面不可达** — `aio/__init__.py:803-822、873-892`
  同步两类的 `xy_octal` 参数未镜像;异步用户无法开八进制口径 → `X10` 按十六进制解析,
  **静默读错软元件**。镜像守卫只查方法名不查构造签名(盲区)。修法:补参数透传;把
  native 面已有的构造签名对表移植进 aio 守卫。
- **[P2] FINS 结束码表取词漏包 `_()`,en 模式 ~80 条漏翻** — `plc/omron/codec.py:466`
  `FINS_END_CODE_TEXT.get(base, _("详见 Omron FINS 手册"))` 表值未包 `_()`,i18n 表为
  FINS 全表准备的键成死键;同函数 hint 有包 → en 输出中英混拼。其余五个码表取词点均有包。
- **[P2] CIP 扩展状态表取词漏包 `_()`,en 模式 40+ 条漏翻** — `plc/ab/codec_cip.py:1016-1022`
  `text = table.get(extended)` 未过 `_()` 直接入模板;同文件主表(:969)与 EIP 表(:326)
  都有包,显系笔误。修法一行:`text = _(table.get(extended))`。
- **[P2] native `connect()` 进锁后缺 `_ensure_open()` 复查,关闸后仍可复活建连** — `native/base.py:156-158`
  与第五轮已修的 `_execute` 同款窗口(锁内复查 + 重连前复查),connect 路径漏网;
  排队中的 connect 在 close 置 `_closed` 后仍真建连。修法:进锁首行补 `_ensure_open()`。
- **[P2] core 两处用户可见报错未过 `_()`** — `core/base_client.py:697-699`、`core/types.py:60`
  `write_tag` 的 scale/offset isfinite 报错与 `DataType.from_name` 未知类型名报错为裸
  f-string,en 模式输出中文(i18n 模块口径:异常消息属覆盖范围)。

---

## 四、P3(择要归组,~20 条)

**扫描/串行设备**
- SR:`strict` 解码失败裸抛 `UnicodeDecodeError/LookupError`(`scanner/keyence_sr.py:157、257`),
  last_error 未记、违反「公共 API 失败写 last_error」契约;扫码超时后 `_drain_line` 返回值弃用
  (`:173`),残行/迟到应答可污染下一事务(下一枪把上一条码当本次结果,误归属)。
- KV:地址解析十进制软元件混入十六进制字母(`R1F`/`DM1G`)裸英文 ValueError
  (`plc/keyence/address.py:90、101`),与同函数其余 i18n 文案不一致。

**AB / 欧姆龙**
- `parse_multiple_service_payload` 应答条数不符抛 `ValueError` 逃逸 `(False,None)` 契约
  (`plc/ab/codec_cip.py:508-511`,应答侧应 ProtocolFrameError);`decode_string_payload`
  硬 decode 非 UTF-8 冒泡(`:1287`,Identity 解码器同文件用 errors="replace");
  `get_attribute_list` 非 Identity 分支每项挂同一份完整数据域,与 docstring「逐项」不符
  (`ab.py:438-439`)。
- FINS `_node_from_host` 把末段纯数字的主机名(`plc.9`)误当 IPv4 字面量,不解析 DNS
  (`plc/omron/omron.py:58-62`);NJ STRING 超长报错文案「字符」实为字节数
  (`plc/omron/cip.py:212-215`)。

**三菱 MC / MX / 松下 / 丰田**
- 串口 ASCII 数据区裸 `decode("ascii")`,噪声字节(0x80~0xFF)以 1/256 概率过和校验后抛
  内置异常,绕过坏帧契约且残渣留缓冲(`plc/melsec/codec_serial.py:228、233`、
  `codec_serial_a.py:258、267`;同文件 `_hex_int` 已示范正确写法)。
- MX:`_MxComLink.connect` 的 `Open()` 抛 COMError 时 CoInitialize 计数泄漏 + 原始 COMError
  未翻译(`mx.py:474`);零参形态(GetCpuType/GetClockData/GetErrorMessage)不校验返回码,
  形态本身待真机核证但缺口未声明(`mx.py:298-309、337-343、418-419`)。
- 松下 MC 位软元件字块(0406 字访问)编号语义无手册依据且位号≠0 不对齐(`plc/panasonic/mc.py:109-112`),
  建 3E 帧统一 16 对齐校验或显式拒绝,并标「待核」。
- TOYOPUC `M0100L/H` 单字节语法声明于地址表但三条访问路全堵,能力空悬
  (`plc/toyopuc/address.py:15` 对照 `toyopuc.py:126-148、202-220、273`)。
- MEWTOCOL TCP 长读无预算上限(UDP 侧 2048 拦截已修,TCP 侧 word_count 可达 99999,
  占事务锁至超时)(`plc/panasonic/mewtocol.py:194-222`)。

**core / native 机制**
- `read_tag` 缺 scale/offset isfinite(读侧 inf → `(True, inf)` 静默成功,与
  write_tag/TagTable 三处不对称)(`core/base_client.py:663-679`);`write_tag` 对
  value=±inf/NaN 在整数点位裸逃逸 OverflowError/ValueError(`:708-719`)。
- `convert.py` 数值入口 `words_to_bytes/registers_to_canonical/to_signed/get_bit/set_bit`
  用 `int()` 静默截断 float 入参(与同文件「不做静默掩码」理念相悖)(`convert.py:92-135、376-417`)。
- 退避门控拒绝的调用计入 `transactions`(零流量高计数)(`core/base_client.py:759-770`);
  `set_debug(True)` 并发首次调用 check-then-act 竞态可挂双 handler(`core/debug.py:130-143`)。
- native:`_await_with_timeout` 超时收尾宽 except 吞外层取消 → 已取消事务复活重试
  (`native/transport.py:86-91`,已知 P3 的同族升级路径,触发面更宽);`_clear_stale_selector`
  摘「当前 loop」非创建时 loop,契约外场景原 loop 注册残留(`:534-537`);close() 等锁被取消
  则闸关连接未断(`native/base.py:285-287`,可重复 close 补救);`_disconnect_locked` 关传输
  失败不计 disconnect_count 与同步口径漂移(`:270-274`);TCP connect 多地址各自计时与
  native 绝对 deadline 分裂(`transport/tcp.py:55-58`)。

**码表 / 文档**
- CIP 扩展状态 0x0305 中英文案语义互斥(zh「签名匹配」/ en「Signature mismatch」,疑 zh 漏「不」,
  需对 ODVA Vol 1 表核证)(`core/constants.py:1034` ↔ `core/i18n.py:86`)。
- `MC_MAX_DATAGRAM` docstring「数据长上限 8190」与实现 `MC_MAX_RESPONSE_CONTENT = 8192` 矛盾
  (`core/constants.py:261-266`)。
- Modbus FC11/FC12 规范节号引用错位(§6.11/§6.12 应为 §6.9/§6.10,§6.11/6.12 实为 FC15/16)
  (`modbus/modbus.py:816、834`);异常码 0x07(NAK)缺失被标「厂商自定义」
  (`core/constants.py:188-198` + `modbus/codec.py:224`);`_coalesce_and_read` 内联 2000/125
  未用 constants(`modbus/modbus.py:317`);write_batch「值非法不进事务锁」docstring 承诺与
  实现(锁内编码、幻影事务计数)不符(`modbus/modbus.py:392、410-476`)。

---

## 五、抽检验证记录(主会话亲验)

| 项 | 验证手段 | 结论 |
|---|---|---|
| P0-1/2/3(MC 条目序×2、位向) | SH-080008 英文抽文本 `%TEMP%\opencode\sh080008.txt` 行 3912(0403 例)、4135(1402 例)、4377-4390(0406 例:列头 Device number/code/points、位图 M15→M0 + bit 序标注)、4534 | 手册原文逐字节确认;0401/0403/4C 同库正确路径反证 |
| P0-4(OPC-UA fut.result) | 读 `.venv` asyncua 1.1.5 `sync.py` `ThreadLoop.post` 源码:`return futur.result(self.timeout)` | post 已返回结果,`.result()` 必 AttributeError |
| P1-1(native 随机写) | `melsec.py:553` 有 `_parse_write` ↔ `native/melsec.py:511-521` 无(仅 591/598 有) | 分裂确认 |
| P1-2(i18n 编号混用) | `i18n.py:588、759` 原文 | `{}` 与 `{1}/{1!r}` 同串,format 必抛 |
| P1-3(S7 1.3 判据) | 读 snap7 1.3 `common.py:75-118`:check_error → C 库 Cli_ErrorText;error.py 仅自述不用 | 1.3 异常文本不含 hex/err* 名称,判据失效确认(3.x 线不受影响) |

另:两个独立子代理对 native UDP 正码/Proactor 截断结论一致,且其一以 3.7.9/3.12.10
双解释器回环实测(5050B 报文/1024B 缓冲:Proactor 成功返回 1024 无异常,Selector 抛 10040)。

---

## 六、已核查通过项(概览,子代理逐域报告合并)

- **Modbus**:FC01~24/43 全 FC 组帧/解析边界、CRC 黄金样本逐字、MBAP、区域×类型收口、
  批量合笔/RMW 分流、FC43 翻页收敛与 RTU 增量封顶(第五轮修复复核通过)。
- **FINS**:头格式/三重回显/结束码分类(bit6/7 屏蔽)/0101·0102·0104 组帧解析/元素上限/
  0x1101 回退/TCP 握手与错误域。
- **TOYOPUC**:帧包裹/基址表自洽(`_BIT_BASE == _WORD_BASE << 4`)/packed 校验无 `>>4`/
  低字在前与补码写/RC=10 码表(标待核)。
- **MEWTOCOL**:帧结构/BCC 手工验算/数字域宽度/UDP 长读预算(已修复核)/错误码表 28 条。
- **汇川**:X17 八进制换算、R/B 基址 0x3000(对照 H3U 手册实证,SM/SD=0x2400 亦实证)、
  32 位计数器 C205→0xF70A 与手册算例一致、批量上限 docstring 声明。
- **AB/ADS**:ENIP/CPF 头、0x91/0x28/0x29/0x2A padding 逐字节(第五轮修复成立)、
  Forward Open/Close、Get_Attribute_List、0x0A 预算、ADS 通断码集与 TE1000 §8 一致。
- **S7**:3.x 线判据、read_multi_vars 1.x ctypes/3.x dict 双线、STRING/WSTRING 读写、地址解析。
- **NJ/CIP**:STRING 布局、BOOL 数组 DWORD 回退、消息路由路径。
- **aio**:29 个 A* 类转发漏转 0 多转 0、executor 线程模型与 close 次序、属性无锁快照、
  word_order 枚举双向、`_run_sync_attribute_set` 行为正确(注释口径失真 P3)。
- **native 对表**:modbus/melsec/omron 逐方法守卫与同步一致(除已列 P1/P2)。
- **i18n 引擎**:缺键永不 KeyError、set_lang GIL 原子、1027 条 zh/en 占位符逐条相等、
  调用点 key 抽样 200+ 处全命中、3.7.9 兼容全绿。

## 七、结构性结论

1. **「同步层协议编码经黄金向量双向核证」的前五轮结论被证伪一处重灾区**:0406/0403/1402
   请求侧从未对照手册通信例,golden 帧照实现反写使回归测试变「防修复」。修复 P0-1/2/3 时
   必须连同黄金测试、`codec_qna.py` docstring、`review.md`/`review-1922.md` 中「首软元件在
   bit15」记载一并更正,黄金字节直接取 SH-080008 印刷页 101/107/113-114 通信例原样。
2. **i18n(v0.47.0)无 en 模式门禁**:编号混用(P1)、码表取词漏包×2(P2)、裸文案×2(P2)
   全在英文侧,中文门禁天然看不见。建议补 en 端到端抽查门禁(错误路径断言文案语言)。
3. **native 层口径分裂仍是惯性区**:负码、isfinite、循环亲和、connect 关闸,全是「同步修了
   native 没跟」同款;守卫测试应把构造签名对表(aio)与守卫条件对表(native)扩为常规门禁。
4. **第五轮 P0 修复本身质量成立**(方法名/间隔/aio_obj/0x29 padding/关闸复查),但死区订阅
   底层路径(`fut.result()`)是修复时新引入代码,未经任何用例覆盖——「修复引入新 P0」首例,
   提示修复批次应自带分支级用例。

## 八、修复批次建议

1. **第一批(P0×4 + 同根因收尾)**:MC 0406/0403/1402 条目序+位向(golden 重写)、
   OPC-UA `fut.result()`;连带更正台账与 docstring。
2. **第二批(P1×7)**:native 随机写结束码、i18n 编号 ×2、S7 1.3 文本判据、native connect
   亲和+isfinite、OPC-UA Event 字段、KV check_errors 收窄。
3. **第三批(P2×9)**:native UDP 负码+Proactor 披露、广播 turnaround、MX/xy_octal、
   i18n 取词漏包 ×2 + 裸文案 ×2、connect `_ensure_open`。
4. P3 随批顺带或单列。
