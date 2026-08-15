"""ConfigManageTool 二次确认（仅 update）与类型强转健壮性测试。

回归防护：
- require_confirm 此前名存实亡：config_manage 写 config.yaml 却未加确认门控；
  现改为仅 action=='update' 需确认，'view' 只读放行（needs_confirm 钩子）。
- 类型强转 `value.lower()` 在 LLM 传 JSON bool/int 时会崩；现先归一化为字符串。
"""
from __future__ import annotations

import io
import os as _real_os
import tempfile as _real_tempfile
from pathlib import Path as _real_path
import yaml as _real_yaml
from unittest.mock import MagicMock

import src.tools.management as mgt
from src.tools.management import ConfigManageTool


def _patch_fs(monkeypatch, config_dict):
    """把 config_manage 的磁盘读写全部桩掉，只验证内存中的强转逻辑。"""
    cfg = MagicMock()
    monkeypatch.setattr(mgt, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(_real_yaml, "safe_load", lambda *a, **k: config_dict)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(""))
    monkeypatch.setattr(_real_tempfile, "mkstemp", lambda **k: (1, "/tmp/x"))
    monkeypatch.setattr(_real_os, "replace", lambda *a, **k: None)
    monkeypatch.setattr(_real_os.path, "exists", lambda *a, **k: False)
    monkeypatch.setattr(_real_os.path, "dirname", lambda *a, **k: "/tmp")
    monkeypatch.setattr(_real_os.path, "abspath", lambda *a, **k: "/tmp/c.yaml")


class TestConfirmGate:
    def test_view_does_not_require_confirm(self):
        t = ConfigManageTool()
        assert t.needs_confirm({"action": "view"}) is False

    def test_update_requires_confirm(self):
        t = ConfigManageTool()
        assert t.needs_confirm({
            "action": "update", "section": "llm", "key": "temperature", "value": "0.7"
        }) is True

    def test_preview_update(self):
        t = ConfigManageTool()
        pv = t.build_confirmation_preview({
            "action": "update", "section": "llm", "key": "temperature", "value": "0.7"
        })
        assert "llm.temperature" in pv
        assert "0.7" in pv


class TestTypeCoercion:
    def test_json_bool_value(self, monkeypatch):
        # 用仓库内完整且合法的 config.yaml.example 作基础配置：update 现在写入前会做
        # AppConfig.model_validate 预校验以防损坏 config.yaml，字段不全的极简 dict 会被拒绝。
        example_path = _real_path(__file__).parent.parent / "config.yaml.example"
        config_dict = _real_yaml.safe_load(example_path.read_text(encoding="utf-8"))
        _patch_fs(monkeypatch, config_dict)
        t = ConfigManageTool()
        # LLM 传 JSON bool（非字符串）：旧代码 value.lower() 会崩，新代码应正确解析
        res = t.execute({
            "action": "update", "section": "tools", "key": "enabled", "value": True
        })
        assert res.get("success") is True
        assert config_dict["tools"]["enabled"] is True

    def test_bad_int_returns_error(self, monkeypatch):
        config_dict = {
            "llm": {"temperature": 0.5},
            "tools": {"enabled": True},
            "embedding": {"enabled": True},
        }
        _patch_fs(monkeypatch, config_dict)
        t = ConfigManageTool()
        res = t.execute({
            "action": "update", "section": "llm", "key": "temperature", "value": "abc"
        })
        assert "error" in res
