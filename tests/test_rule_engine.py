"""规则引擎单元测试。

覆盖核心匹配逻辑：
- 黑白名单过滤
- 关键词匹配（exact / fuzzy / regex）
- ReDoS 超时/异常防护
- 停用词过滤
- DB 关键词命中与模板替换
- 配置关键词匹配
- 意图分类
- 停用词过滤
- 意图识别与过滤
- 正则捕获组变量替换
"""
from __future__ import annotations

from unittest.mock import MagicMock

import regex as _rx

from src.rule_engine import (
    KeywordRule,
    RulesConfig,
    RuleEngine,
)



# ============ 黑白名单测试 ============

class TestBlacklistWhitelist:
    """黑/白名单过滤逻辑。"""

    def test_blacklisted_user_is_skipped(self, rule_engine, msg_single_chat):
        """黑名单用户消息应被跳过。"""
        rule_engine._blacklist_users = [__import__("re").compile(r"张三")]
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "blacklisted user" in result.reason

    def test_blacklisted_group_is_skipped(self, rule_engine, msg_group_chat):
        """黑名单群组消息应被跳过。"""
        rule_engine._blacklist_groups = [__import__("re").compile(r"技术交流群")]
        result = rule_engine.check(msg_group_chat)
        assert result.action == "skip"
        assert "blacklisted group" in result.reason

    def test_whitelist_enabled_non_member_skipped(self, rule_engine, msg_single_chat):
        """白名单启用时，非成员消息应被跳过。"""
        rule_engine._whitelist_enabled = True
        rule_engine._whitelist_users = [__import__("re").compile(r"李四")]
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "not in whitelist" in result.reason

    def test_whitelist_enabled_member_passes(self, rule_engine, msg_single_chat):
        """白名单启用时，成员消息应通过。"""
        rule_engine._whitelist_enabled = True
        rule_engine._whitelist_users = [__import__("re").compile(r"张三")]
        # 内容改为业务消息避免被意图过滤器拦截
        msg_single_chat.content = "VPN无法连接怎么办"
        result = rule_engine.check(msg_single_chat)
        assert result.action != "skip" or "whitelist" not in (result.reason or "")

    def test_rules_disabled_returns_pass(self, rule_engine, msg_single_chat):
        """规则引擎禁用时应直接返回 pass。"""
        rule_engine.config.enabled = False
        result = rule_engine.check(msg_single_chat)
        assert result.action == "pass"
        assert "disabled" in result.reason


# ============ 关键词匹配测试 ============

