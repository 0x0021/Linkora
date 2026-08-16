"""平台隔离日志过滤测试。

验证：
- classify_log_platform 按 logger 名映射平台 id；
- resolve_log_platform 五级级联（extra→ContextVar→线程名→logger前缀→内容特征）；
- InMemoryLogHandler emit 时打平台戳，get_records/count 在 platform 过滤下：
  仅隐藏「其它平台专属」日志，中性（共享核心/LLM 等）日志始终保留。
"""

import logging

import pytest

from src.utils.logger import (
    InMemoryLogHandler,
    classify_log_platform,
    log_platform_scope,
    resolve_log_platform,
)


def _mk(logger, levelno=20, rid=1):
    return {
        "id": rid,
        "ts": "2026-07-27 00:00:00",
        "level": "INFO",
        "levelno": levelno,
        "logger": logger,
        "message": "msg-" + logger,
    }


def _filled_handler():
    h = InMemoryLogHandler(maxlen=1000)
    h._buffer.append(_mk("src.dws_adapter", rid=1))          # dingtalk
    h._buffer.append(_mk("src.approval.dingtalk", rid=2))    # dingtalk
    h._buffer.append(_mk("src.im_adapter.feishu", rid=3))    # feishu
    h._buffer.append(_mk("src.im_adapter.feishu_doc_mixin", rid=4))  # feishu
    h._buffer.append(_mk("src.im_adapter.wecom", rid=5))     # wecom
    h._buffer.append(_mk("src.llm.router", rid=6))           # 中性
    h._buffer.append(_mk("src.poller_core_parse", rid=7))     # 中性
    h._next_id = 8
    return h


@pytest.mark.parametrize("logger,expected", [
    ("src.dws_adapter", "dingtalk"),
    ("src.approval.dingtalk", "dingtalk"),
    ("src.im_adapter.feishu", "feishu"),
    ("src.im_adapter.feishu_doc_mixin", "feishu"),
    ("src.kb.feishu_importer", "feishu"),
    ("src.im_adapter.wecom", "wecom"),
    # CLI 二进制名（某些路径下 logger 名即为工具名）
    ("lark-cli", "feishu"),
    ("DWS", "dingtalk"),
    ("wecom-cli", "wecom"),
    # 中性 / 共享模块
    ("src.llm.router", None),
    ("src.poller_core_parse", None),
    ("src.rule_engine", None),
    ("src.im_adapter.base", None),
    ("", None),
])
def test_classify_log_platform(logger, expected):
    assert classify_log_platform(logger) == expected


def test_filter_all_returns_everything():
    h = _filled_handler()
    recs = h.get_records(platform="all")
    assert len(recs) == 7
    assert h.count(platform="all") == 7


def test_filter_dingtalk_hides_other_platforms_but_keeps_neutral():
    h = _filled_handler()
    recs = h.get_records(platform="dingtalk")
    loggers = {r["logger"] for r in recs}
    # dingtalk 专属 + 中性（共享），无 feishu/wecom
    assert "src.dws_adapter" in loggers
    assert "src.llm.router" in loggers
    assert "src.poller_core_parse" in loggers
    assert "src.im_adapter.feishu" not in loggers
    assert "src.im_adapter.wecom" not in loggers
    assert len(recs) == 4  # 2 dingtalk + 2 neutral


def test_filter_feishu_hides_dingtalk_and_wecom():
    h = _filled_handler()
    recs = h.get_records(platform="feishu")
    loggers = {r["logger"] for r in recs}
    assert "src.im_adapter.feishu" in loggers
    assert "src.dws_adapter" not in loggers
    assert "src.im_adapter.wecom" not in loggers
    assert "src.llm.router" in loggers  # 中性保留
    assert len(recs) == 4  # 2 feishu + 2 neutral


def test_default_param_no_filter():
    h = _filled_handler()
    # platform 缺省（None）等价于不过滤
    assert len(h.get_records()) == 7
    assert len(h.get_records(platform=None)) == 7


def test_filter_combines_with_level_and_since():
    h = _filled_handler()
    # 仅 ERROR 以上 + dingtalk：只有 dingtalk/中性 中 error 级
    h._buffer.append(_mk("src.dws_adapter", levelno=40, rid=8))   # dingtalk ERROR
    h._buffer.append(_mk("src.im_adapter.wecom", levelno=40, rid=9))  # wecom ERROR (隐藏)
    h._next_id = 10
    recs = h.get_records(level_no=40, platform="dingtalk")
    loggers = {r["logger"] for r in recs}
    assert "src.dws_adapter" in loggers
    assert "src.im_adapter.wecom" not in loggers
    assert len(recs) == 1


