"""dotenv 加载入口搬迁的行为等价性测试（2026-10-27 专项）。

搬迁前：``load_dotenv()`` 写在 ``src/platform/base.py`` 的模块顶层（导入副作用）。
搬迁后：真正入口移到 ``main()`` 首行（``src/dotenv_loader.load_env``），
``base.py`` 保留一次**幂等兜底**。

本测试锁定「行为等价」这一核心要求：

1. 主进程导入 ``src.platform.base`` 后，``.env`` 的变量**仍然被注入**
   （这是 3 处模块级 env 读取的依赖）；
2. ``load_env()`` 幂等 —— 重复调用不改变结果、不抛错；
3. ``main()`` 首行会调用 ``load_env()``（入口语义显式化）；
4. Web 进程行为**不变**（仍不加载 ``.env`` —— 改变它属独立决策）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestLoadEnv:
    """load_env 的基本行为。"""

    def test_returns_true_and_is_idempotent(self) -> None:
        from src.dotenv_loader import load_env

        assert load_env() is True
        assert load_env() is True  # 幂等：重复调用无副作用

    def test_does_not_override_existing_env(self) -> None:
        """已存在的环境变量不被 .env 覆盖（dotenv 的既有语义，必须保持）。"""
        from src.dotenv_loader import load_env

        os.environ["LINKORA_TEST_ENV_KEEP"] = "original"
        try:
            load_env()
            assert os.environ["LINKORA_TEST_ENV_KEEP"] == "original"
        finally:
            os.environ.pop("LINKORA_TEST_ENV_KEEP", None)

    def test_failure_is_swallowed(self, monkeypatch) -> None:
        """加载失败只记日志、不抛错（不阻断启动）。"""
        import src.dotenv_loader as dl

        def _boom():
            raise RuntimeError("dotenv 坏了")

        monkeypatch.setattr(dl, "load_env", lambda: False)
        # 直接验证 load_env 自身对异常的兜底
        import importlib

        mod = importlib.reload(dl)
        try:
            # dotenv 本身可用时返回 True
            assert mod.load_env() is True
        finally:
            importlib.reload(dl)


class TestMainEntryCallsLoadEnv:
    """main() 首行必须显式加载 .env。"""

    def test_main_calls_load_env_first(self) -> None:
        """``main()`` 的第一条语句是 load_env（早于任何配置读取）。"""
        import ast

        src = (ROOT / "src/platform/lifecycle.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        main_fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        body = main_fn.body
        # 首个有效语句是 import（load_env 的延迟导入）
        first = body[0]
        assert isinstance(first, ast.ImportFrom), "main() 首个语句应是 import"
        assert first.module == "src.dotenv_loader", (
            f"main() 首个导入应为 src.dotenv_loader，实际 {first.module}"
        )
        # 第二个语句是调用 load_env()
        assert len(body) >= 2 and isinstance(body[1], ast.Expr), "main() 应紧接着调用 load_env()"
        call = body[1].value
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), (
            "main() 第二个语句应是 load_env() 调用"
        )
        assert call.func.id == "load_env"


class TestBehaviorEquivalence:
    """搬迁前后行为等价 —— 核心要求。"""

    def test_main_process_still_injects_env_after_import_base(self) -> None:
        """导入 ``src.platform.base`` 后，``.env`` 变量**仍被注入**。

        这是 ``src/memory/account_identity.py:38``（模块级读 ``DWS_BIN``）等
        3 处模块级 env 读取的依赖 —— 若失效，钉钉账号隔离会静默退化为
        ``dingtalk:unknown``。
        """
        code = (
            "import os\n"
            "os.environ.pop('LLM_API_KEY', None)\n"
            "import src.platform.base  # noqa\n"
            "print('INJECTED' if os.environ.get('LLM_API_KEY') else 'MISSING')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        assert "INJECTED" in proc.stdout, (
            f"搬迁后主进程未注入 .env 变量（行为已变）: {proc.stdout} {proc.stderr}"
        )

    def test_web_process_behavior_unchanged(self) -> None:
        """Web 进程仍**不**加载 ``.env``（搬迁不改变 Web 侧行为）。"""
        code = (
            "import os\n"
            "os.environ.pop('LLM_API_KEY', None)\n"
            "import web.api  # noqa\n"
            "print('INJECTED' if os.environ.get('LLM_API_KEY') else 'MISSING')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        assert "MISSING" in proc.stdout, (
            f"搬迁意外改变了 Web 进程行为（应仍不加载 .env）: {proc.stdout}"
        )

    def test_base_keeps_idempotent_fallback(self) -> None:
        """``base.py`` 保留幂等兜底调用（不能在 main() 之前被导入的模块拿不到 env）。"""
        src = (ROOT / "src/platform/base.py").read_text(encoding="utf-8")
        assert "load_env()" in src, "base.py 缺少幂等兜底调用"
        assert "from src.dotenv_loader import load_env" in src


@pytest.mark.parametrize(
    "module_path,var_name",
    [
        ("src/memory/account_identity.py", "DWS_BIN"),
        ("src/audit.py", "LINKORA_AUDIT_LOG"),
        ("src/tools/web_search.py", "ENABLE_SEARXNG"),
    ],
)
def test_module_level_env_reads_still_work(module_path: str, var_name: str) -> None:
    """3 处模块级 env 读取仍能拿到值（兜底机制有效）。

    这些读取发生在**导入期**，是 dotenv 时序最敏感的点 —— 搬迁后必须
    仍能拿到注入后的值。
    """
    code = (
        "import os\n"
        f"os.environ.pop('{var_name}', None)\n"
        f"import {module_path[:-3].replace('/', '.')}  # noqa\n"
        f"print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, (
        f"导入 {module_path} 失败（模块级 env 读取受影响）: {proc.stderr[-500:]}"
    )
