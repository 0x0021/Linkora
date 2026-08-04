from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.memory.few_shot_diversity import (
    greedy_select,
    len_bucket,
    normalize,
    topic_key,
    trigram_similarity,
)
from pathlib import Path
from typing import Optional

import numpy as np

from src.config import DEFAULT_STORAGE_PATH
from src.memory.schema import init_schema, init_conv_schema
from src.memory import account_identity
from src.models import Message

logger = logging.getLogger(__name__)


@dataclass
class ConversationSummaryRow:
    """H2-A 会话摘要缓存行（conversation_summaries 表的读模型）。

    仅承载 agent._read_cached_summary 判定新鲜度/覆盖率所需的字段；
    写回由 SQLiteStore.upsert_conversation_summary 负责（CAS 代际）。
    """

    chat_id: str
    summary_text: str
    older_boundary_msg_id: str
    covered_count: int
    generation: int
    created_at: str
    updated_at: str

# Shared faiss index lock — imported from src.memory.index_lock
from src.memory.index_lock import _INDEX_LOCK, with_index_lock

logger = logging.getLogger(__name__)


@dataclass
class ConversationSummaryRow:
    """H2-A 会话摘要缓存行（conversation_summaries 表的读模型）。

    仅承载 agent._read_cached_summary 判定新鲜度/覆盖率所需的字段；
    写回由 SQLiteStore.upsert_conversation_summary 负责（CAS 代际）。
    """

    chat_id: str
    summary_text: str
    older_boundary_msg_id: str
    covered_count: int
    generation: int
    created_at: str
    updated_at: str


_with_index_lock = with_index_lock  # 兼容别名


_PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\d{3,4}-?\d{7,8})(?!\d)")


_IDCARD_RE = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


_ADDR_RE = re.compile(
    r"(?:[\u4e00-\u9fa5]{1,8}(?:省|市|区|县|镇|乡|街道|社区|村))|"
    r"(?:[\u4e00-\u9fa5]{1,8}(?:路|街|道|巷|弄)\d*号?)|"
    r"(?:\d+[\u4e00-\u9fa5]*(?:路|街|道|巷|弄))|"
    r"(?:\d+栋|\d+幢|\d+座|\d+单元|\d+室|\d+房)|"
    r"(?:[\u4e00-\u9fa5]{1,8}(?:小区|花园|苑|大厦|广场|公寓|别墅|新村))"
)


_ADDR_FINE_RE = re.compile(
    r"(?:\d+号楼|\d+[栋幢座楼])[^，。\n\s]{0,12}|"
    r"(?:[\u4e00-\u9fa5]{1,8}省[\u4e00-\u9fa5]{1,8}市[\u4e00-\u9fa5]{1,8}(?:区|县)"
    r"(?:[\u4e00-\u9fa5]{1,8}(?:路|街|道|巷|弄)\d*号?|[\u4e00-\u9fa5]{1,8}[栋幢座]\d*[单元室房]?)?)|"
    r"(?:[\u4e00-\u9fa5]{1,8}(?:路|街|道|巷|弄)\d+号\s*[\u4e00-\u9fa5]{0,4}\d*[栋幢座单元室房]*)"
)


_PII_PATTERNS = (_PHONE_RE, _IDCARD_RE, _EMAIL_RE, _ADDR_RE, _ADDR_FINE_RE)


_PII_PLACEHOLDER = "[已脱敏]"


def _has_residual_pii(text: str) -> bool:
    """二次校验：脱敏后文本是否仍残留任一 PII 模式（用于监控 LLM 后处理是否彻底）。"""
    if not text:
        return False
    return any(pat.search(text) for pat in _PII_PATTERNS)


_INAPPROPRIATE_HINTS = (
    "法轮", "操你", "草你", "傻逼", "傻屄", "妈的", "他妈", "卧槽", "我草",
    "去死", "滚蛋", "贱人", "婊子", "日你", "屁眼", "鸡巴", "乳房", "骚货",
    "精液", "性爱", "做爱", "强奸", "嫖娼", "裸聊", "约炮", "卖淫",
)


