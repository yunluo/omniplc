# 全项目第十七轮审查(review-1018)——十协议细胞级审查

> 日期:2026-10-03 · 触发:user "对几个 PLC 协议,全面,细胞级审查"(用户选全部 10 协议)
> 范围:HEAD = `5b40bf0`(v0.55.0);10 协议 × 逐字段/逐字节对照手册
> (Modbus/MC/FINS+OJ-CIP/AB/S7/汇川/KV/TOYOPUC/松下/OPC-UA)
> 方法:10 并行只读子代理(两批)+ **主会话亲验 4 项关键发现**(手册 PDF 逐页抽取 +
> 参考库 fins/aphyt/pycomm3/pylogix/snap7 源码实测对照)
> 防锚定:主会话未读 review*.md/CHANGELOG;纯只读未改文件。

> **核实与修复批注(2026-10-06,主会话复核后入库)**:P1×3 独立复核
> **全部属实**(P1-1 手册铁证坐实——W342 地址表 CNT 行三处 `800000 to
> 8FFF00`,但 libfins/fins 双参考实测均无前缀逻辑,真分歧待真机终裁;
> P1-2/P1-3 代码现状属实)——已随 8c78b0d 修复。P2 修复:P2-1/2/3
> (d5305e7)、P2-9/P2-12(699eaa7)、P2-14~18(cd9564e);P2-16 为
> 文档修正。**误报两项撤销**:P2-8(Modbus STRING 批量吞错)实测三
> 路径均同步抛 ValueError 符合契约;P2-7(AB 0x25 实例段大端)实测
> pycomm3 LogicalSegment `_encode` + pylogix `pack('<HH')` 双源均为
> 小端 LE、与本库逐字节一致。**待核证不修**:P2-4/5/6(NJ,真机)、
> P2-10(KV EA/EB,手册)、P2-11/13(松下 L/点号,手册)、P2-19/20
> (S7 宽容度,真机)——均已落真机清单与代码披露(628bc0b)。
> P3 随批按 todo.md 登记;台账自本轮起恢复入库(2026-10-05 用户指示,
> review-1017 先例)。

---

## 一、总览

| 协议 | P0 | P1 | P2 | P3 | 核心结论 |
|---|---|---|---|---|---|
| Modbus | 0 | 1 | 2 | 多 | 帧面与规范逐字段吻合;native FC11 忙态计数口径漂移 |
| 三菱 MC | 0 | 0 | 3 | 5 | 核心命令逐字节与 SH-080008 一致;写侧门控不对称 |
| 欧姆龙 FINS/CIP | 0 | **2** | 2 | 多 | **T/C 计数器寻址(可能 P0)** + NJ STRING D0 布局 |
| AB EtherNet/IP | 0 | **1** | 2 | 多 | **connected list_tags 必拆连**(解析器用错) |
| 西门子 S7 | 0 | 0 | 2 | 多 | 帧面与 snap7 3.2.0 逐字节核平,型号参数化全对 |
| 汇川 | 0 | 0 | 0 | 4 | 两本手册逐项相符,无实质问题 |
| 基恩士 KV | 0 | 0 | 1 | 多 | EA/EB 扩展错码未覆盖 |
| TOYOPUC | 0 | 0 | 0 | 7 | 与参考 4.2.0 逐项一致,仅 P3 |
| 松下 | 0 | 0 | 3 | 多 | L read_range 拒绝复现 + UDP 预算逃逸 + 点号门控误拒 |
| OPC-UA | 0 | 0 | 5 | 多 | 与 asyncua 1.1.5 逐点验证一致;5 项 P2 校验/索引缝隙 |

---

## 二、P1(主会话亲验)

### P1-1|FINS T/C 计数器寻址——C 区地址缺 0x8000 前缀,与 Timer 逐字节相同

- 位置:`src/omniplc/core/constants.py:835-836`(`"C": (0x09, 0x89)` 与 T 同码)+
  `src/omniplc/plc/omron/codec.py:842-847`(`_address_bytes` 对 C 区不加地址偏移)
- 手册铁证(W342 §5-2-2 印刷页 165,PDF p.188 逐页抽取):CNT 行**码列空**
  (垂直合并到 Timer 行,共用 09/89),但地址列 **`C0000 to C4095 = 800000 to 8FFF00`**
  (vs Timer `000000 to 0FFF00`)——计数器字地址带 **0x8000 前缀**
- 参考库实测:`fins.FinsPLCMemoryAreas`:`COUNTER_FLAG=0x09 = TIMER_FLAG`、
  `COUNTER_WORD=0x89 = TIMER_WORD`(码一致,手册同)
- 判定:本库 `C10` 发帧 `09 00 0A 00` 与 `T10` **逐字节相同**——按手册字面口径,
  C10 读写会**静默命中 Timer 10 的 PV**。测试把现状钉死
  (`test_fins_codec.py:167-168`"与 C 共享"、`test_fins_clients.py:314` 断言 `sent[12]==0x89`)
- 修法:按手册 C 区 offset 加 0x8000 前缀(或按参考实现裁决后定);**真机核证
  C10 PV 读先落 Timer 还是 Counter**,裁决后重锚测试

