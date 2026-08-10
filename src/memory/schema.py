"""SQLite 数据库 schema 初始化与迁移。

将 DDL 语句与兼容迁移逻辑从 SQLiteStore 中独立抽离，
降低 sqlite_store.py 的体量。
"""

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, col_def: str) -> None:
    """为已有表补充新列（兼容旧数据库）。

    使用 PRAGMA table_info 前置检查列是否存在，替代 try/except 的粗糙幂等。
    若整个表不存在则直接跳过，避免 "no such table" 导致整个 init 失败。
    """
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not cursor.fetchone():
        return
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """初始化/迁移数据库 schema。

    该函数幂等：所有 DDL 使用 IF NOT EXISTS 或前置列检查。
    """
    cur = conn.cursor()

    # ── 核心表 ──────────────────────────────────────────────────────────
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS dedup_messages (
            msg_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT,
            chat_type TEXT NOT NULL,
            peer_user_id TEXT,
            peer_open_dingtalk_id TEXT,
            last_message_time TEXT,
            message_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            chat_type TEXT,
            msg_id TEXT UNIQUE,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            msg_type TEXT,
            timestamp TEXT,
            role TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_sender_ts ON messages(chat_id, sender_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT,
            content TEXT NOT NULL,
            source TEXT,
            chat_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            embedding TEXT,
            created_at TEXT NOT NULL,
            scope TEXT DEFAULT 'personal'
        );

        CREATE INDEX IF NOT EXISTS idx_memories_chat_id ON memories(chat_id);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_sender ON memories(sender_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key ON memories(key);

        CREATE TABLE IF NOT EXISTS kb_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_id TEXT,
            url TEXT,
            chunk_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kb_docs_status ON kb_documents(status);
        CREATE INDEX IF NOT EXISTS idx_kb_docs_type ON kb_documents(doc_type);

        CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding TEXT,
            retry_pending INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);

        CREATE TABLE IF NOT EXISTS keyword_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT DEFAULT 'default',
            match_pattern TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            match_type TEXT DEFAULT 'fuzzy',
            priority INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            hit_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_kw_category ON keyword_rules(category);
        CREATE INDEX IF NOT EXISTS idx_kw_enabled ON keyword_rules(enabled);

        CREATE TABLE IF NOT EXISTS dingtalk_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            doc_type TEXT,
            space_id TEXT,
            parent_id TEXT,
            url TEXT,
            content TEXT,
            last_modified TEXT,
            synced_at TEXT,
            auto_sync INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ddoc_title ON dingtalk_docs(title);

        CREATE TABLE IF NOT EXISTS tool_execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            input_args TEXT,
            output_result TEXT,
            success INTEGER DEFAULT 1,
            duration_ms REAL,
            error_message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tool_logs_name ON tool_execution_logs(tool_name);
        CREATE INDEX IF NOT EXISTS idx_tool_logs_created ON tool_execution_logs(created_at);
    """)

    # ── 列迁移：兼容旧数据库缺失字段 ──────────────────────────────────
    _ensure_column(cur, "conversations", "peer_user_id", "TEXT")
    _ensure_column(cur, "conversations", "peer_open_dingtalk_id", "TEXT")
    _ensure_column(cur, "conversations", "last_reply_time", "TEXT")
    _ensure_column(cur, "messages", "role", "TEXT")
    _ensure_column(cur, "messages", "chat_type", "TEXT")
    _ensure_column(cur, "dingtalk_docs", "auto_sync", "INTEGER DEFAULT 0")
    _ensure_column(cur, "tool_execution_logs", "input_args", "TEXT")
    _ensure_column(cur, "tool_execution_logs", "output_result", "TEXT")
    _ensure_column(cur, "messages", "image_path", "TEXT DEFAULT ''")
    _ensure_column(cur, "messages", "is_bot", "INTEGER DEFAULT 0")
    _ensure_column(cur, "messages", "is_archived", "INTEGER DEFAULT 0")
    _ensure_column(cur, "messages", "skip_reason", "TEXT")
    _ensure_column(cur, "messages", "is_withdrawn", "INTEGER DEFAULT 0")
    _ensure_column(cur, "decisions", "skill_name", "TEXT DEFAULT ''")
    _ensure_column(cur, "decisions", "skill_source", "TEXT DEFAULT ''")
    _ensure_column(cur, "memories", "sender_id", "TEXT")
    _ensure_column(cur, "memories", "sender_name", "TEXT")
    _ensure_column(cur, "memories", "scope", "TEXT DEFAULT 'personal'")
    _ensure_column(cur, "kb_chunks", "retry_pending", "INTEGER DEFAULT 0")
    _ensure_column(cur, "conversations", "last_summary_at", "TEXT")
    _ensure_column(cur, "conversations", "last_replied_msg_id", "TEXT")

    # ── 补充索引 ───────────────────────────────────────────────────────
    _try_create_index(cur, "idx_memories_scope ON memories(scope)")
    _try_create_index(cur, "idx_ddoc_auto_sync ON dingtalk_docs(auto_sync)")
    _try_create_index(cur, "idx_messages_archived ON messages(chat_id, is_archived)")
    _try_create_index(cur, "idx_messages_chat_ts ON messages(chat_id, timestamp)")
    _try_create_index(cur, "idx_memories_sender ON memories(sender_id)")

    # ── 外部好友映射表 ─────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS external_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            open_dingtalk_id TEXT NOT NULL UNIQUE,
            chat_id TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ef_name ON external_friends(name)")

    # ── 不遍历黑名单 ───────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_conversations (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT,
            chat_type TEXT,
            reason TEXT,
            detected_at TEXT NOT NULL,
            source TEXT,
            last_error TEXT,
            cooldown_until TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_blocked_source ON blocked_conversations(source)")

    # 迁移：blocked_conversations 老表补列
    existing_cols = {
        row["name"]
        for row in cur.execute("PRAGMA table_info(blocked_conversations)").fetchall()
    }
    for col_name, col_ddl in (
        ("cooldown_until", "ALTER TABLE blocked_conversations ADD COLUMN cooldown_until TEXT"),
        ("failure_count", "ALTER TABLE blocked_conversations ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"),
    ):
        if col_name in existing_cols:
            continue
        try:
            cur.execute(col_ddl)
        except Exception:
            logger.warning("[resilience] add column %s failed", col_name, exc_info=True)

    # ── 死信队列 ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dead_letter_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id TEXT,
            chat_id TEXT,
            chat_name TEXT,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            msg_type TEXT,
            stage TEXT NOT NULL DEFAULT 'llm_inference',
            error TEXT,
            raw TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            replayed_at TEXT,
            replay_note TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dl_status ON dead_letter_messages(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dl_chat ON dead_letter_messages(chat_id)")

    # ── 草稿管理表 ─────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id TEXT UNIQUE NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT 'single',
            sender_id TEXT NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            user_message TEXT NOT NULL,
            ai_reply TEXT NOT NULL,
            rag_confidence REAL,
            rag_threshold REAL,
            rag_best_chunk TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            processed_by TEXT DEFAULT '',
            final_reply TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_md_status ON message_drafts(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_md_platform ON message_drafts(platform)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_md_draft_id ON message_drafts(draft_id)")

    # ── 决策追踪持久化表 ───────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            sender_name TEXT DEFAULT '',
            conversation_id TEXT DEFAULT '',
            conversation_name TEXT DEFAULT '',
            content_preview TEXT DEFAULT '',
            intent TEXT DEFAULT '',
            action TEXT NOT NULL,
            routing_mode TEXT DEFAULT '',
            routed_tools TEXT DEFAULT '',
            skill_name TEXT DEFAULT '',
            skill_source TEXT DEFAULT '',
            reply_preview TEXT DEFAULT '',
            request_id TEXT DEFAULT '',
            platform_id TEXT DEFAULT '',
            llm_calls INTEGER DEFAULT 0,
            fallback_used INTEGER DEFAULT 0,
            tool_calls INTEGER DEFAULT 0,
            total_latency_ms INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    for _col, _ddl in [
        ("request_id", "ALTER TABLE decisions ADD COLUMN request_id TEXT DEFAULT ''"),
        ("platform_id", "ALTER TABLE decisions ADD COLUMN platform_id TEXT DEFAULT ''"),
        ("llm_calls", "ALTER TABLE decisions ADD COLUMN llm_calls INTEGER DEFAULT 0"),
        ("fallback_used", "ALTER TABLE decisions ADD COLUMN fallback_used INTEGER DEFAULT 0"),
        ("tool_calls", "ALTER TABLE decisions ADD COLUMN tool_calls INTEGER DEFAULT 0"),
        ("total_latency_ms", "ALTER TABLE decisions ADD COLUMN total_latency_ms INTEGER DEFAULT 0"),
        # 成本/质量看板（Roadmap ③）质量标记：低置信转人工 / RAG 命中 / 引文页脚命中
        ("handoff", "ALTER TABLE decisions ADD COLUMN handoff INTEGER DEFAULT 0"),
        ("rag_grounded", "ALTER TABLE decisions ADD COLUMN rag_grounded INTEGER DEFAULT 0"),
        ("cited", "ALTER TABLE decisions ADD COLUMN cited INTEGER DEFAULT 0"),
    ]:
        try:
            cur.execute(f"SELECT {_col} FROM decisions LIMIT 0")
        except Exception:
            try:
                cur.execute(_ddl)
            except Exception as e:
                logger.debug("[resilience] add column %s failed: %s", _col, e)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_sender ON decisions(sender_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_decisions_rid ON decisions(request_id)")

    # ── 路由质量追踪表 ─────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS routing_quality (
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
            stages_json TEXT DEFAULT '[]',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_routing_quality_skill ON routing_quality(primary_skill)")

    # 路由质量表补充 token/cost 列（兼容旧库：本版本前建表不含这些列，
    # 导致 update_routing_quality_trace 写入静默失败、token_stats 走 available=False 全 0）。
    _ensure_column(cur, "routing_quality", "input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "output_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "total_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "cost_usd", "REAL DEFAULT 0.0")

    # ── 风格画像表 ─────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS style_profiles (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            profile_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS style_profile_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_no INTEGER NOT NULL,
            profile_json TEXT NOT NULL,
            trigger TEXT NOT NULL DEFAULT 'manual',
            confidence TEXT DEFAULT '',
            cleaned_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spv_no ON style_profile_versions(version_no DESC)")

    # ── 通用键值表 ─────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ── 回复反馈表 ─────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            conversation_id TEXT DEFAULT '',
            sender_id TEXT DEFAULT '',
            rating INTEGER NOT NULL,
            correction TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_msg ON feedback(message_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_routing_quality_created ON routing_quality(created_at)")

    # ── 会话摘要缓存表 ────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            chat_id                 TEXT PRIMARY KEY,
            summary_text           TEXT NOT NULL,
            older_boundary_msg_id  TEXT NOT NULL,
            covered_count          INTEGER NOT NULL,
            generation             INTEGER NOT NULL DEFAULT 0,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cs_updated ON conversation_summaries(updated_at)")

    # ── 全链路追踪字段迁移 ─────────────────────────────────────────────
    _ensure_column(cur, "routing_quality", "intent_disposition", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "intent_action", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "intent_actions", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "blocked_by_disabled_skill", "TEXT DEFAULT '[]'")
    _ensure_column(cur, "routing_quality", "message_type", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "llm_model", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "llm_rounds", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "llm_latency_ms", "REAL DEFAULT 0.0")
    _ensure_column(cur, "routing_quality", "total_latency_ms", "REAL DEFAULT 0.0")
    _ensure_column(cur, "routing_quality", "reply_len", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "stages_json", "TEXT DEFAULT '[]'")
    _ensure_column(cur, "routing_quality", "reply_text", "TEXT DEFAULT ''")
    _ensure_column(cur, "routing_quality", "input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "output_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "total_tokens", "INTEGER DEFAULT 0")
    _ensure_column(cur, "routing_quality", "cost_usd", "REAL DEFAULT 0.0")

    # ── 版本表回填 ─────────────────────────────────────────────────────
    try:
        cur.execute("SELECT profile_json, updated_at FROM style_profiles WHERE id = 1")
        sp_row = cur.fetchone()
        if sp_row and sp_row["profile_json"]:
            _existing = cur.execute(
                "SELECT COUNT(*) AS c FROM style_profile_versions"
            ).fetchone()
            if _existing and _existing["c"] == 0:
                _bp = json.loads(sp_row["profile_json"])
                cur.execute(
                    """INSERT INTO style_profile_versions
                           (version_no, profile_json, trigger, confidence, cleaned_count, created_at)
                       VALUES (1, ?, 'baseline', ?, ?, ?)""",
                    (
                        sp_row["profile_json"],
                        _bp.get("confidence", ""),
                        _bp.get("cleaned_count", 0),
                        sp_row["updated_at"],
                    ),
                )
    except Exception:
        logger.warning("[resilience] silent exception in init_schema", exc_info=True)

    conn.commit()

    # ── 完整性检查 ─────────────────────────────────────────────────────
    try:
        cur.execute("PRAGMA integrity_check")
        ok_row = cur.fetchone()
        if ok_row and ok_row[0] != "ok":
            logger.error("数据库完整性检查失败：%s，路径：%s", ok_row[0], db_path)
    except Exception as e:
        logger.error("数据库完整性检查异常：%s，路径：%s", e, db_path)

    logger.debug("数据库状态正常：%s", db_path)


def _try_create_index(cursor: sqlite3.Cursor, index_def: str) -> None:
    """安全创建索引，失败时记录 debug 日志。"""
    try:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {index_def}")
    except sqlite3.OperationalError as e:
        logger.debug("创建索引 %s 失败: %s", index_def.split(" ON ")[0], e)


def init_conv_schema(conn: sqlite3.Connection, db_path: str) -> None:
    """仅初始化「会话相关」表（用于 per-account 独立 DB 文件）。

    与 ``init_schema`` 的区别：只建会话隔离范围内的 6 张表
    （conversations / messages / conversation_summaries / external_friends /
    blocked_conversations / dedup_messages），且一次性建齐全量列（新库无需 ALTER 迁移）。
    其余平台无关表（kb / memories / decisions / feedback / style / drafts ...）留在主库。

    幂等：所有 DDL 使用 IF NOT EXISTS。
    """
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS dedup_messages (
            msg_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT,
            chat_type TEXT NOT NULL,
            peer_user_id TEXT,
            peer_open_dingtalk_id TEXT,
            last_message_time TEXT,
            message_count INTEGER DEFAULT 0,
            last_reply_time TEXT,
            last_replied_msg_id TEXT,
            last_summary_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            chat_type TEXT,
            msg_id TEXT UNIQUE,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            msg_type TEXT,
            timestamp TEXT,
            role TEXT,
            image_path TEXT DEFAULT '',
            is_bot INTEGER DEFAULT 0,
            is_archived INTEGER DEFAULT 0,
            skip_reason TEXT,
            is_withdrawn INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_sender_ts ON messages(chat_id, sender_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

        CREATE TABLE IF NOT EXISTS conversation_summaries (
            chat_id                 TEXT PRIMARY KEY,
            summary_text           TEXT NOT NULL,
            older_boundary_msg_id  TEXT NOT NULL,
            covered_count          INTEGER NOT NULL,
            generation             INTEGER NOT NULL DEFAULT 0,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cs_updated ON conversation_summaries(updated_at);

        CREATE TABLE IF NOT EXISTS external_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            open_dingtalk_id TEXT NOT NULL UNIQUE,
            chat_id TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ef_name ON external_friends(name);

        CREATE TABLE IF NOT EXISTS blocked_conversations (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT,
            chat_type TEXT,
            reason TEXT,
            detected_at TEXT NOT NULL,
            source TEXT,
            last_error TEXT,
            cooldown_until TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_blocked_source ON blocked_conversations(source);
    """)
    conn.commit()
    logger.debug("会话库状态正常：%s", db_path)