def _redact_pii(text: str) -> str:
    """对文本做 PII 脱敏：手机号 / 身份证 / 邮箱 / 地址 → 占位符。

    幂等：已脱敏文本不会被二次破坏（正则不匹配占位符本身）。
    """
    if not text:
        return text
    for pat in _PII_PATTERNS:
        text = pat.sub(_PII_PLACEHOLDER, text)
    return text


def _is_inappropriate(text: str) -> bool:
    """命中不当内容拦截词返回 True（最低限度护栏，不追求穷尽）。"""
    if not text:
        return False
    t = text.lower()
    return any(hint in t for hint in _INAPPROPRIATE_HINTS)


def clean_document_content(content: str, source_format: str = "auto") -> str:
    """清洗文档内容，去除无效信息。

    Args:
        content: 原始文档内容
        source_format: 源格式 (html/markdown/auto)

    Returns:
        清洗后的纯文本内容
    """
    if not content:
        return ""

    # 自动检测格式
    if source_format == "auto":
        if "<html" in content.lower() or "<body" in content.lower():
            source_format = "html"
        elif content.strip().startswith("#") or "**" in content or "```" in content:
            source_format = "markdown"

    if source_format == "html":
        content = _clean_html(content)
    elif source_format == "markdown":
        content = _clean_markdown(content)

    # 通用清理
    content = _clean_common_artifacts(content)

    return content.strip()


