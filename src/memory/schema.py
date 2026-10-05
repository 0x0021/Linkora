"""SQLite 数据库 schema 初始化与迁移。

将 DDL 语句与兼容迁移逻辑从 SQLiteStore 中独立抽离，
降低 sqlite_store.py 的体量。
"""

import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, col_def: str) -> None:
    """为已有表补充新列（兼容旧数据库）。

    使用 PRAGMA table_info 前置检查列是否存在，替代 try/except 的粗糙幂等。
    若整个表不存在则直接跳过，避免 "no such table" 导致整个 init 失败。

    【HIGH-5 并发安全】同一 db 文件可能被多个 store 实例并发初始化，存在
    "PRAGMA 检查列不存在 → 另一连接抢先 ADD → 本连接 ADD 撞 duplicate column name"
    的竞态窗口。因此对 ALTER 的 "duplicate column"/"already exists" 类错误做幂等兜底：
    捕获后二次核对该列确已存在即视为成功（数据零风险），其它错误照常上抛。
    """
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not cursor.fetchone():
        return
    cursor.execute(f"PRAGMA table_info({table})")
    # 用 row[1]（PRAGMA 第二列即列名）而非 row["name"]，避免依赖 row_factory=sqlite3.Row
    # （生产路径 sqlite_store_conn 设了 Row，但 init_conv_schema 直接接裸连接时不应强依赖）。
    existing = {row[1] for row in cursor.fetchall()}
    if column in existing:
        return
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            # 并发迁移竞态：另一连接已抢先加好该列，二次核验后安全跳过。
            cursor.execute(f"PRAGMA table_info({table})")
            if column in {row[1] for row in cursor.fetchall()}:
                logger.debug("列 %s.%s 已由并发迁移添加，幂等跳过", table, column)
                return
        raise


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
            scope TEXT DEFAULT 'personal',
            -- 摘要化记忆改造（2026-09-23）：
            -- kind: 'fact'（旧版逐条事实，迁移后删除）/ 'summary'（整合摘要，新默认）
            -- group_key: 聚合键——个人记忆= sender_id；公共记忆= 主题桶（如 'public:网络'）
            -- updated_at: 摘要最近一次合并/更新时间，支撑「更新」语义
            kind TEXT DEFAULT 'fact',
            group_key TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_memories_chat_id ON memories(chat_id);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_sender ON memories(sender_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
        CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
        CREATE INDEX IF NOT EXISTS idx_memories_group ON memories(scope, group_key);

        -- 待整合事实缓冲表：自动提取 / 手动保存的事实先落这里，由汇总调度器
        -- 周期性合并进「按 (scope, group_key) 聚合的整合摘要」，避免逐条堆积。
        CREATE TABLE IF NOT EXISTS memory_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT DEFAULT 'personal',
            group_key TEXT,
            content TEXT NOT NULL,
            sender_name TEXT,
            chat_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memory_pending_group ON memory_pending(scope, group_key);
        CREATE INDEX IF NOT EXISTS idx_memory_pending_created ON memory_pending(created_at);

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

        -- 答复门禁规则：对用户提问做内容级拦截（私人隐私 / 违法信息 / 负面情绪 / 其他可扩展）
        CREATE TABLE IF NOT EXISTS gate_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'other',
            category_label TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            match_type TEXT NOT NULL DEFAULT 'keyword',
            pattern TEXT NOT NULL,
            intercept_message TEXT NOT NULL DEFAULT '',
            priority INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            hit_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_gate_category ON gate_rules(category);
        CREATE INDEX IF NOT EXISTS idx_gate_enabled ON gate_rules(enabled);

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
    # 记忆作废/版本控制：superseded_by 指向更新的那条记忆，status='superseded' 的
    # 行在召回时被过滤，实现「同主题只留最新结论、旧结论自动沉底」。
    _ensure_column(cur, "memories", "status", "TEXT DEFAULT 'active'")
    _ensure_column(cur, "memories", "superseded_by", "INTEGER")
    # 摘要化记忆改造（2026-09-23）：旧库补列；新库已在 CREATE TABLE 中带齐。
    _ensure_column(cur, "memories", "kind", "TEXT DEFAULT 'fact'")
    _ensure_column(cur, "memories", "group_key", "TEXT")
    _ensure_column(cur, "memories", "updated_at", "TEXT")
    _ensure_column(cur, "kb_chunks", "retry_pending", "INTEGER DEFAULT 0")
    # KB 分块作废/版本控制：与 memories 同构，支持重投文档时作废旧 chunk 而非累积。
    _ensure_column(cur, "kb_chunks", "status", "TEXT DEFAULT 'active'")
    _ensure_column(cur, "kb_chunks", "superseded_by", "INTEGER")
    _ensure_column(cur, "kb_chunks", "updated_at", "TEXT")
    _ensure_column(cur, "kb_documents", "version", "INTEGER DEFAULT 1")
    _ensure_column(cur, "conversations", "last_summary_at", "TEXT")

    # 【P0 2026-10-05 索引自愈】检测并修复损坏索引。
    #
    # 事故：idx_conversations_updated 出现 49 条「row N missing from index」
    # （integrity_check 可证）。损坏索引会让每次写入都退化为全表扫描重建索引，
    # 写事务耗时暴涨 → 写锁被长期占用 → 其他写者全部 database is locked。
    # 表现为「代码怎么改都治不好」，因为病根在数据文件而非代码。
    # 损坏成因：CREATE INDEX 过程中进程被强杀（--dev 热重载 / 手动重启）。
    #
    # 这里用 PRAGMA integrity_check（quick_check 对索引不一致会漏检，实测踩过），
    # 发现索引类损坏就 DROP + 重建该索引。幂等、无数据损失。
    _heal_corrupted_indexes(cur, db_path)
    _ensure_column(cur, "conversations", "last_replied_msg_id", "TEXT")

    # ── 补充索引 ───────────────────────────────────────────────────────
    _try_create_index(cur, "idx_memories_scope ON memories(scope)")
    _try_create_index(cur, "idx_memories_status ON memories(status)")
    _try_create_index(cur, "idx_kb_chunks_status ON kb_chunks(status)")
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
            notes TEXT DEFAULT '',
            read_at TEXT
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
    # 工具执行结果与失败归因（2026-10-05）：此前 6 个 stage 中无 tool_execution，
    # 也没有任何 error 分类列 → 排查「AI 答得不对」时无法区分
    # 「工具根本没返回数据」（kb_search 空结果 / web_search 失败）与
    # 「数据拿到了但模型表达错」，而这两者的修复方向完全相反。
    # tool_results_json: [{tool, success, duration_ms, error_class, result_count}]
    # failure_class: tool_error / tool_empty_result / no_tool_selected /
    #                llm_error / prompt_truncated / ''（成功）
    _ensure_column(cur, "routing_quality", "tool_results_json", "TEXT DEFAULT '[]'")
    _ensure_column(cur, "routing_quality", "failure_class", "TEXT DEFAULT ''")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_quality_failure "
        "ON routing_quality(failure_class)"
    )

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

    # ── 展示用全量会话摘要表（与 H2-A/动态摘要解耦，专供 Web「对话摘要」页）──
    # 覆盖整段近期对话（非仅 older 段），避免卡片只显示部分聊天记录。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversation_display_summaries (
            chat_id                 TEXT PRIMARY KEY,
            summary_text           TEXT NOT NULL,
            boundary_msg_id        TEXT NOT NULL,
            covered_count          INTEGER NOT NULL,
            generation             INTEGER NOT NULL DEFAULT 0,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL,
            boundary_ts            TEXT NOT NULL DEFAULT ''
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_cds_updated ON conversation_display_summaries(updated_at)"
    )
    # 注意：boundary_ts 索引必须在 _ensure_column 补齐列之后创建。
    # 旧库的 conversation_display_summaries 无 boundary_ts（CREATE TABLE IF NOT EXISTS
    # 是空操作），提前建索引会 "no such column: boundary_ts" 致 init_schema 整体失败，
    # 进而让所有 Web 请求 500。
    _ensure_column(cur, "conversation_display_summaries", "boundary_ts", "TEXT DEFAULT ''")
    # 回填：已有展示摘要的 boundary_ts 取该会话最新消息时间，窗口过滤依赖它；
    # 否则用 updated_at（=摘要生成时间）会让「今天/昨日」筛选器失准（全部命中）。
    try:
        cur.execute(
            "UPDATE conversation_display_summaries "
            "SET boundary_ts = COALESCE("
            "   (SELECT MAX(timestamp) FROM messages m WHERE m.chat_id = conversation_display_summaries.chat_id),"
            "   updated_at) "
            "WHERE boundary_ts IS NULL OR boundary_ts = ''"
        )
    except sqlite3.OperationalError:
        logger.debug("backfill boundary_ts 跳过（messages 表缺失或无需回填）")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_cds_boundary ON conversation_display_summaries(boundary_ts)"
    )

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
    # 工具执行结果与失败归因（2026-10-05）：此前 6 个 stage 中无 tool_execution，
    # 也没有任何 error 分类列 → 排查「AI 答得不对」时无法区分
    # 「工具根本没返回数据」（kb_search 空结果 / web_search 失败）与
    # 「数据拿到了但模型表达错」，而这两者的修复方向完全相反。
    # tool_results_json: [{tool, success, duration_ms, error_class, result_count}]
    # failure_class: tool_error / tool_empty_result / no_tool_selected /
    #                llm_error / prompt_truncated / ''（成功）
    _ensure_column(cur, "routing_quality", "tool_results_json", "TEXT DEFAULT '[]'")
    _ensure_column(cur, "routing_quality", "failure_class", "TEXT DEFAULT ''")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_quality_failure "
        "ON routing_quality(failure_class)"
    )

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

    # ── 元数据 KV 表（全局，跨平台）───────────────────────────────────
    # 用于持久化进程级状态（如 last_run_at：上次成功运行时间，供摘要连续性补跑检测停机时长）。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
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


