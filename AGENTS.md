# AGENTS.md — omniplc 协作者约定(agent 用)

## PDF 文本抽取

- **统一用 PyMuPDF(不要用 pypdf)**:
  ```pwsh
  uv run --python 3.12 --no-project --with pymupdf python <script> <pdf> <out.txt>
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

## 协议依据

协议实现(组帧/解析/字段/码表/常量/错误码)必须有 `docs/protocol` 手册的页码级引用，
规则见 `CONTRIBUTING.md`「二、代码约定」首条;无依据者标「待核」，不得臆造。
