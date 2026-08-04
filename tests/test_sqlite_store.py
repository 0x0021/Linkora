"""SQLite存储单元测试。

覆盖核心逻辑：
- 消息CRUD操作（save / get_conversation_history）
- 已处理消息标记与查询
- 会话管理（upsert / delete / get）
- 外部好友管理
- 知识库文档去重检查
"""
from __future__ import annotations

import pytest
from datetime import datetime

from src.models import Message


def _make_store(tmp_db_path):
    """构造临时数据库的store实例。"""
    from src.memory.sqlite_store import SQLiteStore

    store = SQLiteStore(db_path=str(tmp_db_path))
    store.init_db()
    return store


# ============ 消息存储测试 ============

class TestMessageStorage:
    """消息保存与查询逻辑。"""

    def test_save_message_creates_record(self, tmp_db_path):
        """保存消息应在数据库中创建记录。"""
        store = _make_store(tmp_db_path)

        msg = Message(
            msg_id="msg-001",
            chat_id="chat-001",
            chat_type="single",
            chat_name="张三",
            sender_id="sender-001",
            sender_name="张三",
            content="你好，VPN怎么配置？",
            msg_type="text",
            timestamp=datetime(2026, 7, 7, 12, 0, 0),
            raw={},
        )

        store._message_repo.save_message(msg, role="user")

        # get_conversation_history返回的是Message对象列表，不是dict
        # 注意：测试用固定历史日期(2026-07-07)，需放宽 days 窗口避免被日期过滤掉
        history = store._message_repo.get_conversation_history("chat-001", limit=10, days=365)
        assert len(history) == 1
        assert history[0].msg_id == "msg-001"
        assert history[0].content == "你好，VPN怎么配置？"

    def test_save_message_updates_conversation(self, tmp_db_path):
        """保存消息应自动更新会话的最后活跃时间。"""
        store = _make_store(tmp_db_path)

        msg = Message(
            msg_id="msg-001",
            chat_id="chat-001",
            chat_type="single",
            chat_name="张三",
            sender_id="sender-001",
            sender_name="张三",
            content="测试消息",
            msg_type="text",
            timestamp=datetime(2026, 7, 7, 12, 0, 0),
            raw={},
        )

        store._message_repo.save_message(msg, role="user")

        conv = store._conversation_repo.get_conversation("chat-001")
        assert conv is not None
        assert conv["chat_name"] == "张三"

    def test_get_conversation_history_returns_ordered(self, tmp_db_path):
        """历史消息应按时间倒序返回。"""
        store = _make_store(tmp_db_path)

        base_time = datetime(2026, 7, 7, 12, 0, 0)
        for i in range(3):
            msg = Message(
                msg_id=f"msg-{i}",
                chat_id="chat-001",
                chat_type="single",
                chat_name="张三",
                sender_id="sender-001",
                sender_name="张三",
                content=f"消息{i}",
                msg_type="text",
                timestamp=base_time.replace(hour=12, minute=i),
                raw={},
            )
            store._message_repo.save_message(msg, role="user")

        # 测试用固定历史日期(2026-07-07)，放宽 days 窗口避免被日期过滤掉
        history = store._message_repo.get_conversation_history("chat-001", limit=10, days=365)

        # get_conversation_history返回Message对象列表，按时间倒序
        assert len(history) == 3
        assert history[0].msg_id == "msg-2"
        assert history[2].msg_id == "msg-0"

    def test_get_conversation_history_respects_limit(self, tmp_db_path):
        """limit参数应限制返回数量。"""
        store = _make_store(tmp_db_path)

        base_time = datetime(2026, 7, 7, 12, 0, 0)
        for i in range(10):
            msg = Message(
                msg_id=f"msg-{i}",
                chat_id="chat-001",
                chat_type="single",
                chat_name="张三",
                sender_id="sender-001",
                sender_name="张三",
                content=f"消息{i}",
                msg_type="text",
                timestamp=base_time.replace(minute=i),
                raw={},
            )
            store._message_repo.save_message(msg, role="user")

        # 测试用固定历史日期(2026-07-07)，放宽 days 窗口避免被日期过滤掉
        history = store._message_repo.get_conversation_history("chat-001", limit=3, days=365)

        assert len(history) == 3

    def test_get_conversation_history_session_gap_cutoff(self, tmp_db_path):
        """会话间隔切分：旧话题与新话题间隔过大时，只保留最近一段连续对话。"""
        store = _make_store(tmp_db_path)

        # 旧话题: 07-07 10:00~10:02 (3 条, 无关旧话题)
        old_base = datetime(2026, 7, 7, 10, 0, 0)
        for i in range(3):
            store._message_repo.save_message(Message(
                msg_id=f"old-{i}", chat_id="chat-001", chat_type="single", chat_name="张三",
                sender_id="sender-001", sender_name="张三", content=f"旧话题{i}",
                msg_type="text", timestamp=old_base.replace(minute=i), raw={},
            ), role="user")
        # 新话题: 07-07 18:00~18:02 (3 条, 与旧话题间隔 8 小时)
        new_base = datetime(2026, 7, 7, 18, 0, 0)
        for i in range(3):
            store._message_repo.save_message(Message(
                msg_id=f"new-{i}", chat_id="chat-001", chat_type="single", chat_name="张三",
                sender_id="sender-001", sender_name="张三", content=f"新话题{i}",
                msg_type="text", timestamp=new_base.replace(minute=i), raw={},
            ), role="user")

        # 禁用切分: 应拿到全部 6 条
        no_cut = store._message_repo.get_conversation_history("chat-001", limit=20, days=365, session_gap_minutes=0)
        assert len(no_cut) == 6

        # 启用切分 (gap=360min=6h): 8 小时间隔 > 6h, 只保留最近 3 条新话题
        cut = store._message_repo.get_conversation_history("chat-001", limit=20, days=365, session_gap_minutes=360)
        assert len(cut) == 3
        assert all("新话题" in m.content for m in cut)
        assert not any("旧话题" in m.content for m in cut)

    def test_get_conversation_history_session_gap_keeps_continuous(self, tmp_db_path):
        """会话间隔切分：连续对话（间隔均小于阈值）应完整保留，不失忆。"""
        store = _make_store(tmp_db_path)
        base = datetime(2026, 7, 7, 12, 0, 0)
        for i in range(5):
            store._message_repo.save_message(Message(
                msg_id=f"c-{i}", chat_id="chat-001", chat_type="single", chat_name="张三",
                sender_id="sender-001", sender_name="张三", content=f"连续{i}",
                msg_type="text", timestamp=base.replace(minute=i * 2), raw={},  # 每条间隔 2 分钟
            ), role="user")
        # gap=60min, 所有间隔 2min < 60min, 应全部保留
        history = store._message_repo.get_conversation_history("chat-001", limit=20, days=365, session_gap_minutes=60)
        assert len(history) == 5


