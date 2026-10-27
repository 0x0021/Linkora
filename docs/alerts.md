# Linkora 错误监控告警系统

## 功能

1. **关键异常监控**：监控 LLMNetworkError、LLMRateLimitError、LLMAuthError、DBBusyError 等
2. **阈值告警**：在异常频率超过阈值时触发告警
3. **多渠道通知**：支持日志、Webhook、邮件
4. **防抖动**：同一错误类型在静默期内不重复告警

## 快速开始

```python
from src.alerts.manager import get_alert_manager, record_error

# 记录错误（LLM 最终失败会自动调用，一般无需手动）
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

## 与 LLM 客户端的接线（**已自动接好**）

**v0.8.2 起，LLM 最终失败会自动记入告警，不需要你手动集成。**

此前这套告警系统没有任何调用方 —— `record_error()` 全仓零调用，`src/llm/client.py` 里
也没有记录点。结果是 `GET /api/alerts/stats` 永远返回空、阈值告警永不触发，而本文档
还在教「在 `src/llm/client.py` 中集成告警」——照着做等于自己接一遍。

现在的接线位置（`src/llm/client.py` 的 `chat()`）：

- **只在最终失败出口记录一次** —— 主模型池耗尽（限频 / 达到全局最大尝试次数 / 全部模型
  处于冷却期）与降级池耗尽，共 6 个出口；
- **不在重试循环内记录** —— 否则一次限频风暴会按重试次数虚增计数，把阈值告警虚假触发；
- 记录动作是**旁路**的：记录本身失败不会影响主链路的抛错行为。

**异常类型 → 告警类型的映射**（决定告警的严重程度分级）：

| 异常 | 告警类型 | 严重程度 |
|---|---|---|
| `LLMRateLimitError` / 限频 | `LLMRateLimitError` | ERROR |
| `LLMAuthError` / 401·403 | `LLMAuthError` | CRITICAL |
| 其它（含网络异常） | `LLMNetworkError` | ERROR |

## ⚠️ 目前 webhook 还没有 `config.yaml` 配置入口

`AlertConfig` 是个独立的数据类，**不在 `src/config_models/` 的配置体系里**，因此：

- `config.yaml` 里**没有**告警相关配置项，加了也不会被读取；
- 管理台的 `PUT /api/alerts/config` 只开放三个阈值字段（`error_threshold` /
  `time_window_seconds` / `silence_period_seconds`），**改不了渠道与 webhook 地址**；
- 要用 webhook 渠道，需在代码里构造：

```python
from src.alerts.manager import AlertConfig, AlertManager, get_alert_manager

manager = AlertManager(
    AlertConfig(
        error_threshold=10,
        time_window_seconds=300,
        silence_period_seconds=600,
        channels=["log", "webhook"],
        webhook_url="https://hooks.slack.com/...",
    )
)
```

> 需要「配在 YAML 里就能用」的话，说一声就加 —— 这是一个独立的小改动。

**邮件渠道**同理：仓库内没有 SMTP 基础设施，配了也发不出去。现在会**明确记日志**而不是
静默丢弃（此前是静默的，容易让人以为邮件发出去了）。

## Webhook 投递的安全约束

Webhook 地址由部署方配置（不是用户输入），但仍按出站请求处理：投递前会解析 DNS 并
**钉死目标 IP**，挡掉指向内网 / 保留地址的写法，且不跟随重定向（避免 302 到内网）。
投递失败降级为日志，不影响分身正常回复。
```

## 测试

```bash
.venv/bin/python -m pytest tests/test_alert_manager.py -v
.venv/bin/python -m pytest tests/test_llm_alert_integration.py -v
.venv/bin/python -m pytest tests/test_alert_wiring.py -v   # 接线与 webhook 消费
```
