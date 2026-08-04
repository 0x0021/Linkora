"""模型池（同服务商免费模型轮换）的单元测试。

覆盖：
- LLMClient 构建轮换顺序（主模型 + model_pool 去重）
- 主模型失败后按 model_pool 顺序轮换
- 鉴权错误（不可重试）直接跳到下一模型而非重试浪费
- 池内全部失败后降级到跨服务商 fallback
- config.yaml 中 model_pool 字段可解析
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402


def _make_config(model_pool=None, fallback_model="", fallback_base_url=""):
    cfg = load_config("config.yaml")
    cfg.llm.model = "primary-model"
    cfg.llm.base_url = "https://example.com/v1"
    cfg.llm.api_key = "test-key"
    cfg.llm.fallback_model = fallback_model
    cfg.llm.fallback_base_url = fallback_base_url
    cfg.llm.fallback_model_pool = []  # 默认清空真实配置里的备用池，避免污染单测
    # 同理隔离第二层备用池（真实 config.yaml 配了 GLM-4.6V-Flash 等），
    # 否则单测会无意继承运行时配置、产生意料外的调用序列。
    cfg.llm.secondary_fallback_model_pool = []
    cfg.llm.secondary_fallback_model = ""
    cfg.llm.model_pool = model_pool or []
    return cfg.llm


def _fake_response(model):
    resp = MagicMock()
    resp.content = f"ok-from-{model}"
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = f"ok-from-{model}"
    resp.choices[0].message.tool_calls = None
    resp.choices[0].finish_reason = "stop"
    resp.usage = None
    return resp


def test_model_pool_order_dedup():
    """主模型 + model_pool 去重，构建正确轮换顺序。"""
    cfg = _make_config(model_pool=["mimo:free", "primary-model", "kimi:free"])
    client = LLMClient(cfg)
    assert client.model_pool == ["primary-model", "mimo:free", "kimi:free"]
    assert client._pool_alternates == ["mimo:free", "kimi:free"]


def test_pool_rotation_on_primary_failure(monkeypatch):
    """主模型抛瞬时异常，重试耗尽后轮换到池内第一个备选并成功。"""
    cfg = _make_config(model_pool=["alt-1", "alt-2"], fallback_model="fb",
                       fallback_base_url="https://fb/v1")
    cfg.max_retries = 3  # 显式设定，消除对 config.yaml 默认 max_retries 的隐式依赖
    client = LLMClient(cfg)
    monkeypatch.setattr("src.llm.client.time.sleep", lambda s: None)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        if kwargs["model"] == "primary-model":
            raise RuntimeError("429 rate_limit_exceeded")
        return _fake_response(kwargs["model"])

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "ok-from-alt-1"
    # primary 重试 max_retries(3) 次全部失败，随后轮换到 alt-1 成功
    assert calls["model"] == ["primary-model", "primary-model", "primary-model", "alt-1"]


def test_auth_error_skips_retry_and_rotates(monkeypatch):
    """鉴权错误（不可重试）不浪费重试，直接跳到下一模型。"""
    cfg = _make_config(model_pool=["alt-1"], fallback_model="fb",
                       fallback_base_url="https://fb/v1")
    client = LLMClient(cfg)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        # 两类模型都报鉴权错误（同 api_key 全池无效）
        raise RuntimeError("401 authentication failed")

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)

    with pytest.raises(RuntimeError):
        client.chat([{"role": "user", "content": "hi"}])
    # primary + alt-1 各被调用一次（鉴权错误不可重试，无重试），随后降级到 fb（单备用模型）也鉴权失败终止
    assert calls["model"] == ["primary-model", "alt-1", "fb"]


def test_pool_exhausted_then_fallback(monkeypatch):
    """池内全部失败，降级到跨服务商 fallback 模型成功。"""
    cfg = _make_config(model_pool=["alt-1"], fallback_model="fb-model",
                       fallback_base_url="https://fb/v1")
    client = LLMClient(cfg)
    monkeypatch.setattr("src.llm.client.time.sleep", lambda s: None)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        if kwargs["model"] == "fb-model":
            return _fake_response(kwargs["model"])
        raise RuntimeError("429 rate_limit_exceeded")

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)

    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "ok-from-fb-model"
    # 调用序列：primary(重试) -> alt-1(重试) -> fb-model(fallback 成功)
    assert "fb-model" in calls["model"]
    assert calls["model"].count("fb-model") == 1


def test_config_yaml_has_model_pool_field():
    """config.yaml 的 llm 段可正常解析，model_pool 缺省为空列表。"""
    cfg = load_config("config.yaml")
    assert isinstance(cfg.llm.model_pool, list)


def test_fallback_pool_rotation(monkeypatch):
    """主池全部失败后，跨服务商备用池按列表顺序轮换（而非单模型）。"""
    cfg = _make_config(
        model_pool=["alt-1"],
        fallback_model="",
        fallback_base_url="https://fb/v1",
    )
    cfg.fallback_model_pool = ["fb-a", "fb-b", "fb-c"]
    cfg.fallback_api_key = "fb-key"
    client = LLMClient(cfg)
    monkeypatch.setattr("src.llm.client.time.sleep", lambda s: None)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        # 主池与备用池前两个都失败，第三个备用成功
        if kwargs["model"] in ("primary-model", "alt-1", "fb-a", "fb-b"):
            raise RuntimeError("429 rate_limit_exceeded")
        return _fake_response(kwargs["model"])

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "ok-from-fb-c"
    # 调用序：primary(重试) -> alt-1(重试) -> fb-a -> fb-b -> fb-c(成功)
    assert calls["model"].count("fb-a") == 1
    assert calls["model"].count("fb-b") == 1
    assert calls["model"].count("fb-c") == 1


def test_fallback_auth_error_continues_through_pool(monkeypatch):
    """备用池遇 401/403 鉴权错误：不重试该模型，但继续尝试池中其余模型直至耗尽。

    设计意图（见 client._is_retryable_error 与 fallback 分支日志「尝试下一备用」）：
    鉴权错误属不可重试故障，不应浪费重试；但跨模型/跨服务商的 failover 韧性要求
    继续尝试下一个备用模型。同一备用池内 fb-a、fb-b 共享凭据会一同失败，故最终
    抛错；契约是「贯穿整个池」而非「遇鉴权即整体终止」。
    """
    cfg = _make_config(model_pool=[], fallback_base_url="https://fb/v1")
    cfg.fallback_model_pool = ["fb-a", "fb-b"]
    cfg.fallback_api_key = "fb-key"
    client = LLMClient(cfg)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        raise RuntimeError("401 authentication failed")

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)
    with pytest.raises(RuntimeError):
        client.chat([{"role": "user", "content": "hi"}])
    # primary 调用一次（鉴权错误跳过重试），fb-a、fb-b 各一次（继续尝试直至池耗尽）
    assert calls["model"] == ["primary-model", "fb-a", "fb-b"]


def test_fallback_pool_falls_back_to_single_model(monkeypatch):
    """备用池为空时，回退到单 fallback_model（旧字段兼容）。"""
    cfg = _make_config(model_pool=[], fallback_model="fb-single",
                       fallback_base_url="https://fb/v1")
    cfg.fallback_model_pool = []
    client = LLMClient(cfg)
    monkeypatch.setattr("src.llm.client.time.sleep", lambda s: None)

    calls = {"model": []}

    def fake_do_chat(inner_client, kwargs, stream=False, **_kw):
        calls["model"].append(kwargs["model"])
        if kwargs["model"] == "fb-single":
            return _fake_response(kwargs["model"])
        raise RuntimeError("429 rate_limit_exceeded")

    monkeypatch.setattr(client, "_do_chat", fake_do_chat)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.content == "ok-from-fb-single"
    assert "fb-single" in calls["model"]
