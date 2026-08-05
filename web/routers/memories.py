"""记忆读写路由。

从 `web/api.py` 抽取（原 2048–2113 行），业务逻辑不变。
- get_store 取自 `web.dependencies`；
- MemoryItem / MemoryUpdate 模型原定义于 api.py，随本模块一并迁入（仅被本路由使用）。
支持「公共记忆 / 个人记忆」范围(scope)的增删改查与筛选。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from web.dependencies import get_store

router = APIRouter()


class MemoryItem(BaseModel):
    content: str
    source: str = ""
    chat_id: str = ""
    scope: str | None = None  # 'public' / 'personal'，不传则按算法自动判定


class MemoryUpdate(BaseModel):
    content: str | None = None
    scope: str | None = None


@router.get("/api/memories")
async def memories(limit: int = 200, object_type: str = "all",
                   sender: str = "", keyword: str = "", scope: str = "all"):
    try:
        def _work():
            store = get_store()
            limit_ = max(1, min(limit, 500))
            memories_list = store._memory_repo.get_memories_filtered(
                object_type=object_type, sender=sender, keyword=keyword,
                limit=limit_, scope=scope)
            return {"memories": memories_list}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/api/memories/facets")
async def memory_facets():
    """记忆筛选用的 facets：对象类型计数 + 范围(scope)计数 + 可搜索/选择的具体人列表。"""
    try:
        def _work():
            store = get_store()
            return store._memory_repo.get_memory_facets()
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/api/memories/classify-spec")
async def memory_classify_spec():
    """返回记忆范围自动分类算法的规则说明，供前端「分类规则」卡片渲染。"""
    try:
        from src.memory.classifier import ALGORITHM_SPEC
        return {"spec": ALGORITHM_SPEC}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/memories")
async def add_memory(item: MemoryItem):
    try:
        def _work():
            store = get_store()
            import hashlib
            key = "mem_" + hashlib.md5(item.content.encode("utf-8")).hexdigest()[:12]
            # 范围判定：显式传入 scope 优先；否则按算法自动判断（手动添加且无归属 → 公共）。
            scope = item.scope
            if scope not in ("public", "personal"):
                try:
                    from src.memory.classifier import classify_memory_scope
                    scope, _ = classify_memory_scope(item.content, source="manual")
                except Exception:
                    scope = "personal"
            memory_id = store._memory_repo.save_memory(
                key=key, content=item.content,
                source=item.source or "manual", chat_id=item.chat_id,
                scope=scope,
            )
            return {"success": True, "memory_id": memory_id, "scope": scope, "message": "记忆添加成功"}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.delete("/api/memories/{mem_id}")
async def delete_memory(mem_id: int):
    try:
        def _work():
            store = get_store()
            store._memory_repo.delete_memory(mem_id)
            return {"success": True, "message": "记忆删除成功"}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.put("/api/memories/{mem_id}")
async def update_memory(mem_id: int, update: MemoryUpdate):
    try:
        def _work():
            store = get_store()
            store._memory_repo.update_memory(mem_id, content=update.content, scope=update.scope)
            return {"success": True, "message": "记忆更新成功"}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
