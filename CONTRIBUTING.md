# 贡献指南 · Contributing

感谢你考虑为 **灵桥 · Linkora** 做贡献！本文在 README「贡献指南」基础上补充开发环境、测试与 Pull Request 流程。

## 我该如何开始？

- **报 Bug / 提需求**：请使用仓库的 Issue 模板（Bug report / Feature request）。
- **修小问题 / 补文档**：直接开 PR 即可。
- **大改动（新平台、新子系统）**：建议先开 Discussion 或 Issue 讨论设计，避免返工。

## 开发环境

```bash
# 1. 克隆
git clone <your-fork-or-this-repo> && cd Linkora

# 2. 一律使用项目内 .venv（不要用系统 python）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock

# 3. 提供配置模板（config.yaml 被 gitignore，测试依赖 example）
cp config.yaml.example config.yaml

# 4. 跑测试
pytest

# macOS 上涉及 torch / faiss 的测试需加：
KMP_DUPLICATE_LIB_OK=TRUE pytest
```

> 改完 `src/` 后需重启 bot（`scripts/run_linkora.py`）让改动生效；纯测试 / 文档改动无需重启。

## 提交规范

采用中文 `type(scope)` 前缀（与现有历史保持一致）：

| type | 含义 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构（零行为变更） |
| `perf` | 性能优化 |
| `test` | 测试补充 / 对齐 |
| `docs` | 文档 |
| `chore` | 杂项 / 配置 |

示例：`fix(poller): 收敛 list-all 时间窗避免启动期全扫`

**铁律**：提交信息、代码注释、文档中**不要写入个人真实姓名、手机号、邮箱等隐私信息**——CI 的 `gitleaks` 会扫描历史，且这与项目匿名协作约定冲突。

## 新增 Agent 工具时必须同步 5 处

单一真源 `BUILTIN_TOOL_MANIFEST` → `config.py` 的 `ToolsConfig.available` → `config.yaml.example` 的 `tools.available` → live `config.yaml` → `TOOL_ACTION_MAP`。缺一不可，否则启动级漂移会被回归测试拦截。

详见 [开发指南](docs/DEV_GUIDE.md)。

## Pull Request 流程

1. Fork 并切出特性分支（如 `fix/list-all-window`）；
2. 保证本地 `pytest` 全绿，且 `ruff check` 无新增 `C901` / `PGH004` 违规；
3. PR 描述说明：**改了什么、为什么、如何验证**；
4. 关联对应 Issue（如 `Closes #123`）；
5. 等待 CI（secret-scan / lint / test on py3.12 & 3.13）通过；
6. 维护者 Review 后合并。

### PR 自检清单

- [ ] 提交信息符合 `type(scope)` 规范，无个人隐私信息
- [ ] 新增 / 修改有对应测试，全量测试通过
- [ ] 未引入新的 `C901`（圈复杂度）/ `PGH004`（f-string 安全）违规
- [ ] 文档（README / docs）已同步更新（如涉及配置、工具、接口）
- [ ] 涉及安全 / 密钥的改动仅作用于本地 `config.yaml`，未入库

## 代码风格

- 格式化与 lint 以 `ruff` 为准（版本见 `pyproject.toml` / CI 钉版）；
- 类型注解逐步补全（pyright 当前为 report-only 基线，欢迎收敛）；
- 命名、注释以中文为主，保持与现有代码一致。

## 许可

贡献即表示你同意以 **GPL-3.0** 许可证发布你的改动。详见 [LICENSE](LICENSE)。
