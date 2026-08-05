"""流式响应处理辅助函数。

从 src.llm.agent 拆出——处理流式 LLM 响应的占位消息发送、内容更新、收口逻辑。
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

logger = logging.getLogger(__name__)


def handle_stream_response(
    stream,
    message,
    im_adapter,
    enforce_brevity_fn,
    ensure_complete_fn,
    gate_reply_fn,
) -> Iterator[str]:
    """处理流式 LLM 响应，发送占位消息并逐步更新。

    参数:
        stream: LLM 流式响应迭代器
        message: 原始消息对象
        im_adapter: IM 适配器（用于发送/更新消息）
        enforce_brevity_fn: enforce_brevity 函数（接收 agent, reply）
        ensure_complete_fn: ensure_complete_reply 函数（接收 text, client, ...)
        gate_reply_fn: gate_reply 函数（接收 reply, user_name, user_title）
    """
    from src.llm.client import LLMStreamChunk

    accumulated_content = ""
    msg_id = None
    last_update_time = 0
    update_interval = 0.5

    try:
        for chunk in stream:
            if not isinstance(chunk, LLMStreamChunk):
                continue

            if chunk.content:
                accumulated_content += chunk.content

                if not msg_id:
                    placeholder = im_adapter.chat_message_send(
                        text="...",
                        group=message.chat_id,
                        user=message.sender_id,
                        ai_tag=True,
                    )
                    msg_id = placeholder.get("msgId") or placeholder.get("message_id")
                    logger.info("[流式输出] 已发送占位消息: msg_id=%s", msg_id)

                now = time.time()
                if now - last_update_time >= update_interval or len(accumulated_content) > 200:
                    if msg_id and accumulated_content.strip():
                        try:
                            text_to_update = enforce_brevity_fn(None, accumulated_content)
                            im_adapter.chat_message_update(
                                message_id=msg_id,
                                text=text_to_update,
                                group=message.chat_id,
                                user=message.sender_id,
                            )
                        except Exception as e:
                            logger.warning("[流式输出] 更新消息失败: %s", e)
                    last_update_time = now
                    yield accumulated_content

            if chunk.is_done:
                if msg_id and accumulated_content.strip():
                    final_text = enforce_brevity_fn(None, accumulated_content)
                    completed_text = ensure_complete_fn(final_text, None)
                    completed_text, _gated = gate_reply_fn(
                        completed_text, getattr(message, "sender_name", ""), "")
                    if _gated:
                        logger.info("[B闸门] 流式回复命中末端闸门，已整句替换为安全模板")
                    try:
                        im_adapter.chat_message_update(
                            message_id=msg_id,
                            text=completed_text,
                            group=message.chat_id,
                            user=message.sender_id,
                        )
                    except Exception as e:
                        logger.warning("[流式输出] 最终更新失败: %s", e)
                yield accumulated_content
                break

    except Exception as e:
        logger.error("[流式输出] 处理失败: %s", e, exc_info=True)
        if msg_id:
            try:
                recalled = im_adapter.chat_message_recall(
                    message_id=msg_id, group=message.chat_id, user=message.sender_id)
            except Exception as _exc:
                logger.warning(f"_handle_stream_response: swallowed exception: {_exc}")
                recalled = False
            if not recalled:
                try:
                    im_adapter.chat_message_update(
                        message_id=msg_id,
                        text="（回复生成中断，内容不完整，已停止）",
                        group=message.chat_id,
                        user=message.sender_id,
                    )
                except Exception as _exc:
                    logger.warning(f"_handle_stream_response: swallowed exception: {_exc}")
        raise
