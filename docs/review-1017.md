# review-1017:S7 调整批复审(型号参数化 + 交叉优化批,v0.54.0)

> 2026-10-05。对象:v0.54.0 西门子 S7 调整批——型号参数化(b4d3813/95f70e4/8eaaac5)
> + 交叉优化批(f930ac7/f481f44)+ 发布(2480464/53ec342)。
> 方法:参考实现逐字节裁决 + 程序化对拍 + 测试面核查(只审查,不修复)。
> 前八轮审计(review-1009~1016)未建文档,结论存 CHANGELOG 与协作记忆
> (2026-10-04「台账勿再建」裁决);本文按 2026-10-05 用户指示重建 review 记录,
> 自本轮起恢复落档。
>
> **闭环(2026-10-06 回写)**:两项发现均已随 3b0b258 修复并发布于 v0.55.0——
> P2-1 native `read_wstring` 守卫在位(`native/siemens.py` L683-686,
> `length <= 0` 抛 ValueError + `int(length)` 归一,+1 例守卫测试);
> P3-1 档案 §2 CC 段(L62-67)与 §11(L221-224,含「CPU 小型号字节仍不
> 校验与 itpub 原帖一致」的口径区分)订正在位。下文「待裁决修复」等表述
> 均为审查时快照;测试态现状:S7 两文件 95 passed(93 + 收口批守卫例
> + 后续批次补充),全量 1814 passed(收口时 1765,FOCAS 入库后 2 失败
> 消除)。

## 一、结论

**帧面 P0/P1 零缺陷**。调整批全部改动点逐项核实无误(§二);扩展对拍 21/21
逐字节一致(§三);S7 测试 93/93 绿、全量 1736 过(2 失败为并行 FOCAS 工作稿,
与 S7 无关,§五)。

发现两项,均非帧面问题(已随 3b0b258 修复闭环,见文首闭环注):

| 编号 | 级别 | 发现 | 落点 |
|---|---|---|---|
| P2-1 | P2(轻) | native `read_wstring` 缺同步侧 `length <= 0` 前置守卫与 `int(length)` 归一 | `src/omniplc/native/siemens.py` `read_wstring`(约 L670) |
| P3-1 | P3 | 帧面档案 §11 一句描述滞后于 f481f44 的回显校验;§2 CC 段未提该加固 | `docs/protocol/siemens/s7comm/README.md` §11(约 L215-216)、§2 CC 段(L62-66) |

## 二、调整点逐项核查(全部通过)

| 调整点 | 核查结果 |
|---|---|
| `S7_MODEL_PRESETS` 六款预设 | 1200/1500=PG+slot1(远端 0x0101)、300/400=PG+slot2(0x0102)=python-snap7 3.2.0 主源(client.py L334/L621);SMART=本端 0x1000/远端 0x0300/TPDU 0x0A、200-CP243=两侧 TSAP "MW"(0x4D57)/TPDU 0x09=IoTClient `SiemensConstant.cs` 字节黄金(HSL 7.0.1 双源坐实,档案 §2) |
| `resolve_s7_connection` | `fixed_remote_tsap` 分支下 rack/slot 仅校验(0~7/0~31)+进调试标签、不参与组帧,与测试锁定一致;非枚举 model 构造期拒 |
| `build_cotp_cr` 参数化 | `local_tsap`/`tpdu_size_code` 进参,缺省值不变;标准 CR 与参考 `ISOTCPConnection._build_cotp_cr()` 实例方法逐字节一致 |
| V 区记号(=DB1) | 翻译映射(V10.3→DB1.DBX10.3、V10→DB1.DBB10、VB/VW/VD/VS 同构)与型号门禁(仅 200/200 SMART)sync/native 两侧同构;V 读写请求帧地址规范 DB 号 1+区码 0x84 逐字节验证 |
| WString 型号门禁 | 仅 1200/1500 放行,sync/native 同构;ValueError 经 `_execute` 直抛(参数错误约定),单测锁定 |
| CC 回显校验(f481f44 新增) | TLV 不按序扫描(cursor=7 起、`end=min(len, pdu_len+1)`)对 itpub 真机帧(`11 D0 … C0 01 09 C1 02 01 00 C2 02 01 01`)逐字节正确;命中放行/不符拒/无参数宽容三形态独立复核通过,会话层不符拆连单测在位 |
| 分片与 DR | 读 PDU−18 / 写 PDU−35 与参考 client_base.py L208/L219 一致;COTP DR 的 dst_ref=CC 回显取法与参考 `_send_cotp_disconnect`(L436-444)一致 |
| SZL 0x0424 偏移 | snap7 C `opReadSZL`(`PDataFirst=ResDataFirst+8`;opData=LENTHDR/N_DR/记录区)复核 `opData[7]`=记录区[3],本库 `entries[3]` 取法成立 |
| 三面同构 | sync/native/aio 构造签名、`model` 属性贯通;aio 转发同步侧(守卫随同步生效);根包 `S7Model` 导出在位;S7Cpu 更名零残留(S7CpuStatus* 为 snap7 状态字符串,有意保留) |
| 文档登记 | 真机清单⑨⑩(SMART/CP243 专项)、CHANGELOG v0.54.0 破坏性披露(位置实参 rack/slot 改键字)在位 |