### P1-2|AB connected 模式 list_tags 必拆连——应答解析器用错

- 位置:`src/omniplc/plc/ab/ab.py:412-447`(`_transact_with_status` connected 分支
  把 SendUnitData 应答交给 `parse_service_reply_with_status`)+
  `src/omniplc/plc/ab/codec_cip.py:1018-1029`(`_parse_rr_data_cip` 只放行
  `(0x6F, 0x66)`)
- 现象:connected 模式发 SendUnitData(命令 0x70),PLC 回 0x70 应答帧——
  `_check_enip_reply` 拒 0x70 → ProtocolFrameError → **拆连重连**;
  `list_tags()` 在 connected 模式下必失败
- 对比:`_transact` connected 分支(ab.py:309-321)用 `parse_send_unit_data_reply`
  (接受 0x70),`_transact_with_status` 用错了解析器
- review-1007 P1-1(connected 拆连转换)已修复 ✅(ab.py:440-447),但本解析器
  错配是**新发现的独立缺陷**
- 测试缺口:`test_ab_ethip_clients.py` 无任何 connected list_tags 用例
- 修法:connected 分支改走 `parse_send_unit_data_reply`(校验 T->O ID/序列号),
  与 `_transact` 真正同构;补 connected list_tags 黄金用例

### P1-3|Modbus native FC11 忙态错误码口径与同步不一致

- 位置:`src/omniplc/native/modbus.py:944-947` vs `src/omniplc/plc/modbus/modbus.py:1018-1020`
- 现象:sync 按 review-1001 R9-2 改 `DeviceError(..., 0)`(无码口径,不计
  device_error_count);native 仍 `DeviceError(..., int(status))`(0xFFFF 直接作
  错误码 → `last_error_code=0xFFFF`、`device_error_count+1`)
- 修法:native 改 `code=0`,补 native 忙态镜像测试

---

## 三、P2(主会话亲验 3 项,其余子代理报告)

| # | 协议 | 发现 | 位置 |
|---|---|---|---|
| 1 | MC | **同步 `_write` 缺位软元件字单位门控**(读侧 191-203 有、写侧 235-246 无;`write_short("M16",5)` 静默按 16 点/字写)——不对称 | `melsec.py:235-246` |
| 2 | MC | native 单点 `_read`/`_write` 均缺同款门控(仅 read_range/read_batch 有) | `native/melsec.py:146-198` |
| 3 | MC | native random_write 位软元件编号边界恒 `0xFFFFFF-15`,同步已按 `byte_count*8-1` 修(双字 -31) | `native/melsec.py:631` vs `melsec.py:710` |
| 4 | FINS | NJ/NX BOOL 写缺 Forced 字节(W506 §7-6-2 要求 2 数据字节,现发 1 字节) | `cip.py:137-145` |
| 5 | FINS | NJ/NX BOOL 数组 DWORD(32 位)打包 vs W506 WORD(16 位)打包 | `cip.py:99-119,147-166` |
| 6 | FINS | NJ STRING 类型码 D0 vs 实现 A0(STRUCT)+ 值布局双未知——read/write_string 可能全失败 | `cip.py:199-205` |
| 7 | AB | 0x55 16 位实例段(0x25)填充位置与双参考不一致(`20 6B 25 00 01 00` vs 参考 `20 6B 25 00 00 01`),>255 点分页潜伏风险 | `codec_cip.py:528-531` |
| 8 | Modbus | STRING 经 read_many/read_batch/write_batch 静默吞成 (False, None)(ValueError 锁内被吞,违反 docstring 契约) | `modbus.py:245-253/278-284/535-558` |
| 9 | Modbus | FC43 RTU 增量收包封顶 off-by-2(循环内 `len(tail)>254` 在 CRC 追加前检查,帧可达 258 > 256) | `modbus.py:1815-1828` |
| 10 | KV | EA/EB 扩展出错码未覆盖(`_ERROR_RE = ^E[0-9]$`);EA 读路径拆连、.H 路径静默当数据 0xEA=234 | `codec.py:27,121` |
| 11 | 松下 | MEWTOCOL read_range 拒绝 L(LT)字访问(review-1005 复现,仍在)——字语境 L 必为 LT,接点判定只命中 L | `mewtocol.py:229` |
| 12 | 松下 | MEWTOCOL UDP 超数据报预算抛裸 ValueError 逃出 `(bool, values)` 契约(MC 同型已修,MEWTOCOL 未入参期预检) | `mewtocol.py:303-311` |
| 13 | 松下 | MC 点号形式 `R1.15` 字访问/read_range BOOL 被基类门控误拒(与等价记号 R001F 同址不同命) | `melsec.py:184,331,349` + `panasonic/mc.py:53-71` |
| 14 | OPC-UA | GUID 花括号不成对放行(正则 `\{?`/`\}?` 各自可选),运行时 UaError 才暴露 | `address.py:34-39` |
| 15 | OPC-UA | deadband_type 校验深埋事务锁内(先触发惰性重连才发现参数非法),其余参数均入参期校验 | `client.py:117-124,978` |
| 16 | OPC-UA | browse「None = 所有参考」与实现不符(实际 asyncua 缺省 refs=33 分层引用) | `client.py:803-804` |
| 17 | OPC-UA | read_values 应答数量不校验(zip 静默截断,违反"按请求序同数量") | `client.py:259-269` |
| 18 | OPC-UA | stale 句柄 sub_id 别名误删活跃索引(SubscriptionId 会话作用域,重连后服务端可复用 id) | `client.py:1063-1064/1078-1079` |
| 19 | S7 | SZL 应答类型强校验 0x84 严于参考(0x44 真机回显会拆连) | `codec.py:730` |
| 20 | S7 | 读应答长度交叉校验严于参考(老 300/SMART 填充怪癖无宽容) | `codec.py:537-543` |

