# review-1030 CNC 族审查报告(MTConnect / FANUC FOCAS)

> 2026-10-11 · 基线:master `1803055`(review-1028 后;工作区另有维护者未提交批)
> 范围:`cnc/mtconnect.py`(799)+ `cnc/focas.py`(905)= **1704 行**;
> 测试 `test_mtconnect_client.py` 等;依据 `docs/protocol/mtconnect/`(3 份)、
> `docs/protocol/fanuc/`(`fwlib32.h` + `README.md`)。
> **本轮只检查与差距分析,不落地任何修改**。
>
> **性质声明**:只读审查台账,不含代码改动,不代表修复承诺(review-1019~1029 同款)。
> **覆盖边界声明**:本报告为**主会话亲验的正面结论 + 覆盖清单**;
> MTConnect 的**协议面(元素/属性/增量拉取语义)逐条核**与 FOCAS 的**错误码/句柄生命周期**
> 两个切片**尚未回报**(子代理并发已满),其覆盖边界见 §四。

## 〇、执行摘要

| 切片 | 结论 | 最严重项 |
| --- | --- | --- |
| **MTConnect XML 安全面** | **XXE / 实体炸弹防护 7/7 实测全部正确拒绝**(含 UTF-16LE 与 NUL 夹插绕过尝试) | — |
| **FOCAS ctypes 绑定** | **7 个函数的 `argtypes`/`restype` 与 `fwlib32.h` 逐一吻合**;结构体字段逐一对应;条件编译分支选择正确 | — |
| MTConnect 协议面逐条核 | 待切片(§四) | 待定 |
| FOCAS 错误码/句柄生命周期 | 待切片(§四) | 待定 |

**本切片未发现 P0/P1**;两处**高价值防护**均经实测确认有效。

---

## 一、MTConnect:XML 安全面(亲验,7/7 通过)

### 1.1 现状

`mtconnect.py` 的 `_fromstring_rejecting_doctype` **不直接用** `ET.fromstring`,而是:

1. 先做**字节级 DOCTYPE 扫描**(大小写不敏感);
2. **额外扫描"剔除 NUL 字节后"的形式** —— 覆盖 UTF-16/UTF-32 编码(其 ASCII 字符高字节夹 `0x00`,
   裸 ASCII 扫描会漏判);
3. 命中即抛 `ET.ParseError`,由既有 `ParseError` 路径收口为 `ProtocolFrameError`(响应路径语义不变)。

**设计理由(代码注释自陈)**:Python 3.8 起 `XMLParser` 提供 `forbid_dtd`/`forbid_external`
(纵深防御),**3.7.9 缺这两参**,只能靠解析前字节扫描;MTConnect 合法负载**从不使用 DOCTYPE**,
故扫描**零误伤**。

### 1.2 实测(亲验,7 例)

| # | 输入 | 结果 |
| --- | --- | --- |
| 1 | 普通 XML(`<MTConnectStreams><x/></MTConnectStreams>`) | **解析通过** ✔ |
| 2 | 小写 `<!doctype …>` | **拒绝**(`ParseError`)✔ |
| 3 | 大写 `<!DOCTYPE …>` | **拒绝** ✔ |
| 4 | `<!DOCTYPE r SYSTEM 'file:///etc/passwd'>`(XXE) | **拒绝** ✔ |
| 5 | Billion Laughs 内部实体(`<!ENTITY a 'x'><!ENTITY b '&a;&a;'>`) | **拒绝** ✔ |
| 6 | **UTF-16LE 编码**的 DOCTYPE | **拒绝**(靠"剔 NUL 后扫描"这一支)✔ |
| 7 | **NUL 夹插**绕过尝试(`<!DOC\x00TYPE …>`) | **拒绝** ✔ |

**结论**:在 Python 3.7.9 缺 `forbid_dtd` 的前提下,这一层**实现了等效防护**,且
**两个真实的绕过向量(编码变形、NUL 夹插)都已被覆盖** —— 属**高价值正面结论**,
建议在测试面切片到位后确认是否已有对应用例(若缺,应把上表 6/7 两例补入)。