## 三、程序化对拍(21/21 全绿)

环境:`%TEMP%\s7ref2`(python-snap7 3.2.0,3.12 项目外临时环境);脚本
`%TEMP%\s7_cross.py`(上轮 7 项)+ `%TEMP%\s7_cross2.py`(本轮扩展 21 项)。

**对拍盲区补正(方法论)**:上轮 review-1013 对拍全部 start=0——参考
`build_read_request` 收**字节地址**(`encode_address` 内部 ×8),本库
`build_read`/`build_write` 收**已 ×8 位地址**(调用方负责 ×8)、
`build_multi_read` 收字节起点(内部 ×8);start=0 时两种口径巧合一致,
掩盖了 ×8 换算路径。本轮以非零地址重对拍坐实,今后对拍脚本禁止全零地址。

扩展 21 项:非零字节起点读(BYTE/REAL/MK)、BIT 位地址读(byte<<3|bit)、
非零起点写(BYTE/WORD/DWORD/INT/REAL/BIT,含 count=数据长//元素宽、
数据长位数/字节数分支)、multi read 非零 3 项、SZL 非零 index、协商 PDU 1920、
COTP CR/TPKT/DT 三层 vs `ISOTCPConnection` 实例方法、奇数长 multi 应答
+ 填充字节解析、CC 回显三形态。**全部逐字节一致**。

## 四、P2-1 实证(native read_wstring 守卫缺位)

同步守卫 `src/omniplc/plc/siemens/client.py` L775-776(6916c13 旧封装时代
加入);native 首版 d3316ec 未镜像。桩探针
(`%TEMP%\s7_wstring_guard_probe.py`)实证:

- 同步侧:`length=0` / `length=-2` → 构造期即抛 `ValueError`(不触网);
- native 侧:`length=0` → 发一次 `read_area(4)` 事务后**静默返回
  `(True, '')`**(与"未初始化空串"不可区分);`length=-2` → `size=0`
  零字节读 → 落成误导性 `DeviceError`(「S7 WString 响应过短:0」)+
  `(False, None)`,而非参数错误 ValueError。

修复落点:native `read_wstring` 开头补同款守卫两行 + `int(length)` 归一,
与同步逐字同构。**归属:既有镜像缝隙(v0.53 native 批),非本轮调整批引入。**

P3-1:档案 §11 一句「本库 `parse_cotp_cc` 只校验类型 0xD0 与长度,不校验
该字节」写于 f930ac7(回显校验落地前),f481f44 加 C2 回显校验后未同步;
§2 CC 段亦未提该加固(加固依据现仅存 codec.py `parse_cotp_cc` docstring
与提交消息)。

## 五、测试态与范围外事项

- S7 两文件(`test_siemens_s7_clients.py` / `test_native_siemens.py`)
  93/93 绿(调整批后两次复跑一致)。
- 全量:1736 过 + 2 失败——
  `test_aio_mirror_surface.py::test_concrete_clients_mirror_sync_extensions`
  (FanucFocasClient 缺 cnc_id)与
  `test_i18n.py::test_all_raise_templates_in_translations`(focas.py 新模板
  未入 _TRANSLATIONS)。**两者均为并行会话 FOCAS 驱动工作稿所致
  (工作区未跟踪 `cnc/focas.py` + cnc/aio/i18n 改动),与 S7 无关。**
  (2026-10-06 现状:FOCAS 入库后 2 失败消除,全量 1814 passed;S7 两文件
  现 95 passed——93 + P2-1 收口守卫例 + 后续批次补充。)

## 六、待真机核证项(维持原登记,无新增)

①SMART 槽位分歧(本库缺省 0x0300,社区/S7netplus 0x0301,`slot=1` 覆写可切);
②SZL 请求前缀两字节(本库 0x0A/00 vs C/Sharp7/HSL FF/09,被拒首先改此);
③CC 回显在 SMART/CP243 上的实际形态(缺参数宽容,有参不符即拆)。
