"""F13 回归：市场榜单缓存并发单飞（single-flight）。

验证并发请求只触发一次 CLI/安装，避免：
1. 多个请求同时跑 `bash install.sh --cli-only` 抢装 ~/.local/bin；
2. 模块级缓存 dict 在 async 下的并发读写竞态。
"""
import asyncio
import time
from unittest import mock

from web import dependencies as dep


def _reset_cache():
    dep._MARKET_RANKINGS_CACHE["data"] = None
    dep._MARKET_RANKINGS_CACHE["ts"] = 0.0


def test_market_rankings_single_flight_under_concurrency():
    _reset_cache()
    call_count = [0]

    def slow_install():
        call_count[0] += 1
        time.sleep(0.05)  # 模拟 CLI / 自动安装耗时
        return (True, "")

    proc = mock.Mock(returncode=0, stdout='{"rankings": {}}')

    async def _gather():
        return await asyncio.gather(*[dep._fetch_market_rankings() for _ in range(8)])

    with mock.patch("web.dependencies._ensure_skillhub_cli", side_effect=slow_install), \
         mock.patch("web.dependencies.subprocess.run", return_value=proc):
        results = asyncio.run(_gather())

    # 单飞：无论多少并发请求，底层安装/CLI 只跑一次
    assert call_count[0] == 1
    # 所有请求都拿到一致的有效结果
    for r in results:
        assert r["stale"] is False
        assert "sections" in r
