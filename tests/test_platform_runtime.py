"""Phase 3 运行期多平台隔离单元测试。

不构造完整 LinkoraEngine（嵌入后台线程/技能引擎等过重），改用轻量 harness 直接装配
self.platforms 注册表，验证：
- 四个属性（store/dws/poller/llm_agent）按运行期平台上下文正确解析；
- _make_platform_callback 进入/退出时设置与复位 ContextVar；
- handle_message 记录消息所属平台，供 Timer 线程还原上下文；
- _process_pending_messages 在 Timer 线程中还原平台上下文；
- reload_config 遍历所有平台分别更新其组件。
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from main import LinkoraEngine, PlatformContext, _active_platform_ctx
# 拆分后 SQLiteStore / LLMAgent / MessagePoller / load_config / set_config 等符号
# 被各 mixin 通过 `from .base import *` 复制到自身命名空间（star-import 副本）。
# patch 必须指向真实使用方模块（primary / runtime）才能生效。
from src.platform import primary as platform_primary, runtime as platform_runtime
from src.platform import runtime_lifecycle  # reload_config 真实使用方模块（load_config/set_config 经 star-import 落在此命名空间）
from src.models import Message


def _make_message(chat_id="c1", sender_id="s1", content="你好"):
    return Message(
        msg_id="m1",
        chat_id=chat_id,
        chat_type="single",
        chat_name="测试",
        sender_id=sender_id,
        sender_name="张三",
        content=content,
        msg_type="text",
        timestamp=None,
        raw={},
    )


def _make_ctx(pid):
    return PlatformContext(
        id=pid, display_name=pid, enabled=True, adapter_type=pid,
        store=None, dws=None, poller=None, llm_agent=None, config=None,
    )


def _harness(platforms):
    bot = object.__new__(LinkoraEngine)
    bot.platforms = {p: _make_ctx(p) for p in platforms}
    bot.config_path = "config.yaml"
    bot._running = False
    bot._shutdown_event = MagicMock()
    return bot


def test_platform_context_dataclass():
    ctx = _make_ctx("feishu")
    assert ctx.id == "feishu"
    assert ctx.store is None
    assert ctx.enabled is True


def test_properties_resolve_to_active_platform():
    bot = _harness(["dingtalk", "feishu"])
    feishu_store = MagicMock()
    feishu_dws = MagicMock()
    feishu_poller = MagicMock()
    feishu_llm = MagicMock()
    bot.platforms["feishu"].store = feishu_store
    bot.platforms["feishu"].dws = feishu_dws
    bot.platforms["feishu"].poller = feishu_poller
    bot.platforms["feishu"].llm_agent = feishu_llm

    token = _active_platform_ctx.set("feishu")
    try:
        assert bot.store is feishu_store
        assert bot.dws is feishu_dws
        assert bot.poller is feishu_poller
        assert bot.llm_agent is feishu_llm
    finally:
        _active_platform_ctx.reset(token)

    # 无上下文时回退主平台
    assert bot.store is bot.platforms["dingtalk"].store
    assert bot.dws is bot.platforms["dingtalk"].dws


def test_make_platform_callback_sets_and_restores_context():
    bot = _harness(["dingtalk", "feishu"])
    feishu_store = MagicMock()
    bot.platforms["feishu"].store = feishu_store

    seen = {}

    def spy(msg):
        seen["platform"] = _active_platform_ctx.get()
        seen["store"] = bot.store

    bot.handle_message = spy
    wrapper = bot._make_platform_callback("feishu")

    assert _active_platform_ctx.get() == "dingtalk"
    wrapper(MagicMock())
    assert seen["platform"] == "feishu"
    assert seen["store"] is feishu_store
    # 退出后上下文复位
    assert _active_platform_ctx.get() == "dingtalk"


def _seed_timer_state(bot):
    bot._pending_messages = {}
    bot._pending_timers = {}
    bot._pending_platform = {}
    bot._timer_lock = threading.Lock()
    bot._pending_first_seen = {}
    bot._pending_incomplete_wait = {}
    bot._incomplete_delay_count = 0
    bot._incomplete_extra_sec = 0.0
    bot._incomplete_fired_with_request = 0
    bot._incomplete_fired_without_request = 0
    bot.config = SimpleNamespace(poller=SimpleNamespace(reply_cooldown_seconds=5))


def test_handle_message_records_pending_platform():
    bot = _harness(["dingtalk", "feishu"])
    _seed_timer_state(bot)
    token = _active_platform_ctx.set("feishu")
    try:
        msg = _make_message()
        bot.handle_message(msg)
    finally:
        _active_platform_ctx.reset(token)

    key = (msg.chat_id, msg.sender_id)
    assert bot._pending_platform[key] == "feishu"
    # 取消定时器，避免测试结束后触发真实处理
    bot._pending_timers[key].cancel()


def test_process_pending_messages_restores_context():
    bot = _harness(["dingtalk", "feishu"])
    bot.platforms["feishu"].store = MagicMock()
    bot.platforms["feishu"].poller = MagicMock()
    bot._running = True  # 需要设置为 True 才能让方法正常执行
    _seed_timer_state(bot)

    key = ("c1", "s1")
    msg = _make_message()
    bot._pending_messages[key] = [msg]
    bot._pending_platform[key] = "feishu"

    seen = {}

    def fake_impl(m):
        seen["platform"] = _active_platform_ctx.get()
        seen["store"] = bot.store

    bot._handle_message_impl = fake_impl
    bot._process_pending_messages(key)

    assert seen["platform"] == "feishu"
    assert seen["store"] is bot.platforms["feishu"].store
    # 退出后复位
    assert _active_platform_ctx.get() == "dingtalk"


def _fake_config(retention=10):
    return SimpleNamespace(
        storage=SimpleNamespace(decisions_retention_days=retention),
        poller=SimpleNamespace(interval_seconds=5, max_concurrent_replies=3),
        llm=SimpleNamespace(model="m", base_url="http://x"),
        skills=object(),
        rules=object(),
        tools=SimpleNamespace(available=["a", "b"]),
        llm_throttle=object(),
        embedding=SimpleNamespace(enabled=True, provider="p", offline=False, model="m"),
        dws=SimpleNamespace(timeout=30, retries=2, dry_run=True, profile=""),
        web=SimpleNamespace(host="127.0.0.1"),
    )


def test_reload_config_updates_all_platforms():
    bot = _harness(["dingtalk", "feishu", "wecom"])
    for pid in bot.platforms:
        bot.platforms[pid].store = MagicMock()
        bot.platforms[pid].dws = MagicMock()
        bot.platforms[pid].poller = MagicMock()
        bot.platforms[pid].llm_agent = MagicMock()
        bot.platforms[pid].config = MagicMock()
    bot.llm_client = MagicMock()
    bot.rule_engine = MagicMock()
    bot.tool_router = MagicMock()
    bot._bg_throttle = MagicMock()
    emb = SimpleNamespace(enabled=True, provider="p", offline=False, model="m")
    bot.embedding_client = MagicMock()
    bot.embedding_client.config = emb
    bot.embedding_client.enabled = True

    old_cfg = _fake_config(retention=10)
    new_cfg = _fake_config(retention=99)
    bot.config = old_cfg

    with patch.object(platform_primary, "load_config", return_value=new_cfg), \
         patch.object(runtime_lifecycle, "load_config", return_value=new_cfg), \
         patch.object(platform_primary, "set_config"), \
         patch.object(runtime_lifecycle, "set_config"), \
         patch.object(bot, "_compute_tool_whitelist_drift", return_value={}), \
         patch.object(bot, "_build_adapter", side_effect=lambda pcfg: MagicMock()) as mk_adapter, \
         patch.object(bot, "_rebuild_kb_search_tool"):
        bot.reload_config()

    # 每个平台的 store 都更新了 retention
    for pid in ("dingtalk", "feishu", "wecom"):
        bot.platforms[pid].store.set_decisions_retention_days.assert_called_with(99)
    # poller / llm_agent 配置同步
    assert bot.platforms["feishu"].poller.config is new_cfg.poller
    assert bot.platforms["wecom"].llm_agent.config is new_cfg.llm
    assert bot.platforms["wecom"].llm_agent.skills_config is new_cfg.skills
    # 共享组件
    assert bot.llm_client.config is new_cfg.llm
    assert bot.tool_router.config is new_cfg.tools
    assert bot._bg_throttle.cfg is new_cfg.llm_throttle
    # 每个平台都重建了 adapter
    assert mk_adapter.call_count == 3


def test_run_launches_per_platform_poller_threads(monkeypatch):
    """run() 应为每个启用平台启动独立轮询线程，并在主线程等待关闭信号。"""
    bot = _harness(["dingtalk", "feishu"])
    bot.config = _fake_config(retention=10)
    # 仅 dingtalk 启用
    bot.platforms["feishu"].enabled = False
    for pid in bot.platforms:
        bot.platforms[pid].poller = MagicMock()
        bot.platforms[pid].poller.run_loop.side_effect = lambda *a, **k: None

    bot.doc_sync_scheduler = MagicMock()
    bot.db_backup = None
    bot._bg_threads = []
    bot._start_memory_cleanup_scheduler = lambda: MagicMock()
    bot._start_conversation_summary_scheduler = lambda: MagicMock()
    bot._start_decision_cleanup_scheduler = lambda: MagicMock()
    bot._start_messages_cleanup_scheduler = lambda: MagicMock()
    # 主线程立即触发关闭，避免无限等待
    bot._shutdown_event = threading.Event()
    bot._running = True
    bot._shutdown_event.set()

    # 阻止真实信号处理与 web 启动
    started = []
    orig_thread = threading.Thread

    def fake_thread(*args, **kwargs):
        t = orig_thread(*args, **kwargs)
        started.append(kwargs.get("name") or args)
        return t

    monkeypatch.setattr("main.signal.signal", lambda *a, **k: None)
    monkeypatch.setattr(threading, "Thread", fake_thread)

    try:
        bot.run(web_port=0)
    except Exception:
        pass

    assert any("poller-dingtalk" in (s or "") for s in started)
    assert not any("poller-feishu" in (s or "") for s in started)


def _wecom_pcfg(cli_path, dry_run=False):
    return SimpleNamespace(
        id="wecom", display_name="企业微信", adapter_type="wecom",
        adapter=SimpleNamespace(cli_path=cli_path, timeout=30, retries=2,
                                dry_run=dry_run, profile=None),
    )


def test_build_adapter_wecom_degrades_when_cli_missing():
    """企微 CLI 缺失时应自动降级为 dry_run，避免每轮轮询报错（镜像飞书）。"""
    bot = object.__new__(LinkoraEngine)
    bot.config = _fake_config()
    with patch("main.os.path.isfile", return_value=False), \
         patch("main.shutil.which", return_value=None):
        adapter = bot._build_adapter(_wecom_pcfg("/no/such/wecom-cli"))
    assert adapter.dry_run is True


def test_build_adapter_wecom_uses_configured_dry_run_when_cli_present():
    """企微 CLI 存在时透传配置的 dry_run（不触发降级）。"""
    bot = object.__new__(LinkoraEngine)
    bot.config = _fake_config()
    # 指向真实存在的文件，绕过降级守卫
    adapter = bot._build_adapter(_wecom_pcfg(sys.executable, dry_run=False))
    assert adapter.dry_run is False
    adapter2 = bot._build_adapter(_wecom_pcfg(sys.executable, dry_run=True))
    assert adapter2.dry_run is True


def test_build_platform_context_wecom_resolves_self_user_id():
    """_build_platform_context 对企微应解析自身 user_id 用于自我消息过滤，
    而非错误地沿用钉钉 ID（否则可能自循环回复）。"""
    bot = object.__new__(LinkoraEngine)
    bot.config = _fake_config()
    bot.current_open_dingtalk_id = "dt_openid"
    bot.current_user_id = "dt_user"
    bot.current_user_name = "徐"
    bot.current_user_dept = "总裁办"
    bot.current_user_org = "公司"
    bot.llm_client = MagicMock()
    bot.tool_router = MagicMock()
    bot._skill_manager = MagicMock()
    bot.store = MagicMock()  # fallback_store
    bot.rule_engine = MagicMock()

    pcfg = SimpleNamespace(
        id="wecom", display_name="企业微信", adapter_type="wecom",
        storage=SimpleNamespace(path="./data/wecom-ai.db"),
        poller=SimpleNamespace(max_concurrent_replies=3),
        adapter=SimpleNamespace(cli_path="wecom-cli", timeout=30, retries=2,
                                dry_run=False, profile=None),
        llm=MagicMock(), rag=None, tools=None,
    )
    fake_adapter = MagicMock()
    fake_adapter.contact_user_get_self.return_value = {"user_id": "wecom_u1", "name": "我"}

    sb = MagicMock()
    la = MagicMock()
    mp = MagicMock()
    with patch.object(platform_primary, "SQLiteStore", sb), \
         patch.object(platform_runtime, "SQLiteStore", sb), \
         patch.object(bot, "_build_adapter", return_value=fake_adapter), \
         patch.object(platform_primary, "LLMAgent", la), \
         patch.object(platform_runtime, "LLMAgent", la), \
         patch.object(platform_primary, "MessagePoller", mp), \
         patch.object(platform_runtime, "MessagePoller", mp):
        bot._build_platform_context(pcfg)

    _, kwargs = mp.call_args
    # poller 应以企微自身 user_id 作为 current_user_id（覆盖钉钉 id）
    assert kwargs["current_user_id"] == "wecom_u1"
    assert kwargs["current_user_user_id"] == "wecom_u1"


def test_build_platform_context_wecom_aggregates_title():
    """_build_platform_context 对企微应把自身职位聚合进 current_user_title
    （供系统提示认知 owner 岗位，避免误编'转给IT'）。"""
    bot = object.__new__(LinkoraEngine)
    bot.config = _fake_config()
    bot.current_open_dingtalk_id = "dt_openid"
    bot.current_user_id = "dt_user"
    bot.current_user_name = "徐"
    bot.current_user_dept = "总裁办"
    bot.current_user_org = "公司"
    bot.current_user_title = ""
    bot.llm_client = MagicMock()
    bot.tool_router = MagicMock()
    bot._skill_manager = MagicMock()
    bot.store = MagicMock()
    bot.rule_engine = MagicMock()

    pcfg = SimpleNamespace(
        id="wecom", display_name="企业微信", adapter_type="wecom",
        storage=SimpleNamespace(path="./data/wecom-ai.db"),
        poller=SimpleNamespace(max_concurrent_replies=3),
        adapter=SimpleNamespace(cli_path="wecom-cli", timeout=30, retries=2,
                                dry_run=False, profile=None),
        llm=MagicMock(), rag=None, tools=None,
    )
    fake_adapter = MagicMock()
    fake_adapter.contact_user_get_self.return_value = {
        "user_id": "wecom_u1", "name": "我", "title": "企微产品经理",
    }
    sb = MagicMock()
    la = MagicMock()
    mp = MagicMock()
    with patch.object(platform_primary, "SQLiteStore", sb), \
         patch.object(platform_runtime, "SQLiteStore", sb), \
         patch.object(bot, "_build_adapter", return_value=fake_adapter), \
         patch.object(platform_primary, "LLMAgent", la), \
         patch.object(platform_runtime, "LLMAgent", la), \
         patch.object(platform_primary, "MessagePoller", mp), \
         patch.object(platform_runtime, "MessagePoller", mp):
        bot._build_platform_context(pcfg)
    assert bot.current_user_title == "企微产品经理"


def test_load_current_user_extracts_title():
    """_load_current_user 应从钉钉 orgEmployeeModel 抽取职位字段。"""
    bot = object.__new__(LinkoraEngine)
    bot.dws = MagicMock()
    bot.dws._get_current_profile_local.return_value = {
        "userId": "u1", "userName": "徐", "corpName": "公司",
    }
    bot.dws.contact_user_get_self.return_value = {
        "orgEmployeeModel": {
            "depts": [{"deptName": "总裁办"}],
            "title": "IT",
            "orgUserName": "OWNER",
            "orgName": "公司",
        }
    }
    info = bot._load_current_user()
    assert info["title"] == "IT"
    assert info["dept"] == "总裁办"


def test_merge_platform_title_prefers_existing():
    """_merge_platform_title 仅当当前为空时采用（钉钉主平台优先）。"""
    bot = object.__new__(LinkoraEngine)
    bot.current_user_title = "钉钉IT"
    bot._merge_platform_title("企微产品经理")
    assert bot.current_user_title == "钉钉IT"
    bot.current_user_title = ""
    bot._merge_platform_title("   ")
    assert bot.current_user_title == ""
    bot._merge_platform_title("企微产品经理")
    assert bot.current_user_title == "企微产品经理"
