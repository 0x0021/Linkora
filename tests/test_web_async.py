"""F17 —— Web 异步解阻塞守护测试。

背景：FastAPI 的 `async def` 路由若直接调用同步阻塞代码（SQLiteStore 查询、
embedding 计算、LLM 推理、dws 发送），会占住 asyncio 事件循环，导致一条慢查询
把整个管理端 UI 冻住。F17 把这些调用统一挪进线程池
（`web.dependencies.run_sync` / `fastapi.concurrency.run_in_threadpool`），
或把纯同步路由降级为 `def`（Starlette 自动线程池化）。

本文件提供两层保护：
1. 静态 AST 守护：扫描所有 `web/routers/*.py`，禁止 async 路由体内出现裸的
   同步阻塞调用 —— 防止后续新代码回退。
2. 运行时行为验证：真起 ASGI app，用一条「慢 DB 请求」与一条「快请求」并发，
   证明慢请求不再阻塞事件循环；并对包装后的路由做返回结构不变的冒烟校验。
"""

from __future__ import annotations

import ast
import pathlib
import time
from unittest.mock import MagicMock, patch

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROUTERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "web" / "routers"

# 这些符号一旦在 async 路由体内被【直接】调用（未经线程池），即视为阻塞事件循环。
BLOCKING_SYMBOLS = ("get_store", "_repo", "process_message", "replay_dead_letter")


