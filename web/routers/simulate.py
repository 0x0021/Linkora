"""模拟测试面板路由。

提供 API 供前端模拟发送消息，验证 LLM 技能调用和回复效果，
而不需要实际发送钉钉/飞书消息。
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Body
from fastapi.concurrency import run_in_threadpool

import web.api as _api
from src.models import Message
from web.dependencies import logger, get_current_platform

router = APIRouter()


@router.post("/api/simulate/message")
def simulate_message(
    content: str = Body(...),
    sender_name: str = Body("测试用户"),
    chat_id: str = Body("test-chat"),
    sender_id: str = Body("test-user"),
    enable_stream: bool = Body(False),
):
    """模拟发送一条消息，返回 LLM 处理结果。

    用于测试面板，不实际发送到钉钉/飞书，仅返回处理结果。

    声明为同步 `def`：函数体内 `agent.process_message` 是完整的同步 LLM 推理管线
    （秒级～分钟级），且读历史消息也是同步 DB 调用。Starlette 会自动把同步路由
    放进线程池，避免单次模拟把整个管理端 UI 卡死。
    """
    if not content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    platform_id = get_current_platform()

    try:
        app = _api.get_app_instance()
        if not app or not hasattr(app, "llm_agent"):
            raise HTTPException(status_code=500, detail="Agent 未初始化")

        agent = app.llm_agent
        if not agent:
            raise HTTPException(status_code=500, detail="LLM Agent 未初始化")

        msg = Message(
            msg_id=f"sim-{hash(content)}-{hash(str(__import__('time').time()))}",
            content=content,
            sender_name=sender_name,
            sender_id=sender_id,
            chat_id=chat_id,
            chat_type="private",
            chat_name=chat_id,
            msg_type="text",
            timestamp=__import__('datetime').datetime.now(),
            raw={},
            role="user",
        )

        history = []
        try:
            store = _api.get_store()
            if store:
                history = store._message_repo.get_conversation_history(chat_id, limit=10)
        except Exception:
            logger.debug("获取历史消息失败，使用空历史")

        result = agent.process_message(
            msg,
            history=history,
            enable_stream=enable_stream,
        )

        if hasattr(result, "__iter__") and not isinstance(result, str):
            accumulated_text = ""
            final_reply = None
            try:
                while True:
                    chunk = next(result)
                    if isinstance(chunk, str):
                        accumulated_text += chunk
                    else:
                        final_reply = chunk
            except StopIteration as e:
                if e.value is not None:
                    final_reply = e.value

            if final_reply and hasattr(final_reply, "text"):
                response = {
                    "success": True,
                    "text": final_reply.text or accumulated_text,
                    "routing_mode": final_reply.routing_mode or ("streaming" if enable_stream else ""),
                    "routed_tools": final_reply.routed_tools,
                    "skill_name": final_reply.skill_name,
                    "skill_source": final_reply.skill_source,
                    "confidence": final_reply.confidence,
                    "evidence_source": final_reply.evidence_source,
                    "already_sent": final_reply.already_sent,
                }
            else:
                response = {
                    "success": True,
                    "text": accumulated_text or str(final_reply) if final_reply else "",
                    "routing_mode": "streaming" if enable_stream else "",
                    "routed_tools": [],
                    "skill_name": "",
                    "skill_source": "",
                    "confidence": 0.0,
                    "evidence_source": "",
                    "already_sent": False,
                }
        else:
            response = {
                "success": True,
                "text": result.text,
                "routing_mode": result.routing_mode,
                "routed_tools": result.routed_tools,
                "skill_name": result.skill_name,
                "skill_source": result.skill_source,
                "confidence": result.confidence,
                "evidence_source": result.evidence_source,
                "already_sent": result.already_sent,
            }

        return response

    except Exception as e:
        logger.error("模拟消息处理失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")


@router.get("/api/simulate/sample-messages")
async def get_sample_messages():
    """获取预设的测试消息样本。"""
    return {
        "success": True,
        "samples": [
            {
                "name": "天气查询",
                "content": "北京今天天气怎么样？",
                "sender_name": "张三",
            },
            {
                "name": "知识问答",
                "content": "公司的 VPN 怎么配置？",
                "sender_name": "李四",
            },
            {
                "name": "闲聊",
                "content": "你好，今天忙吗？",
                "sender_name": "王五",
            },
            {
                "name": "工具调用",
                "content": "帮我查一下最近的待办事项",
                "sender_name": "赵六",
            },
            {
                "name": "复杂查询",
                "content": "帮我分析一下上周的销售数据，看看哪个产品卖得最好",
                "sender_name": "钱七",
            },
        ],
    }


@router.get("/api/simulate/status")
async def get_simulate_status():
    """获取模拟测试相关的系统状态信息。"""
    status = {
        "llm": {"available": False, "models": [], "active_model": ""},
        "skills": {"count": 0, "list": []},
        "system": {"version": "unknown"},
    }

    try:
        app = _api.get_app_instance()
        if app and hasattr(app, "llm_agent") and app.llm_agent:
            agent = app.llm_agent

            if hasattr(agent, "skill_manager") and agent.skill_manager:
                mgr = agent.skill_manager
                status["skills"]["count"] = len(mgr._skills)
                status["skills"]["list"] = [
                    {"name": s.name, "description": s.description, "tools": len(s.allowed_tools)}
                    for s in mgr._skills.values()
                ]

            if hasattr(agent, "client") and agent.client:
                client = agent.client
                status["llm"]["available"] = True
                all_models = []
                if hasattr(client, "model_pool") and client.model_pool:
                    all_models.extend(client.model_pool)
                if hasattr(client, "fallback_model_pool") and client.fallback_model_pool:
                    all_models.extend(client.fallback_model_pool)
                if hasattr(client, "secondary_fallback_model_pool") and client.secondary_fallback_model_pool:
                    all_models.extend(client.secondary_fallback_model_pool)
                status["llm"]["models"] = list(dict.fromkeys(all_models))
                if hasattr(client, "config") and client.config and hasattr(client.config, "model"):
                    status["llm"]["active_model"] = client.config.model

        import subprocess
        try:
            result = await run_in_threadpool(
                subprocess.run, ["git", "describe", "--tags", "--always"],
                capture_output=True, text=True, cwd=os.getcwd(),
            )
            status["system"]["version"] = result.stdout.strip() or "unknown"
        except Exception:
            pass

    except Exception as e:
        logger.error("获取模拟测试状态失败: %s", e)

    return {"success": True, "data": status}


_sim_history = []
_MAX_HISTORY = 20


@router.get("/api/simulate/history")
async def get_simulate_history():
    """获取最近的测试历史记录。"""
    return {"success": True, "history": _sim_history}


@router.post("/api/simulate/history")
async def save_simulate_history(
    content: str = Body(...),
    sender_name: str = Body(""),
    result_text: str = Body(""),
    routing_mode: str = Body(""),
    skill_name: str = Body(""),
):
    """保存一条测试历史记录。"""
    global _sim_history

    record = {
        "id": f"h-{hash(content)}-{hash(str(__import__('time').time()))}",
        "content": content,
        "sender_name": sender_name,
        "result_text": result_text[:200],
        "routing_mode": routing_mode,
        "skill_name": skill_name,
        "timestamp": __import__('datetime').datetime.now().isoformat(),
    }

    _sim_history.insert(0, record)
    if len(_sim_history) > _MAX_HISTORY:
        _sim_history = _sim_history[:_MAX_HISTORY]

    return {"success": True}