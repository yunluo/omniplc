# omniplc 开发铁律(rules)

> 2026-10-10 整理(v0.56.0 后,review-1021 修复批随批沉淀)。
> 层级:**用户当次指令 > `CONTRIBUTING.md`(完整开发规范)> 本文件(铁律清单)>
> `AGENTS.md`(面向 AI 代理的摘编)**。本文件只收**不可违反**的红线条目,
> 每条附拦截点(门禁 / 流程 / 守卫用例);修改铁律须经用户明示。

## 一、协议依据铁律

1. 协议实现(组帧 / 解析 / 字段含义 / 码表 / 常量 / 错误码)必须有 `docs/protocol/`
   收录的官方文档依据,代码**就近注释页码级出处**,格式:
   `依据:SH-080008 p.8-12 §8.4(0406 位块,1 点 = 16 位)`
   ——**文档编号/名称 + 印刷页码 + 具体引用位置(章节/小节/表/字段)**缺一不可;
   PDF 阅读器页码与印刷页不同时必须写明是哪种。
   (拦截:新驱动 / 协议面改动 PR 逐条核对引用可回溯。)
2. **找不到文档依据的协议不得新增实现**,不得臆造帧格式;缺口登记
   `docs/protocol/README.md`「待补」表;存量无文档实现标「待核」,拿到手册后
   逐处补引用。
3. 帧面争议无官方文档时,以**多实现交叉 + 真机抓包**裁决(双向裁决见 §七);
   帧面核对必须覆盖协议极值——字段宽度巧合一致是已知盲区(1~255 巧合一致、
   唯 256 暴露)。
4. 单源(无第二权威源)的帧面实现必须登记真机核证
   (`docs/real-machine-checklist.md`),真机是最终裁决。

## 二、范围红线

1. **不做库内模拟器**:不引入 Virtual/Fake PLC 之类代码;测试用黄金样本 +
   脚本化传输(`ScriptedTransport`),真机核证人工完成
   (联机脚本 `tools/manual_*.py` 永不入库)。
2. 外部服务器集成联测只在 `OMNIPLC_INTEGRATION=1` **显式启用**,默认恒 skip——
   门禁永不依赖模拟器状态。
3. **TLS/SSL 永不考虑**:内网部署口径,任何协议的安全栈(TLS/X.509/加密)不做,
   相关提案直接驳回。
4. **不可入库**:`tools/manual_test.*`、`tools/_ref_*/`(第三方参考源码)、
   `.idea/`、`__pycache__/`、`.venv/`;`docs/**/*.pdf|pptx|ppt` 仅本地留存
   (git 只跟踪对应 README)。
5. **竞品名纪律**:同类库 / 参考实现 / 作者仓库名一律中性表述(依赖名、厂商协议
   与 SDK 名、现场配套工具除外);从台账 / 记忆 / 外部材料抄写进仓库前先过一遍。
6. 图片资产只留压缩版;误入库的大文件要连 git 历史一起清(重建尾部提交 +
   `diff --stat` 必检 + force-with-lease 双远程 + gc)。

## 三、提交与发版纪律

1. **阶段提交**:每完成一个阶段(实现 / 测试 / 文档)立即 `git commit`,单一目的,
   便于 cherry-pick / revert;`git add` 只加显式文件,**禁止 `git add -A`**。
2. **push 需单独确认**:任何 `git push`(含标签——标签推送触发 GitHub Release +
   PyPI 可信发布,**不可撤回**)必须用户明确发话,绝不自动推。
3. commit 消息中文,格式 `<类型>(<范围>): <一句话总结>`;多行消息用
   `-F <独占名>.txt`(并行会话勿共用消息文件)。
4. 版本号 / CHANGELOG / architecture 履历随**发版**统一落;不发版的批次先提交
   代码,登记 `docs/todo.md`。
5. 破坏性变更**提前一个 minor** 发 `DeprecationWarning`(CONTRIBUTING §六),
   CHANGELOG 逐项披露行为变更;修复批的行为变更(签名 / 异常口径 / 门控行为)
   须同批改写受影响用例,真机核证项入清单。
