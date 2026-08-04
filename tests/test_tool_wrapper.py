"""SkillTool 自动包装器测试。

覆盖：
- CLI 模板解析（bash/sh/shell/无语言标记/注释过滤/无引号/空输入）
- _build_command（query 替换 + shlex 分词 + 降级兜底）
- _find_entry_script（scripts/ 优先 / .py > .sh / __init__.py 排除）
- SkillTool 初始化 / has_cli_entry / parameters
- execute()（模板执行 / 兜底脚本 / 无 CLI 入口 / 超时 / 错误 / 空 query）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


from src.skills.loader import Skill
from src.skills.tool_wrapper import SkillTool


def _skill(name="test-skill", body="", source_path="/fake/data/skills/test-skill/SKILL.md", **kw):
    """快速构造 Skill 实例。"""
    defaults = {
        "name": name,
        "description": "测试技能描述",
        "body": body,
        "source_path": source_path,
        "fallback_tools": ["web_search"],
    }
    defaults.update(kw)
    return Skill(**defaults)


# ── CLI 模板解析 ──────────────────────────────────────────────

class TestExtractCliTemplate:
    def test_bash_block(self):
        body = '```bash\npython scripts/search.py "查询词"\n```'
        assert SkillTool._extract_cli_template(body) == 'python scripts/search.py "查询词"'

    def test_sh_block(self):
        body = '```sh\npython main.py "输入"\n```'
        assert SkillTool._extract_cli_template(body) == 'python main.py "输入"'

    def test_shell_block(self):
        body = '```shell\npython run.py "args"\n```'
        assert SkillTool._extract_cli_template(body) == 'python run.py "args"'

    def test_no_lang_block(self):
        body = '```\npython script.py "test"\n```'
        assert SkillTool._extract_cli_template(body) == 'python script.py "test"'

    def test_skip_comment_lines(self):
        body = '```bash\n# 这是注释\n// 也是注释\npython tool.py "query"\n```'
        result = SkillTool._extract_cli_template(body)
        assert result == 'python tool.py "query"'

    def test_no_quoted_arg_returns_none(self):
        body = '```bash\npython script.py\n```'
        assert SkillTool._extract_cli_template(body) is None

    def test_only_comments_returns_none(self):
        body = '```bash\n# just a comment\n```'
        assert SkillTool._extract_cli_template(body) is None

    def test_empty_body(self):
        assert SkillTool._extract_cli_template("") is None

    def test_none_body(self):
        assert SkillTool._extract_cli_template(None) is None

    def test_no_code_block(self):
        assert SkillTool._extract_cli_template("just plain text") is None

    def test_unclosed_code_block(self):
        """未闭合代码块（末尾无 ```）仍能提取。"""
        body = '```bash\npython app.py "input"\n'
        assert SkillTool._extract_cli_template(body) == 'python app.py "input"'

    def test_uv_run_command(self):
        body = '```bash\nuv run scripts/main.py "query"\n```'
        assert SkillTool._extract_cli_template(body) == 'uv run scripts/main.py "query"'

    def test_npx_command(self):
        body = '```bash\nnpx tsx scripts/index.ts "query"\n```'
        assert SkillTool._extract_cli_template(body) == 'npx tsx scripts/index.ts "query"'

    def test_node_command(self):
        body = '```bash\nnode scripts/tool.js "query"\n```'
        assert SkillTool._extract_cli_template(body) == 'node scripts/tool.js "query"'

    def test_first_viable_line_picked(self):
        """首条有效的非注释命令被提取。"""
        body = '```bash\npython a.py "a"\npython b.py "b"\n```'
        assert SkillTool._extract_cli_template(body) == 'python a.py "a"'


# ── _build_command ───────────────────────────────────────────