### 1.3 未核实(该切片)

- 解析**前**的扫描与解析**后**的根元素校验(`MTConnectStreams`/`Devices`/`Assets`/`Error` 四类)
  之间的次序与短路;
- HTTP 层:`Accept`/`X-MTConnect-Version` 头、超时、分块响应;
- `Streams`/`ComponentStream`/`Samples` 的元素与属性名、`dataItemId`/`subType`/`units`、
  **`sequence`/`nextSequence` 增量拉取语义**、心跳缺失处理 —— **全部待协议面切片**。

---

## 二、FANUC FOCAS:ctypes 绑定与结构体(亲验)

### 2.1 `argtypes`/`restype` 与 `fwlib32.h` 逐一吻合(7/7)

| 函数 | 代码声明 | `fwlib32.h` 原型(行号) | 判定 |
| --- | --- | --- | --- |
| `cnc_allclibhndl3` | `(c_char_p, c_ushort, c_long, POINTER(c_ushort))` | `short WINAPI (const char*, unsigned short, long, unsigned short*)`(15122) | ✔ |
| `cnc_freelibhndl` | `(c_ushort)` | `(unsigned short)`(12239) | ✔ |
| `cnc_settimeout` | `(c_ushort, c_long)` | `(unsigned short, long)`(15128) | ✔ |
| `cnc_rdcncid` | `(c_ushort, POINTER(c_ulong * 16))` | `(unsigned short, unsigned long*)`(11288) | ✔(**16 为库侧数组长度**) |
| `cnc_sysinfo` | `(c_ushort, POINTER(_ODBSYS))` | `(unsigned short, ODBSYS*)`(12140) | ✔ |
| `cnc_rddynamic2` | `(c_ushort, c_short, c_short, POINTER(_ODBDY2))` | `(unsigned short, short, short, ODBDY2*)`(10574) | ✔ |
| `cnc_statinfo2` | `(c_ushort, POINTER(_ODBST2))` | `(unsigned short, ODBST2*)`(12146) | ✔ |

**`restype` 统一 `c_short`**(FOCAS 返回 `short`)—— **与全部 7 个原型一致** ✔

**另注**:代码**就近标注了头文件行号**(如「free library handle(12239 行)」),
且**行号与头文件实际位置吻合** —— 这是本库"依据就近注释"纪律在**头文件依据**上的正确应用
(对照 review-1028 §四:基恩士 KV 全族 920 行仅 3 行引用)。

### 2.2 结构体字段逐一对应

- **`_ODBSYS`**:`addinfo`/`max_axis`(short)+ `cnc_type[2]`/`mt_type[2]`/`series[4]`/
  `version[4]`/`axes[2]`(char 数组)—— 与头文件 **2850-2858** 的**第一支**逐字段一致,
  注释亦写明「fwlib32.h 2850~2858 行默认分支,18 字节」;
- **条件编译分支选择正确**:头文件是 `#if !defined (FS15BD)` … `#else` … `#endif`(2849/2859/2869),
  即**未定义 `FS15BD` 时才用第一支**;`FS15BD` 是**编译期**宏(对应 FS15B-D 系列),
  库在运行期无法得知,取默认库口径(`addinfo`/`max_axis` 为 short)**是正确取舍** ✔
- **`_ODBST2`**:`hdck`/`tmmode`/`aut`/`run`/`motion`/`mstb`/`emergency`/`alarm`/`edit`/
  `warning`/`o3dchk` 等 —— 与头文件 **2928-2942** 一致;
  注释另说明「`ODBST`(2884~2925)有三种系列布局,**直接用 `ODBST2` 规避系列差异**」——
  **这是有依据的设计取舍**(用无条件编译的结构体避开系列分支)✔
- **`_pack_ = 4`**:头文件用 `short`/`char` 混合,Windows 默认对齐即 4;**与"18 字节"自述自洽**
  (2+2+2+2+4+4+2 = 18)✔

### 2.3 未核实(该切片)

