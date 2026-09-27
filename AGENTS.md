# AGENTS.md — omniplc 协作者约定(agent 用)

> 面向 AI 编码代理的硬纪律摘编。完整开发规范见 `CONTRIBUTING.md`;本文件与其冲突时,
> 以 CONTRIBUTING.md 与用户当次指令为准。

## 提交纪律

- **阶段提交**:每完成一个阶段(实现 / 测试 / 文档)立即 `git commit`,单一目的,
  便于 cherry-pick / revert;`git add` 只加显式文件,**禁止 `git add -A`**。
- **push 需单独确认**:任何 `git push`(含标签——标签推送会触发 GitHub Release +
  PyPI 可信发布,**不可撤回**)必须用户明确发话,绝不自动推。
- 提交前门禁四件套全绿(见下);commit 消息中文,格式 `<类型>(<范围>): <一句话总结>`。

## 范围红线

- **不要库内模拟器**:不引入 Virtual/Fake PLC 之类代码;测试用黄金样本 + 脚本化传输,
  真机核证人工完成(联机脚本 `tools/manual_*.py` 永不入库)。
- **TLS/SSL 永不考虑**:内网部署口径,任何协议的安全栈(TLS/X.509/加密)不做,
  相关提案直接驳回。
- **不可入库**:`tools/manual_test.*`、`tools/_ref_*/`(第三方参考源码)、`.idea/`、
  `__pycache__/`、`.venv/`。

## 协议依据铁律

- 协议实现(组帧/解析/字段含义/码表/常量/错误码)必须有 `docs/protocol/` 收录的官方
  文档依据,代码**就近注释页码级出处**,格式:
  `依据:SH-080008 p.8-12 §8.4(0406 位块,1 点 = 16 位)`
  ——**文档编号/名称 + 第几页 + 具体引用位置(章节/小节/表/字段)**缺一不可;
  页码以手册自印页码为准(PDF 阅读器页码可并列,不同时写明是哪种)。
- **找不到文档依据的协议不得新增实现**,不得臆造帧格式(缺口登记
  `docs/protocol/README.md`「待补」表;存量无文档实现标「待核」,拿到手册后逐处补引用)。
- 规则全文见 `CONTRIBUTING.md`「二、代码约定」首条;新驱动 / 协议面改动 PR 按此
  逐条核对引用可回溯。

## 环境纪律

- **临时跑非项目 Python 版本(如 3.12)必须用项目外临时环境**——在项目目录内
  `uv run --python 3.12` 会把 `.venv` 重建成 3.12、顶掉 3.7.9 门禁环境。安全形态:

  ```pwsh
  uv run --directory "$env:TEMP" --no-project --python 3.12 --with <包> --with-editable "D:\Python_Work\omniplc" python <script>
  ```

  只有重建门禁环境时才允许在项目目录跑 `uv sync --extra dev`。
- **COM 依赖一律 comtypes**,不用 pywin32/win32com(用户明确纠正过)。

## PDF 文本抽取

- **统一用 PyMuPDF(不要用 pypdf)**:

  ```pwsh
  uv run --directory "$env:TEMP" --no-project --python 3.12 --with pymupdf python <script> <pdf> <out.txt>
  ```

  脚本内用 `import pymupdf`(模块名已是 `pymupdf`;`fitz` 已弃用),例如:

  ```python
  import pymupdf, pathlib, sys
  doc = pymupdf.open(sys.argv[1])
  out = []
  for i, page in enumerate(doc):
      out.append("===== PAGE {} =====".format(i + 1))
      out.append(page.get_text())
  pathlib.Path(sys.argv[2]).write_text("\n".join(out), encoding="utf-8")
  ```

- 加密/扫描件:PyMuPDF 对 AES 加密 PDF 直接可用(无需 cryptography);扫描件无文本层时，
  改用 `page.get_text()` 为空判断，并另找有文本层的手册或标记「待核」。

## 门禁四件套(任一挂下即不通过)

```pwsh
uv run --extra dev python -m pytest tests -q   # 3.7.9 全量
uvx ruff check src tests
uvx --python 3.12 mypy src/omniplc
uvx ty check src/omniplc
```

## 代理工具链(本机专属,换机无效)

- **文件变更通知**:每次修改仓库文件后向本地 NATS(`0.0.0.0:4222`)发一条——
  `python C:\Users\yuki\.zcode\notify\nats_notify.py <file> <action> <note>`
  (主题 `omniplc.file.changed`,stdlib socket 实现,`--ping` 探连通)。
  服务未启动时发送失败不无限重试,汇报即可。
- **全中文**:交流、注释、docstring、commit 消息、文档一律中文。
- **参考实现双向裁决**:用户会给本地参考库 / PyPI 钉版(asyncua==1.1.5、pyads 3.5.1、
  python-snap7 按解释器分版本等);对照是双向的——修自己库的错,**也明确指出参考库的错**
  (用户会问"我错了还是参考库错了",要给依据明确的裁决),结论写入
  `docs/architecture.md` §8.1。