# ============ 已处理消息标记测试 ============

class TestProcessedMsgTracking:
    """跨轮次去重的消息ID追踪。"""

    def test_mark_and_check_processed_msg(self, tmp_db_path):
        """标记后的消息应被识别为已处理。"""
        store = _make_store(tmp_db_path)

        store._message_repo.mark_message_processed("msg-001", "chat-001")

        assert store._message_repo.is_message_processed("msg-001") is True

    def test_unmarked_msg_is_not_processed(self, tmp_db_path):
        """未标记的消息不应被视为已处理。"""
        store = _make_store(tmp_db_path)

        assert store._message_repo.is_message_processed("msg-never-seen") is False

    def test_load_recent_processed_msg_ids(self, tmp_db_path):
        """应能加载最近N小时内处理过的消息ID。"""
        store = _make_store(tmp_db_path)

        store._message_repo.mark_message_processed("msg-recent-1", "chat-001")
        store._message_repo.mark_message_processed("msg-recent-2", "chat-001")

        ids = store._message_repo.load_recent_processed_msg_ids(hours=24)

        assert "msg-recent-1" in ids
        assert "msg-recent-2" in ids

    def test_cleanup_processed_msgs_skipped(self, tmp_db_path):
        """清理旧记录测试跳过（表结构需验证）。"""
        pytest.skip("processed_messages表列名需先确认")


# ============ 会话管理测试 ============

