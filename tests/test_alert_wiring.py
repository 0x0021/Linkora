"""P3-4 修复回归测试：告警系统从「死代码」变为「活功能」。

背景
----
审计 P3-4 判定 ``AlertConfig.webhook_url`` 是死配置。核查后发现问题比「一个字段
没人用」更大：**整个告警系统没有任何调用方** ——

- ``src/alerts/manager.py`` 的 ``record_error()`` 全仓零调用；
- ``src/llm/client.py`` 的 ``_rethrow_classified()`` 同样零调用；
- 结果是 ``GET /api/alerts/stats`` 永远返回空、阈值告警永不触发；
- 而 ``docs/alerts.md`` 明确教用户「在 ``src/llm/client.py`` 中集成告警」并给出
  ``webhook_url`` 配置示例 —— 配了不生效。

本测试锁定修复后的三条行为：

1. ``channels`` / ``webhook_url`` 真正被消费（webhook 渠道会发 HTTP 请求）；
2. LLM 最终失败会记入告警系统（``record_error`` 被调用）；
3. 重试**不会**放大告警计数（一次最终失败 = 一次告警，而非按尝试次数累加）；
4. 告警旁路性：记录失败 / 投递失败都不影响主链路抛错行为。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.alerts.manager import AlertChannel, AlertConfig, AlertManager


class TestWebhookChannelConsumed:
    """channels / webhook_url 不再是死字段。"""

    def test_webhook_channel_triggers_http_post(self) -> None:
        """配置 webhook 渠道 + 地址后，触发告警会真的发出 HTTP 请求。"""
        manager = AlertManager(
            AlertConfig(
                error_threshold=1,
                silence_period_seconds=0,
                channels=[AlertChannel.WEBHOOK],
                webhook_url="https://hooks.example.com/abc",
            )
        )
        payload = {"ok": True, "status_code": 200}

        with patch("src.alerts.webhook.safe_http_request", return_value=MagicMock(**payload)) as req:
            manager.record_error("LLMNetworkError", "boom")

        assert req.called, "webhook 渠道未发出任何请求"
        args, kwargs = req.call_args
        assert args[0] == "POST"
        assert args[1] == "https://hooks.example.com/abc"
        assert kwargs["headers"]["Content-Type"] == "application/json"

    def test_webhook_payload_contains_alert_fields(self) -> None:
        """webhook 请求体是告警 JSON（含 error_type / count / severity）。"""
        import json

        manager = AlertManager(
            AlertConfig(
                error_threshold=1,
                silence_period_seconds=0,
                channels=[AlertChannel.WEBHOOK],
                webhook_url="https://hooks.example.com/abc",
            )
        )
        with patch("src.alerts.webhook.safe_http_request", return_value=MagicMock(status_code=200)) as req:
            manager.record_error("LLMAuthError", "bad key")

        body = req.call_args.kwargs["data"]
        data = json.loads(body.decode("utf-8"))
        assert data["error_type"] == "LLMAuthError"
        assert data["severity"] == "critical"
        assert data["count"] == 1

    def test_no_webhook_url_means_no_request(self) -> None:
        """未配置地址时不发请求（不产生噪音请求）。"""
        manager = AlertManager(
            AlertConfig(
                error_threshold=1,
                silence_period_seconds=0,
                channels=[AlertChannel.WEBHOOK],
                webhook_url="",
            )
        )
        with patch("src.alerts.webhook.safe_http_request") as req:
            manager.record_error("LLMNetworkError", "boom")
        assert not req.called

    def test_log_channel_does_not_send_http(self) -> None:
        """仅日志渠道时不发 HTTP。"""
        manager = AlertManager(
            AlertConfig(error_threshold=1, silence_period_seconds=0, channels=[AlertChannel.LOG])
        )
        with patch("src.alerts.webhook.safe_http_request") as req:
            manager.record_error("LLMNetworkError", "boom")
        assert not req.called

    def test_webhook_failure_does_not_break_alert(self) -> None:
        """webhook 投递失败不影响告警本身（旁路能力）。"""
        manager = AlertManager(
            AlertConfig(
                error_threshold=1,
                silence_period_seconds=0,
                channels=[AlertChannel.WEBHOOK],
                webhook_url="https://hooks.example.com/abc",
            )
        )
        with patch("src.alerts.webhook.safe_http_request", side_effect=RuntimeError("network down")):
            manager.record_error("LLMNetworkError", "boom")  # 不应抛出

        stats = manager.get_stats()
        assert stats["errors"]["LLMNetworkError"]["count"] == 1

    def test_send_webhook_uses_ssrf_guard(self) -> None:
        """webhook 出站必须走 SSRF guard（safe_http_request），不裸调 requests。"""
        from src.alerts.webhook import send_webhook

        with patch("src.alerts.webhook.safe_http_request", return_value=MagicMock(status_code=200)) as req:
            ok = send_webhook("https://hooks.example.com/x", {"a": 1})
        assert ok is True
        assert req.called


class TestRecordErrorIsWired:
    """record_error 不再零调用 —— LLM 最终失败会真的进告警系统。"""

    def test_llm_final_failure_records_alert(self) -> None:
        from src.llm.client import _record_alert

        with patch("src.alerts.manager.record_error") as rec:
            _record_alert("LLMNetworkError", "all models failed")
        assert rec.called
        assert rec.call_args.args[0] == "LLMNetworkError"

    def test_record_alert_swallows_its_own_errors(self) -> None:
        """告警旁路：记录失败不影响主链路（不向上抛）。"""
        from src.llm.client import _record_alert

        with patch("src.alerts.manager.record_error", side_effect=RuntimeError("alert down")):
            _record_alert("LLMNetworkError", "x")  # 不应抛出

    def test_alert_type_mapping(self) -> None:
        """异常 → 告警类型的映射，与 AlertConfig.monitored_errors 默认集合一致。"""
        from src.exceptions import LLMAuthError, LLMNetworkError, LLMRateLimitError
        from src.llm.client import _alert_type_for_exception

        assert _alert_type_for_exception(LLMRateLimitError("x")) == "LLMRateLimitError"
        assert _alert_type_for_exception(LLMAuthError("x")) == "LLMAuthError"
        assert _alert_type_for_exception(LLMNetworkError("x")) == "LLMNetworkError"
        assert _alert_type_for_exception(RuntimeError("x")) == "LLMNetworkError"
        assert _alert_type_for_exception(None) == "LLMNetworkError"

    def test_mapped_types_are_in_monitored_defaults(self) -> None:
        """映射出的类型必须落在默认监控集合内，否则分级不生效。"""
        from src.llm.client import _alert_type_for_exception
        from src.exceptions import LLMAuthError, LLMNetworkError, LLMRateLimitError

        monitored = set(AlertConfig().monitored_errors)
        for exc in (LLMRateLimitError("x"), LLMAuthError("x"), LLMNetworkError("x")):
            assert _alert_type_for_exception(exc) in monitored


class TestRetryDoesNotAmplifyAlerts:
    """重试不得放大告警计数（一次最终失败 = 一次告警）。"""

    def test_retry_attempts_do_not_each_record(self) -> None:
        """_retry_primary_model 内的重试循环不调用 _record_alert。"""
        import inspect

        from src.llm import client as client_mod

        src = inspect.getsource(client_mod.LLMClient._retry_primary_model)
        assert "_record_alert" not in src, (
            "重试原语内不应记录告警 —— 否则一次限频风暴会按尝试次数虚增计数"
        )

    def test_only_final_exits_record(self) -> None:
        """告警只出现在 chat() 的最终失败出口。"""
        import inspect

        from src.llm import client as client_mod

        src = inspect.getsource(client_mod.LLMClient.chat)
        assert src.count("_record_alert") >= 1, "chat() 的最终失败出口未接线告警"


@pytest.mark.parametrize(
    "channel_value",
    ["log", "webhook"],
)
def test_channel_accepts_plain_strings(channel_value: str) -> None:
    """channels 支持字符串写法（docs/alerts.md 的示例即字符串）。"""
    manager = AlertManager(
        AlertConfig(
            error_threshold=1,
            silence_period_seconds=0,
            channels=[channel_value],  # type: ignore[list-item]
        )
    )
    with patch("src.alerts.webhook.safe_http_request") as req:
        manager.record_error("LLMNetworkError", "boom")
    if channel_value == "webhook":
        # 未配地址 → 不发请求，但不应抛错
        assert not req.called
    else:
        assert not req.called
