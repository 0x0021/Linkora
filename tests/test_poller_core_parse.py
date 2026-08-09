"""poller_core_parse.ParseMixin 单元测试。

覆盖: _parse_timestamp, _effective_skip_types, _detect_msg_type, _extract_rich_text。
"""

from datetime import datetime
from unittest.mock import MagicMock

import json

from src.poller_core_parse import ParseMixin


class FakeParse(ParseMixin):
    """最小 fake，提供 parse mixin 所需的属性。"""

    def __init__(self):
        self.config = MagicMock()
        self.config.skip_msg_types = set()
        self.config.image_ocr_enabled = False
        self.config.graceful_fallback_msg_types = set()

    def _is_system_sender(self, sender_name: str) -> bool:
        """_detect_msg_type 依赖 AccessControlMixin._is_system_sender。
        这里简化：空字符串返回 False，非空时不在系统关键词中返回 False。"""
        if not sender_name:
            return False
        return sender_name in ("系统", "群助手", "OA审批", "智能人事")


# ============ _parse_timestamp ============

class TestParseTimestamp:
    def setup_method(self):
        self.fp = FakeParse()

    def test_dingtalk_format(self):
        dt = self.fp._parse_timestamp("2026-07-26 14:30:00")
        assert dt == datetime(2026, 7, 26, 14, 30, 0)
        assert dt.tzinfo is None

    def test_iso_naive(self):
        dt = self.fp._parse_timestamp("2026-07-26T14:30:00")
        assert dt == datetime(2026, 7, 26, 14, 30, 0)
        assert dt.tzinfo is None

    def test_iso_with_z_utc(self):
        dt = self.fp._parse_timestamp("2026-07-26T06:00:00Z")
        assert dt.tzinfo is None  # 转成本地 naive
        # 应该是本地时间，至少不是 UTC 凌晨

    def test_invalid_format_returns_now(self):
        dt = self.fp._parse_timestamp("not a date")
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None

    def test_empty_string_returns_now(self):
        dt = self.fp._parse_timestamp("")
        assert isinstance(dt, datetime)


# ============ _effective_skip_types ============

class TestEffectiveSkipTypes:
    def test_basic_skip_types(self):
        fp = FakeParse()
        fp.config.skip_msg_types = {"image", "video", "voice"}
        result = fp._effective_skip_types()
        assert result == {"image", "video", "voice"}

    def test_ocr_enabled_excludes_image(self):
        fp = FakeParse()
        fp.config.skip_msg_types = {"image", "video"}
        fp.config.image_ocr_enabled = True
        result = fp._effective_skip_types()
        assert "image" not in result
        assert "video" in result

    def test_graceful_fallback_excludes(self):
        fp = FakeParse()
        fp.config.skip_msg_types = {"image", "voice", "video"}
        fp.config.graceful_fallback_msg_types = {"voice", "video"}
        result = fp._effective_skip_types()
        assert result == {"image"}  # voice/video 被 graceful_fallback 剔除

    def test_empty(self):
        fp = FakeParse()
        fp.config.skip_msg_types = set()
        assert fp._effective_skip_types() == set()


# ============ _detect_msg_type ============