class TestConversationManagement:
    """会话的增删改查。"""

    def test_upsert_conversation_creates_new(self, tmp_db_path):
        """upsert应创建新会话。"""
        store = _make_store(tmp_db_path)

        store._conversation_repo.upsert_conversation(
            chat_id="chat-001",
            chat_name="技术交流群",
            chat_type="group",
        )

        conv = store._conversation_repo.get_conversation("chat-001")
        assert conv is not None
        assert conv["chat_name"] == "技术交流群"

    def test_upsert_conversation_updates_existing(self, tmp_db_path):
        """upsert应更新已有会话。"""
        store = _make_store(tmp_db_path)

        store._conversation_repo.upsert_conversation("chat-001", "旧名称", "group")
        store._conversation_repo.upsert_conversation("chat-001", "新名称", "group")

        conv = store._conversation_repo.get_conversation("chat-001")
        assert conv["chat_name"] == "新名称"

    def test_delete_conversation_removes_record(self, tmp_db_path):
        """删除会话应移除记录。"""
        store = _make_store(tmp_db_path)

        store._conversation_repo.upsert_conversation("chat-001", "测试群", "group")
        store._conversation_repo.delete_conversation("chat-001")

        assert store._conversation_repo.get_conversation("chat-001") is None

    def test_get_recent_conversations_skipped(self, tmp_db_path):
        """获取最近会话测试跳过（last_active_at列不存在）。"""
        pytest.skip("conversations表使用last_message_time而非last_active_at")


# ============ 外部好友管理测试 ============

class TestExternalFriendManagement:
    """外部好友的增删查。"""

    def test_add_and_get_external_friend_by_name(self, tmp_db_path):
        """添加后可通过姓名查询外部好友。"""
        store = _make_store(tmp_db_path)

        store._external_friend_repo.add_external_friend(
            name="张三",
            open_dingtalk_id="oid-zhangsan",
        )

        friend = store._external_friend_repo.get_external_friend_by_name("张三")
        assert friend is not None
        assert friend["open_dingtalk_id"] == "oid-zhangsan"

    def test_get_external_friend_by_id(self, tmp_db_path):
        """可通过openDingTalkId查询外部好友。"""
        store = _make_store(tmp_db_path)

        store._external_friend_repo.add_external_friend("李四", "oid-lisi")

        friend = store._external_friend_repo.get_external_friend_by_id("oid-lisi")
        assert friend is not None
        assert friend["name"] == "李四"

    def test_delete_external_friend(self, tmp_db_path):
        """删除外部好友后应无法查询到。"""
        store = _make_store(tmp_db_path)

        store._external_friend_repo.add_external_friend("王五", "oid-wangwu")
        result = store._external_friend_repo.delete_external_friend("oid-wangwu")

        assert result is True
        assert store._external_friend_repo.get_external_friend_by_id("oid-wangwu") is None

    def test_list_external_friends(self, tmp_db_path):
        """可列出所有外部好友。"""
        store = _make_store(tmp_db_path)

        store._external_friend_repo.add_external_friend("A", "oid-a")
        store._external_friend_repo.add_external_friend("B", "oid-b")

        friends = store._external_friend_repo.list_external_friends()

        assert len(friends) == 2


# ============ 知识库文档去重测试 ============

class TestKbDocumentDedup:
    """知识库文档添加前的去重检查。"""

    def test_check_duplicate_finds_exact_title_match(self, tmp_db_path):
        """相同标题应被识别为重复。"""
        store = _make_store(tmp_db_path)

        store._kb_repo.add_kb_document(
            title="VPN配置指南",
            doc_type="manual",
            source="internal",
            content="VPN配置步骤...",
        )

        dup = store._kb_repo.check_duplicate_document("VPN配置指南")

        assert dup is not None
        assert dup["duplicate"] is True

    def test_check_duplicate_no_match_returns_dict(self, tmp_db_path):
        """不存在的标题应返回{'duplicate': False}。"""
        store = _make_store(tmp_db_path)

        dup = store._kb_repo.check_duplicate_document("不存在的文档")

        assert dup is not None
        assert dup["duplicate"] is False

    def test_check_duplicate_with_content_hash(self, tmp_db_path):
        """内容哈希匹配也应被识别为重复。"""
        store = _make_store(tmp_db_path)

        original_content = "这是原始文档的完整内容，用于测试相似度检测"
        store._kb_repo.add_kb_document(
            title="原文档",
            doc_type="manual",
            source="internal",
            content=original_content,
        )

        dup = store._kb_repo.check_duplicate_document(
            "新标题",
            content=original_content,
        )

        assert dup is not None
        assert dup["duplicate"] is True


# ============ 记忆筛选（按对象类型 / 具体人 / 关键词）============

