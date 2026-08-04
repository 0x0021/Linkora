"""决策追踪器：记录每条消息的意图判定与工具路由决策。

为什么需要它：
- 用户（非技术）常困惑"某条消息为什么被跳过 / 我的机器人到底调了哪些工具"。
- 把「意图识别结果」「处理动作（跳过/规则回复/LLM 处理）」「实际路由了哪些工具」
  收敛为一条结构化记录，管理端即可可视化展示，无需再去翻日志、截图排查。

设计要点：
- 进程级单例 `tracker`：rule_engine（意图）、agent（路由）、main（编排）共享同一份最近决策。
- 内存有界队列（默认 300 条）供首页卡片近实时展示。
- 同时持久化到 SQLite，进程重启不丢失，支持按人/时间线/意图筛选。
- 字段全部可选，兼容"仅意图判定（被跳过）"与"完整 LLM 处理（含路由）"两类记录。
"""

import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, List, Dict

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


@dataclass
class DecisionRecord:
    """单条消息处理决策。"""

    ts: str                      # ISO 时间戳（UTC，秒级）
    sender: str                  # 发送者名称
    chat: str                    # 会话名称
    content: str                 # 消息内容（已截断摘要）
    intent: str                  # 处置意图：business | social.gratitude | social.acknowledge | ...
    action: str                  # skip（跳过）| reply-rule（关键词规则直接回复）| llm（交给 LLM 代理）
    sender_id: str = ""          # 发送者 ID
    platform_id: str = ""        # 平台 ID（如 dingtalk/feishu），用于多平台隔离
    routing_mode: Optional[str] = None      # 工具路由模式：smart | all | keyword（被跳过时为 None）
    routed_tools: Optional[List[str]] = None  # 本轮实际暴露给 LLM 的工具名（被跳过时为 None）
    skill_name: Optional[str] = None          # 本轮激活的技能名（无技能激活时为 None）
    skill_source: Optional[str] = None        # 技能激活来源：explicit | intent | keyword
    reply_preview: Optional[str] = None     # 回复内容预览（前若干字）
    # 成本/质量看板（Roadmap ③）质量标记：低置信转人工 / RAG 命中 / 引文页脚命中
    handoff: int = 0                        # 是否触发低置信转人工（草稿推主人）
    rag_grounded: int = 0                   # RAG 是否命中（best_score 非空）
    cited: int = 0                          # 是否实际追加了引文溯源页脚


