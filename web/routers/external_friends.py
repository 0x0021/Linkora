"""外部好友路由：列出 / 添加 / 删除外部好友。

从 `web/api.py` 抽取（原 1369–1413 行），业务逻辑不变。
`ExternalFriendCreate` 模型内聚于此模块（仅本路由使用）；通过 `import web.api as _api`
持有模块引用，运行时取 `get_store`，避免与 api.py 的挂载产生循环导入。
"""
from __future__ import annotations

from web.dependencies import get_store, run_sync
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel


class ExternalFriendCreate(BaseModel):
    name: str
    user_id: str
    chat_id: str = ""
    notes: str = ""


router = APIRouter()


@router.get("/api/external-friends")
async def list_external_friends(platform: str = Query(default="")):
    """列出所有外部好友。"""
    def _work():
        store = get_store(platform)
        return store._external_friend_repo.list_external_friends()
    result = await run_sync(_work)
    return {"success": True, "data": result}

@router.post("/api/external-friends")
async def add_external_friend(body: ExternalFriendCreate, platform: str = Query(default="")):
    """添加外部好友。"""
    def _work():
        store = get_store(platform)
        return store._external_friend_repo.add_external_friend(
            name=body.name,
            open_dingtalk_id=body.user_id,
            chat_id=body.chat_id,
            notes=body.notes,
        )
    ef = await run_sync(_work)
    try:
        return {"success": True, "data": ef, "message": f"已添加外部好友：{body.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.delete("/api/external-friends/{user_id}")
async def delete_external_friend(user_id: str, platform: str = Query(default="")):
    """删除外部好友。"""
    def _work():
        store = get_store(platform)
        return store._external_friend_repo.delete_external_friend(user_id)
    ok = await run_sync(_work)
    try:
        if not ok:
            raise HTTPException(status_code=404, detail="未找到该外部好友")
        return {"success": True, "message": "已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
