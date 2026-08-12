"""意图分类体系观测路由。

从 `web/api.py` 抽取（原 2946–2982 行），业务逻辑不变。
- 配置经 `_api._get_cfg()` 读取（单例优先 + 磁盘兜底，统一真源）；
- IntentRegistry 直接取自 src.intent。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.intent import IntentRegistry
from src.tools.registry import get_builtin_tool_platforms
import web.api as _api

from web.dependencies import get_app_instance, logger

router = APIRouter()


def _build_allowed_tools_for_platform(platform: str) -> set[str] | None:
    """根据平台构建允许的工具名集合。

    规则（与技能平台隔离一致）：
    - 空 platforms = 通用（全平台可见）
    - 有明确平台标记 = 仅该平台可见
    返回 None 表示不过滤（platform 为空时）。
    """
    if not platform:
        return None
    tool_platforms = get_builtin_tool_platforms()
    allowed: set[str] = set()
    for tool_name, platforms in tool_platforms.items():
        if not platforms or platform in platforms:
            allowed.add(tool_name)
    # 同时收集已注册的技能工具（技能工具不在 BUILTIN_TOOL_MANIFEST 中）
    app_instance = get_app_instance()
    if app_instance is not None:
        agent = getattr(app_instance, "llm_agent", None)
        if agent is not None and agent.tool_router is not None:
            for tool in agent.tool_router._tools.values():
                tool_platforms_field = getattr(tool, "platforms", []) or []
                if not tool_platforms_field or platform in tool_platforms_field:
                    allowed.add(tool.name)
    return allowed


@router.get("/api/intents")
async def intent_taxonomy(platform: str = ""):
    """返回抽象意图分类体系（类别/层次/语义边界/触发条件/证据词量/工具映射）。

    用于观测当前生效的意图模型：处置层（business / social 子型）与行动层
    （query/execute/analyze/communicate/media）。证据词会应用 config 中的覆盖。
    """
    # 配置缺失时抛 503（放在 try 外：HTTPException 是 Exception 子类，
    # 落进下方 except 会被压平成语义错误的 500）。
    config = _api._require_cfg()
    try:
        registry = IntentRegistry()
        registry.apply_intent_filter(getattr(config.rules, "intent_filter", {}) or {})
        allowed_tools = _build_allowed_tools_for_platform(platform)
        data = registry.as_definitions(allowed_tools=allowed_tools)
        # 附带当前生效的路由模式与工具数，便于管理端展示
        tools_cfg = getattr(config, "tools", None)
        try:
            fs = getattr(tools_cfg, "model_fields_set", set()) or set()
        except Exception:
            fs = set()
        if tools_cfg is not None and "tool_routing_mode" in fs:
            routing_mode = tools_cfg.tool_routing_mode
        elif tools_cfg is not None and "expose_all_tools" in fs:
            routing_mode = "all" if tools_cfg.expose_all_tools else "keyword"
        else:
            routing_mode = "smart"
        data["meta"] = {
            "routing_mode": routing_mode,
            "tools_count": len(getattr(tools_cfg, "available", []) or []),
            "routing_mode_desc": {
                "smart": "按意图关键词精准暴露相关工具；无明确意图时回退全量（不漏工具）",
                "all": "每轮把全部工具暴露给模型（简单但易乱调/费 token）",
                "keyword": "仅按关键词过滤工具（最省但可能漏）",
            }.get(routing_mode, ""),
        }
        return data
    except Exception as e:
        logger.error("意图体系API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
