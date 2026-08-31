"""Poller 子系统 mixin 共享基类（F9 类型治理）。

组合类 MessagePoller 由 8 个 poller mixin 经多继承组合：
    PollerStrategyMixin + AccessControlMixin + OcrMixin + ParseMixin +
    DedupMixin + DispatchMixin + HistorySyncMixin + DiscoveryMixin
这些 mixin 交叉访问彼此的状态（self._xxx）与方法（self._xxx()），而 pyright 逐文件
静态分析时 self 只认当前 mixin，故报 "unknown attribute"。

本类作为所有 poller mixin 的共同祖先，集中声明这些交叉成员：
- 状态：实例属性注解（无值，组合类 / 各 mixin __init__ 赋值）。内部状态统一用 Any，
  因为各属性在赋值点已声明精确类型（如 self._last_poll_time: dict[...]），基类用 Any
  既能消解跨 mixin 访问的 unknown-attribute 报错，又不会与赋值点的真实类型冲突。
- 方法：以「真实实现的完整签名」原样声明（非 *args/**kwargs，避免与子类实现的
  具体签名触发 reportIncompatibleMethodOverride）。真实实现保留在各自 mixin 或
  MessagePoller 中，运行时经 MRO 解析到真实实现，零行为风险。

惰性注解 + TYPE_CHECKING 导入，运行时零 import、零 MRO 风险。
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

from src.component_base import LinkoraComponentBase

if TYPE_CHECKING:
    from src.config import PollerConfig
    from src.models import Message


class PollerMixinBase(LinkoraComponentBase):
    # === 共享状态（组合类 __init__ 赋值；poller 收到的是 PollerConfig） ===
    config: PollerConfig

    # poller 家族中 current_user_* 由 MessagePoller.__init__ 以必填 str 注入
    # （current_user_user_id 默认 ""），故在此收窄为 str，避免跨 mixin 调用时
    # 被共享基类的 str | None 误判（如 fix_self_message_roles 要求 str）。
    current_user_id: str
    current_user_name: str
    current_user_user_id: str

    # === poller 内部状态（组合类 / 各 mixin __init__ 赋值；精确类型见赋值点） ===
    _deferred_total: Any
    _dispatch_total: Any
    _first_poll: Any
    _image_executor: Any
    _inaccessible_conversations: Any
    _last_cycle_deferred: Any
    _last_cycle_dispatched: Any
    _last_error: Any
    _last_error_at: Any
    _last_fetch_time: Any
    _last_fetch_round: Any
    _last_poll_at: Any
    _chat_rate_limited_until: Any
    _last_poll_time: Any
    _poll_shared_lock: Any
    # 类级 fallback 锁：未显式初始化（如单元测试 Fake 不调 MessagePoller.__init__）时，
    # 所有实例共享此锁即可满足并发临界区串行化；生产 MessagePoller.__init__ 会用独立
    # 实例锁覆盖它，保证各平台轮询器锁隔离。
    _poll_shared_lock = threading.Lock()
    _metadata_unavailable: Any
    _ocr_cache_lock: Any
    _ocr_call_lock: Any
    _ocr_futures: Any
    _ocr_image_paths: Any
    _ocr_lock: Any
    _ocr_results: Any
    _perm_fail_streak: Any
    _perm_warned: Any
    _poll_count: Any
    _processed_msg_ids: Any
    _queue_depth: Any
    _reconcile_every: Any
    _rule_engine: Any
    _skip_ocr: Any

    # === 跨 mixin 方法（真实签名原样；实现在同文件 mixin 或 MessagePoller） ===

    def _max_new_message_age_days(self) -> float:
        """轮询侧「新消息」年龄门槛（天）：超过此天数的消息不触发 AI 回复。

        实现放在共享基类而非某个 mixin：list-all 路径（DiscoveryMixin）与
        per-conversation 路径（PollerStrategyMixin）都要用它，而测试里的 fake
        往往只继承单个 mixin——放 mixin 里会让另一条路径拿到基类的空桩（None）。

        与 history_days 的语义区别（两者必须分开，混用会出事故）：
        - history_days：取最近 N 天的历史作为对话上下文 / RAG 语料，7 天是合理值；
        - 本值：判定「这条消息够不够新，要不要触发 AI 回复」。

        去重是防重放的第一道防线，但它并不总是可靠——handler 抛错时不标记
        （_dispatch_one 的语义：失败留给下轮重试）、服务中断、历史同步路径异常
        都会导致老消息在去重表里查不到。此时若年龄门槛沿用 7 天，list-all 会把
        几天前的老消息当新消息重放，与当前新消息合并成同一批投喂，LLM 会把用户
        的新问题认成旧话题的收尾。

        实测事故（2026-08-31）：8-25 的一张「桌面分配失败」截图因当天漏标，
        在 8-31 被重放并与当天「VDI 更新后黑屏」合并投喂，AI 回了
        「收到，问题已解决就好。」——因为 8-25 那个话题当时确实已经解决。

        取 min(history_days, poll_new_message_max_age_hours / 24)；
        两者都 <= 0 时返回 inf（不做年龄过滤）。
        """
        # 只用 getattr 读取：旧配置 / 测试 fake 可能没有新字段，不能让它炸
        try:
            history_days = float(getattr(self.config, "history_days", 0) or 0)
            cap_hours = float(getattr(self.config, "poll_new_message_max_age_hours", 0) or 0)
        except (TypeError, ValueError):
            return float("inf")

        limit = history_days if history_days > 0 else float("inf")
        if cap_hours > 0:
            limit = min(limit, cap_hours / 24.0)
        return limit

    def _block_chats_from_list_all(self, result: Any, source: str = "feishu_permission") -> int: ...
    def _block_conversation(self, open_id: str, title: str, chat_type: str, error: Exception, source: str = "runtime_error") -> None: ...
    def _check_if_bot_message(self, msg: Message) -> bool: ...
    def _detect_chat_type(self, conv: dict) -> str: ...
    def _dispatch_one(self, msg: "Message", handler: Callable[["Message"], None]) -> None: ...
    def _download_card_images(self, content: str, chat_id: str, chat_name: str, msg_id: str) -> dict[str, str]: ...
    def _download_image_only(self, raw: dict, chat_id: str, chat_name: str, fallback: str, msg_id: str) -> tuple[str, str]: ...
    def _download_received_file(self, raw: dict, chat_id: str, chat_name: str, msg_id: str, media_type: str) -> tuple[str, str]: ...
    def _effective_skip_types(self) -> set: ...
    def _extract_image_caption(self, raw: dict) -> str: ...
    def _feishu_correct_chat_type(self, conv_id: str, title: str = "", current_chat_type: str = "") -> str: ...
    def _fetch_messages_via_list_all(self) -> list[Message]: ...
    def _get_cached_top_conversations(self) -> list: ...
    def _get_recent_conversations_from_db(self) -> list[dict]: ...
    def _handle_edit_message(self, msg: Message) -> None: ...
    def _handle_recall_message(self, msg: Message) -> None: ...
    def _is_at_me(self, raw: dict) -> bool: ...
    def _is_blacklisted_conversation(self, chat_name: str, chat_type: str) -> bool: ...
    def _is_blocked(self, open_id: str) -> bool: ...
    def _is_duplicate_self_message(self, msg: Message) -> bool: ...
    def _is_global_permission_error(self, error: Exception) -> bool: ...
    def _is_msg_processed(self, msg_id: str) -> bool: ...
    def _is_permission_error(self, error: Exception) -> bool: ...
    def _is_self_message(self, message: Message) -> bool: ...
    def _is_self_sender(self, sender_id: str) -> bool: ...
    def _is_system_sender(self, sender_name: str) -> bool: ...
    def _store_self_message_if_new(self, msg: "Message") -> None: ...
    def _mark_msg_processed(self, msg_id: str, chat_id: str, msg=None) -> None: ...
    def _merge_consecutive_messages(self, messages: list[Message], window_seconds: int = 60) -> list[Message]: ...
    def _raw_to_message(self, raw: dict, chat_id: str, chat_type: str, chat_name: Optional[str]) -> Message: ...
    def _reconcile_blocklist(self) -> int: ...
    def _register_perm_failure(self, open_id: str) -> tuple[bool, int]: ...
    def _resolve_single_chat_peer(self, open_id: str, title: str) -> dict: ...
    def _should_skip_longtail_fetch(self, open_id: str, forced: bool) -> bool: ...
    def _submit_image_for_ocr(self, raw: dict, chat_id: str, chat_name: str, fallback: str, msg_id: str) -> tuple[str, str]: ...
    def _warn_permission_once(self, key: str, message: str) -> None: ...