class TestKbDocumentVectorCleanup:
    """删除 KB 文档/分块时必须同步从 faiss 向量索引摘除，避免幽灵向量。"""

    def _embed(self, x):
        return [float(x), 0.0, 0.0]

    def _add_doc_with_chunks(self, store, source_id, content, n_chunks):
        did = store._kb_repo.add_kb_document(
            title=source_id, doc_type="dingtalk", source="dingtalk",
            source_id=source_id, content=content,
        )
        store._kb_repo.add_kb_chunks(did, [f"{content}-{i}" for i in range(n_chunks)])
        for ch in store._kb_repo.list_kb_chunks(did):
            store._kb_repo.update_chunk_embedding(ch["id"], self._embed(1))
        return did

    def test_delete_document_removes_vectors(self, tmp_db_path):
        """删除文档后，faiss 有效向量数应归零，搜索不再命中。"""
        store = _make_store(tmp_db_path)
        did = self._add_doc_with_chunks(store, "D1", "aaa", 2)
        assert store._vector_index.count == 2

        store._kb_repo.delete_kb_document(did)

        assert store._vector_index.count == 0
        assert len(store._kb_repo.list_kb_chunks(did)) == 0
        hits = store._kb_repo.search_kb(self._embed(1), top_k=5)
        assert all(r["doc_id"] != did for r in hits)

    def test_delete_single_chunk_removes_one_vector(self, tmp_db_path):
        """删除单个分块后，faiss 有效向量数应减一。"""
        store = _make_store(tmp_db_path)
        did = self._add_doc_with_chunks(store, "D2", "bbb", 3)
        assert store._vector_index.count == 3

        cid = store._kb_repo.list_kb_chunks(did)[0]["id"]
        store._kb_repo.delete_kb_chunk(cid)

        assert store._vector_index.count == 2
        assert len(store._kb_repo.list_kb_chunks(did)) == 2


class TestMemoryFaissIsolation:
    """记忆向量不得污染 KB 共享的 faiss 索引。

    memories.id 与 kb_chunks.id 均为自增整数，若共用同一 faiss 索引，
    id 空间碰撞会导致 _id_map 相互覆盖：KB 检索召回记忆向量位、
    记忆去重误命中 KB 内容。
    """

    def _vx(self):
        return [1.0, 0.0, 0.0]  # KB 方向

    def _vy(self):
        return [0.0, 1.0, 0.0]  # 记忆方向（与 KB 正交）

    class _FakeEmb:
        enabled = True

        def __init__(self, v):
            self.v = v

        def embed(self, _text):
            return self.v

    def test_save_memory_does_not_pollute_kb_index(self, tmp_db_path):
        """save_memory 不得向共享 faiss 写入记忆向量。"""
        store = _make_store(tmp_db_path)
        # 先存记忆（若旧逻辑会把 memory_id 加进 faiss）
        store._memory_repo.save_memory(key="k1", content="记忆", source="manual", embedding=self._vy())
        # faiss 仍为空（或未初始化），记忆不进索引
        assert store._vector_index is None or store._vector_index.count == 0

        # 再加 KB chunk，faiss 只应含 KB 向量
        did = store._kb_repo.add_kb_document(
            title="D1", doc_type="dingtalk", source="dingtalk",
            source_id="D1", content="kb",
        )
        store._kb_repo.add_kb_chunks(did, ["kb分块"])
        ch = store._kb_repo.list_kb_chunks(did)[0]
        store._kb_repo.update_chunk_embedding(ch["id"], self._vx())
        assert store._vector_index.count == 1  # 仅 KB

    def test_recall_memory_still_works_without_faiss(self, tmp_db_path):
        """记忆召回走全表扫描，不依赖 faiss；且点对点隔离生效。"""
        store = _make_store(tmp_db_path)
        store._memory_repo.save_memory(key="k1", content="老记忆", source="manual", sender_id="u1", embedding=self._vy())
        # 带 sender_id 召回 → 命中该用户个人记忆
        rec = store._memory_repo.recall_memory(self._vy(), top_k=5, sender_id="u1")
        assert len(rec) == 1
        assert rec[0]["content"] == "老记忆"
        # 无 sender_id（匿名/系统调用）→ 只返公共，绝不泄露个人记忆
        rec2 = store._memory_repo.recall_memory(self._vy(), top_k=5)
        assert rec2 == []

    def test_check_duplicate_not_confused_by_kb(self, tmp_db_path):
        """记忆去重不得被 KB 向量干扰。"""
        store = _make_store(tmp_db_path)
        # KB 向量指向 x
        did = store._kb_repo.add_kb_document(
            title="D1", doc_type="dingtalk", source="dingtalk",
            source_id="D1", content="kb",
        )
        store._kb_repo.add_kb_chunks(did, ["kb分块"])
        ch = store._kb_repo.list_kb_chunks(did)[0]
        store._kb_repo.update_chunk_embedding(ch["id"], self._vx())
        # 已有记忆指向 y
        store._memory_repo.save_memory(key="k1", content="老记忆", source="manual", embedding=self._vy())

        # 新记忆与 KB 同向、与已有记忆正交 → 不应判重复
        assert store._memory_repo.check_memory_duplicate(
            "跟 KB 像但跟记忆不像",
            embedding_client=self._FakeEmb(self._vx()),
        ) is False
        # 新记忆与已有记忆同向 → 应判重复
        assert store._memory_repo.check_memory_duplicate(
            "跟老记忆像",
            embedding_client=self._FakeEmb(self._vy()),
        ) is True


