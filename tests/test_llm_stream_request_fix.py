"""LLM 流式调用的两个 P0 回归测试（2026-10-04 审计发现）。

**缺陷 1：首次请求不带 stream=True**
`_do_chat_stream` 最初只加 `stream_options` 不加 `stream=True`，服务端返回**非流式**
响应对象，下游按迭代器消费时抛 `'_Resp' object is not iterable`，表现为「回复生成中断」。
降级分支虽写了 `stream=True`，但它只在首次请求抛异常时才到达，而缺 `stream=True`
的首次请求通常不抛异常（直接返回非流式对象）→ 该分支形同虚设。

**缺陷 2：降级逻辑因生成器惰性而是死代码**
`_do_chat_stream` 是生成器函数，调用它**只创建生成器对象、不发请求**。历史代码
`return self._do_chat(...)` 写在 try 里看着能兜住异常，实际网络异常发生在**调用方
迭代生成器时**，早已跳出该 try —— 「流式失败降级为非流式」从未生效过。
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.llm.client import LLMClient, _prepend_chunk  # noqa: E402


def _make_client():
    cfg = load_config("config.yaml")
    cfg.llm.model = "primary-model"
    cfg.llm.base_url = "https://example.com/v1"
    cfg.llm.api_key = "test-key"
    cfg.llm.fallback_model = ""
    cfg.llm.fallback_base_url = ""
    cfg.llm.fallback_model_pool = []
    cfg.llm.secondary_fallback_model_pool = []
    cfg.llm.secondary_fallback_model = ""
    cfg.llm.model_pool = []
    # load_config 返回 AppConfig，LLMClient 构造需要其 llm 段（LlmConfig）
    return LLMClient(cfg.llm)


def _chunk(content: str):
    ch = MagicMock()
    ch.type = "text"
    ch.content = content
    ch.tool_calls = []
    ch.usage = None
    ch.finish_reason = None
    return ch


def _stream_client(chunks):
    """构造一个返回指定 chunk 序列的假 OpenAI client。"""
    fake = MagicMock()
    fake.chat.completions.create.return_value = iter(chunks)
    return fake


class TestStreamTruePresent:
    def test_first_request_includes_stream_true(self):
        """首次请求必须带 stream=True（否则拿到非流式对象，迭代即崩）。"""
        client = _make_client()
        fake = _stream_client([_chunk("a")])
        gen = client._do_chat_stream(fake, {"model": "m", "messages": []})
        list(gen)

        assert fake.chat.completions.create.called
        kwargs = fake.chat.completions.create.call_args.kwargs
        assert kwargs.get("stream") is True, (
            f"首次请求缺 stream=True，实际参数: {sorted(kwargs)}"
        )

    def test_stream_options_retry_keeps_stream_true(self):
        """stream_options 不被网关支持时降级重试，仍必须保留 stream=True。"""
        client = _make_client()
        fake = MagicMock()
        calls = []

        def create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                raise TypeError("stream_options unsupported")
            return iter([_chunk("a")])

        fake.chat.completions.create.side_effect = create
        gen = client._do_chat_stream(fake, {"model": "m", "messages": []})
        list(gen)

        assert len(calls) == 2, "应发生一次降级重试"
        # 关键：降级重试也必须带 stream=True
        assert calls[1].get("stream") is True, (
            f"降级重试丢了 stream=True: {sorted(calls[1])}"
        )
        assert "stream_options" not in calls[1], "降级应去掉 stream_options"


class TestGeneratorEagerness:
    def test_chat_wraps_stream_errors_into_fallback(self):
        """流式建连失败时 chat() 应真正降级为非流式，而不是把异常抛给调用方。

        这是缺陷 2 的核心断言：修复前 `return self._do_chat(...)` 只建生成器、
        不发请求，异常在调用方迭代时才发生 → try/except 形同虚设。
        关键：必须用 stream=True 走**流式分支**，否则测的是非流式路径、证明不了什么。
        """
        client = _make_client()

        # 让第一次（流式）建连直接抛网络错误，第二次（非流式）返回正常响应
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "非流式兜底"
        resp.choices[0].message.tool_calls = None
        resp.choices[0].finish_reason = "stop"
        resp.usage = None

        calls = []

        def create(**kw):
            calls.append(kw)
            if kw.get("stream") is True:
                raise ConnectionError("模拟流式建连失败")
            return resp

        fake = MagicMock()
        fake.chat.completions.create.side_effect = create
        client.client = fake

        # 走流式分支：chat() 内部建连失败 → 应降级 stream=False 并重试成功，
        # 最终返回**非流式响应对象**（不是抛异常，也不是把流生成器泄漏给调用方）
        out = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

        assert not isinstance(out, type(None)), "降级后不应返回 None"
        stream_flags = [kw.get("stream") for kw in calls]
        assert True in stream_flags, f"未曾发起流式请求，实际参数: {stream_flags}"
        assert stream_flags[-1] is not True, "降级后应改为非流式请求"
        # 关键断言：建连失败被 try/except 捕获（异常未泄漏给调用方）
        assert not hasattr(out, "__next__"), "降级失败，流生成器泄漏给了调用方"

    def test_prepend_chunk_restores_first_frame(self):
        """拆开建连/迭代后，已消费的首帧必须无损拼回（不能丢第一个 token）。"""
        rest = iter([_chunk("b"), _chunk("c")])
        out = list(_prepend_chunk(_chunk("a"), rest))
        assert [c.content for c in out] == ["a", "b", "c"]

    def test_prepend_chunk_handles_empty_first(self):
        """首帧为 None（空流）时不应凭空多产出一个 chunk。"""
        rest = iter([_chunk("b")])
        out = list(_prepend_chunk(None, rest))
        assert [c.content for c in out] == ["b"]

    def test_prepend_chunk_with_empty_rest(self):
        """只有首帧的情况（单帧流）也要正确返回。"""
        out = list(_prepend_chunk(_chunk("a"), iter([])))
        assert [c.content for c in out] == ["a"]


def test_concurrency_semaphore_path_also_sets_stream():
    """有并发信号量时（生产路径）同样必须带 stream=True。"""
    client = _make_client()
    client._concurrency_semaphore = threading.Semaphore(1)
    fake = _stream_client([_chunk("a")])
    gen = client._do_chat_stream(fake, {"model": "m", "messages": []})
    list(gen)
    kwargs = fake.chat.completions.create.call_args.kwargs
    assert kwargs.get("stream") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
