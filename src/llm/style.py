"""主人风格画像 + RAG 检索 + 回复处理模块（薄入口）。

所有实现已迁移到子模块，本文件只做重导出以维持向后兼容：
- src.llm.rag          — RAG 检索、风格画像（load_style_profile/get_style_prompt）、Citation
- src.llm.reply        — 回复清洗（sanitize_reply/gate_reply/enforce_brevity/strip_internal_artifacts）
- src.llm.reply_helper — 末尾补全（ensure_complete_reply）
- src.llm.stream_helper — 流式响应处理（handle_stream_response）

保留 style.py 作为入口的原因：
- agent.py 通过 _style 引用，测试通过 monkey-patch style 模块验证行为
- 新增工具/测试直接 import style 即可拿到所有符号
"""
from __future__ import annotations

# ── RAG 检索 + 风格画像 ──────────────────────────────────────────────────────
from src.llm.rag import (  # noqa: F401
    Citation,
    _LOW_CONF_GUARDRAIL,
    _LOW_CONF_NEUTRAL_STYLE,
    _PROACTIVE_ACTION_CATS,
    _RAG_DISPLAY_SIMILARITY,
    _RAG_GROUND_SIMILARITY,
    cosine,
    embed_message,
    extract_relevant_snippets,
    get_embedding_client,
    get_style_prompt,
    is_document_query,
    load_style_profile,
    retrieve_relevant_knowledge,
)

# ── 回复处理 ─────────────────────────────────────────────────────────────────
from src.llm.reply import (  # noqa: F401
    _GATE_FALLBACK_REPLY,
    _PROCESS_FABRICATION_WORDS,
    _PROCESS_KEYWORDS_RE,
    _REPLY_CONNECTORS,
    _RE_BULLET_LIST,
    _RE_CITATION_FULL,
    _RE_CITATION_SCORE,
    _RE_FEW_SHOT_PAIR,
    _RE_FULL_REASONING_LINE,
    _RE_HAS_TEXT,
    _RE_INLINE_REASONING,
    _RE_INTERNAL_MARKERS,
    _RE_LEADING_THINKING_BLOCK,
    _RE_MD_HEADING,
    _RE_META_NARRATION,
    _RE_ORDERED_LIST,
    _RE_PROMPT_ECHO_LINES,
    _RE_REASONING_PREFIXES,
    _RE_SECOND_PERSON_IDENTITY,
    _RE_TOOL_CALL_XML,
    _REASONING_BODY,
    _REASONING_TAIL,
    _TERMINAL_PUNCT,
    _is_reply_incomplete,
    _segment_is_incomplete,
    _split_sentences,
    enforce_brevity,
    gate_reply,
    sanitize_reply,
    strip_internal_artifacts,
)

# ── 末尾补全 + 流式响应（agent.py 通过 _style 使用）──────────────────────────
from src.llm.reply_helper import ensure_complete_reply  # noqa: F401
from src.llm.stream_helper import handle_stream_response  # noqa: F401
