"""轮询并发化 + 长尾限频轮次级节流 相关单元测试。

覆盖:
1. _should_skip_longtail_fetch 轮次级逻辑（修复「串行慢轮询击穿时间窗口导致长尾限频形同虚设」）
2. _poll_conversations 受控并发聚合正确性（不丢消息 / 不重复 / 单 worker 异常隔离 / 不死锁）
"""

import threading
import time
from types import SimpleNamespace

from src.poller_core_access import AccessControlMixin
from src.poller_strategy import PollerStrategyMixin


# ============ 轮次级长尾限频 ============

class FakeThrottle(AccessControlMixin):
    """最小 fake：提供 _should_skip_longtail_fetch 所需的实例属性。"""

    def __init__(self, interval: int = 60, rounds: int = 1, poll_count: int = 5):
        self.config = SimpleNamespace(
            min_conversation_poll_interval_seconds=interval,
            min_conversation_poll_rounds=rounds,
        )
        self._poll_count = poll_count
        self._last_fetch_time: dict[str, float] = {}
        self._last_fetch_round: dict[str, int] = {}
        self._poll_shared_lock = threading.Lock()


class TestShouldSkipLongtailFetchRoundBased:
    def test_forced_never_skipped(self):
        # 未读/forced 会话不受任何限频影响（实时优先）
        ft = FakeThrottle(interval=60, rounds=1)
        ft._last_fetch_time["cid1"] = time.time()
        ft._last_fetch_round["cid1"] = ft._poll_count
        assert ft._should_skip_longtail_fetch("cid1", True) is False

    def test_rounds_skip_when_within_window(self):
        # rounds=1 → 每 2 轮才抓一次：上一轮(距当前1)跳过，更早(距当前2)抓取
        ft = FakeThrottle(interval=0, rounds=1, poll_count=5)
        ft._last_fetch_round = {"cidA": 4}  # 当前第5轮，距上次1轮
        assert ft._should_skip_longtail_fetch("cidA", False) is True
        ft._last_fetch_round = {"cidB": 3}  # 距上次2轮
        assert ft._should_skip_longtail_fetch("cidB", False) is False

    def test_never_fetched_not_skipped(self):
        # 从未抓取过（无时间、无轮次记录）不跳过
        ft = FakeThrottle(interval=60, rounds=2, poll_count=5)
        assert ft._should_skip_longtail_fetch("cidNew", False) is False

    def test_time_window_skips(self):
        ft = FakeThrottle(interval=60, rounds=0, poll_count=5)
        ft._last_fetch_time = {"cid1": time.time() - 30}  # 30s < 60s
        assert ft._should_skip_longtail_fetch("cid1", False) is True
        ft._last_fetch_time = {"cid2": time.time() - 90}  # 90s > 60s
        assert ft._should_skip_longtail_fetch("cid2", False) is False

    def test_both_layers_any_hit_skips(self):
        # 时间窗口不跳(90s)但轮次级跳(距1轮) → 任一命中即跳过
        ft = FakeThrottle(interval=60, rounds=1, poll_count=5)
        ft._last_fetch_time = {"cid1": time.time() - 90}
        ft._last_fetch_round = {"cid1": 4}
        assert ft._should_skip_longtail_fetch("cid1", False) is True

    def test_all_disabled_never_skips(self):
        ft = FakeThrottle(interval=0, rounds=0, poll_count=5)
        ft._last_fetch_time = {"cid1": time.time()}
        ft._last_fetch_round = {"cid1": ft._poll_count}
        assert ft._should_skip_longtail_fetch("cid1", False) is False


# ============ 受控并发聚合正确性 ============

class FakeConvPoll:
    """最小对象：复用 PollerStrategyMixin._poll_conversations 的并发聚合逻辑。

    _poll_one_conversation 用可预测的 fake 替代，专注验证并发结果聚合
    （不丢 / 不重 / 单 worker 异常隔离 / 不死锁）。
    """

    def __init__(self, conc: int, payload: dict, error_cid: str | None = None):
        self.config = SimpleNamespace(poll_concurrency=conc)
        self._poll_shared_lock = threading.Lock()
        self._poll_count = 0
        self.payload = payload
        self.error_cid = error_cid

    def _poll_one_conversation(self, conv, group_cache, forced_ids):
        cid = conv.get("openConversationId")
        if self.error_cid and cid == self.error_cid:
            raise RuntimeError("boom in worker")
        return (self.payload.get(cid, []), [], False)


def _build_convs(n: int) -> list[dict]:
    return [{"openConversationId": f"cid{i}"} for i in range(n)]


def _expected_total(payload: dict) -> int:
    return sum(len(v) for v in payload.values())


class TestPollConversationsConcurrent:
    def test_serial_collects_all(self):
        payload = {f"cid{i}": [f"m{i}_{j}" for j in range(3)] for i in range(20)}
        fp = FakeConvPoll(conc=1, payload=payload)
        msgs, skip = PollerStrategyMixin._poll_conversations(fp, _build_convs(20), None, set())
        assert len(msgs) == _expected_total(payload) == 60
        assert skip == 0

    def test_concurrent_collects_all_no_loss(self):
        # 并发与串行结果集合必须完全一致（不丢消息）
        payload = {f"cid{i}": [f"m{i}_{j}" for j in range(3)] for i in range(20)}
        expected = sorted(
            mid for vals in payload.values() for mid in vals
        )
        # 串行
        fp_s = FakeConvPoll(conc=1, payload=payload)
        s_msgs, _ = PollerStrategyMixin._poll_conversations(fp_s, _build_convs(20), None, set())
        # 并发
        fp_c = FakeConvPoll(conc=4, payload=payload)
        c_msgs, _ = PollerStrategyMixin._poll_conversations(fp_c, _build_convs(20), None, set())
        assert sorted(s_msgs) == expected
        assert sorted(c_msgs) == expected
        assert len(c_msgs) == 60

    def test_concurrent_no_duplication(self):
        payload = {f"cid{i}": [f"m{i}"] for i in range(15)}
        fp = FakeConvPoll(conc=8, payload=payload)
        msgs, _ = PollerStrategyMixin._poll_conversations(fp, _build_convs(15), None, set())
        # 每个会话恰好1条，总计不应重复
        assert len(msgs) == 15
        assert len(set(msgs)) == 15

    def test_worker_exception_isolated(self):
        # 单 worker 抛异常不应冲垮整轮：其余会话消息仍被收集
        payload = {f"cid{i}": [f"m{i}"] for i in range(10)}
        fp = FakeConvPoll(conc=4, payload=payload, error_cid="cid5")
        msgs, _ = PollerStrategyMixin._poll_conversations(fp, _build_convs(10), None, set())
        # 仅 cid5 因异常缺失，其余 9 条都在
        assert len(msgs) == 9
        assert "m5" not in msgs

    def test_no_deadlock_under_repeat(self):
        # 反复并发跑，确认共享锁不会死锁
        payload = {f"cid{i}": [f"m{i}_{j}" for j in range(2)] for i in range(12)}
        for _ in range(5):
            fp = FakeConvPoll(conc=4, payload=payload)
            msgs, _ = PollerStrategyMixin._poll_conversations(fp, _build_convs(12), None, set())
            assert len(msgs) == 24
