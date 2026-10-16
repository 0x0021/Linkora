"""消息轮询器单元测试。

覆盖核心逻辑：
- 消息去重缓存（LRU容量淘汰，TTL由sqlite_store负责）
- 已处理消息ID过滤（内存+DB双查）
- 无效会话持久化（文件读写）
- 权限错误分类（会话级 vs 全局级）
- 消息类型检测
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_message


def _make_poller(tmp_db_path, max_ids=5):
    """快速构造 poller 实例（mock store 和 dws）。"""
    from src.config import PollerConfig
    from src.poller import MessagePoller

    config = PollerConfig(
        interval_seconds=6,
        unread_conversation_count=20,
        messages_per_conversation=20,
        history_window=20,
        merge_window_seconds=60,
        max_processed_msg_ids=max_ids,
        list_all_time_window_minutes=30,
        list_all_first_run_minutes=5,
        empty_poll_protection_minutes=5,
        inaccessible_file=str(tmp_db_path.parent / "inaccessible.txt"),
        skip_notification_patterns=[],
        skip_msg_types=[],
        reply_cooldown_seconds=60,
        first_run_ignore_older_than_minutes=10,
    )

    mock_store = MagicMock()
    mock_store._message_repo.load_recent_processed_msg_ids.return_value = []
    mock_dws = MagicMock()

    return MessagePoller(
        config=config,
        dws=mock_dws,
        store=mock_store,
        current_user_id="user-001",
        current_user_name="测试用户",
    ), mock_store


# ============ 消息去重缓存测试 ============

class TestProcessedMsgCache:
    """跨轮次消息去重逻辑。"""

    def test_new_msg_is_not_processed(self, tmp_db_path):
        """新消息ID不应被标记为已处理。"""
        poller, mock_store = _make_poller(tmp_db_path)
        mock_store._message_repo.is_message_processed.return_value = False

        assert poller._is_msg_processed("msg-brand-new") is False

    def test_marked_msg_is_processed(self, tmp_db_path):
        """标记过的消息应被识别为已处理。"""
        poller, mock_store = _make_poller(tmp_db_path)

        poller._mark_msg_processed("msg-001", "chat-001")

        assert poller._is_msg_processed("msg-001") is True
        mock_store._message_repo.mark_message_processed.assert_called_once_with("msg-001", "chat-001")

    def test_merged_original_ids_marked_live_key(self, tmp_db_path):
        """合并消息（生产路径 original_ids）经 _mark_msg_processed 后，所有原始 ID 必须被标记。

        回归：poller._mark_msg_processed 旧实现只读 merged_original_ids，
        而生产合并路径(poller._combine_message_group)写的是 original_ids，
        导致 run_loop 兜底路径永远漏标组内原始消息 -> 下一轮重跑。
        """
        poller, mock_store = _make_poller(tmp_db_path)
        t1 = make_message("m1", "chat-001", "文本1", msg_type="text")
        t2 = make_message("m2", "chat-001", "文本2", msg_type="text")
        t3 = make_message("m3", "chat-001", "文本3", msg_type="text")
        merged = poller._merge_consecutive_messages([t1, t2, t3])[0]
        assert merged.raw.get("original_ids") == ["m1", "m2", "m3"]

        poller._mark_msg_processed(merged.msg_id, "chat-001", msg=merged)

        # 组合消息末条 id（m3）被标记
        assert merged.msg_id in poller._processed_msg_ids
        # 所有原始 id 均被标记
        for oid in ("m1", "m2", "m3"):
            assert oid in poller._processed_msg_ids
        # store 对每个原始 id 都写入了去重记录
        marked = {c.args[0] for c in mock_store._message_repo.mark_message_processed.call_args_list}
        assert marked == {"m1", "m2", "m3", merged.msg_id}

    def test_merged_original_ids_marked_alt_key(self, tmp_db_path):
        """合并消息（poller_utils 路径 merged_original_ids）同样必须被标全。"""
        poller, mock_store = _make_poller(tmp_db_path)
        merged = make_message(
            "mX", "chat-001", "合并内容", msg_type="text",
            raw={"merged": True, "merged_original_ids": ["a1", "a2", "a3"]},
        )
        poller._mark_msg_processed(merged.msg_id, "chat-001", msg=merged)
        for oid in ("a1", "a2", "a3"):
            assert oid in poller._processed_msg_ids
        marked = {c.args[0] for c in mock_store._message_repo.mark_message_processed.call_args_list}
        assert marked == {"a1", "a2", "a3", "mX"}

    def test_lru_eviction_when_cache_full(self, tmp_db_path):
        """缓存满时应淘汰最旧的条目。"""
        poller, _ = _make_poller(tmp_db_path, max_ids=3)

        for i in range(3):
            poller._mark_msg_processed(f"msg-{i}", "chat-001")

        poller._mark_msg_processed("msg-new", "chat-001")

        assert len(poller._processed_msg_ids) == 3
        assert "msg-0" not in poller._processed_msg_ids
        assert "msg-new" in poller._processed_msg_ids

    def test_lru_order_updated_on_db_hit(self, tmp_db_path):
        """DB命中后应将对应ID标为最近使用，避免刚访问的ID被LRU误淘汰。"""
        poller, mock_store = _make_poller(tmp_db_path, max_ids=3)
        mock_store._message_repo.is_message_processed.return_value = False

        # 填满缓存
        for i in range(3):
            poller._mark_msg_processed(f"msg-{i}", "chat-001")
        # msg-0 现在是最旧的
        assert next(iter(poller._processed_msg_ids)) == "msg-0"

        # 模拟 DB 命中 msg-0（本应保留），使其被标为最近使用
        mock_store._message_repo.is_message_processed.return_value = True
        assert poller._is_msg_processed("msg-0") is True
        assert next(iter(poller._processed_msg_ids)) == "msg-1"  # msg-0 已移到末尾

    def test_is_msg_processed_checks_db_fallback(self, tmp_db_path):
        """内存中没有但DB中有记录时，应同步到内存并返回True。"""
        poller, mock_store = _make_poller(tmp_db_path)

        assert "msg-db-only" not in poller._processed_msg_ids
        mock_store._message_repo.is_message_processed.return_value = True

        result = poller._is_msg_processed("msg-db-only")

        assert result is True
        assert "msg-db-only" in poller._processed_msg_ids


# ============ 无效会话持久化测试 ============
# 注意: 黑名单持久化已从旧的文件方案重构为 SQLite (store.blocked_conversations 表),
# 启动时限从 DB 加载, 运行时写入, _reconcile_blocklist 自愈解除。以下测试验证 DB 方案。

class TestInaccessiblePersistence:
    """无权限会话的数据库持久化逻辑（跨重启存活 + '=' 归一化）。"""

    def _make_poller_with_store(self, tmp_db_path):
        """构造使用真实 SQLiteStore 的 poller（用于验证持久化）。"""
        from src.config import PollerConfig
        from src.poller import MessagePoller
        from src.memory.sqlite_store import SQLiteStore

        config = PollerConfig(
            interval_seconds=6,
            unread_conversation_count=20,
            messages_per_conversation=20,
            history_window=20,
            merge_window_seconds=60,
            max_processed_msg_ids=500,
            list_all_time_window_minutes=30,
            list_all_first_run_minutes=5,
            empty_poll_protection_minutes=5,
            skip_notification_patterns=[],
            skip_msg_types=[],
            reply_cooldown_seconds=60,
            first_run_ignore_older_than_minutes=10,
        )

        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()  # 建表 (生产环境由 main.py 调用)
        mock_dws = MagicMock()
        # 模拟会话仍不可达: 自愈探测 chat_message_list 抛权限错误, 保留黑名单
        # (否则 MagicMock 默认返回 truthy, 会被误判为'已恢复访问'而解除黑名单)
        mock_dws.chat_message_list.side_effect = Exception("OpendId is not in conversation")

        return MessagePoller(
            config=config,
            dws=mock_dws,
            store=store,
            current_user_id="user-001",
            current_user_name="测试用户",
        ), store

    def test_load_inaccessible_from_db(self, tmp_db_path):
        """启动时应从数据库加载无效会话ID（模拟重启后不复发）。"""
        poller1, store = self._make_poller_with_store(tmp_db_path)
        poller1._block_conversation(
            open_id="cid-001", title="群A", chat_type="group",
            error=RuntimeError("OpendId is not in conversation"),
        )
        poller1._block_conversation(
            open_id="cid-002", title="群B", chat_type="group",
            error=RuntimeError("OpendId is not in conversation"),
        )
        # 模拟重启: 新 poller 实例从同一 DB 加载
        poller2, _ = self._make_poller_with_store(tmp_db_path)
        assert "cid-001" in poller2._inaccessible_conversations
        assert "cid-002" in poller2._inaccessible_conversations

    def test_save_inaccessible_writes_db(self, tmp_db_path):
        """加入黑名单时应写入数据库。"""
        poller, store = self._make_poller_with_store(tmp_db_path)
        poller._block_conversation(
            open_id="cid-new", title="群C", chat_type="group",
            error=RuntimeError("OpendId is not in conversation"),
        )
        blocked = store._blacklist_repo.load_blocked_conversations()
        assert any(b["chat_id"] == "cid-new" for b in blocked)

    def test_inaccessible_normalizes_trailing_equals(self, tmp_db_path):
        """加载和保存时应统一去掉末尾的 '=' (钉钉 open_id 偶发 padding)。"""
        poller, store = self._make_poller_with_store(tmp_db_path)
        poller._block_conversation(
            open_id="cid-with-equals=", title="群D", chat_type="group",
            error=RuntimeError("OpendId is not in conversation"),
        )
        # 内存集合不含 '='
        assert "cid-with-equals" in poller._inaccessible_conversations
        assert "cid-with-equals=" not in poller._inaccessible_conversations
        # DB 也不含 '='
        blocked = store._blacklist_repo.load_blocked_conversations()
        assert any(b["chat_id"] == "cid-with-equals" for b in blocked)
        assert not any(b["chat_id"].endswith("=") for b in blocked)


# ============ 权限错误分类测试 ============

class TestPermissionErrorClassification:
    """区分会话级权限错误与全局权限错误。"""

    def test_not_in_conversation_is_session_error(self, tmp_db_path):
        """'OpendId is not in conversation' 应识别为会话级错误。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("130003 OpendId is not in conversation")
        assert poller._is_permission_error(error) is True
        assert poller._is_global_permission_error(error) is False

    def test_confidential_group_is_session_error(self, tmp_db_path):
        """保密群错误应识别为会话级。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("1001 保密群无法获取消息")
        assert poller._is_permission_error(error) is True

    def test_token_verified_failed_is_global_error(self, tmp_db_path):
        """TOKEN_VERIFIED_FAILED 应识别为全局错误。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("TOKEN_VERIFIED_FAILED")
        assert poller._is_global_permission_error(error) is True
        assert poller._is_permission_error(error) is False

    def test_org_not_enabled_is_global_error(self, tmp_db_path):
        """组织未开启CLI权限应识别为全局错误。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("该组织尚未开启 CLI 数据访问权限")
        assert poller._is_global_permission_error(error) is True
        assert poller._is_permission_error(error) is False

    def test_agent_code_not_exists_is_global_error(self, tmp_db_path):
        """AGENT_CODE_NOT_EXISTS 应识别为全局错误。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("AGENT_CODE_NOT_EXISTS")
        assert poller._is_global_permission_error(error) is True
        assert poller._is_permission_error(error) is False

    def test_no_permission_without_token_is_session_error(self, tmp_db_path):
        """'no permission' 但不含 TOKEN_VERIFIED_FAILED 应为会话级。"""
        poller, _ = _make_poller(tmp_db_path)

        error = Exception("no permission to access this chat")
        assert poller._is_permission_error(error) is True


