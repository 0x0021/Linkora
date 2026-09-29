"""LLM 用量台账（usage_ledger）回归测试。

覆盖：记账 → 按用途/平台聚合 → 日成本 → 汇率换算 → 客户端层挂接
（LLMClient._record_usage 真实/估算双路径）→ 防御性（坏数据不抛）。
台账库经 _db_path_override 重定向到 tmp 文件，绝不触碰真实主库。
"""
from __future__ import annotations

import sqlite3

import pytest

from src.llm import usage_ledger
from src.llm.usage_ledger import (
    PURPOSE_MEMORY,
    PURPOSE_REPLY,
    PURPOSE_SUMMARY,
    cleanup_old_usage,
    get_daily_cost_usd,
    get_stats,
    record_usage,
)


@pytest.fixture()
def tmp_ledger(tmp_path, monkeypatch):
    """台账重定向到 tmp 库，测试结束自动清理。"""
    db = str(tmp_path / "usage_test.db")
    monkeypatch.setattr(usage_ledger, "_db_path_override", db)
    yield db
    monkeypatch.setattr(usage_ledger, "_db_path_override", None)


def test_record_and_stats_by_purpose(tmp_ledger):
    record_usage(PURPOSE_REPLY, "gpt-4o", input_tokens=1000, output_tokens=100, cost_usd=0.01)
    record_usage(PURPOSE_SUMMARY, "gpt-4o", input_tokens=500, output_tokens=50, cost_usd=0.005)
    record_usage(PURPOSE_SUMMARY, "gpt-4o", input_tokens=300, output_tokens=30, cost_usd=0.003)

    stats = get_stats(hours=None)
    assert stats["available"] is True
    t = stats["totals"]
    assert t["calls"] == 3
    assert t["input_tokens"] == 1800
    assert t["output_tokens"] == 180
    assert t["total_tokens"] == 1980
    assert abs(t["cost_usd"] - 0.018) < 1e-9

    by_purpose = stats["by_purpose"]
    assert by_purpose["reply"]["calls"] == 1
    assert by_purpose["summary"]["calls"] == 2
    assert by_purpose["summary"]["total_tokens"] == 880


def test_stats_hours_window(tmp_ledger):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=5)
    # 0 小时窗口：只有刚写入的记录，能查到
    stats = get_stats(hours=1)
    assert stats["totals"]["calls"] == 1
    # 未来时间窗剪裁边界：cutoff 晚于写入时间即可查到（ hours=1 覆盖刚才那条）
    assert stats["by_purpose"]["reply"]["total_tokens"] == 15


def test_by_platform_aggregation(tmp_ledger):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=5, platform="dingtalk")
    record_usage(PURPOSE_SUMMARY, "m", input_tokens=20, output_tokens=8, platform="wecom")
    # platform 缺省 → 从平台上下文取。测试环境无显式 ctx，按项目约定兜底回退
    # 主平台 dingtalk（_active_platform_ctx default 非空，见 platform_context 文档）。
    record_usage(PURPOSE_MEMORY, "m", input_tokens=1, output_tokens=1)

    stats = get_stats(hours=None)
    bp = stats["by_platform"]
    assert bp["dingtalk"]["calls"] == 2
    assert bp["dingtalk"]["total_tokens"] == 17
    assert bp["wecom"]["total_tokens"] == 28


def test_daily_cost_usd(tmp_ledger):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=10, cost_usd=0.02)
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    assert abs(get_daily_cost_usd(day) - 0.02) < 1e-9
    assert get_daily_cost_usd("1999-01-01") == 0.0


def test_totals_include_cost_cny(tmp_ledger):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=10, cost_usd=1.0)
    stats = get_stats(hours=None)
    # 汇率换算与 collector 的 USD_CNY_RATE 同源（7.2）
    assert abs(stats["totals"]["cost_cny"] - 7.2) < 1e-6


def test_estimated_flag_roundtrip(tmp_ledger):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=10, is_estimated=True)
    stats = get_stats(hours=None)
    assert stats["by_purpose"]["reply"]["estimated_calls"] == 1


def test_record_never_raises(tmp_ledger, monkeypatch):
    """记账防御性：任何内部异常都不能抛给调用方（主链路保护）。"""
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(usage_ledger, "_connect", _boom)
    record_usage(PURPOSE_REPLY, "m", input_tokens=1, output_tokens=1)  # 不应抛
    assert get_stats(hours=None)["available"] is False  # 查询同样降级不抛


def test_stats_on_missing_table(tmp_ledger):
    """全新空库：建表幂等、统计返回零值而非异常。"""
    conn = sqlite3.connect(tmp_ledger)
    conn.execute("SELECT 1")
    conn.close()
    stats = get_stats(hours=None)
    assert stats["available"] is True
    assert stats["totals"]["calls"] == 0


def test_ts_index_created(tmp_ledger):
    """D7 延伸修复：时间范围查询必须走索引，否则 llm_usage 长大后全表扫描。"""
    record_usage(PURPOSE_REPLY, "m", input_tokens=1, output_tokens=1)
    conn = sqlite3.connect(tmp_ledger)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='llm_usage'")]
    conn.close()
    assert "idx_llm_usage_ts" in names