---

## 四、P3 摘要(子代理报告,主会话未逐条亲验)

- **MC**:1E 码表缺 F/T/C 族(§18.4 印刷页 398 有);1E 写侧点数未分口(位 160/字 64/位字 10);
  `_merge_bit_blocks` docstring 位图标错(M4/M5/M13 → M5/M12/M13);MX docstring 与 read_batch
  行为矛盾;上限未按机型分口(已披露)
- **FINS**:握手错误码未解码(0x20~0x25,libfins 有表);codec 双重取词;native 测试夹具路由字段
  错位;D/EM read_range BOOL 保守拒绝(已披露);EM bank 13~15 码仅限 CJ2 机型(过度放行)
- **AB**:扩展状态 word_count==2 只读首字;页码级出处缺口(ENIP 头/RegisterSession/Forward Open
  无就近引用);docstring「ListIdentity 待核」陈旧(odva/ 实收);`_tag_list_operation` 停滞
  防御只查空页;constants "合法最大约 2KB" 与 AB_EIP_MAX_FRAME=8192 不符
- **S7**:WString length 无上界(10^9 → 400 万次分片);read_wstring 浮点/布尔 length 静默截断;
  resolve_s7_connection rack/slot 裸 int() 强转;型号门禁仅在线生效;STRING/WString 数据布局
  无档案依据行;CC 的 DR 引用口径继承参考(照抄,无风险);死常量 S7_DEFAULT_RACK/SLOT 无引用;
  architecture 类图未含 200/200SMART;测试缺口(截断/门禁全型号/declared_max/BMP)
- **汇川**:4031 错误消息指 MELSEC 手册未指 H5U 16.5;get_cpu_type 未随 _has_ping=False 禁用;
  双层越界行为不一致(已披露);测试缺口(T 上限/字型 read_range native×sync 对拍)
- **KV**:`_has_ping=True` 维持(review-1005 建议未采纳,披露兜底);请求帧无行长/点数上限;
  RDS 未暴露为 read_range/read_many;测试缺口(64 位/INT 写/位读/奇长/X≥10/设备族)
- **TOYOPUC**:M 打包段前导零放行(参考 4.2.0 拒绝,docstring"口径一致"不完整);read_range W
  后缀 docstring 与行为矛盾(M0100W 实际放行);M0100L/H 字节访问客户端层不可达(死路径);
  未实现面清单不全(0x22~0x25/0x32/0x96~0x99/0xA0/0xCA);字节地址超 16 位报错晦涩;
  出错码表与参考完全一致(缺口同源);测试缺口(FLOAT range/M010W 边界/字符串 H 链路)
- **松下**:MEWTOCOL TCP 4 字符错误码截帧拆连(已披露待核);UDP 请求帧无大小拦截;
  docs/firmware-notes.md 引用已删除的 `_ensure_header`;跨协议 BOOL 语义不一致(D100 bit0);
  MEWTOCOL 错误码表缺口(31~39/44~49/54~59/64);TC/CC 位写线圈无码表(已登记)
- **OPC-UA**:connect docstring 与实现不符(ImportError 逃逸);深树 RecursionError 静默截断;
  参数类型未校验(bool 通过);close 可阻塞至 120s;Part 1 页码缺失;read_many([]) 与基类契约
  不一致;read_batch docstring 机制过期;aio 跨 loop 复用失效(重订哑火);订阅队列不可配;
  _build_endpoint path 不校验

---

## 五、门禁

未跑(纯只读审查,无代码改动;修复批提交前再跑)。

---

## 六、待办建议(优先级)

1. **P1×3**:FINS C 区 0x8000 偏移(真机核证后落)+ AB connected list_tags 解析器 +
   native FC11 忙态 code=0
2. **P2 高价值选优**:MC 写侧门控(同步+native)、native random_write 边界对齐、
   OPC-UA 五条校验/索引缝隙、AB 0x25 实例段填充
3. **P2 披露批**:NJ BOOL Forced 字节/BOOL 数组 16 位打包/STRING D0(真机核证项)、
   S7 SZL 0x84 与读应答长度宽容度(真机核证项)、Modbus STRING 吞错与 FC43 off-by-2
4. **P3 随批**:各协议文档/测试缺口按 todo.md 登记
