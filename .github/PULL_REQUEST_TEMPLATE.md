## 改了什么

<!-- 一两句话说明本 PR 的目的与范围 -->

## 关联 issue

<!-- 关联的 issue 编号(若有);无则填 "无" -->

## 改动类型(可多选)

- [ ] 功能(feature)
- [ ] 修复(fix)
- [ ] 重构(refactor)
- [ ] 文档(docs)
- [ ] 测试(test)
- [ ] 协议面新增 / 改动
- [ ] 性能
- [ ] 其它(说明): ______

## 破坏性变更

- [ ] 无
- [ ] 有 —— 影响描述: ______

## 测试

- [ ] `uv run python -m pytest tests -q` 全过(本地验证)
- [ ] `uvx ruff check src tests` 零告警
- [ ] `uvx mypy src/omniplc` 零问题
- [ ] 新增/改动对应测试已补(含黄金向量,若涉及协议帧)
- [ ] 真机联测(协议面改动):______

## 文档同步

- [ ] 无文档改动需要
- [ ] `README.md`「变更历史」节加版本条目
- [ ] `CHANGELOG.md` 加详细条目
- [ ] `docs/architecture.md` 状态链 + 版本履历表加行
- [ ] 设计稿(`docs/superpowers/specs/...`)已写
- [ ] `docs/review.md` P0/P1 backlog 已更新(若适用)

## 自检清单

- [ ] 未引入新核心依赖(否则先开 issue 讨论)
- [ ] 未提交 `tools/manual_test.*` / `tools/_ref_*/` / `.idea/` / `__pycache__` / `.venv/`
- [ ] 未引入库内模拟器(Virtual PLC / Fake PLC)
- [ ] 公共 API 失败语义保持 `(False, None)` / `False`,原因进 `client.last_error`
- [ ] 阶段 commit,每个 commit 单一目的

## 备注

<!-- 其他需要评审者知道的:截图、追踪实验结果、相关 issue 链接 -->