# 会话分库需要「确保存在」的列。与 init_conv_schema 内的 _ensure_column 调用
# 保持同源（新增列时在此登记一处即可），供只读探针 conv_schema_needs_migration()
# 判断是否真有缺列——web 启动据此跳过无谓的 DDL，避免对 110MB 分库取写锁。
_CONV_REQUIRED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("conversation_display_summaries", "boundary_ts"),
    ("messages", "image_path"),
    ("messages", "is_bot"),
    ("messages", "is_archived"),
    ("messages", "skip_reason"),
    ("messages", "is_withdrawn"),
    ("conversations", "peer_user_id"),
    ("conversations", "peer_open_dingtalk_id"),
    ("conversations", "last_reply_time"),
    ("conversations", "last_replied_msg_id"),
    ("conversations", "last_summary_at"),
)


def _heal_corrupted_indexes(cur: sqlite3.Cursor, db_path: str) -> None:
    """检测并重建**损坏的索引**（P0 2026-10-05 事故自愈）。

    ## 为什么需要（真实事故）

    生产库 ``dingtalk__4c11dc67bc0226ad.db`` 的 ``idx_conversations_updated``
    出现 49 条 ``row N missing from index``（``PRAGMA integrity_check`` 可证）。
    损坏索引会让每次写入都退化为「全表扫描 + 重建索引」，写事务耗时暴涨 →
    **写锁被长期占用** → 其他写者全部 ``database is locked``。

    这类故障的特点是**「代码怎么改都治不好」**：闸门、限流、busy_timeout 全都无效，
    因为病根在**数据文件**而非代码。此前排查耗时极久（先误判为跨进程争用、
    再误判为未关闭游标），最后靠 ``integrity_check`` 才定位到真因。

    损坏成因：``CREATE INDEX`` 过程中进程被强杀（``--dev`` 热重载 / 手动重启）。

    ## 策略

    - ⚠️ **必须用 ``PRAGMA integrity_check``，不能用 ``quick_check``**：
      实测 ``quick_check`` 对「row N missing from index」这类**索引与表不一致**
      返回 ``ok``（漏检），而 ``integrity_check`` 能报出 49 条。用 quick_check 会
      让本自愈形同虚设（实测踩过：quick_check=ok 但 integrity_check=49 条）。
    - 只在**检出索引类损坏**时才重建，且**只重建报错的索引**（不重建全库索引）；
    - 全部走 ``DROP`` + ``CREATE``（幂等、不动数据），索引定义从 sqlite_master
      读回原 SQL，**不硬编码**——硬编码会与 schema 漂移；
    - 任何异常都吞掉并降级为 warning：本函数是修复手段，不能自己成为故障源。

    注：``integrity_check`` 在 110MB 库上约 0.3~1s，故只在
    ``init_conv_schema``（每进程每文件一次）调用，不进每请求路径。
    """
    try:
        rows = cur.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as e:
        logger.debug("[schema] integrity_check 失败（跳过索引自愈）%s: %s", db_path, e)
        return
    if not rows:
        return
    # 正常时返回单行 'ok'；异常时返回逐条问题描述
    if len(rows) == 1 and str(rows[0][0]).lower() == "ok":
        return

    # 收集损坏的索引名：形如 "row 43 missing from index idx_xxx"
    bad: set[str] = set()
    for r in rows:
        text = str(r[0]) if r else ""
        if "index" not in text:
            continue
        for part in text.split("from index")[-1:]:
            name = part.strip().rstrip(".").split()[0] if part.strip() else ""
            if name:
                bad.add(name)
    if not bad:
        logger.warning("[schema] quick_check 报 %d 个问题（非索引类，跳过自愈）: %s",
                       len(rows), str(rows[0][0])[:120])
        return

    logger.warning("[schema] 检测到索引损坏 %d 个: %s（正在重建）", len(bad), sorted(bad))
    for name in sorted(bad):
        try:
            row = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            if row is None or not row[0]:
                # 索引已不存在（可能被并发清掉）→ 跳过
                continue
            create_sql = str(row[0])
            cur.execute(f"DROP INDEX IF EXISTS {name}")
            cur.execute(create_sql)
            cur.connection.commit()
            logger.info("[schema] 索引已重建: %s", name)
        except sqlite3.Error as e:
            logger.warning("[schema] 重建索引 %s 失败（忽略）: %s", name, e)
    logger.info("[schema] 索引自愈完成 %s: %s", db_path, sorted(bad))


