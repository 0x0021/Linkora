"""Repository for style profile / memory operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
import re as _re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from src.memory.platform_context import get_current_platform
from src.memory.sqlite_store import _redact_pii, _is_inappropriate

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class MemoryOpsRepo:
    """Repository extracted from SQLiteStore for style profile operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self, platform: str = "") -> sqlite3.Connection:
        """按当前平台/账号隔离的会话连接（messages 属会话数据；style_profiles 走主库）。"""
        return self.store.conv_conn(platform or get_current_platform())

    def compute_style_profile(self, owner_name: str, sample_limit: int = 1000,
                              platform: str = "") -> dict:
        """从主人历史发出的消息随机抽样，抽取沟通风格画像。

        采样策略：用 `ORDER BY RANDOM() LIMIT ?` 在全量清洗后候选集里**随机**抽样，
        而非 `ORDER BY id DESC`（只取最新）——避免画像被近期口吻偏差主导，
        更能代表主人的长期稳定风格。

        数据清洗（关键）：画像应反映「主人真实口吻」，因此抽样前会过滤掉
        ① AI 自动回复（is_bot=1）/ ② 非主人发出的消息（role!='assistant'，如他人或 OA/审批系统）
        ③ [自动回复] 前缀 / ④ 系统类卡片（system/app）/ ⑤ 空或单字无口吻价值的消息。

        返回含 `prompt`（可直接注入 system prompt 的中文口吻描述）的 dict；
        无样本时返回空 dict。并附带 `raw_count`（清洗前候选）与 `cleaned_count`（清洗后实际抽样）。
        """
        if not owner_name:
            return {}
        cur = self._cc(platform).cursor()
        # 清洗前候选（该主人名下、有正文、非[自动回复]）
        raw_count = cur.execute(
            """SELECT count(*) FROM messages
               WHERE sender_name = ? AND content IS NOT NULL
                 AND content NOT LIKE '[自动回复]%'""",
            (owner_name,),
        ).fetchone()[0]
        cur.execute(
            """SELECT content FROM messages
               WHERE sender_name = ? AND content IS NOT NULL
                 AND content NOT LIKE '[自动回复]%'
                 AND is_bot = 0                          -- 排除 AI 自动回复
                 AND role = 'assistant'                 -- 仅保留主人发出的消息（他人/系统为 user）
                 AND msg_type NOT IN ('system','app')    -- 排除系统卡片/应用卡片
                 AND length(trim(content)) >= 2         -- 排除空串/单字无口吻价值
               ORDER BY RANDOM() LIMIT ?  -- 随机抽样，避免只取最新造成口吻偏差
            """,
            (owner_name, sample_limit),
        )
        rows = [r["content"] for r in cur.fetchall() if r["content"]]
        if not rows:
            return {}
        emoji_re = _re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
        polite_re = _re.compile(r"您|请|谢谢|感谢|麻烦|辛苦")
        casual_re = _re.compile(r"哈|哦|嗯|搞|整|咋|啥|呗")
        # ---- 二次清洗：去除媒体占位符、Markdown 残留、纯标点等噪声 ----
        media_re = _re.compile(r"^\s*\[(?:图片|文件|视频|动画表情|链接|语音|位置|红包|名片|小程序|互动卡片|AI卡片)")
        punct_only_re = _re.compile(r"^[\s\W_]+$")
        ascii_short_re = _re.compile(r"^[A-Za-z0-9\s.,!?;:'\"()\-_]{1,4}$")

        def _clean_msg(text: str) -> str:
            t = text.strip()
            t = _re.sub(r"^\*{1,3}\s*", "", t)
            t = _re.sub(r"^#{1,6}\s*", "", t)
            t = _re.sub(r"^>\s*", "", t)
            t = _re.sub(r"^```\w*\s*", "", t)
            t = _redact_pii(t.strip())   # 隐私护栏：脱敏手机号/身份证/邮箱/地址
            return t

        def _is_noise(text: str) -> bool:
            t = text.strip()
            if not t:
                return True
            if _is_inappropriate(t):       # 不当内容直接丢弃，不进入画像
                return True
            if media_re.match(t):
                return True
            if punct_only_re.match(t):
                return True
            if ascii_short_re.match(t):
                return True
            return False

        clean_rows: list[str] = []
        for raw in rows:
            cleaned = _clean_msg(raw)
            if not _is_noise(cleaned):
                clean_rows.append(cleaned)
        if not clean_rows:
            clean_rows = rows  # fallback

        # ---- 统计指标 ----
        avg_len = sum(len(x) for x in clean_rows) / len(clean_rows)
        emoji_rate = sum(1 for x in clean_rows if emoji_re.search(x)) / len(clean_rows)
        polite_rate = sum(1 for x in clean_rows if polite_re.search(x)) / len(clean_rows)
        casual_rate = sum(1 for x in clean_rows if casual_re.search(x)) / len(clean_rows)
        short_rate = sum(1 for x in clean_rows if len(x.strip()) <= 10) / len(clean_rows)
        question_rate = sum(1 for x in clean_rows if "？" in x or "?" in x) / len(clean_rows)

        # ---- 开场白提取：取清洗后的中文/有意义开头（2~4 字） ----
        opener_re = _re.compile(r"^([\u4e00-\u9fa5A-Za-z]{2,4})")
        openers: list[str] = []
        for x in clean_rows:
            t = x.strip()
            if len(t) < 3:
                continue
            m = opener_re.match(t)
            if m:
                openers.append(m.group(1))
        top_openers = [w for w, _ in Counter(openers).most_common(5) if w]

        # ---- 代表性短语：从清洗样本中精选「最能代表个人口吻」的短句（面向可解释面板）----
        # 过滤：长度 5~30 字、非链接、去除标点/空白后不少于 3 字、去重，最多 8 条。
        # 与 sample_messages（原始 60 条）不同，这里是经过筛选、可直接展示给用户的典型例句。
        _seen: set = set()
        representative_samples: list[str] = []
        for x in clean_rows:
            t = x.strip()
            if not (5 <= len(t) <= 30):
                continue
            if "http" in t.lower():
                continue
            # 去除标点/空白后若不足 3 字，视为无信息（纯 emoji / 纯标点）
            _stripped = _re.sub(r"[\s\W_]+", "", t)
            if len(_stripped) < 3:
                continue
            _key = t.lower()
            if _key in _seen:
                continue
            _seen.add(_key)
            representative_samples.append(t)
            if len(representative_samples) >= 8:
                break

        # ---- 生成自然语言画像描述 ----
        traits: list[str] = []
        if avg_len <= 15:
            traits.append("回复极简干练")
        elif avg_len <= 40:
            traits.append("回复简洁明了")
        elif avg_len <= 80:
            traits.append("回复详略得当")
        else:
            traits.append("回复偏详尽")

        if polite_rate >= 0.3:
            traits.append("语气礼貌正式")
        elif casual_rate >= 0.3:
            traits.append("语气随和口语化")
        else:
            traits.append("语气中性直接")

        if emoji_rate >= 0.3:
            traits.append("习惯用 emoji 点缀表达")
        elif emoji_rate >= 0.1:
            traits.append("偶尔使用 emoji")

        if short_rate >= 0.5:
            traits.append("多以短语快速回复")

        if question_rate >= 0.2:
            traits.append("常以提问方式推进话题")

        prompt = "、".join(traits)
        if top_openers:
            prompt += "。典型开场如「" + "」「".join(top_openers[:3]) + "」"
        prompt += "。"

        # ---- 派生指标：置信度 & 画像完整度（供前端展示与自动重算判断）----
        # 置信度：仅由样本量决定（清洗后实际可用条数）。
        #   < 30 条 → low（样本太少，画像代表性弱）
        #   30~149 条 → medium
        #   >= 150 条 → high
        if len(clean_rows) >= 150:
            confidence = "high"
        elif len(clean_rows) >= 30:
            confidence = "medium"
        else:
            confidence = "low"

        # 画像完整度（0~100）：
        #   体量分（0~60）：约 150 条清洗样本达满分，之后不再增益；
        #   维度覆盖分（0~40）：5 个风格率中「有信号」（>0.03，非死值）的维度数比例。
        _dims = [emoji_rate, polite_rate, casual_rate, short_rate, question_rate]
        _informative = sum(1 for d in _dims if d > 0.03)
        completeness = int(round(
            min(len(clean_rows) / 150.0, 1.0) * 60 + (_informative / 5.0) * 40
        ))

        return {
            "sample_count": len(rows),
            "raw_count": raw_count,
            "cleaned_count": len(clean_rows),
            "avg_len": round(avg_len, 1),
            "emoji_rate": round(emoji_rate, 2),
            "polite_rate": round(polite_rate, 2),
            "casual_rate": round(casual_rate, 2),
            "short_rate": round(short_rate, 2),
            "question_rate": round(question_rate, 2),
            "top_openers": top_openers,
            "representative_samples": representative_samples,
            "prompt": prompt,
            "sample_messages": clean_rows[:60],
            "confidence": confidence,
            "completeness": completeness,
            "updated_at": datetime.now().isoformat(),
        }


    def get_style_profile(self) -> dict:
        cur = self.store.conn.cursor()
        try:
            cur.execute("SELECT profile_json FROM style_profiles WHERE id = 1")
            row = cur.fetchone()
            if row and row["profile_json"]:
                _p = json.loads(row["profile_json"])
                _p.pop("_save_trigger", None)  # 去除内部溯源字段，保持对外干净
                return _p
        except Exception:
            logger.warning("[resilience] silent exception in get_style_profile", exc_info=True)
        return {}


    def save_style_profile(self, profile: dict, trigger: str = "manual",
                           max_versions: int = 0) -> None:
        """写入/覆盖风格画像（单例行 id=1），覆盖前先把当前版本归档进 versions 表。

        trigger 标记本次写入来源，会随画像持久化（_save_trigger），供历史列表溯源展示：
          'manual'    — 用户手动重新分析 / 覆盖
          'auto'      — #16 调度过期自动重算
          'rollback'  — 从历史版本回滚
          'baseline'  — 旧库迁移回填（仅初始化时使用，详见 init_db）

        max_versions: 画像历史版本最大保留数。> 0 时，写入后滚动删除超量的最旧版本，
          保留最新的 N 个（按 version_no 降序）。默认 0 表示不限制。
        """
        cur = self.store.conn.cursor()
        # ---- 归档当前版本（若有）：记录其来源 trigger（取自旧画像的 _save_trigger）----
        cur.execute("SELECT profile_json, updated_at FROM style_profiles WHERE id = 1")
        old_row = cur.fetchone()
        if old_row and old_row["profile_json"]:
            try:
                _old = json.loads(old_row["profile_json"])
            except Exception:
                logger.warning("[resilience] silent exception in save_style_profile", exc_info=True)
                _old = {}
            _old_trigger = _old.get("_save_trigger", "manual")
            _max = cur.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS m FROM style_profile_versions"
            ).fetchone()
            next_no = (_max["m"] if _max else 0) + 1
            cur.execute(
                """INSERT INTO style_profile_versions
                       (version_no, profile_json, trigger, confidence, cleaned_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    next_no,
                    old_row["profile_json"],
                    _old_trigger,
                    _old.get("confidence", ""),
                    _old.get("cleaned_count", 0),
                    old_row["updated_at"],
                ),
            )
        # ---- 覆盖为新版本：把 trigger 嵌入画像，便于下次归档时溯源 ----
        _new = dict(profile)
        _new["_save_trigger"] = trigger
        cur.execute(
            """INSERT INTO style_profiles (id, profile_json, updated_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
            (json.dumps(_new, ensure_ascii=False), datetime.now().isoformat()),
        )
        self.store.conn.commit()

        # ---- 滚动清理超量旧版本：保留最新 N 个 ----
        if max_versions > 0:
            _count = cur.execute(
                "SELECT COUNT(*) AS c FROM style_profile_versions"
            ).fetchone()
            total = _count["c"] if _count else 0
            if total > max_versions:
                cutoff_no = cur.execute(
                    """SELECT version_no FROM style_profile_versions
                        ORDER BY version_no DESC LIMIT 1 OFFSET ?""",
                    (max_versions - 1,),
                ).fetchone()
                if cutoff_no:
                    cur.execute(
                        "DELETE FROM style_profile_versions WHERE version_no < ?",
                        (cutoff_no["version_no"],),
                    )
                    deleted = cur.rowcount
                    if deleted > 0:
                        self.store.conn.commit()
                        logger.info(
                            "[cleanup] 画像历史版本：%d -> %d（滚动删除了 %d 个旧版本）",
                            total, total - deleted, deleted,
                        )


    def list_style_profile_versions(self, limit: int = 20) -> list[dict]:
        """列出历史版本（version_no 倒序，最新在前），供前端对比/回滚。"""
        try:
            # 游标获取纳入 try：连接失效时 conn.cursor() 本身会抛，
            # 放在 try 外会绕过下面的兜底返回、直接炸穿调用方。
            cur = self.store.conn.cursor()
            rows = cur.execute(
                """SELECT id, version_no, trigger, confidence, cleaned_count, created_at
                     FROM style_profile_versions
                    ORDER BY version_no DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "version_no": r["version_no"],
                    "trigger": r["trigger"],
                    "confidence": r["confidence"],
                    "cleaned_count": r["cleaned_count"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        except Exception:
            logger.warning("[resilience] silent exception in list_style_profile_versions", exc_info=True)
            return []


    def get_style_profile_version(self, vid: int) -> dict | None:
        """读取某一历史版本的完整画像 JSON（含 prompt / 各维度指标）。"""
        cur = self.store.conn.cursor()
        try:
            row = cur.execute(
                "SELECT profile_json FROM style_profile_versions WHERE id = ?", (vid,)
            ).fetchone()
            if row and row["profile_json"]:
                return json.loads(row["profile_json"])
        except Exception:
            logger.warning("[resilience] silent exception in get_style_profile_version", exc_info=True)
        return None


    def rollback_style_profile(self, vid: int) -> bool:
        """回滚到指定历史版本：将其设为当前画像（当前版本会被自动归档）。"""
        target = self.get_style_profile_version(vid)
        if not target:
            return False
        # 复用 save_style_profile：先归档当前、再写入目标，历史链完整不丢失
        self.save_style_profile(target, trigger="rollback")
        return True

