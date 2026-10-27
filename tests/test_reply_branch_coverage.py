"""P3-3 整改：把「验证 mock」的两处测试改为**驱动真实生产分支**。

审计（P3-3）抽样复核发现两处真实盲区 —— 经**变异测试**证实：把生产代码的关键
判断改坏后，全仓库 4515 个用例**零报警**（失败列表与基线完全一致）。

盲区 1 —— 双重回复防护（``src/platform/runtime_llm_reply.py:134``）
---------------------------------------------------------
``tests/test_double_reply.py::test_main_skips_second_send_when_already_sent``
**自己重写了一遍生产消费逻辑**::

    if getattr(reply, "already_sent", False):
        pass  # 跳过发送
    elif reply.text:
        main_mod.LinkoraEngine._send_reply(app, _msg(), reply.text)

于是它验证的是「测试自己写的 if/else 是否正确」，而非生产代码的分支 ——
把生产侧的 ``if getattr(reply_text, "already_sent", False):`` 改成恒假，
该测试**依然全绿**，而线上会向同一会话发两条消息。

本文件改为**直接驱动生产方法** ``LLMReplyMixin._process_llm_reply``，
让生产分支成为被测对象。

盲区 2 —— 死信队列分支（``src/platform/runtime_llm_reply.py:155``）
------------------------------------------------------------------
``tests/test_qa_p0_exception_triage.py`` 覆盖了「限频 + DLQ 开启」与
「LLMProcessingError + DLQ 关闭」，但**没有**覆盖
「LLMProcessingError + DLQ **开启**」—— 即「死信入队且**不**向用户发兜底回复」
这条分支。把 ``_enqueue_dead_letter(...)`` 调用删掉，该分支失效不会被发现。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm.exceptions import LLMProcessingError
from src.platform.runtime_llm_reply import LLMReplyMixin


def _make_host(dlq_enabled: bool = True):
    """构造最小宿主（沿用 tests/test_qa_p0_exception_triage.py 的既有范式）。

    关键：**不**替换 ``_enqueue_dead_letter`` —— 用真实实现，只 mock 其下游
    ``store._draft_repo.add_dead_letter``，这样「是否入队」由生产代码决定。
    """
    host = LLMReplyMixin.__new__(LLMReplyMixin)
    host.store = SimpleNamespace(
        _message_repo=SimpleNamespace(get_conversation_history=MagicMock(return_value=[])),
        _draft_repo=SimpleNamespace(add_dead_letter=MagicMock()),
        _conversation_repo=SimpleNamespace(update_last_replied_msg_id=MagicMock()),
    )
    host.llm_agent = SimpleNamespace(process_message=MagicMock())
    host.config = SimpleNamespace(
        poller=SimpleNamespace(
            history_window=10, history_days=7, history_session_gap_minutes=30
        ),
        dead_letter=SimpleNamespace(enabled=dlq_enabled),
        safety=SimpleNamespace(default_fallback="兜底文案"),
    )
    host.poller = SimpleNamespace(_mark_msg_processed=MagicMock())
    host._send_reply = MagicMock(return_value=True)
    host._mark_inbound_processed = MagicMock()
    return host


def _msg():
    return SimpleNamespace(
        msg_id="m1", chat_id="c1", chat_name="peer", sender_id="u1",
        sender_name="peer", content="hi", msg_type="text", raw={},
    )


def _result():
    return SimpleNamespace(intent="", action="llm")


# ───────────────── 盲区 1：双重回复防护（驱动生产分支） ─────────────────

def test_already_sent_skips_second_send_via_production_branch():
    """生产分支：LLM 已自回复当前会话 → **不得**二次发送。

    与 ``tests/test_double_reply.py`` 的差异：这里**直接调用生产方法**
    ``_process_llm_reply``，由生产代码自己判断 ``already_sent``。
    若把 ``runtime_llm_reply.py:134`` 的判断改坏，本测试立即失败。
    """
    from src.llm.agent import AgentReply

    host = _make_host()
    host.llm_agent.process_message.return_value = AgentReply(
        text="已通知。", already_sent=True,
    )

    host._process_llm_reply(_msg(), _result())

    # 生产分支判定为「已自回复」→ 不发第二次
    host._send_reply.assert_not_called()


def test_not_already_sent_does_send_via_production_branch():
    """对照组：``already_sent=False`` → 走文本出站分支（发送一次）。

    与上一条构成对照，证明生产分支确实在起作用（而非「什么都不发」也绿）。
    """
    from src.llm.agent import AgentReply

    host = _make_host()
    host.llm_agent.process_message.return_value = AgentReply(
        text="今天天气不错。", already_sent=False,
    )
    # 出站分支的前置检查（内部确认 / 低置信转人工 / 引文页脚）
    host._is_internal_confirmation = MagicMock(return_value=False)
    host._should_handoff_low_confidence = MagicMock(return_value=False)
    host._append_citation_footer = MagicMock(side_effect=lambda text, *_a, **_k: text)
    host._mark_decision_cited = MagicMock()
    host._auto_save_memory = MagicMock()

    host._process_llm_reply(_msg(), _result())

    host._send_reply.assert_called_once()
    sent_text = host._send_reply.call_args.args[1]
    assert sent_text == "今天天气不错。"


def test_already_sent_marks_message_processed():
    """已自回复路径必须做去重标记（否则下一轮轮询重复自回复 → 循环刷屏）。

    这条断言直接盯住 ``_mark_agent_self_replied`` 的行为 —— 生产代码里
    ``already_sent`` 分支**只调**它，不调 ``_mark_inbound_processed``。
    """
    from src.llm.agent import AgentReply

    host = _make_host()
    host.llm_agent.process_message.return_value = AgentReply(
        text="已通知。", already_sent=True,
    )
    host._mark_agent_self_replied = MagicMock()

    host._process_llm_reply(_msg(), _result())

    host._mark_agent_self_replied.assert_called_once()
    # 已自回复路径**不**走「入站已处理」标记（两者语义不同，别混淆）
    host._mark_inbound_processed.assert_not_called()


# ───────────── 盲区 2：LLMProcessingError + DLQ 开启（死信入队） ─────────────

def test_processing_error_with_dlq_enabled_enqueues_and_does_not_fallback():
    """LLMProcessingError + DLQ **开启** → 入死信队列，且**不**发兜底回复。

    生产代码（runtime_llm_reply.py:155）::

        if isinstance(e, LLMProcessingError) and self.config.dead_letter.enabled:
            self._enqueue_dead_letter(message, stage=..., error=...)
            ...
            self._mark_inbound_processed(message)
        else:
            ... self._send_reply(message, self.config.safety.default_fallback)

    把 ``_enqueue_dead_letter(...)`` 调用删掉 → 死信丢失，本测试失败。
    """
    host = _make_host(dlq_enabled=True)
    host.llm_agent.process_message.side_effect = LLMProcessingError(
        "主备均失败", stage="llm_inference",
    )

    host._process_llm_reply(_msg(), _result())

    # 死信必须入队（用真实 _enqueue_dead_letter，只 mock 其下游 repo）
    host.store._draft_repo.add_dead_letter.assert_called_once()
    kwargs = host.store._draft_repo.add_dead_letter.call_args.kwargs
    assert kwargs["stage"] == "llm_inference"
    assert kwargs["msg_id"] == "m1"

    # **不发**兜底回复（避免给用户发无意义内容）
    host._send_reply.assert_not_called()

    # 必须标记已处理，否则每轮轮询重复入队刷屏
    host._mark_inbound_processed.assert_called_once()


def test_processing_error_with_dlq_enabled_uses_real_enqueue_path():
    """确认上面那条走的是**真实** ``_enqueue_dead_letter``（未被 mock 掉）。"""
    host = _make_host(dlq_enabled=True)
    # 刻意**不**替换 _enqueue_dead_letter —— 若实现被改成不调用它，
    # add_dead_letter 就不会被调用，上面那条断言即失败。
    host.llm_agent.process_message.side_effect = LLMProcessingError(
        "主备均失败", stage="llm_inference",
    )

    host._process_llm_reply(_msg(), _result())

    assert host.store._draft_repo.add_dead_letter.called


def test_processing_error_dlq_disabled_still_falls_back():
    """对照组：DLQ 关闭 → 走兜底回复（与 DLQ 开启形成对照）。"""
    host = _make_host(dlq_enabled=False)
    host.llm_agent.process_message.side_effect = LLMProcessingError(
        "主备均失败", stage="llm_inference",
    )

    host._process_llm_reply(_msg(), _result())

    host._send_reply.assert_called_once()
    assert host._send_reply.call_args.args[1] == "兜底文案"
    host.store._draft_repo.add_dead_letter.assert_not_called()


def test_connection_error_never_enters_dead_letter():
    """网络类瞬时故障（ConnectionError）**不进**死信队列。

    死信语义是「彻底失败、需人工重放」；网络抖动是瞬时的，入了 DLQ 会造成
    死信队列被瞬时故障淹没。
    """
    host = _make_host(dlq_enabled=True)
    host.llm_agent.process_message.side_effect = ConnectionError("网络抖动")

    host._process_llm_reply(_msg(), _result())

    host.store._draft_repo.add_dead_letter.assert_not_called()
    host._send_reply.assert_called_once()
    assert host._send_reply.call_args.args[1] == "兜底文案"
