"""控制台日志格式美化模块 (log_formatter)

重新设计控制台日志输出格式，通过 logging config 钩子注入，不侵入业务代码。

特性：
  - HH:MM:SS 彩色时间戳
  - 模块名固定宽度对齐 + 颜色标识
  - 日志级别 emoji 前缀（🟢INFO 🟡WARN 🔴ERROR 🔵DEBUG）
  - 多行日志（JSON dump / traceback）自动添加缩进边框
  - 启动汇总横幅（启动耗时、模块加载数）

用法：在 setup_logger 中调用 ``inject_fancy_console_handler`` 即可替换控制台 handler。
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime


# ── ANSI 颜色常量 ──
class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    # 前景色
    GRAY = "\033[90m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    BRIGHT_MAGENTA = "\033[95m"
    # 背景色
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_CYAN = "\033[46m"


# ── Emoji 级别前缀 ──
LEVEL_EMOJI: dict[int, str] = {
    logging.DEBUG: "\U0001f535",     # 🔵
    logging.INFO: "\U0001f7e2",      # 🟢
    logging.WARNING: "\U0001f7e1",  # 🟡
    logging.ERROR: "\U0001f534",     # 🔴
    logging.CRITICAL: "\U0001f480",  # 💀
}

LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: Ansi.BLUE + Ansi.DIM,
    logging.INFO: Ansi.GREEN,
    logging.WARNING: Ansi.YELLOW,
    logging.ERROR: Ansi.RED + Ansi.BOLD,
    logging.CRITICAL: Ansi.MAGENTA + Ansi.BOLD,
}

# ── 模块名颜色映射 ──
_MODULE_COLORS: dict[str, str] = {
    "src.poller": Ansi.CYAN,
    "src.dws_adapter": Ansi.BRIGHT_MAGENTA,
    "src.im_adapter": Ansi.BRIGHT_MAGENTA,
    "src.llm": Ansi.MAGENTA,
    "src.rule_engine": Ansi.GREEN,
    "src.intent": Ansi.GREEN,
    "src.memory": Ansi.YELLOW,
    "src.skills": Ansi.YELLOW,
    "src.tools": Ansi.BLUE,
    "src.kb": Ansi.BLUE,
    "src.platform": Ansi.CYAN,
    "__main__": Ansi.GREEN,
    "web": Ansi.BLUE,
}

_MODULE_WIDTH = 24  # 模块名字段固定宽度


def _module_color(name: str) -> str:
    for prefix, color in _MODULE_COLORS.items():
        if name.startswith(prefix):
            return color
    return Ansi.GRAY


def _format_module(name: str) -> str:
    """模块名固定宽度对齐，超出截断。"""
    if len(name) > _MODULE_WIDTH:
        name = "…" + name[-(_MODULE_WIDTH - 1):]
    return f"{name:<{_MODULE_WIDTH}}"


_MULTI_LINE_RE = re.compile(r"\n")


class FancyConsoleFormatter(logging.Formatter):
    """增强控制台日志格式化器。

    - 单行：``HH:MM:SS 🟢 INFO    module_name          : 消息内容``
    - 多行：自动在后续行添加 │ 缩进边框。
    """

    def __init__(self):
        super().__init__()
        self._startup_time: float | None = None

    def mark_startup(self) -> None:
        """标记启动时刻，供启动横幅计算耗时。"""
        self._startup_time = time.time()

    def _build_prefix(self, record: logging.LogRecord) -> str:
        """构建单行日志前缀：时间戳 + 级别 emoji + 模块名。"""
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        emoji = LEVEL_EMOJI.get(record.levelno, "  ")
        level_color = LEVEL_COLORS.get(record.levelno, "")
        module = record.name

        # 级别名统一左对齐到 8 格（最长的 CRITICAL=8），保证列宽一致
        padded_level = f"{record.levelname:<8}"
        prefix = (
            f"{Ansi.DIM}{ts}{Ansi.RESET} "
            f"{level_color}{emoji} {padded_level}{Ansi.RESET} "
            f"{_module_color(module)}{_format_module(module)}{Ansi.RESET}"
        )
        return prefix

    def format(self, record: logging.LogRecord) -> str:
        prefix = self._build_prefix(record)
        msg = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        # 单行消息
        if "\n" not in msg and not record.exc_text:
            return f"{prefix} : {msg}"

        # 多行消息：首行正常，后续行添加缩进边框
        lines = msg.split("\n")
        result_lines = [f"{prefix} : {lines[0]}"]

        # 构建对齐前缀（相同宽度但用空白填充）
        align_prefix = " " * (len(_strip_ansi(prefix)) + 3)  # +3 for " : "
        indent = f"{Ansi.GRAY}{align_prefix[:-1]}│{Ansi.RESET} "

        for line in lines[1:]:
            result_lines.append(f"{indent}{line}")

        # traceback 同样缩进
        if record.exc_text:
            for tb_line in record.exc_text.rstrip("\n").split("\n"):
                result_lines.append(f"{indent}{Ansi.RED}{tb_line}{Ansi.RESET}")

        return "\n".join(result_lines)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


# ── 启动横幅 ──
_STARTUP_BANNER = r"""
{color}
╔══════════════════════════════════════════════════════════╗
║                   灵桥 (Linkora) 启动成功                    ║
╚══════════════════════════════════════════════════════════╝
{reset}"""


class StartupBanner:
    """启动汇总横幅：在启动完成后输出一行汇总信息。"""

    _printed: bool = False

    @classmethod
    def print_banner(cls, formatter: FancyConsoleFormatter | None = None) -> None:
        if cls._printed:
            return
        cls._printed = True

        elapsed = ""
        if formatter and formatter._startup_time is not None:
            dt = time.time() - formatter._startup_time
            elapsed = f" 耗时 {dt:.2f}s"
        line = f"{Ansi.CYAN}━━━ 灵桥(Linkora) 启动完成{elapsed} ━━━{Ansi.RESET}"
        print(line, file=sys.stderr)


# ── 注入入口 ──
def inject_fancy_console_handler(
    *,
    console_level: int = logging.INFO,
) -> FancyConsoleFormatter:
    """替换 root logger 的控制台 handler 为新格式。

    保留文件 handler 和 InMemoryLogHandler，仅替换 RichHandler / StreamHandler。
    返回 formatter 实例，调用方可调用 ``formatter.mark_startup()`` 记录启动时刻。
    """
    formatter = FancyConsoleFormatter()
    formatter.mark_startup()

    root = logging.getLogger()
    new_handler = logging.StreamHandler(sys.stderr)
    new_handler.setLevel(console_level)
    new_handler.setFormatter(formatter)

    # 移除旧的控制台 handler（RichHandler / StreamHandler），保留其他
    to_remove: list[logging.Handler] = []
    for h in root.handlers:
        htype = type(h).__name__
        # RichHandler / StreamHandler 且不是 FileHandler 的 → 要替换的
        if htype in ("RichHandler", "StreamHandler"):
            to_remove.append(h)

    for h in to_remove:
        root.removeHandler(h)

    root.addHandler(new_handler)
    return formatter