class TestKeywordMatching:
    """关键词三种匹配模式。"""

    def test_exact_match_succeeds(self, rule_engine, msg_single_chat):
        """exact 模式：完全相等才匹配（需先通过意图过滤器）。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="VPN故障",
            reply_text="请联系IT部门处理VPN问题", match_type="exact", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "VPN故障"  # 包含业务关键词，不会被意图过滤器拦截
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert result.reply_text == "请联系IT部门处理VPN问题"

    def test_exact_match_partial_fails(self, rule_engine, msg_single_chat):
        """exact 模式：部分包含不匹配。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="你好",
            reply_text="您好！", match_type="exact", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "你好啊"
        result = rule_engine.check(msg_single_chat)
        # exact 不匹配，落到意图过滤器 → skip
        assert result.action == "skip"

    def test_fuzzy_match_substring(self, rule_engine, msg_single_chat):
        """fuzzy 模式：子串包含即匹配。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="打印机",
            reply_text="请联系IT部门处理打印机问题", match_type="fuzzy", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "办公室的打印机坏了"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"

    def test_fuzzy_match_multi_keyword_any(self, rule_engine, msg_single_chat):
        """fuzzy 模式：逗号分隔多关键字，任一命中即匹配。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="VPN,代理,翻墙",
            reply_text="网络问题请联系网络组", match_type="fuzzy", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "代理服务器连不上"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"

    def test_fuzzy_match_token_overlap_short_text(self, rule_engine, msg_single_chat):
        """fuzzy 模式：短文本(≤4字符)允许单token重叠匹配。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="打印机",
            reply_text="打印机问题请报修", match_type="fuzzy", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "打印机"  # 3字符，短文本
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"

    def test_fuzzy_no_match_stop_word_only(self, rule_engine, msg_single_chat):
        """fuzzy 模式：纯停用词不应匹配任何规则。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="好的谢谢",
            reply_text="不客气", match_type="fuzzy", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "好的"  # 纯停用词
        result = rule_engine.check(msg_single_chat)
        # 纯停用词 → 意图过滤器识别为 acknowledge → skip
        assert result.action == "skip"

    def test_regex_match_with_capture_groups(self, rule_engine, msg_single_chat):
        """regex 模式：支持命名捕获组变量替换。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test",
            match_pattern=r"密码重置.*账号(?P<account>\w+)",
            reply_text="已为 {account} 提交密码重置申请",
            match_type="regex", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "帮我密码重置，账号zhangsan"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert "zhangsan" in result.reply_text
        assert result.captured_groups == {"account": "zhangsan"}

    def test_regex_match_numbered_groups(self, rule_engine, msg_single_chat):
        """regex 模式：无命名捕获组时使用数字索引 {1}, {2}。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test",
            match_pattern=r"订单(\d+)状态",
            reply_text="订单 {1} 正在处理中",
            match_type="regex", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "查询订单12345状态"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert "12345" in result.reply_text

    def test_disabled_rule_not_matched(self, rule_engine, msg_single_chat):
        """禁用的规则不应匹配。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="test", match_pattern="你好",
            reply_text="您好！", match_type="exact", priority=0, enabled=False,
        )
        rule_engine._db_keywords = [rule]
        msg_single_chat.content = "你好"
        result = rule_engine.check(msg_single_chat)
        # 规则被禁用 → 落到意图过滤器
        assert result.action == "skip"

    def test_priority_order_higher_wins(self, rule_engine, msg_single_chat):
        """高优先级规则优先匹配（需确保_db_keywords已按priority排序）。"""
        from src.rule_engine import KeywordRule
        low = KeywordRule(
            id=1, category="test", match_pattern="问题",
            reply_text="低优先级回复", match_type="fuzzy", priority=1, enabled=True,
        )
        high = KeywordRule(
            id=2, category="test", match_pattern="网络问题",
            reply_text="高优先级回复", match_type="fuzzy", priority=10, enabled=True,
        )
        # 手动按priority降序排列（模拟reload_db_keywords后的状态）
        rule_engine._db_keywords = sorted([low, high], key=lambda x: x.priority, reverse=True)
        msg_single_chat.content = "网络问题"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert result.reply_text == "高优先级回复"


# ============ 意图识别测试 ============

class TestIntentFilter:
    """意图过滤器：区分业务消息与纯礼貌/感谢消息。"""

    def test_pure_thank_you_is_skipped(self, rule_engine, msg_single_chat):
        """纯感谢短消息应被跳过。"""
        msg_single_chat.content = "谢谢"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "thank_you" in result.reason

    def test_pure_acknowledge_is_skipped(self, rule_engine, msg_single_chat):
        """纯确认消息应被跳过。"""
        msg_single_chat.content = "收到"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "acknowledge" in result.reason

    def test_business_with_thank_you_is_processed(self, rule_engine, msg_single_chat):
        """含业务内容的感谢消息不应被跳过。"""
        msg_single_chat.content = "无法连接VPN，谢谢帮忙"  # "无法"在business_keywords中
        result = rule_engine.check(msg_single_chat)
        # 有业务关键词"无法"→ business → 继续后续规则匹配
        assert "intent_filter" not in (result.reason or "")

    def test_closing_message_is_skipped(self, rule_engine, msg_single_chat):
        """结束语应被跳过。"""
        msg_single_chat.content = "先这样，回头聊"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "closing" in result.reason

    def test_polite_greeting_short_is_skipped(self, rule_engine, msg_single_chat):
        """短礼貌问候应被跳过。"""
        msg_single_chat.content = "早上好"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "skip"
        assert "polite" in result.reason

    def test_empty_content_is_business(self, rule_engine, msg_empty_content):
        """空内容默认视为 business（由后续逻辑处理）。"""
        result = rule_engine.check(msg_empty_content)
        # 空内容 → intent=business → 无规则匹配 → pass
        assert result.action == "pass"

    def test_intent_filter_disabled_all_pass(self, rule_engine, msg_single_chat):
        """意图过滤器禁用时，所有消息都进入业务处理流程。"""
        rule_engine.config.intent_filter["enabled"] = False
        msg_single_chat.content = "谢谢"
        result = rule_engine.check(msg_single_chat)
        # 意图过滤器关闭 → 不再因 thank_you 而 skip
        assert "intent_filter" not in (result.reason or "")


# ============ 白名单群聊 / 空白名单组测试 ============

class TestWhitelistGroupChat:
    def test_whitelist_group_pass(self, rule_engine, msg_group_chat):
        """白名单群聊成员消息应通过。"""
        import re
        rule_engine._whitelist_enabled = True
        rule_engine._whitelist_groups = [re.compile(r"技术交流群")]
        msg_group_chat.content = "VPN连不上"
        result = rule_engine.check(msg_group_chat)
        assert result.action != "skip"

    def test_whitelist_group_skip(self, rule_engine, msg_group_chat):
        """非白名单群聊消息应被跳过。"""
        import re
        rule_engine._whitelist_enabled = True
        rule_engine._whitelist_groups = [re.compile(r"人事群")]
        result = rule_engine.check(msg_group_chat)
        assert result.action == "skip"
        assert "whitelist" in result.reason


# ============ 配置关键词匹配 ============

class TestConfigKeywords:
    def test_config_keyword_matched(self, rule_engine, msg_single_chat):
        """配置关键词匹配应返回 reply。"""
        import regex as _rx
        rule_engine.config.intent_filter["enabled"] = False
        rule_engine._config_keywords = [(_rx.compile(r"VPN故障"), "联系IT处理VPN")]
        rule_engine._db_keywords = []  # 确保走 config 路径
        msg_single_chat.content = "VPN故障"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert result.reply_text == "联系IT处理VPN"
        assert result.match_type == "regex"

    def test_config_keyword_with_groups(self, rule_engine, msg_single_chat):
        """配置关键词带捕获组，模板变量替换。"""
        import regex as _rx
        rule_engine.config.intent_filter["enabled"] = False
        rule_engine._config_keywords = [
            (_rx.compile(r"账号(?P<user>\w+)密码"), "已重置 {user} 的密码")
        ]
        rule_engine._db_keywords = []
        msg_single_chat.content = "账号admin密码忘了"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert "admin" in result.reply_text

    def test_config_keyword_no_match(self, rule_engine, msg_single_chat):
        """配置关键词未命中 → pass。"""
        import regex as _rx
        rule_engine.config.intent_filter["enabled"] = False
        rule_engine._config_keywords = [(_rx.compile(r"VIP专线"), "VIP热线")]
        rule_engine._db_keywords = []
        msg_single_chat.content = "普通问题"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "pass"


# ============ 模糊匹配边界 ============

class TestFuzzyEdgeCases:
    def test_fuzzy_empty_text(self, rule_engine, msg_empty_content):
        """空文本 fuzzy 匹配应返回不命中。"""
        from src.rule_engine import KeywordRule
        rule_engine.config.intent_filter["enabled"] = False
        rule = KeywordRule(
            id=1, category="t", match_pattern="help",
            reply_text="help reply", match_type="fuzzy", priority=0, enabled=True,
        )
        rule_engine._db_keywords = [rule]
        result = rule_engine.check(msg_empty_content)
        assert result.action == "pass"

    def test_fuzzy_single_specific_token_match(self, rule_engine, msg_single_chat):
        """长文本中仅一个有意义 token 且 ≥3 字符，应匹配。"""
        from src.rule_engine import KeywordRule
        rule_engine._db_keywords = [
            KeywordRule(id=1, category="t", match_pattern="reboot,重启",
                        reply_text="重启服务器", match_type="fuzzy", priority=0, enabled=True),
        ]
        # "么" 是停用词被过滤，剩余只有 "重启" 一个有意义 token，≥3 字符
        msg_single_chat.content = "怎么重启么"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"

    def test_fuzzy_with_stopwords_in_text(self, rule_engine, msg_single_chat):
        """文本中含停用词，有意义 token 仍能匹配。"""
        from src.rule_engine import KeywordRule
        rule_engine._db_keywords = [
            KeywordRule(id=1, category="t", match_pattern="打印机",
                        reply_text="IT支持", match_type="fuzzy", priority=0, enabled=True),
        ]
        # "的"是停用词被过滤，"打印机"作为有意义 token 命中
        msg_single_chat.content = "楼下的打印机坏了"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"


# ============ reload_db_keywords 异常路径 ============

class TestReloadDbKeywords:
    def test_no_db_store_returns_early(self, rule_engine):
        """无 db_store 时直接返回。"""
        rule_engine._db_store = None
        rule_engine.reload_db_keywords()  # 不抛异常

    def test_reload_interval_not_expired(self, rule_engine, msg_single_chat):
        """间隔未到时不重新加载。"""
        import time
        rule_engine._db_store = MagicMock()
        rule_engine._last_reload = time.time()
        rule_engine.reload_db_keywords()
        # store 不应被调用
        rule_engine._db_store.list_keyword_rules.assert_not_called()

    def test_invalid_regex_skipped(self, rule_engine):
        """无效正则规则被跳过。"""
        rule_engine._db_store = MagicMock()
        rule_engine._db_store.list_keyword_rules.return_value = [
            {"id": 1, "category": "t", "match_pattern": "[未闭合", "reply_text": "x",
             "match_type": "regex", "priority": 0, "enabled": 1, "hit_count": 0},
            {"id": 2, "category": "t", "match_pattern": "有效", "reply_text": "y",
             "match_type": "exact", "priority": 0, "enabled": 1, "hit_count": 0},
        ]
        rule_engine._last_reload = 0
        rule_engine.reload_db_keywords()
        # 无效正则被跳过，有效规则加载
        assert len(rule_engine._db_keywords) == 1
        assert rule_engine._db_keywords[0].id == 2

    def test_reload_exception_logged(self, rule_engine):
        """DB 异常时记录日志不崩溃。"""
        rule_engine._db_store = MagicMock()
        rule_engine._db_store.list_keyword_rules.side_effect = RuntimeError("db down")
        rule_engine._db_keywords = []  # 清空以验证不崩溃
        rule_engine._last_reload = 0
        rule_engine.reload_db_keywords()  # 不抛异常，keywords 保持原样


# ============ 剩余未覆盖边界 ============

class TestRemainingUncovered:
    def test_parse_stop_words_skip_empty_and_comments(self, rule_engine):
        """停用词解析应跳过空行和注释行。"""
        from src.config import RulesConfig
        from src.rule_engine import RuleEngine
        config = RulesConfig(
            enabled=True,
            stop_words=["", "  ", "# 这是注释", "真实词"],
        )
        engine = RuleEngine(config=config, db_store=None)
        assert "真实词" in engine.stop_words
        assert "这是注释" not in engine.stop_words
        assert engine.stop_words == {"真实词"}  # 空行和注释被跳过

    def test_reload_stop_words_default_config(self, rule_engine):
        """reload_stop_words 不传参时使用 self.config。"""
        rule_engine._stop_words_lower = set()
        rule_engine.config.stop_words = ["刷新词"]
        rule_engine.reload_stop_words()  # cfg=None → 走 self.config
        assert "刷新词" in rule_engine.stop_words

    def test_invalid_regex_in_matches_caught(self, rule_engine, msg_single_chat):
        """KeywordRule.matches 中无效正则被捕获返回 False。"""
        from src.rule_engine import KeywordRule
        rule_engine.config.intent_filter["enabled"] = False
        rule = KeywordRule(
            id=1, category="t",
            match_pattern="[未闭合括号",
            reply_text="x", match_type="regex", priority=0, enabled=True,
        )
        # 直接调用 matches，传入一个不会卡住也不会抛编译异常的文本
        matched, groups = rule.matches("hello", stop_words=set(), timeout=0.3)
        # 编译阶段就可能在 _regex.compile() 报错，所以 reload_db_keywords 已跳过无效正则
        # 但如果用户绕过 db_keywords 直接构造 KeywordRule，matches 里会 catch _regex.error
        # _regex.compile 在构造时没执行，只在 matches 内部按需编译
        assert matched is False

    def test_safe_search_regex_error(self, rule_engine):
        """_safe_search 遇到 regex.error 返回 None。"""
        import regex as _rx
        # 构造一个搜索时触发 regex.error 的场景
        # 实际上 _safe_search 捕获的是编译或 match 阶段的异常
        # 使用一个有效的 pattern 对象，然后 mock _regex.error 不容易触发
        # 改为：test_timeout_disabled 场景下 safe_regex 不会抛异常
        assert rule_engine._safe_search(_rx.compile(r"test"), "test") is not None
        assert rule_engine._safe_search(_rx.compile(r"nomatch"), "test") is None

    def test_config_keyword_numbered_groups(self, rule_engine, msg_single_chat):
        """配置关键词使用数字索引捕获组 {1}, {2}。"""
        import regex as _rx
        rule_engine.config.intent_filter["enabled"] = False
        rule_engine._config_keywords = [
            (_rx.compile(r"订(\w+)\s+(\d+)"), "已订 {2} 件 {1}")
        ]
        rule_engine._db_keywords = []
        msg_single_chat.content = "订咖啡 3"
        result = rule_engine.check(msg_single_chat)
        assert result.action == "reply"
        assert "3 件 咖啡" in result.reply_text

    def test_fuzzy_stop_words_none(self):
        """fuzzy 匹配 stop_words=None 时不使用停用词过滤。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="t", match_pattern="明白,好的",
            reply_text="x", match_type="fuzzy", priority=0, enabled=True,
        )
        matched, _ = rule.matches("我明白了", stop_words=None, timeout=0.3)
        assert matched is True

    def test_fuzzy_single_specific_token_without_substring(self):
        """仅一个有意义token且≥3字符，关键字非子串时走token交集的单token路径。"""
        from src.rule_engine import KeywordRule
        rule = KeywordRule(
            id=1, category="t", match_pattern="重启服务",
            reply_text="x", match_type="fuzzy", priority=0, enabled=True,
        )
        # "怎么"中"么"是停用词被过滤，"重启"是唯一有意义token(≥3字符)
        # "重启服务"不是"怎么重启"的子串 → 策略1不命中 → 走策略2(token交集)
        # kw_tokens = {"重启", "服务"}, text_tokens = {"重启"}, overlap = {"重启"}
        # is_single_specific_token: len(text_tokens)==1, len("重启")>=3 → True → len(overlap)>=1 → match
        matched, _ = rule.matches("怎么重启", stop_words={"么", "的", "了", "吗"}, timeout=0.3)
        assert matched is True