# ============ 消息类型检测测试 ============

class TestMessageTypeDetection:
    """从原始消息字典检测消息类型。"""

    def test_detect_system_message_by_sender(self, tmp_db_path):
        """发送者为'系统'应识别为system类型。"""
        poller, _ = _make_poller(tmp_db_path)

        raw = {"sender": "系统", "senderOpenDingTalkId": ""}
        assert poller._detect_msg_type(raw) == "system"

    def test_detect_link_by_content_url(self, tmp_db_path):
        """内容以http开头应识别为link（需提供有效sender避免被判定为system）。"""
        poller, _ = _make_poller(tmp_db_path)

        raw = {
            "sender": "张三",
            "senderOpenDingTalkId": "oid-zhangsan",
            "content": "https://example.com/doc",
        }
        assert poller._detect_msg_type(raw) == "link"

    def test_detect_call_by_keyword(self, tmp_db_path):
        """内容包含通话关键词应识别为call。"""
        poller, _ = _make_poller(tmp_db_path)

        raw = {
            "sender": "李四",
            "senderOpenDingTalkId": "oid-lisi",
            "content": "通话结束，时长5分钟",
        }
        assert poller._detect_msg_type(raw) == "call"

    def test_detect_text_as_default(self, tmp_db_path):
        """普通文本内容默认识别为text。"""
        poller, _ = _make_poller(tmp_db_path)

        raw = {
            "sender": "王五",
            "senderOpenDingTalkId": "oid-wangwu",
            "content": "你好，请问VPN怎么配置？",
        }
        assert poller._detect_msg_type(raw) == "text"


