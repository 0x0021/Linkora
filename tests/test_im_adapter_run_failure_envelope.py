"""P2 回归：CLI 失败外壳归一化（dws v1.0.68 升级后系统性静默失败修复）。

背景：钉钉 dws 在 v1.0.68-beta.1 把失败外壳从 ``{"success": false, ...}`` 改成
``{"ok": false, "outcome": "failure", ...}`` 或裸 ``{"error": {...}}``，原 ``run()``
只认 ``success`` 字段 → CLI 报错被当成成功原样返回、下游静默取到空结果。

本文件固化：
- ``_extract_cli_failure`` 对三种失败外壳都能提取原因串；
- 成功响应（含 dws 裸数据 / 飞书 wecom 的 errcode/code/msg 外壳）**不**被误判；
- ``run()`` 在 exit 0 + 新失败外壳时正确抛异常（而非返回数据）。
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from src.im_adapter.base import BaseIMAdapter
from src.im_adapter.errors import IMAdapterError
from src.dws_adapter import DwsAdapter


def _failure(data: dict) -> str | None:
    return BaseIMAdapter._extract_cli_failure(data)


# ── _extract_cli_failure 单元 ──────────────────────────────────────────────
def test_old_success_false_envelope():
    msg = _failure({"success": False, "error": {"message": "boom", "category": "api"}})
    assert msg is not None and "boom" in msg and "category=api" in msg


def test_new_ok_false_envelope():
    msg = _failure({"ok": False, "outcome": "failure",
                    "error": {"message": "bad", "type": "validation"}})
    assert msg is not None and "bad" in msg and "type=validation" in msg


def test_bare_error_envelope():
    msg = _failure({"error": {"message": "api fail", "code": 1, "category": "api"}})
    assert msg is not None and "api fail" in msg and "code=1" in msg


def test_success_ok_true_not_flagged():
    assert _failure({"ok": True, "outcome": "success", "result": {"x": 1}}) is None


def test_success_success_true_not_flagged():
    assert _failure({"success": True, "result": {"x": 1}}) is None


def test_bare_data_success_not_flagged():
    # dws 成功常返回裸数据（无 result/success/ok 包裹）
    assert _failure({"conversationMessagesList": [{"messages": []}]}) is None
    assert _failure({"complete": True, "conversations": []}) is None
    assert _failure({"complete": False, "count": 5, "groups": []}) is None


def test_errorcode_null_success_shape_not_flagged():
    # message search-advanced 成功外壳：errorCode/errorMsg 为 null，非 error
    assert _failure({"errorCode": None, "errorMsg": None,
                     "result": {"conversationMessagesList": []}}) is None


def test_empty_or_null_error_not_flagged():
    assert _failure({"error": {}}) is None
    assert _failure({"error": None}) is None


def test_feishu_success_not_flagged():
    # 飞书 lark-cli 成功外壳用 code/data/msg
    assert _failure({"code": 0, "data": {"a": 1}, "msg": "ok"}) is None


def test_wecom_success_not_flagged():
    # 企业微信成功外壳用 errcode/errmsg
    assert _failure({"errcode": 0, "errmsg": "ok"}) is None


def test_non_dict_returns_none():
    assert _failure([{"ok": False}]) is None
    assert _failure("not a dict") is None


# ── run() 集成（mock subprocess.run） ─────────────────────────────────────
def _make_dws() -> DwsAdapter:
    return DwsAdapter(dry_run=False, retries=0)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


def test_run_raises_on_new_failure_envelope():
    env = {"ok": False, "outcome": "failure",
           "error": {"message": "validation", "type": "validation"}}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        try:
            dws.run(["chat", "message", "send", "--conversation-id", "cidX", "--content", "x"])
            raise AssertionError("应抛异常")
        except IMAdapterError as e:
            assert "validation" in str(e)


def test_run_raises_on_bare_error_envelope():
    env = {"error": {"message": "api error", "category": "api", "code": 1}}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        try:
            dws.run(["chat", "message", "reply", "--conversation-id", "cidX",
                     "--ref-msg-id", "m", "--ref-sender", "s", "--content", "x"])
            raise AssertionError("应抛异常")
        except IMAdapterError as e:
            assert "api error" in str(e)


def test_run_returns_data_on_success_envelope():
    env = {"ok": True, "outcome": "success", "result": {"sent": True}}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        data = dws.run(["chat", "message", "send", "--conversation-id", "cidX", "--content", "x"])
    # run() 返回原始 data（不拆 ok/outcome），由调用方 _get_result 解 result
    assert data.get("ok") is True and data.get("result", {}).get("sent") is True


def test_run_returns_bare_success_data():
    env = {"complete": True, "conversations": [{"conversationName": "g"}]}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        data = dws.run(["chat", "+conversation-list-top"])
    assert data.get("conversations") == [{"conversationName": "g"}]


def test_run_returns_feishu_success_unflagged():
    env = {"code": 0, "data": {"chat_id": "oc_1"}, "msg": "ok"}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        data = dws.run(["chat", "message", "send"])
    assert data.get("code") == 0


def test_run_returns_wecom_success_unflagged():
    env = {"errcode": 0, "errmsg": "ok"}
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed(json.dumps(env))):
        data = dws.run(["chat", "message", "send"])
    assert data.get("errcode") == 0


def test_run_still_raises_on_nonzero_exit():
    dws = _make_dws()
    with patch("src.im_adapter.base.subprocess.run",
               return_value=_completed("", returncode=2)):
        try:
            dws.run(["chat", "message", "send"])
            raise AssertionError("非零退出应抛异常")
        except IMAdapterError:
            pass
