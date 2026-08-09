"""poller_core_ocr.OcrMixin 单元测试。

覆盖: _extract_media_id 钉钉/飞书格式 + 边界条件、_download_received_file 文件名提取。
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.poller_core_ocr import OcrMixin


class FakeOcr(OcrMixin):
    """最小 fake。_extract_media_id 是静态方法，无需额外属性。"""
    pass


# ============ _extract_media_id ============

class TestExtractMediaId:
    def test_dingtalk_media_id_format(self):
        result = OcrMixin._extract_media_id("mediaId=abc123def")
        assert result == "abc123def"

    def test_dingtalk_media_id_in_query_string(self):
        # 注意: 实际正则捕获到下一个非字母数字字符或 & 前的位置为止
        result = OcrMixin._extract_media_id("some_prefix mediaId=xyz789&other")
        # 验证返回的 id 以 xyz789 开头
        assert result is not None
        assert result.startswith("xyz789")

    def test_feishu_image_key(self):
        result = OcrMixin._extract_media_id('{"image_key": "img_abc123"}')
        assert result == "img_abc123"

    def test_feishu_image_key_no_match(self):
        result = OcrMixin._extract_media_id('{"other_field": "value"}')
        assert result is None

    def test_empty_content(self):
        assert OcrMixin._extract_media_id("") is None

    def test_none_content(self):
        assert OcrMixin._extract_media_id(None) is None

    def test_invalid_json(self):
        result = OcrMixin._extract_media_id("{not valid json}")
        assert result is None

    def test_no_media_id(self):
        result = OcrMixin._extract_media_id("hello world")
        assert result is None


# ============ _download_received_file 文件名提取 ============

class TestReceivedFileName:
    """P0-2026-08-09：纯文本形态的 `fileName=` 须被提取，否则视频落盘成
    `video_<mediaId>.mp4`，丢掉真实文件名，影响「把刚才那个视频转发给 XX」。"""

    def _run(self, raw_content, media_type="video"):
        """跑 _download_received_file 并截获最终落盘的 safe_name。"""
        captured = {}

        class FP(OcrMixin):
            def __init__(self):
                self.dws = MagicMock()

            def _file_storage(self, chat_id, safe_name):
                captured["safe_name"] = safe_name
                return Path(tempfile.gettempdir()) / "linkora_t" / safe_name, safe_name

        fp = FP()
        fp._download_received_file(
            {"content": raw_content}, "chat1", "群", "msg1", media_type)
        return captured.get("safe_name")

    def test_plain_text_filename_extracted(self):
        name = self._run(
            "[视频消息](mediaId=@lQbPJwotjO5Eob8AALCBaiJf1GTSGQpKu120vFkA) "
            "fileName=mmexport1786244232175.mp4 url: @l")
        assert name == "mmexport1786244232175.mp4"

    def test_json_filename_still_preferred(self):
        name = self._run(
            '{"mediaId": "abc123", "fileName": "季度报告.pdf"}', media_type="file")
        assert name.endswith(".pdf")

    def test_fallback_default_name_when_absent(self):
        name = self._run("[视频消息](mediaId=@lQbXYZ)")
        assert name.startswith("video_") and name.endswith(".mp4")

    def test_path_traversal_stripped(self):
        name = self._run("mediaId=abc fileName=../../etc/passwd", media_type="file")
        assert ".." not in name and "/" not in name
