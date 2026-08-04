"""P2-11 回归：SearXNG 实例缓存读写必须加锁且原子写，避免并发丢更新/损坏。

背景：_searx_pick_instance（推进游标）与 _searx_mark_bad（写冷却）都做
read-modify-write，无锁时 mark_bad 的冷却会被 pick 写回的旧 cooldown 顶掉，
导致坏实例未被真正冷却而反复重试；非原子写则可能在写一半时崩溃损坏缓存文件。
"""

from __future__ import annotations

import json
import threading
import time

import src.tools.web_search as web_search


class TestSearxCacheThreadSafety:
    def test_concurrent_pick_and_mark_bad_preserves_cooldown(self, tmp_path, monkeypatch):
        cache = tmp_path / "searx_instances.json"
        cache.write_text(json.dumps({
            "fetched_at": int(time.time()),
            "urls": ["https://a.test", "https://b.test", "https://c.test"],
            "cursor": 0,
            "cooldown": {},
        }), encoding="utf-8")
        monkeypatch.setattr(web_search, "_SEARXNG_CACHE_PATH", cache)

        # 先冷却 b，再并发 pick + mark_bad c，验证两者冷却都不丢失
        web_search._searx_mark_bad("https://b.test")

        errors: list = []

        def worker():
            try:
                for _ in range(30):
                    web_search._searx_pick_instance()
                    web_search._searx_mark_bad("https://c.test")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发缓存读写抛错: {errors}"

        data = json.loads(cache.read_text())
        assert "https://b.test" in data["cooldown"], "b 的冷却不应被并发丢失"
        assert "https://c.test" in data["cooldown"], "c 的冷却不应被并发丢失"

    def test_save_cache_is_atomic_json(self, tmp_path, monkeypatch):
        cache = tmp_path / "searx_instances.json"
        monkeypatch.setattr(web_search, "_SEARXNG_CACHE_PATH", cache)
        payload = {"fetched_at": int(time.time()), "urls": ["https://x.test"], "cursor": 0, "cooldown": {}}
        web_search._searx_save_cache(payload)
        # 应写出合法 JSON，且不含临时文件残留
        assert json.loads(cache.read_text()) == payload
        assert not list(tmp_path.glob(".searx_*.tmp")), "不应残留临时文件"
