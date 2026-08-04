"""LLM JSON 输出稳健解析 + 还原度回测评委打分的回归测试。

背景：2026-08-01 线上「还原度回测」按钮报错，根因是 `_judge_clone` 用裸
`json.loads` 解析评委输出，模型一旦用 markdown 围栏/寒暄前缀包裹 JSON 就抛
`JSONDecodeError: Expecting value: line 1 column 1 (char 0)`，日志刷满 traceback，
且降级正则会把围栏原文当作 reason 展示。本测试锁死修复后的行为。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.llm_json import extract_json, extract_last_json, strip_reasoning
from web.routers.persona import _parse_judge_output


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_markdown_fenced(self):
        self.assertEqual(extract_json('```json\n{"score": 80}\n```'), {"score": 80})

    def test_fenced_without_lang(self):
        self.assertEqual(extract_json('```\n{"score": 80}\n```'), {"score": 80})

    def test_prefix_and_suffix_chatter(self):
        text = '好的，以下是评分结果：\n{"score": 72, "reason": "偏正式"}\n希望对你有帮助。'
        self.assertEqual(extract_json(text), {"score": 72, "reason": "偏正式"})

    def test_think_tag_stripped(self):
        text = '<think>先比较句长再打分</think>{"score": 55}'
        self.assertEqual(extract_json(text), {"score": 55})

    def test_unclosed_think_tag_truncates(self):
        # 被 max_tokens 截断：思考标签未闭合，标签后的内容一律丢弃
        self.assertIsNone(extract_json('<think>还在想'))

    def test_array_supported(self):
        self.assertEqual(extract_json('```json\n[1, 2]\n```'), [1, 2])

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_json("完全不是 json 的一堆文字"))
        self.assertIsNone(extract_json(""))
        self.assertIsNone(extract_json(None))

    def test_strip_reasoning(self):
        self.assertEqual(strip_reasoning("<thinking>x</thinking> hi"), "hi")
        self.assertEqual(strip_reasoning(""), "")


class TestExtractLastJson(unittest.TestCase):
    """extract_last_json：取最后一个合法 JSON（CLI stdout 噪声场景）。"""

    def test_trailing_noise_picks_last(self):
        """stdout 末尾有非 JSON 内容时，取最后一个合法 JSON 对象。"""
        text = '{"a": 1}\nlog line\n{"b": 2}\ntrailing noise'
        self.assertEqual(extract_last_json(text), {"b": 2})

    def test_progress_noise_then_json(self):
        """进度条/安装提示在前，JSON 响应在后。"""
        text = "progress...\n{\"result\": 42}"
        self.assertEqual(extract_last_json(text), {"result": 42})

    def test_markdown_fenced_last(self):
        text = '```json\n{"x": 1}\n```\n之后还有 {"y": 2}'
        self.assertEqual(extract_last_json(text), {"y": 2})

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_last_json("完全不是 json"))
        self.assertIsNone(extract_last_json(""))
        self.assertIsNone(extract_last_json(None))

    def test_array_as_last(self):
        self.assertEqual(extract_last_json('先有 {"a": 1}\n然后是 [3, 4]'), [3, 4])


class TestParseJudgeOutput(unittest.TestCase):
    def test_clean_json(self):
        score, reason = _parse_judge_output('{"score": 88, "reason": "口吻一致"}')
        self.assertEqual(score, 88)
        self.assertEqual(reason, "口吻一致")

    def test_fenced_json_no_longer_leaks_raw_text(self):
        """修复前：围栏导致 json.loads 抛错，reason 退化为 '```json...' 原文。"""
        score, reason = _parse_judge_output('```json\n{"score": 65, "reason": "更正式"}\n```')
        self.assertEqual(score, 65)
        self.assertEqual(reason, "更正式")

    def test_leading_newline(self):
        score, _ = _parse_judge_output('\n{"score": 65, "reason": "x"}')
        self.assertEqual(score, 65)

    def test_float_score_coerced(self):
        score, _ = _parse_judge_output('{"score": 72.5, "reason": "x"}')
        self.assertEqual(score, 72)

    def test_truncated_json_regex_fallback(self):
        """JSON 被截断：仍能从 "score": NN 救回分数，而不是整条丢弃。"""
        score, reason = _parse_judge_output('{"score": 40, "reason": "语气差异较')
        self.assertEqual(score, 40)
        self.assertTrue(reason)

    def test_prose_answer_fallback(self):
        score, _ = _parse_judge_output("我给这次克隆打 75 分，因为语气偏正式。")
        self.assertEqual(score, 75)

    def test_out_of_range_rejected(self):
        self.assertEqual(_parse_judge_output('{"score": 900}')[0], None)

    def test_unparsable_returns_none(self):
        self.assertEqual(_parse_judge_output("评委罢工了，没有任何数字")[0], None)

    def test_no_traceback_on_bad_input(self):
        """降级路径必须静默返回，绝不向上抛异常（否则整轮回测 500）。"""
        for bad in ["", "```", "{", "null", "<think>", "score: 无"]:
            try:
                _parse_judge_output(bad)
            except Exception as e:  # pragma: no cover - 失败即回归
                self.fail(f"_parse_judge_output({bad!r}) 抛异常: {e}")


if __name__ == "__main__":
    unittest.main()
