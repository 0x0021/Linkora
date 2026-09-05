# Linkora 测试覆盖报告

> 生成时间：2026-09-05
> 生成方式：全量 `pytest` 收集 + 统计，`scripts/gen_docs.py --coverage`

## 测试统计

- 测试文件：**267 个**
- 测试用例：**3952 个**（3948 passed / 2 skipped / 2 xfailed）
- 测试耗时：**172.05s**

## 关键路径覆盖

| 模块 | 测试文件 | 用例数 |
|------|---------|--------|
| 意图分类 | `test_classifier.py` | 10 |
| 语义路由 | `test_intent.py` | 25 |
| 规则引擎 | `test_rule_engine.py` | 68 |
| DWS 适配器 | `test_dws_adapter.py` | 34 |
| 轮询器 | `test_poller.py` | 78 |
| 轮询策略 | `test_poller_strategy.py` | 87 |
| 消息解析 | `test_poller_core_parse.py` | 50 |
| OCR 处理 | `test_poller_core_ocr.py` | 18 |
| SQLite 存储 | `test_sqlite_store.py` | 51 |
| 向量索引 | `test_vector_index.py` | 20 |
| 记忆注入 | `test_memory_inject.py` | 9 |
| Web 认证 | `test_web_auth.py` | 22 |
| Web API | `test_web_api_endpoints.py` | 101 |
| RAG 门控 | `test_rag_gating.py` | 24 |
| RAG 注入 | `test_rag_inject.py` | 17 |
| 天气工具 | `test_weather.py` | 122 |
| LLM 客户端 | `test_llm_client_retry_primitive.py` | 23 |
| LLM 路由 | `test_llm_router.py` | 7 |
| 异常分类 | `test_llm_exception_classification.py` | 14 |
| 工具包装 | `test_tool_wrapper.py` | 44 |
| 技能路由 | `test_skill_router.py` | 32 |

用例数最多的三个文件：`test_sanitize_prompt_leak.py`（132）、`test_weather.py`（122）、`test_web_api_endpoints.py`（101）。

## 新增测试（v0.4.8 以来）

- `test_summary_scope_regression.py` - 6 个用例（摘要取材范围回归：时区基准、元消息过滤、增量取最新）
- `test_dynamic_summary_scheduler.py` - 5 个用例
- `test_orphan_cleanup.py` - 9 个用例
- `test_tool_idempotency.py` - 5 个用例

**新增总计：25 个用例**