class TestDetectMsgType:
    def setup_method(self):
        self.fp = FakeParse()

    def test_text_default(self):
        assert self.fp._detect_msg_type({"content": "hello world", "senderId": "uid_001"}) == "text"

    def test_system_by_sender(self):
        assert self.fp._detect_msg_type({"sender": "系统"}) == "system"

    def test_system_by_empty_sender_id(self):
        assert self.fp._detect_msg_type({"content": "test"}) == "system"

    def test_image_by_msgType(self):
        assert self.fp._detect_msg_type({"msgType": "image", "senderId": "uid_001"}) == "image"

    def test_image_by_content_mediaId(self):
        assert self.fp._detect_msg_type({"content": "mediaId=abc123", "senderId": "uid_001"}) == "image"

    # ---- P0-2026-08-09: msgType 缺失时视频/语音不得被误判为 image ----
    # 线上实测：钉钉视频消息 msgType 为空、content 含 mediaId=，旧逻辑一律判 image，
    # 导致 3.6MB MP4 被存成 .png 喂 OCR（cannot identify image file），且防抖
    # 「等 OCR 完成」白阻塞 30 秒。

    def test_video_by_content_marker_not_image(self):
        raw = {
            "content": ("[视频消息](mediaId=@lQbPJwotjO5Eob8AALCBaiJf1GTSGQpKu120vFkA) "
                        "fileName=mmexport1786244232175.mp4 url: @l"),
            "senderId": "uid_001",
        }
        assert self.fp._detect_msg_type(raw) == "video"

    def test_voice_by_content_marker_not_image(self):
        raw = {
            "content": ("[语音消息](mediaId=@lR_PKdYMu7EwVokAALDRvM-IJNZKfgdcIV96z3MA) "
                        "注意：如需下载使用dws chat message download"),
            "senderId": "uid_001",
        }
        assert self.fp._detect_msg_type(raw) == "voice"

    def test_image_marker_still_image_with_caption(self):
        """图文混排（标记后跟随文字说明）仍须走 image，保住 OCR 能力。"""
        raw = {
            "content": "@主人[图片消息](mediaId=@lQLPJxPGGIj_y-nNAj3NA0uwRR_V2Kj)现场挂VPN上不去",
            "senderId": "uid_001",
        }
        assert self.fp._detect_msg_type(raw) == "image"

    def test_media_kind_by_filename_extension(self):
        """无中文标记时按 fileName 扩展名分流；图片扩展名/无扩展名仍回退 image。"""
        def t(content):
            return self.fp._detect_msg_type({"content": content, "senderId": "uid_001"})
        assert t("mediaId=abc fileName=clip.MOV") == "video"
        assert t("mediaId=abc fileName=rec.amr") == "voice"
        assert t("mediaId=abc fileName=合同.pdf") == "file"
        assert t("mediaId=abc fileName=shot.PNG") == "image"
        assert t("mediaId=abc123&text=老查询串格式") == "image"

    def test_voice_by_msgType(self):
        assert self.fp._detect_msg_type({"msgType": "voice", "senderId": "uid_001"}) == "voice"

    def test_video_by_msgType(self):
        assert self.fp._detect_msg_type({"msgType": "video", "senderId": "uid_001"}) == "video"

    def test_link_by_content_url(self):
        assert self.fp._detect_msg_type({"content": "https://example.com", "senderId": "uid_001"}) == "link"

    def test_file_by_json_content(self):
        assert self.fp._detect_msg_type({"content": '{"filename": "report.pdf", "fileSize": 1024}', "senderId": "uid_001"}) == "file"

    def test_markdown_by_json_content(self):
        assert self.fp._detect_msg_type({"content": '{"markdown": {"title": "test"}}', "senderId": "uid_001"}) == "markdown"

    def test_call_by_keyword(self):
        assert self.fp._detect_msg_type({"content": "通话时长 5分钟", "senderId": "uid_001"}) == "call"

    def test_call_notification_mislabeled_as_text(self):
        """回归：钉钉语音通话结束通知 dws 标 msgType=text，必须仍识别为 call 跳过。

        真实日志曾出现「[语音通话] 通话时长 48秒」被当成用户发言并触发 LLM 回复。
        """
        raw = {"msgType": "text", "content": "[语音通话] 通话时长 48秒", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "call"

    def test_call_notification_mislabeled_as_markdown(self):
        raw = {"msgType": "markdown", "content": "语音通话 通话时长 1:24", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "call"

    def test_edit_mislabeled_as_text(self):
        raw = {"msgType": "text", "content": "消息已编辑", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "edit"

    def test_recall_mislabeled_as_text(self):
        raw = {"msgType": "text", "content": "消息已撤回", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "recall"

    # ============ 已读回执拦截（2026-08-01 新增） ============
    def test_read_receipt_bracketed_notification(self):
        """钉钉「已读」回执通知（方括号前缀，机器生成）应归为 read_receipt 跳过。"""
        raw = {"msgType": "text", "content": "[已读]", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "read_receipt"

    def test_read_receipt_mislabeled_as_text(self):
        """回归：dws 把已读回执标 msgType=text 时，必须仍识别为 read_receipt 跳过。"""
        raw = {"msgType": "text", "content": "[已读回执] 你发的消息", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "read_receipt"

    def test_read_receipt_keyword(self):
        assert self.fp._detect_msg_type({"content": "已读回执", "senderId": "uid_001"}) == "read_receipt"
        assert self.fp._detect_msg_type({"content": "消息已读回执", "senderId": "uid_001"}) == "read_receipt"

    def test_read_receipt_latin_keyword(self):
        # 英文 read receipt 不区分大小写
        assert self.fp._detect_msg_type({"msgType": "text", "content": "Read Receipt", "senderId": "uid_001"}) == "read_receipt"
        assert self.fp._detect_msg_type({"msgType": "text", "content": "msg_read", "senderId": "uid_001"}) == "read_receipt"

    def test_human_text_with_已读_not_misclassified(self):
        """负例：真人消息提到「已读」不得被误判为回执（锚定方括号/回执，裸已读放行）。"""
        raw = {"msgType": "text", "content": "这样来看就能明白为啥之前他们已读不回了", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "text"
        raw2 = {"msgType": "text", "content": "标记已读后过连天就忘了", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw2) == "text"

    def test_latin_recall_keyword_case_insensitive(self):
        # 原实现 msgEdited/msgRecalled 因大小写不匹配无法命中，现应不区分大小写
        assert self.fp._detect_msg_type({"msgType": "text", "content": "msgEdited", "senderId": "uid_001"}) == "edit"
        assert self.fp._detect_msg_type({"msgType": "text", "content": "msgRecalled", "senderId": "uid_001"}) == "recall"

    def test_plain_text_not_misclassified(self):
        # 普通用户文本不应被关键词误判
        raw = {"msgType": "text", "content": "刚才的通话你说到哪了？", "senderId": "uid_001"}
        assert self.fp._detect_msg_type(raw) == "text"

    def test_edit_by_keyword(self):
        assert self.fp._detect_msg_type({"content": "消息已编辑", "senderId": "uid_001"}) == "edit"

    def test_recall_by_keyword(self):
        assert self.fp._detect_msg_type({"content": "消息已撤回", "senderId": "uid_001"}) == "recall"

    def test_action_card(self):
        assert self.fp._detect_msg_type({"msgType": "action_card", "senderId": "uid_001"}) == "app"


# ============ _extract_rich_text ============

class TestExtractRichText:
    def test_simple_text_items(self):
        blob = '[{"text":{"items":[{"data":{"text":"你好"},"type":"text"}]}}]'
        text, has_img = ParseMixin._extract_rich_text(blob)
        assert "你好" in text
        assert has_img is False

    def test_with_image(self):
        blob = '[{"text":{"items":[{"data":{"text":"图"},"type":"text"},{"type":"image","preview":true}]}}]'
        text, has_img = ParseMixin._extract_rich_text(blob)
        assert has_img is True

    def test_empty_string(self):
        text, has_img = ParseMixin._extract_rich_text("")
        assert text == ""
        assert has_img is False

    def test_invalid_json(self):
        text, has_img = ParseMixin._extract_rich_text("not json")
        assert text == ""


# ============ is_read_receipt_content（发送前硬闸门判定逻辑） ============
from src.poller_utils import is_read_receipt_content


class TestIsReadReceiptContent:
    def test_bracketed_read_receipt(self):
        assert is_read_receipt_content("[已读]")
        assert is_read_receipt_content("[已读回执] 你发的消息")

    def test_回执_keyword(self):
        assert is_read_receipt_content("已读回执")
        assert is_read_receipt_content("消息已读回执")
        assert is_read_receipt_content("已读通知")

    def test_latin_keyword_case_insensitive(self):
        assert is_read_receipt_content("Read Receipt")
        assert is_read_receipt_content("readreceipt")
        assert is_read_receipt_content("msg_read")

    def test_human_text_with_已读_not_matched(self):
        assert not is_read_receipt_content("他们已读不回了")
        assert not is_read_receipt_content("标记已读后过连天就忘了")
        assert not is_read_receipt_content("默认给已读 ai 就不接管了")

    def test_empty_and_none(self):
        assert not is_read_receipt_content("")
        assert not is_read_receipt_content(None)


# ============ _extract_content OA 卡片健壮性（防全员停复回归） ============

class TestExtractContentOARobustness:
    """钉钉 OA 审批卡片 head/body/form 可能为显式 null，必须降级而非崩溃。

    修复前：head/body 为 null 时 .get 链式返回 None → AttributeError/TypeError
    逃逸（except 只捕 JSONDecodeError/ValueError）→ 中断整批轮询，卡片留在时间窗内
    每轮复现 → 全员停止回复。
    """

    def setup_method(self):
        self.fp = FakeParse()

    def test_oa_null_head_and_body_does_not_crash(self):
        raw = {"content": json.dumps({"oa": {"head": None, "body": None}})}
        out = self.fp._extract_content(raw)
        assert "[OA审批]" in out

    def test_oa_null_form_does_not_crash(self):
        raw = {"content": json.dumps(
            {"oa": {"head": {"text": "请假申请"}, "body": {"form": None}}})}
        out = self.fp._extract_content(raw)
        assert "[OA审批] 请假申请" in out

    def test_oa_normal_still_extracted(self):
        raw = {"content": json.dumps({
            "oa": {
                "head": {"text": "报销"},
                "body": {"form": [{"key": "金额", "value": "100"}]},
            }})}
        out = self.fp._extract_content(raw)
        assert "[OA审批] 报销" in out
        assert "金额: 100" in out
