#!/usr/bin/env python3
"""``except Exception`` / 裸 ``except:`` 非回归门禁（P3 渐进收敛，2026-10-05）。

本项目历史上 `except Exception` 高达 600+ 处，宽异常会**静默吞噬真实错误**、阻碍排障
（2026-10-05 事故中「静默吞噬」吃过亏）。本脚本提供两件事：

1. **防回潮闸门**（默认 / ``count`` 子命令）：统计 src/ + web/ 中 ``except Exception``
   与裸 ``except:`` 的总数，与锁定的基线 ``BARE_EXCEPT_BASELINE`` 比较：

   - total <= BASELINE -> 通过（允许逐步收敛，但不允许回退）。
   - total >  BASELINE -> 失败（exit 1），阻止新增宽异常合入 main。

2. **静默吞噬分类**（`report` 子命令）：用 AST 扫描所有宽异常处理器，按危险度分类：

   - SILENT      : 既无日志调用、也无 re-raise —— **真正会吞掉错误**，优先修。
   - LOGGED      : 处理器内有 logging 调用（debug/info/warning/error/...）。
   - RERAISE     : 处理器内 re-raise（含 ``raise`` / ``raise X from ...``）。
   - OTHER       : 其它（如转换为领域异常后 raise，已算 RERAISE；此处多为空 body）。

基线更新流程（每收敛一批后）：
  1. 修复一批：给 SILENT 处理器补日志（零行为变更），或收窄异常类型；
  2. 跑 ``python scripts/check_no_bare_except.py`` 确认 total 下降；
  3. 把本脚本的 ``BARE_EXCEPT_BASELINE`` 改为新 total，提交，CI 门禁随之收紧。

用法：
    python scripts/check_no_bare_except.py            # 闸门（对照基线）
    python scripts/check_no_bare_except.py count      # 同上
    python scripts/check_no_bare_except.py report     # 打印 SILENT 处理器清单
    python scripts/check_no_bare_except.py report --json out.json
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 锁定基线：在 main 实测 src+web 的宽异常总数（640，AST 权威计数；早先用 grep
# 粗数得 647 含 HTTPException 等具体异常，已排除）。这是「只减不增」的起点；
# 每收敛一批（收窄异常类型 / 删除冗余捕获）后下调此值，使门禁逐步收紧。
# 历次基线：647（grep 粗数，含具体异常虚高）→ 640（AST 精确，2026-10-05）→
# 639（draft_repo 收窄 except Exception 为 sqlite3.Error，2026-10-05）。
BARE_EXCEPT_BASELINE = 639

# 扫描范围（生产代码；测试可另行评估，先不纳入防止误伤测试桩）
SCAN_DIRS = ("src", "web")

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            # 跳过虚拟环境 / 构建产物
            if any(part in ("__pycache__", ".venv", "node_modules") for part in p.parts):
                continue
            files.append(p)
    return files


def _is_broad(node: ast.ExceptHandler) -> bool:
    """判定是否为「宽异常」捕获：裸 except: 或字面 except Exception。

    注意：HTTPException / RequestException 等**具体**异常虽然名字里含 "Exception"
    子串，但是是被精确捕获的子类异常，不属于宽异常，必须排除，否则基线虚高。
    """
    if node.type is None:
        return True  # 裸 except:
    if isinstance(node.type, ast.Name):
        return node.type.id == "Exception"
    if isinstance(node.type, ast.Tuple):
        # except (A, Exception, B) —— 含字面 Exception 也算宽捕获
        return any(
            isinstance(e, ast.Name) and e.id == "Exception" for e in node.type.elts
        )
    return False


def count_bare_excepts(files: list[Path]) -> int:
    """统计 ``except Exception`` 与裸 ``except:`` 的总数。"""
    total = 0
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_broad(node):
                total += 1
    return total


def _has_logging(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in LOG_METHODS:
                # 必须是某个对象/模块的方法调用（logger.error / logging.error / ...）
                if isinstance(f.value, (ast.Name, ast.Attribute)):
                    return True
    return False


def _has_reraise(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
    return False


def classify(files: list[Path]) -> dict:
    """分类所有宽异常处理器，返回按危险度分组的清单。"""
    cats: dict[str, list[dict]] = {"SILENT": [], "LOGGED": [], "RERAISE": [], "OTHER": []}
    for p in files:
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_broad(node):
                continue
            name = "bare" if node.type is None else ast.unparse(node.type)
            rel = str(p.relative_to(ROOT))
            lineno = node.lineno
            body_src = ast.get_source_segment(src, node) or ""
            has_log = _has_logging(node.body)
            has_raise = _has_reraise(node.body)
            if has_raise:
                cat = "RERAISE"
            elif has_log:
                cat = "LOGGED"
            elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                cat = "OTHER"
            else:
                cat = "SILENT"
            cats[cat].append(
                {
                    "file": rel,
                    "line": lineno,
                    "name": name or "bare",
                    "snippet": body_src.splitlines()[0] if body_src else "",
                }
            )
    return cats


def _cmd_count() -> int:
    files = _iter_py_files()
    total = count_bare_excepts(files)
    print(f"broad except (except Exception / bare) total : {total}")
    print(f"locked baseline                          : {BARE_EXCEPT_BASELINE}")
    if total > BARE_EXCEPT_BASELINE:
        print(
            f"FAIL: broad except increased by {total - BARE_EXCEPT_BASELINE} "
            f"(baseline={BARE_EXCEPT_BASELINE}). 请收敛宽异常（先修 SILENT 类），"
            f"或将基线随收敛同步下调。"
        )
        return 1
    if total < BARE_EXCEPT_BASELINE:
        print(
            f"PASS: broad except reduced by {BARE_EXCEPT_BASELINE - total} "
            f"(baseline={BARE_EXCEPT_BASELINE}). 建议将 BARE_EXCEPT_BASELINE 下调至 {total} 以固化收敛。"
        )
    else:
        print(f"PASS: broad except at baseline ({BARE_EXCEPT_BASELINE}), 未新增。")
    return 0


def _cmd_report(json_path: str | None) -> int:
    files = _iter_py_files()
    cats = classify(files)
    if json_path:
        Path(json_path).write_text(
            json.dumps(cats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"wrote {json_path}")
    for cat in ("SILENT", "OTHER", "LOGGED", "RERAISE"):
        items = cats[cat]
        print(f"\n### {cat} ({len(items)})")
        for it in items:
            print(f"  {it['file']}:{it['line']}  [{it['name']}]  {it['snippet'][:80]}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "count"
    if cmd in ("count", "gate"):
        return _cmd_count()
    if cmd == "report":
        json_path = argv[2] if len(argv) > 2 and argv[2] != "--json" else None
        # 支持 `--json out.json`
        if "--json" in argv:
            i = argv.index("--json")
            if i + 1 < len(argv):
                json_path = argv[i + 1]
        return _cmd_report(json_path)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
