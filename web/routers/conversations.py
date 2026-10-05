"""会话列表 / 消息浏览 路由。

从 `web/api.py` 抽取（原 631–787 行），业务逻辑不变。
- get_store / get_dws / _get_cfg 经 `import web.api as _api` 做属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch。
- 私有 helper `_resolve_missing_image_path` 随迁到本模块（仅本路由使用）。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

import web.api as _api
from src.config import DEFAULT_TMP_IMAGES_DIR
from src.image_path import safe_path_component
from src.memory.message_repo import MessageRepo
from web.dependencies import logger, get_current_platform, get_app_instance
from web.errors import SAFE_OPERATION_FAILED

router = APIRouter()


def _resolve_current_user() -> tuple[str, str]:
    """解析当前登录用户的 id / name（可能调用 DWS CLI，须离开事件循环执行）。

    H1-2026-08-08：原实现直接在 async 视图里调用 dws._get_current_profile_local()
    / dws.contact_user_get_self()（subprocess CLI），阻塞事件循环。改为由调用方
    经 run_in_threadpool 在 worker 线程执行本函数。返回 (current_user_id, current_user_name)。
    """
    current_user_id = ""
    current_user_name = ""
    try:
        platform = get_current_platform()
        inst = get_app_instance()
        if inst and hasattr(inst, "platforms") and platform in inst.platforms:
            adapter = inst.platforms[platform].dws
            if adapter:
                try:
                    if hasattr(adapter, '_get_current_profile_local'):
                        profile = adapter._get_current_profile_local()
                        if profile:
                            # ★ P-1 修复：补全 current_user_id 提取（同时兼容 openDingTalkId/userid/staffId 三种字段名）
                            current_user_id = (
                                profile.get("openDingTalkId", "")
                                or profile.get("userid", "")
                                or profile.get("staffId", "")
                                or profile.get("userId", "")
                            )
                            if not current_user_name:
                                current_user_name = profile.get("userName", "")
                    if (not current_user_id or not current_user_name) and hasattr(adapter, 'contact_user_get_self'):
                        user = adapter.contact_user_get_self()
                        if user:
                            # 兜底：拉 contact_user_get_self 返回的 orgEmployeeModel
                            emp = user.get("orgEmployeeModel", {}) or {}
                            if not current_user_id:
                                current_user_id = (
                                    user.get("openDingTalkId", "")
                                    or emp.get("userid", "")
                                    or emp.get("staffId", "")
                                    or ""
                                )
                            if not current_user_name:
                                current_user_name = emp.get("orgUserName", "") or user.get("name", "")
                except Exception as _e:
                    logger.warning("broad except swallowed in _resolve_current_user() @ web/routers/conversations.py:73, see exc_info", exc_info=True)
                    _ = _e  # 取当前用户失败则保留空值
        else:
            dws = _api.get_dws()
            profile = dws._get_current_profile_local()
            if profile:
                current_user_id = (
                    profile.get("openDingTalkId", "")
                    or profile.get("userid", "")
                    or profile.get("staffId", "")
                    or profile.get("userId", "")
                )
                if not current_user_name:
                    current_user_name = profile.get("userName", "")
            if not current_user_id or not current_user_name:
                user = dws.contact_user_get_self()
                if user:
                    emp = user.get("orgEmployeeModel", {}) or {}
                    if not current_user_id:
                        current_user_id = (
                            user.get("openDingTalkId", "")
                            or emp.get("userid", "")
                            or emp.get("staffId", "")
                            or ""
                        )
                    if not current_user_name:
                        current_user_name = emp.get("orgUserName", "") or user.get("name", "")
    except Exception as _e:
        logger.warning("broad except swallowed in _resolve_current_user() @ web/routers/conversations.py:100, see exc_info", exc_info=True)
        _ = _e  # 取当前用户失败则保留空值
    return current_user_id, current_user_name


@router.get("/api/conversations")
async def conversations(limit: int = 50):
    try:
        limit = max(1, min(limit, 500))
        def _work():
            store = _api.get_store()
            platform = get_current_platform()
            rows = store._conversation_repo.list_conversations_with_preview(
                limit=limit, platform=platform
            )

            result = []
            for d in rows:
                if 'last_message_time' in d and 'last_message_at' not in d:
                    d['last_message_at'] = d.pop('last_message_time')

                # 回填缺失的会话名 / 消息数（优先用单次查询已取回的值）
                if not d.get('chat_name'):
                    d['chat_name'] = (d.get('peer_name') or d['chat_id'])
                if not d.get('message_count'):
                    d['message_count'] = d.get('real_count') or 0

                result.append(d)
            return {"conversations": result}
        return await run_in_threadpool(_work)
    except Exception as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e


@router.get("/api/messages")
async def messages(
    background_tasks: BackgroundTasks = None,  # type: ignore[arg-type]
    chat_id: str = "",
    limit: int = 50,
):
    try:
        limit = max(1, min(limit, 500))
        if background_tasks is None:
            background_tasks = BackgroundTasks()

        # H1-2026-08-08：DWS 身份解析涉及 subprocess CLI，移出事件循环到 worker 线程
        current_user_id, current_user_name = await run_in_threadpool(_resolve_current_user)

        # 标准化（strip 兼容前后空格差异）
        current_user_id = (current_user_id or "").strip()
        current_user_name = (current_user_name or "").strip()

        def _work():
            store = _api.get_store()
            rows = store._message_repo.list_messages_with_chat_name(
                chat_id=chat_id, limit=limit, platform=get_current_platform()
            )
            messages = []
            pending_backfills: list[tuple[str, str, str]] = []
            for d in rows:
                chat_name = d.get('chat_name') or ''
                # ★ P-1 修复：方向判断 (sender_name OR sender_id 都对得上 → out)
                #   之前只判 sender_name，遇到 current_user_name 解析失败就全错位。
                #   79 条「OWNER」手发消息 role='user'，前端 isMe 误判为"对方"就是这原因。
                sender_name_key = (d.get('sender_name') or '').strip()
                sender_id_key = (d.get('sender_id') or '').strip()
                is_out = (
                    (current_user_name and sender_name_key == current_user_name)
                    or (current_user_id and sender_id_key == current_user_id)
                )
                d['direction'] = 'out' if is_out else 'in'
                if is_out:
                    d['receiver_name'] = chat_name
                else:
                    d['receiver_name'] = current_user_name or ''
                # 构造图片 URL + 飞书卡片图多图映射
                # 飞书消息卡片（interactive / post）里的图走新通道：image_path 存
                # JSON {"img_v3_xxx": "飞书智能助手/card_xxx.png", ...}；前端 _renderCardBody
                # 按映射命中后渲染真图，json 解析失败时静默降级。
                img_path = d.get('image_path') or ''
                img_path_map: dict[str, str] = {}
                if img_path and img_path.startswith('{'):
                    try:
                        img_path_map = json.loads(img_path)
                        if not isinstance(img_path_map, dict):
                            img_path_map = {}
                    except (json.JSONDecodeError, TypeError):
                        img_path_map = {}
                if img_path and not img_path.startswith('{') and not img_path_map:
                    d['image_url'] = f"/api/image/{img_path}"
                elif img_path_map:
                    d['image_path_map'] = img_path_map
                elif d.get('msg_type') == 'image':
                    # 兜底：image 类型但 image_path 为空（OCR 回调可能未回写）
                    # 尝试按发送者名称 + 时间戳从磁盘匹配最近的图片文件
                    fallback_path = _resolve_missing_image_path(d)
                    d['image_url'] = f"/api/image/{fallback_path}" if fallback_path else ""
                    # 收集待回填：写动作延迟到请求返回后由后台线程执行
                    # （见 _flush_image_backfills），读路径不同步写会话库，
                    # 避免争用写锁、阻塞响应。回填本身幂等（只写一次、走写闸门）。
                    if fallback_path:
                        pending_backfills.append(
                            (d.get('msg_id') or "", fallback_path, get_current_platform())
                        )
                else:
                    d['image_url'] = ""
                messages.append(d)

            return {
                "messages": messages,
                "current_user_name": current_user_name,
                "current_user_id": current_user_id,  # ★ P-1 顺手回传，前端可双保险
            }, pending_backfills
        result, pending = await run_in_threadpool(_work)
        # GET 读路径不同步写库：请求返回后由后台线程回填图片路径（幂等、走写闸门）
        if pending:
            background_tasks.add_task(_flush_image_backfills, pending)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e


@router.post("/api/messages/batch-delete")
async def batch_delete_messages(payload: dict):
    """批量删除会话的消息记录（messages 页多选批量删除的后端补齐）。

    前端 messages.js 一直调用本端点（多选会话 → 批量删除），但后端从未实现，
    属「前端功能死链」：点击必 404。按 chat_id 删除会话及其消息/摘要/去重记录
    （不可恢复，前端已有 confirm 二次确认）。
    """
    try:
        raw_ids = payload.get("chat_ids") or []
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(status_code=400, detail="chat_ids 必须为非空数组")
        if len(raw_ids) > 200:
            raise HTTPException(status_code=400, detail="单次最多删除 200 个会话")
        chat_ids = [str(c) for c in raw_ids]
        def _work():
            store = _api.get_store()
            platform = get_current_platform()
            n = store._conversation_repo.delete_conversations(chat_ids, platform)
            return {"deleted": n}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e


def _flush_image_backfills(pending: list[tuple[str, str, str]]) -> None:
    """读路径图片路径回填的收尾：请求返回后在后台线程写库。

    ``/api/messages`` 是 GET 读路径，不应在请求关键路径内同步写会话库
    （争用写锁 + 阻塞响应）。此处把回填动作延迟到响应返回后由 FastAPI
    BackgroundTasks 在后台线程执行。回填本身经 ``store._lock``（叠加全局写闸门），
    幂等（仅当 image_path 为空才写入），失败留痕而非静默吞掉。
    """
    if not pending:
        return
    try:
        store = _api.get_store()
        repo = store._message_repo
        for msg_id, image_path, platform in pending:
            try:
                repo.backfill_missing_image_path(msg_id, image_path, platform)
            except Exception as e:  # noqa: BLE001
                logger.warning("图片路径后台回填失败 msg_id=%s: %s", msg_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("图片路径后台回填初始化失败: %s", e)


def _resolve_missing_image_path(msg: dict) -> str:
    """为 image 类型但 image_path 为空的消息，从磁盘匹配图片文件回填。

    匹配策略（先后两路，任一命中即返回相对路径）：
      1) 新结构：扫 ``<image_temp_dir>/*/*/<chat_id>/``（平台/账号段通配），
         按文件修改时间距离消息时间戳最近（容差 ±600 秒）的图；
      2) 旧结构兼容：``<image_temp_dir>/<sender_name>/`` 下同样按时间匹配
         （历史库未迁移时的兜底）。
    命中后回填 DB 避免下次重复计算。
    """
    import re
    from datetime import datetime
    cfg = _api._get_cfg()
    image_temp_dir = DEFAULT_TMP_IMAGES_DIR
    if cfg and getattr(cfg, 'poller', None):
        image_temp_dir = getattr(cfg.poller, 'image_temp_dir', image_temp_dir)

    sender = msg.get('sender_name') or ''
    chat_id = msg.get('chat_id') or ''
    ts_str = msg.get('timestamp') or ''
    if not ts_str:
        return ''
    try:
        msg_ts = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return ''

    base = Path(image_temp_dir).expanduser()

    def _best_under(directory: Path, tol: int = 600) -> "Path | None":
        if not directory.is_dir():
            return None
        best = None
        best_gap = 999999
        for f in sorted(directory.iterdir(), key=lambda p: p.name, reverse=True):
            if not f.is_file():
                continue
            if f.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bin'):
                continue
            f_ts = datetime.fromtimestamp(f.stat().st_mtime)
            gap = abs((f_ts - msg_ts).total_seconds())
            if gap < best_gap and gap <= tol:
                best_gap = gap
                best = f
        return best

    # 新结构：按 chat_id 通配定位（平台/账号段不确定，glob 两层）
    if chat_id:
        for cand in base.glob(f"*/*/{safe_path_component(chat_id)}"):
            hit = _best_under(cand)
            if hit:
                return str(hit.relative_to(base)).replace('\\', '/')

    # 旧结构兼容：按发送者名称目录
    if sender:
        safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', sender)[:40]
        hit = _best_under(base / safe_name)
        if hit:
            return str(hit.relative_to(base)).replace('\\', '/')
    return ''


@router.get("/api/messages/export")
async def export_messages(chat_id: str = "", limit: int = 1000):
    """导出消息记录为 CSV（utf-8-sig BOM，Excel 兼容）。"""
    try:
        limit = max(1, min(limit, 10000))

        def _work():
            store = _api.get_store()
            return store._message_repo.export_messages(
                chat_id=chat_id, limit=limit, platform=get_current_platform()
            )

        rows = await run_in_threadpool(_work)

        output = io.StringIO()
        output.write('\ufeff')  # BOM
        writer = csv.writer(output)
        # 列定义与顺序以 MessageRepo.EXPORT_COLUMNS 为准，避免表头与查询列漂移。
        writer.writerow(MessageRepo.EXPORT_COLUMNS)
        for r in rows:
            writer.writerow([r[k] for k in MessageRepo.EXPORT_COLUMNS])

        output.seek(0)
        date_tag = datetime.now().strftime('%Y%m%d')
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=messages_{date_tag}.csv"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=SAFE_OPERATION_FAILED) from e