class TestRichTextExtraction:
    """钉钉富文本/深链注入泄露的清洗（防止原始 JSON 污染历史并営给 LLM）。"""

    def test_rich_text_items_extracted(self, tmp_db_path):
        """'* 仅你和对方可见\n[{...items...}]' 富文本应提取纯文本，不吐 JSON。"""
        poller, _ = _make_poller(tmp_db_path)
        content = (
            '* 仅你和对方可见\n'
            '[{"text":{"minSupportVersion":"1.1","items":['
            '{"data":{"text":"[向右]"},"type":"text"},'
            '{"data":{"text":"请直接说明问题"},"type":"text"}'
            ']}}]'
        )
        out = poller._extract_content({"content": content})
        assert "minSupportVersion" not in out
        assert not out.startswith("[{")
        assert "请直接说明问题" in out

    def test_rich_text_multi_segment(self, tmp_db_path):
        """多段拼接 JSON（[{...}]\n{...deeplink...}）也能提取文本并剔除深链。"""
        poller, _ = _make_poller(tmp_db_path)
        content = (
            '[{"text":{"items":[{"data":{"text":"你好"},"type":"text"}]}}]\n'
            '{"previewUrl":"dingtalk://dingtalkclient/action/open_platform_link?x=1"}'
        )
        out = poller._extract_content({"content": content})
        assert "你好" in out
        assert "dingtalk://" not in out
        assert "previewUrl" not in out

    def test_deeplink_stripped_from_plain_text(self, tmp_db_path):
        """普通文本里嵌入的 dingtalk:// 深链应被剔除，正文保留。"""
        poller, _ = _make_poller(tmp_db_path)
        content = "账号已开通 [dingtalk://dingtalkclient/action/open?x=1]"
        out = poller._extract_content({"content": content})
        assert "账号已开通" in out
        assert "dingtalk://" not in out

    def test_normal_business_text_untouched(self, tmp_db_path):
        """正常业务文本（含“名片”“审批”等词）不得被误伤。"""
        poller, _ = _make_poller(tmp_db_path)
        content = "徐工，看下名片印制流程报错，没有审批人"
        out = poller._extract_content({"content": content})
        assert out == content


