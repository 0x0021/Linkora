"""LAST_RATE_LIMIT_TS 并发写锁测试。

护栏 P0-3 伴生修复：多线程并发调 mark_rate_limited 不应崩/死锁，
时间戳应至少更新到最后一次写入时刻附近。
"""
from __future__ import annotations

import threading

from src.llm.client import mark_rate_limited, seconds_since_rate_limit


def test_concurrent_mark_rate_limited_does_not_deadlock():
    """20 线程并发 mark_rate_limited，应在合理时间内全部返回。"""
    errors = []
    def worker():
        try:
            for _ in range(50):
                mark_rate_limited()
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    for t in threads:
        assert not t.is_alive(), "存在死锁或卡死线程"
    assert not errors, f"并发 mark_rate_limited 抛错: {errors}"
    # 时间戳应非常新（< 1 秒）
    assert seconds_since_rate_limit() < 1.0


def test_mark_rate_limited_updates_timestamp():
    """单线程顺序写入后，秒数应 < 1。"""
    import time
    mark_rate_limited()
    time.sleep(0.05)
    assert seconds_since_rate_limit() < 1.0


def test_bg_throttle_blocks_during_rate_limit_backoff():
    """主模型限流(429)后，后台 LLM 节流器 acquire() 须返回 False（退避生效）。

    这守护了「记忆提取/摘要」路径：429 风暴期间后台任务应暂停，不再轰炸免费额度。
    用最小间隔=0 隔离出「仅退避」语义，避免 min_interval 干扰判定。
    """
    from types import SimpleNamespace

    from main import BackgroundLLMThrottle
    from src.llm.client import _llm_state

    # 隔离：重置进程级限流时间戳（避免同会话其它用例已触发 mark_rate_limited 干扰判定）
    _llm_state.last_rate_limit_ts = 0.0

    cfg = SimpleNamespace(
        enabled=True,
        background_min_interval_seconds=0,   # 隔离：min_interval 不拦截
        idle_min_interval_seconds=0,
        idle_threshold_seconds=9999,
        rate_limit_backoff_seconds=600,
    )
    throttle = BackgroundLLMThrottle(cfg)

    # 退避期外：首次获取应成功
    assert throttle.acquire() is True

    # 触发主模型限流信号
    mark_rate_limited()
    # 退避窗口内（600s）：acquire 必须拒绝，后台任务应跳过
    assert throttle.acquire() is False
    # 退避剩余秒数应 > 0
    assert (cfg.rate_limit_backoff_seconds - seconds_since_rate_limit()) > 0


def test_mark_rate_limited_fires_on_observed_429_even_if_retry_succeeds():
    """观测到 429 即标记信号，即便后续重试成功（主模型429但同池备选救回）。

    此前 mark_rate_limited 仅在「某模型重试耗尽」才触发，会漏掉
    「主模型429→同池免费备选成功返回」的情形：用户拿到回复，但后台
    摘要/记忆任务仍会撞同一免费额度。本用例验证该缺口已修复。
    """
    from types import SimpleNamespace

    from src.llm.client import LLMClient, LLMResponse

    # 隔离：重置进程级限流时间戳
    from src.llm.client import _llm_state
    _llm_state.last_rate_limit_ts = 0.0

    cfg = SimpleNamespace(
        api_key="dummy",
        base_url="https://api.openai.com/v1",
        timeout=30,
        max_retries=2,
        base_backoff=0.05,
        model="free-model",
        temperature=0.7,
        max_tokens=512,
        model_pool=[],
        fallback_model=None,
        fallback_model_pool=["fb-model"],
        fallback_base_url="https://api.openai.com/v1",
        fallback_api_key="dummy",
    )
    client = LLMClient(cfg)

    # 第一次调用模拟 429 限流，第二次（重试）成功返回
    calls = {"n": 0}

    def fake_do_chat(c, kwargs, stream=False, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Error: rate_limit_exceeded (429) please retry later")
        return LLMResponse(content="ok", tool_calls=[], finish_reason="stop", usage={})

    # 实例属性函数不会被绑定 self，故签名仅需 (client, kwargs)
    client._do_chat = fake_do_chat

    resp = client.chat([{"role": "system", "content": "hi"}])
    # 重试成功，chat 返回正常内容
    assert resp.content == "ok"
    assert calls["n"] == 2
    # 关键：观测到 429 的那一刻已标记信号，而非等重试耗尽
    assert seconds_since_rate_limit() < 1.0