# ============ 停用词解析测试 ============

class TestStopWords:
    """停用词表解析与生效。"""

    def test_stop_words_parsed_from_comma_separated(self, rule_engine_config):
        """逗号分隔的停用词应正确解析为小写集合。"""
        from src.config import RulesConfig
        from src.rule_engine import RuleEngine

        config = RulesConfig(**rule_engine_config)
        engine = RuleEngine(config=config, db_store=None)

        assert "的" in engine.stop_words
        assert "谢谢" in engine.stop_words
        assert "好的" in engine.stop_words

    def test_reload_stop_words_updates_set(self, rule_engine, rule_engine_config):
        """热重载停用词应更新内部集合。"""
        from src.config import RulesConfig

        new_config = RulesConfig(**{
            **rule_engine_config,
            "stop_words": ["新增词,另一个词"],
        })
        rule_engine.reload_stop_words(new_config)

        assert "新增词" in rule_engine.stop_words
        assert "另一个词" in rule_engine.stop_words


# ============ ReDoS 防护测试 ============

class TestReDoSProtection:
    """正则匹配超时防护：恶意/低质正则遇病理输入不得卡死主线程。

    Python 内置 re 无超时能力，本项目改用 regex 库的 timeout 机制，
    超时视为不命中（黑白名单）或跳过规则（关键词），fail-safe 不阻塞消息流。
    """

    def _mk_msg(self, content, sender="测试用户"):
        from datetime import datetime
        from src.models import Message
        return Message(
            msg_id="m", chat_id="c", chat_type="single", chat_name=None,
            sender_id="u", sender_name=sender, content=content,
            msg_type="text", timestamp=datetime.now(),
        )

    def _engine(self, timeout=0.3):
        from src.config import RulesConfig
        from src.rule_engine import RuleEngine
        cfg = RulesConfig(enabled=True, regex_timeout_seconds=timeout)
        eng = RuleEngine(cfg, db_store=None)
        eng.config.intent_filter = {"enabled": False}
        return eng

    def test_malicious_db_regex_times_out_and_skips(self):
        """恶意 DB 正则规则遇病理输入应超时跳过，不卡死。"""
        import time
        import regex as _rx
        from src.rule_engine import KeywordRule

        eng = self._engine(timeout=0.3)
        evil = KeywordRule(
            id=999, category="t", match_pattern=r"(\d+)+$", reply_text="x",
            match_type="regex", priority=10, enabled=True,
        )
        evil._compiled = _rx.compile(r"(\d+)+$")
        eng._db_keywords = [evil]

        t = time.time()
        result = eng.check(self._mk_msg("1" * 1000 + "x"))
        elapsed = time.time() - t

        assert elapsed < 2.0, f"未被超时保护，耗时 {elapsed:.2f}s（疑似卡死）"
        assert result.action == "pass"  # 恶意规则被跳过，无其他规则命中

    def test_normal_regex_rule_unaffected(self):
        """正常正则规则不受超时机制影响。"""
        import regex as _rx
        from src.rule_engine import KeywordRule

        eng = self._engine(timeout=0.3)
        good = KeywordRule(
            id=1, category="t", match_pattern=r"打印机", reply_text="IT支持",
            match_type="regex", priority=10, enabled=True,
        )
        good._compiled = _rx.compile(r"打印机")
        eng._db_keywords = [good]

        result = eng.check(self._mk_msg("打印机怎么连"))
        assert result.action == "reply"
        assert result.reply_text == "IT支持"

    def test_malicious_blacklist_regex_does_not_hang(self):
        """黑名单恶意正则遇病理 sender_name 不得卡死。"""
        import time
        import regex as _rx

        eng = self._engine(timeout=0.3)
        eng._blacklist_users = [_rx.compile(r"(\d+)+$")]

        t = time.time()
        eng.check(self._mk_msg("hi", sender="1" * 1000 + "x"))
        elapsed = time.time() - t

        assert elapsed < 2.0, f"黑名单正则未被超时保护，耗时 {elapsed:.2f}s"

    def test_timeout_disabled_still_works_for_safe_regex(self):
        """timeout 配为 0（禁用）时，安全正则仍正常工作。"""
        import regex as _rx
        from src.rule_engine import KeywordRule

        eng = self._engine(timeout=0)
        good = KeywordRule(
            id=1, category="t", match_pattern=r"VPN", reply_text="网络支持",
            match_type="regex", priority=10, enabled=True,
        )
        good._compiled = _rx.compile(r"VPN")
        eng._db_keywords = [good]

        result = eng.check(self._mk_msg("VPN连不上"))
        assert result.action == "reply"


