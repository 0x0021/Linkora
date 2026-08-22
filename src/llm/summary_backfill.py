"""摘要连续性补跑（SummaryBackfill）：检测停机时长，按自然日补齐遗漏窗口的摘要。

背景与问题：
- 现有 ``RollingSummaryScheduler``（H2-B）与 ``ProactiveDigestScheduler``（P4-13）
  均基于「当前时间滚动窗口」，进程中断期间不会写缓存、也不会补跑错过的那一天。
- 用户中途关机/未运行 Linkora，再启动时「当天 / 近七天」摘要会出现空洞，破坏连续性。

本模块职责（best-effort，非阻塞主回复链路）：
1. 启动时读取主库 ``meta.last_run_at``（上次成功运行时间），与「现在」算出停机时长。
2. 若停机跨越了自然日，把遗漏窗口切成若干「自然日」[day_start, day_end)。
3. 对**每个遗漏自然日、每个当天有消息的会话**，现场调 LLM 聚合当日消息生成摘要，
   复用 ``summarize_conversation`` 同一 prompt 与格式（以「【对话摘要】」开头），
   写回 ``conversation_summaries``（与正常周期同源同口径），``updated_at`` 标为该日结束时刻，
   使其正确归属到那一天（近七天视图 ``since=7天前`` 自然覆盖）。
4. 补跑完成后，若 ``proactive`` 未启用，仍用 ``build_digest``（与主动推送同源）生成一份
   「近七天对话摘要」文本并 INFO 输出，保证「当天 / 近七天」两维度都有一致格式产出。
5. 最后把 ``last_run_at`` 更新为「现在」，下次启动若无停机则不补跑（幂等）。

设计约束：
- **绝不拖垮主链路**：全程在独立 daemon 线程，所有 DB/LLM 异常仅记日志。
- **节流**：两次 LLM 调用间按 ``llm_throttle.background_min_interval_seconds`` 休眠，
  防止停机多天时 LLM 被轰炸。
- **上限保护**：``max_backfill_days``（默认 14）钳制最多补跑的天数，避免极端停机刷爆。
- **时区稳健**：消息 ``timestamp`` 库内格式可能混合（naive 本地 / 带 Z 的 UTC），
  SQL 仅做粗筛，python 端解析后按本地时区精确归到自然日，避免字符串比较跨时区误判。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环导入
    from src.config_models import SummaryBackfillConfig
    from src.llm.agent import LLMAgent
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

META_LAST_RUN_AT = "last_run_at"
_LAST_RUN_GRACE_MINUTES = 2  # 距上次运行不足此分钟数视为「未停机」，不补跑

# 进程级本地时区（进程内不变）。标准库无 timezone.local，用 now().astimezone() 推断。
LOCAL_TZ = datetime.now().astimezone().tzinfo


def _parse_ts(ts: str) -> Optional[datetime]:
    """把消息库里的 timestamp 解析为本地时区 datetime；解析失败返回 None。

    兼容：带 Z 的 UTC ISO、带 +00:00 偏移的 ISO、naive 本地时间字符串。
    """
    if not ts:
        return None
    try:
        s = ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # naive：按本地时间处理
            return dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    except (ValueError, TypeError):
        return None


class SummaryBackfill:
    """启动时检测停机并补生成遗漏窗口摘要的后台调度器（默认开启）。"""

    def __init__(
        self,
        agent: "LLMAgent",
        store: "SQLiteStore",
        config: "SummaryBackfillConfig",
        platform: str = "dingtalk",
        min_interval_seconds: float = 20.0,
    ) -> None:
        self._agent = agent
        self._store = store
        self._cfg = config
        self._platform = platform
        self._min_interval = max(1.0, float(min_interval_seconds))
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not self._cfg.enabled:
            logger.info("[摘要补跑] 未启用（summary_backfill.enabled=false），不启动")
            return
        if self._thread is not None and self._thread.is_alive():
            logger.debug("[摘要补跑] 已运行，忽略重复 start()")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="summary-backfill", daemon=True,
        )
        self._thread.start()
        logger.info("[摘要补跑] 已启动（平台=%s，最大补跑=%d天）",
                    self._platform, self._cfg.max_backfill_days)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[摘要补跑] 停止超时（%.1fs）", timeout)
        self._thread = None

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        try:
            self.run()
        except Exception as e:  # noqa: BLE001
            logger.warning("[摘要补跑] 执行异常: %s", e, exc_info=True)
        finally:
            # 无论成功失败，都刷新 last_run_at，避免下次启动误判持续停机疯狂补跑
            try:
                self._store.set_meta(META_LAST_RUN_AT, datetime.now().isoformat())
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 核心
    def run(self, now: Optional[datetime] = None) -> None:
        """执行一次补跑（可被测试直接调用，不触碰线程）。

        ``now`` 可注入用于测试；缺省取当前时间。
        """
        now = now or datetime.now()
        last_run = self._read_last_run_at()

        if last_run is None:
            # 首次运行：仅记录时间，不补跑（无历史可补）
            logger.info("[摘要补跑] 首次运行，记录基准时间，不补跑历史")
            self._store.set_meta(META_LAST_RUN_AT, now.isoformat())
            return

        gap = now - last_run
        if gap.total_seconds() < _LAST_RUN_GRACE_MINUTES * 60:
            logger.debug("[摘要补跑] 距上次运行 %.1f 分钟，未停机，跳过", gap.total_seconds() / 60)
            return

        # 停机跨越的自然日数（last_run 当天也算遗漏，因为那天可能没跑完整）
        days_gap = (now.date() - last_run.date()).days
        if days_gap <= 0:
            logger.debug("[摘要补跑] 未跨自然日（停机 %.1f 小时），无需按天补跑", gap.total_seconds() / 3600)
            return

        max_days = max(1, int(self._cfg.max_backfill_days))
        backfill_days = min(days_gap, max_days)
        if days_gap > max_days:
            logger.warning(
                "[摘要补跑] 停机 %d 天超过上限 %d 天，仅补最近 %d 天",
                days_gap, max_days, max_days,
            )

        # 取最近 (backfill_days + 7) 天粗窗口消息，python 端精确归日到自然日
        lookback_start = (now - timedelta(days=backfill_days + 7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # 遗漏的自然日列表：从 last_run 所在自然日的次日 到 昨天（今天由正常滚动/主动覆盖）
        missed_days = self._list_missed_days(last_run, now, backfill_days)
        if not missed_days:
            logger.debug("[摘要补跑] 无可补跑的自然日")
            return

        logger.info(
            "[摘要补跑] 检测到停机 %.1f 天，将补跑 %d 个自然日（%s ~ %s）",
            gap.total_seconds() / 86400, len(missed_days),
            missed_days[0].isoformat()[:10], missed_days[-1].isoformat()[:10],
        )

        # 粗筛：一次性取出窗口内消息（SQL 字符串比较，可能略多，python 端精分）
        start_iso = lookback_start.isoformat()
        end_iso = now.isoformat()
        grouped = self._store._conversation_repo.fetch_messages_in_range(
            start_iso=start_iso,
            end_iso=end_iso,
            platform=self._platform,
            limit_per_chat=200,
        )

        # 把消息按「自然日 → chat_id」归桶
        day_chat_msgs: "dict[datetime, dict[str, list[dict]]]" = {}
        for chat_id, msgs in grouped.items():
            for m in msgs:
                dt = _parse_ts(m.get("timestamp") or "")
                if dt is None:
                    continue
                day_key = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                if day_key.tzinfo is not None:
                    day_key = day_key.astimezone(LOCAL_TZ).replace(tzinfo=None)
                if day_key not in missed_days:
                    continue
                day_chat_msgs.setdefault(day_key, {}).setdefault(chat_id, []).append(m)

        # 逐日、逐会话补生成
        total_backfilled = 0
        for day_key in missed_days:
            if self._stop.is_set():
                break
            chat_map = day_chat_msgs.get(day_key, {})
            day_end = (day_key + timedelta(days=1) - timedelta(seconds=1))
            for chat_id, raw_msgs in chat_map.items():
                if self._stop.is_set():
                    break
                if len(raw_msgs) < int(self._cfg.min_messages_per_chat):
                    continue
                summary = self._summarize_day(chat_id, raw_msgs)
                if not summary:
                    continue
                try:
                    self._store._conversation_repo.upsert_conversation_summary(
                        chat_id=chat_id,
                        summary=summary,
                        older_boundary_msg_id=raw_msgs[-1].get("msg_id", ""),
                        covered_count=len(raw_msgs),
                        # updated_at 标为该日结束时刻，使其正确归属到这一天
                        platform=self._platform,
                    )
                    # upsert 用 now 写 updated_at，这里手工覆盖为 day_end 以正确归日
                    self._store._conversation_repo.update_summary_updated_at(
                        chat_id, day_end.isoformat(), platform=self._platform,
                    )
                    total_backfilled += 1
                    logger.debug(
                        "[摘要补跑] %s 当日摘要已写回 chat_id=%s（%d 条）",
                        day_key.isoformat()[:10], chat_id[:20], len(raw_msgs),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[摘要补跑] 写回失败 chat_id=%s: %s", chat_id, e)
                self._throttle()
            if not self._stop.is_set():
                logger.info("[摘要补跑] %s 当日补跑完成（%d 个会话）",
                            day_key.isoformat()[:10], len(chat_map))

        logger.info("[摘要补跑] 本轮共补写 %d 个会话摘要", total_backfilled)

        # 近七天视图：若 proactive 未启用，仍用同源 build_digest 输出一份近七天摘要
        self._emit_recent_7d_if_needed(now)

    # ------------------------------------------------------------------ 辅助
    def _read_last_run_at(self) -> Optional[datetime]:
        raw = self._store.get_meta(META_LAST_RUN_AT, "")
        if not raw:
            return None
        dt = _parse_ts(raw)
        if dt is None:
            return None
        # 统一到 naive 本地，便于与 now() 做 date 比较
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return dt

    @staticmethod
    def _list_missed_days(last_run: datetime, now: datetime, max_days: int) -> "list[datetime]":
        """返回需补跑的自然日列表（本地时区 00:00 的 datetime）。

        从 last_run 所在自然日的**次日** 起到 **昨天**（今天由正常调度覆盖）。
        最多 max_days 天，超出取最近的 max_days 天。
        """
        last_day = last_run.replace(hour=0, minute=0, second=0, microsecond=0)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days: "list[datetime]" = []
        cur = last_day + timedelta(days=1)
        while cur < today:
            days.append(cur)
            cur += timedelta(days=1)
        if not days:
            return []
        if len(days) > max_days:
            days = days[-max_days:]
        return days

    def _summarize_day(self, chat_id: str, raw_msgs: "list[dict]") -> str:
        """把当日原始消息转成 Message 列表，调 LLM 生成当日摘要（与正常周期同源同格式）。"""
        from src.models import Message
        from src.llm.agent_steps.reply import summarize_conversation

        messages = []
        for m in raw_msgs:
            ts = _parse_ts(m.get("timestamp") or "")
            if ts is None:
                ts = datetime.now(LOCAL_TZ)
            elif ts.tzinfo is not None:
                ts = ts.astimezone(LOCAL_TZ).replace(tzinfo=None)
            messages.append(Message(
                msg_id=m.get("msg_id") or "",
                chat_id=m.get("chat_id") or chat_id,
                chat_type=m.get("chat_type") or "",
                chat_name="",
                sender_id=m.get("sender_id") or "",
                sender_name=m.get("sender_name") or "",
                content=m.get("content") or "",
                msg_type=m.get("msg_type") or "text",
                timestamp=ts,
            ))
        if not messages:
            return ""
        try:
            return (summarize_conversation(self._agent, messages) or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("[摘要补跑] LLM 生成当日摘要失败 chat_id=%s: %s", chat_id, e)
            return ""

    def _emit_recent_7d_if_needed(self, now: datetime) -> None:
        """补跑完成后用同源 build_digest 生成并输出一份近七天摘要（与 Web/主动推送同源同格式）。

        无论 proactive 是否启用都输出预览日志：保证「当天 / 近七天」两维度都有一致格式产出，
        且数据已写回 conversation_summaries，Web「对话摘要」页与主动推送的近七天视图自动完整。
        """
        try:
            from src.llm.proactive_digest import build_digest
            since = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
            rows = self._store._conversation_repo.list_recent_summaries(
                limit=50, platform=self._platform, since=since.isoformat(),
            )
            items = [{
                "chat_id": r.get("chat_id"),
                "chat_name": r.get("chat_name"),
                "summary": (r.get("summary_text") or "").strip(),
            } for r in rows if (r.get("summary_text") or "").strip()]
            digest = build_digest(items, title="📋 近七天对话摘要（补跑后）")
            logger.info("[摘要补跑] 近七天摘要预览：\n%s", digest)
        except Exception as e:  # noqa: BLE001
            logger.debug("[摘要补跑] 生成近七天预览失败: %s", e)

    def _throttle(self) -> None:
        """两次 LLM 调用间节流休眠（可被 stop 提前唤醒）。"""
        if self._stop.wait(timeout=self._min_interval):
            return