def _blocking_calls_in_async_routes(path: pathlib.Path) -> list[tuple[str, int, str]]:
    """返回 (函数名, 行号, 调用表达式) 列表：async 函数体内裸的同步阻塞调用。

    判定规则：
    - 位于嵌套 `def`（即 `_work()` 闭包）内的调用 → 已在线程池，放行；
    - 被 `await` 直接包住的调用（如 `await run_sync(...)`）→ 放行；
    - 其余命中 BLOCKING_SYMBOLS 的调用 → 违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        nested_ids = {
            id(inner)
            for fn in ast.walk(node)
            if isinstance(fn, ast.FunctionDef)
            for inner in ast.walk(fn)
        }
        awaited_ids = {id(a.value) for a in ast.walk(node) if isinstance(a, ast.Await)}
        for n in ast.walk(node):
            if id(n) in nested_ids or id(n) in awaited_ids:
                continue
            if isinstance(n, ast.Call):
                src = ast.unparse(n.func)
                if any(sym in src for sym in BLOCKING_SYMBOLS):
                    violations.append((node.name, n.lineno, src))
    return violations


class TestNoBlockingCallsInAsyncRoutes:
    """静态守护：async 路由体内不得出现裸的同步 DB / LLM 调用。"""

    def test_routers_dir_exists(self):
        assert ROUTERS_DIR.is_dir(), f"router 目录不存在: {ROUTERS_DIR}"
        assert list(ROUTERS_DIR.glob("*.py")), "router 目录下没有 .py 文件"

    def test_no_blocking_calls(self):
        offenders: dict[str, list] = {}
        for path in sorted(ROUTERS_DIR.glob("*.py")):
            hits = _blocking_calls_in_async_routes(path)
            if hits:
                offenders[path.name] = hits

        assert not offenders, (
            "以下 async 路由体内存在裸的同步阻塞调用，会卡死事件循环。\n"
            "修复方式：包进 `def _work(): ...` 后 `await run_sync(_work)` / "
            "`await run_in_threadpool(_work)`，或把整条纯同步路由改成 `def`（Starlette 自动线程池）。\n"
            + "\n".join(
                f"  {fname}: " + ", ".join(f"{fn}() L{ln} -> {src}" for fn, ln, src in hits)
                for fname, hits in offenders.items()
            )
        )


class TestRunSyncHelper:
    """`web.dependencies.run_sync` 语义校验。"""

    def test_returns_value_and_runs_in_worker_thread(self):
        import threading

        from web.dependencies import run_sync

        main_thread = threading.current_thread().name
        seen: dict[str, str] = {}

        def _work(a, b, *, c):
            seen["thread"] = threading.current_thread().name
            return a + b + c

        result = anyio.run(lambda: run_sync(_work, 1, 2, c=3))
        assert result == 6
        assert seen["thread"] != main_thread, "run_sync 未真正切换到 worker 线程"

    def test_propagates_exception(self):
        from web.dependencies import run_sync

        def _boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            anyio.run(lambda: run_sync(_boom))


class TestEventLoopNotBlocked:
    """运行时验证：慢 DB 请求不再阻塞并发的快请求。"""

    SLOW_SECONDS = 0.6

    def _build_app(self, mock_store):
        from web.routers.drafts import router as drafts_router

        app = FastAPI()
        app.include_router(drafts_router)

        @app.get("/__ping")
        async def ping():  # 纯 async、无阻塞，用作事件循环健康探针
            return {"pong": True}

        return app

    def test_slow_db_query_does_not_block_event_loop(self):
        slow = self.SLOW_SECONDS

        store = MagicMock()
        store._draft_repo = MagicMock()

        def _slow_list(**kwargs):
            time.sleep(slow)  # 模拟慢 SQLite 查询（同步阻塞）
            return ([{"draft_id": "d1", "status": "pending"}], 1)

        store._draft_repo.list_drafts.side_effect = _slow_list
        store._draft_repo.count_pending_drafts.return_value = 1

        app = self._build_app(store)
        results: dict[str, float] = {}

        async def _scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                async def _slow_req():
                    t0 = time.perf_counter()
                    r = await client.get("/api/drafts")
                    results["slow_status"] = r.status_code
                    results["slow_elapsed"] = time.perf_counter() - t0

                async def _fast_req():
                    # 让慢请求先进入线程池，再发探针
                    await anyio.sleep(0.05)
                    t0 = time.perf_counter()
                    r = await client.get("/__ping")
                    results["fast_status"] = r.status_code
                    results["fast_elapsed"] = time.perf_counter() - t0

                async with anyio.create_task_group() as tg:
                    tg.start_soon(_slow_req)
                    tg.start_soon(_fast_req)

        with patch("web.routers.drafts.get_store", return_value=store):
            anyio.run(_scenario)

        assert results["slow_status"] == 200
        assert results["fast_status"] == 200
        # 慢请求确实慢（证明 sleep 生效，不是 mock 短路）
        assert results["slow_elapsed"] >= slow * 0.8, results
        # 关键断言：探针请求没有被慢查询拖住 —— 事件循环仍在响应
        assert results["fast_elapsed"] < slow * 0.5, (
            f"事件循环被慢 DB 查询阻塞：探针耗时 {results['fast_elapsed']:.3f}s "
            f"（慢查询 {slow}s）。说明该路由的同步调用未进线程池。"
        )


class TestWrappedRoutesSmoke:
    """冒烟：包装后返回结构与状态码保持不变。"""

    @pytest.fixture
    def store(self):
        s = MagicMock()
        s._draft_repo = MagicMock()
        s._memory_repo = MagicMock()
        s._kb_repo = MagicMock()
        return s

    def test_list_drafts_shape_unchanged(self, store):
        from web.routers.drafts import router

        store._draft_repo.list_drafts.return_value = (
            [{"draft_id": "d1", "status": "pending"}], 1)
        store._draft_repo.count_pending_drafts.return_value = 3

        app = FastAPI()
        app.include_router(router)
        with patch("web.routers.drafts.get_store", return_value=store):
            resp = TestClient(app).get("/api/drafts")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total"] == 1
        assert data["count"] == 1
        assert data["pending_count"] == 3

    def test_count_pending_drafts_shape_unchanged(self, store):
        from web.routers.drafts import router

        store._draft_repo.count_pending_drafts.return_value = 7

        app = FastAPI()
        app.include_router(router)
        with patch("web.routers.drafts.get_store", return_value=store):
            resp = TestClient(app).get("/api/drafts/count")

        assert resp.status_code == 200
        assert resp.json() == {"success": True, "pending_count": 7}

    def test_get_draft_404_still_propagates(self, store):
        """闭包内 raise HTTPException(404) 必须原样透出，不能被降级成 500。"""
        from web.routers.drafts import router

        store._draft_repo.get_draft.return_value = None

        app = FastAPI()
        app.include_router(router)
        with patch("web.routers.drafts.get_store", return_value=store):
            resp = TestClient(app).get("/api/drafts/nonexistent")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "draft not found"

    def test_memories_list_shape_unchanged(self, store):
        from web.routers.memories import router

        store._memory_repo.get_memories_filtered.return_value = [
            {"id": 1, "content": "x"}]

        app = FastAPI()
        app.include_router(router)
        with patch("web.routers.memories.get_store", return_value=store):
            resp = TestClient(app).get("/api/memories")

        assert resp.status_code == 200
        assert resp.json() == {"memories": [{"id": 1, "content": "x"}]}

    def test_kb_stats_shape_unchanged(self, store):
        from web.routers.kb import router

        store._kb_repo.kb_stats.return_value = {"total": 5, "indexed": 4}

        app = FastAPI()
        app.include_router(router)
        with patch("web.api.get_store", return_value=store):
            resp = TestClient(app).get("/api/kb/stats")

        assert resp.status_code == 200
        assert resp.json() == {"total": 5, "indexed": 4}


class TestSyncRoutesDowngraded:
    """纯同步重路由应声明为 `def`（由 Starlette 自动线程池化），而非 `async def`。"""

    EXPECTED_SYNC = {
        "web/routers/metrics.py": ["export_metrics"],
        "web/routers/simulate.py": ["simulate_message"],
        "web/routers/persona.py": ["backtest"],
        "web/routers/kb.py": ["import_kb_from_url"],
    }

    def test_declared_as_sync_def(self):
        root = ROUTERS_DIR.parents[1]
        for rel, names in self.EXPECTED_SYNC.items():
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            sync_names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
            async_names = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
            for name in names:
                assert name in sync_names, (
                    f"{rel}::{name} 应为同步 `def`（函数体全同步且耗时，交给 Starlette "
                    f"线程池），当前为 async: {name in async_names}"
                )