def test_filter_cli_tool_names():
    """CLI 二进制名（lark-cli / DWS / wecom-cli）应被正确归类并过滤。"""
    h = InMemoryLogHandler(maxlen=1000)
    h._buffer.append(_mk("lark-cli", rid=1))       # feishu
    h._buffer.append(_mk("DWS", rid=2))             # dingtalk
    h._buffer.append(_mk("wecom-cli", rid=3))       # wecom
    h._buffer.append(_mk("src.llm.router", rid=4))  # 中性
    h._next_id = 5

    # 钉钉视图：无 lark-cli/wecom-cli，有 DWS + 中性
    dt = h.get_records(platform="dingtalk")
    assert {r["logger"] for r in dt} == {"DWS", "src.llm.router"}
    assert len(dt) == 2

    # 飞书视图：有 lark-cli + 中性，无 DWS/wecom-cli
    fs = h.get_records(platform="feishu")
    assert {r["logger"] for r in fs} == {"lark-cli", "src.llm.router"}
    assert len(fs) == 2

    # 企微视图：有 wecom-cli + 中性
    wc = h.get_records(platform="wecom")
    assert {r["logger"] for r in wc} == {"wecom-cli", "src.llm.router"}


# ---------- 写入时平台戳：resolve_log_platform 五级级联 ----------

