"""仪表盘实时数据聚合端点（F-H6：合并多路轮询为单通道 + 增量游标）。

前端原本对 /api/logs、/api/decisions、/api/messages 各起独立 setInterval
（2s / 5s / 5s ≈ 54 req/min），且 loadDashboardData 内又单独 fetch 一次
decisions，存在冗余。本端点一次性返回三路增量数据，前端改为单 setInterval
（5s）拉取，请求量降到 ≈ 12 req/min。

- 增量游标：last_log_id / last_message_id 由前端持有时钟，后端只回传增量。
- 复用现有 router handler 逻辑，不重复实现业务。
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web.routers.conversations import messages as _messages_handler
from web.routers.decisions import recent_decisions as _recent_decisions
from web.routers.logs import get_logs as _get_logs

router = APIRouter()


@router.get("/api/dashboard/stream-data")
async def dashboard_stream_data(
    last_message_id: int = 0,
    last_log_id: int = 0,
    log_level: str = "info",
    decisions_n: int = 2,
    decisions_platform: str = "",
    platform: str = "all",
):
    """合并仪表盘实时流所需的三路数据，支持增量游标。

    - logs：复用 /api/logs（platform=all 保留全局概览，与仪表盘实时面板一致）。
    - decisions：复用 /api/decisions（默认 n=2，按当前平台过滤可选）。
    - messages：复用 /api/messages（只看最新若干条，由前端按游标过滤出新增）。
    """
    # 日志：复用 /api/logs 逻辑（get_logs 返回 JSONResponse，body 为 bytes|memoryview）
    log_resp = await _get_logs(level=log_level, since=last_log_id, limit=300, platform=platform)
    if isinstance(log_resp, JSONResponse):
        log_payload: dict = json.loads(bytes(log_resp.body).decode("utf-8"))
    else:
        log_payload = {}

    # 决策：复用 /api/decisions
    decision_payload = await _recent_decisions(n=decisions_n, platform=decisions_platform)

    # 最近消息：复用 /api/messages（dashboard 实时流只看最新若干条）
    msg_payload = await _messages_handler(chat_id="", limit=10)
    msgs = (msg_payload or {}).get("messages") or []

    new_msgs = [m for m in msgs if (m.get("id") or 0) > last_message_id]
    max_message_id = max((m.get("id") or 0) for m in msgs) if msgs else last_message_id

    return {
        "logs": log_payload,
        "decisions": decision_payload,
        "messages": new_msgs,
        "max_message_id": max_message_id,
    }
