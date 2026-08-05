"""草稿审阅路由：列出 / 审批 / 编辑 / 丢弃低置信度草稿。

从 dead_letters.py 参考结构，业务逻辑独立。
共享符号（get_store / get_app_instance / logger）统一从 `web.dependencies` 导入。
"""
from __future__ import annotations

from web.dependencies import get_app_instance, get_store, logger, run_sync
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class EditDraftBody(BaseModel):
    final_reply: str


@router.get("/api/drafts")
async def list_drafts(status: str | None = None, limit: int = 50, offset: int = 0, platform: str | None = None):
    """列出草稿，支持 status / limit / offset / platform 查询参数。"""
    try:
        def _work():
            store = get_store()
            status_ = None if status == "all" else status
            limit_ = max(1, min(limit, 500))
            offset_ = max(0, offset)
            items, total = store._draft_repo.list_drafts(status=status_, platform=platform, limit=limit_, offset=offset_)
            pending_count = store._draft_repo.count_pending_drafts(platform=platform)
            return {"success": True, "items": items, "total": total, "count": len(items), "pending_count": pending_count}
        return await run_sync(_work)
    except Exception as e:
        logger.error("获取草稿列表失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/api/drafts/count")
async def count_pending_drafts():
    """获取待处理草稿总数。"""
    try:
        def _work():
            store = get_store()
            return {"success": True, "pending_count": store._draft_repo.count_pending_drafts()}
        return await run_sync(_work)
    except Exception as e:
        logger.error("获取草稿计数失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: str):
    """获取单条草稿详情。"""
    try:
        def _work():
            store = get_store()
            draft = store._draft_repo.get_draft(draft_id)
            if not draft:
                raise HTTPException(status_code=404, detail="draft not found")
            return {"success": True, "draft": draft}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("获取草稿详情失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

def _send_draft_reply(draft: dict, final_reply: str) -> dict:
    """通过对应平台适配器发送消息。

    Args:
        draft: 草稿 dict（含 platform / chat_id / chat_type / sender_id 等字段）
        final_reply: 最终要发送的文本内容

    Returns:
        适配器返回的 dict
    """
    app_instance = get_app_instance()
    if app_instance is None or not hasattr(app_instance, "platforms"):
        raise HTTPException(status_code=500, detail="应用实例不可用，无法发送消息")

    platform = draft.get("platform", "dingtalk")
    ctx = app_instance.platforms.get(platform)
    if ctx is None or ctx.dws is None:
        raise HTTPException(status_code=500, detail=f"平台 {platform} 不可用，无法发送消息")

    chat_id = draft.get("chat_id", "")
    if not chat_id:
        raise HTTPException(status_code=400, detail="草稿缺少 chat_id")

    # 【提示词泄漏纵深防御】草稿内容在生成侧已经过 enforce_brevity 清洗，
    # 但 edit 入口允许人工改写、旧库存草稿可能生成于清洗规则上线前——
    # 发送出口统一再洗一遍（幂等，干净文本零改动）。
    try:
        from src.llm.style import sanitize_reply
        cleaned = sanitize_reply(final_reply)
        if cleaned != final_reply:
            logger.warning(
                "[sanitize 草稿发送] 草稿含提示词泄漏痕迹，已清洗: %d -> %d 字符 (draft chat_id=%s)",
                len(final_reply), len(cleaned), chat_id,
            )
            if not cleaned:
                raise HTTPException(status_code=400, detail="草稿内容全部为内部推理痕迹，已拦截不发送")
            final_reply = cleaned
    except HTTPException:
        raise
    except Exception:
        logger.warning("[resilience] 草稿发送清洗失败，按原文发送", exc_info=True)

    chat_type = draft.get("chat_type", "")
    sender_id = draft.get("sender_id", "")

    # 平台感知分派：参照 runtime._send_reply 的分发逻辑，不再硬编码钉钉参数
    if chat_type == "group":
        logger.info("[草稿发送] 群聊: group=%s", chat_id)
        return ctx.dws.chat_message_send(group=chat_id, text=final_reply)

    # 单聊：按 chat_id 格式判断发送参数
    if str(chat_id).startswith("oc_"):
        # 外部好友（跨租户）：DWS 用 --group 传 oc_xxx（与 runtime._send_reply 对齐）
        logger.info("[草稿发送] 外部好友（跨租户）: group=%s", chat_id)
        return ctx.dws.chat_message_send(group=chat_id, text=final_reply)

    if sender_id and str(sender_id).startswith("ou_"):
        # 单聊内部用户：用 sender_id 作为 open_dingtalk_id 回复
        logger.info("[草稿发送] 单聊（sender_id）: open_dingtalk_id=%s", sender_id)
        return ctx.dws.chat_message_send(open_dingtalk_id=sender_id, text=final_reply)

    if str(chat_id).startswith("ou_"):
        logger.info("[草稿发送] 单聊（chat_id oid）: open_dingtalk_id=%s", chat_id)
        return ctx.dws.chat_message_send(open_dingtalk_id=chat_id, text=final_reply)

    # cid 格式：钉钉单聊 openConversationId，dws 无法直接按 --user 解析，
    # 需从 conversations 表查到对方的 openDingTalkId（与 runtime._send_reply 对齐）
    if str(chat_id).startswith("cid"):
        try:
            store = get_store()
            conv = store._conversation_repo.get_conversation(chat_id)
            peer_oid = (conv or {}).get("peer_open_dingtalk_id", "") or ""
            peer_user_id = (conv or {}).get("peer_user_id", "") or ""
            if peer_oid:
                logger.info("[草稿发送] 单聊（查库 peer_oid）: open_dingtalk_id=%s", peer_oid)
                return ctx.dws.chat_message_send(open_dingtalk_id=peer_oid, text=final_reply)
            if peer_user_id:
                logger.info("[草稿发送] 单聊（查库 peer_user_id）: user=%s", peer_user_id)
                return ctx.dws.chat_message_send(user=peer_user_id, text=final_reply)
        except Exception:
            logger.warning("[草稿发送] 查库 conversation 失败", exc_info=True)

    # 兜底：chat_id 可能是 userId 或其他格式
    logger.info("[草稿发送] 单聊（兜底）: user=%s", chat_id)
    return ctx.dws.chat_message_send(user=chat_id, text=final_reply)


@router.post("/api/drafts/{draft_id}/approve")
async def approve_draft(draft_id: str):
    """审批通过草稿：发送原始 AI 回复并标记为 approved。"""
    try:
        def _work():
            store = get_store()
            draft = store._draft_repo.get_draft(draft_id)
            if not draft:
                raise HTTPException(status_code=404, detail="draft not found")
            if draft.get("status") != "pending":
                raise HTTPException(status_code=400, detail="draft already processed")

            final_reply = draft.get("ai_reply", "")
            send_result = _send_draft_reply(draft, final_reply)

            store._draft_repo.resolve_draft(draft_id, "approved", final_reply=final_reply, notes="管理台审批通过")
            logger.info("[草稿] 审批通过 draft_id=%s", draft_id)
            return {"success": True, "draft_id": draft_id, "send_result": send_result}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("审批草稿失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/api/drafts/{draft_id}/discard")
async def discard_draft(draft_id: str):
    """丢弃草稿（标记为 discarded，不发送）。"""
    try:
        def _work():
            store = get_store()
            draft = store._draft_repo.get_draft(draft_id)
            if not draft:
                raise HTTPException(status_code=404, detail="draft not found")
            if draft.get("status") != "pending":
                raise HTTPException(status_code=400, detail="draft already processed")

            ok = store._draft_repo.resolve_draft(draft_id, "discarded", notes="管理台手动丢弃")
            if not ok:
                raise HTTPException(status_code=404, detail="not_found")
            logger.info("[草稿] 已丢弃 draft_id=%s", draft_id)
            return {"success": True, "draft_id": draft_id}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("丢弃草稿失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/api/drafts/{draft_id}/edit")
async def edit_draft(draft_id: str, body: EditDraftBody):
    """编辑并发送草稿：使用修改后的文本发送并标记为 edited。"""
    try:
        def _work():
            store = get_store()
            draft = store._draft_repo.get_draft(draft_id)
            if not draft:
                raise HTTPException(status_code=404, detail="draft not found")
            if draft.get("status") != "pending":
                raise HTTPException(status_code=400, detail="draft already processed")

            final_reply = body.final_reply.strip()
            if not final_reply:
                raise HTTPException(status_code=400, detail="final_reply 不能为空")

            send_result = _send_draft_reply(draft, final_reply)

            store._draft_repo.resolve_draft(draft_id, "edited", final_reply=final_reply, notes="管理台编辑后发送")
            logger.info("[草稿] 编辑发送 draft_id=%s", draft_id)
            return {"success": True, "draft_id": draft_id, "send_result": send_result}
        return await run_sync(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("编辑草稿失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
