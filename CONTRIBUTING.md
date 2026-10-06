# 贡献指南

感谢你考虑为 omniplc 贡献代码。本指南涵盖本地开发、测试、提交与 PR 流程的约定;
符合这些约定可让评审更快、合并更顺。

## 一、开发环境

- **Python**:本仓库声明 `requires-python = ">=3.7.9"`;本地推荐钉 3.7.9 真机门禁
  (`.python-version` 已设)。CI 跑版本矩阵 **`3.7.9` + `3.12`**(3.7 必保——老设备 vendor
  SDK 只到 3.7;`uv` 不托管下载 3.7,详见 `.github/workflows/ci.yml` 与 `CHANGELOG.md` v0.31.4)。
- **包管理 / 构建**:`uv`(`uv sync --extra dev` 装 dev 依赖——dev 是 optional-dependencies
  的 extra,不是 dependency-group;`uv lock` 同步 lockfile)。
- **编辑器**:任意;提交前请跑通门禁五件套(见「三、测试」,静态检查用
  `uvx ruff check`,格式统一用 `uvx ruff format`(2026-10-04 起,配置取
  ruff 默认与 同类参考实现 对齐,提交前 `uvx ruff format src tests` 再入检))。

## 二、代码约定

- **铁律:协议实现必须有文档出处,代码必须记录页码级引用**:所有协议实现(组帧 / 解析 /
  字段含义 / 码表 / 常量 / 错误码)必须能在官方文档中找到对应依据(收录于
  [`docs/protocol/`](docs/protocol/README.md),PDF 仅本地留存不入库);协议实现代码必须
  **就近注释**记录出处,格式:`依据:SH-080008 p.8-12 §8.4(0406 位块,1 点 = 16 位)`
  ——即 **文档编号/名称 + 第几页 + 具体引用位置(章节 / 小节 / 表 / 字段)** 缺一不可。
  页码以手册自印页码为准(PDF 阅读器页码可并列标注,两者页码不同时务必写明是哪种);
  扫描件无文本层的至少给章节 + 可定位的特征段落。**找不到文档依据的协议不得新增实现**
  (`docs/protocol/README.md`「待补」表登记拿不到的厂商手册;存量无文档实现保持登记并标注
  待核,补齐文档时逐处补引用)。新驱动 / 协议面改动的 PR 按此逐条核对引用可回溯。
  **两条硬约束**:(a)**引用必须指向正确的文档编号**——同一厂商多份手册不得张冠李戴
  (例:三菱 MC 协议 = `SH-080008`,`SH-080956` 是 SLMP 参考手册;以
  [`docs/protocol/README.md`](docs/protocol/README.md) 索引表的「编号/来源」列为准)。
  (b)**码表 / 错误码 / 常量不得按"看似连续"推断或照抄相邻行**——必须逐项对到手册表里
  的**该行原值**(设备码、地址进制、字宽三者都要核);凡"待真机终核"的取值须同时给出手册
  依据,不得以"与手册吻合"草率结案。
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

本仓库门禁五件套,**任一挂下即不通过**:

```bash
uv run python -m pytest tests -q         # 全量(当前 1699 例,随批次增长;无平台跳过;超 120s 由 pytest-timeout 判失败)
uvx ruff format --check src tests        # 格式零漂移(ruff 默认配置,与 同类参考实现 对齐;改动后先 uvx ruff format src tests)
uvx ruff check src tests                 # 0 告警(规则集显式固定,target-version 由 requires-python 推导为 py37)
uvx --python 3.12 mypy src/omniplc       # 0 问题(81 源文件,目标 python_version=3.10;mypy 已不支持 <3.10,3.7 兼容由 ruff + 3.7.9 测试腿兜底)
uvx ty check src/omniplc                 # 0 问题(Astral 第二类型检查器,与 mypy 互补)
```

**本地双版本覆盖(对齐 CI 矩阵 3.7.9 + 3.12)**:项目 `.venv` 钉 3.7.9(3.7 腿直接
`uv run`);3.12 腿**不得**在项目内 `uv run --python 3.12`(会重建 `.venv`),在仓库外
建独立环境:

```bash
uv venv --python 3.12 "%LOCALAPPDATA%\omniplc_py312"
uv pip install --python "%LOCALAPPDATA%\omniplc_py312\Scripts\python.exe" -e ".[dev]"
"%LOCALAPPDATA%\omniplc_py312\Scripts\python.exe" -m pytest tests -q
```

依赖线差异按需跳过:仅存在于单一扩展依赖轨道的用例(如历史 同类开源库 双轨期的 `同类开源库.types`,该双轨已随 S7 自研退役)
ctypes 线)须在入口用 `importlib.util.find_spec` 探测并 `pytest.skip`,保证两条
腿各自全绿(3.7:0 skipped;3.12:仅依赖线跳过)。

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
3. 跑门禁五件套(见三)全过;新增/改动至少同步覆盖测试。
4. 填 `.github/PULL_REQUEST_TEMPLATE.md` 的清单。
5. 协议/breaking change 必须:
   - 在 `docs/architecture.md` 状态链与版本履历表加行;
   - README 与 `docs/architecture.md` 文案同步;
   - **在 PR 描述里列出所依据的 `docs/protocol` 手册编号 + 页码/章节/表**(可用「文档复核」
     小节形式),并确认代码就近注释与该引用一致(见「二、代码约定」铁律);协议字段/码表
     改动要说明「逐项对表」的核对方法,而非只给结论。

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