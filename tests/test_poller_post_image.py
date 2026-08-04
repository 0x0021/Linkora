"""飞书图片/图文混排消息解析测试。

覆盖此前 bug：飞书「图文混排」消息（msg_type=post，图+文字在一个气泡）原本被
_detect_msg_type 原样归为 "post"，既不走图片下载/OCR 分支、又把嵌套 JSON 泄进
历史与 LLM。修复后含图归 image、纯文字归 text，且能从 post 嵌套结构拿到 image_key。
"""
import sys
from types import SimpleNamespace

import pytest

from src.poller_core_parse import ParseMixin
from src.poller_core_ocr import OcrMixin

# 最小桩对象：仅需 _is_system_sender，post 消息不会触发清洗分支里的 _extract_rich_text
FAKE = SimpleNamespace(_is_system_sender=lambda sender: False)


def _msg(msg_type, content, *, sender="张三", sender_id="ou_test"):
    return {"msgType": msg_type, "sender": sender, "senderId": sender_id, "content": content}


# --------------------------------------------------------------------------- #
# _detect_msg_type
# --------------------------------------------------------------------------- #
def test_image_standalone_detected():
    raw = _msg("image", '{"image_key": "img_v3_aaaa"}')
    assert ParseMixin._detect_msg_type(FAKE, raw) == "image"


def test_post_with_image_detected_as_image():
    raw = _msg("post", '{"title":"","content":[[{"tag":"text","text":"帮我看下报错"},'
                       '{"tag":"img","image_key":"img_v3_bbbb"}]]}')
    assert ParseMixin._detect_msg_type(FAKE, raw) == "image"


def test_post_text_only_detected_as_text():
    raw = _msg("post", '{"title":"","content":[[{"tag":"text","text":"这是纯文字说明"}]]}')
    assert ParseMixin._detect_msg_type(FAKE, raw) == "text"


def test_post_image_only_detected_as_image():
    raw = _msg("post", '{"title":"","content":[[{"tag":"img","image_key":"img_v3_dddd"}]]}')
    assert ParseMixin._detect_msg_type(FAKE, raw) == "image"


# --------------------------------------------------------------------------- #
# _extract_media_id（OcrMixin 静态方法）
# --------------------------------------------------------------------------- #
def test_extract_media_id_standalone_image_key():
    assert OcrMixin._extract_media_id('{"image_key": "img_v3_aaaa"}') == "img_v3_aaaa"


def test_extract_media_id_from_post_nested():
    content = '{"title":"","content":[[{"tag":"text","text":"x"},' \
              '{"tag":"img","image_key":"img_v3_bbbb"}]]}'
    assert OcrMixin._extract_media_id(content) == "img_v3_bbbb"


def test_extract_media_id_post_text_only_is_none():
    content = '{"title":"","content":[[{"tag":"text","text":"纯文字"}]]}'
    assert OcrMixin._extract_media_id(content) is None


def test_extract_media_id_dingtalk_mediaid_unaffected():
    # 钉钉 mediaId= 查询串仍走正则分支，不受 post 递归影响
    assert OcrMixin._extract_media_id("mediaId=@lALOxxxx") == "@lALOxxxx"


def test_extract_media_id_dingtalk_stops_at_ampersand():
    # 钉钉图片 content 形如 mediaId=@lALOxxxx&text=说明，& 之后是随图文字说明，
    # 不应并入 media_id（修复前会吞成 "@lALOxxxx&text=说明"，导致下载失败）
    assert OcrMixin._extract_media_id("mediaId=@lALOxxxx&text=说明") == "@lALOxxxx"


# --------------------------------------------------------------------------- #
# _extract_content
# --------------------------------------------------------------------------- #
def test_extract_content_post_with_image():
    raw = _msg("post", '{"title":"","content":[[{"tag":"text","text":"帮我看下报错"},'
                       '{"tag":"img","image_key":"img_v3_bbbb"}]]}')
    assert ParseMixin._extract_content(FAKE, raw) == "帮我看下报错 [图片]"


def test_extract_content_post_text_only():
    raw = _msg("post", '{"title":"","content":[[{"tag":"text","text":"这是纯文字说明"}]]}')
    assert ParseMixin._extract_content(FAKE, raw) == "这是纯文字说明"


def test_extract_content_post_image_only():
    raw = _msg("post", '{"title":"","content":[[{"tag":"img","image_key":"img_v3_dddd"}]]}')
    assert ParseMixin._extract_content(FAKE, raw) == "[图片]"


def test_extract_content_standalone_image_is_clean():
    # 飞书单独发的图不应再泄漏原始 JSON
    raw = _msg("image", '{"image_key": "img_v3_aaaa"}')
    assert ParseMixin._extract_content(FAKE, raw) == "[图片]"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
