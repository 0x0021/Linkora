"""LLM 全模型冷却期的错误分类回归测试（P0-7，2026-10-04 审计发现）。

背景：`chat()` 的主模型池遍历中，处于冷却期的模型会被 `continue` 跳过。若**全部**
模型都在冷却期，则 `_retry_primary_model` 一次都没被调用 → `state.last_err` 保持
初值 `None` → 历史代码 `raise state.last_err` 即 `raise None` →
`TypeError: exceptions must derive from BaseException`。

三重危害：
1. 真实原因（模型全在冷却）被彻底掩盖，用户只看到一个语义不明的 TypeError；
2. `agent_steps/reply.py` 的 `isinstance(e, LLMRateLimitExhaustedError)` 分支不会
   命中，错误分类彻底失效；
3. 触发条件恰是最需要优雅降级的时刻——一次限频风暴后
   `rate_limit_cooldown` 秒内的任何重入请求（生产默认 30s）。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.llm.client import LLMClient, LLMRateLimitExhaustedError  # noqa: E402


def _make_config(model_pool):
    cfg = load_config("config.yaml")
    cfg.llm.model = "primary-model"
    cfg.llm.base_url = "https://example.com/v1"
    cfg.llm.api_key = "test-key"
    cfg.llm.fallback_model = ""
    cfg.llm.fallback_base_url = ""
    cfg.llm.fallback_model_pool = []
    cfg.llm.secondary_fallback_model_pool = []
    cfg.llm.secondary_fallback_model = ""
    cfg.llm.model_pool = model_pool
    return cfg.llm


def _cool_down_all(client, models, seconds=60.0):
    for m in models:
        client._set_cooldown(m, seconds)


def test_all_models_in_cooldown_raises_rate_limit_error():
    """全部主模型处于冷却期 → 抛 LLMRateLimitExhaustedError，而非 TypeError。

    回归护栏：修复前此处是 `raise None` → TypeError。
    """
    cfg = _make_config(["m1", "m2", "m3"])
    client = LLMClient(cfg)
    _cool_down_all(client, ["primary-model", "m1", "m2", "m3"])

    with pytest.raises(LLMRateLimitExhaustedError) as ei:
        client.chat(messages=[{"role": "user", "content": "hi"}])

    msg = str(ei.value)
    # 应能看出是「全部冷却」而非泛化的失败
    assert "冷却" in msg, f"错误信息未点明冷却期: {msg}"
    # 且不要求调用方去解析 None
    assert ei.value.__cause__ is None or isinstance(ei.value.__cause__, BaseException)


def test_all_models_in_cooldown_not_generic_typeerror():
    """显式守护：不得抛 TypeError（那正是 raise None 的症状）。"""
    cfg = _make_config(["m1", "m2"])
    client = LLMClient(cfg)
    _cool_down_all(client, ["primary-model", "m1", "m2"])

    with pytest.raises(Exception) as ei:  # noqa: PT011 — 关心的是异常类型
        client.chat(messages=[{"role": "user", "content": "hi"}])
    assert not isinstance(ei.value, TypeError), (
        f"冷却期抛出了 TypeError（raise None 的症状）: {ei.value}"
    )


def test_single_model_pool_all_cooled_also_handled():
    """单模型（无 model_pool）全冷却同样应给可读错误。"""
    cfg = _make_config([])
    client = LLMClient(cfg)
    _cool_down_all(client, ["primary-model"])

    with pytest.raises(LLMRateLimitExhaustedError) as ei:
        client.chat(messages=[{"role": "user", "content": "hi"}])
    assert "冷却" in str(ei.value)


def test_expired_cooldown_still_calls_model():
    """冷却期已过期时应正常尝试模型（防过度拦截：修复不能把正常路径也挡掉）。"""
    cfg = _make_config([])
    client = LLMClient(cfg)
    client._set_cooldown("primary-model", 0.01)
    import time as _t

    _t.sleep(0.05)

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "ok"
    resp.choices[0].message.tool_calls = None
    resp.choices[0].finish_reason = "stop"
    resp.usage = None
    fake = MagicMock()
    fake.chat.completions.create.return_value = resp
    # chat() 的 attempts 来自 [(self.client, m) for m in self.model_pool]
    client.client = fake

    out = client.chat(messages=[{"role": "user", "content": "hi"}])
    assert out is not None
    assert fake.chat.completions.create.called, "冷却期已过期却未真正调用模型"
