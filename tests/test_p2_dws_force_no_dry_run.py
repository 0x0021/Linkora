"""P2-8 回归：dws auth_login / get_current_org 回退必须带 force_no_dry_run。

背景：dws_adapter 的 run() 默认可能走全局 dry_run（空操作），认证/组织探测这类
只读但必须真实执行的命令必须显式 force_no_dry_run=True，否则在 dry_run 环境下
会拿到空结果（进而误判未登录 / 组织探测失败）。
"""

from __future__ import annotations

from src.dws_adapter import DwsAdapter


class TestAuthLoginForceNoDryRun:
    def _make(self) -> DwsAdapter:
        return DwsAdapter(dry_run=True)

    def test_auth_login_passes_force_no_dry_run(self):
        dws = self._make()
        captured: dict = {}

        def fake_run(args, timeout=300, force_no_dry_run=False, **kw):
            captured["args"] = list(args)
            captured["force_no_dry_run"] = force_no_dry_run
            return {"success": True}

        dws.run = fake_run
        dws.auth_login()
        assert captured.get("force_no_dry_run") is True
        assert captured["args"][:2] == ["auth", "login"]

    def test_get_current_org_fallback_passes_force_no_dry_run(self):
        dws = self._make()
        # 让本地 profile 读取返回 None，强制走 dws profile list 回退
        dws._get_current_profile_local = lambda: None
        calls: list = []

        def fake_run(args, force_no_dry_run=False, **kw):
            calls.append((list(args), force_no_dry_run))
            if args and args[0] == "profile":
                return {"success": True, "result": {"profiles": [], "currentProfile": ""}}
            return {"success": True}

        dws.run = fake_run
        # _get_result 返回真实 result 字典，避免 MagicMock 不可迭代
        dws._get_result = lambda data: (data or {}).get("result", {})
        dws.get_current_org()

        profile_calls = [fn for args, fn in calls if args[:1] == ["profile"]]
        assert profile_calls, "应回退调用 profile list"
        assert all(profile_calls), "profile list 回退必须 force_no_dry_run=True"
