# Linkora 测试覆盖报告

> 生成时间：2026-08-24
> 生成方式：`scripts/gen_docs.py --coverage`

## 测试统计

- 测试文件：**190+ 个**
- 测试用例：**1370 个**（2 skipped）
- 测试耗时：**64.72s**

## 关键路径覆盖

| 模块 | 测试文件 | 用例数 |
|------|---------|--------|
| 意图分类 | `test_classifier.py` | 10 |
| 语义路由 | `test_intent.py` | 25 |
| 规则引擎 | `test_rule_engine.py` | 62 |
| DWS 适配器 | `test_dws_adapter.py` | 31 |
| 轮询器 | `test_poller.py` | 67 |
| 轮询策略 | `test_poller_strategy.py` | 91 |
| 消息解析 | `test_poller_core_parse.py` | 50 |
| OCR 处理 | `test_poller_core_ocr.py` | 12 |
| SQLite 存储 | `test_sqlite_store.py` | 51 |
| 向量索引 | `test_vector_index.py` | 20 |
| 记忆注入 | `test_memory_inject.py` | 9 |
| Web 认证 | `test_web_auth.py` | 20 |
| Web API | `test_web_api_endpoints.py` | 101 |
| RAG 门控 | `test_rag_gating.py` | 24 |
| RAG 注入 | `test_rag_inject.py` | 17 |
| 天气工具 | `test_weather.py` | 122 |
| LLM 客户端 | `test_llm_client_retry_primitive.py` | 18 |
| LLM 路由 | `test_llm_router.py` | 7 |
| 异常分类 | `test_llm_exception_classification.py` | 14 |
| 工具包装 | `test_tool_wrapper.py` | 40 |
| 技能路由 | `test_skill_router.py` | 32 |

## 新增测试（v0.4.7）

- `test_llm_exception_classification.py` - 14 个用例
- `test_wecom_download_size_limit.py` - 3 个用例
- `test_json_parse_exception_handling.py` - 9 个用例
- `test_weather_exception_handling.py` - 12 个用例

**新增总计：38 个用例**
