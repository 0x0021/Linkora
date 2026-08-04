"""
H2-A 后台异步摘要（SummaryScheduler）集成测试。

覆盖 4 个场景（详见 docs/sequence-diagram.mermaid）：
① 异步不阻塞主回复（计时断言）——schedule() 是同步非阻塞，主回复链路立即返回；
② 失败后不脏写（DB 无新行）——summarize_conversation 返回空则不写库；
③ 同 chat 并发不双写（pending 去重 + generation CAS）——rapid 两次 schedule 仅入队一次；
④ 缓存命中 / 降级路径——_apply_history_tiering 读缓存命中复用、未命中降级并异步补算。

运行需 KMP_DUPLICATE_LIB_OK=TRUE（macOS 导入 torch）。
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LlmConfig
from src.llm.agent import LLMAgent
from src.llm.summary_scheduler import SummaryJob, SummaryScheduler
from src.memory.sqlite_store import ConversationSummaryRow, SQLiteStore
from src.models import Message


def _msg(content: str, idx: int) -> Message:
    return Message(
        msg_id=f"m{idx}",
        chat_id="chat_async",
        chat_type="single",
        chat_name=None,
        sender_id="u1",
        sender_name="小明",
        content=content,
        msg_type="text",
        timestamp=datetime(2024, 1, 1, 10, 0, 0) + timedelta(minutes=idx),
        role="user",
    )


class _FakeClient:
    """可编排的假 LLMClient：默认返回确定性摘要；可切换为失败（返回空）。"""

    def __init__(self, summary_text: str = "这是一段对话摘要"):
        self.summary_text = summary_text
        self.calls = []

    def chat(self, messages, temperature=0.1):
        self.calls.append(messages)
        # 返回对象需含 .content 属性，复刻 LLMResponse 的最小接口
        class _R:
            content = self.summary_text
        return _R()


class _SummaryAgent(LLMAgent):
    """测试用 agent：用可编排假 client 替换真实 LLMClient，便于断言摘要是否被调用。"""

    def __init__(self, summary_text: str = "这是一段对话摘要", store=None):
        adv = LlmConfig().advanced
        super().__init__(
            config=LlmConfig(advanced=adv),
            client=_FakeClient(summary_text=summary_text),
            tool_router=None,
            store=store,
        )


def _make_scheduler(tmp_path: Path, summary_text: str = "这是一段对话摘要") -> tuple[SummaryScheduler, SQLiteStore, LLMAgent]:
    db_path = str(tmp_path / "test_async.db")
    store = SQLiteStore(db_path=db_path)  # 触发 init_db（含 conversation_summaries 建表）
    agent = _SummaryAgent(summary_text=summary_text, store=store)
    scheduler = SummaryScheduler(agent=agent, store=store)
    agent._summary_scheduler = scheduler
    return scheduler, store, agent


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询 predicate 直到为 True 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestAsyncNonBlocking:
    """场景①：异步不阻塞主回复。"""

    def test_schedule_does_not_block(self, tmp_path):
        scheduler, store, agent = _make_scheduler(tmp_path)
        scheduler.start()
        try:
            older = [_msg(f"旧消息{i}", i) for i in range(5)]
            t0 = time.time()
            # schedule 是同步非阻塞调用（仅 queue.put），不应触发 LLM、不应耗时
            scheduler.schedule("chat_async", older)
            elapsed = time.time() - t0
            # 即便背后要算摘要，schedule 自身应在毫秒级返回（不等待 LLM）
            assert elapsed < 1.0, f"schedule() 不应阻塞主回复，实际耗时 {elapsed:.3f}s"
            # 确认后台确实处理了该任务（摘要被调用）
            assert _wait_until(lambda: len(agent.client.calls) >= 1)
        finally:
            scheduler.stop()


class TestFailureNoDirtyWrite:
    """场景②：摘要失败不脏写（DB 无新行）。"""

    def test_empty_summary_not_written(self, tmp_path):
        # 用空摘要模拟 LLM 失败/超时
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="")
        scheduler.start()
        try:
            older = [_msg(f"旧消息{i}", i) for i in range(5)]
            before = store._conversation_repo.get_conversation_summary("chat_async")
            assert before is None, "前置：DB 无缓存行"
            scheduler.schedule("chat_async", older)
            # 等待后台处理完成（摘要调用发生且返回空）
            assert _wait_until(lambda: len(agent.client.calls) >= 1)
            # 再等一小会儿确保 worker 已处理完该 job
            time.sleep(0.2)
            after = store._conversation_repo.get_conversation_summary("chat_async")
            assert after is None, "摘要失败（空串）时不应写库，避免脏数据"
        finally:
            scheduler.stop()

    def test_success_summary_written(self, tmp_path):
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="对话摘要文本")
        scheduler.start()
        try:
            older = [_msg(f"旧消息{i}", i) for i in range(5)]
            scheduler.schedule("chat_async", older)
            assert _wait_until(
                lambda: store._conversation_repo.get_conversation_summary("chat_async") is not None
            ), "摘要成功时应写回 DB"
            row = store._conversation_repo.get_conversation_summary("chat_async")
            assert row.summary_text == "对话摘要文本"
            assert row.covered_count == 5
            assert row.generation == 1
        finally:
            scheduler.stop()


class TestNoDoubleWriteSameChat:
    """场景③：同 chat 并发不双写（pending 去重 + generation CAS）。"""

    def test_rapid_duplicate_schedule_writes_once(self, tmp_path):
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="摘要A")
        scheduler.start()
        try:
            older = [_msg(f"旧消息{i}", i) for i in range(5)]
            # 几乎同时两次入队同一 chat（模拟并发两回合）
            scheduler.schedule("chat_async", older)
            scheduler.schedule("chat_async", older)
            # 第二次应被 pending 去重跳过 → 队列里只有 1 个 job
            assert scheduler._queue.qsize() <= 1, "同 chat 在途期间应只入队一次"
            # 等待处理完成
            assert _wait_until(
                lambda: store._conversation_repo.get_conversation_summary("chat_async") is not None
            )
            row = store._conversation_repo.get_conversation_summary("chat_async")
            assert row.generation == 1, "同 chat 应只成功写回一次（代际=1）"
            assert len(agent.client.calls) == 1, "同 chat 只应算一次摘要"
        finally:
            scheduler.stop()


class TestCacheHitAndDegrade:
    """场景④：缓存命中复用 / 未命中降级并异步补算。"""

    def test_cache_hit_returns_summary_message(self, tmp_path):
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="缓存命中摘要")
        # 先写一条新鲜且覆盖充分的缓存摘要（covered_count >= older 长度 * 0.6）
        store._conversation_repo.upsert_conversation_summary(
            chat_id="chat_async",
            summary="缓存命中摘要",
            older_boundary_msg_id="m0",
            covered_count=10,
            expected_generation=0,
        )
        history = [_msg(f"消息{i}", i) for i in range(10)]  # 10 条 > max_recent(6)
        out = agent._apply_history_tiering(history)
        # 缓存命中：返回 [summary_msg] + recent(6) = 7 条
        assert len(out) == 7
        assert out[0].role == "system"
        assert out[0].content == "[摘要]缓存命中摘要"
        # 命中缓存时不应触发后台调度（pending 为空）
        assert scheduler._pending == set(), "缓存命中不应再调度后台补算"

    def test_cache_miss_degrades_and_schedules(self, tmp_path):
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="后台补算摘要")
        scheduler.start()
        try:
            history = [_msg(f"消息{i}", i) for i in range(10)]  # 10 条 > max_recent(6)
            out = agent._apply_history_tiering(history)
            # 缓存未命中：降级为仅 recent(6)
            assert len(out) == 6
            assert out == history[-6:]
            # 且异步调度了后台补算（pending 含该 chat）
            assert "chat_async" in scheduler._pending, "缓存未命中应触发后台异步补算"
            # 后台写回成功
            assert _wait_until(
                lambda: store._conversation_repo.get_conversation_summary("chat_async") is not None
            )
            row = store._conversation_repo.get_conversation_summary("chat_async")
            assert row.summary_text == "后台补算摘要"
        finally:
            scheduler.stop()

    def test_cache_expired_falls_back_to_degrade(self, tmp_path):
        """过期缓存（> summary_max_age_seconds）应视为未命中 → 降级并补算。"""
        scheduler, store, agent = _make_scheduler(tmp_path, summary_text="新摘要")
        scheduler.start()
        try:
            # 写入一条「过期」缓存：updated_at 在窗口外（修改 generation 触发 updated_at 刷新，
            # 故直接构造过期行：先写当前，再手工 UPDATE 旧时间）
            store._conversation_repo.upsert_conversation_summary(
                chat_id="chat_async", summary="旧摘要", older_boundary_msg_id="m0",
                covered_count=10, expected_generation=0,
            )
            # 把 updated_at 拨回到 1 小时前，制造过期。
            # 注意：会话摘要表位于 conv_conn（按平台隔离），而非主库 store.conn；
            # 必须在 repo 实际读取的连接上执行 UPDATE，否则写到了另一个库、过期不生效。
            conn = store._conversation_repo._cc("")
            cur = conn.cursor()
            old = (datetime.now() - timedelta(hours=1)).isoformat()
            cur.execute(
                "UPDATE conversation_summaries SET updated_at=? WHERE chat_id=?",
                (old, "chat_async"),
            )
            conn.commit()

            history = [_msg(f"消息{i}", i) for i in range(10)]
            out = agent._apply_history_tiering(history)
            assert len(out) == 6, "过期缓存应降级为 recent 仅"
            assert _wait_until(
                lambda: (store._conversation_repo.get_conversation_summary("chat_async") is not None
                         and store._conversation_repo.get_conversation_summary("chat_async").summary_text == "新摘要")
            ), "过期后应在后台用新摘要补算写回"
        finally:
            scheduler.stop()


class TestSummaryJobDataclass:
    """SummaryJob 数据结构自检（CAS 代际字段存在）。"""

    def test_job_carries_generation(self):
        job = SummaryJob(chat_id="c1", older=[_msg("x", 0)], generation=3, created_at="t")
        assert job.chat_id == "c1"
        assert job.generation == 3
        assert isinstance(job.older, list)

    def test_conversation_row_dataclass(self):
        row = ConversationSummaryRow(
            chat_id="c1", summary_text="s", older_boundary_msg_id="m",
            covered_count=5, generation=1, created_at="a", updated_at="u",
        )
        assert row.covered_count == 5
        assert row.generation == 1
