"""告警管理器单元测试。

验证：
1. 错误计数正确累加
2. 阈值触发告警
3. 静默期防止重复告警
4. 严重程度正确分类
5. 统计信息正确返回
"""
from __future__ import annotations



from src.alerts.manager import AlertConfig, AlertManager, AlertSeverity


class TestAlertManager:
    """AlertManager 基础功能测试。"""

    def test_record_error_increments_count(self):
        """验证错误计数正确累加。"""
        manager = AlertManager()
        manager.record_error("TestError", "error message")
        stats = manager.get_stats()
        assert stats["total_error_types"] == 1
        assert stats["errors"]["TestError"]["count"] == 1

    def test_record_error_same_type_accumulates(self):
        """验证同类型错误累加。"""
        manager = AlertManager()
        for i in range(5):
            manager.record_error("TestError", f"error {i}")
        stats = manager.get_stats()
        assert stats["errors"]["TestError"]["count"] == 5

    def test_record_error_different_types(self):
        """验证不同类型错误独立计数。"""
        manager = AlertManager()
        manager.record_error("ErrorA", "msg A")
        manager.record_error("ErrorB", "msg B")
        stats = manager.get_stats()
        assert stats["errors"]["ErrorA"]["count"] == 1
        assert stats["errors"]["ErrorB"]["count"] == 1

    def test_clear_stats_resets_counts(self):
        """验证清空统计信息。"""
        manager = AlertManager()
        manager.record_error("TestError", "msg")
        manager.clear_stats()
        stats = manager.get_stats()
        assert stats["total_error_types"] == 0


class TestAlertThreshold:
    """告警阈值触发测试。"""

    def test_alert_triggers_when_threshold_reached(self):
        """验证达到阈值时触发告警。"""
        manager = AlertManager(AlertConfig(error_threshold=3, silence_period_seconds=0))
        alerts = []
        manager.register_callback(lambda x: alerts.append(x))

        manager.record_error("TestError", "msg1")
        manager.record_error("TestError", "msg2")
        manager.record_error("TestError", "msg3")

        assert len(alerts) == 1
        assert alerts[0]["error_type"] == "TestError"
        assert alerts[0]["count"] == 3

    def test_alert_not_triggered_below_threshold(self):
        """验证未达到阈值时不触发告警。"""
        manager = AlertManager(AlertConfig(error_threshold=5, silence_period_seconds=0))
        alerts = []
        manager.register_callback(lambda x: alerts.append(x))

        for i in range(3):
            manager.record_error("TestError", f"msg {i}")

        assert len(alerts) == 0

    def test_silence_period_prevents_duplicate_alerts(self):
        """验证静默期防止重复告警。"""
        manager = AlertManager(AlertConfig(error_threshold=3, silence_period_seconds=600))
        alerts = []
        manager.register_callback(lambda x: alerts.append(x))

        # 第一次触发
        for i in range(3):
            manager.record_error("TestError", f"msg {i}")
        assert len(alerts) == 1

        # 再次触发（应在静默期内）
        for i in range(3):
            manager.record_error("TestError", f"msg {i}")
        assert len(alerts) == 1  # 不应新增告警


class TestAlertSeverity:
    """告警严重程度测试。"""

    def test_critical_severity_for_auth_error(self):
        """验证 LLMAuthError 为 critical。"""
        manager = AlertManager()
        assert manager._get_severity("LLMAuthError") == AlertSeverity.CRITICAL.value

    def test_critical_severity_for_db_busy(self):
        """验证 DBBusyError 为 critical。"""
        manager = AlertManager()
        assert manager._get_severity("DBBusyError") == AlertSeverity.CRITICAL.value

    def test_error_severity_for_llm_network(self):
        """验证 LLMNetworkError 为 error。"""
        manager = AlertManager()
        assert manager._get_severity("LLMNetworkError") == AlertSeverity.ERROR.value

    def test_error_severity_for_llm_rate_limit(self):
        """验证 LLMRateLimitError 为 error。"""
        manager = AlertManager()
        assert manager._get_severity("LLMRateLimitError") == AlertSeverity.ERROR.value

    def test_warning_severity_for_unknown(self):
        """验证未知错误类型为 warning。"""
        manager = AlertManager()
        assert manager._get_severity("UnknownError") == AlertSeverity.WARNING.value


class TestAlertSamples:
    """告警样本保存测试。"""

    def test_samples_saved_up_to_limit(self):
        """验证样本保存到最多 5 个。"""
        manager = AlertManager()
        for i in range(10):
            manager.record_error("TestError", f"error message {i}")
        stats = manager.get_stats()
        assert len(stats["errors"]["TestError"]["samples"]) == 5

    def test_sample_truncated(self):
        """验证长消息被截断。"""
        manager = AlertManager()
        long_message = "x" * 200
        manager.record_error("TestError", long_message)
        stats = manager.get_stats()
        assert len(stats["errors"]["TestError"]["samples"][0]) <= 103  # 100 + "..."


class TestGlobalFunctions:
    """全局函数测试。"""

    def test_get_alert_manager_returns_singleton(self):
        """验证 get_alert_manager 返回单例。"""
        from src.alerts.manager import get_alert_manager
        manager1 = get_alert_manager()
        manager2 = get_alert_manager()
        assert manager1 is manager2

    def test_record_error_uses_singleton(self):
        """验证 record_error 使用单例。"""
        from src.alerts.manager import record_error, get_alert_manager
        record_error("TestError", "msg")
        manager = get_alert_manager()
        stats = manager.get_stats()
        assert stats["errors"]["TestError"]["count"] == 1
