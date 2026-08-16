"""QA 独立验证 · P0-2 + P0-3：限频异常不被处理错误吞掉；跨模块 catch 真生效。

P0-2.2（真实调用链）：直接驱动 runtime_llm_reply._process_llm_reply 的分诊 except 链，
证明 LLMRateLimitExhaustedError 走「不回复」分支（DLQ + 标记，绝不套 default_fallback），
LLMProcessingError 在 DLQ 关闭时走「兜底回复」分支——二者语义红线不被继承关系破坏。

P0-2.3：两个 LLM 异常的 __str__ / args 与裸 Exception 行为一致（只传单个 message，
不注入 context，日志格式不变）。

P0-3：src.im_adapter.errors 的异常能被 src.exceptions.IMAdapterError 统一捕获；
7 个子类继承关系未被改坏；src.exceptions 不反向依赖 src.im_adapter（无循环导入）；
两模块类名字面量交集仅 {IMAdapterError}（防「同名不同族」事故）。
"""
from __future__ import annotations

import importlib
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import src.exceptions as src_exc
import src.im_adapter.errors as im_errors
from src.exceptions import LinkoraError
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError
from src.platform.runtime_llm_reply import LLMReplyMixin


# ───────────────────────── P0-2.2 真实分诊链 ─────────────────────────

def _make_triage_host(dlq_enabled: bool):
    host = LLMReplyMixin.__new__(LLMReplyMixin)
    host.store = SimpleNamespace(
        _message_repo=SimpleNamespace(
            get_conversation_history=MagicMock(return_value=[])
        )
    )
    host.llm_agent = SimpleNamespace(process_message=MagicMock())
    host.config = SimpleNamespace(
        poller=SimpleNamespace(
            history_window=10, history_days=7, history_session_gap_minutes=30
        ),
        dead_letter=SimpleNamespace(enabled=dlq_enabled),
        safety=SimpleNamespace(default_fallback="兜底文案"),
    )
    host._send_reply = MagicMock(return_value=True)
    host._enqueue_dead_letter = MagicMock()
    host._mark_inbound_processed = MagicMock()
    return host


def _msg():
    return SimpleNamespace(
        msg_id="m1", chat_id="c1", chat_name="peer", sender_id="u1",
        sender_name="peer", content="hi", msg_type="text", raw={},
    )


def test_rate_limit_goes_to_no_reply_path():
    """限频异常：绝不向用户回复，只入 DLQ + 标记去重。"""
    host = _make_triage_host(dlq_enabled=True)
    host.llm_agent.process_message.side_effect = LLMRateLimitExhaustedError(
        "all models 429", stage="rate_limit"
    )
    host._process_llm_reply(_msg(), SimpleNamespace(intent="", action="llm"))
    # 关键：不向用户发送任何回复（含 default_fallback）
    host._send_reply.assert_not_called()
    # 计入死信队列
    host._enqueue_dead_letter.assert_called_once()
    # 标记已处理避免重复轮询
    host._mark_inbound_processed.assert_called_once()


def test_processing_error_dlq_disabled_falls_back_to_reply():
    """真崩溃 + DLQ 关闭：走兜底回复分支（与限频「不回复」形成对照）。"""
    host = _make_triage_host(dlq_enabled=False)
    host.llm_agent.process_message.side_effect = LLMProcessingError(
        "主备均失败", stage="llm_inference"
    )
    host._process_llm_reply(_msg(), SimpleNamespace(intent="", action="llm"))
    # 必须向用户发送 default_fallback 兜底
    host._send_reply.assert_called_once_with(_msg(), "兜底文案")
    # 此时不落 DLQ
    host._enqueue_dead_letter.assert_not_called()
    host._mark_inbound_processed.assert_called_once()


# ───────────────────────── P0-2.3 str/args 行为 ─────────────────────────