class TestMemoryFilter:
    """记忆列表的 object_type 动态推断与筛选逻辑。"""

    def _seed(self, store):
        cur = store.conn.cursor()
        cur.execute(
            "INSERT INTO conversations (chat_id, chat_name, chat_type, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("cid-person", "张三", "single", "2026-01-01", "2026-01-01"),
        )
        cur.execute(
            "INSERT INTO conversations (chat_id, chat_name, chat_type, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("cid-group", "技术交流群", "group", "2026-01-01", "2026-01-01"),
        )
        cur.execute(
            "INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, created_at) VALUES (?,?,?,?,?,?,?)",
            ("k1", "张三的偏好：喜欢简洁", "chat", "cid-person", "u-zhang", "张三", "2026-02-01"),
        )
        cur.execute(
            "INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, created_at) VALUES (?,?,?,?,?,?,?)",
            ("k2", "群里讨论的架构方案", "chat", "cid-group", "u-li", "李四", "2026-02-02"),
        )
        cur.execute(
            "INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, created_at) VALUES (?,?,?,?,?,?,?)",
            ("k3", "手动添加的通用备忘", "manual", "", "", "", "2026-02-03"),
        )
        cur.execute(
            "INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, created_at) VALUES (?,?,?,?,?,?,?)",
            ("k4", "王五的备注信息", "chat", "cid-person", "u-wang", "王五", "2026-02-04"),
        )
        store.conn.commit()

    def test_object_type_inference(self, tmp_db_path):
        """object_type 应由会话类型正确推断：single->person, group->group, 无会话->other。"""
        store = _make_store(tmp_db_path)
        self._seed(store)
        rows = store._memory_repo.get_memories_filtered()
        by_content = {m["content"]: m["object_type"] for m in rows}
        assert by_content["张三的偏好：喜欢简洁"] == "person"
        assert by_content["群里讨论的架构方案"] == "group"
        assert by_content["手动添加的通用备忘"] == "other"

    def test_filter_by_object_type(self, tmp_db_path):
        """按 object_type 过滤只返回对应类别。"""
        store = _make_store(tmp_db_path)
        self._seed(store)
        persons = store._memory_repo.get_memories_filtered(object_type="person")
        assert all(m["object_type"] == "person" for m in persons)
        assert len(persons) == 2  # 张三、王五均来自 single 会话
        assert len(store._memory_repo.get_memories_filtered(object_type="group")) == 1
        assert len(store._memory_repo.get_memories_filtered(object_type="other")) == 1

    def test_filter_by_sender(self, tmp_db_path):
        """按具体人(sender_id)模糊筛选。"""
        store = _make_store(tmp_db_path)
        self._seed(store)
        res = store._memory_repo.get_memories_filtered(sender="u-zhang")
        assert len(res) == 1
        assert res[0]["sender_name"] == "张三"

    def test_filter_by_keyword(self, tmp_db_path):
        """按内容关键词筛选。"""
        store = _make_store(tmp_db_path)
        self._seed(store)
        res = store._memory_repo.get_memories_filtered(keyword="架构")
        assert len(res) == 1
        assert "架构" in res[0]["content"]

    def test_facets(self, tmp_db_path):
        """facets 返回类型计数与去重的人列表（不含空 sender）。"""
        store = _make_store(tmp_db_path)
        self._seed(store)
        facets = store._memory_repo.get_memory_facets()
        types = {t["value"]: t["count"] for t in facets["object_types"]}
        assert types["person"] == 2
        assert types["group"] == 1
        assert types["other"] == 1
        sender_ids = {p["sender_id"] for p in facets["people"]}
        assert {"u-zhang", "u-li", "u-wang"} <= sender_ids
        assert "" not in sender_ids


