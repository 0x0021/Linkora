"""账号身份键的确定性回归测试。

背景（2026-10-05 CI 红）：`resolve_account_id` 会**探测外部 CLI**（dws / lark-cli）
取真实 corpId。会话库物理路径是 ``<platform>__<sha256(account_id)[:16]>.db``，
于是库名随环境漂移——本机没装 dws 得到 "dingtalk:unknown"，CI 装了 dws 得到
"dingtalk:<真实corpId>"，同一份代码在两侧跑出不同的库文件。

后果是「本地全绿、CI 红」，且失败点看起来与身份探测毫无关系：
`tests/test_display_summary_scheduler.py` 两条用例报
「有新消息应重摘要」/ `assert row is not None`，日志里只有一行
「[账号隔离] 已从主库迁移 dingtalk 平台会话数据」。

修法是 `LINKORA_FORCE_ACCOUNT_ID` 环境变量（见 resolve_account_id），
本组用例锁住该开关的行为契约。
"""

from __future__ import annotations

import pytest

from src.memory import account_identity


class TestForcedAccountId:
    def test_forces_deterministic_key(self, monkeypatch):
        """设置环境变量后，各平台身份键必须稳定且带平台前缀。"""
        monkeypatch.setenv("LINKORA_FORCE_ACCOUNT_ID", "ci-test")
        account_identity.invalidate_cache()
        assert account_identity.resolve_account_id("dingtalk") == "dingtalk:ci-test"
        account_identity.invalidate_cache()
        assert account_identity.resolve_account_id("feishu") == "feishu:ci-test"
        account_identity.invalidate_cache()
        assert account_identity.resolve_account_id("wecom") == "wecom:ci-test"

    def test_force_bypasses_cli_probe(self, monkeypatch):
        """强制模式下不得调用任何外部 CLI 探测（那是环境漂移的根源）。"""
        called: list[str] = []

        def _boom(*_a, **_k):
            called.append("probed")
            raise AssertionError("强制模式下不应探测外部 CLI")

        monkeypatch.setenv("LINKORA_FORCE_ACCOUNT_ID", "ci-test")
        monkeypatch.setattr(account_identity, "_resolve_dingtalk", _boom)
        monkeypatch.setattr(account_identity, "_resolve_feishu", _boom)
        monkeypatch.setattr(account_identity, "_resolve_wecom", _boom)
        account_identity.invalidate_cache()
        for plat in ("dingtalk", "feishu", "wecom"):
            account_identity.resolve_account_id(plat)
        assert called == [], "强制模式仍探测了外部 CLI"

    def test_force_skips_cache_and_is_repeatable(self, monkeypatch):
        """强制值直接返回、不进缓存，故重复调用结果恒定（换账号无需失效缓存）。"""
        monkeypatch.setenv("LINKORA_FORCE_ACCOUNT_ID", "ci-test")
        account_identity.invalidate_cache()
        first = account_identity.resolve_account_id("dingtalk")
        second = account_identity.resolve_account_id("dingtalk")
        assert first == second == "dingtalk:ci-test"

    def test_value_with_platform_prefix_respected(self, monkeypatch):
        """值里已含 "平台:" 前缀时按原样使用（便于精确指定命名空间）。"""
        monkeypatch.setenv("LINKORA_FORCE_ACCOUNT_ID", "dingtalk:customcorp")
        account_identity.invalidate_cache()
        assert account_identity.resolve_account_id("dingtalk") == "dingtalk:customcorp"

    def test_blank_value_falls_back_to_probe(self, monkeypatch):
        """空/空白值视为未设置，走原探测逻辑（不得把环境变量当成万能禁用键）。"""
        monkeypatch.setenv("LINKORA_FORCE_ACCOUNT_ID", "   ")
        account_identity.invalidate_cache()
        # 未设置时至少应返回带命名空间的稳定兜底键，不抛异常
        assert account_identity.resolve_account_id("unknownplat").startswith("unknownplat")

    def test_unset_env_uses_normal_probe(self, monkeypatch):
        """未设置环境变量时保持原有行为（生产路径不受影响）。"""
        monkeypatch.delenv("LINKORA_FORCE_ACCOUNT_ID", raising=False)
        account_identity.invalidate_cache()
        try:
            key = account_identity.resolve_account_id("dingtalk")
        except Exception:  # noqa: BLE001 — 探测可能因本机无 CLI 异常，行为不变即可
            pytest.skip("本机环境探测异常，跳过")
        assert isinstance(key, str) and key


class TestEnvVarName:
    def test_constant_matches_env(self):
        """环境变量名与模块常量一致（改常量须同步改 ci.yml 的 env）。"""
        assert account_identity._FORCE_ID_ENV == "LINKORA_FORCE_ACCOUNT_ID"
