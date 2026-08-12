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
# 实际 6e24aa0 仍 334）→ 95（lint 清理后真实值）→ 74（2026-08-12 安全批次：
# 12 个自包含模块零行为变更修复，含 ocr_postprocess 真实 import bug）。
#
# 当前 74（pyright==1.1.411，src+web）：规则分布 reportArgumentType(29)/
# reportReturnType(16)/reportAttributeAccessIssue(7)/reportOptionalMemberAccess(7)/
# reportOptionalSubscript(5)/reportCallIssue(4)/reportOptionalOperand(3)/
# reportOperatorIssue(2)/reportAssignmentType(1)。
# 剩余大头：web/routers(kb 8/api 4/persona 3/config 2/intents 2)、
# src/memory/embedding(6)、src/platform/runtime_lifecycle(4)、
# src/skills/tool_wrapper(4)、src/llm(agent/history/prompt_builder/system_prompt 各 2-3)。
# 多为 DB 返回 int|None 守卫、None→str 参数、web 字典下标，需逐函数核实后谨慎收敛。
TYPE_ERROR_BASELINE = 74


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
