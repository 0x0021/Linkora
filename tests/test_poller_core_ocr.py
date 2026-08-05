"""poller_core_ocr.OcrMixin 单元测试。

覆盖: _extract_media_id 钉钉/飞书格式 + 边界条件。
"""



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
