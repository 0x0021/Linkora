#!/usr/bin/env python3
"""One-shot AST extraction: split RuntimeMixin (1826 lines / 66 methods) into 6
cohesive mixin submodules under src/platform/. Mechanical, behavior-preserving:
RuntimeMixin re-composes all sub-mixins + the pre-existing ReplyHelpersMixin via
multiple inheritance, so every `self.xxx` call still resolves via MRO.

Run from repo root. Not committed (scratch tool)."""
import ast
import os
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "platform", "runtime.py")

text = open(SRC, encoding="utf-8").read()
tree = ast.parse(text)

# locate the RuntimeMixin class
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RuntimeMixin")
class_lineno = cls.lineno  # 1-based line where `class RuntimeMixin...` starts

# header = everything before the class line (imports + module-level constants)
header_lines = text.splitlines()[: class_lineno - 1]
header = "\n".join(header_lines) + "\n"
# keep logger name identical to before so log channel strings don't change
header = header.replace('logging.getLogger(__name__)', 'logging.getLogger("src.platform.runtime")')

# method -> group
GROUPS = {
    "lifecycle": ["_init_runtime", "_active_ctx", "store", "dws", "poller", "llm_agent",
                  "_ensure_primary", "_make_platform_callback", "_build_dws", "reload_config"],
    "setup": ["_rebuild_kb_search_tool", "_setup_embedding", "_build_tool_services", "_setup_tools",
              "_setup_llm", "_should_handoff_low_confidence", "_notify_owner_draft", "_load_current_user",
              "_resolve_own_open_dingtalk_id", "_filter_sensitive_words"],
    "reply_guard": ["_mark_inbound_processed", "_cleanup_backoff", "_reply_cooldown_active",
                    "_is_followup_message", "_handle_sensitive_blocked_reply", "_prepare_outgoing_text",
                    "_mark_read_before_reply", "_reply_shard_limit", "_reply_send_min_interval",
                    "_reply_send_rate_limit_backoff_seconds", "_is_rate_limit_exception",
                    "_throttle_reply_send", "_mark_reply_sent", "_reply_rate_limited",
                    "_handle_reply_rate_limited", "_mark_shard_processed", "_send_possibly_sharded"],
    "dispatch": ["_dispatch_reply_send", "_send_single_chat_reply", "_record_reply_success", "_send_reply"],
    "inbound": ["_has_replied_after", "_has_user_taken_over", "_is_message_from_self",
                "_is_internal_confirmation", "_is_oa_approval_message", "_oa_message_blob",
                "_oa_approval_is_question", "_oa_approval_is_action", "_handle_message_impl",
                "_should_skip_inbound", "_handle_media_fallback", "_handle_message_with_rid",
                "_handle_oa_approval_urge", "_apply_rule_result"],
    "llm_reply": ["_track_llm_reply", "_mark_agent_self_replied", "_deliver_llm_reply",
                  "_process_llm_reply", "_handle_rate_limit_exhausted", "_enqueue_dead_letter",
                  "replay_dead_letter"],
}
name2group = {n: g for g, names in GROUPS.items() for n in names}

# collect methods in source order
methods = []  # (group, source)
for node in cls.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        grp = name2group.get(node.name)
        if grp is None:
            raise SystemExit(f"UNMAPPED method: {node.name}")
        # Extract the method's raw source by line range (NOT ast.get_source_segment,
        # which returns the first line at column 0 but the body at its original absolute
        # indent — an inconsistent def/body gap that textwrap.dedent cannot normalize).
        # Line slices preserve the original 4-space def indentation, so dedent+indent
        # then yields a clean 4-space class-method / 8-space body layout.
        # IMPORTANT: include decorator lines (e.g. @property / @x.setter) — for decorated
        # functions node.lineno points at the `def`, so without this the decorators are
        # silently dropped, turning properties into plain methods (catastrophic).
        start = node.lineno
        end = node.end_lineno
        for d in node.decorator_list:
            start = min(start, d.lineno)
            d_end = getattr(d, "end_lineno", None) or node.end_lineno
            end = max(end, d_end)
        src_lines = text.splitlines()[start - 1 : end]
        src = "\n".join(src_lines)
        src = textwrap.dedent(src)
        src = textwrap.indent(src, "    ")
        methods.append((grp, src))

