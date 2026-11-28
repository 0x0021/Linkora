"""工作时间外待办回执：LLM 文案生成 / 严格校验 / 固定文案回退。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config_models.system import UnrepliedTodoConfig
from src.offhours_todo.notice_reply import (
    DEFAULT_SENDER_NOTICE_TEXT,
    generate_notice_text,
    normalize_notice_text,
    validate_notice_text,
)
from src.offhours_todo.scanner import UnansweredItem
from src.offhours_todo.service import OffhoursTodoService

_VALID = "收到你的消息，我已记入待办，恢复工作后会查看并回复，感谢理解～"


class _FakeLLM:
    def __init__(self, content: str = _VALID, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    def chat(self, messages: list[dict], **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content)


def _item() -> UnansweredItem:
    return UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天能给我回复吗？", timestamp="2026-11-22T20:05:00",
        reason="weekend", dedupe_key="dingtalk:chat-1:m1",
    )


def _service(llm: object, notice_text: str = DEFAULT_SENDER_NOTICE_TEXT) -> OffhoursTodoService:
    cfg = UnrepliedTodoConfig(sender_notice_text=notice_text)
    engine = SimpleNamespace(llm_client=llm)
    return OffhoursTodoService(cfg, engine, {"dingtalk": SimpleNamespace(store=None)})


# ---------------- 文案校验 ----------------

def test_valid_notice_passes_validation():
    assert validate_notice_text(_VALID) == ""
    assert generate_notice_text(_FakeLLM()) == _VALID


def test_normalize_strips_model_wrappers():
    assert normalize_notice_text("  “ 收到，已记入待办，恢复工作后回复，谢谢～” ") == (
        "收到，已记入待办，恢复工作后回复，谢谢～"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",                                                            # 空
        "收到，已记入待办，恢复工作后回复，谢谢" * 5,                     # 过长
        "收到，已记入待办，明天上午回复，谢谢",                          # 具体时间
        "收到，已记入待办，恢复工作后 3 小时内回复，谢谢",                # 数字时长
        "收到，问题已处理完成，谢谢",                                    # 声称已处理
        "收到，已记入待办，恢复工作后回复，请再发一遍资料，谢谢",          # 引导额外操作
        "收到，已记入待办，恢复工作后回复，谢谢。另外记得打卡",            # 无关内容
        "收到，[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSwUj8)，谢谢",  # 媒体引用
        "你好，在吗？",                                                  # 方向完全不对
        "收到，已记入待办，谢谢",                                        # 缺「恢复工作后」
    ],
)
def test_invalid_notice_is_rejected(text: str):
    assert validate_notice_text(text) != ""


# ---------------- 生成失败 / 校验失败 → 固定文案 ----------------

def test_missing_llm_falls_back_to_fixed_text():
    service = _service(None, notice_text="固定回执文案")
    assert service._resolve_notice_text(_item()) == "固定回执文案"


def test_llm_exception_falls_back_to_fixed_text():
    llm = _FakeLLM(error=RuntimeError("key expired"))
    service = _service(llm)
    assert service._resolve_notice_text(_item()) == DEFAULT_SENDER_NOTICE_TEXT
    assert llm.calls == 1


@pytest.mark.parametrize(
    "bad_text",
    [
        "收到，已记入待办，明天上午回复，谢谢",
        "收到，问题已解决，谢谢",
        "收到，已记入待办，恢复工作后回复，请补充订单号，谢谢",
        "收到，已记入待办，恢复工作后回复，谢谢。顺便看下考勤",
    ],
)
def test_invalid_llm_output_never_reaches_sender(bad_text: str):
    llm = _FakeLLM(content=bad_text)
    service = _service(llm, notice_text="固定回执文案")
    adapter = MagicMock()

    assert service._notify_sender(_item(), SimpleNamespace(store=None), adapter) is True

    adapter.chat_message_send.assert_called_once()
    assert adapter.chat_message_send.call_args.kwargs["text"] == "固定回执文案"
    assert service._resolve_notice_text(_item()) == "固定回执文案"


def test_valid_llm_output_is_sent_to_sender():
    service = _service(_FakeLLM())
    adapter = MagicMock()

    assert service._notify_sender(_item(), SimpleNamespace(store=None), adapter) is True

    adapter.chat_message_send.assert_called_once()
    assert adapter.chat_message_send.call_args.kwargs["text"] == _VALID
    assert adapter.chat_message_send.call_args.kwargs["conversation_id"] == "chat-1"