6. 发版链 = 标签推送 → CI 构建 → Release + PyPI,三个环节全绿才算发版完成。

## 四、门禁纪律

1. **五件套全绿才算完成**,任一挂下即不通过:

   ```pwsh
   uv run --extra dev python -m pytest tests -q   # 3.7.9 全量
   uvx ruff format --check src tests              # 格式零漂移(改动先 uvx ruff format src tests)
   uvx ruff check src tests
   uvx --python 3.12 mypy src/omniplc
   uvx ty check src/omniplc
   ```

2. 门禁输出必须**逐字确认 0 failed**——历史上有"全绿"实为恒红用例的失实记录。
3. 新增 / 改动的 `raise` 文案必须同步 `core/i18n.py` 的 `_TRANSLATIONS` 英文表
   (守卫 `test_i18n` 会红;措辞对齐各族既有条目可零新增)。
4. 验证新测试是真回归的手法:`git stash push -- <实现文件>`(或 checkout 父提交)
   → 应 FAILED → 复原 → 复绿。
5. 修复批新增用例**随批落地不后置**;全量 pytest 计数与 CHANGELOG 口径不同源
   (参数化展开差异),勿互抄数字。

## 五、环境纪律

1. **临时跑非项目 Python 版本(如 3.12)必须用项目外临时环境**——项目目录内
   `uv run --python 3.12` 会把 `.venv` 重建成 3.12、顶掉 3.7.9 门禁环境。安全形态:

   ```pwsh
   uv run --directory "$env:TEMP" --no-project --python 3.12 --with <包> --with-editable "D:\Python_Work\omniplc" python <script>
   ```

   只有重建门禁环境时才允许在项目目录跑 `uv sync --extra dev`。
2. **COM 依赖一律 comtypes**,不用 pywin32 / win32com(用户明确纠正过)。
3. **PDF 文本抽取统一 pdfplumber**(不用 pymupdf/fitz、pypdf);加密件另行解密,
   扫描件无文本层(`extract_text()` 返回 None)改找有文本层的手册或标「待核」。
4. 终端口径:终端是 pwsh 7,工具的 Bash 走 cmd——cmd 无 grep/tail、中文 findstr
   不可靠、多行 `python -c` 会断(临时脚本写文件再跑);git 多行中文提交用 `-F`。

## 六、审查与修复批纪律

1. 审查口径「**只差不做**」:审查台账只登记不落地;修复批落地后以 dated
   **实施批注**回写台账(review-1019 惯例),台账存活至发版后删除,修复记录存
   CHANGELOG;经核不成立的疑似项**当场撤销登记**,避免台账注水。
2. **「写路径过闸」类断言必须沿调用链走到真正发包的入口**再下结论——外层形态
   (不经 `write()` / 不进 `_execute`)不代表闸门失效,内部公共 `self.write` /
   `self.read` 照样过闸;报告引用的守卫代码要推演其对实际调用形态的作用。
3. 每条"文档承诺"做正反对照(承诺出处 + 实现出处)后再定性;机制断言先读事务底
   (`_execute` 返回 `(ok, value)`,包裹事务必须解包;嵌套事务依赖 `RLock`)。
4. **根治类改动要穷举公开方法面**:登记表守卫(tests 内 write 方法登记表)防
   新增写入口漏过闸;新增公开面同步镜像守卫(aio 签名 / 导出双射)。

## 七、协作纪律

1. **全中文**:交流、注释、docstring、commit 消息、文档一律中文。
2. **参考实现双向裁决**:用户会给本地参考库 / PyPI 钉版(asyncua==1.1.5 等);
   对照是双向的——修自己库的错,**也明确指出参考库的错**(用户会问
   "我错了还是参考库错了",要给依据明确的裁决),结论写入
   `docs/architecture.md` §8.1。
3. **文档魔数与代码对拍**:客户端数量 / 版本号等数字写进文档须以实测为准
   (`len(omniplc.__all__)`、`pyproject` version),防多轮发版漂移。
4. 工作区"回退"≠都是 IDE 事故,**先问再还原**;用户命令默认直接执行,不反复确认。
