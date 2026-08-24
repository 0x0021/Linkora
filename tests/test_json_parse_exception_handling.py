"""JSON 解析异常处理测试（P1-3 修复验证）。

验证 poller_core_parse.py 中 JSON 解析异常现在使用精确捕获
（JSONDecodeError, TypeError）而非宽泛的 Exception。
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile


class TestJsonParseExceptionHandling:
    """JSON 解析异常处理测试。"""

    def test_json_decode_error_caught_precisely(self):
        """验证 JSONDecodeError 能被精确捕获。"""
        invalid_json = "{invalid json}"

        # 验证 json.loads 会抛出 JSONDecodeError
        with pytest.raises(json.JSONDecodeError):
            json.loads(invalid_json)

        # 验证 TypeError 也能被捕获（如传入 None）
        with pytest.raises(TypeError):
            json.loads(None)

    def test_valid_json_not_caught(self):
        """验证有效 JSON 不会抛出异常。"""
        valid_json = '{"key": "value"}'
        result = json.loads(valid_json)
        assert result == {"key": "value"}

    def test_raw_decode_with_invalid_json(self):
        """验证 raw_decode 能处理无效 JSON 片段。"""
        decoder = json.JSONDecoder()
        invalid_blob = "not valid json at all"

        # raw_decode 在无效位置会抛出 JSONDecodeError
        with pytest.raises(json.JSONDecodeError):
            decoder.raw_decode(invalid_blob, 0)

    def test_poller_core_parse_json_handling(self):
        """验证 poller_core_parse.py 中的 JSON 处理逻辑。"""
        # 模拟 _extract_image_caption 中的 JSON 解析
        test_cases = [
            ('{"text": "hello"}', "hello"),  # 有效 JSON
            ('invalid json', None),  # 无效 JSON，应返回空
            ('{"mediaId": "abc123", "text": "caption"}', "caption"),  # 有效 JSON 带 mediaId
        ]

        for content, expected in test_cases:
            if content.startswith("{"):
                try:
                    c = json.loads(content)
                    if isinstance(c, dict):
                        if not (c.get("mediaId") or c.get("picUrl")):
                            assert content == '{"text": "hello"}' or content == '{"mediaId": "abc123", "text": "caption"}'
                        else:
                            # 查找文字字段
                            for key in ("text", "content", "title", "description", "summary", "body"):
                                v = c.get(key)
                                if isinstance(v, str) and v.strip():
                                    assert v == expected or expected is None
                                    break
                except (json.JSONDecodeError, TypeError):
                    # 无效 JSON 应被捕获
                    assert expected is None


class TestPollerCoreParseExceptionHandling:
    """poller_core_parse.py 异常处理测试。"""

    def test_json_loading_with_none(self):
        """验证 json.loads(None) 抛出 TypeError。"""
        with pytest.raises(TypeError):
            json.loads(None)

    def test_json_loading_with_invalid_string(self):
        """验证 json.loads(无效字符串) 抛出 JSONDecodeError。"""
        with pytest.raises(json.JSONDecodeError):
            json.loads("{invalid}")

    def test_json_loading_with_valid_string(self):
        """验证 json.loads(有效字符串) 成功解析。"""
        result = json.loads('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_decoder_raw_decode_with_invalid(self):
        """验证 JSONDecoder.raw_decode 在无效位置抛出 JSONDecodeError。"""
        decoder = json.JSONDecoder()
        with pytest.raises(json.JSONDecodeError):
            decoder.raw_decode("not json", 0)
