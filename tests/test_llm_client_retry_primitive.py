"""F7/F8 回归：chat() 抽出的重试原语单测。

覆盖：
- _classify_failure 错误分类（限频/鉴权/可重试）
- _RetryState.check_budget 熔断（限频→LLMRateLimitExhaustedError，否则 RuntimeError）
- LLMClient._retry_primary_model 主池重试原语（成功/重试/退避/不可重试/预算熔断/限频轮换）
- LLMClient._try_fallback_pool 备用池原语（成功/鉴权跳过/限频传播/全失败/空池/tool-hint 注入）

确保重构不改变重试/降级行为（仅结构抽离）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from src.llm.client import (
    LLMClient,
    LLMResponse,
    _RetryState,
    _classify_failure,
    _MAX_EXTRA_GLOBAL_ATTEMPTS,
    _TOOL_RESULT_HINT,
)
from src.llm.exceptions import LLMRateLimitExhaustedError


def _cfg(**overrides) -> SimpleNamespace:
    base = dict(
        api_key="dummy",
        base_url="https://api.openai.com/v1",
        timeout=30,
        max_retries=2,
        base_backoff=0.05,
        model="m1",
        temperature=0.7,
        max_tokens=512,
        model_pool=["m1", "m2"],
        fallback_model=None,
        fallback_model_pool=[],
        fallback_base_url=None,
        fallback_api_key=None,
        secondary_fallback_model=None,
        secondary_fallback_model_pool=[],
        secondary_fallback_base_url=None,
        secondary_fallback_api_key=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_do_chat(seq):
    """按调用顺序依次返回/抛出 seq 中的元素（LLMResponse 或 Exception 实例）。"""
    calls = {"n": 0}

    def _impl(client, kwargs, stream=False):
        i = calls["n"]
        calls["n"] += 1
        item = seq[i] if i < len(seq) else seq[-1]
        if isinstance(item, Exception):
            raise item
        return item

    return _impl, calls


# ---------------- _classify_failure ----------------
def test_classify_rate_limit():
    assert _classify_failure(RuntimeError("rate_limit now")) == (True, False, True)
    assert _classify_failure(RuntimeError("429 too many")) == (True, False, True)


def test_classify_auth_not_retryable():
    assert _classify_failure(RuntimeError("401 unauthorized")) == (False, True, False)
    assert _classify_failure(RuntimeError("403 forbidden")) == (False, True, False)


def test_classify_transient_retryable():
    assert _classify_failure(RuntimeError("connection reset")) == (False, False, True)
    assert _classify_failure(RuntimeError("timeout waiting")) == (False, False, True)


def test_classify_unknown_not_retryable():
    assert _classify_failure(RuntimeError("weird error")) == (False, False, False)


# ---------------- _RetryState.check_budget ----------------
def test_check_budget_ok_within_cap():
    s = _RetryState(global_max_attempts=10, total_attempts=5)
    s.check_budget("phase")  # 不抛


def test_check_budget_rate_limited_raises_special():
    s = _RetryState(global_max_attempts=10, total_attempts=11, rate_limited_observed=True, last_err=RuntimeError("x"))
    try:
        s.check_budget("phase")
        assert False, "should raise"
    except LLMRateLimitExhaustedError:
        pass


def test_check_budget_no_ratelimit_raises_runtime():
    s = _RetryState(global_max_attempts=10, total_attempts=11, rate_limited_observed=False, last_err=RuntimeError("x"))
    try:
        s.check_budget("phase")
        assert False, "should raise"
    except RuntimeError:
        pass


# ---------------- _retry_primary_model ----------------
def test_retry_primary_success_first_try():
    c = LLMClient(_cfg())
    resp = LLMResponse(content="ok", tool_calls=[], finish_reason="stop", usage={})
    c._do_chat = _fake_do_chat([resp])[0]
    state = _RetryState(global_max_attempts=100)
    out = c._retry_primary_model(c.client, "m1", {"model": "m1"}, state, 2, 0.05)
    assert out is resp
    assert state.total_attempts == 1


def test_retry_primary_recovers_after_transient():
    c = LLMClient(_cfg())
    resp = LLMResponse(content="ok", tool_calls=[], finish_reason="stop", usage={})
    impl, calls = _fake_do_chat([RuntimeError("timeout"), resp])
    c._do_chat = impl
    with mock.patch("src.llm.client.time.sleep") as m_sleep:
        state = _RetryState(global_max_attempts=100)
        out = c._retry_primary_model(c.client, "m1", {"model": "m1"}, state, 2, 0.05)
    assert out is resp
    assert calls["n"] == 2
    # 退避应在第 1 次失败后睡眠 base_backoff * 2**0
    m_sleep.assert_called_once_with(0.05)


def test_retry_primary_non_retryable_returns_none():
    c = LLMClient(_cfg())
    impl, calls = _fake_do_chat([RuntimeError("401 unauthorized")])
    c._do_chat = impl
    state = _RetryState(global_max_attempts=100)
    out = c._retry_primary_model(c.client, "m1", {"model": "m1"}, state, 2, 0.05)
    assert out is None  # 交给池内下一模型
    assert calls["n"] == 1


def test_retry_primary_budget_exhausted_raises():
    c = LLMClient(_cfg())
    c._do_chat = _fake_do_chat([RuntimeError("timeout")])[0]
    state = _RetryState(global_max_attempts=10, total_attempts=10, rate_limited_observed=True, last_err=RuntimeError("x"))
    try:
        c._retry_primary_model(c.client, "m1", {"model": "m1"}, state, 2, 0.05)
        assert False, "should raise budget error"
    except LLMRateLimitExhaustedError:
        pass


def test_retry_primary_rate_limit_marks_and_rotates():
    c = LLMClient(_cfg(model_pool=["m1", "m2"]))
    # 两次都限频：每轮尝试都应 mark_rate_limited 且轮换池
    impl, _ = _fake_do_chat([RuntimeError("rate_limit"), RuntimeError("rate_limit")])
    c._do_chat = impl
    with mock.patch("src.llm.client.mark_rate_limited") as m_rl:
        state = _RetryState(global_max_attempts=100)
        out = c._retry_primary_model(c.client, "m1", {"model": "m1"}, state, 2, 0.05)
    assert out is None
    assert m_rl.call_count == 2
    assert state.rate_limited_observed is True


# ---------------- _try_fallback_pool ----------------
def test_fallback_pool_success_on_second_model():
    c = LLMClient(_cfg(fallback_model_pool=["fb1", "fb2"], fallback_base_url="https://fb", fallback_api_key="k"))
    resp = LLMResponse(content="fb", tool_calls=[], finish_reason="stop", usage={})
    impl, calls = _fake_do_chat([RuntimeError("rate_limit"), resp])
    c._do_chat = impl
    pool_clients = {"fb1": object(), "fb2": object()}
    state = _RetryState(global_max_attempts=100)
    out = c._try_fallback_pool(c.fallback_order, pool_clients, {"model": "x"}, [{"role": "user"}], state, label="跨服务商备用")
    assert out is resp
    assert calls["n"] == 2
    assert state.rate_limited_observed is True


def test_fallback_pool_auth_error_skips_to_next():
    c = LLMClient(_cfg(fallback_model_pool=["fb1", "fb2"], fallback_base_url="https://fb", fallback_api_key="k"))
    resp = LLMResponse(content="fb", tool_calls=[], finish_reason="stop", usage={})
    impl, calls = _fake_do_chat([RuntimeError("401 unauthorized"), resp])
    c._do_chat = impl
    pool_clients = {"fb1": object(), "fb2": object()}
    state = _RetryState(global_max_attempts=100)
    out = c._try_fallback_pool(c.fallback_order, pool_clients, {"model": "x"}, [], state, label="跨服务商备用")
    assert out is resp
    assert calls["n"] == 2


def test_fallback_pool_all_fail_returns_none_and_records_err():
    c = LLMClient(_cfg(fallback_model_pool=["fb1"], fallback_base_url="https://fb", fallback_api_key="k"))
    err = RuntimeError("500 boom")
    c._do_chat = _fake_do_chat([err])[0]
    pool_clients = {"fb1": object()}
    state = _RetryState(global_max_attempts=100)
    out = c._try_fallback_pool(c.fallback_order, pool_clients, {"model": "x"}, [], state, label="跨服务商备用")
    assert out is None
    assert state.last_fallback_err is err


def test_fallback_pool_empty_clients_returns_none():
    c = LLMClient(_cfg())
    state = _RetryState(global_max_attempts=100)
    out = c._try_fallback_pool([], None, {"model": "x"}, [], state, label="x")
    assert out is None


def test_fallback_pool_injects_tool_hint_when_tool_msg_present():
    c = LLMClient(_cfg(fallback_model_pool=["fb1"], fallback_base_url="https://fb", fallback_api_key="k"))
    resp = LLMResponse(content="fb", tool_calls=[], finish_reason="stop", usage={})
    captured = {}

    def _impl(client, kwargs, stream=False):
        captured["messages"] = kwargs.get("messages")
        return resp

    c._do_chat = _impl
    pool_clients = {"fb1": object()}
    state = _RetryState(global_max_attempts=100)
    msgs = [{"role": "tool", "content": "result"}, {"role": "user", "content": "q"}]
    c._try_fallback_pool(c.fallback_order, pool_clients, {"model": "x", "messages": list(msgs)}, msgs, state, label="x")
    assert captured["messages"][0] == _TOOL_RESULT_HINT
    assert captured["messages"][1:] == msgs


def test_max_extra_global_attempts_is_positive_constant():
    assert isinstance(_MAX_EXTRA_GLOBAL_ATTEMPTS, int) and _MAX_EXTRA_GLOBAL_ATTEMPTS > 0
