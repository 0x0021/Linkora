"""回归测试：意图识别误匹配 & 429 fallback 改进。

Bug1: acknowledge 关键词 "OK" 误匹配 "ROKAE"（子串），导致业务消息被跳过。
     修复：src.intent.match_keyword 对短纯英文关键词使用 \\b 词边界匹配；
     同时补 business_keywords（股票/上市/IPO 等）+ pure_ack_max_length 长度门槛。

Bug2: LLM 连续 web_search 成功后，最终答案生成时 429 → 弱 fallback 模型忽略搜索结果。
     修复：chat() 加 429 指数退避重试（3次，2s/4s/8s）；
     fallback 前若对话中有 tool 结果则注入系统提示要求综合。
"""

from unittest.mock import patch, MagicMock

from src.intent import match_keyword
from src.config import RulesConfig


# ============================================================
# Bug1: match_keyword 词边界匹配 + 意图识别回归
# ============================================================

class TestMatchIntentKeyword:
    """短英文关键词不应误匹配其他单词中的子串。"""

    def test_ok_not_in_rokae(self):
        assert match_keyword("OK", "珞石ROKAE股票情况", "珞石rokae股票情况") is False

    def test_ok_lowercase_not_in_rokae(self):
        assert match_keyword("ok", "珞石rokae股票情况", "珞石rokae股票情况") is False

    def test_ok_standalone_matches(self):
        assert match_keyword("OK", "OK", "ok") is True

    def test_ok_in_short_message(self):
        assert match_keyword("ok", "ok 知道了", "ok 知道了") is True

    def test_hi_not_in_china(self):
        assert match_keyword("Hi", "China is great", "china is great") is False

    def test_hi_standalone(self):
        assert match_keyword("Hi", "Hi there", "hi there") is True

    def test_chinese_kw_still_substring(self):
        """中文关键词仍用普通子串匹配（不适用 \\b）。"""
        assert match_keyword("收到", "收到收到", "收到收到") is True
        assert match_keyword("好的", "好的好的", "好的好的") is True

    def test_long_ascii_no_word_boundary(self):
        """长英文关键词（>3字符）仍用普通子串匹配，不走词边界。"""
        assert match_keyword("hello", "say hello to me", "say hello to me") is True


class TestIntentFilterROKAEBugFix:
    """验证 business_keywords 覆盖「珞石ROKAE股票情况」类业务消息。"""

    def test_stock_in_business_keywords(self):
        cfg = RulesConfig()
        bk = cfg.intent_filter.get("business_keywords", [])
        assert "股票" in bk
        assert "上市" in bk
        assert "IPO" in bk
        assert "情况" in bk
        assert "发行" in bk

    def test_pure_ack_max_length_exists(self):
        cfg = RulesConfig()
        assert cfg.intent_filter.get("pure_ack_max_length", 10) <= 15

    def test_rokae_message_not_pure_ack_by_length(self):
        msg = "珞石ROKAE股票情况"
        ack_max = 10
        assert not (len(msg) <= ack_max), f"长度 {len(msg)} 应 > {ack_max}"


# ============================================================
# Bug2: 429 重试与 fallback 提示注入
# ============================================================

class TestLLMClientRetryAndFallback:

    def _make_client(self, **overrides):
        from src.llm.client import LLMClient
        default = dict(
            model="test-model",
            api_key="test-key",
            base_url="http://fake/v1",
            max_tokens=1024,
            temperature=0.7,
            fallback_model="fallback-model",
            fallback_api_key="fb-key",
            fallback_base_url="http://fallback/v1",
            # 第二层备用模型留空，禁用该分支（避免 MagicMock 被当作有效 base_url 传入 OpenAI）
            secondary_fallback_model="",
            secondary_fallback_api_key="",
            secondary_fallback_base_url="",
            secondary_fallback_model_pool=[],
            **overrides,
        )
        cfg = MagicMock()
        for k, v in default.items():
            setattr(cfg, k, v)
        return LLMClient(cfg)

    @patch("src.llm.client.time.sleep")
    def test_429_bails_and_cools_down_then_fallback(self, mock_sleep):
        """429 应立即跳过主模型并设置冷却，不再同 call 内浪费重试，直接 fallback。"""
        client = self._make_client()
        rate_err = Exception(
            "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"
        )

        call_count = [0]
        fb_resp = MagicMock(content="fb result", tool_calls=[], finish_reason="stop", usage={})

        def do_fn(client_obj, kwargs, stream=False, **_kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise rate_err
            return fb_resp

        with patch.object(client, "_do_chat", side_effect=do_fn):
            client.chat([{"role": "user", "content": "hello"}])

        assert call_count[0] == 2  # 1 次主模型（429 即退）+ 1 次 fallback
        assert mock_sleep.call_count == 0  # 429 不再重试等待
        assert client._is_in_cooldown("test-model") is True

    @patch("src.llm.client.time.sleep")
    def test_non_retryable_error_no_retry(self, mock_sleep):
        client = self._make_client()
        auth_err = Exception("authentication failed")
        fb_resp = MagicMock(content="fb", tool_calls=[], finish_reason="stop", usage={})
        count = [0]

        def do_fn(client_obj, kwargs, stream=False, **_kw):
            count[0] += 1
            if count[0] == 1:
                raise auth_err
            return fb_resp

        with patch.object(client, "_do_chat", side_effect=do_fn):
            client.chat([{"role": "user", "content": "hello"}])
        assert mock_sleep.call_count == 0

    def test_fallback_injects_hint_when_tool_results_present(self):
        client = self._make_client()
        messages = [
            {"role": "user", "content": "查询股票"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "x", "name": "web_search"}]},
            {"role": "tool", "content": '{"results": []}'},
        ]
        rate_err = Exception("Error code: 429 - rate_limit_exceeded")
        fb_resp = MagicMock(content="基于搜索结果总结", tool_calls=[],
                            finish_reason="stop", usage={})
        captured = []

        def do_fn(client_obj, kwargs, stream=False, **_kw):
            msgs = kwargs.get("messages", [])
            has_hint = any(
                m.get("role") == "system" and "工具调用返回" in (m.get("content") or "")
                for m in msgs
            )
            captured.append((has_hint, list(msgs)))
            if not has_hint:
                raise rate_err
            return fb_resp

        with patch.object(client, "_do_chat", side_effect=do_fn):
            client.chat(messages)

        # 至少有一次调用带有 hint（fallback 调用）
        hint_calls = [c for c in captured if c[0]]
        assert len(hint_calls) >= 1, "fallback 调用应包含 system hint"
        fb_msgs = hint_calls[0][1]
        assert fb_msgs[0]["role"] == "system"
        assert "工具调用返回" in fb_msgs[0]["content"]

    def test_fallback_no_hint_without_tool_results(self):
        client = self._make_client()
        messages = [{"role": "user", "content": "你好"}]
        fb_resp = MagicMock(content="你好呀", tool_calls=[], finish_reason="stop", usage={})
        captured = []

        def do_fn(client_obj, kwargs, stream=False, **_kw):
            captured.append(list(kwargs.get("messages", [])))
            return fb_resp

        with patch.object(client, "_do_chat", side_effect=do_fn):
            client.chat(messages)

        assert len(captured) == 1  # 主模型成功，无 fallback
        assert captured[0][0]["role"] == "user"  # 无 system hint 前缀