class TestBuildCommand:
    def test_replaces_quoted_arg(self):
        tool = SkillTool(_skill(body='```bash\npython search.py "占位"\n```'))
        cmd = tool._build_command("你好世界")
        assert "你好世界" in cmd
        assert cmd[0] == sys.executable

    def test_shlex_split(self):
        """替换第一个引号参数后经 shlex 正确分词。"""
        tool = SkillTool(_skill(body='```bash\npython run.py "query"\n```'))
        cmd = tool._build_command("hello world")
        assert cmd[0] == sys.executable
        assert cmd[1] == "run.py"
        assert cmd[2] == "hello world"

    def test_python3_resolved_to_executable(self):
        """python3 命令也解析为 sys.executable。"""
        tool = SkillTool(_skill(body='```bash\npython3 run.py "query"\n```'))
        cmd = tool._build_command("hello")
        assert cmd[0] == sys.executable

    def test_fallback_on_shlex_failure(self, monkeypatch):
        """shlex 解析失败时退化为简单空格拆分。"""
        import shlex
        monkeypatch.setattr(shlex, "split", lambda s: (_ for _ in ()).throw(ValueError("bad")))
        tool = SkillTool(_skill(body='```bash\npython script.py "query"\n```'))
        cmd = tool._build_command("hello")
        assert "hello" in cmd


# ── _find_entry_script ───────────────────────────────────────

class TestFindEntryScript:
    def test_py_in_scripts_dir(self):
        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / "scripts"
            scripts.mkdir()
            (scripts / "main.py").write_text("print('hi')")
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            result = tool._find_entry_script()
            assert result is not None
            assert result.name == "main.py"

    def test_py_in_root_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "entry.py").write_text("print('hi')")
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            result = tool._find_entry_script()
            assert result is not None
            assert result.name == "entry.py"

    def test_init_py_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "__init__.py").write_text("")
            (Path(td) / "real.py").write_text("print('ok')")
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            result = tool._find_entry_script()
            assert result.name == "real.py"

    def test_sh_in_scripts_dir(self):
        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / "scripts"
            scripts.mkdir()
            (scripts / "run.sh").write_text("echo hi")
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            result = tool._find_entry_script()
            assert result is not None
            assert result.name == "run.sh"

    def test_py_preferred_over_sh(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "a.sh").write_text("echo")
            (Path(td) / "b.py").write_text("print")
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            result = tool._find_entry_script()
            assert result.name == "b.py"

    def test_no_scripts_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            skill = _skill(source_path=str(Path(td) / "SKILL.md"))
            tool = SkillTool(skill)
            assert tool._find_entry_script() is None


# ── SkillTool 初始化 ────────────────────────────────────────

class TestSkillToolInit:
    def test_name_converts_hyphens_to_underscores(self):
        tool = SkillTool(_skill(name="web-search"))
        assert tool.name == "web_search"

    def test_description_from_skill(self):
        tool = SkillTool(_skill(description="网络搜索助手"))
        assert tool.description == "网络搜索助手"

    def test_short_description_truncated(self):
        tool = SkillTool(_skill(description="A" * 60))
        assert len(tool.short_description) == 50

    def test_intent_keywords_copied(self):
        tool = SkillTool(_skill(intent_keywords=["搜索", "查询"]))
        assert tool.intent_keywords == ["搜索", "查询"]

    def test_intent_categories_copied(self):
        tool = SkillTool(_skill(intent_categories=["domain.search"]))
        assert tool.intent_categories == ["domain.search"]

    def test_display_name_is_skill_name(self):
        tool = SkillTool(_skill(name="my-tool"))
        assert tool.display_name == "my-tool"

    def test_cli_template_from_body(self):
        tool = SkillTool(_skill(
            body='```bash\npython scripts/search.py "关键词"\n```'
        ))
        assert tool._cli_template == 'python scripts/search.py "关键词"'

    def test_no_cli_template_no_fallback(self):
        """纯 Prompt 技能（无 CLI 也无入口脚本）：不 crash。"""
        with tempfile.TemporaryDirectory() as td:
            skill = _skill(
                name="pure-prompt",
                body="# Just instructions",
                source_path=str(Path(td) / "SKILL.md"),
            )
            tool = SkillTool(skill)
            assert tool._cli_template is None
            assert tool._fallback_script is None
            assert not tool.has_cli_entry