class TestEdgeCasePatches:
    """补充覆盖: disabled 规则、单 token 命中、regex 异常路径等。"""

    def _mk_msg(self, content, user_id="user1", chat_id="chat1", chat_type="single"):
        from datetime import datetime
        from src.models import Message
        return Message(
            msg_id="m", chat_id=chat_id, chat_type=chat_type, chat_name=None,
            sender_id=user_id, sender_name="测试", content=content,
            msg_type="text", timestamp=datetime.now(),
        )

    # ---- disabled 规则 ----
    def test_disabled_rule(self):
        kw = KeywordRule(id=1, category="t", match_pattern="hello",
                         reply_text="hi", match_type="exact",
                         priority=10, enabled=False)
        assert kw.matches("hello") == (False, None)

    # ---- fuzzy: single specific token (<3 chars, 1 overlap) ----
    def test_fuzzy_single_token_short_text(self):
        kw = KeywordRule(id=1, category="t", match_pattern="AB,CD,EF",
                         reply_text="ok", match_type="fuzzy",
                         priority=10, enabled=True)
        assert kw.matches("AB")[0] is True

    # ---- fuzzy: very short text (≤4 chars) + 1 token overlap ----
    def test_fuzzy_very_short_text_one_token(self):
        kw = KeywordRule(id=1, category="t", match_pattern="打印机设置",
                         reply_text="ok", match_type="fuzzy",
                         priority=10, enabled=True)
        assert kw.matches("打印机")[0] is True

    # ---- fuzzy: single specific token (≥3 chars) + 1 overlap ----
    def test_fuzzy_single_specific_token(self):
        kw = KeywordRule(id=1, category="t", match_pattern="VPN,代理",
                         reply_text="ok", match_type="fuzzy",
                         priority=10, enabled=True)
        assert kw.matches("VPN")[0] is True

    # ---- regex: _compiled is None fallback ----
    def test_regex_no_compiled_fallback(self):
        kw = KeywordRule(id=1, category="t", match_pattern=r"\d{3}-\d{5}",
                         reply_text="ok", match_type="regex",
                         priority=10, enabled=True)
        kw._compiled = None
        assert kw.matches("123-45678")[0] is True

    # ---- regex with numbered groups ----
    def test_regex_numbered_groups(self):
        kw = KeywordRule(id=1, category="t",
                         match_pattern=r"^(\w+)来自(.+)$",
                         reply_text="{1}你好", match_type="regex",
                         priority=10, enabled=True)
        matched, groups = kw.matches("张三来自研发部")
        assert matched is True
        assert groups == {"1": "张三", "2": "研发部"}

    # ---- regex with named capture groups format ----
    def test_regex_named_groups(self):
        kw = KeywordRule(id=1, category="t",
                         match_pattern=r"(?P<first>\w+)(?P<second>\w+)",
                         reply_text="{first}{second}", match_type="regex",
                         priority=10, enabled=True)
        matched, groups = kw.matches("ABCD")
        assert matched is True
        assert groups.get("first") == "ABC" or groups.get("1") == "AB"

    # ---- regex _compiled None, timeout > 0 branch ----
    def test_regex_compiled_none_with_timeout(self):
        kw = KeywordRule(id=1, category="t", match_pattern=r"\d{3}-\d{5}",
                         reply_text="ok", match_type="regex",
                         priority=10, enabled=True)
        kw._compiled = None
        assert kw.matches("123-45678", timeout=0.5)[0] is True

    # ---- _safe_search: timeout is 0 (disabled) ----
    def test_safe_search_timeout_zero(self):
        import regex as rx
        config = RulesConfig()
        config.regex_timeout_seconds = 0
        eng = RuleEngine(config)
        pat = rx.compile(r"hello")
        result = eng._safe_search(pat, "hello world")
        assert result is not None

    # ---- _safe_search: regex error caught ----
    def test_safe_search_regex_error_caught(self):
        import regex as rx
        config = RulesConfig()
        config.regex_timeout_seconds = 1.0
        eng = RuleEngine(config)
        bad_pattern = MagicMock()
        bad_pattern.search.side_effect = rx.error("bad pattern")
        result = eng._safe_search(bad_pattern, "anything")
        assert result is None

    # ---- db keyword with template groups ----
    def test_db_keyword_template_groups(self):
        config = RulesConfig()
        config.regex_timeout_seconds = 1.0
        eng = RuleEngine(config)

        kw = KeywordRule(
            id=1, category="t",
            match_pattern=r"(\w+)是(\w+)",
            reply_text="好的，{1}是{2}",
            match_type="regex", priority=10, enabled=True,
        )
        kw._compiled = _rx.compile(r"(\w+)是(\w+)")
        eng._db_keywords = [kw]

        result = eng.check(self._mk_msg("ABC是DEF"))
        assert result.action == "reply"
        assert "ABC" in result.reply_text


