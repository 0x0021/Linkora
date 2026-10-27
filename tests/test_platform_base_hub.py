"""P2-1 步骤 2 不变量：``src/platform/base.py`` 不得退化为「符号仓库」。

背景
----
``src/platform/base.py`` 曾是星号导入枢纽（12 处 ``from .base import *``），
叠加 43 个纯重导出 import 后成为事实上的「公共符号仓库」：任何人都能从这里
捞到任意第三方符号，于是「base.py 里这个 import 还需要吗」永远无法回答，
改 base.py 的真实影响面也永远无法界定。

P2-1 步骤 1（``c87297e`` 之后的 ``b2e7999``）拆掉星号；步骤 2 把 base.py
瘦身为**只定义自己拥有的东西**（6 个真实定义 + TYPE_CHECKING 类型注解块）。

本测试锁住三条不变量：

1. base.py 只保留自己**定义**的名字，不重导出第三方符号；
2. base.py **不**重新引入 ``from .base import *`` 式的星号转出（由
   ``tests/test_platform_core.py`` 等既有测试间接覆盖，这里做结构层断言）；
3. ``PlatformContext`` 的类型注解在 TYPE_CHECKING 块中仍可被 pyright 解析
   （用 AST 断言，避免删掉导入后注解悄悄退化为 ``Any``）。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

BASE_PY = pathlib.Path(__file__).resolve().parents[1] / "src" / "platform" / "base.py"

# base.py 自己**定义**的名字（不是 import 进来的）
EXPECTED_OWN_DEFS = {
    "SEND_RETRY_BACKOFF_SECONDS",
    "_active_platform_ctx",
    "PlatformContext",
    "BackgroundLLMThrottle",
    "extract_card_title",
    "logger",
}

# PlatformContext 注解里引用、必须仍可解析的类型
EXPECTED_ANNOTATION_TYPES = {
    "SQLiteStore",
    "BaseIMAdapter",
    "MessagePoller",
    "LLMAgent",
    "SummaryScheduler",
    "DynamicSummaryScheduler",
    "DisplaySummaryScheduler",
}


def _parse() -> ast.Module:
    return ast.parse(BASE_PY.read_text(encoding="utf-8"))


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_base_defines_only_its_own_symbols() -> None:
    """base.py 定义的名字集合固定 —— 多一个即说明又开始囤积第三方符号。"""
    defined = _defined_names(_parse())
    extra = defined - EXPECTED_OWN_DEFS
    assert not extra, (
        f"base.py 新增了未登记的顶层定义 {sorted(extra)}；"
        "本文件应只定义自己拥有的符号，第三方依赖请在消费方直接 import"
    )


def test_base_does_not_reexport_third_party_symbols() -> None:
    """base.py 不得重导出第三方符号（纯重导出已全部移除）。"""
    tree = _parse()
    imported = _imported_names(tree)
    defined = _defined_names(tree)

    # 允许的 import：标准库 + dotenv + 自身真实使用的 llm.client + TYPE_CHECKING 块
    allowed_runtime = {
        "annotations",  # from __future__ import annotations
        "logging", "re", "threading", "time",
        "ContextVar", "dataclass", "TYPE_CHECKING",
        # .env 加载的**幂等兜底**（真正入口在 main() 首行，见 src/dotenv_loader.py）。
        # 保留原因：src/memory/account_identity.py 等 3 处仍在**模块级**读环境变量，
        # 这些模块可能在 main() 之前被导入 —— 缺了兜底，DWS_BIN 覆盖会静默失效。
        "load_env",
        "seconds_since_rate_limit",  # BackgroundLLMThrottle 真实使用
    }
    allowed_type_checking = EXPECTED_ANNOTATION_TYPES

    # 找出 TYPE_CHECKING 块内的 import
    tc_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    tc_names.update(a.asname or a.name for a in sub.names)

    runtime_imports = imported - tc_names
    # `annotations` 来自 __future__，是语言特性不是符号
    runtime_imports.discard("annotations")

    unexpected = runtime_imports - allowed_runtime
    assert not unexpected, (
        f"base.py 出现未登记的运行时 import {sorted(unexpected)}；"
        "P2-1 步骤 2 已移除全部纯重导出 —— 若确有新用途，请在本文件真实使用它，"
        "否则应在消费方直接 import"
    )

    missing_tc = allowed_type_checking - tc_names
    assert not missing_tc, (
        f"PlatformContext 注解用到的类型 {sorted(missing_tc)} 从 TYPE_CHECKING 块消失；"
        "删掉会让 pyright 无法解析注解，类型退化为 Any"
    )
    assert not (tc_names & defined), "TYPE_CHECKING 块内不应定义名字"


def _is_type_checking(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
    ) or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def test_base_has_no_star_import() -> None:
    """base.py 自身不得使用星号导入（它是 hub，不该再向外星号转出）。"""
    tree = _parse()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(a.name == "*" for a in node.names), (
                f"base.py 第 {node.lineno} 行出现星号导入"
            )


def test_platform_context_annotations_still_typed() -> None:
    """PlatformContext 的组件字段注解不得退化为 Any（须保留具体类型）。"""
    tree = _parse()
    cls = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlatformContext"),
        None,
    )
    assert cls is not None, "PlatformContext 定义丢失"

    annotated: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotated[node.target.id] = ast.unparse(node.annotation)

    for field in ("store", "dws", "poller", "llm_agent"):
        assert field in annotated, f"PlatformContext.{field} 字段丢失"
        ann = annotated[field]
        assert "None" in ann, f"PlatformContext.{field} 注解应含 None，实际 {ann!r}"
        assert ann.strip("\"'") not in ("Any", "object"), (
            f"PlatformContext.{field} 注解退化为 {ann!r}"
        )

    # 注解里引用的类型名必须在 TYPE_CHECKING 块中可解析
    referenced = {
        name
        for name in EXPECTED_ANNOTATION_TYPES
        if any(name in ann for ann in annotated.values())
    }
    assert referenced == EXPECTED_ANNOTATION_TYPES, (
        f"注解引用的类型集合变化：{sorted(referenced)}"
    )


def test_base_public_api_unchanged() -> None:
    """``src.platform`` 包对外的 5 个名字必须仍可从 base 取到。"""
    from src.platform import base as base_mod

    for name in (
        "PlatformContext",
        "BackgroundLLMThrottle",
        "extract_card_title",
        "_active_platform_ctx",
        "SEND_RETRY_BACKOFF_SECONDS",
    ):
        assert hasattr(base_mod, name), f"base.py 丢失对外名字 {name}"


@pytest.mark.parametrize(
    "module_path",
    [
        "src/platform/runtime.py",
        "src/platform/runtime_setup.py",
        "src/platform/runtime_lifecycle.py",
        "src/platform/runtime_inbound.py",
        "src/platform/runtime_dispatch.py",
        "src/platform/runtime_llm_reply.py",
        "src/platform/runtime_reply_guard.py",
        "src/platform/message_loop.py",
        "src/platform/memory.py",
        "src/platform/reply_helpers.py",
        "src/platform/primary.py",
    ],
)
def test_consumers_have_no_star_import(module_path: str) -> None:
    """12 个消费者均已改为显式导入清单（步骤 1 成果，防回潮）。"""
    path = pathlib.Path(__file__).resolve().parents[1] / module_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(a.name == "*" for a in node.names), (
                f"{module_path} 第 {node.lineno} 行出现星号导入"
            )
