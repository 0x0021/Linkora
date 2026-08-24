# Linkora 错误监控告警系统

## 功能

1. **关键异常监控**：监控 LLMNetworkError、LLMRateLimitError、LLMAuthError、DBBusyError 等
2. **阈值告警**：在异常频率超过阈值时触发告警
3. **多渠道通知**：支持日志、Webhook、邮件
4. **防抖动**：同一错误类型在静默期内不重复告警

## 快速开始

```python
from src.alerts.manager import get_alert_manager, record_error

# 记录错误
record_error("LLMNetworkError", "connection timeout")

# 获取统计
manager = get_alert_manager()
stats = manager.get_stats()
print(stats)
```

## 配置

```python
from src.alerts.manager import AlertConfig, AlertManager

config = AlertConfig(
    error_threshold=10,           # 错误次数阈值
    time_window_seconds=300,      # 时间窗口（5 分钟）
    silence_period_seconds=600,   # 静默期（10 分钟）
    channels=["log", "webhook"],  # 告警渠道
    webhook_url="https://hooks.slack.com/...",
)

manager = AlertManager(config)
```

## 告警严重程度

| 级别 | 错误类型 |
|------|---------|
| CRITICAL | LLMAuthError, DBBusyError, IMAdapterRateLimitError |
| ERROR | LLMNetworkError, LLMRateLimitError |
| WARNING | 其他错误 |

## 集成到 LLM 客户端

在 `src/llm/client.py` 中集成告警：

```python
from src.alerts.manager import record_error

try:
    return self._do_chat(client, model_kwargs, stream=True)
except (APIConnectionError, APITimeoutError) as e:
    record_error("LLMNetworkError", str(e))
    raise
```

## 测试

```bash
.venv/bin/python -m pytest tests/test_alert_manager.py -v
.venv/bin/python -m pytest tests/test_llm_alert_integration.py -v
```
