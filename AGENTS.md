# AGENTS.md — omniplc 协作者约定(agent 用)

> 面向 AI 编码代理的硬纪律摘编。**铁律全集见 `docs/rules.md`**(权威清单,
> 含拦截点与历批沉淀条目);完整开发规范见 `CONTRIBUTING.md`。
> 冲突次序:用户当次指令 > CONTRIBUTING.md > docs/rules.md > 本文件。

## 提交纪律

- **阶段提交**:每完成一个阶段(实现 / 测试 / 文档)立即 `git commit`,单一目的,
  便于 cherry-pick / revert;`git add` 只加显式文件,**禁止 `git add -A`**。
- **push 需单独确认**:任何 `git push`(含标签——标签推送会触发 GitHub Release +
  PyPI 可信发布,**不可撤回**)必须用户明确发话,绝不自动推。
- 提交前门禁五件套全绿(见下);commit 消息中文,格式 `<类型>(<范围>): <一句话总结>`。

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

## 本机可用 CLI 工具(2026-10-10 实测;版本漂移须复核再引)

| 工具 | 实测版本 | 用途与注意 |
| --- | --- | --- |
| git | 2.50.1 | 远程走 SSH 443(本机 22 被代理拦);多行中文提交用 `-F <独占名>.txt` |
| pwsh | 7.6.6 | 实际终端;Bash 工具走 cmd——长管道 / 中文查找 / 多行脚本一律 `pwsh -NoLogo -Command`;cmd 无 grep·head·tail,`python -c` 多行会断,中文 findstr 不可靠 |
| rg(ripgrep) | 15.2.0 | 代码检索首选(2026-10-04 用户裁决:rg 结果照常采信) |
| uv / uvx | 0.11.24 | 环境与工具运行;ruff / mypy / ty 经 uvx(门禁五件套);临时 3.12 按上节走项目外环境 |
| python(项目) | 3.7.9 | 门禁环境 `.venv\Scripts\python.exe`,勿在项目目录用其他版本重建 |
| pdfplumber | uv 临时装 | PDF **文本抽取唯一口径**:`uv run --directory "$env:TEMP" --no-project --python 3.12 --with pdfplumber …`(见下节) |
| pdfcpu | 0.16.0 | `D:\bin\pdfcpu.exe`:PDF 图片提取 / 拆并 / 校验;**无文本模式**,文本仍走 pdfplumber |
| xh | 0.26.2 | HTTP 直连核查(api.github.com 可用);GitHub 目录 / 事实类信息**必须 xh 直核**——WebFetch 小模型会编造目录列表 |
| curl | 8.13.0 | 系统自带,备用 HTTP 客户端 |
| LibreOffice | 管理映像 | PPT 渲染链(python-pptx + LibreOffice + pymupdf 出页图);不在 PATH,用 `%LOCALAPPDATA%\LO_admin\program\soffice.exe`(另装 `C:\Program Files\LibreOffice\program\soffice.exe`);pymupdf 只用于出页图,PDF 文本抽取仍按铁律走 pdfplumber |

- `gh` CLI **未安装**:GitHub 操作走 git(SSH 443)+ xh;Release 资产下载走
  签名资产地址(shell 直连 github.com 443 挂)。

## PDF 文本抽取

- **统一用 pdfplumber(不要用 pymupdf/fitz、pypdf)**:

  ```pwsh
  uv run --directory "$env:TEMP" --no-project --python 3.12 --with pdfplumber python <script> <pdf> <out.txt>
  ```

  脚本内用 `import pdfplumber`,例如:

  ```python
  import pdfplumber, pathlib, sys
  out = []
  with pdfplumber.open(sys.argv[1]) as pdf:
      for i, page in enumerate(pdf.pages):
          out.append("===== PAGE {} =====".format(i + 1))
          out.append(page.extract_text() or "")
  pathlib.Path(sys.argv[2]).write_text("\n".join(out), encoding="utf-8")
  ```

- 加密/扫描件:加密 PDF pdfplumber 打不开时提示另行解密;扫描件无文本层时
  `extract_text()` 返回 None,改找有文本层的手册或标「待核」。

## 门禁五件套(任一挂下即不通过)

```pwsh
uv run --extra dev python -m pytest tests -q   # 3.7.9 全量
uvx ruff format --check src tests              # 格式零漂移(改动先 uvx ruff format src tests)
uvx ruff check src tests
uvx --python 3.12 mypy src/omniplc
uvx ty check src/omniplc
```

## 协作纪律

- **全中文**:交流、注释、docstring、commit 消息、文档一律中文。
- **参考实现双向裁决**:用户会给本地参考库 / PyPI 钉版(asyncua==1.1.5 等;
  第三方封装、同类开源封装 的钉版先例已分别随 ADS 移除、S7 自研退役);对照是双向的——修自己库的错,**也明确指出参考库的错**
  (用户会问"我错了还是参考库错了",要给依据明确的裁决),结论写入
  `docs/architecture.md` §8.1。