# ============ 图片 + 文本 混合消息合并测试 ============

from src.models import Message  # 追加于文件尾部


class TestMixedImageTextMerge:
    """钉钉「图+文本」「图+文本+文本」「文本+图+文本」等混合场景的合并正确性。"""

    def _mk(self, content, msg_type="text", offset=0, msg_id="m"):
        return Message(
            msg_id=msg_id,
            chat_id="c1",
            chat_type="single",
            chat_name="测试会话",
            sender_id="s1",
            sender_name="对方",
            content=content,
            msg_type=msg_type,
            timestamp=datetime(2026, 7, 8, 10, 0, 0) + timedelta(seconds=offset),
            raw={},
        )

    def test_image_then_two_text(self, tmp_db_path):
        """图 + 文本 + 文本：图片识别内容与两段文字均保留，顺序正确。"""
        poller, _ = _make_poller(tmp_db_path)
        img = self._mk("[图片内容]\n报错: NullPointer", "image", 0, "m1")
        t1 = self._mk("这是报错信息", "text", 10, "m2")
        t2 = self._mk("怎么解决", "text", 20, "m3")
        merged = poller._merge_consecutive_messages([img, t1, t2])
        assert len(merged) == 1
        m = merged[0]
        assert m.msg_type == "mixed"
        assert "报错: NullPointer" in m.content
        assert "这是报错信息" in m.content
        assert "怎么解决" in m.content
        # 顺序：图 -> 文本1 -> 文本2
        assert m.content.index("报错: NullPointer") < m.content.index("这是报错信息") < m.content.index("怎么解决")
        # 图片段被区块包裹
        assert "———— 图片识别内容 ————" in m.content
        assert "———— 图片识别内容结束 ————" in m.content

    def test_text_then_image_then_text(self, tmp_db_path):
        """文本 + 图 + 文本：前后文字与图片 OCR 按序拼合，不被丢弃。"""
        poller, _ = _make_poller(tmp_db_path)
        t1 = self._mk("帮我看下这个报错", "text", 0, "m1")
        img = self._mk("[图片内容]\nTraceback...", "image", 10, "m2")
        t2 = self._mk("特别是最后一行", "text", 20, "m3")
        merged = poller._merge_consecutive_messages([t1, img, t2])
        assert len(merged) == 1
        m = merged[0]
        assert m.msg_type == "mixed"
        assert m.content.index("帮我看下这个报错") < m.content.index("Traceback") < m.content.index("特别是最后一行")
        assert "———— 图片识别内容 ————" in m.content

    def test_image_with_caption_and_two_text(self, tmp_db_path):
        """图带随图文字(caption) + 后两条文本：caption 与多段文字均保留。"""
        poller, _ = _make_poller(tmp_db_path)
        img = self._mk("帮我看这个\n[图片内容]\nOCR文本...", "image", 0, "m1")
        t1 = self._mk("上线后就这样", "text", 10, "m2")
        t2 = self._mk("急", "text", 20, "m3")
        merged = poller._merge_consecutive_messages([img, t1, t2])
        m = merged[0]
        assert m.msg_type == "mixed"
        assert "帮我看这个" in m.content
        assert "上线后就这样" in m.content
        assert "急" in m.content

    def test_pure_text_group_stays_text(self, tmp_db_path):
        """纯文本多段合并应保持 text 类型，不被误判为 mixed。"""
        poller, _ = _make_poller(tmp_db_path)
        t1 = self._mk("第一段", "text", 0, "m1")
        t2 = self._mk("第二段", "text", 10, "m2")
        merged = poller._merge_consecutive_messages([t1, t2])
        m = merged[0]
        assert m.msg_type == "text"
        assert "第一段" in m.content and "第二段" in m.content


# ============ _extract_image_caption 随图文字提取测试 ============

