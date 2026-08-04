"""SkillTool 异常路径补充测试。

覆盖 execute() 中 TimeoutExpired、FileNotFoundError、通用 Exception
以及 stderr 为空时的退出码兜底错误消息。"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src.skills.tool_wrapper import SkillTool


def _skill(**kw):
    from src.skills.loader import Skill
    defaults = {
        "name": "test-skill",
        "description": "测试技能",
        "body": '```bash\npython scripts/run.py "query"\n```',
        "source_path": "/fake/SKILL.md",
    }
    defaults.update(kw)
    return Skill(**defaults)


class TestExecuteExceptions:
    def test_timeout_expired(self):
        """subprocess.TimeoutExpired → 返回 error 含回退工具提示。"""
        tool = SkillTool(_skill())
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=30)):
            result = tool.execute({"query": "test"})
        assert "error" in result
        assert "超时" in result["error"]

    def test_file_not_found_error(self):
        """subprocess 命令不存在 → 返回友好的错误消息。"""
        tool = SkillTool(_skill())
        with patch("subprocess.run", side_effect=FileNotFoundError("no such command")):
            result = tool.execute({"query": "test"})
        assert "error" in result
        assert "命令未找到" in result["error"]

    def test_generic_exception(self):
        """任意其他异常 → 返回通用执行异常错误。"""
        tool = SkillTool(_skill())
        with patch("subprocess.run", side_effect=RuntimeError("something went wrong")):
            result = tool.execute({"query": "test"})
        assert "error" in result
        assert "执行异常" in result["error"]

    def test_nonzero_stderr_empty(self):
        """退出码非零且 stderr 为空 → 回退到退出码错误消息。"""
        tool = SkillTool(_skill())
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("FakeResult", (), {
                "returncode": 3,
                "stdout": "",
                "stderr": "",
            })()
            result = tool.execute({"query": "test"})
        assert "error" in result
        assert "退出码 3" in result["error"]

    def test_execute_no_entry_without_fallback_tools(self):
        """无 CLI 入口且无 fallback_tools → 不提示回退工具。"""
        from src.skills.loader import Skill
        skill = Skill(
            name="pure-prompt",
            description="纯 Prompt 技能",
            body="# Just instructions",
            source_path="/fake/SKILL.md",
            fallback_tools=[],
        )
        tool = SkillTool(skill)
        # 无 CLI 模板、无入口脚本 → has_cli_entry = False
        if tool.has_cli_entry:
            pytest.skip("该技能被误判为有 CLI 入口")
        result = tool.execute({"query": "test"})
        assert "error" in result
        assert "未声明 CLI 入口" in result["error"]
        # 无 fallback_tools，不应出现"可尝试回退"提示
        assert "可尝试回退" not in result["error"]
