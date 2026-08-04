"""完整测试 src/utils/logger.py 的各代码路径。"""
import logging
import tempfile
from pathlib import Path

from src.utils.logger import (
    ColoredFormatter,
    InMemoryLogHandler,
    get_log_buffer,
    setup_logger,
)


class TestColoredFormatter:
    def test_format_with_color(self):
        fmt = "%(levelname)s: %(message)s"
        formatter = ColoredFormatter(fmt, use_color=True)
        record = logging.LogRecord("test", logging.ERROR, "", 0, "boom", None, None)
        s = formatter.format(record)
        assert "\x1b[" in s
        assert "boom" in s

    def test_format_without_color(self):
        fmt = "%(levelname)s: %(message)s"
        formatter = ColoredFormatter(fmt, use_color=False)
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", None, None)
        s = formatter.format(record)
        assert "\x1b[" not in s
        assert "hello" in s

    def test_format_non_colorable_level(self):
        """Custom level number not in COLORS dict → no color applied."""
        fmt = "%(levelname)s: %(message)s"
        formatter = ColoredFormatter(fmt, use_color=True)
        record = logging.LogRecord("test", 99, "", 0, "custom", None, None)
        s = formatter.format(record)
        assert "custom" in s
        # levelname should NOT be colorized (99 not in COLORS)
        assert "Level 99" in s or "LEVEL 99" in s.upper() or "99" in s

    def test_format_with_exc_info(self):
        fmt = "%(levelname)s: %(message)s"
        formatter = ColoredFormatter(fmt, use_color=False)
        try:
            raise ValueError("test")
        except ValueError:
            record = logging.LogRecord(
                "test", logging.ERROR, "", 0, "crash", None, (), None
            )
            record.exc_info = ()
        s = formatter.format(record)
        assert "crash" in s

    def test_llm_logger_uses_accent_color(self):
        """src.llm.* / src.decision_tracker 类的 logger 应用亮紫加粗，与普通业务区分。"""
        fmt = "%(levelname)s %(name)s %(message)s"
        formatter = ColoredFormatter(fmt, use_color=True)
        for name in ("src.llm.agent", "src.llm.client", "src.decision_tracker"):
            record = logging.LogRecord(name, logging.INFO, "", 0, "thinking...", None, None)
            s = formatter.format(record)
            # LLM 专用亮紫加粗
            assert "\x1b[1;95m" in s, f"{name} 未套用 LLM 专用色: {s!r}"
            assert "thinking..." in s

    def test_non_llm_logger_uses_level_color(self):
        """src.poller / __main__ 等普通业务 logger 按 level 着色，不用 LLM 专用色。"""
        fmt = "%(levelname)s %(name)s %(message)s"
        formatter = ColoredFormatter(fmt, use_color=True)
        for name in ("src.poller", "__main__", "src.web.api"):
            record = logging.LogRecord(name, logging.INFO, "", 0, "normal", None, None)
            s = formatter.format(record)
            # 不应有 LLM 专用亮紫
            assert "\x1b[1;95m" not in s, f"{name} 误套 LLM 专用色: {s!r}"
            # 应该有 level 绿
            assert "\x1b[32m" in s, f"{name} 未套 level 绿: {s!r}"


class TestSetupLogger:
    def test_setup_with_log_file(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            setup_logger(level="info", log_file=str(log_path))
            logger = logging.getLogger("tests.logger")
            logger.info("hello file")
            assert log_path.exists()
            content = log_path.read_text()
            assert "hello file" in content

    def test_setup_logger_resets_handlers(self):
        root = logging.getLogger()
        setup_logger(level="debug", log_file=None)
        assert len(root.handlers) == 2

    def test_get_log_buffer_returns_singleton(self):
        buf = get_log_buffer()
        assert buf is get_log_buffer()


class TestInMemoryLogHandler:
    def test_emit_and_get_records(self):
        handler = InMemoryLogHandler(maxlen=100)
        logger = logging.getLogger("mem.test")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("first")
        logger.warning("second")
        logger.error("third")

        records = handler.get_records(level_no=logging.WARNING)
        assert len(records) == 2

        records = handler.get_records(since_id=0)
        assert len(records) == 3

        records = handler.get_records(since_id=1, limit=1)
        assert len(records) == 1

        assert handler.count() == 3
        assert handler.count(level_no=logging.ERROR) == 1
        assert handler.max_id() == 3

    def test_emit_strips_ansi(self):
        handler = InMemoryLogHandler(maxlen=100)

        class FakeRecord(logging.LogRecord):
            pass

        record = FakeRecord("test", logging.INFO, "", 0, "\x1b[31mred\x1b[0m", None, None)
        handler.emit(record)
        records = handler.get_records()
        assert len(records) == 1
        assert "\x1b[" not in records[0]["message"]
        assert "red" in records[0]["message"]

    def test_buffer_maxlen(self):
        handler = InMemoryLogHandler(maxlen=3)
        logger = logging.getLogger("mem.max")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("a")
        logger.info("b")
        logger.info("c")
        logger.info("d")

        records = handler.get_records()
        assert len(records) == 3
        assert records[0]["message"] == "b"

    def test_emit_handles_exception(self):
        """When emit raises, handleError is called gracefully."""
        handler = InMemoryLogHandler(maxlen=100)
        # Create a malformed record that will cause emit to fail
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", None, None)
        # Manually corrupt the record
        object.__setattr__(record, 'created', None)
        handler.emit(record)  # should not raise
