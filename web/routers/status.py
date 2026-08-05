"""系统状态总览路由（DB 统计 + 配置 + DWS 身份）。

从 `web/api.py` 抽取（原 560–623 行），业务逻辑不变。
- load_config / CONFIG_PATH / get_store / get_dws 经 `import web.api as _api`
  做属性访问，以尊重测试对 `web.api.*` 的 monkeypatch（TestStatus patch get_dws）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

import web.api as _api
from web.dependencies import logger, get_current_platform, get_app_instance


def _get_git_info() -> dict:
    """获取当前运行代码的 Git 版本信息（运行时采集，不依赖打包）。"""
    cwd = Path(_api.CONFIG_PATH).parent
    try:
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            .strip()
        )
    except Exception:
        commit = "unknown"
    try:
        branch = (
            subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            .strip()
        )
    except Exception:
        branch = "unknown"
    return {"commit": commit, "branch": branch}

router = APIRouter()


@router.get("/api/status")
async def status():
    # 配置缺失时抛 503（放在 try 外：HTTPException 是 Exception 子类，
    # 落进下方 except 会被压平成语义错误的 500）。
    config = _api._require_cfg()
    try:
        def _db_counts():
            store = _api.get_store()
            platform = get_current_platform()
            msg_count = store._message_repo.count_messages(platform=platform)
            conv_count = store._conversation_repo.count_conversations(platform=platform)
            mem_count = store._memory_repo.count_memories()
            kw_count = store.count_keyword_rules(enabled=1)
            kb_count = store._kb_repo.count_kb_documents()
            try:
                dd_count = store._docs_repo.count_dingtalk_docs()
            except Exception:
                dd_count = 0
            dl_count = store._draft_repo.count_dead_letters(status="pending")
            return msg_count, conv_count, mem_count, kw_count, kb_count, dd_count, dl_count
        (msg_count, conv_count, mem_count, kw_count, kb_count, dd_count,
         dl_count) = await run_in_threadpool(_db_counts)

        user_name = "N/A"
        try:
            platform = get_current_platform()
            inst = get_app_instance()
            if inst and hasattr(inst, "platforms") and platform in inst.platforms:
                adapter = inst.platforms[platform].dws
                if adapter:
                    try:
                        if hasattr(adapter, '_get_current_profile_local'):
                            profile = adapter._get_current_profile_local()
                            if profile and profile.get("userName"):
                                user_name = profile.get("userName", "N/A")
                        if user_name == "N/A" and hasattr(adapter, 'contact_user_get_self'):
                            user = adapter.contact_user_get_self()
                            if user:
                                user_name = (user.get("orgEmployeeModel") or {}).get("orgUserName", "") or \
                                            user.get("name", "N/A")
                    except Exception:
                        pass
            else:
                dws = _api.get_dws()
                profile = dws._get_current_profile_local()
                if profile and profile.get("userName"):
                    user_name = profile.get("userName", "N/A")
                else:
                    user = dws.contact_user_get_self()
                    user_name = (user.get("orgEmployeeModel") or {}).get("orgUserName", "N/A")
        except Exception as e:
            err_str = str(e)
            if "TOKEN_VERIFIED_FAILED" in err_str or "Token 验证失败" in err_str:
                user_name = "个人用户"
            else:
                user_name = "N/A"

        # 平台级健康状态（轻量，不额外调用 CLI）
        platforms_status: dict = {}
        try:
            inst = get_app_instance()
            if inst and hasattr(inst, "platforms"):
                for pid, ctx in inst.platforms.items():
                    platforms_status[pid] = {
                        "enabled": getattr(ctx, "enabled", False),
                        "display_name": getattr(ctx, "display_name", pid),
                        "poller_running": getattr(getattr(ctx, "poller", None), "running", False),
                    }
                # 补充最近轮询时间 / 错误（如有）
                if hasattr(inst, "get_poller_status"):
                    poller_summary = inst.get_poller_status()
                    for pid, pstatus in poller_summary.get("platforms", {}).items():
                        if pid in platforms_status:
                            platforms_status[pid].update({
                                "last_poll_at": pstatus.get("last_poll_at"),
                                "last_error": pstatus.get("last_error"),
                                "last_error_at": pstatus.get("last_error_at"),
                                "queue_depth": pstatus.get("queue_depth", 0),
                                "poll_count": pstatus.get("poll_count", 0),
                            })
        except Exception:
            logger.warning("平台健康状态读取失败", exc_info=True)

        return {
            "status": "running",
            "version": _get_git_info(),
            "config": {
                "dry_run": config.dws.dry_run,
                "poll_interval": config.poller.interval_seconds,
                "llm_model": config.llm.model,
                "embedding_enabled": config.embedding.enabled,
                "embedding_model": config.embedding.model,
                "tools_count": len(config.tools.available),
            },
            "stats": {
                "messages": msg_count,
                "conversations": conv_count,
                "memories": mem_count,
                "keyword_rules": kw_count,
                "kb_documents": kb_count,
                "dingtalk_docs": dd_count,
                "dead_letters": dl_count,
            },
            "platforms": platforms_status,
            "user": {
                "name": user_name,
            },
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("状态错误: %s", e)
        return {"status": "error", "error": str(e)}

@router.get("/api/health")
async def health():
    """轻量级健康检查端点，供负载均衡/探针使用。

    不查询数据库，仅返回服务是否存活及基本运行信息。
    """
    try:
        inst = get_app_instance()
        running = inst is not None and getattr(inst, "_running", False)
        return {
            "status": "healthy" if running else "degraded",
            "running": running,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error("健康检查错误: %s", e)
        return {"status": "unhealthy", "error": str(e)}


@router.get("/api/config-drift")
async def config_drift():
    """返回 config.yaml tools.available 白名单与代码实际注册工具的差异。

    当有人在 BUILTIN_TOOL_MANIFEST 加了新工具却忘了同步 config.yaml 时，
    此端点暴露「missing_in_whitelist」以可视化方式显示，防 26→23 式漂移重演。
    """
    try:
        from web.dependencies import get_app_instance as _get_app
        app_inst = _get_app()
        if app_inst is None:
            return {"available": False, "reason": "bot 未就绪"}
        if not hasattr(app_inst, "get_tool_whitelist_drift"):
            return {"available": False, "reason": "方法不可用"}
        return {"available": True, **app_inst.get_tool_whitelist_drift()}
    except Exception as e:
        logger.error("配置漂移API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
