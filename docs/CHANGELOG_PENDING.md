# Linkora v0.4.7 待发布变更

> 生成时间：2026-08-24
> 生成方式：`scripts/gen_docs.py --all --since v0.4.4`

## 2026-08-24 (未发布)

> 自 v0.4.4 以来的变更

### 新功能

- feat(proactive): 主动触达摘要钉钉排版优化
- feat(alerts): 新增错误监控告警系统（src/alerts/）

### 缺陷修复

- fix(defects): 修复 LLM 异常分类、OCR 下载限制、清理 noqa: BLE001
- fix(summary): 摘要第一人称视角加固，杜绝把主人当成第三方的矛盾视角

### 新功能

- feat(alerts): 新增错误监控告警系统（src/alerts/）
- feat(bench): 新增性能基准测试工具（scripts/bench_llm.py）
- feat(docs): 新增文档站点自动生成（scripts/gen_site.py）

### 测试

- test: 补充 LLM 异常分类单元测试（+6 cases）
- test: 新增告警管理器测试（16 cases）
- test: 新增 LLM 告警集成测试（4 cases）
- test: 新增基准测试用例（5 cases）
- test: 新增文档生成器测试（6 cases）

### CI/CD

- chore(ci): 添加 CI/CD 门禁和文档自动生成
