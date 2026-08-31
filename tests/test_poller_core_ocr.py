"""poller_core_ocr.OcrMixin 单元测试。

覆盖: _extract_media_id 钉钉/飞书格式 + 边界条件、_download_received_file 文件名提取、
_resolve_image_content 的 caption 兜底噪声剥离。
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


# ============ _resolve_image_content 的 caption 兜底 ============

_DINGTALK_NOISE = (
    "[图片消息](mediaId=$iwEeAqNwbmcDAQTRCfAF0QT0BrAZFtXEonlPowpftZn8TKUAB9J14R2) "
    "注意：如需下载使用dws chat message download-media命令下载"
)


class FakeResolve(OcrMixin):
    """_resolve_image_content 的最小 fake。

    _extract_image_caption 强制返回空，以触发 fallback 兜底分支；
    _image_storage 直接落一个非空假文件，绕过真实下载。
    """

    def __init__(self, ocr_text="深信服桌面云 桌面分配失败"):
        self._ocr_out = ocr_text
        self.dws = MagicMock()

    def _extract_image_caption(self, raw):  # 模拟 caption 未命中
        return ""

    def _image_storage(self, chat_id, filename):
        p = Path(tempfile.gettempdir()) / "linkora_ocr_t" / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        return p, f"rel/{filename}"

    def _ocr_image(self, path):
        return self._ocr_out


class TestCaptionFallbackNoise:
    """2026-08-31 事故回归：DWS 的工具元信息不得被当成用户随图文字。

    钉钉实际格式是 `(mediaId=`，旧兜底的 split 正则只认 `[?&]mediaId=` 而匹配不上
    左圆括号，导致整串 mediaId + 「注意：如需下载…」被当成 caption，连同 base64 噪声
    与 CLI 指令一起进入 LLM 上下文与消息记录页。
    """

    def _resolve(self, fallback: str, ocr_text="深信服桌面云 桌面分配失败"):
        fake = FakeResolve(ocr_text)
        raw = {
            "content": _DINGTALK_NOISE if "mediaId=" in fallback else fallback,
            "openMessageId": "msgAMCnvS8NcOnMQWiKN",
        }
        return fake._resolve_image_content(raw, "chat1", "群", fallback)

    def test_noise_not_treated_as_caption(self):
        content, _ = self._resolve(_DINGTALK_NOISE)
        assert "mediaId" not in content
        assert "如需下载" not in content
        assert "dws" not in content.lower()

    def test_ocr_text_still_present(self):
        content, _ = self._resolve(_DINGTALK_NOISE)
        assert "桌面分配失败" in content
        assert '<card title="图片内容">' in content

    def test_real_caption_after_noise_is_kept(self):
        """用户真写了随图文字（噪声之后）时，应只保留那部分。"""
        fb = _DINGTALK_NOISE + " 帮我看下这个报错"
        content, _ = self._resolve(fb)
        assert "帮我看下这个报错" in content
        assert "mediaId" not in content
        assert "如需下载" not in content

    def test_real_caption_before_noise_is_kept(self):
        """用户真写了随图文字（噪声之前）时，同样应保留。"""
        fb = "这个报错咋解决 " + _DINGTALK_NOISE
        content, _ = self._resolve(fb)
        assert "这个报错咋解决" in content
        assert "mediaId" not in content

    def test_noise_only_yields_no_caption(self):
        """纯噪声时不应留下任何 caption 残渣（卡片前面不能顶着空行或符号）。"""
        content, _ = self._resolve(_DINGTALK_NOISE)
        assert content.startswith('<card title="图片内容">')

    def test_file_id_noise_stripped(self):
        """文件类消息的 fileId + dws 下载指令同样要剥离。"""
        fb = "[文件] 火绒误杀explorer黑屏恢复方案.md fileId: bva6QBXJwROv " \
             "注意：如需下载使用dws drive download命令下载"
        content, _ = self._resolve(fb, ocr_text="explorer 已恢复")
        # 文件名是有意义的信息，可以留；但 fileId 与下载指令必须消失
        assert "fileId" not in content
        assert "如需下载" not in content