def test_cleanup_old_usage(tmp_ledger):
    """保留期清理：超期记录被删、近期内记录保留（防主库无限膨胀）。"""
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    new = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(tmp_ledger)
    conn.execute(usage_ledger._DDL)
    conn.execute(
        "INSERT INTO llm_usage (ts,purpose,platform,model,input_tokens,output_tokens,"
        "total_tokens,cost_usd,is_estimated) VALUES (?,?,?,?,?,?,?,?,?)",
        (old, "reply", "", "m", 10, 5, 15, 0.0, 0))
    conn.execute(
        "INSERT INTO llm_usage (ts,purpose,platform,model,input_tokens,output_tokens,"
        "total_tokens,cost_usd,is_estimated) VALUES (?,?,?,?,?,?,?,?,?)",
        (new, "reply", "", "m", 10, 5, 15, 0.0, 0))
    conn.commit()
    conn.close()

    deleted = cleanup_old_usage(365)
    assert deleted == 1
    stats = get_stats(hours=None)
    assert stats["totals"]["calls"] == 1  # 仅保留最近一条


# ---------------------------------------------------------------------------
# client 层挂接：LLMClient._record_usage 真实 usage / 估算双路径
# ---------------------------------------------------------------------------

def _mk_client():
    """构造 LLMClient 但不触发真实 OpenAI 客户端初始化（绕过 __new__）。"""
    from src.llm.client import LLMClient
    from types import SimpleNamespace
    c = LLMClient.__new__(LLMClient)
    c.config = SimpleNamespace(model="gpt-4o", model_pricing={"gpt-4o": {"input": 5.0, "output": 15.0}})
    return c


def test_client_record_usage_real(tmp_ledger):
    client = _mk_client()
    client._record_usage(PURPOSE_REPLY, "gpt-4o", {"messages": []},
                         {"prompt_tokens": 1000, "completion_tokens": 200}, "hi")
    stats = get_stats(hours=None)
    row = stats["by_purpose"]["reply"]
    assert row["input_tokens"] == 1000 and row["output_tokens"] == 200
    assert row["estimated_calls"] == 0
    # 价目表估算：1000*5/1M + 200*15/1M = 0.005 + 0.003 = 0.008 USD
    assert abs(row["cost_usd"] - 0.008) < 1e-9


def test_client_record_usage_estimated_when_no_usage(tmp_ledger):
    client = _mk_client()
    client._record_usage(PURPOSE_SUMMARY, "gpt-4o", {"messages": [{"role": "user", "content": "你好世界"}]},
                         None, "这是一段足够长的估算输出内容")
    stats = get_stats(hours=None)
    row = stats["by_purpose"]["summary"]
    assert row["calls"] == 1
    assert row["estimated_calls"] == 1
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0


def test_client_record_usage_zero_usage_skipped(tmp_ledger):
    client = _mk_client()
    client._record_usage(PURPOSE_REPLY, "gpt-4o", {"messages": []}, None, "")
    stats = get_stats(hours=None)
    assert stats["totals"]["calls"] == 0


def test_chat_signature_has_no_purpose_param():
    """chat() 签名不得含 purpose——大量测试以 fake client 替换整个 chat()，
    签名加参会把桩全部炸掉（CI 血泪）；用途经 purpose_scope contextvar 传递。"""
    import inspect
    from src.llm.client import LLMClient
    sig = inspect.signature(LLMClient.chat)
    assert "purpose" not in sig.parameters


def test_purpose_scope_nesting_and_reset():
    """purpose_scope：作用域内取值正确、嵌套恢复、退出后回退默认。"""
    from src.llm.usage_ledger import purpose_scope, current_purpose, PURPOSE_OTHER
    assert current_purpose() == PURPOSE_OTHER
    with purpose_scope("reply"):
        assert current_purpose() == "reply"
        with purpose_scope("summary"):
            assert current_purpose() == "summary"
        assert current_purpose() == "reply"
    assert current_purpose() == PURPOSE_OTHER


# ---------------------------------------------------------------------------
# Web API 接入：cost-quality summary 必须携带全口径 llm_usage 块
# ---------------------------------------------------------------------------

def test_cost_quality_summary_includes_llm_usage(tmp_ledger, monkeypatch):
    record_usage(PURPOSE_REPLY, "m", input_tokens=10, output_tokens=10, cost_usd=0.01)
    record_usage(PURPOSE_SUMMARY, "m", input_tokens=20, output_tokens=5, cost_usd=0.002)

    import web.routers.cost_quality as cq
    # 无 app 实例时按平台循环为空，但 llm_usage（全局台账）仍须返回
    monkeypatch.setattr(cq, "get_app_instance", lambda: None)
    summary = cq._work_summary(hours=24)
    assert "llm_usage" in summary
    lu = summary["llm_usage"]
    assert lu["available"] is True
    assert set(lu["by_purpose"].keys()) == {PURPOSE_REPLY, PURPOSE_SUMMARY}
    assert lu["totals"]["total_tokens"] == 45
