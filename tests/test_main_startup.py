"""主入口启动集成测试。

覆盖：main.py re-export 的完整性、main() 最小启动流程。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


class TestMainReexports:
    """验证 main.py 所有 re-export 符号均可访问。"""

    def test_core_class_reachable(self):
        import main
        assert main.LinkoraEngine is not None
        assert main.PlatformContext is not None

    def test_utility_symbols_reachable(self):
        import main
        assert main.load_config is not None
        assert main.tracker is not None
        assert main.SQLiteStore is not None
        assert main.Message is not None

    def test_message_poller_reachable(self):
        import main
        assert main.MessagePoller is not None

    def test_stdlib_reachable(self):
        import main
        assert main.os is not None
        assert main.signal is not None
        assert main.threading is not None
        assert main.time is not None

    def test_rule_engine_reachable(self):
        import main
        assert main.RuleEngine is not None

    def test_llm_agent_reachable(self):
        import main
        assert main.LLMAgent is not None

    def test_embedding_client_reachable(self):
        import main
        assert main.EmbeddingClient is not None

    def test_tools_reachable(self):
        import main
        assert main.ToolRouter is not None


class TestMainEntry:
    """测试 main() 入口函数的最小启动流程。"""

    @patch("sys.argv", ["main.py", "--dev", "config.yaml"])
    @patch("os.path.exists", return_value=False)  # 跳过 PID 文件检查
    @patch("src.platform.core.LinkoraEngine")
    @patch("src.platform.lifecycle.load_config")
    def test_main_minimal_start(self, mock_load, mock_ai, mock_exists):
        mock_config = MagicMock()
        mock_config.web.port = 8888
        mock_config.poller.enabled = False
        mock_config.pollers = {}
        mock_load.return_value = mock_config
        mock_ai.return_value.run = MagicMock(side_effect=SystemExit(0))

        from src.platform.lifecycle import main
        with pytest.raises(SystemExit, match="0"):
            main("/tmp/fakeroot")

    @patch("sys.argv", ["main.py", "config.yaml"])
    @patch("src.platform.lifecycle.load_config")
    def test_main_fatal_on_bad_config(self, mock_load):
        mock_load.side_effect = ValueError("bad config")

        from src.platform.lifecycle import main
        with pytest.raises(SystemExit, match="1"):
            main("/tmp/fake_project")