# sanity: all 66 accounted for
total = len(methods)
print(f"extracted {total} methods across groups")
from collections import Counter
print(dict(Counter(g for g, _ in methods)))

CLASS_NAMES = {
    "lifecycle": "LifecycleMixin",
    "setup": "SetupMixin",
    "reply_guard": "ReplyGuardMixin",
    "dispatch": "ReplyDispatchMixin",
    "inbound": "InboundMixin",
    "llm_reply": "LLMReplyMixin",
}

# write submodules
for grp, classname in CLASS_NAMES.items():
    body = [f"class {classname}:"]
    first = True
    for g, src in methods:
        if g != grp:
            continue
        # methods are already indented 4 spaces (class-body level) from runtime.py,
        # which is exactly right under the new class — append as-is.
        if first:
            # attach a one-line docstring to the class
            body.append('    """运行时：%s 相关方法（从 runtime.py 抽离，零行为变更）。"""' % grp)
            first = False
        body.append(src)
    if first:
        body.append('    """运行时：%s 相关方法（从 runtime.py 抽离，零行为变更）。"""' % grp)
        body.append("    pass")
    content = header + "\n" + "\n".join(body) + "\n"
    out = os.path.join(ROOT, "src", "platform", f"runtime_{grp}.py")
    open(out, "w", encoding="utf-8").write(content)
    print(f"wrote {out} ({len([1 for g,_ in methods if g==grp])} methods)")

# rewrite runtime.py as composition root
new_runtime = (
    'from __future__ import annotations\n'
    '\n'
    'from .base import *  # noqa: F403\n'
    'from .base import _active_platform_ctx\n'
    'from .reply_helpers import ReplyHelpersMixin, _citation_relevant_to_reply  # 既有回复增强 mixin + 兼容 re-export\n'
    'from .runtime_reply_guard import (  # 兼容 re-export：原 runtime.py 经这些名字对外暴露（含单测的 import 与 monkeypatch）\n'
    '    SHARD_SEND_INTERVAL_SECONDS,\n'
    '    REPLY_SEND_MIN_INTERVAL_DEFAULT,\n'
    '    REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT,\n'
    '    _RATE_LIMIT_HINTS,\n'
    ')\n'
    'from .runtime_lifecycle import LifecycleMixin\n'
    'from .runtime_setup import SetupMixin\n'
    'from .runtime_reply_guard import ReplyGuardMixin\n'
    'from .runtime_dispatch import ReplyDispatchMixin\n'
    'from .runtime_inbound import InboundMixin\n'
    'from .runtime_llm_reply import LLMReplyMixin\n'
    '\n'
    '\n'
    'class RuntimeMixin(  # noqa: F811  (组合运行时子 mixin)\n'
    '    LifecycleMixin,\n'
    '    SetupMixin,\n'
    '    ReplyGuardMixin,\n'
    '    ReplyDispatchMixin,\n'
    '    InboundMixin,\n'
    '    LLMReplyMixin,\n'
    '    ReplyHelpersMixin,  # 保持原继承位置（回复增强子系统）\n'
    '):\n'
    '    """组合运行时 mixin。\n'
    '\n'
    '    原 src/platform/runtime.py 单文件 1826 行 / 66 方法（可读性债），按内聚职责拆分为\n'
    '    6 个子类 mixin（生命周期/初始化、工具与 LLM 装配、回复护栏与分片、回复分发、\n'
    '    入站处理与 OA、LLM 回复与死信），经多继承组合。各方法名跨组唯一，方法解析顺序(MRO)\n'
    '    保持原语义，`self.xxx` 调用全部依旧解析。零行为变更。\n'
    '    """\n'
)
open(SRC, "w", encoding="utf-8").write(new_runtime)
print(f"rewrote {SRC} as composition root")