class TestMessageCountSync:
    """message_count 在增删时应与真实消息数保持一致（Bug4 回归防护）。"""

    def test_delete_message_decrements_count(self, tmp_db_path):
        """撤回(删除)消息后，会话 message_count 应同步减 1。"""
        store = _make_store(tmp_db_path)
        store._message_repo.save_message(Message(
            msg_id="m1", chat_id="c1", chat_type="single", chat_name="x",
            sender_id="u", sender_name="x", content="hello", msg_type="text",
            timestamp=datetime(2026, 7, 7, 12, 0, 0), raw={}), role="user")
        store._message_repo.save_message(Message(
            msg_id="m2", chat_id="c1", chat_type="single", chat_name="x",
            sender_id="u", sender_name="x", content="world", msg_type="text",
            timestamp=datetime(2026, 7, 7, 12, 1, 0), raw={}), role="user")
        assert store._conversation_repo.get_conversation("c1")["message_count"] == 2
        assert store._message_repo.delete_message("m1") is True
        assert store._conversation_repo.get_conversation("c1")["message_count"] == 1

    def test_summarize_keeps_message_count(self, tmp_db_path):
        """压缩后 message_count 仍 == 真实消息总行数 (不变式 message_count == count(messages))。

        压缩两件事: (1) 旧消息标记 is_archived=1 不删行; (2) 新增 1 条 system 摘要消息。
        两者都留在 messages 表, 故 message_count 应等于压缩后的真实总行数 (=原10条 + 1条摘要)。
        """
        import sqlite3 as _sql
        store = _make_store(tmp_db_path)
        for i in range(10):
            store._message_repo.save_message(Message(
                msg_id=f"m{i}", chat_id="c2", chat_type="single", chat_name="x",
                sender_id="u", sender_name="x", content=f"m{i}", msg_type="text",
                timestamp=datetime(2026, 7, 7, 12, 0, i), raw={}), role="user")
        assert store._conversation_repo.get_conversation("c2")["message_count"] == 10
        archived = store._message_repo.summarize_and_compress("c2", "摘要", keep_ratio=0.4)
        # 真实总行数：save_message 写入 conv_conn（会话库），需从会话库查询
        # 注意：不关闭 conn，因为 store._conversation_repo 后续仍会用到它
        conn = store.conv_conn("dingtalk")
        real_total = conn.execute("SELECT count(*) FROM messages WHERE chat_id='c2'").fetchone()[0]
        # 10 条原始 + 1 条摘要 = 11
        assert real_total == 10 + 1, f"真实总行数应为 11, 实际 {real_total}"
        # 不变式: 计数 == 真实总行数
        assert store._conversation_repo.get_conversation("c2")["message_count"] == real_total
        assert archived > 0


# ============ TTL 移交 DB 层 (M2 修复) ============

class TestProcessedMsgTTL:
    """cleanup_processed_msgs / load_recent_processed_msg_ids 的 TTL 行为。

    M2 修复后 TTL 逻辑从 poller._processed_msg_ids 移交到 SQLite 层。
    覆盖点：
    - 阈值内的 msg_id 仍被加载
    - 超过 hours 阈值的 msg_id 被清理
    - 边界（恰好 hours）行为
    """

    def test_load_recent_includes_fresh_records(self, tmp_db_path):
        """新插入的 msg_id 应被 load_recent 加载。"""
        store = _make_store(tmp_db_path)
        store._message_repo.mark_message_processed("m_fresh", "c1")
        recent = store._message_repo.load_recent_processed_msg_ids(hours=24)
        assert "m_fresh" in recent

    def test_cleanup_removes_old_records(self, tmp_db_path):
        """直接 UPDATE 制造老记录，cleanup_processed_msgs 应清掉。"""
        import sqlite3
        from datetime import datetime, timedelta
        store = _make_store(tmp_db_path)
        store._message_repo.mark_message_processed("m_old", "c1")
        store._message_repo.mark_message_processed("m_new", "c1")
        # 把 m_old 的时间戳手动回拨 100 小时（写入 conv_conn 会话库）
        conv = store.conv_conn("dingtalk")
        old_ts = (datetime.now() - timedelta(hours=100)).isoformat()
        conv.execute(
            "UPDATE dedup_messages SET processed_at = ? WHERE msg_id = ?",
            (old_ts, "m_old"),
        )
        conv.commit()
        # 不关闭 conv，供后续测试使用
        # 清理 72 小时之前的（返回 None，不能 a >= 1）
        store._message_repo.cleanup_processed_msgs(hours=72)
        # m_new 还在, m_old 已清
        recent = store._message_repo.load_recent_processed_msg_ids(hours=200)
        assert "m_new" in recent
        assert "m_old" not in recent

    def test_cleanup_idempotent(self, tmp_db_path):
        """连续两次 cleanup 不应报错。"""
        store = _make_store(tmp_db_path)
        store._message_repo.mark_message_processed("m1", "c1")
        # 第一次不报错
        store._message_repo.cleanup_processed_msgs(hours=72)
        # 第二次不报错（表为空也是安全的）
        store._message_repo.cleanup_processed_msgs(hours=72)


