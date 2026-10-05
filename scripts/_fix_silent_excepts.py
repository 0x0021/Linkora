#!/usr/bin/env python3
"""一次性工具：给 SILENT 宽异常处理器注入日志行（仅新增，不改控制流）。

目的：把「静默吞噬真实错误」的宽异常处理器变成「至少记一条 exc_info 日志」，
便于排障。纯文本行插入（不在 except 头与首个语句之间破坏缩进），行为零变更。

- 跳过 src/utils/logger.py（日志基础设施自身，避免递归）。
- 日志符号优先级：模块级 `logger` -> `logging`。
- 注入行放在处理器体首行，使用 exc_info=True 保留完整堆栈。

不属于门禁，不进 CI；修完即弃。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_FILES = {"src/utils/logger.py"}


def _is_broad(node: ast.ExceptHandler) -> bool:
    if node.type is None:
        return True
    if isinstance(node.type, ast.Name):
        return node.type.id == "Exception"
    if isinstance(node.type, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id == "Exception" for e in node.type.elts)
    return False


def _has_logging(body):
    for n in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                n.func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"} \
                and isinstance(n.func.value, (ast.Name, ast.Attribute)):
            return True
        # 也认 traceback.print_exc() 这类显式堆栈打印（视为已处理）
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                n.func.attr == "print_exc" and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "traceback":
            return True
    return False


def _has_reraise(body):
    return any(isinstance(n, ast.Raise) for n in ast.walk(ast.Module(body=body, type_ignores=[])))


def _enclosing_func(tree, target):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    return node.name
    return "<module>"


def _avail_symbols(src: str):
    """判断文件可用的日志符号。返回 (has_logger, has_logging)。"""
    tree = ast.parse(src)
    has_logger = False
    has_logging = False
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == "logger":
                    has_logger = True
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "logger" or a.asname == "logger":
                    has_logger = True
                if a.name == "logging":
                    has_logging = True
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "logging" or a.asname == "logging":
                    has_logging = True
    return has_logger, has_logging


def fix_file(rel: str) -> int:
    if rel in SKIP_FILES:
        print(f"  SKIP {rel} (日志基础设施)")
        return 0
    path = ROOT / rel
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 收集 SILENT 处理器（按行号降序，便于逆序插入不影响行号）
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node):
            if _has_reraise(node.body) or _has_logging(node.body):
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                # 裸 pass 也注入（OTHER 类），但不算 SILENT 的危险优先——仍注入
                pass
            targets.append(node)
    if not targets:
        return 0
    has_logger, has_logging = _avail_symbols(src)
    file_is_print_script = rel.startswith("src/skills/") and "/scripts/" in rel
    targets.sort(key=lambda n: n.lineno, reverse=True)
    lines = src.splitlines(keepends=True)
    inserted = 0
    need_import = None
    for node in targets:
        # 计算体首行缩进
        if node.body and isinstance(node.body[0], ast.Pass) and len(node.body) == 1:
            indent = " " * (node.body[0].col_offset if node.body[0].col_offset else 8)
        elif node.body:
            indent = " " * node.body[0].col_offset
        else:
            indent = " " * 8
        func = _enclosing_func(tree, node)
        is_module_level = func == "<module>"
        # 模块级处理器：logger 可能尚未定义（如文件顶部探测 import 可用性），
        # 必须用 logging（始终可用），绝不能引用模块级 logger。
        if is_module_level:
            symbol = "logging"
            if not has_logging and need_import is None:
                need_import = "import logging"
        elif file_is_print_script:
            symbol = "traceback"
            if need_import is None:
                need_import = "import traceback"
        elif has_logger:
            symbol = "logger"
        else:
            symbol = "logging"
            if not has_logging and need_import is None:
                need_import = "import logging"
        if symbol == "traceback":
            stmt = f"{indent}traceback.print_exc()  # 静默吞掉前至少打印堆栈，便于排障\n"
        else:
            stmt = f'{indent}{symbol}.warning("broad except swallowed in {func}() @ {rel}:{node.lineno}, see exc_info", exc_info=True)\n'
        lines.insert(node.lineno, stmt)
        inserted += 1
    # 需要时补 import（只插在**模块级**最后一个 import 之后，避免插进函数体）
    if need_import:
        tree2 = ast.parse("".join(lines))
        last_mod_import = 0
        for n in ast.walk(tree2):
            if isinstance(n, (ast.Import, ast.ImportFrom)) and (n.col_offset or 0) == 0:
                last_mod_import = max(last_mod_import, n.end_lineno or n.lineno)
        if last_mod_import:
            lines.insert(last_mod_import, need_import + "\n")
        else:
            insert_at = 0
            if lines and lines[0].startswith("#!"):
                insert_at = 1
            lines.insert(insert_at, need_import + "\n")
        inserted += 1
    path.write_text("".join(lines), encoding="utf-8")
    print(f"  FIXED {rel}: +{inserted} line(s)")
    return inserted


def main(argv):
    import sys
    from collections import defaultdict
    sys.path.insert(0, str(ROOT))
    from scripts.check_no_bare_except import classify, _iter_py_files
    # 直接复用闸门脚本的分类器，避免依赖外部 JSON 文件，做到自包含可复用。
    cats = classify(_iter_py_files())
    by_file = defaultdict(list)
    for item in cats["SILENT"] + cats["OTHER"]:
        by_file[item["file"]].append(item)
    total = 0
    for rel in by_file:
        total += fix_file(rel)
    print(f"\nTOTAL inserted: {total}")
    print("提示：修完后跑 `python scripts/check_no_bare_except.py report` 复查剩余 SILENT。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
