from __future__ import annotations

import json as _json
import logging
import re
import sys
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from src.utils.request_id import get_request_id, get_trace_id

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|password|secret|token|access[_-]?token)\s*[=:]\s*['\"]?([^'\"]+)['\"]?"),
    re.compile(r"(?i)(sk-[a-zA-Z0-9_-]{20,})"),
    re.compile(r"(?i)(pk-[a-zA-Z0-9_-]{20,})"),
]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text) if text else ""


# 平台归类：已知平台专属日志模块 → 平台 id；其余（核心/LLM/规则引擎等共享模块）返回
# None（中性，全平台可见）。用于在 Web 日志视图按平台隔离，减少跨平台噪声。
# 映射以 logger 名为准（logger = 模块 __name__），覆盖 IM 适配器与各平台专属模块。
_PLATFORM_LOGGER_PREFIXES: dict[str, str] = {
    # 钉钉（DWS CLI 适配器 + 钉钉审批）
    "src.dws_adapter": "dingtalk",
    "src.approval.dingtalk": "dingtalk",
    # 飞书（lark-cli 适配器 + 飞书文档导入）
    "src.im_adapter.feishu": "feishu",
    "src.im_adapter.feishu_doc_mixin": "feishu",
    "src.im_adapter.feishu_media_mixin": "feishu",
    "src.kb.feishu_importer": "feishu",
    # 飞书示例骨架（capabilities 以飞书为例，极少日志）
    "src.im_adapter.capabilities": "feishu",
    # 企业微信（wecom-cli 适配器）
    "src.im_adapter.wecom": "wecom",
    # 钉钉文档同步调度器（同步的是 dingtalk_docs，钉钉专属）
    "src.doc_sync_scheduler": "dingtalk",
    # ---- CLI 二进制名（某些路径下 logger 名即为工具名）----
    "lark-cli": "feishu",       # 飞书 CLI
    "DWS": "dingtalk",          # 钉钉 DWS CLI（大写，与 poller_strategy._PLATFORM_CLI_LABEL 一致）
    "wecom-cli": "wecom",       # 企微 CLI
}

# 消息内容特征词（最后兜底）：共享模块（如 im_adapter.base）替某平台 CLI 干活时，
# logger 名是中性的，但消息里带 CLI 二进制名。仅用词边界严格匹配 CLI 名，避免误伤；
# 同一条消息命中多个平台特征 → 视为中性（无法唯一归属）。
_CONTENT_PLATFORM_MARKERS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"lark[-_]cli", re.IGNORECASE), "feishu"),
    (re.compile(r"wecom[-_]cli", re.IGNORECASE), "wecom"),
    (re.compile(r"(?<![A-Za-z])dws(?![A-Za-z])", re.IGNORECASE), "dingtalk"),
)

# 日志平台上下文：消息处理链路（平台回调/防抖 flush/死信重放）进入时设置，
# 使该链路内**所有**日志（LLM/规则引擎/poller 核心等共享模块）都能精确归属平台。
# 与 src.platform.base._active_platform_ctx 分离，因后者 default="dingtalk"，
# 无法区分「显式设置」与「默认回退」（web/调度器线程会被误标钉钉）。
_log_platform_ctx: ContextVar[str | None] = ContextVar("log_platform", default=None)

_POLLER_THREAD_RE = re.compile(r"^poller-([A-Za-z0-9_\-]+)$")


@contextmanager
def log_platform_scope(platform: str | None):
    """在作用域内将日志归属到指定平台（None 表示不标记）。"""
    token = _log_platform_ctx.set(platform)
    try:
        yield
    finally:
        _log_platform_ctx.reset(token)


def classify_log_platform(logger_name: str) -> str | None:
    """返回日志归属平台 id；中性/共享模块返回 None（全平台可见）。

    归类依据：logger 名（模块 __name__）前缀。例如 src.dws_adapter → dingtalk，
    src.im_adapter.feishu → feishu，src.im_adapter.wecom → wecom；
    src.llm / src.poller / src.rule_engine 等共享模块 → None。
    """
    name = logger_name or ""
    for prefix, pid in _PLATFORM_LOGGER_PREFIXES.items():
        if name.startswith(prefix):
            return pid
    return None


