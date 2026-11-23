"""媒体引用「转述」误判 → 图片下载无限重试刷屏 的回归测试。

🔴 事故背景（2026-11-23 线上）：Linkora 用主人账号给主人发出「未回复待办」
汇总通知，正文里引用了**对方那条消息**的 mediaId：

    📋 工作时间外未回复消息已建待办（共 1 条） · [dingtalk] 张旭：[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw)

该通知被轮询器回灌后：
  1. 正文含 ``mediaId=`` → 消息类型被判成 ``image``；
  2. 走图片下载 → 用**通知自己**的 msgId/会话去换下载地址；
  3. 但那段 mediaId 属于**另一个会话里张旭的消息** → 钉钉返回 ``NO_PERMISSION``；
  4. 文件永不落盘 → 「文件已存在则跳过」的去重失效 → 每轮轮询重复下载、
     每轮失败、每轮刷一条 WARNING（实测每分钟一条，噪音淹没有效日志）。

三处修复各自的验证：
  - 根因：待办通知文本剥离媒体引用标记（不再把 mediaId 带出去）；
  - 兜底：AI 代发消息中的媒体引用判定为「转述」，不进图片下载管线；
  - 降噪：下载失败记住原因并进入冷却窗口，不重复重试。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.poller_core_ocr import OcrMixin
from src.poller_core_parse import ParseMixin, _is_transcribed_media_ref
from src.offhours_todo.service import _summarize_preview
from src.utils.media_text import has_media_ref, strip_media_refs

# 真实事故数据（取自线上 dws 原始返回）
AI_NOTICE_CONTENT = (
    "📋 工作时间外未回复消息已建待办（共 1 条） · [dingtalk] 张旭："
    "[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw"
)
# 张旭原始消息里的**完整** mediaId（46 字符，通知里被截成了 25 字符的碎片）
FULL_MEDIA_ID = "@lQLPJyHo-Z-v_DXNAzTNBgSwUj8GKV7o8vkK0PbPqr35AA"


# ============ 根因：待办通知文本不再携带 mediaId ============

class TestSummarizePreview:
    def test_strips_media_id_from_notification_text(self):
        """通知文本里的 mediaId 必须被剥离（否则带出去就是无效碎片）。"""
        out = _summarize_preview(AI_NOTICE_CONTENT)
        assert "mediaId=" not in out
        assert "@lQLPJyHo-Z-v_DXNAzTNBgSw" not in out

    def test_keeps_human_readable_text(self):
        """剥离媒体标记后应保留人话部分（通知要能说明是谁发了什么）。

        摘要超长时会截断，截断**优先保留靠近占位符的尾部**（谁发的）——
        开头是「📋 工作时间外未回复消息已建待办」这类固定文案，丢失无妨。
        """
        out = _summarize_preview(
            "📋 工作时间外未回复消息已建待办（共 1 条） · [dingtalk] 张旭："
            "[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw"
        )
        assert "张旭" in out
        assert "mediaId=" not in out

    def test_pure_media_message_gets_placeholder(self):
        """整条只有媒体标记 → 留占位符，说明「对方发了张图」。"""
        out = _summarize_preview("[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSwUj8GKV7o8vkK0PbPqr35AA)")
        assert out
        assert "mediaId=" not in out

    def test_truncation_never_cuts_into_resource_id(self):
        """截断必须在剥离媒体标记之后做，否则会切出 ID 碎片。"""
        content = "[图片消息](mediaId=%s)后续文字" % FULL_MEDIA_ID
        out = _summarize_preview(content, limit=40)
        assert "mediaId=" not in out
        assert FULL_MEDIA_ID not in out

    def test_truncation_never_leaves_half_bracket(self):
        """截断不能切进方括号内（曾留下半截 ``[dingtalk``）。"""
        notice = (
            "📋 工作时间外未回复消息已建待办（共 1 条） · [dingtalk] 张旭："
            "[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw"
        )
        out = _summarize_preview(notice)
        # 判据：方括号成对 + 平台前缀完整保留（不能只剩半个 '[dingtalk'）
        assert out.count("[") == out.count("]")
        assert "[dingtalk]" in out

    def test_media_placeholder_survives_truncation(self):
        """截断时「对方发了张图」这个信息不能丢。"""
        notice = (
            "📋 工作时间外未回复消息已建待办（共 1 条） · [dingtalk] 张旭："
            "[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw"
        )
        assert "[媒体]" in _summarize_preview(notice)

    def test_plain_text_unchanged(self):
        assert _summarize_preview("徐工，这个流程麻烦帮忙调整下") == "徐工，这个流程麻烦帮忙调整下"


class TestStripMediaRefs:
    def test_removes_full_media_id(self):
        out = strip_media_refs("[图片消息](mediaId=%s)" % FULL_MEDIA_ID)
        assert FULL_MEDIA_ID not in out
        assert "mediaId=" not in out

    def test_removes_dws_tool_hint(self):
        out = strip_media_refs(
            "[图片消息](mediaId=$iwEeAqNwbmcDAQTRBOYF0QPYBrCNS4ya9v0xbAo84jFH88AAB9IAssT5CAAJomltCgAL0gACoH4) "
            "注意：如需下载使用dws chat message download-media命令下载"
        )
        assert "注意：如需下载" not in out
        assert "mediaId=" not in out

    def test_keeps_caption_text(self):
        out = strip_media_refs("[图片消息](mediaId=%s)宇坤哥，帮我看下这个报错" % FULL_MEDIA_ID)
        assert "宇坤哥" in out
        assert "帮我看下这个报错" in out

    def test_platform_prefix_is_preserved(self):
        """平台前缀 ``[dingtalk]`` 不是媒体标记，必须保留（裸删方括号会丢分组语义）。"""
        out = strip_media_refs("[dingtalk] 张旭：[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw)")
        assert "[dingtalk]" in out
        assert "张旭" in out

    def test_placeholder_survives_bracket_cleanup(self):
        """占位符自身的方括号不能被「清理残留空括号」吃掉（曾退化成「媒」）。"""
        out = strip_media_refs("[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw)")
        assert "[媒体]" in out
        assert "媒]" not in out.replace("[媒体]", "")

    def test_idempotent(self):
        once = strip_media_refs(AI_NOTICE_CONTENT)
        assert strip_media_refs(once) == once

    def test_empty_input(self):
        assert strip_media_refs("") == ""

    def test_has_media_ref(self):
        assert has_media_ref(AI_NOTICE_CONTENT)
        assert not has_media_ref("就是普通的一句话")


# ============ 兜底：AI 代发消息的媒体引用不进下载管线 ============

class TestIsTranscribedMediaRef:
    def test_ai_sent_notice_with_foreign_media_id_is_transcribed(self):
        """事故消息：AI 代发 + 引用他人 mediaId → 判定为转述。"""
        raw = {
            "messageAiSendFlag": "DWS",
            "sendType": "user",
            "content": AI_NOTICE_CONTENT,
        }
        assert _is_transcribed_media_ref(raw) is True

    def test_ai_sent_own_uploaded_image_is_not_transcribed(self):
        """AI 代发但 mediaId 是 $iw（本账号上传的资源）→ 属于本消息，可下载。"""
        raw = {
            "messageAiSendFlag": "DWS",
            "sendType": "user",
            "content": "[图片消息](mediaId=$iwEcAqNwbmcDAQTRD2YF0QnGBrBJuOjZLqjjIArTV7GNp0gAB9IAssT5CAAJomltCgAL0gAWnL4)",
        }
        assert _is_transcribed_media_ref(raw) is False

    def test_human_image_message_is_not_transcribed(self):
        """真人发的图（无 AI 代发标志）→ 正常下载。"""
        raw = {
            "sendType": "user",
            "content": "[图片消息](mediaId=%s)宇坤哥，麻烦看下" % FULL_MEDIA_ID,
        }
        assert _is_transcribed_media_ref(raw) is False

    def test_ai_sent_plain_text_is_not_transcribed(self):
        """AI 代发但正文没有媒体引用 → 与本问题无关。"""
        raw = {"messageAiSendFlag": "DWS", "content": "已处理，稍后回复你"}
        assert _is_transcribed_media_ref(raw) is False


class _FakeParseForPipeline(ParseMixin):
    """最小 fake，用于驱动 _raw_to_message 的图片分支。"""

    def __init__(self):
        self.config = MagicMock()
        self.config.skip_msg_types = set()
        self.config.image_ocr_enabled = True
        self.config.graceful_fallback_msg_types = set()
        self.current_user_id = "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4"
        self.current_user_user_id = ""
        self.current_user_name = "坤子"
        self._skip_ocr = False
        self.download_calls: list[dict] = []

    def _is_system_sender(self, sender_name: str) -> bool:
        return False

    def _extract_content(self, raw: dict) -> str:
        return raw.get("content") or ""

    def _parse_timestamp(self, ts: str):
        from datetime import datetime

        return datetime.now()

    def _image_storage(self, chat_id: str, filename: str):
        from pathlib import Path

        base = Path("/tmp/linkora-test-images") / chat_id
        return base / filename, f"{chat_id}/{filename}"

    def _extract_image_caption(self, raw: dict) -> str:
        return ""

    def _download_image_only(self, raw, chat_id, chat_name, fallback, msg_id):
        self.download_calls.append({"kind": "image_only", "raw": raw})
        return fallback, ""

    def _submit_image_for_ocr(self, raw, chat_id, chat_name, fallback, msg_id):
        self.download_calls.append({"kind": "ocr", "raw": raw})
        return fallback, ""

    def _download_card_images(self, content, chat_id, chat_name, msg_id):
        return {}

    def _download_received_file(self, raw, chat_id, chat_name, msg_id, media_type):
        return "", ""


class TestRawToMessageSkipsTranscribedMedia:
    def test_ai_notice_does_not_trigger_download(self):
        """核心回归：事故通知不得触发任何图片下载调用。"""
        fp = _FakeParseForPipeline()
        raw = {
            "openMessageId": "msg/yMwomnnzNjKGqFjuFX50g==",
            "messageAiSendFlag": "DWS",
            "sendType": "user",
            "sender": "坤子",
            "senderId": "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4",
            "content": AI_NOTICE_CONTENT,
        }
        fp._raw_to_message(raw, "cidlDJoAOMCanOp1NpKb", "single", "坤子")
        assert fp.download_calls == []

    def test_human_image_still_triggers_download(self):
        """真人图片消息仍走下载（不能被兜底误伤）。"""
        fp = _FakeParseForPipeline()
        raw = {
            "openMessageId": "msgkh/br78B5L8suy2OHJh7Fg==",
            "sendType": "user",
            "sender": "张旭",
            "senderId": "OTHER_SENDER",
            "content": "[图片消息](mediaId=%s)徐工，麻烦看下" % FULL_MEDIA_ID,
        }
        fp._raw_to_message(raw, "cidLtSgfFoq7M88mnzivqw", "single", "张旭")
        assert len(fp.download_calls) == 1

    def test_ai_sent_own_image_still_triggers_download(self):
        """AI 代发但资源属于自己（$iw）→ 仍走下载（兜底不得误伤）。"""
        fp = _FakeParseForPipeline()
        raw = {
            "openMessageId": "msgzUTVeuMLwM1VtYBc3WmHJQ==",
            "messageAiSendFlag": "DWS",
            "sendType": "user",
            "sender": "坤子",
            "senderId": "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4",
            "content": "[图片消息](mediaId=$iwEcAqNwbmcDAQTRD2YF0QnGBrBJuOjZLqjjIArTV7GNp0gAB9IAssT5CAAJomltCgAL0gAWnL4)",
        }
        fp._raw_to_message(raw, "cidlDJoAOMCanOp1NpKb", "single", "坤子")
        assert len(fp.download_calls) == 1


# ============ 降噪：下载失败记住原因 + 冷却窗口不重复重试 ============

class _FakeOcrForFailure(OcrMixin):
    """最小 fake：可控的 download_media 行为。"""

    def __init__(self, tmp_path, fail: bool = True):
        import threading

        self.config = MagicMock()
        self.config.image_temp_dir = str(tmp_path)
        self._media_download_failures = {}
        self._ocr_lock = threading.Lock()
        self.calls = 0
        self._fail = fail

        dws = MagicMock()

        def _download(**kwargs):
            self.calls += 1
            if self._fail:
                raise RuntimeError("dws error: no permission")
            # 成功路径要真的落盘：生产代码校验「文件存在且非空」，
            # 不落盘会走到「下载产物为空」分支，测的就不是成功路径了。
            out = kwargs.get("output_path")
            if out:
                from pathlib import Path

                p = Path(out)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"fake-image-bytes")
            return out

        dws.download_media.side_effect = _download
        self.dws = dws

    def _image_storage(self, chat_id: str, filename: str):
        from pathlib import Path

        base = Path(self.config.image_temp_dir) / chat_id
        return base / filename, f"{chat_id}/{filename}"

    def _extract_image_caption(self, raw: dict) -> str:
        return ""


class TestMediaDownloadFailureMemory:
    def _raw(self):
        return {
            "openMessageId": "msg/yMwomnnzNjKGqFjuFX50g==",
            "content": "[图片消息](mediaId=@lQLPJyHo-Z-v_DXNAzTNBgSw)",
        }

    def test_failure_reason_is_logged(self, tmp_path, caplog):
        """失败日志必须带上真因（原先只记「下载失败」四个字，把真因吞了）。"""
        fo = _FakeOcrForFailure(tmp_path)
        with caplog.at_level("WARNING"):
            fo._download_image_only(self._raw(), "cid1", "测试", "", "msg1")
        assert any("no permission" in r.getMessage() for r in caplog.records)

    def test_second_call_is_skipped_during_cooldown(self, tmp_path):
        """冷却窗口内不重复下载（避免每轮轮询刷屏）。"""
        fo = _FakeOcrForFailure(tmp_path)
        fo._download_image_only(self._raw(), "cid1", "测试", "", "msg1")
        assert fo.calls == 1
        fo._download_image_only(self._raw(), "cid1", "测试", "", "msg1")
        assert fo.calls == 1, "冷却期内不应重复调用下载"

    def test_success_clears_failure_memory(self, tmp_path):
        """下载成功后清掉失败记忆（瞬时故障可自愈）。"""
        fo = _FakeOcrForFailure(tmp_path)
        fo._download_image_only(self._raw(), "cid1", "测试", "", "msg1")
        assert fo._media_download_failures
        fo._fail = False
        # 冷却期未到，第二次仍跳过；手动清冷却后应可重试并成功
        fo._media_download_failures.clear()
        fo._download_image_only(self._raw(), "cid1", "测试", "", "msg1")
        assert fo._media_download_failures == {}
