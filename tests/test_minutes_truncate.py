"""GetMinutesTool 转写原文截断测试（防止冲爆 LLM 上下文）。"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.tools.minutes import GetMinutesTool, MAX_TRANSCRIPT_CHARS


class TestTranscriptionTruncation:
    def test_transcription_truncated(self):
        dws = MagicMock()
        huge = "x" * (MAX_TRANSCRIPT_CHARS + 500)
        dws.minutes_get_transcription.return_value = huge
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "transcription"})
        assert res.get("truncated") is True
        assert res["total_chars"] == len(huge)
        assert len(res["result"]) <= MAX_TRANSCRIPT_CHARS + 60
        assert "已截断" in res["result"]

    def test_summary_not_truncated(self):
        dws = MagicMock()
        dws.minutes_get_summary.return_value = "简短摘要"
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "summary"})
        assert "truncated" not in res
        assert res["result"] == "简短摘要"

    def test_todos_not_truncated(self):
        dws = MagicMock()
        dws.minutes_get_todos.return_value = [{"todo": "1"}]
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "todos"})
        assert "truncated" not in res
        assert res["result"] == [{"todo": "1"}]

    def test_transcription_short_not_truncated(self):
        dws = MagicMock()
        dws.minutes_get_transcription.return_value = "短转写"
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "transcription"})
        assert "truncated" not in res
        assert res["result"] == "短转写"