def _clean_html(html_content: str) -> str:
    """清理 HTML 标签和无效内容。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')

        # 移除脚本和样式
        for tag in soup(['script', 'style', 'meta', 'link']):
            tag.decompose()

        # 提取纯文本
        text = soup.get_text(separator='\n', strip=True)

        # 清理多余空行
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return '\n'.join(lines)

    except ImportError:
        # 无 BeautifulSoup 时使用正则降级方案
        logger.warning("bs4 不可用，使用正则表达式降级方案处理 HTML")
        # 移除 HTML 标签
        text = re.sub(r'<[^>]+>', '', html_content)
        # 解码常见 HTML 实体
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&amp;', '&')
        text = text.replace('&quot;', '"')
        # 清理多余空白
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        return '\n'.join(lines)


def _clean_markdown(md_content: str) -> str:
    """清理 Markdown 格式标记。"""
    # 移除代码块（保留内容）
    md_content = re.sub(r'```[\s\S]*?```', '', md_content)

    # 移除图片链接
    md_content = re.sub(r'!\[.*?\]\(.*?\)', '', md_content)

    # 转换链接为纯文本
    md_content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', md_content)

    # 移除标题标记
    md_content = re.sub(r'^#{1,6}\s+', '', md_content, flags=re.MULTILINE)

    # 移除粗体/斜体标记
    md_content = re.sub(r'\*\*(.+?)\*\*', r'\1', md_content)
    md_content = re.sub(r'\*(.+?)\*', r'\1', md_content)
    md_content = re.sub(r'__(.+?)__', r'\1', md_content)
    md_content = re.sub(r'_(.+?)_', r'\1', md_content)

    # 移除行内代码标记
    md_content = re.sub(r'`([^`]+)`', r'\1', md_content)

    # 移除引用标记
    md_content = re.sub(r'^>\s+', '', md_content, flags=re.MULTILINE)

    # 移除水平线
    md_content = re.sub(r'^[-*_]{3,}\s*$', '', md_content, flags=re.MULTILINE)

    # 移除列表标记
    md_content = re.sub(r'^[\s]*[-*+]\s+', '', md_content, flags=re.MULTILINE)
    md_content = re.sub(r'^\s*\d+\.\s+', '', md_content, flags=re.MULTILINE)

    # 清理多余空行
    lines = [line.strip() for line in md_content.split('\n') if line.strip()]
    return '\n'.join(lines)


def _clean_common_artifacts(text: str) -> str:
    """清理通用无效内容。"""
    # 移除过多连续空行（最多保留2个）
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 移除首尾空白
    lines = [line.rstrip() for line in text.split('\n')]

    # 移除纯空白行
    lines = [line for line in lines if line.strip()]

    return '\n'.join(lines)


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    if not vec1 or not vec2:
        return 0.0
    # 维度不一致（如旧模型 768 维向量 vs 当前 1024 维）直接跳过，避免
    # "shapes (1024,) and (768,) not aligned" 报错。静默返回 0.0（不相似）。
    if len(vec1) != len(vec2):
        logger.debug(
            "余弦相似度跳过：向量维度不匹配 (%d vs %d)", len(vec1), len(vec2)
        )
        return 0.0
    try:
        a = np.array(vec1)
        b = np.array(vec2)
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except Exception as e:
        logger.warning("余弦相似度计算失败: %s", e)
        return 0.0


from src.memory.sqlite_store_conn import SQLiteStoreConnMixin
from src.memory.sqlite_store_index import SQLiteStoreIndexMixin


class SQLiteStore(SQLiteStoreConnMixin, SQLiteStoreIndexMixin):
    def __init__(self, db_path: str = DEFAULT_STORAGE_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # 【架构重构】per-thread 连接缓存：key=thread ident，value=该线程独立 Connection。
        # 各线程通过 self.conn 拿到自己线程的连接，绝不跨线程共享，
        # 满足 docs/architecture.md 核心约束 "SQLite 连接禁止跨线程共享"。
        self._conns: dict[int, sqlite3.Connection] = {}
        self._conns_lock = threading.Lock()  # 仅保护 _conns 字典本身
        self._closed = False  # 防止 close() 后再创建新连接
        self._max_conns = 64  # 每线程连接回收上限（P2-9：防止动态线程增长导致 FD 泄漏）
        self._vector_index: Optional[object] = None
        self._index_dim: int = 0
        # 当前已加载 FAISS 对应的 KB 版本号；每次 KB 写入/重索引自增。
        # 用于替代「仅比 chunk 数量」的同步判据，覆盖「同计数、向量被重索引」场景。
        self._index_revision: int = 0
        self._lock = threading.RLock()  # 保留：单线程内写操作串行安全网（与 per-thread 连接并存无害）
        self._schema_initialized = False  # 首次 conn 访问时自动 init_db()
        # ── per-account 会话连接（账号隔离）：key=(thread_id, platform)，
        #    value=(db_path, Connection)。每个 (线程×平台) 独立连接，且按当前账号
        #    身份路由到 data_root/conversations/<platform>__<hash>.db。
        #    账号切换（re-login）导致 db_path 变化时自动重开，旧账号 DB 不再被打开。
        self._conv_conns: dict[tuple[int, str], tuple[str, sqlite3.Connection]] = {}
        self._conv_conns_lock = threading.Lock()
        self._conv_migrated: set[str] = set()  # 已执行过首次迁移的 db_path
        self._conv_root = os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "conversations")
        # ── Lazy repo instances (extracted submodules) ──
        self.__blacklist_repo = None
        self.__conversation_repo = None
        self.__draft_repo = None
        self.__external_friend_repo = None
        self.__kb_repo = None
        self.__memory_repo = None
        self.__message_repo = None
        self.__decisions_repo = None
        self.__routing_quality_repo = None
        self.__docs_repo = None
        self.__memory_ops_repo = None
        self.__few_shot_repo = None
        self.__baseline_repo = None
        self.__feedback_repo = None
        self.__keyword_rule_repo = None
        self.__tool_execution_repo = None

        _MIGRATE_PLATFORM_PREFIXES = {
            "feishu": ["oc_"],
            "dingtalk": ["cid", "DD"],  # DD 开头为钉钉单聊以 open_id 直作 chat_id 的形态
            "wecom": None,
        }

        DEFAULT_COOLDOWN_HOURS = 1  # 默认冷却时长（不传 cooldown_until 时启用）

        PERMANENT_BLOCK_THRESHOLD = 3  # 连续失败 ≥ 该阈值才升级永久黑名单

    def _remove_chunks_from_index(self, chunk_ids: list[int]) -> None:
        return self._kb_repo._remove_chunks_from_index(chunk_ids)

    def add_keyword_rule(self, match_pattern: str, reply_text: str,
                         category: str = "default", match_type: str = "fuzzy",
                         priority: int = 0, enabled: int = 1) -> int:
        return self._keyword_rule_repo.add(match_pattern, reply_text, category, match_type, priority, enabled)

    def list_keyword_rules(self, category: str = "", enabled: int | None = None,
                           limit: int = 200) -> list[dict]:
        return self._keyword_rule_repo.list(category, enabled, limit)

    def get_keyword_rule(self, rule_id: int) -> dict | None:
        return self._keyword_rule_repo.get(rule_id)

    def update_keyword_rule(self, rule_id: int, **kwargs) -> None:
        self._keyword_rule_repo.update(rule_id, **kwargs)

    def delete_keyword_rule(self, rule_id: int) -> None:
        self._keyword_rule_repo.delete(rule_id)

    def keyword_categories(self) -> list[str]:
        return self._keyword_rule_repo.categories()

    def batch_import_keywords(self, rules: list[dict]) -> dict:
        imported = 0
        skipped = 0
        for r in rules:
            try:
                match_pattern = r.get("keywords") or r.get("match_pattern") or r.get("match") or ""
                reply_text = r.get("reply") or r.get("reply_text") or ""
                match_type = r.get("match_type") or "fuzzy"
                category = r.get("category") or "default"
                priority = r.get("priority") or 0
                enabled = 0 if r.get("enabled") is False else 1
                if match_pattern and reply_text:
                    self.add_keyword_rule(
                        match_pattern=match_pattern,
                        reply_text=reply_text,
                        category=category,
                        match_type=match_type,
                        priority=priority,
                        enabled=enabled,
                    )
                    imported += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning("知识库导入单条记录失败: %s", e)
                skipped += 1
        return {"imported": imported, "skipped": skipped}

    def increment_keyword_hit(self, rule_id: int) -> None:
        self._keyword_rule_repo.increment_hit(rule_id)

    def count_keyword_rules(self, enabled: int | None = None) -> int:
        return self._keyword_rule_repo.count(enabled)

    def keyword_rules_stats(self, top_hits_limit: int = 50) -> dict:
        return self._keyword_rule_repo.stats(top_hits_limit)

    def log_tool_execution(
        self,
        tool_name: str,
        input_args: str,
        output_result: str,
        success: bool,
        duration_ms: float,
        error_message: str = "",
    ) -> None:
        self._tool_execution_repo.log(tool_name, input_args, output_result, success, duration_ms, error_message)

    def get_tool_call_stats(self, days: int) -> list[dict]:
        return self._tool_execution_repo.stats(days)

    def get_tool_call_health(self) -> list[dict]:
        return self._tool_execution_repo.health()

    def set_decisions_retention_days(self, days: int) -> None:
        self._decisions_repo.set_decisions_retention_days(days)

    def _prune_decisions(self) -> None:
        self._decisions_repo._prune_decisions()

    def update_routing_quality_trace(self, rq_id: int, llm_latency_ms: float = 0.0,
                                     llm_rounds: int = 0, llm_model: str = "",
                                     total_latency_ms: float = 0.0, reply_len: int = 0,
                                     reply_text: str = "", stages_json: str = "[]",
                                     input_tokens: int = 0, output_tokens: int = 0,
                                     total_tokens: int = 0, cost_usd: float = 0.0) -> None:
        self._routing_quality_repo.update_routing_quality_trace(
            rq_id, llm_latency_ms, llm_rounds, llm_model, total_latency_ms,
            reply_len, reply_text, stages_json,
            input_tokens, output_tokens, total_tokens, cost_usd)

    def set_rq_retention_days(self, days: int) -> None:
        self._routing_quality_repo.set_rq_retention_days(days)

    def _prune_routing_quality(self) -> None:
        self._routing_quality_repo._prune_routing_quality()

    @property
    def _blacklist_repo(self):
        """Lazy-load BlacklistRepo."""
        if self.__blacklist_repo is None:
            from src.memory.blacklist_repo import BlacklistRepo
            self.__blacklist_repo = BlacklistRepo(self)
        return self.__blacklist_repo

    @property
    def _conversation_repo(self):
        """Lazy-load ConversationRepo."""
        if self.__conversation_repo is None:
            from src.memory.conversation_repo import ConversationRepo
            self.__conversation_repo = ConversationRepo(self)
        return self.__conversation_repo

    @property
    def _draft_repo(self):
        """Lazy-load DraftRepo."""
        if self.__draft_repo is None:
            from src.memory.draft_repo import DraftRepo
            self.__draft_repo = DraftRepo(self)
        return self.__draft_repo

    @property
    def _external_friend_repo(self):
        """Lazy-load ExternalFriendRepo."""
        if self.__external_friend_repo is None:
            from src.memory.external_friend_repo import ExternalFriendRepo
            self.__external_friend_repo = ExternalFriendRepo(self)
        return self.__external_friend_repo

    @property
    def _kb_repo(self):
        """Lazy-load KbRepo."""
        if self.__kb_repo is None:
            from src.memory.kb_repo import KbRepo
            self.__kb_repo = KbRepo(self)
        return self.__kb_repo

    @property
    def _memory_repo(self):
        """Lazy-load MemoryRepo."""
        if self.__memory_repo is None:
            from src.memory.memory_repo import MemoryRepo
            self.__memory_repo = MemoryRepo(self)
        return self.__memory_repo

    @property
    def _message_repo(self):
        """Lazy-load MessageRepo."""
        if self.__message_repo is None:
            from src.memory.message_repo import MessageRepo
            self.__message_repo = MessageRepo(self)
        return self.__message_repo

    @property
    def _decisions_repo(self):
        """Lazy-load DecisionsRepo."""
        if self.__decisions_repo is None:
            from src.memory.decisions_repo import DecisionsRepo
            self.__decisions_repo = DecisionsRepo(self)
        return self.__decisions_repo

    @property
    def _routing_quality_repo(self):
        """Lazy-load RoutingQualityRepo."""
        if self.__routing_quality_repo is None:
            from src.memory.routing_quality_repo import RoutingQualityRepo
            self.__routing_quality_repo = RoutingQualityRepo(self)
        return self.__routing_quality_repo

    @property
    def _docs_repo(self):
        """Lazy-load DocsRepo."""
        if self.__docs_repo is None:
            from src.memory.docs_repo import DocsRepo
            self.__docs_repo = DocsRepo(self)
        return self.__docs_repo

    @property
    def _memory_ops_repo(self):
        """Lazy-load MemoryOpsRepo."""
        if self.__memory_ops_repo is None:
            from src.memory.memory_ops_repo import MemoryOpsRepo
            self.__memory_ops_repo = MemoryOpsRepo(self)
        return self.__memory_ops_repo

    @property
    def _few_shot_repo(self):
        """Lazy-load FewShotRepo."""
        if self.__few_shot_repo is None:
            from src.memory.few_shot_repo import FewShotRepo
            self.__few_shot_repo = FewShotRepo(self)
        return self.__few_shot_repo

    @property
    def _baseline_repo(self):
        """Lazy-load BaselineRepo."""
        if self.__baseline_repo is None:
            from src.memory.baseline_repo import BaselineRepo
            self.__baseline_repo = BaselineRepo(self)
        return self.__baseline_repo

    @property
    def _feedback_repo(self):
        """Lazy-load FeedbackRepo."""
        if self.__feedback_repo is None:
            from src.memory.feedback_repo import FeedbackRepo
            self.__feedback_repo = FeedbackRepo(self)
        return self.__feedback_repo

    @property
    def _keyword_rule_repo(self):
        """Lazy-load KeywordRuleRepo."""
        if self.__keyword_rule_repo is None:
            from src.memory.keyword_rule_repo import KeywordRuleRepo
            self.__keyword_rule_repo = KeywordRuleRepo(self)
        return self.__keyword_rule_repo

    @property
    def _tool_execution_repo(self):
        """Lazy-load ToolExecutionRepo."""
        if self.__tool_execution_repo is None:
            from src.memory.tool_execution_repo import ToolExecutionRepo
            self.__tool_execution_repo = ToolExecutionRepo(self)
        return self.__tool_execution_repo

    @_with_index_lock
    def close(self) -> None:
        if self._vector_index:
            try:
                self._vector_index.save()
            except Exception as e:
                logger.warning("保存向量索引 %s 失败了", e)
        # 关闭所有线程持有的连接（per-thread 连接字典，见 __init__）
        with self._conns_lock:
            self._closed = True
            conns = list(self._conns.values())
            self._conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception as e:
                logger.debug("关闭数据库连接失败: %s", e)
        # 关闭 per-account 会话连接
        with self._conv_conns_lock:
            conv_items = list(self._conv_conns.values())
            self._conv_conns.clear()
            self._conv_migrated.clear()
        for _path, c in conv_items:
            try:
                c.close()
            except Exception as e:
                logger.debug("关闭会话数据库连接失败: %s", e)