def resolve_log_platform(record: logging.LogRecord, message: str | None = None) -> str | None:
    """emit 时解析单条日志的归属平台（五级级联，写入时打戳）。

    优先级（前者命中即返回）：
    1. 显式 extra：logger.info(..., extra={"platform": "feishu"})；
    2. 日志平台 ContextVar：消息处理链路进入时设置（覆盖链路内共享模块日志）；
    3. 线程名：各平台轮询器线程名为 poller-<pid>，轮询循环内所有日志天然归属；
    4. logger 名前缀：平台专属模块的静态映射；
    5. 消息内容 CLI 特征词：lark-cli/wecom-cli/dws（词边界），多平台命中→中性。
    全部未命中 → None（中性，全平台可见：启动/Web/调度器等）。
    """
    # 1. 显式 extra
    explicit = record.__dict__.get("platform")
    if isinstance(explicit, str) and explicit:
        return explicit
    # 2. 日志平台上下文
    ctx_pid = _log_platform_ctx.get()
    if ctx_pid:
        return ctx_pid
    # 3. 线程名 poller-<pid>
    m = _POLLER_THREAD_RE.match(getattr(record, "threadName", "") or "")
    if m:
        return m.group(1)
    # 4. logger 名前缀
    by_name = classify_log_platform(record.name)
    if by_name:
        return by_name
    # 5. 消息内容特征词（唯一命中才归属）
    if message is None:
        try:
            message = record.getMessage()
        except Exception:
            return None
    hits = {pid for pat, pid in _CONTENT_PLATFORM_MARKERS if pat.search(message)}
    if len(hits) == 1:
        return next(iter(hits))
    return None


def _redact_sensitive(text: str) -> str:
    if not text:
        return text
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(r"\1=***REDACTED***", text)
    return text


class ColoredFormatter(logging.Formatter):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    LLM_LOGGER_COLOR = "\033[1;95m"
    LLM_LOGGER_PREFIXES: tuple[str, ...] = (
        "src.llm.",
        "src.decision_tracker",
    )

    def __init__(self, fmt: str, use_color: bool = True):
        super().__init__(fmt)
        self.use_color = use_color

    def _is_llm_logger(self, name: str) -> bool:
        return any(name.startswith(prefix) for prefix in self.LLM_LOGGER_PREFIXES)

    def _colorize(self, text: str, color: str | None) -> str:
        if self.use_color and color:
            return f"{color}{text}{self.RESET}"
        return text

    def format(self, record: logging.LogRecord) -> str:
        # 始终设置 request_id，防止 formatter 使用 %(request_id)s 时 KeyError
        rid = get_request_id()
        record.request_id = rid if rid else "-"
        if self.use_color:
            is_llm = self._is_llm_logger(record.name)
            if is_llm:
                accent = self.LLM_LOGGER_COLOR
                record.levelname = self._colorize(record.levelname, accent)
                record.name = self._colorize(record.name, accent)
                # 消息前注入 [rid=xxxx]，让用户一眼关联全链路
                if rid:
                    record.msg = self._colorize(f"[rid={rid[:10]}] ", accent) + self._colorize(str(record.msg), accent)
                else:
                    record.msg = self._colorize(str(record.msg), accent)
            elif record.levelno in self.COLORS:
                level_color = self.COLORS[record.levelno]
                record.levelname = self._colorize(record.levelname, level_color)
                record.name = self._colorize(record.name, "\033[96m")
                if rid:
                    record.msg = self._colorize(f"[rid={rid[:10]}] ", "\033[96m") + self._colorize(str(record.msg), self.COLORS[record.levelno])
                else:
                    record.msg = self._colorize(str(record.msg), self.COLORS[record.levelno])
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        formatted = super().format(record)
        return formatted


