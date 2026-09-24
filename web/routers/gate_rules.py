"""答复门禁规则管理路由。

提供门禁规则的增删改查、启用/停用（单条 + 批量）、统计与命中测试。
规则内容（分类 / 命中模式 / 拦截提示）均可后台灵活维护。

从 `web/api.py` 抽取 router 模式，业务逻辑不变：
- get_store / _require_cfg 经 `import web.api as _api` 做属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch。
- 各写操作放入 run_in_threadpool（store 连接按线程隔离）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

import web.api as _api
from web.schemas import (
    GateRule,
    GateRuleUpdate,
    GateRuleMatchTest,
    GateRuleBatchOp,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 预置分类（前端下拉 + 校验参考；管理员仍可在后台输入自定义分类名）
_PRESET_CATEGORIES = {
    "private_privacy": "私人隐私",
    "illegal_info": "违法信息",
    "negative_emotion": "负面情绪",
    "other": "其他",
}


def _normalize_category(category: str, category_label: str) -> "tuple[str, str]":
    """归一化分类：取 slug + 中文标签，自定义分类也允许（可扩展）。"""
    cat = (category or "other").strip() or "other"
    label = (category_label or "").strip()
    if not label:
        label = _PRESET_CATEGORIES.get(cat, cat)
    return cat, label


@router.get("/api/gate-rules")
async def list_gate_rules(
    category: str = "",
    enabled: int | None = None,
    search: str = "",
    limit: int = 500,
):
    """列出门禁规则（可按分类 / 启用状态 / 关键词过滤）。"""
    try:
        limit = max(1, min(limit, 1000))

        def _work():
            store = _api.get_store()
            rules = store.list_gate_rules(category=category, enabled=enabled, limit=limit)
            if search:
                s = search.lower()
                rules = [
                    r for r in rules
                    if s in (r.get("pattern") or "").lower()
                    or s in (r.get("name") or "").lower()
                    or s in (r.get("category_label") or "").lower()
                    or s in (r.get("intercept_message") or "").lower()
                ]
            categories = store.gate_rule_categories()
            return {
                "rules": rules,
                "categories": [
                    {"category": c, "category_label": lbl} for c, lbl in categories
                ],
                "preset_categories": [
                    {"category": k, "category_label": v}
                    for k, v in _PRESET_CATEGORIES.items()
                ],
                "total": len(rules),
            }

        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/gate-rules")
async def add_gate_rule(rule: GateRule):
    try:
        if not rule.pattern or not rule.pattern.strip():
            raise HTTPException(status_code=400, detail="命中模式(pattern)不能为空")
        if rule.match_type not in ("keyword", "regex"):
            raise HTTPException(status_code=400, detail="match_type 仅支持 keyword / regex")

        cat, label = _normalize_category(rule.category, rule.category_label)

        def _work():
            store = _api.get_store()
            rid = store.add_gate_rule(
                category=cat,
                category_label=label,
                name=(rule.name or "").strip(),
                match_type=rule.match_type,
                pattern=rule.pattern.strip(),
                intercept_message=(rule.intercept_message or "").strip(),
                priority=rule.priority,
                enabled=rule.enabled,
            )
            return rid

        rule_id = await run_in_threadpool(_work)
        from src.gate.reply_gate import invalidate_gate_cache
        invalidate_gate_cache()
        return {"success": True, "id": rule_id, "message": "门禁规则添加成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/gate-rules/stats")
async def gate_rules_stats():
    try:
        def _work():
            return _api.get_store().gate_rules_stats()
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/gate-rules/{rule_id}")
async def get_gate_rule(rule_id: int):
    try:
        def _work():
            store = _api.get_store()
            rule = store.get_gate_rule(rule_id)
            if not rule:
                raise HTTPException(status_code=404, detail="规则不存在")
            return {"rule": rule}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put("/api/gate-rules/{rule_id}")
async def update_gate_rule(rule_id: int, update: GateRuleUpdate):
    try:
        def _work():
            store = _api.get_store()
            existing = store.get_gate_rule(rule_id)
            if not existing:
                raise HTTPException(status_code=404, detail="规则不存在")
            data = {k: v for k, v in update.model_dump().items() if v is not None}
            if "pattern" in data and not str(data["pattern"]).strip():
                raise HTTPException(status_code=400, detail="命中模式(pattern)不能为空")
            if "match_type" in data and data["match_type"] not in ("keyword", "regex"):
                raise HTTPException(status_code=400, detail="match_type 仅支持 keyword / regex")
            # 分类归一化（若同时改了分类/标签）
            if "category" in data or "category_label" in data:
                cat = data.get("category", existing.get("category", "other"))
                lbl = data.get("category_label", existing.get("category_label", ""))
                data["category"], data["category_label"] = _normalize_category(cat, lbl)
            if data:
                store.update_gate_rule(rule_id, **data)
            return {"success": True, "message": "门禁规则更新成功"}
        result = await run_in_threadpool(_work)
        from src.gate.reply_gate import invalidate_gate_cache
        invalidate_gate_cache()
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete("/api/gate-rules/{rule_id}")
async def delete_gate_rule(rule_id: int):
    try:
        def _work():
            store = _api.get_store()
            store.delete_gate_rule(rule_id)
            return {"success": True, "message": "门禁规则删除成功"}
        result = await run_in_threadpool(_work)
        from src.gate.reply_gate import invalidate_gate_cache
        invalidate_gate_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/gate-rules/{rule_id}/toggle")
async def toggle_gate_rule(rule_id: int, update: GateRuleUpdate):
    """单条启用/停用切换（enabled=1/0）。"""
    try:
        def _work():
            store = _api.get_store()
            existing = store.get_gate_rule(rule_id)
            if not existing:
                raise HTTPException(status_code=404, detail="规则不存在")
            new_enabled = 1 if not update.enabled else 0
            # update.enabled 为 None 时按「取反」语义：未传则基于当前状态翻转
            if update.enabled is None:
                new_enabled = 0 if existing.get("enabled") else 1
            store.update_gate_rule(rule_id, enabled=new_enabled)
            return {
                "success": True,
                "enabled": new_enabled,
                "message": "已启用" if new_enabled else "已停用",
            }
        result = await run_in_threadpool(_work)
        from src.gate.reply_gate import invalidate_gate_cache
        invalidate_gate_cache()
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/gate-rules/batch")
async def batch_gate_rule_ops(body: GateRuleBatchOp):
    try:
        def _work():
            store = _api.get_store()
            count = 0
            if body.action == "enable":
                for rid in body.ids:
                    store.update_gate_rule(rid, enabled=1)
                    count += 1
            elif body.action == "disable":
                for rid in body.ids:
                    store.update_gate_rule(rid, enabled=0)
                    count += 1
            elif body.action == "delete":
                for rid in body.ids:
                    store.delete_gate_rule(rid)
                    count += 1
            else:
                raise HTTPException(status_code=400, detail=f"不支持的操作: {body.action}")
            return count
        count = await run_in_threadpool(_work)
        from src.gate.reply_gate import invalidate_gate_cache
        invalidate_gate_cache()
        return {"success": True, "count": count, "message": f"批量操作完成，处理 {count} 条"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/gate-rules/test-match")
async def test_gate_match(body: GateRuleMatchTest):
    """对用户提交的提问做门禁命中测试（force=True 读最新规则）。"""
    try:
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="请输入测试文本")

        def _work():
            from src.gate.reply_gate import evaluate_gate_rules
            store = _api.get_store()
            result = evaluate_gate_rules(store, text, force=body.force)
            return {
                "success": True,
                "input": body.text,
                "blocked": result.blocked,
                "category": result.category,
                "category_label": result.category_label,
                "rule_id": result.rule_id,
                "rule_name": result.rule_name,
                "match_type": result.match_type,
                "pattern": result.pattern,
                "intercept_message": result.intercept_message,
            }

        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
