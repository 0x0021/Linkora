"""答复门禁检测引擎。

对用户提问做内容级拦截判定：命中任一启用的门禁规则，则禁止 AI 答复并给出
拦截提示。规则分类至少覆盖：
  1) 私人隐私（private_privacy）：家庭、公司、家庭成员信息、年龄婚史等
  2) 违法信息（illegal_info）：领导人信息、翻墙工具（梯子）等
  3) 负面情绪（negative_emotion）：脏话、辱骂性用语
  4) 其他（other）：可扩展的禁止类别

匹配方式：
  - keyword：命中模式为「逗号分隔的多个关键词」，任一关键词在文本中（大小写不敏感）
    作为子串出现即命中，适合精确词表类门禁（VPN / 梯子 / 脏话 等）。
  - regex：命中模式为正则表达式，用 regex 库带超时匹配（ReDoS 防护），
    适合需要结构/变体覆盖的复杂模式。

模块级缓存：门禁规则为全局配置（不随平台隔离），启用规则集合按 TTL 缓存，
避免每条入站消息都查库；后台写入后最多 60s 自然生效（与关键词规则热重载一致）。
命中测试接口可传 force=True 强制读最新。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

# regex 库支持 search(text, timeout=N)，能真正中断 CPU-bound 的灾难性回溯
# （Python 内置 re 无此能力），用于 ReDoS 防护。语法与 re 兼容。
import regex as _regex

logger = logging.getLogger(__name__)

# ── 预设分类（管理员仍可在后台自由输入自定义分类名，做到可扩展）─────────────
PRESET_CATEGORIES: dict[str, str] = {
    "private_privacy": "私人隐私",
    "illegal_info": "违法信息",
    "negative_emotion": "负面情绪",
    "other": "其他",
}

# 各分类的默认拦截提示（规则未单独配置 intercept_message 时回退使用）
DEFAULT_INTERCEPT: dict[str, str] = {
    "private_privacy": "抱歉，涉及个人隐私的信息我无法提供或讨论。",
    "illegal_info": "抱歉，相关违法或违规内容我无法协助。",
    "negative_emotion": "请使用文明、得体的表述重新描述您的问题，我会尽力协助。",
    "other": "抱歉，该内容暂不在我可回答的范围之内。",
}

# 模块级缓存（全局规则，无平台隔离）
_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict = {"rules": None, "ts": 0.0}


@dataclass
class GateResult:
    """门禁检测结果。

    blocked=False 时其余字段无意义（放行）。
    """

    blocked: bool
    category: str = ""
    category_label: str = ""
    rule_id: Optional[int] = None
    rule_name: str = ""
    pattern: str = ""
    match_type: str = ""
    intercept_message: str = ""


def _label_for(category: str) -> str:
    return PRESET_CATEGORIES.get(category, category) or category


def _rule_matches(rule: dict, text: str, regex_timeout: float, compiled_cache: dict) -> bool:
    """判断单条规则是否命中文本。"""
    match_type = (rule.get("match_type") or "keyword").lower()
    pattern = (rule.get("pattern") or "").strip()
    if not pattern:
        return False

    if match_type == "regex":
        try:
            obj = compiled_cache.get(rule["id"])
            if obj is None:
                obj = _regex.compile(pattern)
                compiled_cache[rule["id"]] = obj
            try:
                return obj.search(text, timeout=regex_timeout) is not None
            except TypeError:
                # 内置 re.Pattern 不支持 timeout kwarg，回退
                return obj.search(text) is not None
        except _regex.error:
            logger.warning("[门禁] 跳过无效正则规则 id=%s: %s", rule.get("id"), pattern)
            return False
        except TimeoutError:
            # 灾难性回溯被中断：fail-safe 跳过该规则，不阻塞消息流
            logger.warning(
                "[门禁] 正则规则 id=%s 匹配超时(>%.1fs)，已跳过(可能 ReDoS): %s",
                rule.get("id"), regex_timeout, pattern,
            )
            return False

    # keyword（默认）：逗号分隔多关键词，任一作为子串出现即命中（大小写不敏感）
    text_lower = text.lower()
    for kw in pattern.split(","):
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw in text_lower:
            return True
    return False


def load_enabled_gate_rules(store, force: bool = False) -> list[dict]:
    """加载启用中的门禁规则（按优先级降序），带模块级 TTL 缓存。"""
    global _cache
    if not force:
        with _cache_lock:
            if _cache["rules"] is not None and __import__("time").time() - _cache["ts"] < _CACHE_TTL:
                return _cache["rules"]
    try:
        rules = store.list_gate_rules(enabled=1)
    except Exception as e:  # noqa: BLE001
        logger.warning("[门禁] 加载启用规则失败，放行: %s", e)
        return []
    rules.sort(key=lambda r: r.get("priority", 0), reverse=True)
    with _cache_lock:
        _cache["rules"] = rules
        _cache["ts"] = __import__("time").time()
    return rules


def invalidate_gate_cache() -> None:
    """后台写入后手动失效缓存（让最新规则立即生效，不必等 TTL）。"""
    with _cache_lock:
        _cache["rules"] = None
        _cache["ts"] = 0.0


def evaluate_gate_rules(
    store,
    text: str,
    force: bool = False,
    regex_timeout: float = 1.0,
) -> GateResult:
    """检测文本是否命中任一启用的门禁规则。

    Args:
        store: SQLiteStore 实例（提供 list_gate_rules / increment_gate_rule_hit）。
        text: 待检测的用户提问文本。
        force: True 时跳过缓存、强制读最新规则（用于后台命中测试）。
        regex_timeout: regex 单次匹配超时秒数（ReDoS 防护）。

    Returns:
        GateResult：blocked=True 且携带分类/规则/拦截提示；否则 blocked=False。
    """
    if not text or not text.strip():
        return GateResult(blocked=False)

    rules = load_enabled_gate_rules(store, force=force)
    if not rules:
        return GateResult(blocked=False)

    compiled_cache: dict = {}
    for rule in rules:
        if _rule_matches(rule, text, regex_timeout, compiled_cache):
            try:
                store.increment_gate_rule_hit(rule["id"])
            except Exception:  # noqa: BLE001
                logger.debug("[门禁] 命中计数自增失败（不影响拦截）rule_id=%s", rule.get("id"))
            category = rule.get("category") or "other"
            return GateResult(
                blocked=True,
                category=category,
                category_label=rule.get("category_label") or _label_for(category),
                rule_id=rule.get("id"),
                rule_name=rule.get("name", ""),
                pattern=rule.get("pattern", ""),
                match_type=rule.get("match_type", "keyword"),
                intercept_message=(
                    rule.get("intercept_message")
                    or DEFAULT_INTERCEPT.get(category, DEFAULT_INTERCEPT["other"])
                ),
            )
    return GateResult(blocked=False)
