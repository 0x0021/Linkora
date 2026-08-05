from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging

logger = logging.getLogger(__name__)


def _citation_relevant_to_reply(citation, reply_text: str) -> bool:
    """校验单条引文是否与回复正文存在语义关联（关键词重叠启发式）。

    判定规则（任一命中即视为相关）：
    1. 引文 source 标题中的「有义术语」（中文≥2字 / 英文≥3字母）出现在回复中；
    2. 引文 snippet 中 ≥2 个有义术语出现在回复中。

    这能同时解决：
    - 引用不相关（如问股价却引用 CRM 文档）→ source 名不含股价/股票等词 → 剔除；
    - 无引用仍追加（回复完全没提任何引文内容）→ 全部剔除 → 不追加页脚。
    """
    import re as _re
    if not reply_text or not getattr(citation, "source", ""):
        return False
    _reply_lower = reply_text.lower()
    _term_re = _re.compile(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}')

    # 规则1：source 标题术语重叠
    source_terms = set(_term_re.findall(getattr(citation, "source", "")))
    for t in source_terms:
        if t.lower() in _reply_lower:
            return True

    # 规则2：snippet 术语重叠（需 ≥2 个不同术语）
    snippet = getattr(citation, "snippet", "") or ""
    if snippet:
        snip_terms = set(_term_re.findall(snippet[:120]))
        matched = sum(1 for t in snip_terms if t.lower() in _reply_lower)
        if matched >= 2:
            return True

    return False


