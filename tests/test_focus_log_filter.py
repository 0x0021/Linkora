"""焦点模式（--focus）控制台噪声过滤测试。

验证：
- ConsoleNoiseFilter 只拦「已知噪声模块」的 INFO/DEBUG；WARNING 及以上永远放行；
- 信号 logger（poller_strategy 收到消息 / runtime_llm_reply LLM 回复）不受影响；
- setup_logger(focus=True) 只给控制台 handler 装配过滤器，文件 handler 与
  InMemoryLogHandler（Web 日志视图）不过滤——焦点模式不损失日志完整性。
"""

import io
import logging
from logging.handlers import RotatingFileHandler

from src.utils.logger import (
    ConsoleNoiseFilter,
    FOCUS_SUPPRESSED_LOGGERS,
    InMemoryLogHandler,
    RichHandler,
    get_log_buffer,
    setup_logger,
)


def _record(logger: str, level: int, msg: str = "x") -> logging.LogRecord:
    return logging.LogRecord(
        name=logger, level=level, pathname="t.py", lineno=1,
        msg=msg, args=(), exc_info=None,
    )


# ---------- 过滤器行为 ----------

def test_filter_suppresses_info_from_noisy_loggers():
    f = ConsoleNoiseFilter()
    for name in (
        "src.platform.runtime_dispatch",
        "src.platform.message_loop",
        "src.platform.runtime_inbound",
        "src.platform.runtime_reply_guard",
        "src.platform.memory",
        "src.rule_engine",
        "src.llm.client",
        "src.llm.router",
        "src.llm.prompt_builder",
        "src.llm.agent_steps.reply",
        "src.llm.tool_orchestrator",
        "src.llm.display_summary_scheduler",
        "src.llm.dynamic_summary_scheduler",
        "src.doc_sync_scheduler",
        "uvicorn.access",
    ):
        assert f.filter(_record(name, logging.INFO)) is False, name


def test_filter_keeps_signal_loggers():
    """信号行必须可见：收到的新消息 / LLM 回复 / 生命周期 / 启动初始化。"""
    f = ConsoleNoiseFilter()
    for name in (
        "src.poller_strategy",
        "src.poller_core_discovery",
        "src.platform.runtime_llm_reply",
        "src.platform.lifecycle",
        "src.platform.primary",
        "src.platform.runtime_setup",
        "src.poller",
        "src.poller_core_history",
        "src.poller_core_ocr",
    ):
        assert f.filter(_record(name, logging.INFO)) is True, name


def test_filter_always_passes_warning_and_above():
    """重大错误（WARNING/ERROR/CRITICAL）即使来自噪声模块也放行。"""
    f = ConsoleNoiseFilter()
    for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
        assert f.filter(_record("src.platform.runtime_dispatch", level)) is True
        assert f.filter(_record("src.llm.client", level)) is True


def test_filter_passes_debug_from_non_suppressed_loggers():
    f = ConsoleNoiseFilter()
    assert f.filter(_record("src.poller_strategy", logging.DEBUG)) is True
    assert f.filter(_record("some.unknown.module", logging.DEBUG)) is True


def test_suppressed_list_covers_noise_sources():
    """清单自身健全性：既覆盖链路内部模块，又不含信号模块。"""
    assert "src.platform.runtime_dispatch" in FOCUS_SUPPRESSED_LOGGERS
    assert "src.poller_strategy" not in FOCUS_SUPPRESSED_LOGGERS
    assert "src.platform.runtime_llm_reply" not in FOCUS_SUPPRESSED_LOGGERS


# ---------- setup_logger 装配 ----------

def test_setup_logger_focus_only_console(tmp_path):
    """focus=True：控制台 handler 装过滤器；文件/内存缓冲不装。"""
    log_file = tmp_path / "focus.log"
    setup_logger(level="info", log_file=str(log_file), use_fancy=False, focus=True)
    root = logging.getLogger()

    console_seen = False
    for h in root.handlers:
        if isinstance(h, (RotatingFileHandler, InMemoryLogHandler)):
            assert not any(isinstance(x, ConsoleNoiseFilter) for x in h.filters), h
        else:
            # 控制台（RichHandler 或 fancy StreamHandler）
            assert any(isinstance(x, ConsoleNoiseFilter) for x in h.filters), h
            console_seen = True
    assert console_seen, "应至少存在一个控制台 handler"


def test_setup_logger_without_focus_has_no_filter(tmp_path):
    """默认（focus=False）不改变任何行为：控制台无过滤器。"""
    log_file = tmp_path / "plain.log"
    setup_logger(level="info", log_file=str(log_file), use_fancy=False)
    root = logging.getLogger()
    for h in root.handlers:
        assert not any(isinstance(x, ConsoleNoiseFilter) for x in h.filters), h


def test_focus_mode_memory_handler_still_receives_noise(tmp_path):
    """端到端：焦点模式下噪声记录仍进内存缓冲（Web /api/logs 全量可见）。"""
    log_file = tmp_path / "focus2.log"
    setup_logger(level="info", log_file=str(log_file), use_fancy=False, focus=True)
    root = logging.getLogger()
    # 先摘掉控制台 handler：避免测试期向真实终端打印（过滤器装配已由
    # test_setup_logger_focus_only_console 断言）
    for h in list(root.handlers):
        if not isinstance(h, (RotatingFileHandler, InMemoryLogHandler)):
            root.removeHandler(h)

    logging.getLogger("src.platform.runtime_dispatch").info("[发送] DWS 返回: {...}")
    logging.getLogger("src.poller_strategy").info("✅ 收到 张三 的新消息：在吗")

    recs = get_log_buffer().get_records()
    msgs = [r["message"] for r in recs]
    assert any("DWS 返回" in m for m in msgs)          # 噪声仍在缓冲里
    assert any("收到 张三 的新消息" in m for m in msgs)  # 信号也在缓冲里


def test_focus_filter_via_stream_handler_capture():
    """过滤器挂在普通 StreamHandler 上同样生效（模拟控制台语义）。"""
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setLevel(logging.INFO)
    h.addFilter(ConsoleNoiseFilter())
    root = logging.getLogger()
    # 临时独占 root handlers：同进程内其它测试已装配过控制台/文件 handler，
    # 不隔离会被一并触发（且向真实终端打印）。
    saved = root.handlers[:]
    root.handlers = [h]
    try:
        logging.getLogger("src.platform.runtime_dispatch").info("[发送] 噪声行")
        logging.getLogger("src.platform.runtime_dispatch").warning("[发送] 告警行")
        logging.getLogger("src.poller_strategy").info("✅ 收到新消息")
        h.flush()
        out = stream.getvalue()
        assert "噪声行" not in out
        assert "告警行" in out
        assert "✅ 收到新消息" in out
    finally:
        root.handlers = saved


def test_rich_handler_used_in_non_fancy_path(tmp_path):
    """use_fancy=False 走 RichHandler 分支，focus 也应能装上（不抛异常即可观测）。"""
    setup_logger(level="info", use_fancy=False, focus=True)
    root = logging.getLogger()
    rich = [h for h in root.handlers if isinstance(h, RichHandler)]
    assert rich, "use_fancy=False 应装配 RichHandler"
    assert any(isinstance(x, ConsoleNoiseFilter) for x in rich[0].filters)