# ── has_cli_entry / parameters ───────────────────────────────

def test_has_cli_entry_true_with_template():
    tool = SkillTool(_skill(body='```bash\npython s.py "q"\n```'))
    assert tool.has_cli_entry


def test_has_cli_entry_true_with_fallback():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "main.py").write_text("print('ok')")
        tool = SkillTool(_skill(source_path=str(Path(td) / "SKILL.md")))
        assert tool.has_cli_entry


def test_has_cli_entry_false():
    with tempfile.TemporaryDirectory() as td:
        tool = SkillTool(_skill(source_path=str(Path(td) / "SKILL.md")))
        assert not tool.has_cli_entry


def test_parameters_schema():
    tool = SkillTool(_skill())
    params = tool.parameters
    assert params["type"] == "object"
    assert "query" in params["required"]
    assert "query" in params["properties"]


# ── execute ──────────────────────────────────────────────────

def test_execute_empty_query():
    with tempfile.TemporaryDirectory() as td:
        tool = SkillTool(_skill(source_path=str(Path(td) / "SKILL.md")))
        result = tool.execute({"query": ""})
        assert "error" in result
        assert "缺少 query 参数" in result["error"]


def test_execute_no_query_key():
    with tempfile.TemporaryDirectory() as td:
        tool = SkillTool(_skill(source_path=str(Path(td) / "SKILL.md")))
        result = tool.execute({})
        assert "error" in result


def test_execute_template_success():
    """用模板命令成功执行。"""
    with tempfile.TemporaryDirectory() as td:
        scripts = Path(td) / "scripts"
        scripts.mkdir()
        (scripts / "echo.py").write_text(
            "import sys; print(f'result: {sys.argv[1]}')"
        )
        skill = _skill(
            body=f'```bash\npython {scripts}/echo.py "query"\n```',
            source_path=str(Path(td) / "SKILL.md"),
        )
        tool = SkillTool(skill)
        result = tool.execute({"query": "hello"})
        assert "result: hello" in result


def test_execute_fallback_script():
    """无模板时走 _find_entry_script 兜底。"""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "main.py").write_text(
            "import sys; print(f'out: {sys.argv[1]}')"
        )
        skill = _skill(
            body="# No CLI template here",
            source_path=str(Path(td) / "SKILL.md"),
        )
        tool = SkillTool(skill)
        result = tool.execute({"query": "world"})
        assert "out: world" in result


def test_execute_no_entry_returns_error():
    with tempfile.TemporaryDirectory() as td:
        skill = _skill(
            body="# Pure prompt skill",
            source_path=str(Path(td) / "SKILL.md"),
            fallback_tools=["web_search"],
        )
        tool = SkillTool(skill)
        result = tool.execute({"query": "test"})
        assert "error" in result
        assert "fallback_tools" not in str(result) or "可尝试回退" in result["error"]


def test_execute_command_returns_nonzero():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "fail.py").write_text(
            "import sys; sys.stderr.write('something broke'); sys.exit(2)"
        )
        skill = _skill(
            body=f'```bash\npython {Path(td)}/fail.py "q"\n```',
            source_path=str(Path(td) / "SKILL.md"),
        )
        tool = SkillTool(skill)
        result = tool.execute({"query": "test"})
        assert "error" in result
        assert "something broke" in result["error"] or "退出码 2" in result["error"]


def test_execute_truncates_long_output():
    """超长 stdout 被截断。"""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "big.py").write_text(
            "print('x' * 20000)"
        )
        skill = _skill(
            body=f'```bash\npython {Path(td)}/big.py "q"\n```',
            source_path=str(Path(td) / "SKILL.md"),
        )
        tool = SkillTool(skill)
        result = tool.execute({"query": "test"})
        assert "输出已截断" in result