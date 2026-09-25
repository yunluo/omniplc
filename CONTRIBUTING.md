# 贡献指南

感谢你考虑为 omniplc 贡献代码。本指南涵盖本地开发、测试、提交与 PR 流程的约定;
符合这些约定可让评审更快、合并更顺。

## 一、开发环境

- **Python**:本仓库声明 `requires-python = ">=3.7.9"`;本地推荐钉 3.7.9 真机门禁
  (`.python-version` 已设),CI 用 3.12(`uv` 不托管下载 3.7,详见 `CHANGELOG.md` v0.31.4)。
- **包管理 / 构建**:`uv`(`uv sync --extra dev` 装 dev 依赖——dev 是 optional-dependencies
  的 extra,不是 dependency-group;`uv lock` 同步 lockfile)。
- **编辑器**:任意;提交前请跑通门禁四件套(见「三、测试」,静态检查用
  `uvx ruff check`,本仓库不设 `ruff format` 步骤)。

## 二、代码约定

- **零核心依赖**:协议实现除已声明的 `[project.dependencies]` 外不得引入新依赖;
  新增第三方包须先开 issue 讨论。
- **类型标注**:全量 `typing`(PEP 484)+ `# type: ignore` 仅在必要时局部使用;
  `mypy` 与 `ty` 双检查器必过(配置见 `pyproject.toml [tool.mypy]` 与 `[tool.ty.src]`)。
- **注释**:全库中文注释;公开 API docstring 必须有(中文,与 README 一致)。
- **不可入库**:
  - `tools/manual_test.*`(手工测试脚本)
  - `tools/_ref_*/`(第三方参考项目源码,如 `tools/_ref_edge/`)
  - `.idea/`、`__pycache__/`、`.venv/`(本地环境)
- **不要库内模拟器**:不引入 Virtual PLC / Fake PLC 之类代码;协议真机核证由人工完成
  (`README.md`「真机联测待做」表登记)。
- **公共 API 不抛自定义异常**:失败一律 `(False, None)` / `False`,原因进 `client.last_error`;
  内部异常仅用于控制流(`core/errors.py` 见)。

## 三、测试

本仓库门禁四件套,**任一挂下即不通过**:

```bash
uv run python -m pytest tests -q         # 全量(当前 838 例 + 2 skipped)
uvx ruff check src tests                 # 0 告警(规则集显式固定,与 ruff 版本漂移解耦)
uvx mypy src/omniplc                     # 0 问题(70 源文件,目标 python_version=3.9,配置见 pyproject.toml)
uvx ty check src/omniplc                 # 0 问题(Astral 第二类型检查器,与 mypy 互补)
```

新增功能必须补测试;黄金报文样本(protocol 字节级契约)在 `tests/golden/`,改动帧
格式须同步生成脚本(`tests/golden/generate_*.py`)。

## 四、提交与分支

- **阶段提交**:`git add` 只加显式文件,禁止 `git add -A`;每个 commit 单一目的,便于
  cherry-pick / revert。
- **Commit 消息**:中文,格式 `<类型>(<范围>): <一句话总结>`;
  类型用 `feat` `chore` `fix` `refactor` `docs` `test` `design` 等。
- **push 需单独确认**:本仓库惯例,避免误推。

## 五、PR 流程

1. 从 `master` 拉特性分支(`feature/<简述>` 或 `fix/<简述>`)。
2. 提交遵循「阶段提交」约定。
3. 跑门禁四件套(见三)全过;新增/改动至少同步覆盖测试。
4. 填 `.github/PULL_REQUEST_TEMPLATE.md` 的清单。
5. 协议/breaking change 必须:
   - 在 `docs/architecture.md` 状态链与版本履历表加行;
   - README 与 `docs/architecture.md` 文案同步。

## 六、版本发布

发版走 5 落点同步(无人值守容易漏):

1. `pyproject.toml` `version`
2. `src/omniplc/__init__.py` `__version__`
3. `CHANGELOG.md` 加版本条目(README 特性区如有用户可见变化则同步;变更日志已迁出 README)
4. `docs/architecture.md` 状态链头 + 版本履历表
5. `uv.lock`(`uv lock` 自动)

发布由 tag 推送触发(`.github/workflows/build-release.yml`,`v*` 标签):自动构建
sdist + wheel、挂 GitHub Release、可信发布(OIDC)到 PyPI;PR 触发的是门禁工作流
`.github/workflows/ci.yml`。

## 七、行为准则

本仓库参与讨论须遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md);
安全漏洞披露见 [`SECURITY.md`](SECURITY.md)(**不要**在公开 issue 提)。

## 八、真机联测

单元测试走黄金报文 + 模拟传输,**不等于**真机联测;协议面改动提交 PR 时须:

- 「测试」节勾选「真机联测」
- 在 [`docs/real-machine-checklist.md`](docs/real-machine-checklist.md) 填写真机型号 / 读取 / 写入 / 备注
- 模拟器(PLCSIM Advanced / TwinCAT Simulator 等)需在备注里明确标注

现有未真机项目清单:`README.md`「真机联测待做」表。