"""IM 适配器抽象层回归测试。

覆盖：
- DwsAdapter 正确继承 BaseIMAdapter（MRO / 行为不变）
- Dws* 异常别名 == 通用 IMAdapter* 异常
- BaseIMAdapter 执行引擎：错误分类钩子、可重试退避、不可重试立即抛、通用 _build_command
- DwsAdapter.download_media 复用基类 _run_download
- 飞书 / 企业微信适配器均已实现核心能力（桩不再抛 NotImplementedError）
"""
from __future__ import annotations

import os
import subprocess

import pytest

from src.im_adapter import (
    BaseIMAdapter,
    FeishuCliAdapter,
    IMAdapterError,
    IMAdapterNonRetryableError,
    IMAdapterPermissionError,
    IMAdapterRetryableError,
    WecomCliAdapter,
)
from src.dws_adapter import (
    DwsAdapter,
    DwsError,
    DwsNonRetryableError,
    DwsPermissionError,
    DwsRetryableError,
)


class _Res:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class _FakeRetryableAdapter(BaseIMAdapter):
    """测试用假适配器：所有错误归为可重试，命令固定为 echo。"""
    def _build_command(self, args, *, force_no_dry_run=False):
        return ["echo", *args]

    def _classify_error(self, msg):
        return IMAdapterRetryableError

    # 桩实现：仅放行 __new__，实际测试不调用这些方法
    def _infer_single_chat(self, chat): return False
    def chat_message_reply(self, **kw): return {}
    def chat_message_send(self, **kw): return {}
    def chat_message_update(self, **kw): return {}
    def chat_message_list(self, *a, **kw): return []
    def chat_message_list_all(self, *a, **kw): return {}
    def chat_message_list_direct(self, *a, **kw): return []
    def chat_message_list_unread_conversations(self, *a, **kw): return []
    def auth_login(self, *a, **kw): return {}
    def auth_status(self, *a, **kw): return {}
    def is_authenticated(self, *a, **kw): return False
    def get_current_org(self, *a, **kw): return {}
    def list_orgs(self, *a, **kw): return []
    def profile_list(self, *a, **kw): return {}
    def contact_user_get_self(self, *a, **kw): return {}
    def contact_user_search(self, *a, **kw): return []
    def media_upload(self, *a, **kw): return ""
    def download_media(self, *a, **kw): return {}
    def doc_search(self, *a, **kw): return []
    def doc_read(self, *a, **kw): return {}


class _FakeNonRetryableAdapter(BaseIMAdapter):
    """测试用假适配器：所有错误归为不可重试，且不覆写 _build_command。"""
    def _build_command(self, args, *, force_no_dry_run=False):
        return super()._build_command(args, force_no_dry_run=force_no_dry_run)

    def _classify_error(self, msg):
        return IMAdapterNonRetryableError

    # 桩实现
    def _infer_single_chat(self, chat): return False
    def chat_message_reply(self, **kw): return {}
    def chat_message_send(self, **kw): return {}
    def chat_message_update(self, **kw): return {}
    def chat_message_list(self, *a, **kw): return []
    def chat_message_list_all(self, *a, **kw): return {}
    def chat_message_list_direct(self, *a, **kw): return []
    def chat_message_list_unread_conversations(self, *a, **kw): return []
    def auth_login(self, *a, **kw): return {}
    def auth_status(self, *a, **kw): return {}
    def is_authenticated(self, *a, **kw): return False
    def get_current_org(self, *a, **kw): return {}
    def list_orgs(self, *a, **kw): return []
    def profile_list(self, *a, **kw): return {}
    def contact_user_get_self(self, *a, **kw): return {}
    def contact_user_search(self, *a, **kw): return []
    def media_upload(self, *a, **kw): return ""
    def download_media(self, *a, **kw): return {}
    def doc_search(self, *a, **kw): return []
    def doc_read(self, *a, **kw): return {}


# ---------------------------------------------------------------------------
# 继承关系 / 向后兼容
# ---------------------------------------------------------------------------

def test_dws_adapter_subclasses_base():
    assert issubclass(DwsAdapter, BaseIMAdapter)
    # 拆分后：钉钉能力由 9 个 mixin 提供，BaseIMAdapter 兜底在 mixin 链尾部
    # （抽象接口/统一引擎），MRO 中所有钉钉能力层排在 BaseIMAdapter 之前。
    # 能力层 = 各 *Mixin + 类型共享基类 DwsAdapterBase（仅声明，无实现）。
    mro = DwsAdapter.__mro__
    base_idx = next(i for i, c in enumerate(mro) if c is BaseIMAdapter)
    assert all(
        "Mixin" in c.__name__ or c.__name__ == "DwsAdapterBase"
        for c in mro[1:base_idx]
    )
    # 共享基类必须排在所有 mixin 之后（真实实现优先于 stub 声明）
    names = [c.__name__ for c in mro[1:base_idx]]
    assert names[-1] == "DwsAdapterBase", f"共享基类须位于能力层末尾: {names}"


