"""对话摘要聚合端点（供 Web「对话摘要」页实时展示）。

- items：近期 per-conversation 摘要列表（chat_name / summary_text / covered_count / updated_at），
  按 updated_at 倒序，单次 JOIN 查询取回。
- digest：复用 ``src.llm.proactive_digest.build_digest`` 拼装的「每日对话摘要（共 N 段）」文本，
  与 proactive 每日 17:30 推送给主人的内容**完全同源**。
- 多平台隔离：platform 缺省时读取请求上下文（由 web.api 平台中间件设置）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from web.dependencies import get_current_platform, get_store_dep, run_sync
from src.llm.proactive_digest import build_digest

logger = logging.getLogger("web.api")

router = APIRouter()


@router.get("/api/summaries")
async def get_summaries(
    limit: int = Query(30, ge=1, le=200),
    max_chars: int = Query(200, ge=20, le=2000),
    platform: str | None = Query(default=None),
    store=Depends(get_store_dep),
):
    """近期对话摘要 + 每日聚合摘要文本。"""
    pf = platform or get_current_platform()
    try:
        rows = await run_sync(store._conversation_repo.list_recent_summaries, limit, pf)
        items = [
            {
                "chat_id": r["chat_id"],
                "chat_name": r.get("chat_name") or "",
                "summary": r["summary_text"],
                "covered_count": r.get("covered_count") or 0,
                "updated_at": r.get("updated_at") or "",
            }
            for r in rows
        ]
        digest = build_digest(items, max_summary_chars=max_chars)
        return {
            "ok": True,
            "platform": pf,
            "count": len(items),
            "items": items,
            "digest": digest,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("[摘要] 读取失败 platform=%s: %s", pf, e)
        return {
            "ok": False,
            "error": str(e),
            "platform": pf,
            "count": 0,
            "items": [],
            "digest": "",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