class RichConsoleFormatter:
    ICONS = {
        logging.DEBUG: "🔍",
        logging.INFO: "ℹ️",
        logging.WARNING: "⚠️",
        logging.ERROR: "❌",
        logging.CRITICAL: "💀",
    }

    MODULE_COLORS = {
        "src.poller": "#60a5fa",
        "src.poller_core": "#38bdf8",
        "src.dws_adapter": "#22d3ee",
        "src.im_adapter": "#f472b6",
        "src.llm": "#a855f7",
        "src.rule_engine": "#4ade80",
        "src.intent": "#34d399",
        "src.memory": "#fb923c",
        "src.skills": "#facc15",
        "src.tools": "#fbbf24",
        "src.semantic": "#c084fc",
        "src.config": "#94a3b8",
        "__main__": "#818cf8",
    }

    def __init__(self):
        try:
            from rich.console import Console
            from rich.text import Text
            from rich.style import Style
            self.console = Console(
                file=sys.stdout,
                force_terminal=True,
                force_interactive=False,
                width=200,
            )
            self.Text = Text
            self.Style = Style
            self._rich_available = True
        except ImportError:
            self._rich_available = False

    def format(self, record: logging.LogRecord) -> str:
        rid = get_request_id()
        record.request_id = rid if rid else "-"
        if not self._rich_available:
            ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            rid_part = f" [rid={rid[:10]}]" if rid else ""
            return f"{ts} [{record.levelname:<8}] {record.name}:{rid_part} {record.getMessage()}"

        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        ts_text = self.Text(ts, style=self.Style(color="#64748b", dim=True))

        rid_text = self.Text(f" [rid={rid[:10]}]", style=self.Style(color="#94a3b8", dim=True)) if rid else self.Text("")

        level_icon = self.ICONS.get(record.levelno, "📝")
        level_name = record.levelname
        if record.levelno == logging.DEBUG:
            level_style = self.Style(color="#22d3ee", dim=True)
        elif record.levelno == logging.INFO:
            level_style = self.Style(color="#4ade80")
        elif record.levelno == logging.WARNING:
            level_style = self.Style(color="#fbbf24")
        elif record.levelno == logging.ERROR:
            level_style = self.Style(color="#f87171", bold=True)
        elif record.levelno == logging.CRITICAL:
            level_style = self.Style(color="#f472b6", bold=True)
        else:
            level_style = self.Style(color="#94a3b8")

        level_text = self.Text(f" {level_icon} {level_name:<5} ", style=level_style)

        module_color = self._get_module_color(record.name)
        module_text = self.Text(record.name, style=self.Style(color=module_color))

        msg_color = self._get_module_color(record.name)
        msg_text = self.Text(record.getMessage(), style=self.Style(color=msg_color))

        line = self.Text()
        line.append(ts_text)
        line.append(" ")
        line.append(level_text)
        line.append(" ")
        line.append(module_text)
        if rid_text:
            line.append(rid_text)
        line.append(": ")
        line.append(msg_text)

        return cast(str, line)

    def _get_module_color(self, name: str) -> str:
        for prefix, color in self.MODULE_COLORS.items():
            if name.startswith(prefix):
                return color
        return "#94a3b8"

    def print(self, record: logging.LogRecord):
        if self._rich_available:
            line = self.format(record)
            self.console.print(line, soft_wrap=True, no_wrap=False)
        else:
            print(self.format(record))


class RichHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.formatter = cast("logging.Formatter", RichConsoleFormatter())

    def emit(self, record: logging.LogRecord):
        try:
            cast("RichConsoleFormatter", self.formatter).print(record)
        except Exception:
            self.handleError(record)


class InMemoryLogHandler(logging.Handler):
    def __init__(self, maxlen: int = 5000):
        super().__init__()
        self._buffer: deque = deque(maxlen=maxlen)
        self._next_id = 1
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            raw_level = _strip_ansi(record.levelname)
            raw_logger = _strip_ansi(record.name)
            raw_msg = _redact_sensitive(_strip_ansi(record.getMessage()))
            # 写入时打平台戳（emit 时上下文/线程信息还在，事后无法恢复）
            rec_platform = resolve_log_platform(record, message=raw_msg)
            with self._lock:
                self._buffer.append({
                    "id": self._next_id,
                    "ts": ts,
                    "level": raw_level,
                    "levelno": record.levelno,
                    "logger": raw_logger,
                    "message": raw_msg,
                    "platform": rec_platform,
                })
                self._next_id += 1
        except Exception:
            self.handleError(record)

    @staticmethod
    def _match_platform(rec: dict, platform: str | None) -> bool:
        """平台过滤：platform 为空/"all" → 全量；否则保留「中性(无平台)」+「归属该平台」的记录。

        优先用 emit 时打的平台戳（rec["platform"]，精确）；旧记录无戳则回退
        logger 名前缀归类（兼容缓冲区内升级前的存量记录）。
        """
        if not platform or platform == "all":
            return True
        p = rec.get("platform") or classify_log_platform(rec.get("logger", ""))
        return p is None or p == platform

    def get_records(self, level_no: int = 0, since_id: int = 0, limit: int = 200,
                    platform: str | None = None):
        with self._lock:
            items = [r for r in self._buffer
                     if r["levelno"] >= level_no and r["id"] > since_id
                     and self._match_platform(r, platform)]
            if limit and len(items) > limit:
                items = items[-limit:]
            return list(items)

    def count(self, level_no: int = 0, platform: str | None = None) -> int:
        with self._lock:
            return sum(1 for r in self._buffer
                       if r["levelno"] >= level_no and self._match_platform(r, platform))

    def max_id(self) -> int:
        with self._lock:
            return self._next_id - 1