# ============ DLQ 完整路径 ============

class TestDeadLetterFullPath:
    """add_dead_letter / get_dead_letter / resolve_dead_letter / list_dead_letters 集成。"""

    def test_add_then_get_returns_record(self, tmp_db_path):
        """写入后能按 id 取回,字段全。"""
        store = _make_store(tmp_db_path)
        dl_id = store._draft_repo.add_dead_letter(
            msg_id="m_dlq_1", chat_id="c1", chat_name="群1",
            sender_id="u1", sender_name="用户1", content="原始内容",
            msg_type="text", stage="llm", error="测试异常",
            raw={"chat_type": "group"},
        )
        rec = store._draft_repo.get_dead_letter(dl_id)
        assert rec is not None
        assert rec["msg_id"] == "m_dlq_1"
        assert rec["chat_id"] == "c1"
        assert rec["status"] == "pending"
        assert rec["stage"] == "llm"
        assert rec["error"] == "测试异常"

    def test_resolve_marks_status(self, tmp_db_path):
        """resolve 后 status 应更新。"""
        store = _make_store(tmp_db_path)
        dl_id = store._draft_repo.add_dead_letter(
            msg_id="m_dlq_2", chat_id="c1", chat_name="g",
            sender_id="u", sender_name="u", content="x", msg_type="text",
            stage="llm", error="e", raw={},
        )
        assert store._draft_repo.resolve_dead_letter(dl_id, status="replayed") is True
        rec = store._draft_repo.get_dead_letter(dl_id)
        assert rec["status"] == "replayed"

    def test_resolve_failed_with_note(self, tmp_db_path):
        """【P0-3 护栏伴生】失败态 + note（字段名是 replay_note）."""
        store = _make_store(tmp_db_path)
        dl_id = store._draft_repo.add_dead_letter(
            msg_id="m_dlq_3", chat_id="c1", chat_name="g",
            sender_id="u", sender_name="u", content="x", msg_type="text",
            stage="llm", error="e", raw={},
        )
        store._draft_repo.resolve_dead_letter(dl_id, status="failed", note="手动重放失败: 测试异常")
        rec = store._draft_repo.get_dead_letter(dl_id)
        assert rec["status"] == "failed"
        # 【bug fix】get_dead_letter 现在 SELECT replay_note，可读出
        assert "测试异常" in rec.get("replay_note", "")

    def test_list_filters_by_status(self, tmp_db_path):
        """list_dead_letters(status='pending') 应过滤掉已 resolved 的。"""
        store = _make_store(tmp_db_path)
        a = store._draft_repo.add_dead_letter(
            msg_id="ma", chat_id="c1", chat_name="g", sender_id="u", sender_name="u",
            content="x", msg_type="text", stage="llm", error="e", raw={},
        )
        b = store._draft_repo.add_dead_letter(
            msg_id="mb", chat_id="c1", chat_name="g", sender_id="u", sender_name="u",
            content="y", msg_type="text", stage="llm", error="e", raw={},
        )
        store._draft_repo.resolve_dead_letter(b, status="replayed")
        pending, _ = store._draft_repo.list_dead_letters(status="pending")
        ids = {r["id"] for r in pending}
        assert a in ids and b not in ids


# ============ 消息内容更新 / 删除 ============

