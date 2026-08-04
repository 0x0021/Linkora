from __future__ import annotations

import logging
import copy
import re
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# regex 库支持 search(text, timeout=N)，能真正中断 CPU-bound 的灾难性回溯
# （Python 内置 re 无此能力），用于 ReDoS 防护。语法与 re 兼容。
import regex as _regex

_REGEX_TIMEOUT_ERRORS = (TimeoutError,)

# 抑制 jieba 内部 pkg_resources 弃用警告（在 import jieba 之前设置）
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pkg_resources")
warnings.filterwarnings("ignore", message=".*pkg_resources.*")

import jieba

from src.config import RulesConfig
from src.intent import IntentRegistry
from src.models import Message

jieba.setLogLevel(20)  # 静默 jieba 初始化日志

logger = logging.getLogger(__name__)

WEEKDAY_MAP = {
    "周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4,
    "周六": 5, "周日": 6, "星期一": 0, "星期二": 1, "星期三": 2,
    "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6,
}


@dataclass
class RuleResult:
    action: str  # skip | reply | pass
    reply_text: Optional[str] = None
    reason: str = ""
    rule_id: Optional[int] = None
    match_type: str = ""
    captured_groups: dict[str, str] | None = None  # 正则捕获组
    intent: str = ""  # 处置意图（来自抽象意图注册表）：business | social.gratitude | ...
    intent_confidence: float = 1.0  # 意图分类置信度 0.0~1.0（低置信业务消息可能为纯表情/噪音）


@dataclass
class KeywordRule:
    id: int
    category: str
    match_pattern: str
    reply_text: str
    match_type: str  # exact | fuzzy | regex
    priority: int
    enabled: bool
    hit_count: int = 0
    # regex 类型规则的预编译对象 (reload_db_keywords 时编译, 避免每条消息重复编译)
    _compiled: "re.Pattern | None" = None

    def matches(self, text: str, stop_words: set[str] | None = None,
                timeout: float = 1.0) -> tuple[bool, dict[str, str] | None]:
        """匹配文本，返回 (是否匹配, 捕获组字典)。

        Args:
            stop_words: 小写停用词集。如果为 None 则不做停用词过滤（不推荐）。
            timeout: regex 匹配单次超时秒数（ReDoS 防护），仅对 regex 类型生效。
        """
        if not self.enabled:
            return False, None
        pattern = self.match_pattern
        if self.match_type == "exact":
            return text.strip() == pattern.strip(), None
        elif self.match_type == "fuzzy":
            # 支持逗号分隔的多关键字，任一命中即匹配
            if not text:
                return False, None
            text = text.strip()
            text_lower = text.lower()
            keywords = [k.strip() for k in pattern.split(",") if k.strip()]

            # 构建有意义 token 判断函数
            def is_meaningful(token: str) -> bool:
                if stop_words is not None:
                    return token not in stop_words and len(token) > 1
                return len(token) > 1

            # 消息的有意义 token（过滤停用词，转小写）
            text_tokens = {t.lower() for t in jieba.lcut(text) if is_meaningful(t.lower())}

            for kw in keywords:
                kw_lower = kw.lower()
                # 策略1: 关键字完整短语在消息中（大小写不敏感）→ 高置信度直接命中
                if kw_lower in text_lower:
                    return True, None

                # 策略2: 有意义 token 交集（需满足最低质量门槛）
                kw_tokens = {t.lower() for t in jieba.lcut(kw) if is_meaningful(t.lower())}
                overlap = text_tokens & kw_tokens
                if overlap:
                    # 允许单 token 匹配的条件（二选一）：
                    #   A. 原始文本很短（≤4 字符），如 "VPN"/"打印机"
                    #   B. 消息仅剩 1 个有意义 token 且该 token ≥ 3 字符（专有名词足够具体）
                    # 其他情况要求 ≥ 2 个 token 重叠
                    is_very_short_text = len(text) <= 4
                    is_single_specific_token = (
                        len(text_tokens) == 1
                        and len(next(iter(text_tokens))) >= 3
                    )
                    if (is_very_short_text or is_single_specific_token) and len(overlap) >= 1:
                        return True, None
                    if len(overlap) >= 2:
                        return True, None
            return False, None
        elif self.match_type == "regex":
            try:
                # 优先用预编译对象 (DB 规则在 reload 时已用 regex 库编译);
                # 回退到临时编译兼容旧路径。
                pattern_obj = self._compiled if self._compiled is not None else _regex.compile(pattern)
                try:
                    if timeout and timeout > 0:
                        try:
                            match = pattern_obj.search(text, timeout=timeout)
                        except TypeError:
                            # 内置 re.Pattern 不支持 timeout kwarg，回退
                            match = pattern_obj.search(text)
                    else:
                        match = pattern_obj.search(text)
                except _REGEX_TIMEOUT_ERRORS:
                    # 灾难性回溯被中断：fail-safe 跳过该规则，不阻塞消息流
                    logger.warning(
                        "正则规则 id=%s 匹配超时(>%.1fs)，已跳过(可能是 ReDoS 灾难性回溯): %s",
                        self.id, timeout, pattern,
                    )
                    return False, None
                if match:
                    # 提取命名捕获组和数字索引捕获组
                    groups = match.groupdict()
                    # 如果没有命名捕获组，使用数字索引
                    if not groups:
                        groups = {str(i): v for i, v in enumerate(match.groups(), start=1)}
                    return True, groups
                return False, None
            except _regex.error:
                logger.warning("无效的正则表达式模式: %s", pattern)
                return False, None
        return False, None


