"""死信队列路由：列出 / 重放 / 丢弃死信消息。

从 `web/api.py` 抽取（原 697–737 行），业务逻辑不变。
共享符号（get_store / get_app_instance / logger）统一从 `web.dependencies`
导入，避免与 api.py 的挂载产生循环导入。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from src.memory.draft_repo import DraftRepo
from web.dependencies import get_app_instance, get_store, get_current_platform, logger, run_sync
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/api/dead-letters")
async def dead_letters(status: str = "pending", limit: int = 100, offset: int = 0, platform: str = ""):
    """列出死信消息（默认仅 pending）。支持分页（offset/limit），返回总数 total。"""
    try:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        def _work():
            store = get_store()
            return store._draft_repo.list_dead_letters(status=status, limit=limit, offset=offset)
        items, total = await run_sync(_work)
        return {"success": True, "items": items, "count": len(items), "total": total}
    except Exception as e:
        logger.error("获取死信列表失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/dead-letters/batch-replay")
async def batch_replay_dead_letters():
    """一键重放全部 pending 死信，返回成功/失败计数与明细。"""
    app_instance = get_app_instance()
    if app_instance is None or not hasattr(app_instance, "replay_dead_letter"):
        raise HTTPException(status_code=500, detail="应用实例不可用，无法批量重放")

    def _work():
        store = get_store()
        return store._draft_repo.list_dead_letters(status="pending", limit=10000, offset=0)
    try:
        items, total = await run_sync(_work)
    except Exception as e:
        logger.error("批量重放-取pending列表失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    if not items:
        return {"success": True, "total": 0, "replayed": 0, "failed": 0, "message": "没有待处理的死信"}

    pid = get_current_platform()

    def _replay_all():
        detail: list[dict] = []
        success_count, fail_count = 0, 0
        for item in items:
            dl_id = item["id"]
            try:
                result = app_instance.replay_dead_letter(dl_id, platform=pid)
                if result.get("success"):
                    success_count += 1
                    detail.append({"id": dl_id, "status": "replayed"})
                else:
                    fail_count += 1
                    detail.append({"id": dl_id, "status": "failed", "error": result.get("error", "")})
            except Exception as e:
                fail_count += 1
                detail.append({"id": dl_id, "status": "failed", "error": str(e)[:200]})
        return detail, success_count, fail_count

    # 重放本身含 DB + LLM + 网络调用，逐条同步执行会长时间阻塞事件循环，整体放线程池。
    detail, success_count, fail_count = await run_sync(_replay_all)

    return {
        "success": True,
        "total": len(items),
        "replayed": success_count,
        "failed": fail_count,
        "detail": detail,
    }


@router.post("/api/dead-letters/{dl_id}/replay")
async def replay_dead_letter(dl_id: int):
    """重放一条死信消息：取出原文重新走处理流程。

    【Phase 3 多平台】透传当前 ?platform= 上下文，确保重放走对应平台 store/dws/llm_agent。
    """
    app_instance = get_app_instance()
    if app_instance is None or not hasattr(app_instance, "replay_dead_letter"):
        raise HTTPException(status_code=500, detail="应用实例不可用，无法重放")
    result = await run_sync(app_instance.replay_dead_letter, dl_id,
                            platform=get_current_platform())
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "replay_failed"))
    return {"success": True, "id": dl_id}


@router.post("/api/dead-letters/{dl_id}/discard")
async def discard_dead_letter(dl_id: int):
    """丢弃一条死信消息（标记为 discarded，不再重放）。"""
    try:
        def _work():
            store = get_store()
            return store._draft_repo.resolve_dead_letter(dl_id, status="discarded", note="管理台手动丢弃")
        ok = await run_sync(_work)
        if not ok:
            raise HTTPException(status_code=404, detail="not_found")
        return {"success": True, "id": dl_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("丢弃死信失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── 导出死信队列为 CSV ─────────────────────────────────────────────────
# 列定义与顺序以 DraftRepo.DEAD_LETTER_EXPORT_COLUMNS 为准，避免表头与查询列漂移。
_DL_CSV_COLS = list(DraftRepo.DEAD_LETTER_EXPORT_COLUMNS)


@router.get("/api/dead-letters/export")
async def export_dead_letters(status: str = "all", limit: int = 10000):
    """导出死信队列为 CSV（utf-8-sig BOM，Excel 兼容）。"""
    try:
        limit = max(1, min(limit, 20000))

        def _work():
            store = get_store()
            return store._draft_repo.export_dead_letters(status=status, limit=limit)
        rows = await run_sync(_work)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(_DL_CSV_COLS)
        for r in rows:
            writer.writerow([r[k] for k in _DL_CSV_COLS])

        output.seek(0)
        date_tag = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=dead_letters_{date_tag}.csv"},
        )
    except Exception as e:
        logger.error("死信导出API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