class TestExtractImageCaption:
    """验证从 DWS 原始图片消息中提取随图文字（caption）的各种格式。

    覆盖 bug: 用户发「图片+文字」混合消息时文字被静默丢失。
    """

    def test_top_level_text(self, tmp_db_path):
        """raw["text"] 存在且非 mediaId 串时直接返回。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"text": "坤哥，帮我看下这个报错", "content": "mediaId=abc"}
        assert poller._extract_image_caption(raw) == "坤哥，帮我看下这个报错"

    def test_top_level_text_ignored_when_mediaid(self, tmp_db_path):
        """raw["text"] == "mediaId=xxx" 时不应误当 caption。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"text": "mediaId=abc123", "content": "mediaId=abc123"}
        assert poller._extract_image_caption(raw) == ""

    def test_json_inner_text(self, tmp_db_path):
        """content 为 JSON 时取内层 text 字段。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"content": '{"mediaId":"abc","text":"流程需要终止"}'}
        assert poller._extract_image_caption(raw) == "流程需要终止"

    def test_json_inner_description(self, tmp_db_path):
        """content JSON 中 description 字段也能被提取。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"content": '{"mediaId":"abc","description":"2026-07-042378流程终止"}'}
        assert poller._extract_image_caption(raw) == "2026-07-042378流程终止"

    def test_query_string_format(self, tmp_db_path):
        """content 为查询串 'mediaId=xxx&text=用户文字' 时提取 text 参数。

        这是钉钉/DWS 可能使用的「图片+文字」混合格式之一。
        """
        poller, _ = _make_poller(tmp_db_path)
        raw = {"content": "mediaId=abc123&text=坤哥，流程需要终止后续流程"}
        assert "坤哥，流程需要终止后续流程" in poller._extract_image_caption(raw)

    def test_query_string_title_param(self, tmp_db_path):
        """查询串中 title 参数也能被提取。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"content": "mediaId=xyz&title=已使用新流程2026-07-142432跑后续"}
        cap = poller._extract_image_caption(raw)
        assert "142432" in cap

    def test_top_level_title_fallback(self, tmp_db_path):
        """raw["title"] 作为兜底字段。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"title": "设备返厂申请截图", "content": "mediaId=abc", "msgType": "image"}
        assert poller._extract_image_caption(raw) == "设备返厂申请截图"

    def test_no_false_positive_on_pure_mediaid(self, tmp_db_path):
        """纯 mediaId=xxx 不应产生假 caption。"""
        poller, _ = _make_poller(tmp_db_path)
        raw = {"content": "mediaId=abc123def", "msgType": "image"}
        assert poller._extract_image_caption(raw) == ""

    def test_empty_raw(self, tmp_db_path):
        """空 raw 不应崩溃。"""
        poller, _ = _make_poller(tmp_db_path)
        assert poller._extract_image_caption({}) == ""
        assert poller._extract_image_caption({"content": ""}) == ""


# ============ 时间戳解析（时区收敛，M3） ============

