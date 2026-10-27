"""P2-7 不变量：``src.config_models`` 是**包**，且公共 API 面稳定。

背景
----
``src/config_models.py``（1127 行 / 28 个 Pydantic 模型）转为包
``src/config_models/``，以便按领域拆分子模块（platform / llm / rules_tools /
memory_skills / system / app）。

转换方式：``git mv src/config_models.py src/config_models/__init__.py``，模型类
**暂留** ``__init__.py``。因此：

- 所有模型的 ``__module__`` 仍是 ``"src.config_models"``（未变）；
- ``from src.config_models import X`` 语义**完全不变**（本文件即包的
  ``__init__``，re-export 即定义处）；
- Pydantic v2 依据 ``cls.__module__`` 取该模块 globals 求值注解 → 类留在
  ``__init__.py`` 时前向引用解析行为与转换前一致。

后续步骤逐组外移时必须遵守：

1. **新模块必须运行时 import 其依赖类**（不可用 ``TYPE_CHECKING``）——
   Pydantic v2 按 ``cls.__module__`` 查 globals 求值注解，仅类型检查期的导入
   会让 ``model_rebuild()`` 抛 ``PydanticUndefinedAnnotation``；
2. 包内不得存在与包同名的 ``.py`` 文件（同名时包遮蔽模块，导入解析到
   ``__init__.py``，历史路径语义改变）；
3. 公共 API 面（``__all__``）不得静默增减 —— 消费者遍布 src/ web/ tests/。

本测试锁住上述契约，防止后续外移步骤无声破坏。
"""
from __future__ import annotations

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
PKG_DIR = SRC / "config_models"

# 公共 API 面（转换前 src/config.py 依赖的全部模型 + 常量）
EXPECTED_PUBLIC_API = {
    # 常量
    "DEFAULT_DATA_DIR",
    "DEFAULT_STORAGE_PATH",
    "DEFAULT_TMP_IMAGES_DIR",
    "DEFAULT_BACKUP_DIR",
    # 平台
    "DwsConfig",
    "AdapterOverrideConfig",
    "PollerConfig",
    "StorageConfig",
    "PlatformRagConfig",
    "PlatformLLMConfig",
    "PlatformToolsConfig",
    "PlatformConfig",
    # 规则与工具
    "KeywordRule",
    "RulesConfig",
    "OcrPostprocessConfig",
    "ToolsConfig",
    # LLM 与嵌入
    "LlmAdvancedConfig",
    "LlmConfig",
    "LlmThrottleConfig",
    "EmbeddingConfig",
    # 记忆 / 技能 / 检索
    "MemoryConfig",
    "SkillsConfig",
    "SkillHubConfig",
    "RagConfig",
    # 系统
    "LoggingConfig",
    "SafetyConfig",
    "DeadLetterConfig",
    "ProactiveConfig",
    "SummaryBackfillConfig",
    "WebConfig",
    "OaApprovalConfig",
    # 根
    "AppConfig",
}

# src/config.py 直接依赖的私有名（模型解析/重建辅助）
EXPECTED_PRIVATE_API = {
    "_resolve_model_cls",
    "_ConfigState",
    "_config_state",
    "_MODELS_REBUILT",
    "_MODELS_REBUILD_LOCK",
    "_ensure_models_rebuilt",
}


def test_config_models_is_a_package_not_a_module() -> None:
    """src/config_models 必须是包（有 __init__.py），且不得有同名 .py 文件。"""
    assert PKG_DIR.is_dir(), "src/config_models 应为包目录"
    assert (PKG_DIR / "__init__.py").is_file(), "src/config_models/__init__.py 缺失"
    assert not (SRC / "config_models.py").exists(), (
        "src/config_models.py 与包 src/config_models/ 并存 —— 同名时包遮蔽模块，"
        "导入解析行为改变"
    )


def test_public_api_surface_stable() -> None:
    """公共 API 面不得静默增减。"""
    import src.config_models as cm

    assert set(cm.__all__) == EXPECTED_PUBLIC_API, (
        "公共 API 面变化："
        f"新增 {sorted(set(cm.__all__) - EXPECTED_PUBLIC_API)}，"
        f"缺失 {sorted(EXPECTED_PUBLIC_API - set(cm.__all__))}"
    )


def test_private_api_still_importable() -> None:
    """src/config.py 依赖的私有名必须仍可显式导入。"""
    import src.config_models as cm

    for name in EXPECTED_PRIVATE_API:
        assert hasattr(cm, name), f"src.config_models 丢失私有名 {name}"


def test_all_entries_resolve() -> None:
    """__all__ 中每个名字都必须真实存在。"""
    import src.config_models as cm

    missing = [n for n in cm.__all__ if not hasattr(cm, n)]
    assert not missing, f"__all__ 含不存在的名字: {missing}"


