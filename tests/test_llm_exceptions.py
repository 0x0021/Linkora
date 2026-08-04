"""LLM 异常类单元测试 — 覆盖 __init__ / __str__ 及不同 stage。"""
from __future__ import annotations

from src.llm.exceptions import LLMProcessingError


def test_default_stage():
    e = LLMProcessingError("主模型调用失败")
    assert e.message == "主模型调用失败"
    assert e.stage == "llm_inference"
    assert e.original is None


def test_custom_stage():
    e = LLMProcessingError("工具执行超时", stage="tool_exec")
    assert e.stage == "tool_exec"


def test_with_original_exception():
    orig = RuntimeError("连接被拒")
    e = LLMProcessingError("调用失败", original=orig)
    assert e.original is orig


def test_str_method():
    e = LLMProcessingError("测试消息")
    assert str(e) == "测试消息"
