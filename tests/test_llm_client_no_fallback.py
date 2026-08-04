"""回归：无备用模型配置时 LLMClient 必须正常初始化且不崩。

历史 latent bug：__init__ 的 else 分支（无 fallback_model / fallback_model_pool）
未初始化 self.fallback_order，导致 chat() 第 211 行 `len(self.fallback_order)`
抛 AttributeError。生产配置恒有 fallback 池故不触发，但属于真实风险，现已补
`self.fallback_order = []` 初始化。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.llm.client import LLMClient, LLMResponse


def _make_no_fallback_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        api_key="dummy",
        base_url="https://api.openai.com/v1",
        timeout=30,
        max_retries=2,
        base_backoff=0.05,
        model="primary-model",
        temperature=0.7,
        max_tokens=512,
        model_pool=[],            # 仅主模型，无同池备选
        fallback_model=None,
        fallback_model_pool=[],   # 无跨服务商备用
        fallback_base_url=None,
        fallback_api_key=None,
    )


def test_no_fallback_config_initializes_fallback_order():
    """无 fallback 时 fallback_order 必须初始化为空列表（不遗漏属性）。"""
    client = LLMClient(_make_no_fallback_cfg())
    assert client.fallback_order == []
    assert client.fallback_clients is None


def test_no_fallback_chat_does_not_crash_on_fallback_order():
    """chat() 在无 fallback 配置下必须走到主模型调用并返回，不抛 AttributeError。"""
    client = LLMClient(_make_no_fallback_cfg())

    def fake_do_chat(c, kwargs, stream=False):
        return LLMResponse(content="ok", tool_calls=[], finish_reason="stop", usage={})

    client._do_chat = fake_do_chat
    resp = client.chat([{"role": "system", "content": "hi"}])
    assert resp.content == "ok"
