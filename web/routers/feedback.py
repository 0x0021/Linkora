from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.dependencies import get_store, run_sync

router = APIRouter()


class FeedbackItem(BaseModel):
    message_id: str = ""
    conversation_id: str = ""
    sender_id: str = ""
    rating: int = 0          # 1=有用 / -1=无用
    correction: str = ""     # 主人纠正的正确回复（可选）
    note: str = ""


@router.post("/api/feedback")
async def add_feedback(item: FeedbackItem):
    """记录一条对 AI 回复的反馈（评估闭环入口）。"""
    try:
        def _work():
            store = get_store()
            return store._feedback_repo.save_feedback(
                message_id=item.message_id,
                conversation_id=item.conversation_id,
                sender_id=item.sender_id,
                rating=item.rating,
                correction=item.correction or "",
                note=item.note or "",
            )
        fb_id = await run_sync(_work)
        return {"success": True, "feedback_id": fb_id, "message": "反馈已记录"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/feedback")
async def list_feedback(limit: int = 200):
    try:
        limit = max(1, min(limit, 500))
        def _work():
            store = get_store()
            return store._feedback_repo.get_feedback(limit=limit)
        rows = await run_sync(_work)
        return {"success": True, "feedback": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