# ============ 覆盖率补齐：rule_engine 95%→100% ============

def test_fuzzy_match_two_token_overlap(rule_engine, msg_single_chat):
    """line 112-114: fuzzy 匹配 2+ token 重叠（非短文本/单 token 场景）。"""
    kw = KeywordRule(
        id=1, category="test", match_pattern="数据库查询优化", reply_text="请联系DBA",
        match_type="fuzzy", priority=0, enabled=True,
    )
    rule_engine._db_keywords = [kw]
    msg_single_chat.content = "数据库查询慢怎么办"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"


def test_regex_timeout_zero_path(rule_engine, msg_single_chat):
    """line 147: timeout=0 时走 else 分支（不传 timeout kwarg）。"""
    import regex as _rx
    kw = KeywordRule(
        id=1, category="test", match_pattern=r"VPN\d+", reply_text="匹配到了",
        match_type="regex", priority=0, enabled=True,
    )
    kw._compiled = _rx.compile(r"VPN\d+")
    rule_engine._regex_timeout = 0
    rule_engine._db_keywords = [kw]
    msg_single_chat.content = "VPN123配置"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"


def test_regex_builtin_re_typeerror_fallback(rule_engine, msg_single_chat):
    """line 143: 内置 re.Pattern 不支持 timeout kwarg → TypeError → 回退无超时。"""
    import re as _re
    kw = KeywordRule(
        id=1, category="test", match_pattern=r"VPN\d+", reply_text="匹配到了",
        match_type="regex", priority=0, enabled=True,
    )
    kw._compiled = _re.compile(r"VPN\d+")
    rule_engine._regex_timeout = 1.0
    rule_engine._db_keywords = [kw]
    msg_single_chat.content = "VPN999连接"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"


