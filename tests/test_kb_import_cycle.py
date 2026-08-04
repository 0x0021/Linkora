"""kb↔api 循环导入回归（#7）。

历史上 web/routers/kb.py 顶层 `import web.api as _api`，而 web/api.py 在文件末尾
`from web.routers.kb import router`（router 在 import web.api 之后才定义）。若 kb 先于
api 被导入，api 在 kb 半初始化阶段取 router 会因尚未定义而抛 AttributeError。

修复后 kb.py 改用惰性代理，顶层不再依赖 web.api。本测试用子进程强制「先导入 kb 再导入
api」的顺序，验证：
  1. 导入链路不再抛 AttributeError（循环依赖已消除）；
  2. kb.router 是合法 APIRouter；
  3. _api 惰性代理解析到的符号与实时 web.api 模块一致（尊重 monkeypatch）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _run(code: str) -> subprocess.CompletedProcess:
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(ROOT) + ":" + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(VENV_PY), "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def test_kb_importable_before_api_no_cycle():
    code = (
        "import web.routers.kb as kb\n"
        "assert kb.router is not None\n"
        "import web.api\n"
        "assert kb._api.get_store is web.api.get_store\n"
        "assert kb._api._get_cfg is web.api._get_cfg\n"
        "print('CYCLE_OK')\n"
    )
    res = _run(code)
    assert res.returncode == 0, (
        f"kb 先于 api 导入应成功，但失败:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    )
    assert "CYCLE_OK" in res.stdout


def test_kb_api_proxy_is_lazy_not_module():
    code = (
        "import web.routers.kb as kb\n"
        # 导入 kb 后、显式导入 web.api 之前，web.api 不应被 kb 的导入拉起
        "import sys\n"
        "assert 'web.api' not in sys.modules, 'kb 顶层不应触发 web.api 导入'\n"
        "import web.api\n"
        "assert kb._api.get_store is web.api.get_store\n"
        "print('LAZY_OK')\n"
    )
    res = _run(code)
    assert res.returncode == 0, (
        f"惰性代理应不触发顶层 web.api 导入:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    )
    assert "LAZY_OK" in res.stdout