def test_shared_type_bases_define_no_init():
    """所有类型共享基类绝不能声明 __init__。

    回归防护（已两次实际踩坑）：
    - DwsAdapterBase 带 __init__ → super().__init__(cli_path=...) 抛 TypeError
    - EngineMixinBase 带 __init__ → InboundMixin() 无参实例化抛 TypeError

    共享基类只做类型声明，任何 dunder 实现都会在 MRO 中截胡真实调用链。
    """
    from src.dws_adapter.dws_mixins_base import DwsAdapterBase
    from src.im_adapter.im_mixins_base import IMAdapterBase
    from src.memory.sqlite_store_mixins_base import SQLiteStoreBase
    from src.platform.engine_mixins_base import EngineMixinBase
    from src.poller_mixins_base import PollerMixinBase

    for base in (DwsAdapterBase, IMAdapterBase, SQLiteStoreBase,
                 EngineMixinBase, PollerMixinBase):
        dunders = [
            n for n in vars(base)
            if n.startswith("__") and n.endswith("__")
            # 排除解释器/ABC 机制自动生成的类属性，只关心显式定义的 dunder 方法
            and n not in {"__module__", "__qualname__", "__doc__",
                          "__annotations__", "__dict__", "__weakref__",
                          "__firstlineno__", "__static_attributes__",
                          "__abstractmethods__", "__parameters__",
                          "__orig_bases__", "__slots__"}
            and callable(getattr(base, n, None))
        ]
        assert not dunders, f"{base.__name__} 不得定义 dunder: {dunders}"


def test_dws_error_aliases_equal_generic():
    assert DwsError is IMAdapterError
    assert DwsRetryableError is IMAdapterRetryableError
    assert DwsNonRetryableError is IMAdapterNonRetryableError
    assert DwsPermissionError is IMAdapterPermissionError
    # 异常层级保持一致
    assert issubclass(DwsPermissionError, DwsNonRetryableError)
    assert issubclass(DwsNonRetryableError, DwsError)


def test_skeletons_subclass_capability_skeleton():
    assert issubclass(FeishuCliAdapter, BaseIMAdapter)
    assert issubclass(WecomCliAdapter, BaseIMAdapter)


# ---------------------------------------------------------------------------
# 基类执行引擎
# ---------------------------------------------------------------------------

def test_base_build_command_appends_dry_run_and_profile():
    """基类 _build_command 拼装 cli_path / --dry-run / --profile。

    使用 _FakeNonRetryableAdapter（不覆写 _build_command），确保走基类实现。
    """
    a = _FakeNonRetryableAdapter(cli_path="mycli", dry_run=True, profile="p1")
    cmd = a._build_command(["sub", "cmd"])
    assert cmd[0] == "mycli"
    assert "--dry-run" in cmd
    assert "--profile" in cmd and cmd[cmd.index("--profile") + 1] == "p1"


def test_base_build_command_force_no_dry_run_suppresses_flag():
    a = _FakeNonRetryableAdapter(dry_run=True)
    cmd = a._build_command(["sub"], force_no_dry_run=True)
    assert "--dry-run" not in cmd


def test_base_run_retries_on_retryable(monkeypatch):
    adapter = _FakeRetryableAdapter(retries=2, timeout=1)
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.TimeoutExpired(cmd, 1)
        return _Res(0, '{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = adapter.run(["x"])
    assert result == {"ok": True}
    assert calls["n"] == 3  # 重试 2 次 + 第 3 次成功


def test_base_run_non_retryable_raises_immediately(monkeypatch):
    adapter = _FakeNonRetryableAdapter(retries=3, timeout=1)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(1, "", "invalid token 401"))
    with pytest.raises(IMAdapterNonRetryableError):
        adapter.run(["x"])


def test_base_run_parses_trailing_json_line(monkeypatch):
    adapter = _FakeRetryableAdapter()
    # stdout 含进度噪声，最后一行才是 JSON
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Res(0, "progress...\n{\"result\": 42}"),
    )
    assert adapter.run(["x"]) == {"result": 42}


