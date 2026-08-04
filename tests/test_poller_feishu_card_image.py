"""飞书消息卡片（interactive）图片标签解析 + 内容清洗测试。

新策略（2026-07-29 复盘）：飞书第三方 bot（智能助手、飞行社等）发的图
**不再**用「跨 app 不可下载」的占位符方案——实测 ``+messages-resources-download``
用 user 身份就能下到本租户内任意消息的资源，image_key 是下载的唯一信号，
**不能丢**。本测试覆盖：

- ``_extract_content`` 保留 ``🖼️ Image(img_key:...)`` / ``✨(img_key:...)`` 标签原样
  （让 ``_download_card_images`` 能扫到 image_key 走下载通道）
- ``_extract_content`` 仍清洗 ``<clickable>...</clickable>`` 容器（保留内部文本、
  丢弃 url 与标签），避免脏 HTML 进 LLM/历史
- ``_extract_card_image_keys`` 从 content 提取所有 image_key（含 emoji 变体）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.poller_core_parse import ParseMixin
from src.poller_core_ocr import OcrMixin


def _mixin():
    # ParseMixin 无 __init__，纯方法集合；_extract_content 不依赖 config。
    return ParseMixin()


def test_extract_content_preserves_image_key_for_download():
    """新策略：保留 🖼️ Image(img_key:...) 标签原样，下载通道靠它拿 image_key。"""
    m = _mixin()
    raw = {"content": (
        '<card title="👏真懂工作、主动帮忙：你的 aily 智能伙伴已上线">\n'
        '<clickable>\n'
        '🖼️ Image(img_key:img_v3_0212a_f084d7c9-ee82-40f8-a842-b7ccb2942d0g)\n'
        '</clickable>\n'
        '👇🏻  **选择一个场景：**\n'
        '🖼️ Image(img_key:img_v3_0212a_178f8694-bf14-4fc4-97c4-f53c7f0d8f3g)\n'
        '[生成工作复盘](https://applink.feishu.cn/client/web_url/open)\n'
        '</card>'
    )}
    out = m._extract_content(raw)
    # image_key 标签必须保留（下载通道依赖它）
    assert "Image(img_key:img_v3_0212a_f084d7c9" in out, f"image_key 标签被误清洗: {out!r}"
    assert "Image(img_key:img_v3_0212a_178f8694" in out, f"第二张图 image_key 被误清洗: {out!r}"
    # clickable 容器标签应解包（保留内部文字、丢弃 url/标签）
    assert "<clickable" not in out, f"clickable 标签未移除: {out!r}"
    assert "</clickable>" not in out, f"clickable 结束标签未移除: {out!r}"
    # 卡片内文字保留
    assert "生成工作复盘" in out


def test_extract_content_preserves_emoji_paren_img_key():
    """✨(img_key:...) / 无前缀(img_key:...) 等变体也要保留原样。"""
    m = _mixin()
    cases = [
        "✨(img_key:img_v3_abc)",
        "📷(file_key:xl_xxx)",
        "🎬(IMG_KEY:img_v3_def)",
        "无前缀(img_key:v)",
        "前面文字 ✨(img_key:v) 后面文字",
    ]
    for raw_text in cases:
        out = m._extract_content({"content": raw_text})
        # 关键：保留 (img_key|file_key|IMG_KEY:...) 整段
        assert "(img_key:" in out or "(file_key:" in out or "(IMG_KEY:" in out, \
            f"未保留 img_key 标签: {raw_text!r} → {out!r}"


def test_extract_content_does_not_touch_plain_english_image_word():
    """普通英文 Image(processing) 不应被任何正则误伤。"""
    m = _mixin()
    out = m._extract_content({"content": "普通文本，没有图片标签，Image(processing) 这种英文不应被误伤"})
    assert "Image(processing)" in out
    # 也不应被替换为 [图片]——新策略直接不动它


def test_extract_content_clickable_with_url_attribute_keeps_inner_text():
    """带 url 的 <clickable> 解包：保留内部文字、丢弃 url 与标签。"""
    m = _mixin()
    raw = {"content": (
        '<card title="飞书新手村">\n'
        '<clickable url="https://applink.feishu.cn/client/web_url/open?url=foo">\n'
        '🖼️ 前往飞书直播大讲堂课程 ✨(img_key:img_v3_02124_6550603a-f9a7-4802-9bcc-30f5f3454b0g)\n'
        '</clickable>\n'
        '欢迎来到飞行社 👋\n'
        '</card>'
    )}
    out = m._extract_content(raw)
    # 标签与 url 属性都不能泄出
    assert "<clickable" not in out
    assert "</clickable>" not in out
    assert 'applink.feishu.cn' not in out
    # 内部文字保留（含 image_key 整段）
    assert "前往飞书直播大讲堂课程" in out
    assert "(img_key:img_v3_02124_6550603a" in out
    # 卡片内其他文字保留
    assert "欢迎来到飞行社" in out


def test_extract_content_feishu_flight_club_real_message():
    """飞行社欢迎消息（DB id=97）实际格式：保留 image_key、清洗 clickable。"""
    m = _mixin()
    raw = {"content": (
        '<card title="飞书新手村通关之旅，开启高效办公新体验！">\n'
        '<clickable url="https://applink.feishu.cn/client/web_url/open?url=foo">\n'
        '🖼️ 前往飞书直播大讲堂课程 ✨(img_key:img_v3_02124_6550603a-f9a7-4802-9bcc-30f5f3454b0g)\n'
        '</clickable>\n'
        'Hi @OWNER(ou_0d149f48d41399f787062b50f6eada38)，欢迎来到飞书官方社区「飞行社」👋\n'
        '---\n'
        '**限时免费福利 - 飞书直播大讲堂课程**\n'
        '[立即报名](https://applink.feishu.cn/...)\n'
        '</card>'
    )}
    out = m._extract_content(raw)
    assert "<clickable" not in out
    assert "(img_key:img_v3_02124_6550603a" in out  # 保留
    # 关键文本保留
    assert "前往飞书直播大讲堂课程" in out
    assert "限时免费福利" in out
    assert "立即报名" in out
    assert "飞行社" in out


# ========== OcrMixin._extract_card_image_keys 覆盖 ==========

def test_extract_card_image_keys_dedupe_preserves_order():
    """多个图按出现顺序去重。"""
    keys = OcrMixin._extract_card_image_keys(
        '<card><clickable>🖼️ Image(img_key:img_v3_aaa)</clickable>'
        '\n🖼️ Image(img_key:img_v3_bbb)'
        '\n✨(img_key:img_v3_aaa)'  # 重复
        '\n📷(file_key:file_xxx)'
        '</card>'
    )
    assert keys == ["img_v3_aaa", "img_v3_bbb", "file_xxx"], f"提取顺序/去重错: {keys}"


def test_extract_card_image_keys_handles_uppercase_underscore():
    """大写 IMG_KEY / file_key 都能识别。"""
    keys = OcrMixin._extract_card_image_keys("Image(IMG_KEY:img_v3_xxx) ✨(img_key:img_v3_yyy) (file_key:f_zzz)")
    assert "img_v3_xxx" in keys
    assert "img_v3_yyy" in keys
    assert "f_zzz" in keys


def test_extract_card_image_keys_empty():
    """空 content / 无图返回空列表。"""
    assert OcrMixin._extract_card_image_keys("") == []
    assert OcrMixin._extract_card_image_keys(None) == []
    assert OcrMixin._extract_card_image_keys("普通文本无图") == []
