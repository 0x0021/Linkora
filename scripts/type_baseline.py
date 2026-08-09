#!/usr/bin/env python3
"""pyright 类型错误非回归门禁（F9 渐进类型收敛）。

读取 `pyright --outputjson` 产出的 JSON，统计 severity == "error" 的诊断数量，
与锁定的基线 TYPE_ERROR_BASELINE 比较：

- errors <= BASELINE  -> 通过（允许逐步收敛，但不允许回退）。
- errors >  BASELINE  -> 失败（exit 1），阻止新增类型错误合入 main。

基线更新流程（每次收敛一批后）：
  1. 在源码上修复一批类型错误；
  2. 本地跑 `pyright --outputjson > r.json`；
  3. 把本脚本的 TYPE_ERROR_BASELINE 改为 r.json 中的 error 数；
  4. 提交，CI 门禁随之收紧（后续任何新增错误都会 fail）。

用法：python scripts/type_baseline.py <pyright-output.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 锁定基线：在 main（Python 3.14.6）实测 src+web 的 pyright error 数。
# 这是「只减不增」的起点；每收敛一批后下调此值，使门禁逐步收紧。
# 历次基线：1057（2026-08-05 初始）→ 205（mixin 共享基类重构）→ 96（误标，
# 实际 6e24aa0 仍 334）→ 95（本轮 lint 清理后真实值）。
#
# 当前 95：lint 清理修复 web/api 被误删的 re-export（get_store/get_rag_config）与
# request_id 动态属性 setattr 写法后，pyright==1.1.411 在 src+web 实测值。
TYPE_ERROR_BASELINE = 95


def count_errors(report: dict) -> int:
    diags = report.get("generalDiagnostics", [])
    return sum(1 for d in diags if d.get("severity") == "error")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <pyright-output.json>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 2

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: failed to parse {path}: {exc}", file=sys.stderr)
        return 2

    errors = count_errors(report)
    warnings = sum(1 for d in report.get("generalDiagnostics", []) if d.get("severity") == "warning")
    print(f"pyright type errors : {errors}")
    print(f"pyright warnings    : {warnings}")
    print(f"locked baseline     : {TYPE_ERROR_BASELINE}")
    if errors > TYPE_ERROR_BASELINE:
        print(
            f"FAIL: type errors increased by {errors - TYPE_ERROR_BASELINE} "
            f"(baseline={TYPE_ERROR_BASELINE}). 请修复新增类型错误，"
            f"或将基线随收敛同步下调。"
        )
        return 1
    if errors < TYPE_ERROR_BASELINE:
        print(
            f"PASS: type errors reduced by {TYPE_ERROR_BASELINE - errors} "
            f"(baseline={TYPE_ERROR_BASELINE}). 建议将 TYPE_ERROR_BASELINE 下调至 {errors} 以固化收敛。"
        )
    else:
        print(f"PASS: type errors at baseline ({TYPE_ERROR_BASELINE}), 未新增。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
