"""管理工具（management）测试——共享 store 单例。

管理工具现已复用注入的 SQLiteStore（非每次新建独立连接），
不再有连接泄漏风险。测试验证共享 store 下各项功能正常。
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.tools.management import (
    ConfigManageTool,
    KeywordRulesTool,
    MessageStatsTool,
    SystemStatusTool,
)


@pytest.fixture
def store():
    """创建独立的 SQLiteStore 实例，测试后清理。"""
    from src.memory.sqlite_store import SQLiteStore
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = SQLiteStore(path)
    s.init_db()
    yield s
    s.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


# ============================================================================
# SystemStatusTool
# ============================================================================
class TestSystemStatusTool:
    def test_execute_returns_status(self, store):
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.return_value = {
            "userName": "测试用户", "corpName": "测试企业",
            "expiresAt": "2026-12-31", "status": "active",
        }
        tool = SystemStatusTool(dws=mock_dws, store=store)
        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent", return_value=12.5), \
             patch("src.tools.management.load_config") as mock_lc:
            mock_vm.return_value = MagicMock(percent=45.2, total=16*1024**3, available=8*1024**3)
            mock_lc.return_value = _fake_config()
            result = tool.execute({})
            assert result["status"]["dws_auth"] == "已配置"
            assert result["status"]["database"] == "正常"
            assert result["system"]["cpu"] == "12.5%"
            assert result["system"]["memory"] == "45.2%"
            assert result["dws"]["user_name"] == "测试用户"

    def test_db_exception_reported(self, store):
        """共享 store 连接异常时捕获并报错，不崩溃。"""
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.return_value = None
        tool = SystemStatusTool(dws=mock_dws, store=store)
        # 关闭连接后触发异常
        store.conn.close()
        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent", return_value=0.0), \
             patch("src.tools.management.load_config") as mock_lc:
            mock_vm.return_value = MagicMock(percent=0, total=1, available=1)
            mock_lc.return_value = _fake_config()
            result = tool.execute({})
            assert "异常" in result["status"]["database"]

    def test_psutil_exception_graceful(self, store):
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.return_value = None
        tool = SystemStatusTool(dws=mock_dws, store=store)
        with patch("psutil.virtual_memory", side_effect=RuntimeError("unavailable")), \
             patch("src.tools.management.load_config") as mock_lc:
            mock_lc.return_value = _fake_config()
            result = tool.execute({})
            assert result["system"]["memory"] == "N/A"

    def test_dws_profile_exception_graceful(self, store):
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.side_effect = RuntimeError("dws error")
        tool = SystemStatusTool(dws=mock_dws, store=store)
        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent", return_value=0.0), \
             patch("src.tools.management.load_config") as mock_lc:
            mock_vm.return_value = MagicMock(percent=0, total=1, available=1)
            mock_lc.return_value = _fake_config()
            result = tool.execute({})
            assert result["dws"]["user_name"] == ""
            assert result["status"]["dws_auth"] == "未知"

    def test_no_config_yaml_graceful(self, store):
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.return_value = None
        tool = SystemStatusTool(dws=mock_dws, store=store, config=None)
        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent", return_value=0.0), \
             patch("src.tools.management.load_config", side_effect=FileNotFoundError("no config")):
            mock_vm.return_value = MagicMock(percent=0, total=1, available=1)
            result = tool.execute({})
            assert "config" in result
            assert result["status"]["tools_enabled"] == "N/A"
            assert result["config"]["poll_interval"] == "N/A"
            assert result["config"]["tools_count"] == "N/A"

    def test_injected_config_used_over_load(self, store):
        mock_dws = MagicMock()
        mock_dws._get_current_profile_local.return_value = None
        fake = _fake_config()
        tool = SystemStatusTool(dws=mock_dws, store=store, config=fake)
        with patch("psutil.virtual_memory") as mock_vm, \
             patch("psutil.cpu_percent", return_value=0.0), \
             patch("src.tools.management.load_config") as mock_lc:
            mock_vm.return_value = MagicMock(percent=0, total=1, available=1)
            result = tool.execute({})
            mock_lc.assert_not_called()
            assert result["status"]["tools_enabled"] is True
            assert result["config"]["tools_count"] == 1


# ============================================================================
# KeywordRulesTool
# ============================================================================
class TestKeywordRulesTool:
    def test_list_returns_rules(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "list"})
        assert "rules" in r

    def test_add_missing_fields_returns_error(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "add", "match_pattern": "", "reply_text": ""})
        assert r.get("error")

    def test_enable_missing_id_returns_error(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "enable"})
        assert r.get("error")

    def test_add_then_list_roundtrip(self, store):
        tool = KeywordRulesTool(store)
        add = tool.execute({"action": "add", "match_pattern": "请假",
                            "reply_text": "请走OA流程", "category": "hr"})
        assert add.get("success") is True
        listed = tool.execute({"action": "list"})
        assert listed["count"] == 1

    def test_add_default_category(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "add", "match_pattern": "打卡", "reply_text": "已记录"})
        assert r.get("success") is True

    def test_disable_success(self, store):
        tool = KeywordRulesTool(store)
        add = tool.execute({"action": "add", "match_pattern": "x", "reply_text": "y"})
        rid = add["rule_id"]
        r = tool.execute({"action": "disable", "rule_id": rid})
        assert r.get("success") is True

    def test_disable_nonexistent(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "disable", "rule_id": 99999})
        assert "未找到" in r.get("error", "")

    def test_unknown_action(self, store):
        tool = KeywordRulesTool(store)
        r = tool.execute({"action": "delete"})
        assert "未知操作" in r.get("error", "")

    def test_db_exception_caught(self, store):
        """store.conn 被关闭后再次调用应被外层 except 捕获。"""
        tool = KeywordRulesTool(store)
        store.conn.close()
        r = tool.execute({"action": "list"})
        assert "error" in r


# ============================================================================
# MessageStatsTool
# ============================================================================
class TestMessageStatsTool:
    def test_stats_returns_counts(self, store):
        tool = MessageStatsTool(store)
        r = tool.execute({"days": 7})
        assert "total_messages" in r

    def test_days_string_no_crash(self, store):
        tool = MessageStatsTool(store)
        r = tool.execute({"days": "七"})
        assert "total_messages" in r

    def test_days_out_of_range_clamped(self, store):
        tool = MessageStatsTool(store)
        r = tool.execute({"days": 100})
        assert r["days"] == 30

    def test_days_zero_clamped(self, store):
        tool = MessageStatsTool(store)
        r = tool.execute({"days": 0})
        assert r["days"] == 1

    def test_db_exception_returns_error(self, store):
        """store.conn 已关闭时应返回 error 而非崩溃。"""
        tool = MessageStatsTool(store)
        # MessageStatsTool 用 conv_conn()（会话库），需关闭会话库连接才能触发异常
        store.conn.close()
        store._conv_conns.clear()
        # 强制让 conv_conn 重新打开已关闭的连接 → 抛 OperationalError
        import sqlite3
        # 通过关闭主库让所有缓存连接失效
        for key in list(store._conv_conns.keys()):
            try:
                store._conv_conns[key][1].close()
            except Exception:
                pass
        store._conv_conns.clear()
        # 触发新连接失败：关闭主库后手动使会话库也失效
        store._closed = True
        r = tool.execute({"days": 7})
        assert "error" in r


# ============================================================================
# ConfigManageTool
# ============================================================================
class TestConfigManageTool:
    def test_view_all(self):
        with patch("src.tools.management.load_config") as mock_lc:
            mock_lc.return_value = _fake_config()
            tool = ConfigManageTool()
            r = tool.execute({"action": "view"})
            assert "dws" in r
            assert "poller" in r
            assert "llm" in r
            assert "tools" in r
            assert "embedding" in r

    def test_view_section(self):
        with patch("src.tools.management.load_config") as mock_lc:
            mock_lc.return_value = _fake_config()
            tool = ConfigManageTool()
            r = tool.execute({"action": "view", "section": "poller"})
            assert r["section"] == "poller"
            assert "interval_seconds" in r["config"]

    def test_view_unknown_section(self):
        with patch("src.tools.management.load_config") as mock_lc:
            mock_lc.return_value = _fake_config()
            tool = ConfigManageTool()
            r = tool.execute({"action": "view", "section": "nonexistent"})
            assert "未知分区" in r.get("error", "")

    def test_update_missing_fields(self):
        tool = ConfigManageTool()
        r = tool.execute({"action": "update"})
        assert "error" in r

    def test_update_success(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "poller": {"interval_seconds": 10, "merge_window_seconds": 3}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({
            "action": "update", "section": "poller",
            "key": "interval_seconds", "value": "30",
        })
        assert r.get("success") is True
        assert r["old_value"] == 10
        assert r["new_value"] == 30

    def test_update_string_value(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "llm": {"model": "gpt-4"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({
            "action": "update", "section": "llm", "key": "model", "value": "gpt-4o",
        })
        assert r.get("success") is True
        assert r["new_value"] == "gpt-4o"

    def test_update_unknown_section(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "poller": {"interval_seconds": 10}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({"action": "update", "section": "nonexistent", "key": "x", "value": "1"})
        assert "未知分区" in r.get("error", "")

    def test_update_unknown_key(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "poller": {"interval_seconds": 10}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({"action": "update", "section": "poller", "key": "no_such_key", "value": "1"})
        assert "未知配置项" in r.get("error", "")

    def test_update_bool_value(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "tools": {"enabled": True}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({"action": "update", "section": "tools", "key": "enabled", "value": "false"})
        assert r.get("success") is True
        assert r["old_value"] is True
        assert r["new_value"] is False

    def test_update_float_value(self, tmp_path, monkeypatch):
        import yaml
        config_data = {"web": {"auth_enabled": False}, "llm": {"temperature": 0.7}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({"action": "update", "section": "llm", "key": "temperature", "value": "0.3"})
        assert r.get("success") is True
        assert r["old_value"] == 0.7
        assert r["new_value"] == 0.3

    def test_update_no_temp_file_leftover(self, tmp_path, monkeypatch):
        import glob
        import yaml

        config_data = {
            "web": {"auth_enabled": False},
            "poller": {"interval_seconds": 10, "merge_window_seconds": 3},
            "llm": {"model": "gpt-4", "temperature": 0.7},
            "tools": {"enabled": True},
        }
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)
        tool = ConfigManageTool()
        r = tool.execute({
            "action": "update", "section": "poller",
            "key": "interval_seconds", "value": "30",
        })
        assert r.get("success") is True
        leftovers = glob.glob(str(tmp_path / ".config.*.tmp"))
        assert leftovers == [], f"原子写遗留临时文件: {leftovers}"
        reloaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert reloaded["poller"]["interval_seconds"] == 30
        assert reloaded["llm"]["model"] == "gpt-4"
        assert reloaded["tools"]["enabled"] is True

    def test_update_atomic_no_corruption_on_replace_failure(self, tmp_path, monkeypatch):
        import os as _os
        import yaml

        config_data = {"poller": {"interval_seconds": 10}, "llm": {"model": "gpt-4"}}
        (tmp_path / "config.yaml").write_text(yaml.dump(config_data))
        monkeypatch.chdir(tmp_path)

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(_os, "replace", _boom)
        tool = ConfigManageTool()
        r = tool.execute({
            "action": "update", "section": "poller",
            "key": "interval_seconds", "value": "30",
        })
        assert "error" in r
        reloaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert reloaded["poller"]["interval_seconds"] == 10
        assert reloaded["llm"]["model"] == "gpt-4"

    def test_get_exception_on_execute(self):
        with patch("src.tools.management.load_config", side_effect=RuntimeError("crash")):
            tool = ConfigManageTool()
            r = tool.execute({"action": "view"})
            assert "error" in r

    def test_unknown_action(self):
        tool = ConfigManageTool()
        r = tool.execute({"action": "destroy"})
        assert "未知操作" in r.get("error", "")


# ============================================================================
# Helpers
# ============================================================================
def _fake_config():
    from src.config import (
        AppConfig, DwsConfig, EmbeddingConfig, LlmConfig, PollerConfig,
        RulesConfig, ToolsConfig,
    )
    return AppConfig(
        dws=DwsConfig(dry_run=True, retries=1, timeout=30),
        poller=PollerConfig(interval_seconds=10, merge_window_seconds=3),
        llm=LlmConfig(model="gpt-4", temperature=0.7, max_tokens=4096, base_url="https://api.openai.com/v1"),
        tools=ToolsConfig(enabled=True, available=["dummy"]),
        embedding=EmbeddingConfig(enabled=True, model="text-embedding", top_k=5),
        rules=RulesConfig(enabled=True),
        web={"auth_enabled": False},
    )