class RuleEngine:
    def __init__(self, config: RulesConfig, db_store=None):
        self.config = config
        self._db_store = db_store
        # ReDoS 防护：正则匹配单次超时秒数（用户/DB 可配置正则均适用）
        self._regex_timeout = getattr(config, "regex_timeout_seconds", 1.0)
        # 黑白名单/配置关键词均用 regex 库编译，匹配时才能传 timeout
        self._blacklist_users = [_regex.compile(p) for p in config.blacklist.get("users", [])]
        self._blacklist_groups = [_regex.compile(p) for p in config.blacklist.get("groups", [])]
        self._whitelist_enabled = config.whitelist.get("enabled", False)
        self._whitelist_users = [_regex.compile(p) for p in config.whitelist.get("users", [])]
        self._whitelist_groups = [_regex.compile(p) for p in config.whitelist.get("groups", [])]
        self._config_keywords = [(_regex.compile(kw.match), kw.reply) for kw in config.keywords]
        self._db_keywords: list[KeywordRule] = []
        self._last_reload = 0.0
        self._reload_interval = 60.0
        self._db_kw_lock = threading.Lock()

        # 从配置加载停用词表（支持逗号分隔的字符串）
        self._stop_words_lower = self._parse_stop_words(config.stop_words)

        # 抽象意图注册表：处置层意图（business / social 子型）集中定义，
        # 支持用 config.intent_filter 中的关键词/阈值覆盖默认证据词（运维可不改代码自定义）。
        self._intent_registry = IntentRegistry()
        self._intent_registry.apply_intent_filter(getattr(config, "intent_filter", {}) or {})
        # Phase 0 启动自检：打印生效关键词数量和配置摘要
        sc = self._intent_registry.self_check()
        logger.info(
            "[意图自检] business_ratio=%.2f | 类别关键词: %s",
            sc["business_ratio_threshold"],
            {cid: info["keyword_count"] for cid, info in sc["categories"].items()},
        )

    def _parse_stop_words(self, stop_words_config: list[str]) -> set[str]:
        """解析停用词配置为小写集合。支持每行逗号分隔的多词格式。"""
        words = set()
        for line in stop_words_config:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 逗号分隔：每个逗号分隔的元素是一个词
            for w in line.split(","):
                w = w.strip()
                if w:
                    words.add(w.lower())
        return words

    @property
    def stop_words(self) -> set[str]:
        """返回当前生效的小写停用词集（供外部如 web/api 使用）。"""
        return self._stop_words_lower

    def reload_stop_words(self, new_config: RulesConfig | None = None) -> None:
        """热重载停用词（不重启服务即可生效）。"""
        cfg = new_config or self.config
        self._stop_words_lower = self._parse_stop_words(cfg.stop_words)
        logger.info("停用词已重新加载，总计=%d", len(self._stop_words_lower))

    def reload_config(self, new_config: RulesConfig) -> None:
        """热重载全部配置（黑名单、白名单、关键词、停用词等全部重新编译）。"""
        self.config = new_config
        self._regex_timeout = getattr(new_config, "regex_timeout_seconds", 1.0)
        self._blacklist_users = [_regex.compile(p) for p in new_config.blacklist.get("users", [])]
        self._blacklist_groups = [_regex.compile(p) for p in new_config.blacklist.get("groups", [])]
        self._whitelist_enabled = new_config.whitelist.get("enabled", False)
        self._whitelist_users = [_regex.compile(p) for p in new_config.whitelist.get("users", [])]
        self._whitelist_groups = [_regex.compile(p) for p in new_config.whitelist.get("groups", [])]
        self._config_keywords = [(_regex.compile(kw.match), kw.reply) for kw in new_config.keywords]
        self._stop_words_lower = self._parse_stop_words(new_config.stop_words)
        self._intent_registry.apply_intent_filter(getattr(new_config, "intent_filter", {}) or {})
        logger.info("规则引擎配置已热重载（黑名单用户=%d, 黑名单群=%d, 配置关键词=%d）",
                    len(self._blacklist_users), len(self._blacklist_groups), len(self._config_keywords))

    def _is_meaningful_token(self, token: str) -> bool:
        """判断 token 是否有意义（非停用词且长度>1）。token 应为小写。"""
        return token not in self._stop_words_lower and len(token) > 1

    def _safe_search(self, pattern, text: str):
        """带 ReDoS 超时防护的 search。超时则视为不命中(fail-safe)，返回 None。

        兼容两种 pattern：regex 库(支持 timeout kwarg) 和内置 re(不支持)。
        内置 re.Pattern 传 timeout 会抛 TypeError，此时回退到无超时匹配。
        """
        try:
            if self._regex_timeout and self._regex_timeout > 0:
                try:
                    return pattern.search(text, timeout=self._regex_timeout)
                except TypeError:
                    # 内置 re.Pattern 不支持 timeout kwarg，回退
                    return pattern.search(text)
            return pattern.search(text)
        except _REGEX_TIMEOUT_ERRORS:
            logger.warning(
                "正则匹配超时(>%.1fs)，已视为不命中(可能是 ReDoS 灾难性回溯): %s",
                self._regex_timeout, getattr(pattern, "pattern", "?"),
            )
            return None
        except _regex.error:
            logger.warning("正则匹配出错: %s", getattr(pattern, "pattern", "?"))
            return None

    def _matches_any(self, text: str, patterns: list) -> bool:
        return any(self._safe_search(p, text) for p in patterns)

    def _detect_intent(self, content: str) -> tuple[str, str, float]:
        """检测消息意图，返回 (意图类型, 意图描述)。

        委托给抽象意图注册表（src.intent.IntentRegistry.classify_disposition），
        行为等价于旧实现：business 与 social 子型互斥（business 优先），
        social 含 gratitude/acknowledge/closing/polite 四子型，受长度阈值约束。

        意图类型：
        - "business": 业务消息，需要处理
        - "thank_you"/"acknowledge"/"closing"/"polite": 对应 social 子型，应跳过
        """
        cfg = self.config.intent_filter
        result = self._intent_registry.classify_disposition(
            content,
            enabled=cfg.get("enabled", True),
            # 配置未显式提供阈值时使用内置合理默认；
            # 这三个阈值现在真实生效（H1 修复：类内不再硬编码 max_length 而永久忽略配置）。
            pure_thank_max_length=cfg.get("pure_thank_max_length", 20),
            pure_ack_max_length=cfg.get("pure_ack_max_length", 10),
            pure_closing_max_length=cfg.get("pure_closing_max_length", 20),
        )
        if result.disposition == "business":
            return "business", result.reason, result.confidence
        # social 子型：用 short_label（thank_you / acknowledge / closing / polite）保持日志兼容
        return result.subtype or result.category_id, result.reason, result.confidence

    def reload_db_keywords(self) -> None:
        if not self._db_store:
            return
        import time
        now = time.time()
        if now - self._last_reload < self._reload_interval:
            return
        with self._db_kw_lock:
            # double-check after acquiring lock
            if now - self._last_reload < self._reload_interval:
                return
            try:
                rules = self._db_store.list_keyword_rules(enabled=1)
                loaded: list[KeywordRule] = []
                for r in rules:
                    kw = KeywordRule(
                        id=r["id"],
                        category=r["category"],
                        match_pattern=r["match_pattern"],
                        reply_text=r["reply_text"],
                        match_type=r["match_type"],
                        priority=r["priority"],
                        enabled=bool(r["enabled"]),
                        hit_count=r.get("hit_count", 0),
                    )
                    # regex 类型: 预编译并跳过无效正则 (避免运行时 error 崩溃)
                    # 用 regex 库编译，匹配时才能传 timeout 做 ReDoS 防护
                    if kw.match_type == "regex" and kw.match_pattern:
                        try:
                            kw._compiled = _regex.compile(kw.match_pattern)
                        except _regex.error as ce:
                            logger.warning("跳过无效正则规则 id=%s: %s (err=%s)", kw.id, kw.match_pattern, ce)
                            continue
                    loaded.append(kw)
                self._db_keywords = loaded
                self._db_keywords.sort(key=lambda x: x.priority, reverse=True)
                self._last_reload = now
                logger.debug("已从数据库重新加载 %d 条关键词规则", len(self._db_keywords))
            except Exception as e:
                logger.error("重新加载关键词规则失败: %s", e)

    def check(self, message: Message, now: Optional[datetime] = None) -> RuleResult:
        if not self.config.enabled:
            return RuleResult(action="pass", reason="rules disabled", intent="business")

        now = now or datetime.now()

        sender_name = message.sender_name or ""
        chat_name = message.chat_name or ""

        if message.chat_type == "single":
            if self._matches_any(sender_name, self._blacklist_users):
                return RuleResult(action="skip", reason=f"blacklisted user: {sender_name}", intent="business")
        else:
            if self._matches_any(chat_name, self._blacklist_groups):
                return RuleResult(action="skip", reason=f"blacklisted group: {chat_name}", intent="business")

        if self._whitelist_enabled:
            if message.chat_type == "single":
                if not self._matches_any(sender_name, self._whitelist_users):
                    return RuleResult(action="skip", reason=f"not in whitelist users: {sender_name}", intent="business")
            else:
                if not self._matches_any(chat_name, self._whitelist_groups):
                    return RuleResult(action="skip", reason=f"not in whitelist groups: {chat_name}", intent="business")

        self.reload_db_keywords()

        content = message.content or ""

        # === 先匹配关键词规则（DB + config），命中则直接回复 ===
        # 关键词规则优先于意图过滤，确保用户配置的规则不会被意图识别提前拦截
        keywords = copy.copy(self._db_keywords)
        for rule in keywords:
            matched, groups = rule.matches(
                content, stop_words=self._stop_words_lower, timeout=self._regex_timeout
            )
            if matched:
                if self._db_store:
                    try:
                        self._db_store.increment_keyword_hit(rule.id)
                    except Exception:
                        logger.warning("increment_keyword_hit failed rule_id=%s", rule.id, exc_info=True)
                # 模板变量替换：支持 {group_name} 或 {1}, {2} 等
                reply_text = rule.reply_text
                if groups:
                    for key, value in groups.items():
                        reply_text = reply_text.replace(f"{{{key}}}", value)
                return RuleResult(
                    action="reply",
                    reply_text=reply_text,
                    reason=f"keyword rule matched: {rule.match_pattern}",
                    rule_id=rule.id,
                    match_type=rule.match_type,
                    captured_groups=groups,
                    intent="business",
                )

        for pattern, reply in self._config_keywords:
            match = self._safe_search(pattern, content)
            if match:
                groups = match.groupdict()
                if not groups:
                    groups = {str(i): v for i, v in enumerate(match.groups(), start=1)}
                reply_text = reply
                if groups:
                    for key, value in groups.items():
                        reply_text = reply_text.replace(f"{{{key}}}", value)
                return RuleResult(
                    action="reply",
                    reply_text=reply_text,
                    reason=f"config keyword matched: {pattern.pattern}",
                    match_type="regex",
                    captured_groups=groups,
                    intent="business",
                )

        # === 关键词未命中，再做意图识别：跳过无业务价值的消息 ===
        intent_type, intent_desc, confidence = self._detect_intent(content)
        if intent_type != "business":
            logger.info("[意图识别] 用户消息: '%s' | 识别: %s | 处理: 跳过", content, intent_desc)
            return RuleResult(action="skip", reason=f"intent_filter: {intent_type} - {intent_desc}",
                               intent=intent_type)

        return RuleResult(action="pass", reason="no rule matched", intent=intent_type,
                           intent_confidence=confidence)
