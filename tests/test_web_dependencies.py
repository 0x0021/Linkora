"""web/dependencies 多平台隔离测试（不依赖真实 config / 网络）。

覆盖：get_store(platform) 按平台返回独立 SQLiteStore 实例、同平台同线程复用、
未知平台回退派生路径、get_platforms 返回配置平台。
"""
import pytest

import web.dependencies as deps


@pytest.fixture(autouse=True)
def _clear_store_cache():
    """每个测试后关闭并清空 per-thread 缓存，避免连接泄漏与跨测试串味。"""
    yield
    stores = getattr(deps._store_local, "stores", None)
    if stores:
        for _s in stores.values():
            try:
                _s.close()
            except Exception:
                pass
        deps._store_local.stores = {}


def _fake_resolve(tmp_path):
    def _resolve(platform: str) -> str:
        return str(tmp_path / f"{platform}.db")
    return _resolve


def test_get_store_isolates_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "_resolve_platform_path", _fake_resolve(tmp_path))
    s_dt = deps.get_store("dingtalk")
    s_fs = deps.get_store("feishu")
    s_dt2 = deps.get_store("dingtalk")
    assert s_dt.db_path.endswith("dingtalk.db")
    assert s_fs.db_path.endswith("feishu.db")
    assert s_dt is not s_fs          # 不同平台 → 不同实例
    assert s_dt is s_dt2             # 同平台同线程 → 复用


def test_get_store_unknown_platform_derives_path(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "_resolve_platform_path", _fake_resolve(tmp_path))
    s = deps.get_store("bogus_platform")
    # 未知平台不报错，按 id 派生兜底路径（仍隔离）
    assert s.db_path.endswith("bogus_platform.db")


def test_get_platforms_returns_configured(monkeypatch):
    class _P:
        id = "dingtalk"
        display_name = "钉钉"
        enabled = True
        adapter_type = "dingtalk"

    class _Cfg:
        platforms = [_P()]

    class _Inst:
        config = _Cfg()

    monkeypatch.setattr(deps, "get_app_instance", lambda: _Inst())
    pls = deps.get_platforms()
    assert pls == [{
        "id": "dingtalk",
        "display_name": "钉钉",
        "enabled": True,
        "adapter_type": "dingtalk",
    }]


def test_get_platforms_empty_when_no_config(monkeypatch):
    # 运行期实例与 load_config 均不可用时 → 返回空列表且不抛异常
    monkeypatch.setattr(deps, "get_app_instance", lambda: None)
    monkeypatch.setattr(deps, "load_config", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no config")))
    pls = deps.get_platforms()
    assert pls == []