- 句柄生命周期(分配/释放配对、异常路径释放、重复关闭幂等、多实例);
- 返回码 → 文案/分类的依据来源与全表覆盖;
- 定长缓冲的边界(长度参数与缓冲大小一致性)、字符串解码、截断;
- 库不可用(未装 FOCAS / 非 Windows)时的降级路径与异常类型;
- 与同步/异步层的关系(native 无 FOCAS 的覆盖说明) —— **全部待切片**。

---

## 三、逐族依据状态(亲验)

| 族 | 依据归档 | 形态 | 判定 |
| --- | --- | --- | --- |
| **MTConnect** | `docs/protocol/mtconnect/` **3 份**:Part 1 概览 v1.5 / 入门指南 / 架构白皮书 2012 | **只有概览层,无标准正文** | ⚠ **Part 1 是 Overview**,`Streams`/`Samples` 的元素与属性、`sequence` 增量语义**无正典可依** → 该族协议面属"**随实现/随参考**",须在切片里区分"可核/无正典" |
| **FANUC FOCAS** | `docs/protocol/fanuc/` 2 份:`fwlib32.h` + `README.md` | **头文件即正典**(函数签名/结构体/错误码宏的定义源) | ✔ **头文件足以支撑调用面依据**;但**错误码语义**需另找(头文件只给宏名,不给含义) → 切片需核实错误码文案的来源 |

---

## 四、待补切片(覆盖边界)

> 子代理并发已达上限(8/8),以下两个切片**尚未回报**;到位后并入本报告。

1. **MTConnect 协议面切片**:HTTP 头与超时、分块响应、四类根文档校验次序、
   `Streams`/`ComponentStream`/`Samples` 元素与属性逐条核、`sequence`/`nextSequence` 增量拉取、
   心跳、错误面分类;以及本报告 §1.2 的 6/7 两例**是否已有测试用例**。
2. **FOCAS 切片**:句柄生命周期与异常路径释放、错误码表来源与分类、定长缓冲边界、
   库不可用降级、与 native 层的关系、测试面覆盖矩阵。

**覆盖边界声明**:§一/§二 的结论(安全面 7/7、绑定 7/7 与结构体一致)**独立成立**,
不依赖上述切片;§三 的依据判定亦为亲验。

---

## 五、登记与排期

- 本报告**未发现需修复项**(两处均为正面结论);
- §四 切片若发现缺陷,将在并入时追加"确认问题"节;
- **零行为风险可做项**:把 §1.2 的用例 6/7(UTF-16LE 与 NUL 夹插)补进测试
  —— 它们**证明了防护的两个关键绕过向量**,值得成为**回归护栏**;
- **真机/环境**:FOCAS 仅 Windows 且需 `fwlib32.dll`,本轮**未加载任何真实 DLL**
  (绑定核验为**静态对拍 + 结构体布局自洽**);MTConnect 未连真实 Agent。

> **真机核验**:本报告结论为**头文件逐参数对拍 + 库内动态实证**,**未上真机、未加载真实 DLL**。

---

## 六、经核无问题(亲验,该族最扎实的部分)

| 项 | 核法 | 结论 |
| --- | --- | --- |
| MTConnect DOCTYPE 防护 | **7/7 用例**(含 UTF-16LE 与 NUL 插空变体) | ✔ `_fromstring_rejecting_doctype` 实测有效 |
| FOCAS ctypes 绑定 | 与 `fwlib32.h` **逐项**比对 | ✔ **7/7 函数签名吻合** |
| FOCAS 结构体字段 | 与头文件逐字段比对 | ✔ 一致 |
| `#if !defined(FS15BD)` 条件分支 | 读头文件条件编译 | ✔ 处理正确 |

## 七、虚警留痕

| 疑点 | 排查结论 |
| --- | --- |
| 一度疑「MTConnect 的 DOCTYPE 防护可被编码变体绕过」 | **不成立**:UTF-16LE 与 NUL 插空两种变体均已覆盖(7/7 通过),防护**有效** |
| 一度疑「FOCAS 绑定有遗漏函数」 | **不成立**:与 `fwlib32.h` 逐项比对**7/7 吻合**,无遗漏 |