def test_models_still_resolve_annotations() -> None:
    """Pydantic 模型必须已完成注解解析（无需 rebuild 也能构造）。"""
    from src.config_models import AppConfig, PlatformConfig

    assert AppConfig.__pydantic_complete__, "AppConfig 注解未解析完成"
    assert PlatformConfig.__pydantic_complete__, "PlatformConfig 注解未解析完成"


def test_model_module_identity_unchanged() -> None:
    """留在 ``__init__.py`` 的模型类 ``__module__`` 仍为 ``src.config_models``。

    这一条同时保护两件事：
    - 包化本身未改变类的身份（``__module__`` 决定 Pydantic 求值命名空间，
      也影响序列化/反序列化时的类型标识）；
    - 后续外移子模块时，若某模型的依赖类不在其所在模块 globals 中，
      ``model_rebuild()`` 会抛 ``PydanticUndefinedAnnotation`` —— 此测试
      会先一步暴露问题。
    """
    from src.config_models import AppConfig, PlatformConfig, WebConfig

    for cls in (AppConfig, PlatformConfig, WebConfig):
        assert cls.__module__ == "src.config_models", (
            f"{cls.__name__}.__module__ 变为 {cls.__module__!r}；"
            "若已外移到子模块，需确认其依赖类为**运行时** import（非 TYPE_CHECKING），"
            "否则 model_rebuild 会抛 PydanticUndefinedAnnotation"
        )


# P2-7 第 2 步已外移的组 → 模型类
EXTRACTED_GROUPS: dict[str, list[str]] = {
    "llm": ["LlmAdvancedConfig", "LlmConfig", "LlmThrottleConfig", "EmbeddingConfig"],
    "rules_tools": ["KeywordRule", "RulesConfig", "OcrPostprocessConfig", "ToolsConfig"],
    "memory_skills": ["MemoryConfig", "SkillsConfig", "SkillHubConfig", "RagConfig"],
}


@pytest.mark.parametrize(
    "group,member",
    [(g, m) for g, ms in EXTRACTED_GROUPS.items() for m in ms],
)
def test_extracted_model_module_identity(group: str, member: str) -> None:
    """已外移的模型类 ``__module__`` 指向其子模块。

    外移会改变 ``__module__``，而 Pydantic v2 依此查找注解求值命名空间 ——
    若该模块缺少依赖类的**运行时**绑定，注解解析会失败（表现为
    ``PydanticUndefinedAnnotation`` 或字段类型退化为 Any）。本测试锁住
    「类与子模块的对应关系」，配合 complete 断言共同守护该约束。
    """
    import src.config_models as cm

    cls = getattr(cm, member)
    assert cls.__module__ == f"src.config_models.{group}", (
        f"{member}.__module__ = {cls.__module__!r}，期望 src.config_models.{group}"
    )
    assert cls.__pydantic_complete__, f"{member} 注解未解析完成（跨模块依赖需运行时 import）"


def test_extracted_groups_have_no_missing_runtime_deps() -> None:
    """已外移的三个组**零外部配置类依赖** → 注解解析不依赖其他模块的 globals。

    若后续有人给这些组添加了对其它配置类的引用（如 RulesConfig 引用
    PlatformConfig），必须同时在子模块里做**运行时** import，否则
    ``__pydantic_complete__`` 会变 False。
    """
    import ast
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[1] / "src" / "config_models"
    init_tree = ast.parse((pkg / "__init__.py").read_text(encoding="utf-8"))
    init_classes = {
        n.name for n in init_tree.body if isinstance(n, ast.ClassDef)
    }

    for group in EXTRACTED_GROUPS:
        tree = ast.parse((pkg / f"{group}.py").read_text(encoding="utf-8"))
        own = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        referenced: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    referenced.add(sub.id)
        cross = (referenced & init_classes) - own
        assert not cross, (
            f"{group}.py 引用了其它模块的模型类 {sorted(cross)}，"
            "必须在该子模块做**运行时** import（不可用 TYPE_CHECKING），"
            "否则 Pydantic 注解解析会失败"
        )


def test_ensure_models_rebuilt_still_works() -> None:
    """延迟 rebuild 辅助仍可调用且幂等。"""
    from src.config_models import _ensure_models_rebuilt

    _ensure_models_rebuilt()
    _ensure_models_rebuilt()  # 幂等


def test_no_star_import_of_config_models() -> None:
    """禁止 `from src.config_models import *`（会绕过 __all__ 契约）。"""
    import ast

    for path in list(SRC.rglob("*.py")) + list((SRC.parent / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "src.config_models":
                assert not any(a.name == "*" for a in node.names), (
                    f"{path} 出现 from src.config_models import *"
                )


@pytest.mark.parametrize(
    "name",
    sorted(EXPECTED_PUBLIC_API),
)
def test_every_public_model_importable(name: str) -> None:
    """每个公共名字都能从 src.config_models 直接导入。"""
    ns: dict = {}
    exec(f"from src.config_models import {name}", ns)  # noqa: S102
    assert name in ns
