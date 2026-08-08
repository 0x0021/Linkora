"""2026-08-08 性能修复验证（H1/H2/H3/H4）。

- H1：Web 异步端点事件循环阻塞 → DWS / subprocess 调用移出事件循环（run_in_threadpool）。
- H2：消息热路径门控重复查库 → _reply_gate_reason 复用前置已算的接管/在场标志。
- H3：飞书每会话 subprocess 重复调用 → _feishu_correct_chat_type 按 conv_id 单轮缓存。
- H4：记忆召回/去重全表余弦 → 加按 created_at 倒序的候选上限，避免全表扫描。
"""
from __future__ import annotations

import json
import types
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import PollerConfig
from src.memory.memory_repo import MemoryRepo, _MEMORY_CANDIDATE_CAP
from src.memory.sqlite_store import SQLiteStore
from src.platform.runtime_inbound import InboundMixin
from src.poller import MessagePoller

# 预导入 web.api：它会在初始化时 import 各 router，先完整加载可打破循环导入。
import web.api  # noqa: E402,F401

from web.routers.conversations import _resolve_current_user, router as conv_router  # noqa: E402
from web.routers.orgs import router as orgs_router  # noqa: E402
from web.routers.status import _resolve_user_name, _get_git_info, router as status_router  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# H1：Web 异步端点事件循环阻塞
# ─────────────────────────────────────────────────────────────────────────────

def _make_adapter(user_name: str = "", contact_name: str = ""):
    adapter = MagicMock()
    adapter._get_current_profile_local.return_value = (
        {"userName": user_name, "openDingTalkId": "ou_x"} if user_name else {}
    )
    if contact_name:
        adapter.contact_user_get_self.return_value = {
            "orgEmployeeModel": {"orgUserName": contact_name}, "name": contact_name
        }
    else:
        adapter.contact_user_get_self.return_value = {}
    return adapter


def _make_inst(adapter):
    inst = MagicMock()
    inst.platforms = {"dingtalk": MagicMock(dws=adapter)}
    inst.get_poller_status.return_value = {"platforms": {}}
    return inst


class TestResolveUserNameHelper:
    def test_from_profile(self):
        inst = _make_inst(_make_adapter(user_name="张三"))
        with patch("web.routers.status.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.status.get_app_instance", return_value=inst):
            assert _resolve_user_name() == "张三"

    def test_from_contact_fallback(self):
        inst = _make_inst(_make_adapter(contact_name="王五"))
        with patch("web.routers.status.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.status.get_app_instance", return_value=inst):
            assert _resolve_user_name() == "王五"

    def test_token_verify_failed(self):
        # 外层异常（如 get_app_instance 抛 TOKEN_VERIFIED_FAILED）→ "个人用户"
        with patch("web.routers.status.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.status.get_app_instance",
                      side_effect=Exception("TOKEN_VERIFIED_FAILED xxx")):
            assert _resolve_user_name() == "个人用户"


class TestResolveCurrentUserHelper:
    def test_from_profile(self):
        inst = _make_inst(_make_adapter(user_name="赵六"))
        with patch("web.routers.conversations.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.conversations.get_app_instance", return_value=inst):
            uid, name = _resolve_current_user()
        assert uid == "ou_x"
        assert name == "赵六"


def _status_app():
    app = FastAPI()
    app.include_router(status_router)
    return TestClient(app)


def _build_cfg():
    cfg = MagicMock()
    cfg.dws.dry_run = False
    cfg.poller.interval_seconds = 5
    cfg.llm.model = "m"
    cfg.embedding.enabled = False
    cfg.embedding.model = ""
    cfg.tools.available = []
    return cfg


def _mock_store_zero():
    store = MagicMock()
    store._message_repo.count_messages.return_value = 0
    store._conversation_repo.count_conversations.return_value = 0
    store._memory_repo.count_memories.return_value = 0
    store.count_keyword_rules.return_value = 0
    store._kb_repo.count_kb_documents.return_value = 0
    store._draft_repo.count_dead_letters.return_value = 0
    return store


class TestStatusEndpoint:
    def test_returns_user_and_version_off_event_loop(self):
        cfg = _build_cfg()
        inst = _make_inst(_make_adapter(user_name="张三"))
        store = _mock_store_zero()
        client = _status_app()
        with patch("web.api._require_cfg", return_value=cfg), \
                patch("web.api.get_store", return_value=store), \
                patch("web.routers.status.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.status.get_app_instance", return_value=inst), \
                patch("web.routers.status._get_git_info",
                      return_value={"commit": "abc123", "branch": "main"}):
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["name"] == "张三"
        assert data["version"] == {"commit": "abc123", "branch": "main"}

    def test_git_info_cached(self):
        # lru_cache(maxsize=1)：第二次调用不重新执行 subprocess（每次内部 2 次 check_output）
        with patch("web.routers.status.subprocess.check_output",
                   return_value="x") as mock_co:
            _get_git_info()  # 首次：2 次 check_output
            _get_git_info()  # 命中缓存：0 次 check_output
        assert mock_co.call_count == 2