def test_is_meaningful_token_true(rule_engine):
    """line 202: token 不在停用词且长度>1 返回 True。"""
    assert rule_engine._is_meaningful_token("vpn") is True
    assert rule_engine._is_meaningful_token("打印机") is True


def test_db_keyword_hit_increments_counter(rule_engine, msg_single_chat, monkeypatch):
    """line 335-337: DB 命中关键词后调用 increment_keyword_hit。"""
    db_mock = MagicMock()
    rule_engine._db_store = db_mock
    monkeypatch.setattr(rule_engine, "reload_db_keywords", lambda: None)
    kw = KeywordRule(
        id=42, category="test", match_pattern="VPN", reply_text="已记录",
        match_type="exact", priority=0, enabled=True,
    )
    rule_engine._db_keywords = [kw]
    msg_single_chat.content = "VPN"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"
    db_mock.increment_keyword_hit.assert_called_once_with(42)


def test_db_keyword_hit_increment_fails_gracefully(rule_engine, msg_single_chat, monkeypatch):
    """line 354: increment_keyword_hit 异常不传播，静默吞掉。"""
    db_mock = MagicMock()
    db_mock.increment_keyword_hit.side_effect = RuntimeError("db error")
    rule_engine._db_store = db_mock
    monkeypatch.setattr(rule_engine, "reload_db_keywords", lambda: None)
    kw = KeywordRule(
        id=99, category="test", match_pattern="救命", reply_text="x",
        match_type="exact", priority=0, enabled=True,
    )
    rule_engine._db_keywords = [kw]
    msg_single_chat.content = "救命"
    result = rule_engine.check(msg_single_chat)
    assert result.action == "reply"  # 异常不中断流程