def conv_schema_needs_migration(db_path: str) -> bool:
    """**只读**探针：该分库是否缺列（只查 sqlite_master，不取写锁）。

    2026-10-05 锁争用止血：web 启动曾对**所有**分库无条件跑 ``init_conv_schema``
    （含 CREATE INDEX / ALTER / 回填 UPDATE），对 110MB 真实分库取写锁；而
    ``run_linkora.py --dev`` 在文件变更时重启 web，于是「改一行代码」=「对全部库
    做一次 DDL 风暴」，实测 6 次重载对应 6 轮 database is locked 洪峰、轮询停摆。

    本函数用只读 URI 连接（``mode=ro``）查询列是否存在：
      - 库文件不存在 / 打不开 → 返回 True（交给调用方走完整迁移兜底）
      - 任一登记列缺失      → True
      - 全部齐备            → False，调用方可安全跳过 DDL

    注意：``init_conv_schema`` 里的 ``_ensure_column`` 仍是最终自愈兜底，本函数
    只是「先问一句要不要 DDL」的前置判断，不替代它。
    """
    if not os.path.exists(db_path):
        return True
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as e:
        logger.debug("[schema] 只读探针无法打开 %s: %s", db_path, e)
        return True
    try:
        # 按表缓存现有列，避免对每列重复查 sqlite_master
        table_cols: dict[str, set[str]] = {}
        for table, column in _CONV_REQUIRED_COLUMNS:
            cols = table_cols.get(table)
            if cols is None:
                cur = conn.execute(f"PRAGMA table_info({table})")
                cols = {row[1] for row in cur.fetchall()}
                cur.close()
                table_cols[table] = cols
                if not cols:
                    # 表本身不存在 → 属于「缺结构」，需要迁移
                    return True
            if column not in cols:
                return True
        return False
    except sqlite3.Error as e:
        logger.debug("[schema] 只读探针查询 %s 失败，按需要迁移处理: %s", db_path, e)
        return True
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


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

        CREATE TABLE IF NOT EXISTS conversation_display_summaries (
            chat_id                 TEXT PRIMARY KEY,
            summary_text           TEXT NOT NULL,
            boundary_msg_id        TEXT NOT NULL,
            covered_count          INTEGER NOT NULL,
            generation             INTEGER NOT NULL DEFAULT 0,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL,
            boundary_ts            TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_cds_updated ON conversation_display_summaries(updated_at);
        -- idx_cds_boundary 不能放这里：旧库无 boundary_ts 列（CREATE TABLE IF NOT EXISTS
        -- 是空操作），会 "no such column" 让整个 executescript 失败。留待 _ensure_column
        -- 补列后再建索引（见下方 Python 迁移）。

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
    # 会话展示摘要表：补充 boundary_ts（会话最新消息时间），供「今日/昨日」窗口过滤。
    # 必须在 executescript 之外以 Python 执行——_ensure_column 是 Python API，不能进 SQL 脚本。
    _ensure_column(cur, "conversation_display_summaries", "boundary_ts", "TEXT DEFAULT ''")
    try:
        cur.execute(
            "UPDATE conversation_display_summaries "
            "SET boundary_ts = COALESCE("
            "   (SELECT MAX(timestamp) FROM messages m WHERE m.chat_id = conversation_display_summaries.chat_id),"
            "   updated_at) "
            "WHERE boundary_ts IS NULL OR boundary_ts = ''"
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.debug("backfill boundary_ts 跳过（messages 表缺失或无需回填）")
    # 列已确保存在，此处建索引才安全（旧库在 executescript 阶段没有该列）。
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_cds_boundary "
        "ON conversation_display_summaries(boundary_ts)"
    )
    conn.commit()
    # ── 列迁移：兼容「建库早于本列新增」的存量分库 ─────────────────────
    # 上面的 CREATE TABLE IF NOT EXISTS 只对新建分库生效；已存在的分库表
    # 不会自动 ALTER 补列，会导致 Web 查询报 no such column（2026-08-10 复现：
    # is_withdrawn 加在 CREATE 里但存量分库缺列 → /api/dashboard/stream-data 500）。
    # 这里与 init_schema 对齐，对存量分库做 _ensure_column 兜底。
    _ensure_column(cur, "messages", "image_path", "TEXT DEFAULT ''")
    _ensure_column(cur, "messages", "is_bot", "INTEGER DEFAULT 0")
    _ensure_column(cur, "messages", "is_archived", "INTEGER DEFAULT 0")
    _ensure_column(cur, "messages", "skip_reason", "TEXT")
    _ensure_column(cur, "messages", "is_withdrawn", "INTEGER DEFAULT 0")
    _ensure_column(cur, "conversations", "peer_user_id", "TEXT")
    _ensure_column(cur, "conversations", "peer_open_dingtalk_id", "TEXT")
    _ensure_column(cur, "conversations", "last_reply_time", "TEXT")
    _ensure_column(cur, "conversations", "last_replied_msg_id", "TEXT")
    _ensure_column(cur, "conversations", "last_summary_at", "TEXT")
    conn.commit()
    # 【P0 2026-10-05 索引自愈】分库路径同样需要：损坏索引 → 写锁被长期占用
    # → 其他写者 database is locked（详见 _heal_corrupted_indexes docstring）。
    # 必须在 commit **之后**调用：重建索引是独立写事务，不与建表/补列混在一起。
    _heal_corrupted_indexes(cur, db_path)
    logger.debug("会话库状态正常：%s", db_path)
