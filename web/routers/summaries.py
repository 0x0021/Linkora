"""对话摘要聚合端点（供 Web「对话摘要」页实时展示）。

- items：近期 per-conversation 摘要列表（chat_name / summary_text / covered_count / updated_at），
  按 updated_at 倒序，单次 JOIN 查询取回。
- digest：复用 ``src.llm.proactive_digest.build_digest`` 拼装的每日汇总文本，
  与 proactive 每日 17:30 推送给主人的内容**完全同源**（当 window=today 时）。
- 多平台隔离：platform 缺省时读取请求上下文（由 web.api 平台中间件设置）。
- 时间窗口过滤：window 参数支持 "today"/"yesterday"/"7days"，默认 "today"。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query

from web.dependencies import get_current_platform, get_store_dep, run_sync
from src.llm.proactive_digest import build_digest
from src.memory.conversation_repo import _since_iso

logger = logging.getLogger("web.api")

router = APIRouter()

_WINDOW_LABELS = {
    "today": "今日",
    "yesterday": "昨日",
    "7days": "近七天",
}
_DEFAULT_WINDOW = "today"


@router.get("/api/summaries")
async def get_summaries(
    limit: int = Query(30, ge=1, le=200),
    # 默认放宽到 600：卡片正文已支持多段显示，200 会把较长摘要（多日期滚动摘要常
    # 超 200 字）在句中腰斩成「…」，看起来像内容缺失。
    max_chars: int = Query(600, ge=20, le=2000),
    window: str = Query(_DEFAULT_WINDOW, pattern="^(today|yesterday|7days)$"),
    platform: str | None = Query(default=None),
    store=Depends(get_store_dep),
):
    """近期对话摘要 + 每日聚合摘要文本。"""
    pf = platform or get_current_platform()
    try:
        since = await run_sync(_since_iso, window)
        rows = await run_sync(
            store._conversation_repo.list_recent_summaries, limit, pf, since,
        )
        items = [
            {
                "chat_id": r["chat_id"],
                "chat_name": r.get("chat_name") or "",
                "summary": r["summary_text"] or "",
                "covered_count": int(r.get("covered_count") or 0),
                "updated_at": r.get("updated_at") or "",
                "platform": r.get("platform") or "",
            }
            for r in rows
        ]
        digest = build_digest(items, max_summary_chars=max_chars)
        return {
            "ok": True,
            "platform": pf,
            "window": window,
            "count": len(items),
            "items": items,
            "digest": digest,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    except Exception:  # noqa: BLE001
        # 禁止把内部异常细节暴露给客户端（CodeQL py/stack-trace-exposure）。
        logger.exception("[摘要] 读取失败 platform=%s window=%s", pf, window)
        return {
            "ok": False,
            "error": "摘要数据读取失败，请稍后重试",
            "platform": pf,
            "window": window,
            "count": 0,
            "items": [],
            "digest": "",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

