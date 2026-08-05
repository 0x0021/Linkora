"""自动为外部 Skill 生成 Tool 包装器。

当技能没有声明显式的 allowed_tools（即无人手动为其编写 Tool 包装器）时，
从 SKILL.md 正文中自动解析 CLI 入口命令，包装为标准 BaseTool。

工作原理：
1. 从 SKILL.md 的代码块中提取 CLI 命令模板（如 python scripts/search.py "查询词"）
2. 将模板包装为 BaseTool，工具名 = 技能名（连字符转下划线）
3. 注册到 ToolRouter，LLM 即可通过标准 tool_call 调用技能
4. execute() 在技能目录下执行 CLI 命令，捕获 stdout 返回
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from src.skills.loader import Skill
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class SkillTool(BaseTool):
    """将技能的 CLI 入口包装为标准 Tool，使得 LLM 可直接通过 tool_call 调用。

    SKILL.md 中的使用方式（如 python scripts/search.py "查询词"）被解析为
    命令模板，execute() 时用 args["query"] 替换占位参数后执行。
    """

    # 兜底超时（秒），防止某些脚本卡死阻塞整个工具路由
    _DEFAULT_TIMEOUT = 60
    # 返回结果最大字符数，防止巨型输出撑爆 LLM 上下文
    _MAX_OUTPUT_CHARS = 10000

    # ── 子进程环境变量沙箱（F26：收窄技能脚本的凭证泄露面）─────────────
    # 变量名（大写）含以下任一子串即视为密钥/凭证，执行技能脚本时**不继承**，
    # 防止技能脚本（可能来自三方/用户自写）读取并外泄主进程密钥。
    _SECRET_ENV_HINTS = (
        "KEY", "SECRET", "TOKEN", "PASSWORD", "PWD", "PASSWD",
        "API_", "AUTH", "DINGTALK", "OPENAI", "DB", "DATABASE",
        "CREDENTIAL", "PRIVATE", "CERT", "WEBHOOK", "COOKIE",
        "SESSION", "SIGN", "X_API",
    )

    def __init__(self, skill: Skill):
        self._skill = skill
        self._skill_dir = Path(skill.source_path).parent

        # 工具名 = 技能名（连字符 → 下划线，符合 LLM function name 规范）
        tool_name = skill.name.replace("-", "_")

        # BaseTool 字段
        self.name = tool_name
        self.description = skill.description
        self.display_name = skill.name
        self.short_description = skill.description[:50]
        self.intent_keywords = list(skill.intent_keywords) if skill.intent_keywords else []
        self.intent_categories = list(skill.intent_categories) if skill.intent_categories else []
        self.platforms = list(skill.platforms) if skill.platforms else []

        # 解析 CLI 入口
        self._cli_template = self._extract_cli_template(skill.body)
        self._fallback_script = self._find_entry_script()

        if self._cli_template:
            logger.debug("[SkillTool] %s: CLI 模板 = %s", skill.name, self._cli_template)
        elif self._fallback_script:
            logger.debug("[SkillTool] %s: 兜底脚本 = %s", skill.name, self._fallback_script)
        else:
            logger.info("[SkillTool] %s: 纯 Prompt 技能（无 CLI 入口），不注册为 Tool", skill.name)

    @property
    def has_cli_entry(self) -> bool:
        """是否具备可执行的 CLI 入口（模板或脚本）。"""
        return bool(self._cli_template or self._fallback_script)

    # ── BaseTool 抽象方法 ──────────────────────────────────────

    @property
    def parameters(self) -> dict:
        """返回 OpenAI function calling 参数 schema。

        当前所有自动包装技能统一使用 query 参数；
        若技能 SKILL.md 未来声明了更丰富的参数说明，可由此扩展。
        """
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": self.description[:100],
                },
            },
            "required": ["query"],
        }

    def execute(self, args: dict) -> str | dict:
        """在技能目录下执行 CLI 命令并返回结果。"""
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "缺少 query 参数"}

        # 1. 优先使用从 SKILL.md 解析的 CLI 模板
        if self._cli_template:
            cmd = self._build_command(query)
        # 2. 兜底：直接在技能目录下执行找到的入口脚本
        elif self._fallback_script:
            cmd = [sys.executable, str(self._fallback_script), query]
        else:
            return {
                "error": (
                    f"技能 '{self._skill.name}' 未声明 CLI 入口（SKILL.md 中无代码块命令，"
                    f"且目录下无 .py/.sh 脚本），无法自动执行。"
                    f"{' 可尝试回退工具：' + ', '.join(self._skill.fallback_tools) if self._skill.fallback_tools else ''}"
                )
            }

        logger.info("[SkillTool] %s 执行: %s", self._skill.name, " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._DEFAULT_TIMEOUT,
                cwd=str(self._skill_dir),
                env=self._build_safe_env(),
            )
        except subprocess.TimeoutExpired:
            fb = self._skill.fallback_tools
            hint = f" 可尝试回退工具：{', '.join(fb)}" if fb else ""
            return {"error": f"技能 '{self._skill.name}' 执行超时（{self._DEFAULT_TIMEOUT}s）。{hint}"}
        except FileNotFoundError as e:
            fb = self._skill.fallback_tools
            hint = f" 可尝试回退工具：{', '.join(fb)}" if fb else ""
            return {"error": f"命令未找到: {e}。{hint}"}
        except Exception as e:
            fb = self._skill.fallback_tools
            hint = f" 可尝试回退工具：{', '.join(fb)}" if fb else ""
            return {"error": f"执行异常: {e}。{hint}"}

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if not err:
                err = f"退出码 {result.returncode}"
            fb = self._skill.fallback_tools
            hint = f" 可尝试回退工具：{', '.join(fb)}" if fb else ""
            logger.warning("[SkillTool] %s 失败 (rc=%d): %s", self._skill.name, result.returncode, err[:200])
            return {"error": f"{err[:500]}。{hint}"}

        output = (result.stdout or "").strip()
        if len(output) > self._MAX_OUTPUT_CHARS:
            output = output[: self._MAX_OUTPUT_CHARS] + "\n\n...（输出已截断）"
        return output

    # ── CLI 模板解析 ─────────────────────────────────────────

    @staticmethod
    def _extract_cli_template(body: str) -> str | None:
        """从 SKILL.md 的 bash/sh/shell 代码块中提取 CLI 命令模板。

        识别规则：
        - 代码块标记为 ```bash、```sh、```shell 或无语言标记
        - 第一行非注释且以 python/python3/uv/npx/node/bash/./ 开头的命令
        - 包含引号包裹的参数（作为 query 占位符）

        示例输入：
            ```bash
            python scripts/search.py "人工智能发展趋势"
            ```
        返回：
            'python scripts/search.py "人工智能发展趋势"'
        """
        if not body:
            return None

        # 提取所有代码块（支持 bash/sh/shell/无语言标记）
        # 兼容未闭合的代码块（SKILL.md 末尾被截断或格式不完整）
        code_blocks = re.findall(
            r"```(?:bash|sh|shell)?\s*\n(.*?)(?:```|$)",
            body,
            re.DOTALL | re.IGNORECASE,
        )
        for block in code_blocks:
            for line in block.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue
                # 匹配常见可执行命令前缀
                if re.match(r"^(python|python3|uv\s+run|npx|node|bash|sh|\.\/)\s+", line):
                    # 拒绝 bash/sh -c 模板：会把 query 作为 shell 命令解释执行，构成命令注入/RCE
                    if re.match(r"^(bash|sh)\s+-c\b", line):
                        logger.warning("[SkillTool] 拒绝 bash/sh -c CLI 模板（命令注入风险）: %s", line[:120])
                        continue
                    # 必须包含引号参数（否则无法确定 query 占位位置）
                    if re.search(r"""["'][^"']*["']""", line):
                        return line
        return None

    def _build_command(self, query: str) -> list[str]:
        """将命令模板中的引号参数替换为实际 query，返回命令列表。

        安全策略：用 shlex.split() 正确分词，避免 query 中的特殊字符
        被 shell 解释（如 $()、反引号等）。

        shlex 失败时退化为简单空格拆分（极端场景兜底）。
        """
        # 纵深防御：任何 bash/sh -c 模板都拒绝执行（query 会被 -c 当作 shell 命令）
        if re.match(r"^(bash|sh)\s+-c\b", self._cli_template):
            raise ValueError("拒绝执行 bash/sh -c 模板：存在命令注入风险")
        # 用 query 替换模板中第一个引号参数
        template = self._cli_template
        replaced = re.sub(
            r"""["']([^"']*)["']""",
            lambda m: shlex.quote(query) if shlex.quote(query) != f"'{query}'" else f'"{query}"',
            template,
            count=1,
        )

        try:
            cmd = shlex.split(replaced)
        except ValueError as _exc:
            logger.warning(f"_build_command: swallowed exception: {_exc}")
            # shlex 解析失败（罕见）：简单空格拆分兜底
            cmd = replaced.split()

        # 将裸 python / python3 解析为当前解释器路径，避免系统中只有 python3 时找不到命令
        if cmd and cmd[0] in ("python", "python3"):
            cmd[0] = sys.executable

        return cmd

    # ── 子进程环境变量沙箱 ─────────────────────────────────────

    @staticmethod
    def _build_safe_env() -> dict[str, str]:
        """构造传给技能子进程的最小安全环境（F26）。

        默认**不整体继承** ``os.environ``——只透传运行必需的无害变量
        （PATH / HOME / 语言 / PYTHONPATH 等），并剥离任何名字含密钥特征的变量
        （KEY / SECRET / TOKEN / PASSWORD / API_ / AUTH / DINGTALK / OPENAI /
        DB / DATABASE / CREDENTIAL …），防止技能脚本读取并外泄主进程凭证。

        设计取舍：采用「密钥名黑名单 + 其余保留」而非纯白名单，是为避免误删
        脚本运行真正需要的变量（如业务配置项）导致技能失灵；关键目标是阻断
        凭证泄露这一高危面。若需更严格沙箱，可后续引入 SkillsConfig.allowed_env
        显式白名单覆盖。
        """
        env: dict[str, str] = {}
        for key, val in os.environ.items():
            uk = key.upper()
            if any(hint in uk for hint in SkillTool._SECRET_ENV_HINTS):
                continue
            env[key] = val
        # 强制无缓冲输出，保证超时/异常时 stderr/stdout 能完整捕获
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ── 入口脚本发现 ────────────────────────────────────────

    def _find_entry_script(self) -> Path | None:
        """当 SKILL.md 无 CLI 模板时，自动查找技能目录下的入口脚本。

        优先级：scripts/ > 根目录，.py > .sh，__init__.py 排除。
        """
        # 优先 scripts/ 子目录
        scripts_dir = self._skill_dir / "scripts"
        for scripts in [scripts_dir, self._skill_dir]:
            if not scripts.is_dir():
                continue
            for f in sorted(scripts.iterdir()):
                if f.suffix == ".py" and f.name != "__init__.py":
                    return f
            for f in sorted(scripts.iterdir()):
                if f.suffix == ".sh":
                    return f
        return None
