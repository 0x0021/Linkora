# 测试指南

## 快速开始

```bash
# 运行所有测试
pytest tests/ -v

# 运行指定模块测试
pytest tests/test_rule_engine.py -v
pytest tests/test_dws_adapter.py -v

# 查看覆盖率报告
pytest tests/ --cov=src --cov-report=term-missing

# 生成HTML覆盖率报告
pytest tests/ --cov=src --cov-report=html
# 报告位置：htmlcov/index.html
```

## 测试覆盖范围

### ✅ 已覆盖模块

| 模块 | 测试文件 | 用例数 | 核心场景 |
|------|---------|--------|---------|
| **规则引擎** | `test_rule_engine.py` | 24 | 黑白名单、关键词匹配(exact/fuzzy/regex)、意图识别、停用词过滤、正则捕获组替换 |
| **DWS适配器** | `test_dws_adapter.py` | 23 | 错误分类(可重试/不可重试)、重试机制(指数退避)、dry_run模式、JSON解析容错 |
| **日志系统** | `test_logger.py` | 1 | 控制台彩色输出 |

### 📋 待补充模块

以下模块逻辑复杂但尚未有自动化测试，建议后续补充：

| 模块 | 优先级 | 测试要点 |
|------|--------|---------|
| `src/poller.py` (1107行) |  高 | 消息去重缓存(LRU+TTL)、无效会话持久化、轮询循环逻辑 |
| `src/memory/sqlite_store.py` (1429行) |  高 | CRUD操作、向量索引同步、记忆清理策略 |
| `src/llm/agent.py` (381行) |  中 | Tool Calling流程、主备模型切换、回复清洗 |
| `src/tools/kb_search.py` (375行) | 🟡 中 | RAG检索、混合重排序、FAISS查询 |
| `src/doc_sync_scheduler.py` (218行) | 🟢 低 | 定时同步、钉钉文档拉取 |

## 编写新测试的约定

### Fixture 使用

`tests/conftest.py` 提供共享 fixture：

```python
def test_example(rule_engine, msg_single_chat, mock_dws):
    # rule_engine: 预配置的规则引擎实例（无DB依赖）
    # msg_single_chat: 单聊消息 fixture
    # mock_dws: Mock DWS Adapter
    
    msg_single_chat.content = "VPN故障"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"
```

可用 fixture 清单：
- `tmp_db_path` — 临时SQLite数据库路径（自动清理）
- `msg_single_chat` / `msg_group_chat` / `msg_empty_content` — 消息对象
- `mock_dws` — Mock DWS Adapter
- `rule_engine_config` — 基础配置字典
- `rule_engine` — 规则引擎实例

### 命名规范

```python
class TestFeatureName:          # 类名 Test + 功能名（驼峰）
    def test_specific_scenario(self, ...):  # 方法名 test_ + 场景描述（蛇形）
        """中文 docstring 说明测试意图。"""
        assert expected == actual
```

### Mock 原则

- **DWS CLI调用必须mock**：避免真实网络请求和登录态依赖
- **数据库操作用tmp_db_path**：测试后自动清理，不污染生产数据
- **时间敏感逻辑用freezegun**（需额外安装）或手动注入datetime参数

## 持续集成建议

在 CI 中添加测试步骤：

```yaml
# .github/workflows/test.yml
- name: Run Tests
  run: |
    pip install -r requirements.txt
    pytest tests/ --cov=src --cov-report=xml --junitxml=test-results.xml

- name: Upload Coverage
  uses: codecov/codecov-action@v3
  with:
    file: ./coverage.xml
```

## 注意事项

1. **不要测试私有实现细节**：测试行为而非内部状态（如 `_db_keywords` 列表顺序）
2. **每个测试独立**：不依赖执行顺序，fixture 保证隔离性
3. **失败信息要清晰**：assert 消息说明期望 vs 实际，方便定位问题
4. **慢测试加标记**：`@pytest.mark.slow` 标记耗时>1s的测试，CI中可选择跳过
