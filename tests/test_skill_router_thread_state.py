"""SkillRouter 线程级状态读取器的回归护栏。

覆盖两个曾经静默失效的真实缺陷：

1. **跨线程读 last_match 抛 AttributeError**
   `_tl` 只在构造 SkillRouter 的线程上被赋初值，而 `process_message`
   允许并发（reply_semaphore）。工作线程首次读 `router.last_match` 时
   `_tl` 上根本没有该属性，原实现 `return self._tl.last_match` 直接抛
   AttributeError；上层 `except Exception` 会把它吞成一条 warning，
   表现为「技能明明命中却没注入」。

2. **路由质量埋点恒为空**
   SkillRouter 只写 `self._tl.last_routing_detail`，从未提供对外读取器；
   消费方 `agent_steps/routing_trace.py` 却按
   `getattr(skill_router, "_last_routing_detail", {})`（多了个下划线前缀）
   取值 → 恒得 `{}`，导致 routing_quality 表里的 candidates_count /
   convergence_applied 永远是 0。
"""
from __future__ import annotations

import tempfile
import threading

from src.skills.manager import SkillManager
from src.skills.router import SkillRouter


def _patch_skill_dirs(monkeypatch, root: str):
    import src.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [root + "/data/skills"])


def _make_router(monkeypatch) -> tuple[SkillRouter, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory()
    _patch_skill_dirs(monkeypatch, td.name)
    return SkillRouter(SkillManager(td.name)), td


class TestCrossThreadReaders:
    def test_last_match_readable_from_fresh_thread(self, monkeypatch):
        """在未初始化 _tl 的新线程上读三个属性都不得抛异常。"""
        router, td = _make_router(monkeypatch)
        try:
            box: dict = {}

            def _worker():
                try:
                    box["match"] = router.last_match
                    box["matches"] = router.last_matches
                    box["detail"] = router.last_routing_detail
                except BaseException as e:  # noqa: BLE001 — 要的就是把异常带回主线程
                    box["err"] = e

            t = threading.Thread(target=_worker)
            t.start()
            t.join(timeout=5)

            assert "err" not in box, f"跨线程读取抛异常: {box.get('err')!r}"
            assert box["match"] is None
            assert box["matches"] == []
            assert box["detail"] == {}
        finally:
            td.cleanup()

    def test_readers_isolated_per_thread(self, monkeypatch):
        """一个线程写入不得污染另一个线程的读取结果。"""
        router, td = _make_router(monkeypatch)
        try:
            router._tl.last_routing_detail = {"candidates_count": 7}
            assert router.last_routing_detail == {"candidates_count": 7}

            box: dict = {}

            def _worker():
                box["detail"] = router.last_routing_detail

            t = threading.Thread(target=_worker)
            t.start()
            t.join(timeout=5)
            assert box["detail"] == {}, "threading.local 隔离被破坏"
        finally:
            td.cleanup()


class TestRoutingDetailReaderName:
    def test_reader_exists_and_matches_written_field(self, monkeypatch):
        """last_routing_detail 读取器必须存在，且读到 _tl 上真实写入的那份。

        这条断言直接钉死 bug #2：只要读取器被删掉 / 改名，或写入侧字段名漂移，
        埋点就会重新变成恒 {}，此测试会立刻报红。
        """
        router, td = _make_router(monkeypatch)
        try:
            assert hasattr(router, "last_routing_detail")
            payload = {
                "candidates_count": 3,
                "convergence_zone_size": 2,
                "convergence_applied": 1,
                "goal_fit_details": {"a": 0.9},
            }
            router._tl.last_routing_detail = payload
            assert router.last_routing_detail == payload
        finally:
            td.cleanup()

    def test_routing_trace_reads_the_right_attribute(self):
        """routing_trace 消费方必须按 last_routing_detail 取值（不带下划线前缀）。"""
        import inspect
        import src.llm.agent_steps.routing_trace as rt

        src = inspect.getsource(rt)
        assert '"_last_routing_detail"' not in src, (
            "routing_trace 又用回了不存在的 _last_routing_detail，埋点会恒为空"
        )
        assert '"last_routing_detail"' in src


class TestActivatedReadersCrossThread:
    """get_activated_* 读取也必须在未初始化线程上安全兜底。

    这些方法是 _select_tools → tool_router.select_tools 的下游消费方。
    RAG 严格问答模式会跳过 activate_skills，导致处理消息的 worker 线程上
    _tl.last_matches 从未被赋值；若直接读 self._tl.last_matches 会抛
    AttributeError（'_RouterThreadState' object has no attribute 'last_matches'），
    让整条 LLM 回复链路崩溃进死信队列。修复后统一走带 getattr 兜底的
    last_matches 属性（与类注释「其余线程靠读取侧 getattr 兜底」语义一致）。
    """

    def test_activated_readers_safe_on_fresh_thread(self, monkeypatch):
        """在未初始化 _tl 的新线程上读 get_activated_* 都不得抛异常。"""
        router, td = _make_router(monkeypatch)
        try:
            box: dict = {}

            def _worker():
                try:
                    box["name"] = router.get_activated_skill_name()
                    box["names"] = router.get_activated_skill_names()
                    box["tools"] = router.get_activated_tools()
                    box["fallback"] = router.get_activated_fallback_tools()
                except BaseException as e:  # noqa: BLE001 — 要的就是把异常带回主线程
                    box["err"] = e

            t = threading.Thread(target=_worker)
            t.start()
            t.join(timeout=5)

            assert "err" not in box, f"跨线程读取激活结果抛异常: {box.get('err')!r}"
            assert box["name"] is None
            assert box["names"] == []
            assert box["tools"] == []
            assert box["fallback"] == []
        finally:
            td.cleanup()