class DecisionTracker:
    """有界、进程内的决策记录器（支持多平台隔离）。"""

    def __init__(self, maxlen: int = 300):
        self._records: deque = deque(maxlen=maxlen)
        # 多平台：每个平台一个 SQLiteStore；key 为 platform_id（如 dingtalk / feishu）
        self._stores: Dict[str, "SQLiteStore"] = {}

    def set_sqlite_store(self, store: "SQLiteStore") -> None:
        """向后兼容：设置默认（钉钉）平台的持久化后端。"""
        self._stores["dingtalk"] = store

    def add_platform_store(self, platform_id: str, store: "SQLiteStore") -> None:
        """为指定平台注册 store（多平台隔离）。"""
        self._stores[platform_id] = store

    def _store_for(self, platform_id: str = "") -> Optional["SQLiteStore"]:
        """按平台 ID 解析对应 store；未知平台回退 dingtalk。"""
        if platform_id and platform_id in self._stores:
            return self._stores[platform_id]
        return self._stores.get("dingtalk")

    def record(self, **kw) -> None:
        """写入一条决策；ts 缺省自动取当前 UTC 时间。

        同时写入内存 deque（首页卡片近实时展示）和对应平台的 SQLite（持久化历史）。

        自动从 contextvars 注入 request_id（贯穿全链路追踪）。
        """
        kw.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # 自动注入 request_id（如果调用方没传）
        if not kw.get("request_id"):
            try:
                from src.utils.request_id import get_request_id
                rid = get_request_id()
                if rid:
                    kw["request_id"] = rid
            except Exception:
                logger.debug("提取 request_id 失败，决策记录不含 request_id")
        # conversation_id / request_id / platform_id 仅用于 SQLite 持久化
        record_kw = {k: v for k, v in kw.items() if k in DecisionRecord.__dataclass_fields__}
        self._records.append(DecisionRecord(**record_kw))

        platform_id = kw.get("platform_id", "")
        store = self._store_for(platform_id)
        if store:
            try:
                tools = kw.get("routed_tools", None)
                store._decisions_repo.record_decision(
                    sender_id=kw.get("sender_id", ""),
                    sender_name=kw.get("sender", ""),
                    conversation_id=kw.get("conversation_id", ""),
                    conversation_name=kw.get("chat", ""),
                    content_preview=kw.get("content", ""),
                    intent=kw.get("intent", ""),
                    action=kw.get("action", ""),
                    routing_mode=kw.get("routing_mode", ""),
                    routed_tools=tools if isinstance(tools, list) else (tools or []),
                    skill_name=kw.get("skill_name", ""),
                    skill_source=kw.get("skill_source", ""),
                    reply_preview=kw.get("reply_preview", ""),
                    request_id=kw.get("request_id", ""),
                    platform_id=platform_id or "",
                    # 成本/质量看板（Roadmap ③）质量标记：仅在调用方显式传入时落库
                    handoff=int(kw.get("handoff", 0) or 0),
                    rag_grounded=int(kw.get("rag_grounded", 0) or 0),
                    cited=int(kw.get("cited", 0) or 0),
                )
            except Exception:
                logger.warning("决策记录持久化失败", exc_info=True)

    def recent(self, n: int = 50, platform_id: str = "") -> list[dict]:
        """返回最近 n 条决策（按时间正序，便于前端顺序渲染）。

        优先返回内存中的实时数据；内存为空（刚重启）时回退 SQLite 持久化数据，
        确保首页「决策追踪」卡片重启后不空白。

        Args:
            n: 返回条数
            platform_id: 平台 ID（如 dingtalk/feishu），指定时从对应平台 store 读取；
                为空时返回内存中所有数据（向后兼容）。
        """
        recs = list(self._records)

        if platform_id:
            platform_recs = [r for r in recs if r.platform_id == platform_id]
        else:
            platform_recs = recs

        if platform_recs:
            return [asdict(r) for r in platform_recs[-n:]]

        # 指定平台时从对应 store 读取；内存为空时也从默认 store 恢复
        store = self._store_for(platform_id)
        if store:
            try:
                result = store._decisions_repo.get_decisions(page_size=n)
                db_recs = result.get("items", []) if isinstance(result, dict) else []
                if db_recs:
                    # 构建内存中已存在记录的去重键（避免重复回填）
                    existing_keys = set()
                    for r in self._records:
                        key = (r.ts, r.sender, r.content)
                        existing_keys.add(key)
                    # 回填内存队列，跳过已存在的记录
                    for row in reversed(db_recs):
                        key = (
                            row.get("created_at", ""),
                            row.get("sender_name", ""),
                            row.get("content_preview", ""),
                        )
                        if key in existing_keys:
                            continue
                        self._records.append(DecisionRecord(
                            ts=row.get("created_at", ""),
                            sender=row.get("sender_name", ""),
                            chat=row.get("conversation_name", ""),
                            content=row.get("content_preview", ""),
                            intent=row.get("intent", ""),
                            action=row.get("action", ""),
                            sender_id=row.get("sender_id", ""),
                            platform_id=platform_id,
                            routing_mode=row.get("routing_mode"),
                            routed_tools=row.get("routed_tools"),
                            skill_name=row.get("skill_name"),
                            skill_source=row.get("skill_source"),
                            reply_preview=row.get("reply_preview"),
                        ))
                    platform_recs = [r for r in self._records if r.platform_id == platform_id]
                    return [asdict(r) for r in platform_recs[-n:]]
            except Exception:
                logger.warning("从 SQLite 回填决策记录失败", exc_info=True)
        return []

    def clear(self) -> None:
        self._records.clear()


# 进程级单例：rule_engine / agent / main 共享同一份最近决策
tracker = DecisionTracker()
