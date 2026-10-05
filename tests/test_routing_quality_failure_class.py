"""routing_quality 失败归因的回归测试（2026-10-05，P1-10）。

背景：排查「AI 答得不对」时，第一个要判断的是**数据到底有没有到手**：

  - 工具都没成功          → 查工具本身（网络/权限/参数）
  - 工具成功但没返回数据   → 查知识库/索引内容，**不是 prompt 的锅**
  - 数据到手了却没调工具   → 查工具描述与 prompt
  - 数据到手了却没回复     → 查模型/超时

此前 routing_quality 只记 token/cost/耗时，6 个 stage 里也没有 tool_execution，
这个问题在库里**根本无法回答**，只能翻日志人工推断。本组用例锁住归因规则。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.llm.agent_steps.routing_trace import (  # noqa: E402
    FAILURE_LLM_ERROR,
    FAILURE_NO_REPLY,
    FAILURE_NO_TOOL_SELECTED,
    FAILURE_TOOL_EMPTY,
    FAILURE_TOOL_ERROR,
    _classify_failure,
)
from src.llm.tool_orchestrator import _is_empty_tool_result  # noqa: E402
from src.memory.schema import init_schema  # noqa: E402


def _reply(text: str = "好的"):
    r = MagicMock()
    r.text = text
    return r


def _tool(name: str, success: bool = True, empty: bool = False, ms: int = 10):
    return {"tool": name, "success": success, "duration_ms": ms,
            "error_class": "" if success else "tool_error", "empty": empty}


class TestFailureClassification:
    def test_all_tools_failed(self):
        """工具全失败 → tool_error"""
        assert _classify_failure(
            [_tool("kb_search", success=False), _tool("web_search", success=False)],
            _reply("抱歉"), 2,
        ) == FAILURE_TOOL_ERROR

    def test_tool_success_but_empty_result(self):
        """工具成功但没数据 → tool_empty_result（该查知识库，不是 prompt）"""
        assert _classify_failure([_tool("kb_search", empty=True)], _reply("没找到"), 1) \
            == FAILURE_TOOL_EMPTY

    def test_mixed_all_empty_still_empty(self):
        """多个工具都成功但都空 → 仍归 tool_empty_result"""
        assert _classify_failure(
            [_tool("kb_search", empty=True), _tool("web_search", empty=True)],
            _reply("没找到"), 2,
        ) == FAILURE_TOOL_EMPTY

    def test_partial_failure_with_data_is_ok(self):
        """有任一工具返回数据 → 不归因（数据到手，答对与否是另一回事）"""
        assert _classify_failure(
            [_tool("web_search", success=False), _tool("kb_search", empty=False)],
            _reply("答案在这里"), 2,
        ) == ""

    def test_data_returned_but_no_reply_is_llm_error(self):
        """数据到手却没产出回复 → llm_error"""
        assert _classify_failure([_tool("kb_search", empty=False)], _reply(""), 1) \
            == FAILURE_LLM_ERROR

    def test_tools_available_but_none_called(self):
        """给了工具能力但模型一次没调 → no_tool_selected（查 prompt/工具描述）"""
        assert _classify_failure([], _reply("你好"), 3) == FAILURE_NO_TOOL_SELECTED

    def test_no_tools_exposed_and_no_reply(self):
        """没给工具能力且无回复 → no_reply"""
        assert _classify_failure([], _reply(""), 0) == FAILURE_NO_REPLY

    def test_no_tools_exposed_with_reply_is_clean(self):
        """纯闲聊场景正常 → 无归因"""
        assert _classify_failure([], _reply("你好"), 0) == ""

    def test_data_returned_and_reply_ok(self):
        """数据到手 + 正常回复 → 无归因"""
        assert _classify_failure([_tool("kb_search")], _reply("答案"), 1) == ""


class TestEmptyResultDetection:
    @pytest.mark.parametrize("value", [None, "", [], {}, (), set()])
    def test_empty_values(self, value):
        assert _is_empty_tool_result(value) is True

    @pytest.mark.parametrize("value", ["有内容", ["a"], {"k": "v"}, 1, True])
    def test_non_empty_values(self, value):
        assert _is_empty_tool_result(value) is False


class TestSchemaMigration:
    def test_new_columns_created_on_fresh_db(self, tmp_path):
        """新库应建出 tool_results_json 与 failure_class 两列。"""
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        conn.row_factory = sqlite3.Row  # init_schema 内部按列名取值
        init_schema(conn, str(tmp_path / "t.db"))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_quality)")}
        conn.close()
        assert "tool_results_json" in cols
        assert "failure_class" in cols

    def test_old_db_gets_columns_migrated(self, tmp_path):
        """旧库（无这两列）经 init_schema 后必须补齐 —— 缺列会导致写入静默失败。

        旧库形态取自**真实历史版本**：保留当时的全部列，只缺后加的
        input_tokens/output_tokens/total_tokens/cost_usd/tool_results_json/
        failure_class。不可只造「三列残表」——那种库连 primary_skill 都没有，
        init_schema 里既有的 `CREATE INDEX ... ON routing_quality(primary_skill)`
        就会先报错，那是测试造的问题而非本次迁移引入的。
        """
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE routing_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT NOT NULL,
                sender_name TEXT DEFAULT '',
                conversation_id TEXT DEFAULT '',
                content_preview TEXT DEFAULT '',
                primary_skill TEXT DEFAULT '',
                primary_score REAL DEFAULT 0.0,
                primary_source TEXT DEFAULT '',
                combo_count INTEGER DEFAULT 0,
                combo_skills TEXT DEFAULT '[]',
                convergence_zone_size INTEGER DEFAULT 0,
                convergence_applied INTEGER DEFAULT 0,
                goal_fit_details TEXT DEFAULT '{}',
                tools_exposed TEXT DEFAULT '[]',
                routing_mode TEXT DEFAULT '',
                candidates_count INTEGER DEFAULT 0,
                intent_disposition TEXT DEFAULT '',
                intent_action TEXT DEFAULT '',
                intent_actions TEXT DEFAULT '',
                blocked_by_disabled_skill TEXT DEFAULT '[]',
                message_type TEXT DEFAULT '',
                llm_model TEXT DEFAULT '',
                llm_rounds INTEGER DEFAULT 0,
                llm_latency_ms REAL DEFAULT 0.0,
                total_latency_ms REAL DEFAULT 0.0,
                reply_len INTEGER DEFAULT 0,
                reply_text TEXT DEFAULT '',
                stages_json TEXT DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row  # init_schema 内部按列名取值
        init_schema(conn, str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(routing_quality)")}
        conn.close()
        assert {"tool_results_json", "failure_class"} <= cols, (
            f"旧库未迁移出新列，现有: {sorted(cols)}"
        )

    def test_insert_with_new_fields_roundtrip(self, tmp_path):
        """写入并读回 tool_results_json / failure_class（验证非静默失败）。"""
        path = tmp_path / "rw.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row  # init_schema 内部按列名取值
        init_schema(conn, str(path))
        tools = [_tool("kb_search", empty=True, ms=42)]
        conn.execute(
            """INSERT INTO routing_quality
               (sender_id, tool_results_json, failure_class)
               VALUES (?, ?, ?)""",
            ("u1", json.dumps(tools, ensure_ascii=False), FAILURE_TOOL_EMPTY),
        )
        conn.commit()
        row = conn.execute(
            "SELECT tool_results_json, failure_class FROM routing_quality WHERE sender_id=?",
            ("u1",),
        ).fetchone()
        conn.close()
        assert row is not None, "写入失败（可能被静默吞掉）"
        assert json.loads(row[0])[0]["tool"] == "kb_search"
        assert row[1] == FAILURE_TOOL_EMPTY

    def test_failure_index_exists(self, tmp_path):
        """按失败归因筛选是主要查询维度，须建索引。"""
        path = tmp_path / "idx.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row  # init_schema 内部按列名取值
        init_schema(conn, str(path))
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_routing_quality_failure" in idx
