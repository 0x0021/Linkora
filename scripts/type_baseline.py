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
# 当前 205 = 1057 基线 - 852，全部通过「为每个 mixin 家族建共享基类」结构性消除：
#   - poller 家族 PollerMixinBase / LinkoraComponentBase，消 ~320 条；
#   - platform/engine 家族 EngineMixinBase（AST 提取 97 方法 + 49 状态 stub），消 ~308 条；
#   - dws_adapter 家族 DwsAdapterBase(60 方法/4 状态) 82 → 0；
#   - memory 家族 SQLiteStoreBase(19 方法/30 状态) 71 → 0；
#   - im_adapter 家族 IMAdapterBase(14 方法) 25 → 0。
# 共享基类两条硬约束（踩过坑，已由 test_shared_type_bases_define_no_init 固化）：
#   1. 绝不定义任何 dunder（尤其 __init__），否则在 MRO 中截胡真实父类初始化链；
#   2. 所有 stub 必须显式 `-> Any`，否则被推断返回 None，调用点 isinstance 收窄成 Never。
# 顺带修掉被 unknown 掩盖的真实隐患：concurrent 未导入、upsert_conversation 缺
# last_message_time、DocumentParser 收 PollerConfig、SQLiteStore._MIGRATE_PLATFORM_PREFIXES
# 误缩进导致多账号迁移静默失败、wecom auth_login 捕获不存在的 IMAdapterTimeoutError
# 抛 NameError 使 3 次重试完全失效。
# 剩余 205 已无 MRO 噪声，是真实类型问题：web/routers(59)/src/llm(35)/src/memory(31)/
# src/platform(20)/src/poller_utils.py(13)。
TYPE_ERROR_BASELINE = 205


def count_errors(report: dict) -> int:
    diags = report.get("generalDiagnostics", [])
    return sum(1 for d in diags if d.get("severity") == "error")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/type_baseline.py <pyright-output.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    report = json.loads(path.read_text(encoding="utf-8"))
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