def _record(logger="src.some.shared", msg="普通消息", thread_name="MainThread", **extra):
    rec = logging.LogRecord(
        name=logger, level=logging.INFO, pathname="x.py", lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    rec.threadName = thread_name
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_resolve_explicit_extra_wins():
    """显式 extra={"platform": ...} 优先级最高。"""
    rec = _record(platform="wecom", thread_name="poller-feishu")
    assert resolve_log_platform(rec) == "wecom"


def test_resolve_contextvar_beats_thread_name():
    """日志平台 ContextVar 优先于线程名（防抖/重放路径的显式标记）。"""
    rec = _record(thread_name="poller-dingtalk")
    with log_platform_scope("feishu"):
        assert resolve_log_platform(rec) == "feishu"
    # 作用域外回落到线程名推断
    assert resolve_log_platform(rec) == "dingtalk"


def test_resolve_by_poller_thread_name():
    """poller-<pid> 线程内的**共享模块**日志按线程名归属——修复漏过的主路径。"""
    # im_adapter.base 在 poller-feishu 线程替 lark-cli 干活 → 归飞书
    rec = _record(logger="src.im_adapter.base", msg="执行失败",
                  thread_name="poller-feishu")
    assert resolve_log_platform(rec) == "feishu"
    # LLM 共享模块在 poller-wecom 线程 → 归企微
    rec2 = _record(logger="src.llm.router", thread_name="poller-wecom")
    assert resolve_log_platform(rec2) == "wecom"


def test_resolve_by_logger_prefix():
    rec = _record(logger="src.dws_adapter")
    assert resolve_log_platform(rec) == "dingtalk"
    # doc_sync_scheduler 是钉钉专属（新增映射）
    rec2 = _record(logger="src.doc_sync_scheduler")
    assert resolve_log_platform(rec2) == "dingtalk"


def test_resolve_by_content_markers():
    """logger 中性 + 非 poller 线程时，按消息内容 CLI 特征词兜底。"""
    rec = _record(logger="src.im_adapter.base",
                  msg="/Users/x/.local/bin/lark-cli 未知错误: timeout")
    assert resolve_log_platform(rec) == "feishu"
    rec2 = _record(logger="src.im_adapter.base",
                   msg="/Users/x/.local/bin/dws 未知错误: business_error")
    assert resolve_log_platform(rec2) == "dingtalk"
    rec3 = _record(logger="src.poller_strategy", msg="wecom-cli 重试 2 次")
    assert resolve_log_platform(rec3) == "wecom"


def test_resolve_content_multi_hit_stays_neutral():
    """同一消息命中多个平台特征 → 无法唯一归属，保持中性。"""
    rec = _record(msg="切换适配器: dws -> lark-cli")
    assert resolve_log_platform(rec) is None


def test_resolve_content_word_boundary():
    """dws 需词边界匹配：dwsomething / midws 不误伤。"""
    assert resolve_log_platform(_record(msg="dwsomething 初始化")) is None
    assert resolve_log_platform(_record(msg="midws 模块加载")) is None
    assert resolve_log_platform(_record(msg="调用 dws profile x")) == "dingtalk"


def test_resolve_neutral_default():
    """启动/Web/调度器日志：无任何特征 → 中性。"""
    assert resolve_log_platform(_record(msg="Web 服务已启动 :8000")) is None


def test_emit_stamps_platform_and_filter_uses_stamp():
    """emit 写入时打戳；过滤优先用戳——共享模块日志按处理链路精确隔离。"""
    h = InMemoryLogHandler(maxlen=100)
    # 模拟：poller-feishu 线程里 im_adapter.base 打的错误（旧版会漏到钉钉视图）
    h.emit(_record(logger="src.im_adapter.base", msg="lark-cli 未知错误",
                   thread_name="poller-feishu"))
    # 模拟：处理钉钉消息链路中的 LLM 日志（ContextVar 标记）
    with log_platform_scope("dingtalk"):
        h.emit(_record(logger="src.llm.router", msg="路由到主模型"))
    # 模拟：启动日志（中性）
    h.emit(_record(logger="src.platform.lifecycle", msg="服务已启动"))

    recs = h.get_records(platform="all")
    assert [r["platform"] for r in recs] == ["feishu", "dingtalk", None]

    dt = h.get_records(platform="dingtalk")
    msgs = [r["message"] for r in dt]
    assert "lark-cli 未知错误" not in msgs          # 飞书链路日志被隔离
    assert "路由到主模型" in msgs                    # 钉钉链路的共享模块日志保留
    assert "服务已启动" in msgs                      # 中性保留

    fs = h.get_records(platform="feishu")
    msgs_fs = [r["message"] for r in fs]
    assert "lark-cli 未知错误" in msgs_fs
    assert "路由到主模型" not in msgs_fs


def test_legacy_records_without_stamp_fall_back():
    """升级前缓冲区里的旧记录（无 platform 键）回退 logger 名归类。"""
    h = InMemoryLogHandler(maxlen=100)
    h._buffer.append(_mk("src.im_adapter.feishu", rid=1))  # 旧格式，无 platform 键
    h._buffer.append(_mk("src.llm.router", rid=2))
    h._next_id = 3
    dt = h.get_records(platform="dingtalk")
    assert {r["logger"] for r in dt} == {"src.llm.router"}


def test_logs_endpoint_honors_platform():
    """端到端：/api/logs?platform=feishu 只返回飞书专属 + 中性日志。"""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from web.api import app
    from src.utils.logger import get_log_buffer

    client = TestClient(app)
    buf = get_log_buffer()
    start_id = buf.max_id()
    # 注入两条不同平台专属日志 + 一条中性
    buf._buffer.append(_mk("src.dws_adapter", rid=start_id + 1))       # dingtalk
    buf._buffer.append(_mk("src.im_adapter.feishu", rid=start_id + 2))  # feishu
    buf._buffer.append(_mk("src.im_adapter.wecom", rid=start_id + 3))   # wecom
    buf._buffer.append(_mk("src.llm.router", rid=start_id + 4))        # 中性
    buf._next_id = start_id + 5

    # 仅验证平台过滤行为，鉴权/RBAC 不在本测试范围：
    # - _auth_check 恒真跳过凭据校验；
    # - _get_cfg 返回与请求头凭据（test:test）匹配的配置，使 RBAC 角色判定为 admin，
    #   敏感端点 /api/logs 的「仅 admin」检查放行（RBAC 行为由 test_web_auth 覆盖）。
    class _Web:
        auth_enabled = True
        auth_username = "test"
        auth_password = "test"

    class _Cfg:
        web = _Web()

    with patch("web.api._auth_check", return_value=True), \
            patch("web.api._get_cfg", return_value=_Cfg()):
        # feishu 视图：飞书专属 + 中性，无 dingtalk/wecom
        resp = client.get(
            f"/api/logs?platform=feishu&since={start_id}",
            headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        )
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        loggers = {r["logger"] for r in logs}
        assert "src.im_adapter.feishu" in loggers
        assert "src.llm.router" in loggers
        assert "src.dws_adapter" not in loggers
        assert "src.im_adapter.wecom" not in loggers

        # all 视图：全部包含
        resp_all = client.get(
            f"/api/logs?platform=all&since={start_id}",
            headers={"Authorization": "Basic dGVzdDp0ZXN0"},
        )
        all_loggers = {r["logger"] for r in resp_all.json()["logs"]}
        assert "src.dws_adapter" in all_loggers
        assert "src.im_adapter.feishu" in all_loggers
        assert "src.im_adapter.wecom" in all_loggers