class TestConversationsEndpoint:
    def test_messages_returns_current_user(self):
        inst = _make_inst(_make_adapter(user_name="赵六"))
        store = MagicMock()
        store._message_repo.list_messages_with_chat_name.return_value = []
        app = FastAPI()
        app.include_router(conv_router)
        client = TestClient(app)
        with patch("web.routers.conversations._api.get_store", return_value=store), \
                patch("web.routers.conversations.get_current_platform", return_value="dingtalk"), \
                patch("web.routers.conversations.get_app_instance", return_value=inst):
            resp = client.get("/api/messages?chat_id=c1&limit=10")
        assert resp.status_code == 200
        assert resp.json()["current_user_name"] == "赵六"


class TestOrgsEndpoint:
    def test_list_orgs_off_event_loop(self):
        poller = MagicMock()
        poller.dws.list_orgs.return_value = [{"orgId": "1"}]
        poller.current_org = "1"
        poller.target_org_corp_id = ""
        poller._inaccessible_conversations = set()
        inst = MagicMock()
        inst.poller = poller
        app = FastAPI()
        app.include_router(orgs_router)
        client = TestClient(app)
        with patch("web.routers.orgs.get_app_instance", return_value=inst):
            resp = client.get("/api/orgs")
        assert resp.status_code == 200
        assert resp.json()["orgs"] == [{"orgId": "1"}]


# ─────────────────────────────────────────────────────────────────────────────
# H2：消息热路径门控重复查库
# ─────────────────────────────────────────────────────────────────────────────

def _make_gate_fake():
    """构造一个只有 _reply_gate_reason 真实逻辑、其余门控方法为计数桩的对象。

    直接复用生产代码 InboundMixin._reply_gate_reason（不重写），通过实例方法
    替换桩函数，验证「传入 taken_over/owner_present 后不再重复查库」。
    """
    f = InboundMixin.__new__(InboundMixin)
    f.config = MagicMock()
    f.config.poller.suppress_when_owner_read = False
    f.call_counts = {"self": 0, "taken": 0, "present": 0}

    def _self(self, m):
        f.call_counts["self"] += 1
        return False

    def _taken(self, m):
        f.call_counts["taken"] += 1
        return False

    def _present(self, m):
        f.call_counts["present"] += 1
        return False

    def _read(self, m):
        return False

    f._is_message_from_self = types.MethodType(_self, f)
    f._has_user_taken_over = types.MethodType(_taken, f)
    f._is_owner_present = types.MethodType(_present, f)
    f._owner_conversation_is_read = types.MethodType(_read, f)
    return f


