## 变更说明

<!-- 改了什么、为什么、如何验证。关联 Issue：Closes #123 -->

## 类型

- [ ] `feat` 新功能
- [ ] `fix` 缺陷修复
- [ ] `refactor` 重构（零行为变更）
- [ ] `perf` 性能优化
- [ ] `test` 测试
- [ ] `docs` 文档
- [ ] `chore` 配置 / 杂项

## 自检清单

- [ ] 提交信息符合 `type(scope)` 规范
- [ ] 提交 / 代码 / 文档中**不含任何个人真实姓名、手机号、邮箱等隐私信息**
- [ ] 本地 `pytest` 全绿（macOS 涉及 torch/faiss 加 `KMP_DUPLICATE_LIB_OK=TRUE`）
- [ ] 未引入新的 `C901` / `PGH004` 违规（`ruff check --select C901,PGH004`）
- [ ] 新增 agent 工具已同步 5 处接线（manifest / config / example / live / TOOL_ACTION_MAP）
- [ ] 涉及配置 / 工具 / 接口的改动已同步 README 或 `docs/`
- [ ] 安全 / 密钥改动仅作用于本地 `config.yaml`，未入库

## 验证方式

<!-- 手动验证步骤或测试命令 -->