def test_base_run_extracts_json_from_installation_noise(monkeypatch):
    """lark-cli 等工具会在 stdout 输出安装提示后再输出 JSON，需正确提取。"""
    adapter = _FakeRetryableAdapter()
    stdout = (
        "lark-cli v1.0.74 installed successfully\n"
        "{\n"
        '  "ok": true,\n'
        '  "identity": "user",\n'
        '  "data": {"name": "x"}\n'
        "}\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(0, stdout))
    assert adapter.run(["whoami"]) == {"ok": True, "identity": "user", "data": {"name": "x"}}


def test_base_run_extracts_last_json_object(monkeypatch):
    """stdout 末尾有非 JSON 内容时，应取最后一个合法 JSON 对象。"""
    adapter = _FakeRetryableAdapter()
    stdout = '{"a": 1}\nlog line\n{"b": 2}\ntrailing noise'
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(0, stdout))
    assert adapter.run(["x"]) == {"b": 2}


# ---------------------------------------------------------------------------
# DwsAdapter 经基类引擎的兼容点
# ---------------------------------------------------------------------------

def test_dws_download_media_reuses_base_run_download(monkeypatch):
    adapter = DwsAdapter()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(0))
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "getsize", lambda p: 100)
    r = adapter.download_media(
        media_id="m1", message_id="msg1",
        conversation_id="c1", output_path="/tmp/out.png",
    )
    assert r == "/tmp/out.png"


def test_dws_download_media_empty_file_raises(monkeypatch):
    adapter = DwsAdapter()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Res(0))
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(os.path, "getsize", lambda p: 0)
    with pytest.raises(Exception):
        adapter.download_media(
            media_id="m1", message_id="msg1",
            conversation_id="c1", output_path="/tmp/out.png",
        )


def test_dws_no_browser_env_present():
    # 钉钉专属环境（阻止弹窗）仍可从模块导入，且被实例使用
    from src.dws_adapter import _NO_BROWSER_ENV
    assert "BROWSER" in _NO_BROWSER_ENV
    assert DwsAdapter()._no_browser_env is _NO_BROWSER_ENV


# ---------------------------------------------------------------------------
# 继承骨架（飞书 / 企微）
# ---------------------------------------------------------------------------

def test_wecom_adapter_implements_core():
    # 企业微信适配器已实现核心能力（不应再是 NotImplementedError 桩）。
    inst = WecomCliAdapter()
    # 引擎钩子已落地
    assert isinstance(inst._build_command(["msg", "x"]), list)
    assert inst._classify_error('{"errcode":60020,"errmsg":"not allowed"}') is inst._permission_error_class()
    # 能力方法已落地：缺目标时抛 ValueError（业务校验），而非 NotImplementedError
    with pytest.raises(ValueError):
        inst.chat_message_send(text="hi")
    # 联系人 / 下载方法存在且非桩
    assert callable(inst.contact_user_get_self)
    assert callable(inst.download_media)


def test_wecom_auth_login_retries_on_timeout(monkeypatch):
    """企微登录超时应重试 3 次，而不是被 NameError 击穿。

    回归守护：except 子句曾误写为不存在的 IMAdapterTimeoutError —— 求值该子句
    即抛 NameError 并穿透整个 try（同 try 内的 except Exception 也接不住），
    使 3 次重试完全失效。
    """
    import src.im_adapter.wecom as wm

    inst = WecomCliAdapter()
    attempts = []

    def _boom(*a, **kw):
        attempts.append(1)
        raise inst._retryable_error_class()("timeout after 300s")

    monkeypatch.setattr(WecomCliAdapter, "run", _boom)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    with pytest.raises(Exception) as ei:
        inst.auth_login()
    assert not isinstance(ei.value, NameError), "重试逻辑不得被 NameError 击穿"
    assert len(attempts) == 3, f"应重试 3 次，实际 {len(attempts)} 次"


def test_feishu_adapter_implements_core():
    # 飞书适配器已实现核心能力（不应再是 NotImplementedError 桩）。
    inst = FeishuCliAdapter(dry_run=False)
    # 引擎钩子已落地
    assert isinstance(inst._build_command(["im", "+x"]), list)
    assert inst._classify_error('{"error":{"code":99991663}}') is inst._permission_error_class()
    # 能力方法已落地：缺目标时抛 ValueError（业务校验），而非 NotImplementedError
    with pytest.raises(ValueError):
        inst.chat_message_send(text="hi")
    # 联系人 / 下载方法存在且非桩
    assert callable(inst.contact_user_get_self)
    assert callable(inst.download_media)
