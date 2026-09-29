# 全项目第七轮修复复核(review-0929-2)

> 日期:2026-09-29 · 触发:*再进行一轮审查*(第六轮 review-0929 修复批落地后)
> 范围:HEAD `f310db0`(v0.47.2)——21 个提交(第六轮 P0×4+P1×7+P2×9 修复 + 台账登记)+
> v0.47.1 未审重构(MC 4C 收包成块化,048784e)
> 方法:5 组并行只读子代理按修复提交逐项复核(修对/修全/无新竞态三问),另对测试基建与
> i18n 运行时引擎二巡;关键结论(手册字节、snap7 C 源、签名错位、docstring 矛盾)经主会话
> 亲验。分档沿用:**P0/P1/P2/P3**。
> 性质:纯只读复核,**未改动任何文件**;门禁四件套由发布提交承载(3.7.9 1387 passed /
> ruff / mypy 75 / ty 全零),本轮未重跑。

---

## 一、修复批复核裁决(21 提交全部通过,无修复引入回归)

| 域 | 裁决 | 关键核证 |
|---|---|---|
| MC P0-1/2/3(182acc0/396b1de/862a159/1d529a9) | **正确** | golden 与 SH-080008 印刷页 101/107/113-114 通信例逐字节一致(W100=0x100 十六进制编号、X20→0x20);全库扫描无旧口径残留(`15 -`、码在前构造零命中);native 经 import 同一 `_merge_bit_blocks` 三层同口径;汇川测试数据同步 bit0 |
| MC 4C 收包成块化(048784e,上轮漏审) | **正确** | 补块请求 `R+4 ≤ 帧剩余` 数学闭合;超时/空返回/截断全落 `TransportClosedError` 拆连,与旧语义等价;3C/1C 独立未受影响;DLE 配对跨块由 `_ensure(1)` 兜底 |
| OPC-UA P0-4(5643031)/P1-6(754eea3) | **正确** | `ThreadLoop.post` 返回形态对照 .venv asyncua 源;死区真服务端用例断言强度足够(修复前必失败);字段提取 `vars(event)` 排除 `internal_properties` 与 `get_event_props_as_fields_dict` 键集一致(但见 P2-2 docstring 矛盾) |
| native 六处(e247cef/1d73890/d530261/1df34bf/ee4b5f3/9066370) | **全部通过** | UDP 整包与 TCP 帧同构(误判疑虑证伪);connect 亲和检查-使用之间无 await 原子、`_connected=False` 换循环属合法设计、洗白唯一危险路径已被覆盖;全库 28 处 `await self._transact(` 裸丢弃点仅已修一处;isfinite 守卫与同步逐字一致 |
| S7(P1-3)/KV(P1-7)/MX(P2-4)/Modbus turnaround(P2-3) | **全部通过** | S7 三文本模式与 snap7 C 源 `s7_text.cpp` 逐字一致、误报面干净、3.x 旧判据保留;KV `data_format` 全部调用点为字面量;MX 三处 DeviceError(code=0) 无残留;turnaround 对照 V1.02 原文(200ms=规范区间上限;1.750ms 确为 RTU 专用、`>19200` 严格大于、与校验位无关) |
| i18n(P1-2/P2-6/7/9) | **通过且超范围** | 编号混用实修 4 条(台账记 2);两个新扫描门禁落地(盲区见 P3);en 抽 10 条 format 全部可用;FINS/CIP 表值取词双层 `_()` 幂等 |

## 二、新发现

### P1(1 条)

- **[P1] `tools/manual_common.py:506` S7 构造按 v0.32.0 旧位序传参,联机脚本 S7 驱动自 v0.32.0 起完全不可用**
  现签名 `SiemensS7Client(ip_address, port, rack, slot, dll_path)`(client.py:433-440,v0.32.0
  位置参数语义调换);实际传 `(ip, rack, slot, port or 102, dll)` → **port=0 / rack=1 /
  slot=102**,TCP 连 0 号端口必败;`manual_siemens_s7.py` docstring「127.0.0.1:102(rack0/slot1)」
  与实际行为自相矛盾;JSON 配置键名正确也无济于事(错在位序)。
  **根因**:门禁四件套只覆盖 `src tests`,**tools/ 完全在 ruff/mypy/ty 之外**,构造漂移无守卫。
  修法:改按现行签名(建议全关键字);加 build_client 全驱动构造冒烟守卫,或把 tools 纳入静态门禁。

### P2(2 条)

- **[P2] `opcua/client.py:908-913` subscribe_event docstring 三方矛盾(与 asyncua 行为、与自家新测试)**
  docstring 称默认 SelectClauses「不含 SourceNode/Time,node_id 恒空串、时间戳 None」;
  实际 asyncua 1.1.5 默认聚合 BaseEventType 全部 HasProperty 子节点(**含 SourceNode 2044/
  Time 2046**,standard_address_space_services.py),同批新增测试 `test_opcua_client.py:887-888`
  恰断言 `node_id != ""`、`ts is not None` 且门禁全绿——**错的是 docstring**,误导用户写不必要
  的自定义 `event_filter`。修法:docstring 改「默认即含 SourceNode/Time;自定义 event_filter
  时键集以所选字段为准」。
