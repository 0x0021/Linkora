"""src.audit 审计日志模块单测。"""
from __future__ import annotations

import json

import pytest

from src import audit as audit_mod
from src.audit import audit, get_audit_log_path, set_audit_log_path


@pytest.fixture
def audit_file(tmp_path):
    p = tmp_path / "audit.log"
    set_audit_log_path(p)
    yield p
    # 恢复默认，避免污染其它测试
    set_audit_log_path(None)


def test_audit_writes_jsonl(audit_file):
    audit("tool_execution", "transfer_approval", "success",
          actor="张三", session_key="chat-1", target="transfer_approval",
          detail="duration_ms=12")
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "tool_execution"
    assert rec["action"] == "transfer_approval"
    assert rec["status"] == "success"
    assert rec["actor"] == "张三"
    assert rec["session_key"] == "chat-1"
    assert rec["target"] == "transfer_approval"
    assert "ts" in rec


def test_audit_optional_fields_omitted(audit_file):
    audit("config_write", "update_config", "success", actor="web",
          target="/path/config.yaml")
    rec = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[0])
    assert rec["session_key"] is None
    assert rec["detail"] == ""
    assert "meta" not in rec


def test_audit_meta_field(audit_file):
    audit("approval_transfer", "transfer_approval", "failed",
          actor="李四", target="inst-9", meta={"tasks": ["t1"]})
    rec = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[0])
    assert rec["meta"] == {"tasks": ["t1"]}


def test_audit_path_override(audit_file):
    # set_audit_log_path 已在 fixture 中把路径指向 audit_file
    assert get_audit_log_path() == audit_file
    audit("x", "y", "z")
    assert audit_file.exists()


def test_audit_emits_log_tag(audit_file, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="linkora.audit"):
        audit("tool_execution", "send_message", "success", target="send_message")
    assert any("[audit]" in r.message for r in caplog.records)


def test_audit_best_effort_on_unwritable_path(tmp_path, monkeypatch):
    # 指向一个不存在目录下的文件，且让 mkdir 失败 -> 不应抛异常
    bad = tmp_path / "no_such_dir" / "nope.log"
    set_audit_log_path(bad)
    # 即便目录无法创建，audit 也应吞掉异常不向上抛
    audit("tool_execution", "x", "success")
    set_audit_log_path(None)


def test_audit_never_raises_on_bad_meta(tmp_path):
    p = tmp_path / "a.log"
    set_audit_log_path(p)
    # meta 含不可 JSON 序列对象 -> default=str 兜底，不抛
    class Weird:
        def __str__(self):
            return "weird"
    audit("e", "a", "s", meta={"w": Weird()})
    assert p.exists()
    rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert rec["meta"]["w"] == "weird"
    set_audit_log_path(None)
