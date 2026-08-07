"""工具白名单漂移防护测试（P0-1 回归防护）。

P0-1 复盘：曾有某次手工改 `config.yaml` 的 `tools.available` 时漏掉
`web_search` / `get_weather` / `get_attendance` 三项，导致这三个工具被
`ToolRouter` 的白名单硬屏蔽（execute 直接返回 "not in whitelist"），
且**没有任何测试报错**——属于「功能真实失效但测试绿」的隐蔽 bug。

本文件建立两层断言，确保这类漂移在 CI 中立刻变红：
1. 默认配置（config.py 的 ToolsConfig 默认值）必须 ⊇ 全部 manifest 工具名，
   且不多出任何未知条目（防止手滑写错/写漏）。
2. 若仓库存在运行时 `config.yaml`（生产/本地实际生效文件，gitignored），
   其 `tools.available` 也必须 ⊇ 全部 manifest 工具名。config.yaml 以整段
   覆盖默认，一旦少写即静默屏蔽，故必须单独校验。
3. 负向验证：白名单缺漏的工具确实会被 `ToolRouter.execute` 拒绝，
   确认「列表不全 → 工具失效」的链路真实存在（避免未来有人误以为缺项无害）。
4. `tools.available`（默认 + live yaml）必须全部在 TOOL_ACTION_MAP 有 action 映射。
   启动时 `validate_tool_action_coverage` 缺映射直接 ValueError 崩 bot——
   2026-08-03 事故：approval_list_executed 只注册 manifest、漏 TOOL_ACTION_MAP，
   前 3 项断言全绿（只查 manifest↔available），唯独启动崩。此处提前在测试层拦截。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import ToolsConfig, load_config
from src.intent.registry import TOOL_ACTION_MAP, validate_tool_action_coverage
from src.tools.base import BaseTool, ToolRouter
from src.tools.registry import BUILTIN_TOOL_MANIFEST


def _manifest_tool_names() -> set[str]:
    """从单一真源 BUILTIN_TOOL_MANIFEST 抽取全部工具名。

    与 register_builtin_tools 取名的规则一致：优先 cls.name，缺失则回退类名。
    """
    return {getattr(cls, "name", None) or cls.__name__ for cls in BUILTIN_TOOL_MANIFEST}


def test_default_whitelist_covers_all_manifest_tools():
    """默认 ToolsConfig.available 必须覆盖所有 manifest 工具（缺项即 P0-1 类 bug）。"""
    manifest = _manifest_tool_names()
    available = set(ToolsConfig().available)
    missing = manifest - available
    assert not missing, (
        f"默认配置 tools.available 漏掉了以下 manifest 工具（将导致静默屏蔽）: {sorted(missing)}"
    )


def test_default_whitelist_has_no_unknown_entries():
    """默认 available 不应含任何非 manifest 的条目（防手滑写错/写多）。"""
    manifest = _manifest_tool_names()
    available = set(ToolsConfig().available)
    unknown = available - manifest
    assert not unknown, (
        f"默认配置 tools.available 含未知工具名（可能拼写错误或已删除工具）: {sorted(unknown)}"
    )


def test_runtime_config_yaml_whitelist_covers_all_manifest_tools():
    """运行时 config.yaml 的 available 必须覆盖所有 manifest 工具（P0-1 直接防护）。

    config.yaml 以整段覆盖默认，少写即静默屏蔽；它是本地实际生效文件，
    故单独校验。文件缺失时跳过（CI/无本地配置环境不强制）。
    """
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        pytest.skip("config.yaml 不存在，跳过运行时白名单校验")

    cfg = load_config(str(cfg_path))
    manifest = _manifest_tool_names()
    available = set(cfg.tools.available)
    missing = manifest - available
    assert not missing, (
        f"config.yaml 的 tools.available 漏掉了以下 manifest 工具（P0-1 漂移！）: {sorted(missing)}"
    )


def test_available_covered_by_tool_action_map():
    """tools.available（默认 + live yaml）必须全部在 TOOL_ACTION_MAP 有 action 映射。

    直接复用启动校验 validate_tool_action_coverage（primary.py 启动必经），
    把「漏 TOOL_ACTION_MAP → 启动崩溃」的隐患提前到 CI 拦截。
    """
    validate_tool_action_coverage(ToolsConfig().available)
    cfg_path = Path("config.yaml")
    if cfg_path.exists():
        cfg = load_config(str(cfg_path))
        validate_tool_action_coverage(cfg.tools.available)
    # 显式断言 TOOL_ACTION_MAP 非空，防止未来有人把映射表清空导致校验形同虚设
    assert TOOL_ACTION_MAP, "TOOL_ACTION_MAP 不应为空"


def test_example_config_has_no_unknown_tool_entries():
    """config.yaml.example（用户照抄的模板）不得含任何非 manifest 的幽灵工具。

    2026-08-07 事故：审批工具收敛时删除了 get_my_approvals / get_approval_detail
    的实现类，却漏改 config.yaml.example 的 tools.available 与 rate_limit，
    导致照 example 新建配置会启动报「无对应工具」警告。CI 漂移测试此前只校验
    默认/live 值、不校验 example，故漏网。此处补齐。
    """
    example_path = Path("config.yaml.example")
    if not example_path.exists():
        pytest.skip("config.yaml.example 不存在，跳过")
    cfg = load_config(str(example_path))
    manifest = _manifest_tool_names()
    available = set(cfg.tools.available)
    unknown = available - manifest
    assert not unknown, (
        f"config.yaml.example 的 tools.available 含未知工具名（无对应实现类）: {sorted(unknown)}"
    )
    rate_limit_keys = set(getattr(cfg.tools, "rate_limit", {}) or {})
    unknown_rl = rate_limit_keys - manifest
    assert not unknown_rl, (
        f"config.yaml.example 的 tools.rate_limit 含未知工具名: {sorted(unknown_rl)}"
    )
    # example 的 available 也必须全部在 TOOL_ACTION_MAP 有映射（否则启动崩溃）
    validate_tool_action_coverage(cfg.tools.available)


def test_tool_router_refuses_unlisted_tool():
    """负向验证：白名单缺漏的工具会被 execute 拒绝（证实『列表不全→工具失效』链路）。"""

    class _DummyTool(BaseTool):
        name = "dummy_tool"
        description = "测试工具"
        parameters = {"type": "object", "properties": {}}

        def execute(self, args):  # pragma: no cover - 不应被调用
            return "should-not-run"

    # 白名单故意不含 dummy_tool
    router = ToolRouter(ToolsConfig(available=["send_message"]))
    router.register(_DummyTool())

    res = router.execute("dummy_tool", {})
    assert res.success is False
    assert "not in whitelist" in res.error
    # 确认它在已注册集合中，只是被白名单挡住（排除『未注册』的混淆）
    assert "dummy_tool" in router._tools
