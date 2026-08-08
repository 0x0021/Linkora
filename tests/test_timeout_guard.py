"""P0-2026-08-08: 超时保护执行器 run_with_timeout + 调用点行为测试。

验证 ``future.result(timeout=N)`` 的超时保护在「显式 executor +
shutdown(wait=False, cancel_futures=True)」下是真实硬中断（不阻塞调用方），
且 executor 资源被释放。这是修复 `with ThreadPoolExecutor` 块里
``shutdown(wait=True)`` 让超时保护形同虚设（已复现）的核心回归。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.platform._timeout import run_with_timeout


def _slow_fn(duration: float, result=None, exc=None):
    def _fn():
        if exc is not None:
            raise exc
        time.sleep(duration)
        return result
    return _fn


class TestRunWithTimeout:
    def test_returns_result_on_success(self):
        result, timed_out, raised = run_with_timeout(lambda: "ok", timeout=5.0)
        assert result == "ok"
        assert timed_out is False
        assert raised is False

    def test_timeout_does_not_block_caller(self):
        # worker 睡 1s，远超时 0.05s；调用方必须在 ~0.05s 内返回，证明非阻塞。
        start = time.monotonic()
        result, timed_out, raised = run_with_timeout(
            _slow_fn(1.0, result="never"), timeout=0.05)
        elapsed = time.monotonic() - start
        assert timed_out is True
        assert raised is False
        assert result is None
        assert elapsed < 1.0, f"调用方被阻塞 {elapsed:.2f}s，超时保护失效"

    def test_shutdown_called_nonblocking(self):
        # 直接断言 executor.shutdown 以 wait=False / cancel_futures=True 调用，
        # 这是 P0-1 修复的核心契约（与 `with` 块里 shutdown(wait=True) 相对）。
        import concurrent.futures as cf
        real_tpe = cf.ThreadPoolExecutor
        calls = []
        captured = {}

        class _FakeEx:
            def __init__(self, *a, **k):
                self._real = real_tpe(*a, **k)
                captured["ex"] = self._real
            def submit(self, fn, *a, **k):
                return self._real.submit(fn, *a, **k)
            def shutdown(self, *a, **k):
                calls.append({"wait": k.get("wait"), "cancel_futures": k.get("cancel_futures")})
                return self._real.shutdown(*a, **k)

        with patch("concurrent.futures.ThreadPoolExecutor", _FakeEx):
            run_with_timeout(lambda: time.sleep(1.0), timeout=0.05)
        assert len(calls) == 1
        assert calls[0]["wait"] is False
        assert calls[0]["cancel_futures"] is True

    def test_exception_returns_error_value(self):
        result, timed_out, raised = run_with_timeout(
            _slow_fn(0.0, exc=RuntimeError("boom")), timeout=5.0, error_value="ERR")
        assert raised is True
        assert timed_out is False
        assert result == "ERR"


# ── 调用点：runtime_setup._resolve_own_open_dingtalk_id ──

class TestResolveOwnOpenDingTalkIdTimeout:
    def _make_harness(self):
        from src.platform.runtime_setup import SetupMixin

        class _Harness(SetupMixin):
            pass

        h = _Harness()
        h.dws = MagicMock()
        h.current_user_id = "u-1"
        h.current_user_name = "name"
        h.current_open_dingtalk_id = ""
        return h

    def test_timeout_returns_none_without_blocking(self, monkeypatch):
        from src.platform import runtime_setup

        h = self._make_harness()

        def _slow(*a, **k):
            time.sleep(1.0)
            return {}

        h.dws.chat_message_list_all.side_effect = _slow
        monkeypatch.setattr(runtime_setup, "OPEN_DINGTALK_ID_RESOLVE_TIMEOUT", 0.05)
        start = time.monotonic()
        result = h._resolve_own_open_dingtalk_id()
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 1.0, f"调用方被阻塞 {elapsed:.2f}s，超时保护失效"

    def test_success_returns_oid(self, monkeypatch):
        from src.platform import runtime_setup

        h = self._make_harness()
        h.dws.chat_message_list_all.return_value = {
            "conversationMessagesList": [{
                "messages": [{
                    "senderId": "u-1",
                    "senderOpenDingTalkId": "OID-XYZ",
                }]
            }]
        }
        monkeypatch.setattr(runtime_setup, "OPEN_DINGTALK_ID_RESOLVE_TIMEOUT", 5.0)
        assert h._resolve_own_open_dingtalk_id() == "OID-XYZ"


# ── 调用点：primary._init_primary_components（DB 初始化超时） ──

class TestPrimaryDbInitTimeout:
    def test_timeout_resets_schema_initialized(self, monkeypatch, tmp_path):
        import shutil as _shutil

        from src.config import load_config
        from src.memory import store_factory
        from src.platform import primary as primary_mod

        # 用 __new__ 绕过完整 __init__，手工注入 _init_primary_components 所需最小状态
        primary = primary_mod.PrimaryMixin.__new__(primary_mod.PrimaryMixin)
        cfg_dst = tmp_path / "config.yaml"
        _shutil.copy("config.yaml.example", cfg_dst)
        primary.config = load_config(str(cfg_dst))
        primary.platforms = {}
        primary.config_path = str(cfg_dst)

        class _FakeStore:
            _schema_initialized = True

            def init_db(self):
                pass

            def set_decisions_retention_days(self, days):
                pass

        # get_store 在方法内以 `from ... import get_store` 形式于调用时绑定，
        # 故在调用前 patch 模块属性即可生效。
        monkeypatch.setattr(store_factory, "get_store", lambda path: _FakeStore())
        # 跳过真实 DWS 构造
        primary._build_dws = lambda: MagicMock()
        # 避免污染全局决策追踪器单例
        monkeypatch.setattr(primary_mod.tracker, "set_sqlite_store", lambda store: None)

        # 模拟 run_with_timeout 报告超时（不真的等满 30s）
        monkeypatch.setattr(
            primary_mod, "run_with_timeout",
            lambda *a, **k: (None, True, False))

        primary._init_primary_components()

        assert primary.platforms["dingtalk"].store._schema_initialized is False
