#!/usr/bin/env python3
"""弱模型泄漏回归护城河运行器。

单独跑 `tests/test_weak_model_regression.py`（标记 weak_model_regression），
作为部署前的质量闸门：任何对 sanitize_reply / gate_reply / system_prompt 的改动，
都必须让本套件全绿，否则视为弱模型泄漏回归。

用法：
    python scripts/run_weak_model_regression.py
    python scripts/run_weak_model_regression.py --cov      # 带覆盖率

退出码：0 = 全过；非 0 = 有回归（可直接接 CI / pre-commit / 部署流水线）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cmd = [
        sys.executable, "-m", "pytest",
        "-m", "weak_model_regression",
        "-v",
        "--tb=short",
        "tests/test_weak_model_regression.py",
    ]
    if "--cov" in sys.argv:
        cmd += ["--cov=src.llm.style", "--cov-report=term-missing"]
    print("▶ 运行弱模型回归护城河:", " ".join(cmd[1:]), "\n")
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode == 0:
        print("\n✅ 弱模型回归护城河通过：弱模型泄漏修复未被破坏。")
    else:
        print("\n❌ 弱模型回归护城河失败：检测到泄漏回归，禁止部署/合并。")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
