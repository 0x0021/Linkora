"""回归：全模型限频(429)耗尽时的优雅降级（⑤）。

- 主模型池 + 跨服务商备用池**全部因 429/rate_limit 失败**时，chat() 必须抛出
  LLMRateLimitExhaustedError（临时性故障，供 main 层记入死信队列 DLQ、不向
  用户回复），而非通用 RuntimeError。
- 非限频类失败（如 500）仍抛通用 RuntimeError，不受影响。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.client import LLMClient
from src.llm.exceptions import LLMRateLimitExhaustedError


def _make_cfg() -> SimpleNamespace:
    # 主池 2 个模型 + 跨服务商备用 1 个，触发完整「主池→备用池」链路
    return SimpleNamespace(
        api_key="dummy",
        base_url="https://api.openai.com/v1",
        timeout=30,
        max_retries=1,
        base_backoff=0.01,
        model="primary-model",
        temperature=0.7,
        max_tokens=512,
        model_pool=["primary-model", "primary-b"],
        fallback_model=None,
        fallback_model_pool=["fb-a"],
        fallback_base_url="https://api.fallback.com/v1",
        fallback_api_key="dummy-fb",
    )


def test_all_429_raises_rate_limit_exhausted():
    client = LLMClient(_make_cfg())

    def fake_429(c, kwargs, stream=False, **_kw):
        raise RuntimeError("Error 429: rate_limit exceeded, too many requests")

    client._do_chat = fake_429
    with pytest.raises(LLMRateLimitExhaustedError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    text = str(exc.value).lower()
    assert "429" in text or "限频" in text or "rate" in text


def test_non_429_failure_raises_runtime_error():
    client = LLMClient(_make_cfg())

    def fake_generic(c, kwargs, stream=False, **_kw):
        raise RuntimeError("internal server error 500")

    client._do_chat = fake_generic
    with pytest.raises(RuntimeError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    # 必须是通用错误，而非被误判为限频
    assert not isinstance(exc.value, LLMRateLimitExhaustedError)
