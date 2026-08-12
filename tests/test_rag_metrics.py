"""RAG 指标采集器的单元测试 + 打点前后行为一致性校验。

仅验证「计数正确」与「打点不改变 clean/split 返回值」，不依赖真实 LLM。
"""
from __future__ import annotations

import importlib

import pytest

import src.rag_metrics as rag_metrics
from src.tools.utils import clean_document_for_rag, split_text
from src.rag_metrics import snapshot, reset, record_clean, record_chunk


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """每个用例前重置全局计数器，避免相互污染。"""
    reset()
    yield
    reset()


def test_reset_clears_all():
    record_clean(fallback=False, chars_in=100, chars_out=90, duration_ms=5.0)
    reset()
    assert snapshot() == {
        "clean_calls": 0, "clean_llm_ok": 0, "clean_fallback_regex": 0,
        "clean_chars_in": 0, "clean_chars_out": 0, "clean_duration_ms": 0.0,
        "clean_fallback_rate": 0.0, "clean_avg_chars_out": 0.0,
        "chunk_calls": 0, "chunk_total": 0, "chunk_avg": 0.0,
    }


def test_record_clean_ok_and_fallback_counts():
    record_clean(fallback=False, chars_in=200, chars_out=180, duration_ms=12.0)
    record_clean(fallback=True, chars_in=200, chars_out=150, duration_ms=3.0)
    record_clean(fallback=False, chars_in=50, chars_out=48, duration_ms=1.0)
    s = snapshot()
    assert s["clean_calls"] == 3
    assert s["clean_llm_ok"] == 2
    assert s["clean_fallback_regex"] == 1
    assert s["clean_chars_in"] == 450
    assert s["clean_chars_out"] == 378
    assert s["clean_duration_ms"] == 16.0
    # 派生指标（snapshot 会四舍五入：回退率取 3 位、均值取 1 位）
    assert s["clean_fallback_rate"] == round(1 / 3, 3)
    assert s["clean_avg_chars_out"] == round(378 / 3, 1)


def test_record_chunk_counts():
    record_chunk(count=0)
    record_chunk(count=5)
    record_chunk(count=3)
    s = snapshot()
    assert s["chunk_calls"] == 3
    assert s["chunk_total"] == 8
    assert s["chunk_avg"] == round(8 / 3, 1)


def test_negative_inputs_clamped():
    # 防御：负数不应让累计变成负
    record_clean(fallback=False, chars_in=-100, chars_out=-50, duration_ms=-9.0)
    s = snapshot()
    assert s["clean_chars_in"] == 0
    assert s["clean_chars_out"] == 0
    assert s["clean_duration_ms"] == 0.0
    assert s["clean_calls"] == 1


def test_clean_without_llm_does_not_record_but_returns_preclean():
    """未启用 LLM 时：clean 直接返回正则预清洗结果，且不应记 LLM 成功/回退。"""
    text = "<p>  你好  </p>\n\n<div>世界</div>"
    out = clean_document_for_rag(text, enable_llm=False, llm_client=None)
    assert "你好" in out and "世界" in out
    assert "<p>" not in out  # 预清洗确实跑了
    assert snapshot()["clean_calls"] == 0  # 未进 LLM 分支，不计数


def test_split_text_records_chunk_count_and_preserves_output():
    """打点不改变 split_text 返回值，且块数被正确记录。

    注意 split_text 默认 overlap=50 会把上块尾部接入下块，因此断言「块数=3、
    三句内容均保留」，而非硬编码具体字符串（避免与 overlap 实现耦合）。
    """
    text = "第一句。第二句。第三句。"
    chunks = split_text(text, max_len=5)
    assert len(chunks) == 3
    joined = "".join(chunks)
    assert "第一句" in joined and "第二句" in joined and "第三句" in joined
    assert snapshot()["chunk_total"] == len(chunks)
    assert snapshot()["chunk_calls"] == 1


def test_split_text_empty_records_zero():
    assert split_text("") == []
    assert snapshot()["chunk_total"] == 0
    assert snapshot()["chunk_calls"] == 1


def test_module_is_reimport_safe():
    """模块可重复导入且不崩溃；重载后用重载模块自身的函数计数自洽。"""
    mod = importlib.reload(rag_metrics)
    mod.reset()
    mod.record_clean(fallback=False, chars_in=10, chars_out=10)
    assert mod.snapshot()["clean_calls"] == 1