class TestParseTimestamp:
    """钉钉时间戳应统一收敛为本地 naive datetime，避免 naive/aware 混用时区错位。"""

    def test_common_format_is_naive_local(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        dt = poller._parse_timestamp("2026-07-11 13:00:00")
        assert dt.tzinfo is None
        assert dt == datetime(2026, 7, 11, 13, 0, 0)

    def test_iso_naive_is_local(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        dt = poller._parse_timestamp("2026-07-11T13:00:00")
        assert dt.tzinfo is None
        assert dt == datetime(2026, 7, 11, 13, 0, 0)

    def test_iso_utc_z_converted_to_local(self, tmp_db_path):
        """钉钉返回 UTC ISO（Z）应被转换到本地时区，而非被当作 naive UTC。"""
        poller, _ = _make_poller(tmp_db_path)
        local_tz = datetime.now().astimezone().tzinfo
        dt = poller._parse_timestamp("2026-07-11T05:00:00Z")
        assert dt.tzinfo is None  # 收敛为 naive
        expected = datetime(2026, 7, 11, 5, 0, 0, tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None)
        assert dt == expected

    def test_iso_with_offset_converted_to_local(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        local_tz = datetime.now().astimezone().tzinfo
        dt = poller._parse_timestamp("2026-07-11T13:00:00+08:00")
        assert dt.tzinfo is None
        expected = datetime(2026, 7, 11, 13, 0, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(local_tz).replace(tzinfo=None)
        assert dt == expected

    def test_invalid_falls_back_to_now(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        dt = poller._parse_timestamp("not-a-time")
        assert dt.tzinfo is None
        assert isinstance(dt, datetime)


class TestIsPoliteMessage:
    """_is_polite_message 只有当「整条消息除礼貌词外不含其它实质内容」时才返回 True。

    回归：旧实现是子串命中即视为纯礼貌，导致「收到，帮我导出报表」这类
    含业务内容的消息被合并阶段整条丢弃。
    """

    def _poller(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        return poller

    @pytest.mark.parametrize("text", [
        "收到", "好的", "谢谢", "感谢", "辛苦了", "OK", "ok", "再见", "晚安",
        "收到，谢谢", "好的 明白了", "谢谢老板 辛苦了",
    ])
    def test_pure_polite_detected(self, tmp_db_path, text):
        assert self._poller(tmp_db_path)._is_polite_message(text) is True

    @pytest.mark.parametrize("text", [
        "收到，帮我导出报表",
        "谢谢，已处理完成",
        "好的，把昨天的会议纪要发我",
        "辛苦了，这是客户的返厂换新工单",
        "OK，那就按这个方案推进",
        "收到款，请查收",
        "没问题，马上安排",
    ])
    def test_business_with_polite_kept(self, tmp_db_path, text):
        assert self._poller(tmp_db_path)._is_polite_message(text) is False

    def test_empty_not_polite(self, tmp_db_path):
        assert self._poller(tmp_db_path)._is_polite_message("") is False
        assert self._poller(tmp_db_path)._is_polite_message("   ") is False

    def test_ok_substring_not_matching_okr(self, tmp_db_path):
        # "OK" 不应误杀含 "OKR" 等业务词的消息
        assert self._poller(tmp_db_path)._is_polite_message("OKR 目标已对齐") is False


class TestBotMessageDetectionMarkdownPrefix:
    """回归测试：_check_if_bot_message 和 _is_duplicate_self_message 必须容忍
    extract_card_title 去掉的 ## 前导 + 首尾 ** 标记。

    真实场景：bot 12:01:23 发送 markdown（带 ## 标题），存 DB 时 extract_card_title
    去掉 ## 头部和收尾 **。poller 12:01:24 拉回 echo（保留完整 ## 标题...**），
    之前 content[:50] prefix 差 3 字符导致 _check_if_bot_message miss，
    echo 被错存为 role=user, is_bot=0，污染 history 上下文。
    """

    def _make_real_store_poller(self, tmp_db_path):
        """构造带真实 SQLiteStore 的 poller。"""
        from src.config import PollerConfig
        from src.memory.sqlite_store import SQLiteStore
        from src.poller import MessagePoller

        config = PollerConfig(
            interval_seconds=6,
            unread_conversation_count=20,
            messages_per_conversation=20,
            history_window=20,
            merge_window_seconds=60,
            max_processed_msg_ids=100,
            list_all_time_window_minutes=30,
            list_all_first_run_minutes=5,
            empty_poll_protection_minutes=5,
            inaccessible_file=str(tmp_db_path.parent / "inaccessible.txt"),
            skip_notification_patterns=[],
            skip_msg_types=[],
            reply_cooldown_seconds=60,
            first_run_ignore_older_than_minutes=10,
        )
        store = SQLiteStore(str(tmp_db_path))
        store.init_db()
        mock_dws = MagicMock()
        poller = MessagePoller(
            config=config,
            dws=mock_dws,
            store=store,
            current_user_id="bot-open-id-001",
            current_user_name="机器人",
        )
        return poller, store

    def test_check_if_bot_message_matches_md_prefix_variants(self, tmp_db_path):
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-001"
        # 1) bot 12:01:26 存的 assistant 消息：extract_card_title 去掉 ## 头部
        bot_msg = make_message(
            msg_id="bot-uuid-001",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='**"力拔山兮气盖世"** 出自西楚霸王项羽的《垓下歌》。\n**某城市天气** 7/12 阴 30°C',
            role="assistant",
            timestamp=datetime(2026, 7, 12, 12, 1, 26),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        # 2) echo 12:01:24 拉回：含完整 ## 卡片标题头部（extract_card_title 不在 echo 上跑）
        echo_msg = make_message(
            msg_id="openMessageId-001",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='## **"力拔山兮气盖世"** 出自西楚霸王项羽的《垓下歌》。\n**某城市天气** 7/12 阴 30°C',
            timestamp=datetime(2026, 7, 12, 12, 1, 24),
        )
        assert poller._check_if_bot_message(echo_msg) is True, \
            "标准化的 prefix 匹配应能识别 echo 是 bot 代发（去 ## 前导后 echo == bot 存储格式）"

    def test_is_duplicate_self_message_matches_md_prefix_variants(self, tmp_db_path):
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-002"
        bot_msg = make_message(
            msg_id="bot-uuid-002",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='**"力拔山兮"** 出自项羽',
            role="assistant",
            timestamp=datetime(2026, 7, 12, 12, 1, 26),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        echo_msg = make_message(
            msg_id="openMessageId-002",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='## **"力拔山兮"** 出自项羽',
            timestamp=datetime(2026, 7, 12, 12, 1, 24),
        )
        assert poller._is_duplicate_self_message(echo_msg) is True, \
            "应识别为已存在的 bot 回复（防止双写 user 错存）"

    def test_check_if_bot_message_outside_time_window_returns_false(self, tmp_db_path):
        """±120s 时间窗兜底：超出窗口的不应误判。"""
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-003"
        bot_msg = make_message(
            msg_id="bot-uuid-003",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='**力拔山兮** 出自项羽',
            role="assistant",
            timestamp=datetime(2026, 7, 12, 12, 0, 0),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        # echo 比 bot 早 10 分钟（600s > 120s）
        echo_msg = make_message(
            msg_id="openMessageId-003",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='## **力拔山兮** 出自项羽',
            timestamp=datetime(2026, 7, 12, 11, 50, 0),
        )
        # 时间窗外但内容相同，应返回 False（不当作 bot 代发）
        assert poller._check_if_bot_message(echo_msg) is False

    def test_check_if_bot_message_no_assistant_record_returns_false(self, tmp_db_path):
        """无任何 assistant 记录时，新消息不应误判为 bot 代发。"""
        poller, _ = self._make_real_store_poller(tmp_db_path)
        echo_msg = make_message(
            msg_id="openMessageId-004",
            chat_id="cid+test-chat-004",
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='## 完全无历史记录的 echo**',
            timestamp=datetime(2026, 7, 12, 12, 1, 24),
        )
        assert poller._check_if_bot_message(echo_msg) is False

    def test_check_if_bot_message_msg_id_match_takes_priority(self, tmp_db_path):
        """第 1 步 msg_id 精确匹配应当最高优先级（不管内容时间窗）。"""
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-005"
        bot_msg = make_message(
            msg_id="shared-msg-id-005",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='完全不同内容',
            role="assistant",
            timestamp=datetime(2026, 7, 12, 12, 0, 0),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        echo_msg = make_message(
            msg_id="shared-msg-id-005",  # msg_id 一致
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content="无关内容",
            timestamp=datetime(2026, 7, 12, 12, 0, 0),
        )
        assert poller._check_if_bot_message(echo_msg) is True

    def test_check_if_bot_message_whitespace_normalized(self, tmp_db_path):
        """回归：bot 回复发出时 content 带 \\n，钉钉 list-all 抓回时 \\n→空格，
        _check_if_bot_message 必须空格归一化识别为 bot 代发；否则 echo 被错存
        is_bot=0 污染接管判定（误判「用户手动接管」，漏回消息）。"""
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-ws"
        bot_msg = make_message(
            msg_id="bot-uuid-ws",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='视频和图片我这边无法直接查看内容。\n你把视频里的问题或图片里的内容描述一下，我帮你处理。',
            role="assistant",
            timestamp=datetime(2026, 8, 9, 10, 58, 37),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        echo_msg = make_message(
            msg_id="openMessageId-ws",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='视频和图片我这边无法直接查看内容。 你把视频里的问题或图片里的内容描述一下，我帮你处理。',
            timestamp=datetime(2026, 8, 9, 10, 58, 37),
        )
        assert poller._check_if_bot_message(echo_msg) is True, \
            "\\n↔空格 差异不应击穿 bot 检测（F-接管误判根因）"

    def test_is_duplicate_self_message_whitespace_normalized(self, tmp_db_path):
        """同场景：_is_duplicate_self_message 也应空格归一化返回 True，
        避免 AI 回复被双写（is_bot=1 + is_bot=0 两条）污染接管判定。"""
        poller, store = self._make_real_store_poller(tmp_db_path)
        chat_id = "cid+test-chat-ws2"
        bot_msg = make_message(
            msg_id="bot-uuid-ws2",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='视频和图片我这边无法直接查看内容。\n你把视频里的问题描述一下。',
            role="assistant",
            timestamp=datetime(2026, 8, 9, 10, 58, 37),
        )
        bot_msg.is_bot = True
        store._message_repo.save_message(bot_msg, role="assistant")
        echo_msg = make_message(
            msg_id="openMessageId-ws2",
            chat_id=chat_id,
            sender_id="bot-open-id-001",
            sender_name="机器人",
            content='视频和图片我这边无法直接查看内容。 你把视频里的问题描述一下。',
            timestamp=datetime(2026, 8, 9, 10, 58, 37),
        )
        assert poller._is_duplicate_self_message(echo_msg) is True


class TestPollStatsPlatformLabel:
    """周期统计日志（每 12 轮一次）应带平台标识，多平台运行时各平台一目了然。"""

    def _run_one_stats_cycle(self, poller, mock_store, caplog):
        # 下一轮 poll_count 变为 12 → 触发每 12 轮一次的统计日志
        poller._poll_count = 11
        poller._reconcile_every = 100  # 避免触发周期黑名单对账（需额外 mock）
        # 提供一个 openConversationId（非 oc_ 前缀）会话：循环在 chat_id 校验处
        # continue，不触发任何 dws 调用，但 all_conversations 非空（不会提前 return）。
        poller.dws.chat_message_list_unread_conversations.return_value = [
            {"openConversationId": "chat-skip-no-oc"}
        ]
        mock_store._external_friend_repo.list_external_friends.return_value = []
        # 轮询统计 2026-10-16 起降为 DEBUG（周期性输出在稳态运行里属噪声），
        # 此处验证的是统计**内容**（平台标识/CLI 名），与级别无关，故按 DEBUG 捕获。
        caplog.set_level("DEBUG")
        with patch.object(poller, "_fetch_messages_via_list_all", return_value=[]), \
             patch.object(poller, "_get_cached_top_conversations", return_value=[]), \
             patch.object(poller, "_get_recent_conversations_from_db", return_value=[]):
            poller.poll_once()

    def test_stats_log_includes_platform_id(self, tmp_db_path, caplog):
        poller, mock_store = _make_poller(tmp_db_path)
        poller.platform_id = "wecom"

        self._run_one_stats_cycle(poller, mock_store, caplog)

        assert any(
            "wecom" in r.message and "轮询统计" in r.message
            for r in caplog.records
        )

    def test_stats_log_no_platform_id_does_not_crash(self, tmp_db_path, caplog):
        """未传 platform_id 时默认空字符串，日志用占位符，不应 KeyError。"""
        poller, mock_store = _make_poller(tmp_db_path)
        assert poller.platform_id == ""

        self._run_one_stats_cycle(poller, mock_store, caplog)

        assert any("轮询统计" in r.message for r in caplog.records)

    def test_stats_log_emitted_when_no_conversations(self, tmp_db_path, caplog):
        """空平台（如 wecom 0 会话）也应每 12 轮打统计，满足「各平台都要有」。

        回归：旧实现 `if not all_conversations: return` 提前返回，导致 0 会话
        平台永远不打统计；本测试用例验证该分支被移除后仍会输出统计行。
        """
        poller, mock_store = _make_poller(tmp_db_path)
        poller.platform_id = "wecom"
        poller._poll_count = 11
        poller._reconcile_every = 100
        # 关键：未读/置顶/历史均返回空 → all_conversations 为空
        poller.dws.chat_message_list_unread_conversations.return_value = []
        mock_store._external_friend_repo.list_external_friends.return_value = []
        # 轮询统计 2026-10-16 起降为 DEBUG（周期性输出在稳态运行里属噪声），
        # 此处验证的是统计**内容**（平台标识/CLI 名），与级别无关，故按 DEBUG 捕获。
        caplog.set_level("DEBUG")
        with patch.object(poller, "_fetch_messages_via_list_all", return_value=[]), \
             patch.object(poller, "_get_cached_top_conversations", return_value=[]), \
             patch.object(poller, "_get_recent_conversations_from_db", return_value=[]):
            poller.poll_once()

        assert any(
            "wecom" in r.message and "轮询统计" in r.message
            for r in caplog.records
        )
        # 且应显示「检查 0 个会话」
        assert any("检查 0 个会话" in r.message for r in caplog.records)

    def test_stats_log_uses_wecom_cli_label(self, tmp_db_path, caplog):
        """企微应显示 wecom-cli，而非硬编码的 DWS。"""
        poller, mock_store = _make_poller(tmp_db_path)
        poller.platform_id = "wecom"

        self._run_one_stats_cycle(poller, mock_store, caplog)

        assert any("减少 wecom-cli 调用" in r.message for r in caplog.records)
        assert not any("减少 DWS 调用" in r.message for r in caplog.records)

    def test_stats_log_uses_feishu_cli_label(self, tmp_db_path, caplog):
        """飞书应显示 lark-cli。"""
        poller, mock_store = _make_poller(tmp_db_path)
        poller.platform_id = "feishu"

        self._run_one_stats_cycle(poller, mock_store, caplog)

        assert any("减少 lark-cli 调用" in r.message for r in caplog.records)

    def test_stats_log_no_label_when_platform_unknown(self, tmp_db_path, caplog):
        """未知平台无对应 CLI 名时，不应硬加「减少 X 调用」后缀。"""
        poller, mock_store = _make_poller(tmp_db_path)
        poller.platform_id = "some_unknown_platform"

        self._run_one_stats_cycle(poller, mock_store, caplog)

        assert any(
            "轮询统计" in r.message and "减少" not in r.message
            for r in caplog.records
        )


# ============ list-all 快通道派发测试 ============

class TestListAllFastPath:
    """list-all 发现的消息应在 per-conversation 同步抓取之前即时派发（快通道），
    避免被挂死的 dws CLI 调用阻塞整条派发链，导致「发现→回复」延迟达数分钟。

    回归：2026-08-04 实测某轮 poll_once 在 per-conversation 抓取阶段被挂死的 dws
    调用阻塞 ~3 分钟，已发现的 list-all 消息直到阻塞释放后才被派发。
    """

    def _stub_poll_once_side_channels(self, poller):
        """把 list-all 之外的一切通道/会话列表 stub 为空，使 per-conversation 循环不跑。"""
        poller._get_cached_top_conversations = lambda: []
        poller._get_recent_conversations_from_db = lambda: []
        poller.dws.chat_message_list_unread_conversations.return_value = []
        poller.store._external_friend_repo.list_external_friends.return_value = []
        poller._build_group_list_all_cache = lambda convs: None
        # 新消息在最终去重处不应被误判为已处理
        poller.store._message_repo.is_message_processed.return_value = False

    def test_list_all_dispatched_immediately_via_handler(self, tmp_db_path):
        """list-all 消息抓到即经 handler 派发，且不随 return 重复派发。"""
        poller, mock_store = _make_poller(tmp_db_path)
        discovered = [make_message(
            "fast-001", "chat-fast", "快通道测试",
            sender_id="peer-x", sender_name="王博雅",
        )]
        poller._fetch_messages_via_list_all = lambda: discovered
        self._stub_poll_once_side_channels(poller)

        dispatched = []
        def handler(msg):
            dispatched.append(msg)

        result = poller.poll_once(handler=handler)

        # 快通道：handler 被立即调用，且参数正确
        assert len(dispatched) == 1
        assert dispatched[0].msg_id == "fast-001"
        # 不应随整体 return 重复派发（避免同一条消息进两次防抖队列）
        assert all(m.msg_id != "fast-001" for m in result)
        # 派发后立即标记已处理 + 落库（与 run_loop 周期末语义一致）
        mock_store._message_repo.mark_message_processed.assert_called()
        mock_store._message_repo.save_message.assert_called()

    def test_no_handler_falls_back_to_return(self, tmp_db_path):
        """无 handler 时退回旧行为：list-all 消息随 return 返回，不即时派发（测试兼容）。"""
        poller, mock_store = _make_poller(tmp_db_path)
        discovered = [make_message("fallback-001", "chat-fb", "回退测试")]
        poller._fetch_messages_via_list_all = lambda: discovered
        self._stub_poll_once_side_channels(poller)

        dispatched = []
        result = poller.poll_once(handler=None)

        # 无 handler：不即时派发
        assert len(dispatched) == 0
        # 旧行为：消息在 return 列表中
        assert any(m.msg_id == "fallback-001" for m in result)


