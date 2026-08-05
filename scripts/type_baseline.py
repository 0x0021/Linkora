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
# 历次基线：1057（2026-08-05 初始）→ 205（mixin 共享基类重构）→ 96（本轮）。
#
# 当前 96 = 205 基线 - 109，通过「上帝类 MRO 契约 + threading.local 子类化 +
# 真实类型注解」收敛（pyright==1.1.411，与 CI 版本一致）：
#   - component_base：dws 契约从具体 DwsAdapter 改为 BaseIMAdapter，消飞书/企微
#     适配器传参的 62 处 reportArgumentType（reportAttributeAccessIssue 主体之一）；
#   - agent/_AgentThreadState、skills/_RouterThreadState：threading.local 子类化，
#     消除 reportInvalidTypeForm 与并发读 AttributeError 隐患；
#   - llm/client.chat()：@overload 分流返回类型（LLMResponse vs Iterator），
#     消除下游 resp.content 在 Iterator 上的 reportAttributeAccessIssue；
#   - sqlite_store*/kb_repo：_vector_index 从 object/Any 改回 VectorIndex，恢复成员检查；
#   - poller_utils / web/api：用 Callable/Message/EmbeddingClient/AppConfig 替换
#     callable/any/object，消除一批下游 unknown；
#   - 顺带修掉被 unknown 掩盖的真实缺陷：Phase 4 路由埋点 candidates_count/
#     convergence_applied 因属性名拼错（_last_routing_detail）恒为 0；流式
#     tool_calls[].function 为 None 时 AttributeError 打断响应；_require_cfg() 把
#     配置未就绪的 500 收敛为 503；kb_search_enabled 字段漏声明致关闭 RAG 开关恒失效。
# （205→96 之前的 1057→205 来自「为每个 mixin 家族建共享基类」：poller 家族
#  PollerMixinBase/LinkoraComponentBase 消 ~320、platform/engine 家族 EngineMixinBase
#  消 ~308、dws_adapter 家族 DwsAdapterBase 82→0、memory 家族 SQLiteStoreBase 71→0、
#  im_adapter 家族 IMAdapterBase 25→0；共享基类绝不定义 dunder、stub 显式 ->Any。）
TYPE_ERROR_BASELINE = 96


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