_MEM_HANDLER = InMemoryLogHandler()


class PlainFileFormatter(logging.Formatter):
    """文件日志 formatter，自动注入 request_id（不依赖 filter）。"""

    def format(self, record: logging.LogRecord) -> str:
        try:
            from src.utils.request_id import get_request_id
            rid = get_request_id()
            record.request_id = rid if rid else "-"
        except Exception:
            record.request_id = "-"
        # 同时保证 trace_id 字段存在（F29），避免含 %(trace_id)s 的格式串 KeyError
        if not getattr(record, "trace_id", ""):
            try:
                from src.utils.request_id import get_trace_id
                record.trace_id = get_trace_id() or "-"
            except Exception:
                record.trace_id = "-"
        return super().format(record)


class JsonLogFormatter(logging.Formatter):
    """结构化 JSON 日志 formatter（F29）。

    每条日志输出一行紧凑 JSON，便于日志采集/检索/跨服务关联：
    - 自动注入 request_id / trace_id（贯穿 Web→Runtime→LLM/DWS）
    - 时间用 ISO8601、含 level/logger/message/module/lineno
    - 异常 traceback 写入 exc 字段
    - ensure_ascii=False 保留中文；任何序列化异常都不应导致日志丢失（降级为纯文本）
    """

    def format(self, record: logging.LogRecord) -> str:
        # 始终注入，保证字段稳定存在（即便 filter 未装）
        rid = get_request_id()
        tid = get_trace_id()
        record.request_id = rid or "-"
        record.trace_id = tid or "-"
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "levelno": record.levelno,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": rid or "-",
            "trace_id": tid or "-",
            "module": record.module,
            "lineno": record.lineno,
        }
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            payload["exc"] = record.exc_text
        try:
            return _json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            # 极端情况下兜底：至少把消息落盘，避免 JSON 失败吞掉日志
            return _json.dumps(
                {"ts": payload["ts"], "level": record.levelname,
                 "logger": record.name, "message": str(record.msg),
                 "request_id": payload["request_id"], "trace_id": payload["trace_id"]},
                ensure_ascii=False,
            )


def setup_logger(level: str = "info", log_file: str | None = None,
                 max_size_mb: int = 50, max_backups: int = 7,
                 *, use_fancy: bool = True, json_logs: bool = False) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] [%(request_id)s] [%(trace_id)s] %(name)s: %(message)s"
    plain_formatter = PlainFileFormatter(fmt)
    plain_formatter.default_msec_format = "%s.%03d"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for h in root.handlers[:]:
        root.removeHandler(h)

    if use_fancy:
        # 使用增强控制台格式（log_formatter 模块）
        try:
            from src.log_formatter import inject_fancy_console_handler
            _fancy = inject_fancy_console_handler(console_level=log_level)
            # 记录到全局供启动横幅使用
            _setup_fancy_formatter = _fancy
        except Exception:
            use_fancy = False

    if not use_fancy:
        console_handler = RichHandler()
        console_handler.setLevel(log_level)
        root.addHandler(console_handler)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=max_size_mb * 1024 * 1024,
            backupCount=max_backups, encoding="utf-8"
        )
        fh.setLevel(log_level)
        # F29：结构化 JSON 日志（默认关闭，避免影响既有纯文本日志消费方）；
        # 仅文件 handler 用 JSON，控制台保持人类可读。
        fh.setFormatter(JsonLogFormatter() if json_logs else plain_formatter)
        root.addHandler(fh)

    _MEM_HANDLER.setLevel(logging.DEBUG)
    if _MEM_HANDLER not in root.handlers:
        root.addHandler(_MEM_HANDLER)

    from src.utils.request_id import install_log_filter
    install_log_filter()


def get_log_buffer() -> InMemoryLogHandler:
    return _MEM_HANDLER