def test_llm_error_str_and_args_unchanged():
    """LLM 异常 __str__ 即 message，e.args == (message,)，与裸 Exception 一致。"""
    e1 = LLMProcessingError("推理失败", stage="tool_exec")
    assert str(e1) == "推理失败"
    assert e1.args == ("推理失败",)
    assert e1.context == {}  # 不注入 context → LinkoraError.__str__ 不会追加 (code: context)

    e2 = LLMRateLimitExhaustedError("全模型限频", original=RuntimeError("429"))
    assert str(e2) == "全模型限频"
    assert e2.args == ("全模型限频",)
    assert e2.context == {}


# ───────────────────────── P0-3 跨模块 catch / 继承 / 无循环导入 ─────────────────────────

def test_cross_module_catch_by_src_exceptions_root():
    """src.im_adapter 抛出的具体异常能被 src.exceptions.IMAdapterError 统一捕获。"""
    caught = None
    try:
        raise im_errors.IMAdapterRetryableError("临时故障，需退避重试")
    except src_exc.IMAdapterError as e:
        caught = e
    assert isinstance(caught, src_exc.IMAdapterError)
    assert isinstance(caught, im_errors.IMAdapterError)
    assert isinstance(caught, LinkoraError)

    # 子类同样可捕获
    caught2 = None
    try:
        raise im_errors.IMAdapterPermissionError("token 失效")
    except src_exc.IMAdapterError as e:
        caught2 = e
    assert isinstance(caught2, im_errors.IMAdapterPermissionError)


def test_im_hierarchy_preserved():
    """7 个具体子类的继承关系未被改坏。"""
    # 平级兄弟：Retryable 与 NonRetryable 互不继承
    assert not issubclass(im_errors.IMAdapterRetryableError, im_errors.IMAdapterNonRetryableError)
    assert not issubclass(im_errors.IMAdapterNonRetryableError, im_errors.IMAdapterRetryableError)
    # 各下游子类归位
    assert issubclass(im_errors.IMAdapterRateLimitError, im_errors.IMAdapterRetryableError)
    assert issubclass(im_errors.IMAdapterNonRetryableError, im_errors.IMAdapterError)
    assert issubclass(im_errors.IMAdapterPermissionError, im_errors.IMAdapterNonRetryableError)
    assert issubclass(im_errors.IMAdapterResourceNotFoundError, im_errors.IMAdapterNonRetryableError)
    assert issubclass(im_errors.IMAdapterUnsupportedTypeError, im_errors.IMAdapterNonRetryableError)
    assert issubclass(im_errors.IMAdapterShutdownError, im_errors.IMAdapterNonRetryableError)
    assert issubclass(im_errors.IMAdapterError, LinkoraError)


def test_no_circular_import():
    """src.exceptions 不得反向依赖 src.im_adapter（否则循环导入）。"""
    text = pathlib.Path(src_exc.__file__).read_text()
    assert "from src.im_adapter" not in text
    assert "import src.im_adapter" not in text


def test_class_name_intersection_only_imadapter():
    """两模块类名字面量交集仅 {IMAdapterError}（防同名不同族事故）。"""
    exc_classes = {
        n for n, o in src_exc.__dict__.items()
        if isinstance(o, type) and o.__module__ == src_exc.__name__
    }
    im_classes = {
        n for n, o in im_errors.__dict__.items()
        if isinstance(o, type) and o.__module__ == im_errors.__name__
    }
    assert exc_classes & im_classes == {"IMAdapterError"}


def test_im_adapter_errors_import_resolves_via_exceptions():
    """im_adapter.errors 内部 from src.exceptions import ... 不会形成循环导入；
    其 IMAdapterError 的基类正是 src.exceptions.IMAdapterError（别名解析正确）。"""
    # 直接断言基类身份（证明别名 _LinkoraIMAdapterError 解析到根类，而非遮蔽自身）
    assert im_errors.IMAdapterError.__bases__[0] is src_exc.IMAdapterError
    assert issubclass(im_errors.IMAdapterError, src_exc.IMAdapterError)
