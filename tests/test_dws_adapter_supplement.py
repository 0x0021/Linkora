"""DWS Adapter 补充测试：文件 I/O、工具函数、合并逻辑、错误路径。

与 test_dws_adapter.py 互补，不重复已有测试。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.dws_adapter import (
    DwsAdapter,
    DwsError,
    DwsPermissionError,
    is_permission_error,
    is_org_config_problem,
)


# ── 工具函数: is_permission_error ───────────────────────────

class TestIsPermissionError:
    def test_token_verified_failed(self):
        assert is_permission_error("TOKEN_VERIFIED_FAILED: auth expired")

    def test_org_not_enabled(self):
        assert is_permission_error("该组织尚未开启 CLI 数据访问权限")

    def test_not_in_conversation(self):
        assert is_permission_error("user is not in conversation")

    def test_agent_code_not_exists(self):
        assert is_permission_error("AGENT_CODE_NOT_EXISTS: session invalid")

    def test_auth_permission_denied(self):
        assert is_permission_error("AUTH_PERMISSION_DENIED: no access")

    def test_normal_error_not_permission(self):
        assert not is_permission_error("timeout after 30s")
        assert not is_permission_error("connection refused")


# ── 工具函数: is_org_config_problem ─────────────────────────

class TestIsOrgConfigProblem:
    def test_org_not_enabled(self):
        assert is_org_config_problem("该组织尚未开启 CLI 数据访问权限")

    def test_agent_code_not_exists(self):
        assert is_org_config_problem("AGENT_CODE_NOT_EXISTS")

    def test_normal_error_not_org_problem(self):
        assert not is_org_config_problem("timeout")
        assert not is_org_config_problem("401 Unauthorized")


# ── 本地 profile 读取 ───────────────────────────────────────

class TestLocalProfiles:
    """测试 _read_local_profiles 和 _get_current_profile_local。"""

    def test_read_empty_when_no_file(self, monkeypatch):
        """profile 文件不存在时返回空 dict。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            adapter = DwsAdapter(dry_run=True)
            result = adapter._read_local_profiles()
            assert result == {}

    def test_read_valid_profiles(self, monkeypatch):
        """正常读取 profiles.json。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            data = {
                "currentProfile": "corp-abc",
                "profiles": [
                    {"corpId": "corp-abc", "name": "我的企业", "orgId": "123"},
                    {"corpId": "corp-xyz", "name": "另一企业"},
                ],
            }
            (Path(td) / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            result = adapter._read_local_profiles()
            assert result == data

    def test_invalid_json_returns_empty(self, monkeypatch):
        """JSON 损坏时返回空 dict。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            (Path(td) / "profiles.json").write_text("not valid json {{{", encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            result = adapter._read_local_profiles()
            assert result == {}

    def test_non_dict_json_returns_empty(self, monkeypatch):
        """profiles.json 是数组而非对象时返回空。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            (Path(td) / "profiles.json").write_text("[1, 2, 3]", encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            result = adapter._read_local_profiles()
            assert result == {}

    def test_get_current_profile_by_current_id(self, monkeypatch):
        """按 currentProfile 匹配。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            data = {
                "currentProfile": "我的企业",
                "profiles": [
                    {"corpId": "corp-abc", "name": "我的企业", "orgId": "123"},
                ],
            }
            (Path(td) / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            profile = adapter._get_current_profile_local()
            assert profile["orgId"] == "123"

    def test_get_current_profile_by_corp_id(self, monkeypatch):
        """按 corpId 匹配。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            data = {
                "currentProfile": "corp-abc",
                "profiles": [
                    {"corpId": "corp-abc", "name": "企业版"},
                ],
            }
            (Path(td) / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            profile = adapter._get_current_profile_local()
            assert profile["name"] == "企业版"

    def test_current_profile_fallback_primary(self, monkeypatch):
        """currentProfile 为空时回退到 primaryProfile。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            data = {
                "primaryProfile": "corp-backup",
                "profiles": [
                    {"corpId": "corp-backup", "name": "备份企业"},
                ],
            }
            (Path(td) / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            profile = adapter._get_current_profile_local()
            assert profile["name"] == "备份企业"

    def test_current_profile_fallback_first(self, monkeypatch):
        """无 current/primary 标记时返回第一个 profile。"""
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            data = {
                "profiles": [
                    {"corpId": "first", "name": "第一企业"},
                    {"corpId": "second", "name": "第二企业"},
                ],
            }
            (Path(td) / "profiles.json").write_text(json.dumps(data), encoding="utf-8")
            adapter = DwsAdapter(dry_run=True)
            profile = adapter._get_current_profile_local()
            assert profile["name"] == "第一企业"

    def test_no_profiles_returns_empty(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("DWS_CONFIG_DIR", td)
            adapter = DwsAdapter(dry_run=True)
            profile = adapter._get_current_profile_local()
            assert profile == {}

    def test_default_dws_config_dir(self):
        """不设 DWS_CONFIG_DIR 时使用默认 ~/.dws。"""
        adapter = DwsAdapter(dry_run=True)
        assert "/.dws" in str(adapter._dws_config_dir)


# ── _is_personal_dingtalk_error ──────────────────────────────

class TestIsPersonalDingtalkError:
    def test_create_app_failed(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._is_personal_dingtalk_error("CREATE_APP_FAILED")

    def test_token_verified_failed(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._is_personal_dingtalk_error("TOKEN_VERIFIED_FAILED")

    def test_org_not_enabled(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._is_personal_dingtalk_error("该组织尚未开启 CLI 数据访问权限")

    def test_agent_code_not_exists(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._is_personal_dingtalk_error("AGENT_CODE_NOT_EXISTS")

    def test_normal_error_not_personal(self):
        adapter = DwsAdapter(dry_run=True)
        assert not adapter._is_personal_dingtalk_error("timeout")


# ── _merge_list_all_conversations ────────────────────────────

class TestMergeConversations:
    def test_merge_empty_into_empty(self):
        merged = {"conversationMessagesList": []}
        DwsAdapter._merge_list_all_conversations(merged, [])
        assert merged["conversationMessagesList"] == []

    def test_merge_new_conversation(self):
        merged = {"conversationMessagesList": []}
        convs = [{
            "openConversationId": "cid1",
            "title": "群聊A",
            "messages": [{"openMessageId": "m1", "content": "hello"}],
        }]
        DwsAdapter._merge_list_all_conversations(merged, convs)
        assert len(merged["conversationMessagesList"]) == 1
        assert merged["conversationMessagesList"][0]["openConversationId"] == "cid1"

    def test_merge_same_conversation_dedup(self):
        merged = {
            "conversationMessagesList": [{
                "openConversationId": "cid1",
                "messages": [{"openMessageId": "m1", "content": "hello"}],
            }],
        }
        convs = [{
            "openConversationId": "cid1",
            "messages": [
                {"openMessageId": "m1", "content": "hello"},  # dup
                {"openMessageId": "m2", "content": "world"},  # new
            ],
        }]
        DwsAdapter._merge_list_all_conversations(merged, convs)
        msgs = merged["conversationMessagesList"][0]["messages"]
        assert len(msgs) == 2  # 去重后 m1 + m2
        ids = {m["openMessageId"] for m in msgs}
        assert ids == {"m1", "m2"}

    def test_merge_multiple_conversations(self):
        merged = {
            "conversationMessagesList": [
                {"openConversationId": "cid1", "messages": [{"openMessageId": "m1"}]},
            ],
        }
        convs = [
            {"openConversationId": "cid1", "messages": [{"openMessageId": "m2"}]},
            {"openConversationId": "cid2", "messages": [{"openMessageId": "m3"}]},
        ]
        DwsAdapter._merge_list_all_conversations(merged, convs)
        assert len(merged["conversationMessagesList"]) == 2

    def test_skip_conv_without_id(self):
        merged = {"conversationMessagesList": []}
        convs = [
            {"openConversationId": "", "messages": [{"openMessageId": "m1"}]},
            {"openConversationId": "cid1", "messages": [{"openMessageId": "m2"}]},
        ]
        DwsAdapter._merge_list_all_conversations(merged, convs)
        assert len(merged["conversationMessagesList"]) == 1
        assert merged["conversationMessagesList"][0]["openConversationId"] == "cid1"

    def test_empty_messages_handled(self):
        merged = {"conversationMessagesList": []}
        convs = [{"openConversationId": "cid1", "messages": None}]
        DwsAdapter._merge_list_all_conversations(merged, convs)
        assert len(merged["conversationMessagesList"]) == 1
        assert merged["conversationMessagesList"][0]["messages"] is None


# ── DwsError 控制流 ──────────────────────────────────────────

class TestDwsErrorRun:
    """run() 中 DwsPermissionError / DwsError 等异常分支。"""

    def test_permission_error_direct_raise(self):
        """DwsPermissionError 不应重试，直接抛出。"""
        adapter = DwsAdapter(cli_path="dws", retries=2)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="TOKEN_VERIFIED_FAILED"
            )
            with pytest.raises(DwsPermissionError):
                adapter.run(["auth", "status"])

    def test_dws_error_direct_raise(self):
        """DwsError（未知错误）不重试直接抛出。"""
        adapter = DwsAdapter(cli_path="dws", retries=2)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="unknown fatal error"
            )
            with pytest.raises(DwsError):
                adapter.run(["some", "cmd"])

    def test_subprocess_exception_not_timeout(self):
        """非 TimeoutExpired 的其他子进程异常直接抛出。"""
        adapter = DwsAdapter(cli_path="dws", retries=2)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("No such file: dws")
            with pytest.raises(OSError):
                adapter.run(["test"])


# ── _get_result ──────────────────────────────────────────────

class TestGetResult:
    def test_extract_result_key(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._get_result({"result": {"x": 1}}) == {"x": 1}

    def test_no_result_key_returns_original(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._get_result({"x": 1}) == {"x": 1}

    def test_non_dict_returns_original(self):
        adapter = DwsAdapter(dry_run=True)
        assert adapter._get_result([1, 2, 3]) == [1, 2, 3]