- **[P2] 码表文案键 ↔ `_TRANSLATIONS` 无逐表守卫,en 漏翻静默回退中文**
  取词 fallback `_TRANSLATIONS.get(message, message)` 查不到无警告;v0.47.2 刚修过 FINS 结束码
  表/CIP 扩展状态表两处漏包,但「新增码表行忘加翻译」门禁四件套全绿、用户无感知。
  修法:test_i18n 加守卫——import 全部 9 张码表,遍历 values 断言 en 模式 `_(value) != value`。

### P3(11 条,归组)

**修复自留失真 ×3**
- 4C 超时消息「已收 {} 字节」由正文字节数改为线缆字节数(含帧头/附加码),数字含义漂移
  (`melsec.py:1100-1103`;新口径其实更如实,建议措辞注明)
- `native/base.py:749` 注释「native 侧当前无负码来源」已被 ee4b5f3 证伪(恰是 P2-1 点名的
  base 侧残留,修复只更正了 transport 侧)
- `mx.py:375-376` `_new_support_msg_com` docstring 仍写「翻译为 OmniPLCInternalError」,
  函数体已抛 `DeviceError(msg, 0)`(f0f261c 只改函数体)

**文档口径 ×2**
- `aio/__init__.py:1601/1604` FINS **TCP** 构造 docstring 误抄 UDP 的「IP 末段推导」语义
  (TCP 实际 `None` = 握手自动获取,5399ea7 引入)
- `modbus.py:1108/1117/1201` 新注释规范引用为节号级,缺铁律要求的「文档编号+印刷页码」
  (V1.02 印刷页 10 §2.4.1 / 印刷页 13 §2.5.1.1 Remark)

**扫描门禁自身盲区 ×2**
- f-string raise 扫描(test_i18n.py:119-140)只抓单行形态——P2-9 动机案例恰是多行
  `raise ValueError(\n f"…中文…"`;变量中转同样漏(建议升 AST:`ast.walk` 找 raise 的
  JoinedStr 并查中文)
- 编号混用扫描(test_i18n.py:83-101)不扫 zh 键(现表 2 个手动编号键无实害);`{{}}` 字面
  花括号会被 `auto_spec` 误判(理论误报面)

**测试基建 ×3**
- `scripted_async.py:176-182` TcpResponder 把功能码剥掉才交 `pdu_handler`,与 docstring
  「请求 PDU → 响应 PDU」矛盾(现有 3 用例 handler 均忽略入参故未假绿;今后按请求合成
  响应的用例必踩坑)
- `scripted.py:18-22` ScriptedTransport 未调 `super().__init__()`(未过 connect 直接读
  `_receive_timeout` 等 getter 裸抛 AttributeError;test_base_client.py:25 的替身调了)
- `CONTRIBUTING.md:52-54` 门禁命令漂移:pytest 缺 `--extra dev`、mypy 缺 `--python 3.12`、
  用例数「1196」陈旧(现 1387)、「76 源文件」与实际 75 不一致

**工具杂项 ×1(合并)**
- `tools/manual_common.py`:run_connection 属性回填白名单缺 `broadcast_turnaround`;MC 路径
  未暴露 `xy_octal`、串口 `message_wait`;FINS TCP `local_node` 缺省传 0 偏离库默认 None
  (自动推导);配置模式 `--port` 覆盖缺 `"serial" not in conn` 防线;`json.load` 坏配置裸
  traceback

## 三、结构性结论

1. **tools/ 是门禁盲区**——本轮唯一 P1 根因(S7 位序错位潜伏了五个版本)。
2. **「修复提交自带 docstring 失真」成惯性**——本轮 mx、订阅两处(修复者改了函数体/新增了
   与 docstring 矛盾的测试,没同步口径);修复批的 docstring 应与函数体一起过复核。
3. **扫描类门禁要防的形态恰是它扫不到的形态**(多行 f-string、zh 键)——正则扫描建议升 AST。
4. 修复批整体质量高:golden 从「照实现反写」改为手册原字节;收尾提交(汇川测试数据、
   aio 镜像)说明镜像守卫在起作用;21 提交零行为级回归。

## 四、待开发者审核/裁决

| # | 事项 | 待决点 |
|---|---|---|
| 1 | P1 tools/manual_common.py S7 位序 | 修法选择:全关键字改写 + tools 纳入门禁,还是仅改传参 + build_client 冒烟守卫? |
| 2 | P2 订阅 docstring 矛盾 | 按测试与 asyncua 实际行为改 docstring(建议即改,一行);是否顺带在 docstring 标注 asyncua 版本前提 |
| 3 | P2 码表翻译逐表守卫 | 是否加 9 表遍历守卫(建议加,防再犯) |
| 4 | P3 批量 | 11 条随下批顺带还是单列?其中 f-string 门禁升 AST 涉及门禁形态变更,需拍板 |
| 5 | review-0929.md 台账 | 第六轮台账未加「已修复」状态标记(修复记录只在 CHANGELOG v0.47.2)——补标记与否 |
| 6 | tools/_probe_kv_slmp.py | 未跟踪探针仍在工作区——删除、移 $TEMP 还是入库? |

## 五、已核查通过项(概览)

- 脚本传输耗尽行为响亮失败(ConnectionError 非静默挂)、双事件循环参数化、close_server 收口、
  chaos 时序裕量、set_lang 每用例复位隔离、i18n 运行时 GIL 原子性与导入期构造、
  pyproject/CI 四件套命令与钉版口径一致、包导出面五守卫齐备、manual_test.json 27 driver
  与库面同步(manual_common 仅 S7 位序一处错)、tools 其余 26 驱动构造位序逐一核对一致。
