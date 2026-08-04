from __future__ import annotations

import json
import logging
from datetime import datetime as _dt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm.agent import LLMAgent
    from src.models import Message

logger = logging.getLogger(__name__)


class ToolOrchestrator:
    """负责工具调用编排：tool_call 解析、执行、结果注入、防双重回复标记。"""

    def __init__(self, agent: "LLMAgent") -> None:
        self._agent = agent

    def execute_tool_calls(
        self, tool_calls: list[dict], message: "Message",
    ) -> tuple[list[dict], bool]:
        agent = self._agent
        results = []
        # 标记本轮是否有 send_message 成功发往【当前会话】——用于防双重回复
        self_sent_to_current_chat = False
        # 外联拦截开关（默认开启）：禁止 AI 主动联系当前对话之外的第三方。
        # 真值来源是 ToolsConfig（AppConfig.tools，运行期经 tool_router.config 注入）；
        # 兼容测试用 agent.config(LlmConfig) 无 tools 字段的情况，分层兜底。
        block_outbound = self._read_block_outbound(agent)
        for tc in tool_calls:
            tool_name = tc["name"]
            args = tc["args"]

            # 【外联护栏】开启时，拦截一切向"当前对话之外"的主动外联：
            #   - send_ding：本质跨会话强提醒，一律拦截；
            #   - send_message：仅放行发往当前会话（message.chat_id），其余（其他群/第三方
            #     单聊/占位符 chat_id）一律拒绝，避免 AI 幻觉编造联系人发起外联。
            if block_outbound and tool_name in ("send_ding", "send_message"):
                block_reason = self._outbound_block_reason(
                    tool_name, args, message.chat_id
                )
                if block_reason:
                    logger.warning(
                        "[外联护栏] 拦截 %s 外联：%s args=%s", tool_name, block_reason, args,
                    )
                    results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({
                            "success": False,
                            "result": None,
                            "error": block_reason,
                            "_tool": tool_name,
                            "_ts": _dt.now().isoformat(),
                        }, ensure_ascii=False),
                    })
                    continue

            # 自动注入 sender_id 到记忆相关工具，确保记忆跟人绑定
            if tool_name in ("recall_memory", "save_memory"):
                if not args.get("sender_id"):
                    args["sender_id"] = message.sender_id or ""
                if tool_name == "save_memory" and not args.get("sender_name"):
                    args["sender_name"] = message.sender_name or ""
                if not args.get("chat_id"):
                    args["chat_id"] = message.chat_id or ""

            logger.info("工具调用: %s(%s)", tool_name, args)

            # session_key 用于确认门控的会话隔离（同一会话的确认令牌不可跨会话使用）
            session_key = message.chat_id or message.sender_id or ""
            result = agent.tool_router.execute(tool_name, args, session_key=session_key)
            tool_output = {
                "success": result.success,
                "result": result.result if result.success else None,
                "error": result.error,
            }

            logger.info("工具结果: %s -> 成功=%s (耗时 %d 毫秒)",
                        tool_name, result.success, result.duration_ms)

            # 记录工具调用日志到数据库
            try:
                input_args_str = json.dumps(args, ensure_ascii=False) if args else ""
                output_result_str = json.dumps(tool_output, ensure_ascii=False) if tool_output else ""
                agent.store.log_tool_execution(
                    tool_name=tool_name,
                    input_args=input_args_str,
                    output_result=output_result_str,
                    success=result.success,
                    duration_ms=result.duration_ms,
                    error_message=result.error if not result.success else "",
                )
            except Exception as log_err:
                logger.warning("[ToolLog] 记录日志失败: %s", log_err)

            # 防双重回复：send_message 成功发往当前会话，标记 agent 已自回复
            if tool_name == "send_message" and getattr(result, "success", False):
                if args.get("chat_id") == message.chat_id:
                    self_sent_to_current_chat = True
                    logger.info("[防双重回复] send_message 已成功发往当前会话 %s", message.chat_id)

            try:
                content = json.dumps(tool_output, ensure_ascii=False)
            except (TypeError, ValueError) as e:
                logger.warning("[工具] 结果序列化失败 (%s): %s，降级为 str", tool_name, e)
                content = json.dumps({"result": str(tool_output.get("result", ""))}, ensure_ascii=False)

            # 注入时间戳用于过期检测（_check_stale_tool_results 使用）
            try:
                parsed = json.loads(content)
                parsed["_ts"] = _dt.now().isoformat()
                parsed["_tool"] = tool_name
                # kb_search: 注入强制约束，防止模型忽略工具结果凭空编造
                if tool_name == "kb_search":
                    parsed["_instruction"] = (
                        "以上是知识库搜索结果，它是你回答的唯一事实来源。"
                        "你的回复必须严格基于这些结果。如果结果为空或不包含答案，"
                        "只能回复'知识库中未找到相关信息'，严禁编造任何地址/IP/流程/配置/账号信息。"
                    )
                # confirm_required: 注入确认流程指令，让 LLM 知道如何引导用户
                # 并提醒用户在用户确认后重新调用工具携带 confirm_token
                _result = tool_output.get("result") or {}
                if (_result.get("status") == "confirm_required"):
                    _token = _result.get("confirm_token", "")
                    parsed["_instruction"] = (
                        f"[确认操作] 此操作需要用户确认，"
                        f"请先展示 result.preview 内容给用户看，"
                        f"然后请用户回复「确认」或「取消」。"
                        f"如果用户确认，请再次调用本工具并传入参数 "
                        f"confirm_token=\"{_token}\" 以完成实际执行。"
                        f"如果用户取消，请回复「已取消操作」并结束。"
                    )
                content = json.dumps(parsed, ensure_ascii=False)
            except Exception as _exc:
                logger.warning(f"execute_tool_calls: swallowed exception: {_exc}")
                pass

            results.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": content,
            })
        return results, self_sent_to_current_chat

    @staticmethod
    def _read_block_outbound(agent: "LLMAgent") -> bool:
        """从 ToolsConfig（运行期经 tool_router.config 注入）读取外联拦截开关。

        agent.config 是 LlmConfig（无 tools 字段），真正的 ToolsConfig 挂在
        tool_router.config 上；测试用 fake router 可能无 config，则回退到 True。
        """
        tools_cfg = getattr(getattr(agent, "tool_router", None), "config", None)
        if tools_cfg is not None:
            return bool(getattr(tools_cfg, "block_outbound_to_third_party", True))
        return True

    @staticmethod
    def _outbound_block_reason(tool_name: str, args: dict, current_chat_id: str | None) -> str | None:
        """返回非空的拦截原因时，该外联调用应被拒绝。

        send_ding：一律拦截。
        send_message：仅当目标是当前会话（message.chat_id）时放行；其余（其他群 /
        第三方单聊 / 占位符/空 chat_id）均拦截。
        """
        if tool_name == "send_ding":
            return ("已禁止主动外联：当前配置不允许 AI 通过 DING 联系第三方"
                    "（block_outbound_to_third_party=true）。如需转达，请改为在当前会话口述。")
        if tool_name == "send_message":
            chat_id = (args.get("chat_id") or "").strip()
            if not chat_id:
                return ("已禁止主动外联：send_message 缺少 chat_id，无法确认是发往当前会话，"
                        "已拒绝向外联发送。")
            if current_chat_id and chat_id != current_chat_id:
                return ("已禁止主动外联：send_message 目标 chat_id=%s 不是当前会话（%s），"
                        "不允许 AI 主动联系第三方。请改为在当前会话内口头转述。"
                        % (chat_id, current_chat_id))
        return None
