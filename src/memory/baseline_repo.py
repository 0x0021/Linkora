"""Repository for backtest baseline operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
import re as _re
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from src.memory.few_shot_diversity import (
    greedy_select,
    len_bucket,
    normalize,
    topic_key,
    trigram_similarity,
)
from src.memory.platform_context import get_current_platform
from src.memory.sqlite_store import _redact_pii, _is_inappropriate

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class BaselineRepo:
    """Repository extracted from SQLiteStore for backtest baseline operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self, platform: str = "") -> sqlite3.Connection:
        """按当前平台/账号隔离的会话连接（messages 属会话数据；kv 走主库）。"""
        return self.store.conv_conn(platform or get_current_platform())

    def record_backtest(self, mean_score: float, count: int, sample_count: int,
                        limit_keep: int = 100) -> None:
        """记录一次口吻还原度回测基线（平台级隔离，默认保留最近 100 条）。"""
        try:
            history = self.get_backtest_history()
            history.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "mean_score": round(float(mean_score), 1),
                "count": int(count),
                "sample_count": int(sample_count),
            })
            if len(history) > limit_keep:
                history = history[-limit_keep:]
            cur = self.store.conn.cursor()
            cur.execute(
                """INSERT INTO kv (key, value, updated_at) VALUES ('backtest_history', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (json.dumps(history, ensure_ascii=False), datetime.now().isoformat()),
            )
            self.store.conn.commit()
        except Exception as e:
            logger.warning("[store] 记录回测基线失败: %s", e)


    def get_backtest_history(self, limit: int = 30) -> list[dict]:
        """读取口吻还原度回测趋势（平台级隔离，按时间升序，最多 limit 条）。"""
        try:
            cur = self.store.conn.cursor()
            cur.execute("SELECT value FROM kv WHERE key = 'backtest_history'")
            row = cur.fetchone()
            if row and row["value"]:
                data = json.loads(row["value"])
                if isinstance(data, list):
                    data = [d for d in data if isinstance(d, dict)]
                    return data[-limit:] if limit > 0 else data
        except Exception:
            logger.warning("[resilience] silent exception in get_backtest_history", exc_info=True)
        return []


    def recommend_few_shot_pairs(self, owner_name: str, limit: int = 6,
                                 exclude: list[dict] | None = None,
                                 platform: str = "") -> list[dict]:
        """从主人历史对话中推荐高质量 few-shot 样例（user→assistant 配对），并做多样性增强。

        质量门：主人回复需 8~120 字、非媒体/系统/单字、非纯 emoji/标点；
        且能在同一 chat_id 内找到其前的外部来信（role='user'）作为 user 侧。

        多样性增强（#22）：
        - 排除已采纳样例（exclude：来自 cfg.llm.few_shot_examples 的 user/assistant 对）；
        - 文本/近似去重：与已选 reply 字符 trigram 相似度 ≥0.8 视为近似重复，跳过；
        - 长度与主题多样性：按回复长度分桶（短/中/长）+ 用户开场主题，贪心尽量均衡挑选，
          避免推荐一批高度雷同的样例；数据本身同质时放宽约束凑足数量。
        """
        import re as _re

        if not owner_name:
            return []
        cur = self._cc(platform).cursor()
        # 1) 随机抽一批候选主人回复（多抽一些，留出多样性挑选余量）
        pool_target = max(limit * 8, 24)
        cur.execute(
            """SELECT id, chat_id, content FROM messages
               WHERE sender_name = ? AND content IS NOT NULL
                 AND content NOT LIKE '[自动回复]%'
                 AND is_bot = 0
                 AND role = 'assistant'
                 AND msg_type NOT IN ('system','app')
                 AND length(trim(content)) >= 8
                 AND length(trim(content)) <= 120
               ORDER BY RANDOM() LIMIT ?""",
            (owner_name, pool_target),
        )
        candidates = cur.fetchall()
        media_re = _re.compile(r"^\s*\[(?:图片|文件|视频|动画表情|链接|语音|位置|红包|名片|小程序|互动卡片|AI卡片)")
        punct_only_re = _re.compile(r"^[\s\W_]+$")
        json_re = _re.compile(r"^\s*[\{\[]")  # 结构化 JSON 卡片（如 {"textContent":...}）非自然语言，剔除

        # ---- 多样性辅助（已抽到 src.memory.few_shot_diversity）----
        # 已采纳样例排除集（归一化 user|assistant）
        exclude_set = set()
        if exclude:
            for ex in exclude:
                u = normalize(ex.get("user") or "")
                a = normalize(ex.get("assistant") or "")
                if u or a:
                    exclude_set.add((u, a))

        # 2) 基础门 + 隐私护栏 + 排除 + 近似去重 → 候选池
        pool: list[dict] = []
        accepted_replies: list[str] = []
        seen: set = set()
        for row in candidates:
            rid, chat_id, reply = row["id"], row["chat_id"], (row["content"] or "").strip()
            if not reply or media_re.match(reply) or punct_only_re.match(reply) or json_re.match(reply):
                continue
            # 找同一会话中该回复之前最近的外部来信作为 user 侧
            prev = cur.execute(
                """SELECT content FROM messages
                   WHERE chat_id = ? AND id < ?
                     AND role = 'user'
                     AND content IS NOT NULL
                     AND content NOT LIKE '[自动回复]%'
                     AND content NOT LIKE '[{]%'
                     AND content NOT LIKE '{%'
                     AND msg_type NOT IN ('system','app')
                     AND length(trim(content)) >= 2
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, rid),
            ).fetchone()
            if not prev:
                continue
            user_msg = (prev["content"] or "").strip()
            if not user_msg or media_re.match(user_msg) or punct_only_re.match(user_msg) or json_re.match(user_msg):
                continue
            # 隐私护栏：不当内容整对丢弃；命中 PII 则脱敏后入样
            if _is_inappropriate(user_msg) or _is_inappropriate(reply):
                continue
            user_msg = _redact_pii(user_msg)
            reply = _redact_pii(reply)
            # 排除已采纳样例
            if (normalize(user_msg), normalize(reply)) in exclude_set:
                continue
            # 精确去重
            key = (user_msg, reply)
            if key in seen:
                continue
            seen.add(key)
            # 近似去重：与已选 reply 过于相似则跳过
            if any(trigram_similarity(reply, ar) >= 0.8 for ar in accepted_replies):
                continue
            pool.append({
                "user": user_msg,
                "assistant": reply,
                "_bucket": len_bucket(reply),
                "_topic": topic_key(user_msg),
            })
            accepted_replies.append(reply)
            if len(pool) >= pool_target:
                break

        if not pool:
            return []

        # 3) 多样性贪心挑选：长度桶 + 主题均衡；不足则放宽约束补满
        cap_bucket = max(2, (limit + 1) // 2)
        cap_topic = 2
        return greedy_select(pool, limit, cap_bucket=cap_bucket, cap_topic=cap_topic)



def _cosine_local(a: list[float] | None, b: list[float] | None) -> float:
    """零依赖余弦相似度（避免 baseline_repo 反向 import src.llm 造成循环）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class _SceneFewShotSelector:
    """按当前消息场景相似检索主人历史 (user→assistant) 配对。

    复用原 recommend_few_shot_pairs 的质量门（8~120 字、非媒体/系统、非零近似），
    但候选经场景相似度排序后取 top-N，而非随机 + 多样性贪心。

    混合策略（method="hybrid"，默认）：
      1. trigram 粗筛：候选按与 query 的 trigram 相似度降序，取前 K（K=2*n 或 12）；
      2. embedding 精排：若 query_embedding 可用，对粗筛结果按余弦相似度重排取 top-N；
      method="trigram" 仅走步骤 1；method="embedding" 跳过 trigram 直接按向量排序。
    """

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self, platform: str = "") -> sqlite3.Connection:
        """按当前平台/账号隔离的会话连接（messages 属会话数据；kv 走主库）。

        与 ``BaselineRepo._cc`` 同义。此前本类漏了该方法，``retrieve`` 首行
        ``self._cc(platform)`` 必抛 AttributeError，又被 system_prompt.py 的
        ``except Exception`` 吞成 warning —— 结果 dynamic_few_shot 开了也永远
        静默降级为静态样例。
        """
        return self.store.conv_conn(platform or get_current_platform())

    def retrieve(self, owner_name: str, query: str, limit: int = 4,
                 query_embedding: list[float] | None = None,
                 method: str = "hybrid",
                 exclude: list[dict] | None = None,
                 embed_fn=None, platform: str = "") -> list[dict]:
        if not owner_name or not query or not query.strip():
            return []
        query = query.strip()
        topk = max(limit * 3, 12)
        cur = self._cc(platform).cursor()
        cur.execute(
            """SELECT id, chat_id, content FROM messages
               WHERE sender_name = ? AND content IS NOT NULL
                 AND content NOT LIKE '[自动回复]%'
                 AND is_bot = 0
                 AND role = 'assistant'
                 AND msg_type NOT IN ('system','app')
                 AND length(trim(content)) >= 8
                 AND length(trim(content)) <= 120
               ORDER BY id DESC LIMIT 400""",
            (owner_name,),
        )
        rows = cur.fetchall()
        media_re = _re.compile(r"^\s*\[(?:图片|文件|视频|动画表情|链接|语音|位置|红包|名片|小程序|互动卡片|AI卡片)")
        punct_only_re = _re.compile(r"^[\s\W_]+$")
        json_re = _re.compile(r"^\s*[\{\[]")
        exclude_set = set()
        if exclude:
            for ex in exclude:
                u = normalize(ex.get("user") or "")
                a = normalize(ex.get("assistant") or "")
                if u or a:
                    exclude_set.add((u, a))
        pool: list[dict] = []
        for row in rows:
            rid, chat_id, reply = row["id"], row["chat_id"], (row["content"] or "").strip()
            if not reply or media_re.match(reply) or punct_only_re.match(reply) or json_re.match(reply):
                continue
            prev = cur.execute(
                """SELECT content FROM messages
                   WHERE chat_id = ? AND id < ?
                     AND role = 'user'
                     AND content IS NOT NULL
                     AND content NOT LIKE '[自动回复]%'
                     AND content NOT LIKE '[{]%'
                     AND content NOT LIKE '{%'
                     AND msg_type NOT IN ('system','app')
                     AND length(trim(content)) >= 2
                   ORDER BY id DESC LIMIT 1""",
                (chat_id, rid),
            ).fetchone()
            if not prev:
                continue
            user_msg = (prev["content"] or "").strip()
            if not user_msg or media_re.match(user_msg) or punct_only_re.match(user_msg) or json_re.match(user_msg):
                continue
            if _is_inappropriate(user_msg) or _is_inappropriate(reply):
                continue
            user_msg = _redact_pii(user_msg)
            reply = _redact_pii(reply)
            if (normalize(user_msg), normalize(reply)) in exclude_set:
                continue
            # _emb 延后到 trigram 粗筛后对 top-K 候选才计算（经 embed_fn，避免全量 400 候选向量化）
            item = {
                "user": user_msg,
                "assistant": reply,
                "_trigram": trigram_similarity(query, user_msg),
                "_emb": None,
            }
            pool.append(item)
        if not pool:
            return []
        if method == "embedding" and query_embedding is not None:
            pool.sort(key=lambda x: (x["_emb"] if x["_emb"] is not None else 0.0), reverse=True)
        elif method == "trigram":
            pool.sort(key=lambda x: x["_trigram"], reverse=True)
        else:  # hybrid
            pool.sort(key=lambda x: x["_trigram"], reverse=True)
            topk_pool = pool[:topk]
            # 仅对粗筛后 top-K 候选做 embedding 精排（embed_fn 一般为 agent._embed_message）
            if query_embedding is not None and embed_fn is not None:
                for it in topk_pool:
                    try:
                        vec = embed_fn(it["user"])
                        it["_emb"] = _cosine_local(query_embedding, vec)
                    except Exception as _exc:
                        logger.warning(f"retrieve: swallowed exception: {_exc}")
                        it["_emb"] = None
                topk_pool.sort(key=lambda x: (x["_emb"] if x["_emb"] is not None else 0.0), reverse=True)
            pool = topk_pool
        selected = []
        seen = set()
        for it in pool:
            key = (it["user"], it["assistant"])
            if key in seen:
                continue
            seen.add(key)
            selected.append({"user": it["user"], "assistant": it["assistant"]})
            if len(selected) >= limit:
                break
        return selected