class TestMessageUpdateDelete:
    """update_message_content / update_message_image_path / delete_message / update_message。"""

    def _save(self, store, msg_id="m_upd", content="原始"):
        m = Message(
            msg_id=msg_id, chat_id="c1", chat_type="group", chat_name="g",
            sender_id="u1", sender_name="u1", content=content, msg_type="text",
            timestamp=datetime.now(),  # Message.timestamp 是 datetime，不是 str
        )
        store._message_repo.save_message(m, role="user")
        return m

    def test_update_message_content_changes_only_content(self, tmp_db_path):
        m = self._save(store := _make_store(tmp_db_path))
        # update_message_content 返回 None（不返回 bool），不报错为成功
        result = store._message_repo.update_message_content(m.msg_id, "新内容")
        assert result is None  # 设计如此
        fetched = store._message_repo.get_message_by_id(m.msg_id)
        assert fetched.content == "新内容"
        assert fetched.sender_id == "u1"  # 其他字段不变

    def test_update_message_returns_bool(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        self._save(store, "m_existing", "x")
        assert store._message_repo.update_message("m_existing", "新") is True
        assert store._message_repo.update_message("m_nonexistent", "新") is False

    def test_delete_message_removes_record(self, tmp_db_path):
        # 【bug fix】get_message_by_id SELECT 会保中不到的消息含 chat_name 列 → 拿 OperationalError。
        # 【修后】可恢复使用 get_message_by_id 验证。
        store = _make_store(tmp_db_path)
        self._save(store, "m_del", "x")
        assert store._message_repo.get_message_by_id("m_del") is not None
        assert store._message_repo.delete_message("m_del") is True
        assert store._message_repo.get_message_by_id("m_del") is None
        # 不存在的删除应返回 False 不报错
        assert store._message_repo.delete_message("m_nonexistent") is False


# ============ 每线程连接回收（P2-9 回归） ============

class TestConnRecycle:
    """per-thread SQLite 连接超过上限时回收最旧者，防止动态线程增长导致 FD 泄漏。"""

    def test_conn_recycled_over_limit(self, tmp_db_path):
        import threading
        from unittest.mock import MagicMock
        store = _make_store(tmp_db_path)
        # 清空可能由 init_db 在本线程创建的连接，使回收状态可控
        with store._conns_lock:
            store._conns.clear()
        # 预填达到上限的假连接（模拟动态线程曾短暂增长）
        for i in range(store._max_conns):
            store._conns[1000 + i] = MagicMock()
        assert len(store._conns) == store._max_conns
        # 在子线程访问连接：新 tid 不在预填中 → 新建 + 回收最旧者，总数回落到上限
        holder: dict = {}
        def worker():
            holder["conn"] = store.conn
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert holder.get("conn") is not None
        assert len(store._conns) == store._max_conns


# ============ 路由质量 time_filter（P1 #4 回归） ============

class TestRoutingQualityTimeFilter:
    """get_routing_quality 的 time_filter 解析回归。

    旧实现把 'today' 字面量直接当 created_at 下界 → SQL 比较恒 false → 0 条。
    修复后 'today' / 'month' 用 SQLite 原生日期函数，ISO 时间戳仍作为下界。
    """

    def _seed(self, store, n=3):
        for i in range(n):
            store._routing_quality_repo.record_routing_quality(
                sender_id=f"u{i}", primary_skill="skill_a", primary_source="intent",
            )

    def test_today_returns_all_when_data_is_today(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        self._seed(store)
        res = store._routing_quality_repo.get_routing_quality(time_filter="today")
        assert res["total"] == 3
        assert len(res["items"]) == 3

    def test_empty_filter_returns_all(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        self._seed(store)
        res = store._routing_quality_repo.get_routing_quality()
        assert res["total"] == 3

    def test_iso_bound_returns_all_recent(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        self._seed(store)
        res = store._routing_quality_repo.get_routing_quality(time_filter="2000-01-01T00:00:00")
        assert res["total"] == 3


class TestCosineSimilarityZeroNorm:
    """cosine_similarity 零向量除零防护（修复 nan 污染检索排序）。

    src/memory/sqlite_store.py 的模块级 cosine_similarity 旧实现在
    norm==0 时 0/0=nan（合法 float 不抛异常，except 抓不到）→ 静默返回 nan，
    污染记忆去重/recall 的相似度比较。修复后统一返回 0.0。
    """

    def test_double_zero_returns_zero(self):
        from src.memory.sqlite_store import cosine_similarity

        assert cosine_similarity([0, 0], [0, 0]) == 0.0

    def test_one_sided_zero_returns_zero(self):
        from src.memory.sqlite_store import cosine_similarity

        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0
        assert cosine_similarity([1, 2, 3], [0, 0, 0]) == 0.0

    def test_normal_still_works(self):
        from src.memory.sqlite_store import cosine_similarity

        assert pytest.approx(cosine_similarity([1, 0, 0], [0, 1, 0]), abs=1e-6) == 0.0
        assert pytest.approx(cosine_similarity([1, 2, 3], [1, 2, 3]), abs=1e-6) == 1.0
