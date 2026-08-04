"""DWS Adapter 单元测试。

覆盖核心逻辑：
- 错误分类（可重试 vs 不可重试）
- 重试机制（指数退避）
- dry_run 模式
- JSON 解析容错
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.dws_adapter import (
    DwsAdapter,
    DwsError,
    DwsNonRetryableError,
    DwsRetryableError,
    classify_dws_error,
)


# ============ 错误分类测试 ============

class TestErrorClassification:
    """DWS 错误分类逻辑。"""

    def test_timeout_is_retryable(self):
        """超时错误应归类为可重试。"""
        assert classify_dws_error("timeout after 30s") is DwsRetryableError
        assert classify_dws_error("Connection timed out") is DwsRetryableError

    def test_connection_refused_is_retryable(self):
        """连接拒绝应归类为可重试。"""
        assert classify_dws_error("connection refused") is DwsRetryableError

    def test_503_is_retryable(self):
        """503 服务不可用应归类为可重试。"""
        assert classify_dws_error("503 Service Unavailable") is DwsRetryableError
        assert classify_dws_error("gateway timeout 504") is DwsRetryableError

    def test_auth_failed_is_non_retryable(self):
        """认证失败应归类为不可重试。"""
        assert classify_dws_error("authentication failed") is DwsNonRetryableError
        assert classify_dws_error("401 Unauthorized") is DwsNonRetryableError
        assert classify_dws_error("invalid token expired") is DwsNonRetryableError

    def test_permission_denied_is_non_retryable(self):
        """权限不足应归类为不可重试。"""
        assert classify_dws_error("permission denied 403") is DwsNonRetryableError

    def test_bad_request_is_non_retryable(self):
        """参数错误应归类为不可重试。"""
        assert classify_dws_error("400 Bad Request") is DwsNonRetryableError
        assert classify_dws_error("invalid parameter") is DwsNonRetryableError

    def test_rate_limit_is_non_retryable(self):
        """限流应归类为不可重试。"""
        assert classify_dws_error("429 rate limit exceeded") is DwsNonRetryableError

    def test_unknown_error_defaults_to_non_retryable(self):
        """未知错误默认不可重试。"""
        assert classify_dws_error("some weird error xyz") is DwsError

    def test_case_insensitive_matching(self):
        """错误分类应大小写不敏感。"""
        assert classify_dws_error("TIMEOUT") is DwsRetryableError
        assert classify_dws_error("Authentication Failed") is DwsNonRetryableError


# ============ AI 标记（--ai-tag）开关测试 ============

class TestAiTagFlag:
    """chat_message_send 的 AI 标记开关行为。"""

    def _capture_args(self, adapter, **kwargs):
        captured = {}
        adapter.run = lambda args, **k: captured.setdefault("args", args) or {}
        adapter.chat_message_send(group="g1", title="t", text="hi", **kwargs)
        return captured["args"]

    def test_default_on_appends_ai_tag(self):
        """ai_tag_default=True(默认) 时命令应含 --ai-tag。"""
        adapter = DwsAdapter(dry_run=True, ai_tag_default=True)
        assert "--ai-tag" in self._capture_args(adapter)

    def test_default_off_omits_ai_tag(self):
        """ai_tag_default=False 时命令不应含 --ai-tag。"""
        adapter = DwsAdapter(dry_run=True, ai_tag_default=False)
        assert "--ai-tag" not in self._capture_args(adapter)

    def test_default_value_is_true(self):
        """不传 ai_tag_default 时默认为 True（向后兼容旧行为）。"""
        adapter = DwsAdapter(dry_run=True)
        assert "--ai-tag" in self._capture_args(adapter)

    def test_explicit_arg_overrides_default(self):
        """显式传 ai_tag 参数应覆盖实例默认。"""
        adapter_off = DwsAdapter(dry_run=True, ai_tag_default=False)
        assert "--ai-tag" in self._capture_args(adapter_off, ai_tag=True)
        adapter_on = DwsAdapter(dry_run=True, ai_tag_default=True)
        assert "--ai-tag" not in self._capture_args(adapter_on, ai_tag=False)


# ============ DWS Adapter 基础行为测试 ============

class TestDwsAdapterBasics:
    """DWS Adapter 基本行为。"""

    def test_dry_run_appends_flag(self, mock_dws):
        """dry_run=True 时命令应自动追加 --dry-run。"""
        adapter = DwsAdapter(cli_path="dws", dry_run=True)
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"result": {}}', stderr=""
            )
            adapter.run(["chat", "message", "send"])
            
            cmd = mock_run.call_args[0][0]
            assert "--dry-run" in cmd

    def test_dry_run_false_no_flag(self, mock_dws):
        """dry_run=False 时不应追加 --dry-run。"""
        adapter = DwsAdapter(cli_path="dws", dry_run=False)
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"result": {}}', stderr=""
            )
            adapter.run(["auth", "status"])
            
            cmd = mock_run.call_args[0][0]
            assert "--dry-run" not in cmd

    def test_profile_appended_when_set(self, mock_dws):
        """设置 profile 时应追加 --profile 参数。"""
        adapter = DwsAdapter(cli_path="dws", profile="work-account")
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"result": {}}', stderr=""
            )
            adapter.run(["auth", "status"])
            
            cmd = mock_run.call_args[0][0]
            assert "--profile" in cmd
            assert "work-account" in cmd

    def test_json_parse_success(self, mock_dws):
        """成功响应应正确解析 JSON。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"success": true, "result": {"userId": "123"}}',
                stderr="",
            )
            result = adapter.run(["contact", "user", "get-self"])
            
            assert result == {"success": True, "result": {"userId": "123"}}

    def test_json_parse_invalid_raises(self, mock_dws):
        """无效 JSON 应抛出 DwsError。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="not valid json{{{", stderr=""
            )
            with pytest.raises(DwsError, match="Failed to parse"):
                adapter.run(["auth", "status"])

    def test_nonzero_exit_code_raises(self, mock_dws):
        """非零退出码应抛出对应分类的错误。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="authentication failed"
            )
            with pytest.raises(DwsNonRetryableError):
                adapter.run(["auth", "login"])

    def test_empty_output_returns_empty_dict(self, mock_dws):
        """空输出应返回空字典而非报错。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )
            result = adapter.run(["some", "command"])
            assert result == {}


# ============ 重试机制测试 ============

class TestRetryMechanism:
    """重试逻辑验证。"""

    def test_retryable_error_retries_then_succeeds(self, mock_dws):
        """可重试错误应在重试后成功。"""
        adapter = DwsAdapter(cli_path="dws", retries=2)
        
        call_count = 0
        
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise DwsRetryableError("timeout")
            return MagicMock(returncode=0, stdout='{"result": {}}', stderr="")
        
        with patch("subprocess.run", side_effect=side_effect):
            result = adapter.run(["test", "cmd"])
            assert call_count == 3  # 2次失败 + 1次成功

    def test_non_retryable_error_no_retry(self, mock_dws):
        """不可重试错误应立即抛出，不重试。"""
        adapter = DwsAdapter(cli_path="dws", retries=3)
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="401 Unauthorized"
            )
            with pytest.raises(DwsNonRetryableError):
                adapter.run(["auth", "status"])
            
            # 只调用了一次，没有重试
            assert mock_run.call_count == 1

    def test_all_retries_exhausted_raises_last_error(self, mock_dws):
        """所有重试耗尽后应抛出最后一次错误。"""
        adapter = DwsAdapter(cli_path="dws", retries=2)
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="timeout"
            )
            with pytest.raises(DwsRetryableError):
                adapter.run(["test", "cmd"])
            
            # 初始1次 + 重试2次 = 3次
            assert mock_run.call_count == 3

    def test_timeout_always_retryable(self, mock_dws):
        """超时始终可重试。"""
        adapter = DwsAdapter(cli_path="dws", retries=1, timeout=1)
        
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["dws"], timeout=1)
            
            with pytest.raises(DwsRetryableError, match="timeout"):
                adapter.run(["test", "cmd"])


# ============ 高层方法测试 ============

class TestHighLevelMethods:
    """DWS Adapter 高层封装方法。"""

    def test_contact_user_get_self_extracts_result(self, mock_dws):
        """contact_user_get_self 应提取 result 字段。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch.object(adapter, "run") as mock_run:
            mock_run.return_value = {
                "result": {
                    "orgEmployeeModel": {
                        "userId": "123",
                        "orgUserName": "测试",
                    }
                }
            }
            result = adapter.contact_user_get_self()
            assert result["orgEmployeeModel"]["userId"] == "123"

    def test_chat_message_list_unread_conversations_extracts_list(self, mock_dws):
        """unread conversations 应提取 conversations 列表。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch.object(adapter, "run") as mock_run:
            mock_run.return_value = {
                "result": {
                    "conversations": [
                        {"chatId": "cid1"},
                        {"chatId": "cid2"},
                    ]
                }
            }
            result = adapter.chat_message_list_unread_conversations()
            assert len(result) == 2
            assert result[0]["chatId"] == "cid1"

    def test_todo_task_create_builds_correct_args(self, mock_dws):
        """create_todo 应构建正确的命令行参数。"""
        adapter = DwsAdapter(cli_path="dws")
        
        with patch.object(adapter, "run") as mock_run:
            mock_run.return_value = {"result": {"taskId": "t1"}}
            adapter.todo_task_create(
                title="测试待办",
                executors="user123",
                due="2026-07-08T10:00:00Z",
                priority="high",
            )
            
            args = mock_run.call_args[0][0]
            assert "--title" in args
            assert "测试待办" in args
            assert "--executors" in args
            assert "--due" in args
            assert "--priority" in args


# ============ dry_run 线程安全测试 ============

class TestDryRunThreadSafety:
    """只读查询类方法在 dry_run=True 时必须真实执行，且不得临时修改共享的
    self.dry_run 实例状态（DwsAdapter 单实例被 poller/后台摘要/web 多线程共享，
    临时改实例状态会串线程，可能导致 dry-run 模式下真发消息）。"""

    def _mock_ok(self):
        return MagicMock(returncode=0, stdout='{"result": {}}', stderr="")

    def test_readonly_forces_no_dry_run_flag(self, mock_dws):
        """dry_run=True 时，只读查询方法不应带 --dry-run（强制真实执行）。"""
        adapter = DwsAdapter(cli_path="dws", dry_run=True)
        readonly_calls = [
            lambda: adapter.chat_message_list_unread_conversations(),
            lambda: adapter.chat_list_top_conversations(),
            lambda: adapter.chat_message_list_direct(open_dingtalk_id="oid1"),
            lambda: adapter.chat_message_list(group="g1", time_str="2026-07-10 00:00:00"),
            lambda: adapter.doc_search("q"),
            lambda: adapter.doc_read("node1"),
        ]
        for call in readonly_calls:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = self._mock_ok()
                call()
                cmd = mock_run.call_args[0][0]
                assert "--dry-run" not in cmd, f"只读方法误带 --dry-run: {cmd}"

    def test_readonly_does_not_mutate_instance_dry_run(self, mock_dws):
        """只读方法执行前后，self.dry_run 必须保持不变（不串线程）。"""
        adapter = DwsAdapter(cli_path="dws", dry_run=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._mock_ok()
            assert adapter.dry_run is True
            adapter.chat_list_top_conversations()
            assert adapter.dry_run is True, "只读方法污染了共享的 dry_run 状态"

    def test_write_still_respects_dry_run(self, mock_dws):
        """写操作（如 send）在 dry_run=True 时仍应带 --dry-run。"""
        adapter = DwsAdapter(cli_path="dws", dry_run=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = self._mock_ok()
            adapter.run(["chat", "message", "send"])
            cmd = mock_run.call_args[0][0]
            assert "--dry-run" in cmd

    def test_concurrent_readonly_and_write_no_leak(self, mock_dws):
        """并发跑只读(force_no_dry_run)与写操作，写操作绝不能丢失 --dry-run。

        用单一顶层 patch（patch 本身非线程安全，不能在多线程各自嵌套），
        在 side_effect 里直接校验每次 send 命令都带 --dry-run。"""
        import threading
        adapter = DwsAdapter(cli_path="dws", dry_run=True)
        violations = []

        def fake_run(cmd, **kw):
            import time as _t
            _t.sleep(0.001)  # 放大竞态窗口
            # 写操作（send）在 dry_run=True 下必须带 --dry-run
            if "send" in cmd and "--dry-run" not in cmd:
                violations.append(list(cmd))
            return self._mock_ok()

        with patch("subprocess.run", side_effect=fake_run):
            def do_reads():
                for _ in range(50):
                    adapter.chat_list_top_conversations()

            def do_writes():
                for _ in range(50):
                    adapter.run(["chat", "message", "send"])

            t1 = threading.Thread(target=do_reads)
            t2 = threading.Thread(target=do_writes)
            t1.start(); t2.start(); t1.join(); t2.join()
        assert not violations, f"dry-run 模式下写操作丢失 --dry-run（串线程）: {violations[:3]}"
