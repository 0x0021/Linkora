# 大文件拆分方案（2026-08-14）

## 分析结果

| 文件 | 行数 | 主要类/函数 | 拆分建议 |
|------|------|------------|---------|
| tools/weather.py | 939 | WeatherTool + 辅助函数 | ✅ 可拆分为 weather_core.py + weather_cli.py |
| tools/parse_document.py | 932 | DocumentParser | ⚠️ 耦合度高，保持单文件 |
| config_models.py | 901 | 27 个 Pydantic 模型 | ✅ 拆分为 base.py + platforms.py |
| llm/reply.py | 887 | sanitize_reply 等 | ⚠️ 逻辑复杂，保持单文件 |
| im_adapter/wecom.py | 885 | WecomCliAdapter | ❌ 单一类，无需拆分 |
| im_adapter/feishu.py | 876 | FeishuCliAdapter | ❌ 继承链复杂，保持单文件 |
| poller_strategy.py | 860 | PollerStrategyMixin | ⚠️ 可考虑拆分策略核心 |
| tools/web_search.py | 850 | searxng/bing/ddg search | ⚠️ 各引擎独立，保持单文件 |
| poller_core_parse.py | 822 | ParseMixin | ❌ 解析逻辑紧凑，保持单文件 |

## 推荐优先级

1. **config_models.py** → base.py (基础模型) + platforms.py (平台特定模型)
2. **weather.py** → weather_core.py (核心天气逻辑) + weather_cli.py (CLI 调用)
3. **其他文件** → 暂不拆分，评估实际复杂度

## 注意事项

- 拆分后需保持向后兼容的导入路径
- 避免循环导入
- 单元测试需同步更新
