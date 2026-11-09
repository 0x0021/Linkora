"""入站消息安全防护：事件查询 / 统计 / 策略配置 / 检测预览。

全部是**新增端点**，不改动任何既有接口契约。事件数据来自
``src.guard.events``（独立 JSONL），不碰主库与写入闸门。

鉴权：``/api/guard`` 已登记进 ``web/api.py::_SENSITIVE_GET_PATHS``，
GET 需 admin；POST 由 ``_is_sensitive_request`` 统一要求 admin。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.guard.config import GuardConfig
from src.guard.events import list_events, stats
from src.guard.inspect import assess
from src.guard.isolation import isolate_for_prompt
from src.guard.service import (
    get_guard_config,
    get_guard_config_override,
    set_guard_config_override,
)
from web.errors import SAFE_OPERATION_FAILED

router = APIRouter()

_SIGNAL_LABELS = {
    "boundary_escape": "伪造不可信数据边界标记",
    "role_spoofing": "伪造对话角色 / 模板标记",
    "role_label": "行首角色标签（仅记录）",
    "control_chars": "不可见控制字符",
    "oversize": "超长消息",
    "repeat_flood": "病态字符填充",
}


@router.get("/api/guard/events")
def guard_events(
    limit: int = 50,
    offset: int = 0,
    days: int = 7,
    risk: str = "",
    action: str = "",
    platform: str = "",
    keyword: str = "",
):
    """分页列出防护事件（最新在前），支持风险等级 / 处置 / 平台 / 关键词过滤。"""
    try:
        items, total = list_events(
            limit=limit, offset=offset, days=days, risk=risk,
            action=action, platform=platform, keyword=keyword,
        )
        return {"success": True, "items": items, "count": len(items), "total": total}
    except (OSError, ValueError, TypeError) as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e


@router.get("/api/guard/stats")
def guard_stats(days: int = 7):
    """聚合统计：检测总量、风险分布、处置分布、命中信号、趋势、高频来源。"""
    try:
        result = stats(days=days)
        result["success"] = True
        result["policy"] = get_guard_config().model_dump()
        result["override_active"] = get_guard_config_override() is not None
        return result
    except (OSError, ValueError, TypeError) as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e


@router.get("/api/guard/config")
def get_guard_policy():
    """读取当前生效的防护策略。"""
    return {
        "success": True,
        "config": get_guard_config().model_dump(),
        "override_active": get_guard_config_override() is not None,
        "persistent_hint": "运行期调整不写入 config.yaml；重启后回到 safety.guard 段配置。",
    }


@router.post("/api/guard/config")
def update_guard_policy(payload: GuardConfig):
    """调整防护策略（仅当前进程生效，不落盘、不触碰 config.yaml 红线）。"""
    set_guard_config_override(payload)
    return {
        "success": True,
        "config": payload.model_dump(),
        "override_active": True,
        "message": "防护策略已更新，立即生效；重启后回到 config.yaml 的 safety.guard 配置。",
    }


@router.post("/api/guard/config/reset")
def reset_guard_policy():
    """清除运行期覆盖，恢复 config.yaml 的 safety.guard 策略。"""
    set_guard_config_override(None)
    return {
        "success": True,
        "config": get_guard_config().model_dump(),
        "override_active": False,
        "message": "已恢复 config.yaml 中的防护策略。",
    }


class GuardPreviewRequest(BaseModel):
    text: str = Field(default="", max_length=20000)


@router.post("/api/guard/preview")
def preview_guard(req: GuardPreviewRequest):
    """检测预览：输入一段文本，展示风险判定与「送入 LLM 前」的处理结果。

    纯计算，不写事件、不落盘、不进 LLM，用于验证规则是否误伤正常业务消息。
    """
    cfg = get_guard_config()
    result = assess(req.text, cfg)
    isolated = isolate_for_prompt(req.text, cfg, result)
    return {
        "success": True,
        "risk": result.risk,
        "signals": list(result.signals),
        "signal_labels": [_SIGNAL_LABELS.get(s, s) for s in result.signals],
        "char_count": result.char_count,
        "unchanged": isolated == req.text,
        "isolated": isolated,
        "mode": cfg.mode,
        "block_high_risk": cfg.block_high_risk,
    }


__all__ = ["router"]
