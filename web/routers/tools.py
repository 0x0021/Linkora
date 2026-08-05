"""工具链路可视化路由。

列出每个已注册工具的名称、域、当前是否被白名单或停用技能屏蔽，
供前端「工具调用链路」页面展示，让运维能一目了然「到底会不会调 dws」。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from web.dependencies import get_app_instance, logger

router = APIRouter()


@router.get("/api/tools-chain")
async def list_tools(platform: str = ""):
    """返回所有已注册工具的可视化状态清单。

    每个工具包含:
    - name / display_name: 工具标识
    - description: 简要描述
    - intent_categories: 所属域类别
    - platforms: 适用平台（空=通用）
    - in_whitelist: 是否在 config.yaml tools.available 白名单内
    - blocked_by_skill: 是否被已停用技能级联屏蔽
    - source: "builtin" 或 "skill"（内置工具 vs 技能自动包装）
    - status: "active" | "disabled_whitelist" | "disabled_skill"

    当 platform 参数非空时，按平台过滤：
    - 空 platforms = 通用（全平台可见）
    - 有明确平台标记 = 仅该平台可见
    """
    try:
        app_instance = get_app_instance()
        if app_instance is None:
            return {"available": False, "reason": "bot 未就绪", "tools": []}

        agent = getattr(app_instance, "llm_agent", None)
        if agent is None or agent.tool_router is None:
            return {"available": False, "reason": "agent / tool_router 未就绪", "tools": []}

        router_ = agent.tool_router
        all_tools = list(router_._tools.values())
        cfg = app_instance.config
        whitelist = set(getattr(cfg.tools, "available", None) or [])

        # 按平台过滤：空 platforms = 通用（全平台可见），有标记 = 仅对应平台可见
        def _tool_supports_platform(tool) -> bool:
            if not platform:
                return True
            platforms = getattr(tool, "platforms", []) or []
            return not platforms or platform in platforms

        tools = [t for t in all_tools if _tool_supports_platform(t)]

        # 构建域映射用于技能停用检查
        domain_map = {
            t.name: list(getattr(t, "intent_categories", []) or [])
            for t in tools
        }
        skill_manager = getattr(agent, "skill_manager", None)
        blocked_by_skill = set()
        if skill_manager is not None:
            blocked_by_skill = skill_manager.get_disabled_skill_owned_tools(domain_map)

        result = []
        for t in tools:
            name = t.name
            mod = type(t).__module__
            is_builtin = mod.startswith("src.tools") or "tool_wrapper" not in mod
            # 内置工具受白名单约束；skill 工具不受白名单约束（由 skill enabled 门控）
            in_whitelist = (not whitelist) or (name in whitelist) or (not is_builtin)
            blocked = name in blocked_by_skill

            if blocked:
                status = "disabled_skill"
            elif not in_whitelist:
                status = "disabled_whitelist"
            else:
                status = "active"

            result.append({
                "name": name,
                "display_name": getattr(t, "display_name", name),
                "description": (getattr(t, "description", "") or "")[:160],
                "intent_categories": list(getattr(t, "intent_categories", []) or []),
                "platforms": list(getattr(t, "platforms", []) or []),
                "source": "builtin" if is_builtin else "skill",
                "in_whitelist": in_whitelist,
                "blocked_by_skill": blocked,
                "status": status,
            })

        # 屏蔽的排前面，方便一眼看到问题
        result.sort(key=lambda x: (x["status"] != "active", x["source"] != "builtin", x["name"]))
        return {
            "available": True,
            "platform": platform or "all",
            "whitelist_count": len(whitelist),
            "registered_count": len(result),
            "blocked_count": sum(1 for r in result if r["status"] != "active"),
            "tools": result,
        }
    except Exception as e:
        logger.error("工具链路API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
