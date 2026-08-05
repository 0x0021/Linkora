"""src.platform.resilience.init_platform_safe 单测（#4 跨平台降级封装）。"""
from __future__ import annotations

import logging


from src.platform.resilience import init_platform_safe


def _has_resilience_log(records, level_class=logging.ERROR):
    return any(
        logging.ERROR <= r.levelno and "[resilience]" in r.message
        for r in records
    )


def test_build_ok_no_register_returns_true():
    ok = init_platform_safe("feishu", "飞书", build=lambda: object())
    assert ok is True


def test_build_and_register_called_on_success():
    ctx = object()
    registered = []
    ok = init_platform_safe(
        "wecom", "企业微信",
        build=lambda: ctx,
        register=lambda c: registered.append(c),
    )
    assert ok is True
    assert registered == [ctx]


def test_build_raises_is_skipped_and_logs_resilience(caplog):
    register_calls = []

    def boom():
        raise RuntimeError("cli missing")

    with caplog.at_level(logging.ERROR, logger="src.platform.resilience"):
        ok = init_platform_safe(
            "feishu", "飞书",
            build=boom,
            register=lambda c: register_calls.append(c),
        )
    assert ok is False
    assert register_calls == [], "失败时不应执行 register"
    assert _has_resilience_log(caplog.records)
    assert any("飞书" in r.message for r in caplog.records)


def test_register_raises_is_skipped_and_logs_resilience(caplog):
    def boom(ctx):
        raise RuntimeError("tracker wiring failed")

    with caplog.at_level(logging.ERROR, logger="src.platform.resilience"):
        ok = init_platform_safe(
            "wecom", "企业微信",
            build=lambda: object(),
            register=boom,
        )
    assert ok is False
    assert _has_resilience_log(caplog.records)
    assert any("企业微信" in r.message for r in caplog.records)


def test_success_logs_resilience_info(caplog):
    with caplog.at_level(logging.INFO, logger="src.platform.resilience"):
        ok = init_platform_safe("dingtalk", "钉钉", build=lambda: object())
    assert ok is True
    assert any(
        r.levelno == logging.INFO and "[resilience]" in r.message
        for r in caplog.records
    )