class ReplyHelpersMixin(EngineMixinBase):
    """回复增强辅助子系统：风格画像刷新 + 引文溯源页脚。

    从 RuntimeMixin「上帝类」中物理抽离的 cohesive 子系统（F1 重构）。
    仅依赖实例属性 self.config / self.store / self.platform_id /
    self.current_user_name 与 base 提供的 tracker / _active_platform_ctx /
    DEFAULT_STORAGE_PATH，行为与原实现逐字节一致。
    """

    def _refresh_style_profile(self) -> None:
        """启动期：按需从主人历史消息刷新 style_profiles 画像（Feature B）。

        调度策略（#16）：仅当画像【缺失】或【过期（updated_at 距今 ≥30 天）】时才重算；
        画像仍新鲜则跳过，避免每次启动都跑一次昂贵的抽样统计。
        重算在后台守护线程执行（非阻塞），失败不影响主流程。
        """
        try:
            if not self.store or not hasattr(self.store, "get_style_profile"):
                return
            owner = getattr(self, "current_user_name", "") or ""
            if not owner:
                return
            # 主线程快速判断：画像是否存在且仍新鲜
            existing = self.store._memory_ops_repo.get_style_profile()
            if isinstance(existing, dict) and not self._is_style_profile_stale(existing):
                days = self._style_profile_days_since(existing)
                logger.info("[风格] 画像仍新鲜（%s天前更新），跳过启动重算", days)
                return
            # 缺失或过期 → 后台线程重算（非阻塞）
            import threading
            t = threading.Thread(target=self._refresh_style_profile_worker, daemon=True)
            t.start()
        except Exception as e:
            logger.warning("[风格] 调度画像重算失败（不影响主流程）: %s", e)

    def _refresh_style_profile_worker(self) -> None:
        """后台线程：独立 store 实例跑 compute + save（避免与主线程共用连接）。"""
        try:
            from src.memory.store_factory import get_store
            owner = getattr(self, "current_user_name", "") or ""
            if not owner:
                return
            # 用主平台(dingtalk)库路径开独立 store（compute 仅查 messages 表，无需向量索引）
            path = getattr(self.config.storage, "path", None) or DEFAULT_STORAGE_PATH
            store = get_store(path)
            store.init_db()
            prof = store._memory_ops_repo.compute_style_profile(owner, platform=self.platform_id)
            if prof:
                max_v = getattr(self.config.llm, "persona_history_max_versions", 10) or 10
                store._memory_ops_repo.save_style_profile(prof, trigger="auto", max_versions=max_v)
                logger.info("[风格] 已刷新主人沟通风格画像：%s", prof.get("prompt", "")[:60])
        except Exception as e:
            logger.warning("[风格] 刷新画像失败（不影响主流程）: %s", e)

    @staticmethod
    def _style_profile_days_since(prof: dict) -> int | None:
        """画像 updated_at 距今天数（解析失败返回 None）。"""
        from datetime import datetime as _dt
        ua = prof.get("updated_at") if isinstance(prof, dict) else None
        if not ua:
            return None
        try:
            dt = _dt.fromisoformat(str(ua).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return (_dt.now() - dt).days
        except Exception:
            logger.warning("[resilience] silent exception in _style_profile_days_since", exc_info=True)
            return None

    @classmethod
    def _is_style_profile_stale(cls, prof: dict, stale_days: int = 30) -> bool:
        """画像是否过期（无 updated_at / 距今 ≥ stale_days 视为过期需重算）。"""
        if not isinstance(prof, dict) or not prof.get("updated_at"):
            return True
        days = cls._style_profile_days_since(prof)
        if days is None:
            return True
        return days >= stale_days

    def _append_citation_footer(self, text: str, reply, message) -> str:
        """按配置在回复末尾追加「—— 依据 / 参考来源：《标题》（相关度88%）」引文页脚。

        默认关闭（citation_enabled=False）→ 原样返回。开启后：
        - 群聊需 citation_in_group 也为 True 才追加；
        - 仅追加相关度 ≥ citation_low_threshold 的引文（低置信本就走转人工，不追加）；
        - best ≥ high 标「—— 依据：」，[low, high) 标「—— 参考来源：」（低置信不再追加"供参考"）；
        - 最多列 citation_max_items 条。
        - **语义相关性校验**：引文的 source 名称或 snippet 关键词必须在回复正文
          中出现（关键词重叠），否则视为「回复未实际引用该资料」而剔除；全部
          剔除则不追加页脚（解决引用不相关 + 无引用仍追加两个问题）。
        任何异常一律回退无页脚，绝不影响正常回复。
        """
        try:
            adv = self.config.llm.advanced
            if not getattr(adv, "citation_enabled", False):
                return text
            if (getattr(message, "chat_type", "") == "group"
                    and not getattr(adv, "citation_in_group", False)):
                return text
            cites = getattr(reply, "citations", None) or []
            if not cites:
                return text
            low = getattr(adv, "citation_low_threshold", 0.5)
            high = getattr(adv, "citation_high_threshold", 0.75)
            max_items = max(1, int(getattr(adv, "citation_max_items", 2)))

            # 阈值过滤：仅保留 score ∈ [low, 1.0] 的候选（排除异常高分如 300%）
            elig = sorted(
                [c for c in cites if 0 < (getattr(c, "score", 0) or 0) <= 1.0
                 and (getattr(c, "score", 0) or 0) >= low],
                key=lambda c: getattr(c, "score", 0) or 0,
                reverse=True,
            )
            if not elig:
                return text

            # 语义相关性过滤：仅保留与回复正文有关键词重叠的引文
            elig = [c for c in elig if _citation_relevant_to_reply(c, text)]
            if not elig:
                return text

            best = elig[0].score or 0
            # 高置信标「依据」，低置信标「参考来源」；低置信不再追加"供参考"。
            # 页脚统一以独立成行（前导 \n\n\n）拼接到回复末尾。
            if best >= high:
                prefix = "\n\n\n—— 依据："
            else:
                prefix = "\n\n\n—— 参考来源："
            # 内部分数（相关度XX%）默认隐藏（citation_show_score=False），
            # 避免把相似度指标泄露给最终用户；溯源仍靠《文档名》呈现。
            _show_score = getattr(adv, "citation_show_score", False)
            if _show_score:
                parts = [f"《{c.source}》（相关度{(c.score or 0):.0%}）"
                         for c in elig[:max_items]]
            else:
                parts = [f"《{c.source}》" for c in elig[:max_items]]
            footer = prefix + "、".join(parts)
            return text + footer
        except Exception:
            logger.warning("[resilience] 引文页脚拼接失败，回退无页脚", exc_info=True)
            return text

    def _mark_decision_cited(self, message, cited: int) -> None:
        """Roadmap ③ 成本/质量看板：回填当前决策行的 cited 标记。

        cited = 是否实际向用户追加了引文溯源页脚（仅凭 _append_citation_footer
        前后文本是否变化判定）。该标记在 tracker.record 之后才能确定（页脚在下方
        发送分支拼接），故此处按 request_id 定位本请求刚写入的决策行并原地 UPDATE。
        任何异常一律静默忽略，看板埋点绝不影响正常回复。
        """
        try:
            platform_id = _active_platform_ctx.get() or getattr(message, "platform_id", "") or ""
            rid = ""
            try:
                from src.utils.request_id import get_request_id
                rid = get_request_id() or ""
            except Exception as _exc:
                logger.debug(f"_mark_decision_cited: swallowed exception: {_exc}")
                pass
            store = tracker._store_for(platform_id) or self.store
            repo = getattr(store, "_decisions_repo", None)
            if repo is None:
                return
            repo.mark_cited(
                request_id=rid,
                platform_id=platform_id,
                conversation_id=getattr(message, "chat_id", "") or "",
                cited=int(cited or 0),
            )
        except Exception as e:
            logger.debug("[resilience] 回填 cited 标记失败（忽略）: %s", e)