class TestReplyGateDedup:
    def test_recomputes_when_flags_absent(self):
        f = _make_gate_fake()
        assert f._reply_gate_reason(MagicMock()) is None
        # 无传入标志：gate 自行各算一次
        assert f.call_counts["taken"] == 1
        assert f.call_counts["present"] == 1

    def test_reuses_passed_flags(self):
        f = _make_gate_fake()
        # 传入 taken_over=True → 应立即命中"人工接管"，且不再调用 _has_user_taken_over/_is_owner_present
        reason = f._reply_gate_reason(MagicMock(), taken_over=True, owner_present=False)
        assert reason == "人工已接管（消息后已手动回复）"
        assert f.call_counts["taken"] == 0
        assert f.call_counts["present"] == 0

    def test_reuses_passed_owner_present(self):
        f = _make_gate_fake()
        reason = f._reply_gate_reason(MagicMock(), taken_over=False, owner_present=True)
        assert reason == "真人当前在场"
        assert f.call_counts["taken"] == 0
        assert f.call_counts["present"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# H3：飞书每会话 subprocess 重复调用
# ─────────────────────────────────────────────────────────────────────────────

class FeishuCliAdapter(MagicMock):
    """同名假适配器——策略层按 ``type(self.dws).__name__`` 判定平台分支。"""

    def chat_conversation_info(self, conv_id):  # noqa: D401
        return {"chat_mode": "group"}


@pytest.fixture
def poller_factory(tmp_db_path):
    """本地副本：pytest fixture 不跨文件自动共享（与 test_poller_strategy.py 同构）。"""
    created = []

    def _make(dws=None):
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
        store.init_db()
        p = MessagePoller(
            config=config,
            dws=dws if dws is not None else MagicMock(),
            store=store,
            current_user_id="user-001",
            current_user_name="测试用户",
        )
        created.append(store)
        return p, store

    yield _make
    for s in created:
        try:
            s.close()
        except Exception:
            pass


@pytest.fixture
def feishu_poller(poller_factory):
    dws = FeishuCliAdapter()
    dws.sync_external_contacts.return_value = []
    # chat_conversation_info 是 subprocess CLI，替换为可计数的 Mock 以验证缓存
    dws.chat_conversation_info = MagicMock(return_value={"chat_mode": "group"})
    p, store = poller_factory(dws)
    return p, store, dws


class TestFeishuChatTypeCache:
    def test_same_conv_id_calls_cli_once(self, feishu_poller):
        p, _store, dws = feishu_poller
        r1 = p._feishu_correct_chat_type("cid1", "群1", "single")
        r2 = p._feishu_correct_chat_type("cid1", "群1", "single")
        assert r1 == "group"
        assert r2 == "group"
        # H3 核心：同一 conv_id 单轮内只打一次 subprocess CLI
        assert dws.chat_conversation_info.call_count == 1

    def test_different_conv_id_hits_cli_again(self, feishu_poller):
        p, _store, dws = feishu_poller
        p._feishu_correct_chat_type("cid1", "群1", "single")
        p._feishu_correct_chat_type("cid2", "群2", "single")
        assert dws.chat_conversation_info.call_count == 2

    def test_cache_cleared_each_poll_round(self, feishu_poller):
        p, _store, dws = feishu_poller
        p._feishu_correct_chat_type("cid1", "群1", "single")
        assert dws.chat_conversation_info.call_count == 1
        # 模拟进入新一轮：poll_once 开头清空缓存
        p._feishu_conv_info_cache = {}
        p._feishu_correct_chat_type("cid1", "群1", "single")
        assert dws.chat_conversation_info.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# H4：记忆召回/去重全表余弦 → 加候选上限
# ─────────────────────────────────────────────────────────────────────────────

class TestRecallMemoryLimit:
    def test_sql_has_recency_limit_and_cap(self):
        store = MagicMock()
        cur = MagicMock()
        store.conn.cursor.return_value = cur
        cur.fetchall.return_value = [{
            "id": 1, "content": "x", "source": "auto", "chat_id": "c",
            "sender_id": "", "sender_name": "", "embedding": json.dumps([1.0, 0, 0, 0]),
            "created_at": "t", "scope": "public",
        }]
        repo = MemoryRepo(store)
        out = repo.recall_memory(query_embedding=[1.0, 0, 0, 0], top_k=5, sender_id="")
        sql = cur.execute.call_args[0][0]
        assert "ORDER BY created_at DESC LIMIT ?" in sql
        assert _MEMORY_CANDIDATE_CAP in cur.execute.call_args[0][1]
        assert len(out) == 1
        assert out[0]["content"] == "x"

    def test_correctness_small_table(self, tmp_db_path):
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()
        repo = store._memory_repo
        now = datetime.now().isoformat()
        rows = [
            ("k1", "alpha", [1.0, 0, 0, 0]),
            ("k2", "beta", [0.0, 1.0, 0, 0]),
            ("k3", "gamma", [0.0, 0.0, 1.0, 0]),
        ]
        cur = store.conn.cursor()
        for key, content, emb in rows:
            cur.execute(
                "INSERT INTO memories (key,content,source,chat_id,sender_id,sender_name,embedding,created_at,scope) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (key, content, "auto", "c1", "", "", json.dumps(emb), now, "public"),
            )
        store.conn.commit()
        # 候选上限远大于表规模，行为不变：返回与 query 最相似的 alpha
        results = repo.recall_memory(query_embedding=[1.0, 0, 0, 0], top_k=2, sender_id="")
        assert results[0]["content"] == "alpha"
        assert len(results) == 2


class TestCheckMemoryDuplicateLimit:
    def test_semantic_sql_has_recency_limit(self):
        store = MagicMock()
        cur = MagicMock()
        store.conn.cursor.return_value = cur
        cur.fetchone.return_value = None  # 无完全匹配
        cur.fetchall.return_value = [{"embedding": json.dumps([1.0, 0, 0, 0])}]
        repo = MemoryRepo(store)
        emb = MagicMock()
        emb.enabled = True
        emb.embed.return_value = [1.0, 0, 0, 0]
        dup = repo.check_memory_duplicate(
            "new content", embedding_client=emb, similarity_threshold=0.85,
            sender_id="s1", scope="public")
        assert dup is True  # cosine=1.0 >= 0.85
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        assert any("ORDER BY created_at DESC LIMIT ?" in s for s in sqls)
