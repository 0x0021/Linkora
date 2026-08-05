"""weather._geocode 缓存测试：失败结果(Non)不得被永久缓存。

回归防护：旧实现用 functools.lru_cache 会把瞬时失败返回的 None 永久缓存，
导致该城市此后永远走 wttr.in 兜底、再不重试正常路径。
"""
from __future__ import annotations


import src.tools.weather as weather_mod


class TestGeocodeCache:
    def setup_method(self):
        weather_mod._GEO_CACHE.clear()

    def test_none_not_cached(self, monkeypatch):
        calls = {"n": 0}

        def fake_nominatim(city, timeout):
            calls["n"] += 1
            return None

        monkeypatch.setattr(weather_mod, "_geocode_nominatim", fake_nominatim)
        monkeypatch.setattr(weather_mod, "_geocode_open_meteo", lambda c, t: None)

        assert weather_mod._geocode("测试市", 10) is None
        # 第二次调用应重新解析（未命中缓存），而非直接返回缓存的 None
        assert weather_mod._geocode("测试市", 10) is None
        assert calls["n"] == 2

    def test_success_cached(self, monkeypatch):
        calls = {"n": 0}
        geo = {"name": "市A", "latitude": 1.0, "longitude": 2.0}

        def fake_nominatim(city, timeout):
            calls["n"] += 1
            return geo

        monkeypatch.setattr(weather_mod, "_geocode_nominatim", fake_nominatim)
        monkeypatch.setattr(weather_mod, "_geocode_open_meteo", lambda c, t: None)

        assert weather_mod._geocode("市A", 10) is not None
        assert weather_mod._geocode("市A", 10) is not None
        # 第二次命中缓存，不应再调用解析器
        assert calls["n"] == 1
