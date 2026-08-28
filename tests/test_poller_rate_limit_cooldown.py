"""平台级 chat 限频退避（RATE_LIMIT_ERROR 断路器）单元测试。

覆盖:
- _is_chat_rate_limit_error 识别 RATE_LIMIT_ERROR
- _handle_fetch_errors 命中限频 → 设置平台级冷却并返回跳过
- _enter_chat_rate_limit_cooldown 按 config 设置冷却（0=关闭）
- _in_chat_rate_limit_cooldown 冷却期内/到期后的判定
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.poller_strategy import PollerStrategyMixin


class FakeRateLimitPoller(PollerStrategyMixin):
    """最小 fake：只提供限频退避逻辑依赖的属性。"""

    def __init__(self, cooldown: int = 60, platform: str = "dingtalk"):
        self.config = SimpleNamespace(chat_rate_limit_cooldown_seconds=cooldown)
        self.platform_id = platform
        self._chat_rate_limited_until: dict[str, float] = {}


def _rate_limit_err() -> Exception:
    # 复刻 DWS 真实返回：exit 1 + JSON（含 server_error_code=RATE_LIMIT_ERROR）
    return RuntimeError(
        'dws exit 1: {"error": {"server_error_code": "RATE_LIMIT_ERROR", '
        '"message": "[UNCLASSIFIED] business error: success=false '
        '(operation: chat/list_conversation_message_v2)"}}'
    )


def _normal_err() -> Exception:
    return RuntimeError("some unrelated error")


# ============ 识别 ============

class TestIsChatRateLimitError:
    def test_detects_rate_limit(self):
        p = FakeRateLimitPoller()
        assert p._is_chat_rate_limit_error(_rate_limit_err()) is True

    def test_ignores_normal_error(self):
        p = FakeRateLimitPoller()
        assert p._is_chat_rate_limit_error(_normal_err()) is False


# ============ 进入冷却 ============

class TestEnterCooldown:
    def test_sets_cooldown_timestamp(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr("time.time", lambda: now)
        p = FakeRateLimitPoller(cooldown=60)
        p._enter_chat_rate_limit_cooldown()
        assert p._chat_rate_limited_until.get("dingtalk") == now + 60

    def test_disabled_when_cooldown_zero(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeRateLimitPoller(cooldown=0)
        p._enter_chat_rate_limit_cooldown()
        assert p._chat_rate_limited_until == {}

    def test_per_platform_isolation(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr("time.time", lambda: now)
        p = FakeRateLimitPoller(cooldown=60)
        p.platform_id = "feishu"
        p._enter_chat_rate_limit_cooldown()
        assert "feishu" in p._chat_rate_limited_until
        assert "dingtalk" not in p._chat_rate_limited_until


# ============ 冷却期判定 ============

class TestInCooldown:
    def test_true_during_cooldown(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr("time.time", lambda: now)
        p = FakeRateLimitPoller(cooldown=60)
        p._enter_chat_rate_limit_cooldown()
        # 仍在冷却期内
        monkeypatch.setattr("time.time", lambda: now + 30)
        assert p._in_chat_rate_limit_cooldown() is True

    def test_false_after_cooldown(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr("time.time", lambda: now)
        p = FakeRateLimitPoller(cooldown=60)
        p._enter_chat_rate_limit_cooldown()
        # 冷却已过期
        monkeypatch.setattr("time.time", lambda: now + 61)
        assert p._in_chat_rate_limit_cooldown() is False

    def test_false_when_no_cooldown(self):
        p = FakeRateLimitPoller()
        assert p._in_chat_rate_limit_cooldown() is False


# ============ _handle_fetch_errors 集成 ============

class TestHandleFetchErrorsRateLimit:
    def test_rate_limit_sets_cooldown_and_skips(self, monkeypatch):
        now = 1_000.0
        monkeypatch.setattr("time.time", lambda: now)
        p = FakeRateLimitPoller(cooldown=60)
        # store 仅被权限分支使用，限频分支在到达前已 return，用 MagicMock 兜底
        p.store = MagicMock()
        result = p._handle_fetch_errors(
            _rate_limit_err(), "oc_x", "某会话", False, "group"
        )
        assert result is True  # 跳过该会话
        assert p._chat_rate_limited_until.get("dingtalk") == now + 60

    def test_normal_error_not_treated_as_rate_limit(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeRateLimitPoller(cooldown=60)
        p.store = MagicMock()
        # 普通错误不应触发限频冷却
        p._handle_fetch_errors(_normal_err(), "oc_x", "某会话", False, "group")
        assert p._chat_rate_limited_until == {}
