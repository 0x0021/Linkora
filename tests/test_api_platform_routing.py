"""Phase 2 — API 路由 `?platform=` 透传验证（不依赖真实 config / 生产库）。

验证链路：请求 `?platform=<id>` → 平台中间件归一化 → 写入请求级 ContextVar →
endpoint 内 `get_store()`（无论 dingtalk / feishu / wecom）自动落到对应平台库。

- 合法已配置平台 → 透传；
- 缺失 / 非法 → 回退 dingtalk（不报错）；
- GET /api/platforms 返回配置平台列表。

注：本测试通过 spy `_resolve_platform_path` 捕获“实际解析到的平台字符串”并
重定向到 tmp（避免触碰真实生产库），从而验证中间件归一化 + ContextVar 跨线程
传播是否真实生效（若 ContextVar 未传播，feishu 请求会错误落到 dingtalk）。
"""
import pytest
from fastapi.testclient import TestClient

import web.dependencies as deps
from web.api import app


class _Store:
    def __init__(self, path: str):
        self.path = path


class _Plat:
    def __init__(self, pid: str):
        self.id = pid
        self.display_name = pid
        self.enabled = True
        self.adapter_type = pid
        self.storage = _Store(f"./data/{pid}-ai.db")


class _WebCfg:
    auth_enabled = False


class _Cfg:
    platforms = [_Plat("dingtalk"), _Plat("feishu"), _Plat("wecom")]
    web = _WebCfg()


class _Inst:
    config = _Cfg()


@pytest.fixture
def _platform_env(tmp_path, monkeypatch):
    """注入 3 平台配置 + spy _resolve_platform_path（捕获+重定向 tmp）。"""
    monkeypatch.setattr(deps, "get_app_instance", lambda: _Inst())
    # 鉴权中间件走 _get_cfg()（读 shared_state.get_config，非 get_app_instance），
    # 故需同步让其为带 auth_enabled=False 的假配置，避免 Basic Auth 拦截测试请求。
    import web.api as _api_mod
    monkeypatch.setattr(_api_mod, "_get_cfg", lambda: _Cfg())
    captured = []

    def _spy(platform: str) -> str:
        captured.append(platform)
        return str(tmp_path / f"{platform}.db")

    monkeypatch.setattr(deps, "_resolve_platform_path", _spy)
    yield captured
    # 关闭可能建立的 store 连接
    stores = getattr(deps._store_local, "stores", None)
    if stores:
        for _s in list(stores.values()):
            try:
                _s.close()
            except Exception:
                pass
        deps._store_local.stores = {}


def test_platforms_endpoint(_platform_env):
    client = TestClient(app)
    r = client.get("/api/platforms")
    assert r.status_code == 200
    pls = r.json()["platforms"]
    ids = {p["id"] for p in pls}
    assert ids == {"dingtalk", "feishu", "wecom"}


def test_platform_routing_feishu(_platform_env):
    captured = _platform_env
    client = TestClient(app)
    r = client.get("/api/keywords?platform=feishu")
    assert r.status_code == 200
    # 实际解析到的平台必须是 feishu（证明 ContextVar 跨线程传播生效）
    assert captured == ["feishu"]


def test_platform_routing_wecom(_platform_env):
    captured = _platform_env
    client = TestClient(app)
    r = client.get("/api/keywords?platform=wecom")
    assert r.status_code == 200
    assert captured == ["wecom"]


def test_platform_routing_missing_falls_back_dingtalk(_platform_env):
    captured = _platform_env
    client = TestClient(app)
    r = client.get("/api/keywords")
    assert r.status_code == 200
    assert captured == ["dingtalk"]


def test_platform_routing_illegal_falls_back_dingtalk(_platform_env):
    captured = _platform_env
    client = TestClient(app)
    r = client.get("/api/keywords?platform=not_a_real_platform")
    assert r.status_code == 200
    # 非法平台 → 归一化回退 dingtalk
    assert captured == ["dingtalk"]