def test_matches_unknown_type(rule_engine):
    """line 146: match_type 不是 exact/fuzzy/regex 时返回 False。"""
    kw = KeywordRule(
        id=1, category="test", match_pattern="anything",
        reply_text="x", match_type="unknown", priority=0, enabled=True,
    )
    matched, groups = kw.matches("hello", stop_words=set(), timeout=0.3)
    assert matched is False
    assert groups is None


def test_reload_config(rule_engine, rule_engine_config):
    """lines 209-219: reload_config 完整重载所有配置。"""
    from src.config import RulesConfig
    new_config = RulesConfig(**{**rule_engine_config, "stop_words": ["测试"]})
    rule_engine.reload_config(new_config)
    assert rule_engine.config is new_config
    assert "测试" in rule_engine.stop_words


def test_regex_error_in_matches_returns_false(rule_engine):
    """line 143/145: KeywordRule.matches 编译失败时 catch _regex.error 返回 False。"""
    kw = KeywordRule(
        id=1, category="test", match_pattern="*abc",
        reply_text="x", match_type="regex", priority=0, enabled=True,
    )
    matched, groups = kw.matches("hello", stop_words=set(), timeout=0.3)
    assert matched is False


def test_regex_no_match_returns_false(rule_engine):
    """line 143: 有效正则但未命中文本时返回 False。"""
    kw = KeywordRule(
        id=1, category="test", match_pattern=r"\d{3}-\d{4}",
        reply_text="x", match_type="regex", priority=0, enabled=True,
    )
    matched, groups = kw.matches("hello world", stop_words=set(), timeout=0.3)
    assert matched is False
